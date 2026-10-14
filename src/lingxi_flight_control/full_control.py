"""完整驾驶舱模拟控制面。

本模块只维护进程内状态，所有会改变状态的动作都经过预览/确认，且
Runtime binding 同时绑定 actor、项目、机库、无人机、任务批次和上下文版本。
它不包含 MQTT、HTTP、真实飞控或生产任务适配器。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import math
import re
from typing import Any, Callable, Mapping
from uuid import uuid4

from .agent import ToolRegistry
from .models import (
    ExecutionContext,
    ToolDefinition,
    ToolParameter,
    ToolSchema,
    parameter_json_schema,
)
from .safety import SafetyFastPath, normalize
from .simulator import MemoryFlightSimulator

#: 精确安全短语表，与 SafetyFastPathDecider 共用同一份定义。
_SAFETY_FAST_PATH = SafetyFastPath()


class FlightControlAction(str, Enum):
    # control ownership
    CONTROL_ACQUIRE = "control_acquire"
    CONTROL_RELEASE = "control_release"
    REMOTE_REQUEST = "remote_request"
    REMOTE_APPROVE = "remote_approve"
    REMOTE_REJECT = "remote_reject"
    # input mode
    SET_KEYBOARD_MODE = "set_keyboard_mode"
    SET_JOYSTICK_MODE = "set_joystick_mode"
    SET_VIRTUAL_BUTTON = "set_virtual_button"
    # flight movement
    MOVE_FORWARD = "move_forward"
    MOVE_BACKWARD = "move_backward"
    MOVE_LEFT = "move_left"
    MOVE_RIGHT = "move_right"
    MOVE_UP = "move_up"
    MOVE_DOWN = "move_down"
    TURN_LEFT = "turn_left"
    TURN_RIGHT = "turn_right"
    # safety
    EMERGENCY_STOP = "emergency_stop"
    TAKEOFF = "takeoff"
    LAND = "land"
    LAND_ALTERNATE = "land_alternate"
    RETURN_HOME = "return_home"
    CANCEL_RETURN_HOME = "cancel_return_home"
    # route
    ROUTE_PAUSE = "route_pause"
    ROUTE_RESUME = "route_resume"
    CONTINUE_FROM_BREAKPOINT = "continue_from_breakpoint"
    POINT_FLIGHT = "point_flight"
    POINT_ORBIT = "point_orbit"
    UPLOAD_ROUTE = "upload_route"
    EXECUTE_ROUTE = "execute_route"
    # parameters
    SET_MOVE_SPEED = "set_move_speed"
    SET_ROLL_SPEED = "set_roll_speed"
    SET_PITCH_SPEED = "set_pitch_speed"
    SET_VERTICAL_SPEED = "set_vertical_speed"
    SET_YAW_SPEED = "set_yaw_speed"
    RESET_PARAMETERS = "reset_parameters"
    # camera / gimbal
    CAMERA_ELECTRO_OPTICAL = "camera_electro_optical"
    CAMERA_ZOOM = "camera_zoom"
    CAMERA_INFRARED = "camera_infrared"
    CAMERA_WIDE = "camera_wide"
    GIMBAL_UP = "gimbal_up"
    GIMBAL_DOWN = "gimbal_down"
    GIMBAL_LEFT = "gimbal_left"
    GIMBAL_RIGHT = "gimbal_right"
    GIMBAL_DIGITAL_ZOOM = "gimbal_digital_zoom"
    GIMBAL_RESET = "gimbal_reset"
    GIMBAL_45 = "gimbal_45"
    GIMBAL_90 = "gimbal_90"
    CAMERA_LOCK_FOCUS = "camera_lock_focus"
    CAMERA_PANORAMA = "camera_panorama"
    CAMERA_PHOTO = "camera_photo"
    CAMERA_START_RECORDING = "camera_start_recording"
    CAMERA_STOP_RECORDING = "camera_stop_recording"
    # infrared / payload
    INFRARED_SET_LIMITS = "infrared_set_limits"
    PAYLOAD_MEGAPHONE = "payload_megaphone"
    PAYLOAD_SEARCHLIGHT = "payload_searchlight"
    PAYLOAD_METHANE = "payload_methane"
    PAYLOAD_DROPPER = "payload_dropper"
    PAYLOAD_DYNAMIC = "payload_dynamic"
    # state
    READ_STATE = "read_state"


class FlightControlStatus(str, Enum):
    PREVIEW_REQUIRED = "preview_required"
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    STALE_CONTEXT = "stale_context"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REJECTED = "rejected"


_MOVES = {
    FlightControlAction.MOVE_FORWARD, FlightControlAction.MOVE_BACKWARD,
    FlightControlAction.MOVE_LEFT, FlightControlAction.MOVE_RIGHT,
    FlightControlAction.MOVE_UP, FlightControlAction.MOVE_DOWN,
}

#: 这些动作不需要先持有控制权。急停只停止运动、不会引入新的风险，
#: 而且恰恰是「控制权不在自己手上」时最需要它——要求先握手会延误处置。
#: 返航/降落会驱动自主飞行，与他人操控可能冲突，因此仍要求持有控制权。
_CONTROL_FREE_ACTIONS = frozenset({
    FlightControlAction.CONTROL_ACQUIRE,
    FlightControlAction.REMOTE_REQUEST,
    FlightControlAction.REMOTE_APPROVE,
    FlightControlAction.REMOTE_REJECT,
    FlightControlAction.EMERGENCY_STOP,
})


@dataclass(frozen=True)
class RuntimeBinding:
    actor_id: str
    project_id: str
    hangar_id: str
    drone_id: str
    task_batch_id: str
    context_version: int

    def validate(self) -> None:
        for name in ("actor_id", "project_id", "hangar_id", "drone_id", "task_batch_id"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"{name} is required")
        if isinstance(self.context_version, bool) or not isinstance(self.context_version, int):
            raise ValueError("context_version must be an integer")
        if self.context_version < 0:
            raise ValueError("context_version must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "actor_id": self.actor_id,
            "project_id": self.project_id,
            "hangar_id": self.hangar_id,
            "drone_id": self.drone_id,
            "task_batch_id": self.task_batch_id,
            "context_version": self.context_version,
        }


@dataclass(frozen=True)
class FlightControlActionRequest:
    command_id: str
    idempotency_key: str
    action: FlightControlAction | str
    parameters: Mapping[str, Any] = field(default_factory=dict)
    context_version: int | None = None

    def validate(self) -> None:
        if not self.command_id.strip() or not self.idempotency_key.strip():
            raise ValueError("command_id and idempotency_key are required")
        try:
            action = FlightControlAction(self.action)
        except (TypeError, ValueError) as exc:
            raise ValueError("unsupported flight control action") from exc
        if not isinstance(self.parameters, Mapping):
            raise ValueError("parameters must be an object")
        if isinstance(self.context_version, bool) or (
            self.context_version is not None and not isinstance(self.context_version, int)
        ):
            raise ValueError("context_version must be an integer")
        _validate_parameters(action, self.parameters)

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "command_id": self.command_id,
            "idempotency_key": self.idempotency_key,
            "action": FlightControlAction(self.action).value,
            "parameters": dict(self.parameters),
            "context_version": self.context_version,
        }


@dataclass
class FlightControlState:
    execution_mode: str = "simulator"
    revision: int = 0
    context_version: int = 0
    control_owner: str | None = None
    control_request: str = "none"
    operation_mode: str = "keyboard"
    virtual_buttons: dict[str, str] = field(default_factory=dict)
    altitude_m: float = 0.0
    heading_deg: float = 0.0
    gimbal_pitch_deg: float = 0.0
    gimbal_yaw_deg: float = 0.0
    air_speed_mps: float = 0.0
    ground_speed_mps: float = 0.0
    vertical_speed_mps: float = 0.0
    battery_percent: float = 100.0
    cycle_count: int = 0
    return_distance_m: float = 0.0
    roll_deg: float = 0.0
    pitch_deg: float = 0.0
    yaw_deg: float = 0.0
    longitude: float = 0.0
    latitude: float = 0.0
    sea_level_altitude_m: float = 0.0
    task_status: str = "idle"
    route_status: str = "idle"
    route_id: str | None = None
    breakpoint_index: int | None = None
    parameters: dict[str, float] = field(default_factory=lambda: {
        "move_speed_mps": 5.0,
        "roll_speed_dps": 30.0,
        "pitch_speed_dps": 30.0,
        "vertical_speed_mps": 3.0,
        "yaw_speed_dps": 45.0,
    })
    camera: dict[str, Any] = field(default_factory=lambda: {
        "mode": "electro_optical", "zoom": 1.0, "digital_zoom": 1.0,
        "focus_locked": False, "recording": False, "photo_count": 0,
        "panorama_count": 0,
    })
    infrared: dict[str, Any] = field(default_factory=lambda: {
        "enabled": False, "min_c": -20.0, "max_c": 150.0, "last_temperature_c": None,
    })
    payloads: dict[str, Any] = field(default_factory=dict)
    last_action: str | None = None
    last_command_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_mode": "simulator",
            "revision": self.revision,
            "context_version": self.context_version,
            "control_owner": self.control_owner,
            "control_request": self.control_request,
            "operation_mode": self.operation_mode,
            "virtual_buttons": dict(self.virtual_buttons),
            "altitude_m": self.altitude_m,
            "heading_deg": self.heading_deg,
            "gimbal_pitch_deg": self.gimbal_pitch_deg,
            "gimbal_yaw_deg": self.gimbal_yaw_deg,
            "air_speed_mps": self.air_speed_mps,
            "ground_speed_mps": self.ground_speed_mps,
            "vertical_speed_mps": self.vertical_speed_mps,
            "battery_percent": self.battery_percent,
            "cycle_count": self.cycle_count,
            "return_distance_m": self.return_distance_m,
            "roll_deg": self.roll_deg,
            "pitch_deg": self.pitch_deg,
            "yaw_deg": self.yaw_deg,
            "longitude": self.longitude,
            "latitude": self.latitude,
            "sea_level_altitude_m": self.sea_level_altitude_m,
            "task_status": self.task_status,
            "route_status": self.route_status,
            "route_id": self.route_id,
            "breakpoint_index": self.breakpoint_index,
            "parameters": dict(self.parameters),
            "camera": dict(self.camera),
            "infrared": dict(self.infrared),
            "payloads": dict(self.payloads),
            "last_action": self.last_action,
            "last_command_id": self.last_command_id,
        }


@dataclass(frozen=True)
class FlightControlReceipt:
    status: FlightControlStatus
    execution_mode: str
    action: str
    command_id: str
    idempotency_key: str
    confirmation_id: str | None
    confirmation_binding: str | None
    context_version: int
    state: Mapping[str, Any]
    error_code: str | None = None
    message: str = ""
    expires_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "execution_mode": "simulator",
            "action": self.action,
            "command_id": self.command_id,
            "idempotency_key": self.idempotency_key,
            "confirmation_id": self.confirmation_id,
            "confirmation_binding": self.confirmation_binding,
            "context_version": self.context_version,
            "state": dict(self.state),
            "error_code": self.error_code,
            "message": self.message,
            "expires_at": self.expires_at,
        }


@dataclass
class _Pending:
    request: FlightControlActionRequest
    binding: RuntimeBinding
    confirmation_id: str
    confirmation_binding: str
    expires_at: float
    status: FlightControlStatus = FlightControlStatus.PREVIEW_REQUIRED


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


def _json_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _number(value: Any, name: str, low: float | None = None, high: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if low is not None and number < low or high is not None and number > high:
        raise ValueError(f"{name} is out of range")
    return number


def _validate_parameters(action: FlightControlAction, parameters: Mapping[str, Any]) -> None:
    required: dict[FlightControlAction, tuple[str, ...]] = {
        FlightControlAction.SET_VIRTUAL_BUTTON: ("button", "action"),
        FlightControlAction.MOVE_FORWARD: ("distance_m",),
        FlightControlAction.MOVE_BACKWARD: ("distance_m",),
        FlightControlAction.MOVE_LEFT: ("distance_m",),
        FlightControlAction.MOVE_RIGHT: ("distance_m",),
        FlightControlAction.MOVE_UP: ("distance_m",),
        FlightControlAction.MOVE_DOWN: ("distance_m",),
        FlightControlAction.TURN_LEFT: ("angle_deg",),
        FlightControlAction.TURN_RIGHT: ("angle_deg",),
        FlightControlAction.TAKEOFF: ("altitude_m",),
        FlightControlAction.POINT_FLIGHT: ("longitude", "latitude", "altitude_m"),
        FlightControlAction.POINT_ORBIT: ("longitude", "latitude", "radius_m"),
        FlightControlAction.UPLOAD_ROUTE: ("route_id", "waypoints"),
        FlightControlAction.SET_MOVE_SPEED: ("value",),
        FlightControlAction.SET_ROLL_SPEED: ("value",),
        FlightControlAction.SET_PITCH_SPEED: ("value",),
        FlightControlAction.SET_VERTICAL_SPEED: ("value",),
        FlightControlAction.SET_YAW_SPEED: ("value",),
        FlightControlAction.CAMERA_ZOOM: ("value",),
        FlightControlAction.GIMBAL_DIGITAL_ZOOM: ("value",),
        FlightControlAction.INFRARED_SET_LIMITS: ("min_c", "max_c"),
        FlightControlAction.PAYLOAD_DYNAMIC: ("payload", "enabled"),
    }
    optional: dict[FlightControlAction, tuple[str, ...]] = {
        FlightControlAction.CONTINUE_FROM_BREAKPOINT: ("index",),
        FlightControlAction.PAYLOAD_DROPPER: ("payload",),
        FlightControlAction.PAYLOAD_MEGAPHONE: ("enabled",),
        FlightControlAction.PAYLOAD_SEARCHLIGHT: ("enabled",),
        FlightControlAction.PAYLOAD_METHANE: ("enabled",),
    }
    allowed = set(required.get(action, ())) | set(optional.get(action, ()))
    extras = sorted(set(parameters) - allowed)
    if extras:
        raise ValueError(f"{action.value} does not accept parameters: {extras}")
    for field_name in required.get(action, ()):
        if field_name not in parameters:
            raise ValueError(f"{action.value} requires {field_name}")
    if action in {
        FlightControlAction.MOVE_FORWARD, FlightControlAction.MOVE_BACKWARD,
        FlightControlAction.MOVE_LEFT, FlightControlAction.MOVE_RIGHT,
        FlightControlAction.MOVE_UP, FlightControlAction.MOVE_DOWN,
    }:
        _number(parameters["distance_m"], "distance_m", 0.01, 1000)
    if action in {FlightControlAction.TURN_LEFT, FlightControlAction.TURN_RIGHT}:
        _number(parameters["angle_deg"], "angle_deg", 0.01, 360)
    if action == FlightControlAction.TAKEOFF:
        _number(parameters["altitude_m"], "altitude_m", 1, 500)
    if action in {
        FlightControlAction.SET_MOVE_SPEED, FlightControlAction.SET_VERTICAL_SPEED,
    }:
        _number(parameters["value"], "value", 0.1, 50)
    if action in {
        FlightControlAction.SET_ROLL_SPEED, FlightControlAction.SET_PITCH_SPEED,
        FlightControlAction.SET_YAW_SPEED,
    }:
        _number(parameters["value"], "value", 0.1, 360)
    if action in {FlightControlAction.CAMERA_ZOOM, FlightControlAction.GIMBAL_DIGITAL_ZOOM}:
        _number(parameters["value"], "value", 1, 30)
    if action == FlightControlAction.INFRARED_SET_LIMITS:
        low = _number(parameters["min_c"], "min_c", -100, 500)
        high = _number(parameters["max_c"], "max_c", -100, 500)
        if low >= high:
            raise ValueError("min_c must be less than max_c")
    if action == FlightControlAction.UPLOAD_ROUTE:
        if not isinstance(parameters["route_id"], str) or not parameters["route_id"].strip():
            raise ValueError("route_id is required")
        if not isinstance(parameters["waypoints"], list) or not parameters["waypoints"]:
            raise ValueError("waypoints must be a non-empty array")
    if action == FlightControlAction.POINT_ORBIT:
        _number(parameters["radius_m"], "radius_m", 1, 5000)
    if action in {FlightControlAction.POINT_FLIGHT, FlightControlAction.POINT_ORBIT}:
        _number(parameters["longitude"], "longitude", -180, 180)
        _number(parameters["latitude"], "latitude", -90, 90)


def _status_receipt(status: FlightControlStatus, request: FlightControlActionRequest,
                    binding: RuntimeBinding, state: FlightControlState, *,
                    confirmation_id: str | None = None, confirmation_binding: str | None = None,
                    error_code: str | None = None, message: str = "", expires_at: float | None = None) -> FlightControlReceipt:
    return FlightControlReceipt(
        status=status,
        execution_mode="simulator",
        action=FlightControlAction(request.action).value,
        command_id=request.command_id,
        idempotency_key=request.idempotency_key,
        confirmation_id=confirmation_id,
        confirmation_binding=confirmation_binding,
        context_version=binding.context_version,
        state=state.to_dict(),
        error_code=error_code,
        message=message,
        expires_at=None if expires_at is None else datetime.fromtimestamp(expires_at, timezone.utc).isoformat(),
    )


def _context_for(binding: RuntimeBinding) -> ExecutionContext:
    """由运行时绑定派生执行上下文，作为唯一真源，避免两处状态漂移。"""
    return ExecutionContext(
        execution_id=f"{binding.project_id}:{binding.hangar_id}:{binding.task_batch_id}",
        drone_id=binding.drone_id,
        context_version=binding.context_version,
        confirmed=True,
    )


class MemoryFlightControlSimulator(MemoryFlightSimulator):
    """纯内存完整控制面；不会触碰旧的真实控制接口。"""

    def __init__(self, binding: RuntimeBinding, *, clock=None, ttl_sec: float = 120.0):
        binding.validate()
        # Keep the original MemoryFlightSimulator API available while adding
        # the complete action/state model around the same in-memory boundary.
        super().__init__(_context_for(binding))
        self.binding = binding
        self.state = FlightControlState(context_version=binding.context_version)
        self._clock = clock or _now
        self._ttl_sec = float(ttl_sec)
        self._pending: dict[str, _Pending] = {}
        self._by_key: dict[str, FlightControlReceipt] = {}

    def runtime_context(self) -> ExecutionContext:
        """始终由当前绑定派生。

        父类把它存成独立字段，换绑后不同步就等于过期上下文保护失效——
        检查点会在换了无人机之后仍被当成新鲜的可执行上下文。
        """
        context = _context_for(self.binding)
        context.validate()
        return context

    def snapshot(self) -> dict[str, Any]:
        return {
            "execution_mode": "simulator",
            "binding": self.binding.to_dict(),
            "state": self.state.to_dict(),
        }

    def bind(self, binding: RuntimeBinding) -> None:
        binding.validate()
        if binding == self.binding:
            return
        self._pending.clear()
        self.binding = binding
        self.state = FlightControlState(context_version=binding.context_version)
        # 同步父类持有的执行上下文，保持两套 API 一致。
        self.context = _context_for(binding)

    def preview(self, request: FlightControlActionRequest) -> FlightControlReceipt:
        request.validate()
        if request.context_version is not None and request.context_version != self.binding.context_version:
            return _status_receipt(FlightControlStatus.STALE_CONTEXT, request, self.binding, self.state,
                                    error_code="STALE_CONTEXT", message="上下文版本已经变化")
        if request.idempotency_key in self._by_key:
            previous = self._by_key[request.idempotency_key]
            if previous.context_version != self.binding.context_version:
                return _status_receipt(FlightControlStatus.STALE_CONTEXT, request, self.binding, self.state,
                                        error_code="STALE_CONTEXT", message="幂等键属于旧上下文")
            return _status_receipt(FlightControlStatus.DUPLICATE, request, self.binding, self.state,
                                    confirmation_id=previous.confirmation_id,
                                    confirmation_binding=previous.confirmation_binding,
                                    message="重复幂等键，未再次执行")
        for existing in self._pending.values():
            if existing.request.idempotency_key == request.idempotency_key:
                return _status_receipt(FlightControlStatus.DUPLICATE, request, self.binding, self.state,
                                        confirmation_id=existing.confirmation_id,
                                        confirmation_binding=existing.confirmation_binding,
                                        message="重复幂等键，已有待确认动作")
        action = FlightControlAction(request.action)
        if action == FlightControlAction.READ_STATE:
            receipt = _status_receipt(FlightControlStatus.ACCEPTED, request, self.binding, self.state,
                                       message="读取模拟状态")
            self._by_key[request.idempotency_key] = receipt
            return receipt
        if action not in _CONTROL_FREE_ACTIONS and self.state.control_owner != self.binding.actor_id:
            return _status_receipt(FlightControlStatus.REJECTED, request, self.binding, self.state,
                                   error_code="CONTROL_NOT_OWNED", message="请先获取模拟控制权")
        confirmation_id = uuid4().hex
        binding_hash = _json_hash({"binding": self.binding.to_dict(), "request": request.to_dict()})
        pending = _Pending(request, self.binding, confirmation_id, binding_hash,
                           float(self._clock()) + self._ttl_sec)
        self._pending[confirmation_id] = pending
        return _status_receipt(FlightControlStatus.PREVIEW_REQUIRED, request, self.binding, self.state,
                               confirmation_id=confirmation_id, confirmation_binding=binding_hash,
                               message="动作已预览，等待确认", expires_at=pending.expires_at)

    def confirm(self, confirmation_id: str, *, command_id: str, idempotency_key: str,
                confirmation_binding: str, context_version: int) -> FlightControlReceipt:
        request = FlightControlActionRequest(command_id, idempotency_key, FlightControlAction.READ_STATE, {}, context_version)
        if context_version != self.binding.context_version:
            return _status_receipt(FlightControlStatus.STALE_CONTEXT, request, self.binding, self.state,
                                    error_code="STALE_CONTEXT", message="上下文版本已经变化")
        pending = self._pending.get(confirmation_id)
        if pending is None:
            previous = self._by_key.get(idempotency_key)
            if previous is not None and previous.context_version == self.binding.context_version:
                return _status_receipt(FlightControlStatus.DUPLICATE, request, self.binding, self.state,
                                        confirmation_id=previous.confirmation_id,
                                        confirmation_binding=previous.confirmation_binding,
                                        message="动作已经处理，未再次执行")
            return _status_receipt(FlightControlStatus.REJECTED, request, self.binding, self.state,
                                   error_code="CONFIRMATION_NOT_FOUND", message="确认卡不存在")
        request = pending.request
        if command_id != request.command_id or idempotency_key != request.idempotency_key or confirmation_binding != pending.confirmation_binding:
            return _status_receipt(FlightControlStatus.REJECTED, request, self.binding, self.state,
                                   confirmation_id=confirmation_id, confirmation_binding=pending.confirmation_binding,
                                   error_code="CONFIRMATION_MISMATCH", message="确认绑定不匹配")
        if float(self._clock()) >= pending.expires_at:
            pending.status = FlightControlStatus.EXPIRED
            return _status_receipt(FlightControlStatus.EXPIRED, request, self.binding, self.state,
                                   confirmation_id=confirmation_id, confirmation_binding=pending.confirmation_binding,
                                   error_code="CONFIRMATION_EXPIRED", message="确认卡已经过期")
        result = self._apply(request)
        result = _status_receipt(FlightControlStatus.ACCEPTED, request, self.binding, self.state,
                                 confirmation_id=confirmation_id, confirmation_binding=pending.confirmation_binding,
                                 message="模拟动作已执行")
        pending.status = FlightControlStatus.ACCEPTED
        self._pending.pop(confirmation_id, None)
        self._by_key[idempotency_key] = result
        return result

    def cancel(self, confirmation_id: str, *, command_id: str, idempotency_key: str,
               confirmation_binding: str, context_version: int) -> FlightControlReceipt:
        request = FlightControlActionRequest(command_id, idempotency_key, FlightControlAction.READ_STATE, {}, context_version)
        pending = self._pending.get(confirmation_id)
        if context_version != self.binding.context_version:
            return _status_receipt(FlightControlStatus.STALE_CONTEXT, request, self.binding, self.state,
                                    error_code="STALE_CONTEXT", message="上下文版本已经变化")
        if pending is None:
            return _status_receipt(FlightControlStatus.REJECTED, request, self.binding, self.state,
                                   error_code="CONFIRMATION_NOT_FOUND", message="确认卡不存在")
        if float(self._clock()) >= pending.expires_at:
            self._pending.pop(confirmation_id, None)
            return _status_receipt(FlightControlStatus.EXPIRED, pending.request, self.binding, self.state,
                                   confirmation_id=confirmation_id,
                                   confirmation_binding=pending.confirmation_binding,
                                   error_code="CONFIRMATION_EXPIRED", message="确认卡已经过期")
        if confirmation_binding != pending.confirmation_binding or command_id != pending.request.command_id or idempotency_key != pending.request.idempotency_key:
            return _status_receipt(FlightControlStatus.REJECTED, pending.request, self.binding, self.state,
                                   confirmation_id=confirmation_id, confirmation_binding=pending.confirmation_binding,
                                   error_code="CONFIRMATION_MISMATCH", message="确认绑定不匹配")
        pending.status = FlightControlStatus.CANCELLED
        self._pending.pop(confirmation_id, None)
        return _status_receipt(FlightControlStatus.CANCELLED, pending.request, self.binding, self.state,
                               confirmation_id=confirmation_id, confirmation_binding=pending.confirmation_binding,
                               message="模拟动作已取消")

    def _apply(self, request: FlightControlActionRequest) -> None:
        action = FlightControlAction(request.action)
        p = request.parameters
        s = self.state
        if action == FlightControlAction.CONTROL_ACQUIRE:
            s.control_owner = self.binding.actor_id
        elif action == FlightControlAction.CONTROL_RELEASE:
            s.control_owner = None
        elif action == FlightControlAction.REMOTE_REQUEST:
            s.control_request = "pending"
        elif action == FlightControlAction.REMOTE_APPROVE:
            s.control_request = "approved"
        elif action == FlightControlAction.REMOTE_REJECT:
            s.control_request = "rejected"
        elif action == FlightControlAction.SET_KEYBOARD_MODE:
            s.operation_mode = "keyboard"
        elif action == FlightControlAction.SET_JOYSTICK_MODE:
            s.operation_mode = "joystick"
        elif action == FlightControlAction.SET_VIRTUAL_BUTTON:
            s.virtual_buttons[str(p["button"])] = str(p["action"])
        elif action in {
            FlightControlAction.MOVE_FORWARD, FlightControlAction.MOVE_BACKWARD,
            FlightControlAction.MOVE_LEFT, FlightControlAction.MOVE_RIGHT,
            FlightControlAction.MOVE_UP, FlightControlAction.MOVE_DOWN,
        }:
            distance = float(p["distance_m"])
            if action == FlightControlAction.MOVE_FORWARD: s.latitude += distance / 111_000
            if action == FlightControlAction.MOVE_BACKWARD: s.latitude -= distance / 111_000
            if action == FlightControlAction.MOVE_LEFT: s.longitude -= distance / 111_000
            if action == FlightControlAction.MOVE_RIGHT: s.longitude += distance / 111_000
            if action == FlightControlAction.MOVE_UP: s.altitude_m += distance
            if action == FlightControlAction.MOVE_DOWN: s.altitude_m = max(0.0, s.altitude_m - distance)
            s.ground_speed_mps = float(s.parameters["move_speed_mps"])
        elif action == FlightControlAction.TURN_LEFT:
            s.heading_deg = (s.heading_deg - float(p["angle_deg"])) % 360
            s.yaw_deg = s.heading_deg
        elif action == FlightControlAction.TURN_RIGHT:
            s.heading_deg = (s.heading_deg + float(p["angle_deg"])) % 360
            s.yaw_deg = s.heading_deg
        elif action == FlightControlAction.EMERGENCY_STOP:
            s.task_status = "emergency_stopped"
            s.ground_speed_mps = 0.0
            s.vertical_speed_mps = 0.0
        elif action == FlightControlAction.TAKEOFF:
            s.altitude_m = float(p["altitude_m"])
            s.task_status = "flying"
            s.cycle_count += 1
        elif action in {FlightControlAction.LAND, FlightControlAction.LAND_ALTERNATE}:
            s.altitude_m = 0.0
            s.task_status = "landed" if action == FlightControlAction.LAND else "alternate_landed"
        elif action == FlightControlAction.RETURN_HOME:
            s.task_status = "returning_home"
        elif action == FlightControlAction.CANCEL_RETURN_HOME:
            s.task_status = "flying"
        elif action == FlightControlAction.ROUTE_PAUSE:
            s.route_status = "paused"
        elif action == FlightControlAction.ROUTE_RESUME:
            s.route_status = "running"
        elif action == FlightControlAction.CONTINUE_FROM_BREAKPOINT:
            s.route_status = "running"
            s.breakpoint_index = int(p.get("index", s.breakpoint_index or 0))
        elif action == FlightControlAction.POINT_FLIGHT:
            s.longitude, s.latitude, s.altitude_m = float(p["longitude"]), float(p["latitude"]), float(p["altitude_m"])
            s.route_status = "point_flight"
        elif action == FlightControlAction.POINT_ORBIT:
            s.route_status = "point_orbit"
            s.longitude, s.latitude = float(p["longitude"]), float(p["latitude"])
        elif action == FlightControlAction.UPLOAD_ROUTE:
            s.route_id = str(p["route_id"])
            s.route_status = "uploaded"
        elif action == FlightControlAction.EXECUTE_ROUTE:
            s.route_status = "running"
            s.task_status = "route_executing"
        elif action == FlightControlAction.RESET_PARAMETERS:
            s.parameters = FlightControlState().parameters
        elif action in {
            FlightControlAction.SET_MOVE_SPEED, FlightControlAction.SET_ROLL_SPEED,
            FlightControlAction.SET_PITCH_SPEED, FlightControlAction.SET_VERTICAL_SPEED,
            FlightControlAction.SET_YAW_SPEED,
        }:
            key = action.value.removeprefix("set_")
            key = {"move_speed": "move_speed_mps", "roll_speed": "roll_speed_dps",
                   "pitch_speed": "pitch_speed_dps", "vertical_speed": "vertical_speed_mps",
                   "yaw_speed": "yaw_speed_dps"}[key]
            s.parameters[key] = float(p["value"])
        elif action in {FlightControlAction.CAMERA_ELECTRO_OPTICAL, FlightControlAction.CAMERA_INFRARED, FlightControlAction.CAMERA_WIDE}:
            s.camera["mode"] = {FlightControlAction.CAMERA_ELECTRO_OPTICAL: "electro_optical",
                                 FlightControlAction.CAMERA_INFRARED: "infrared",
                                 FlightControlAction.CAMERA_WIDE: "wide"}[action]
            if action == FlightControlAction.CAMERA_INFRARED: s.infrared["enabled"] = True
        elif action == FlightControlAction.CAMERA_ZOOM: s.camera["zoom"] = float(p["value"])
        elif action == FlightControlAction.GIMBAL_DIGITAL_ZOOM: s.camera["digital_zoom"] = float(p["value"])
        elif action == FlightControlAction.GIMBAL_UP: s.gimbal_pitch_deg = min(90.0, s.gimbal_pitch_deg + 5)
        elif action == FlightControlAction.GIMBAL_DOWN: s.gimbal_pitch_deg = max(-90.0, s.gimbal_pitch_deg - 5)
        elif action == FlightControlAction.GIMBAL_LEFT: s.gimbal_yaw_deg -= 5
        elif action == FlightControlAction.GIMBAL_RIGHT: s.gimbal_yaw_deg += 5
        elif action == FlightControlAction.GIMBAL_RESET: s.gimbal_pitch_deg = s.gimbal_yaw_deg = 0.0
        elif action == FlightControlAction.GIMBAL_45: s.gimbal_pitch_deg = -45.0
        elif action == FlightControlAction.GIMBAL_90: s.gimbal_pitch_deg = -90.0
        elif action == FlightControlAction.CAMERA_LOCK_FOCUS: s.camera["focus_locked"] = True
        elif action == FlightControlAction.CAMERA_PANORAMA: s.camera["panorama_count"] += 1
        elif action == FlightControlAction.CAMERA_PHOTO: s.camera["photo_count"] += 1
        elif action == FlightControlAction.CAMERA_START_RECORDING: s.camera["recording"] = True
        elif action == FlightControlAction.CAMERA_STOP_RECORDING: s.camera["recording"] = False
        elif action == FlightControlAction.INFRARED_SET_LIMITS:
            s.infrared.update(enabled=True, min_c=float(p["min_c"]), max_c=float(p["max_c"]))
        elif action == FlightControlAction.PAYLOAD_DYNAMIC:
            s.payloads[str(p["payload"])] = bool(p["enabled"])
        elif action == FlightControlAction.PAYLOAD_DROPPER:
            s.payloads["dropper"] = {"last_drop": True, "payload": p.get("payload", "default")}
        elif action in {FlightControlAction.PAYLOAD_MEGAPHONE, FlightControlAction.PAYLOAD_SEARCHLIGHT, FlightControlAction.PAYLOAD_METHANE}:
            s.payloads[action.value.removeprefix("payload_")] = p.get("enabled", True)
        s.revision += 1
        s.last_action = action.value
        s.last_command_id = request.command_id


_TEXT_ACTIONS: tuple[tuple[str, FlightControlAction, dict[str, Any]], ...] = (
    ("急停", FlightControlAction.EMERGENCY_STOP, {}),
    ("取消返航", FlightControlAction.CANCEL_RETURN_HOME, {}),
    ("返航", FlightControlAction.RETURN_HOME, {}),
    ("起飞", FlightControlAction.TAKEOFF, {"altitude_m": 10}),
    ("降落", FlightControlAction.LAND, {}),
    ("暂停航线", FlightControlAction.ROUTE_PAUSE, {}),
    ("恢复航线", FlightControlAction.ROUTE_RESUME, {}),
    ("拍照", FlightControlAction.CAMERA_PHOTO, {}),
    ("开始录像", FlightControlAction.CAMERA_START_RECORDING, {}),
    ("停止录像", FlightControlAction.CAMERA_STOP_RECORDING, {}),
    ("打开红外", FlightControlAction.CAMERA_INFRARED, {}),
    ("广角", FlightControlAction.CAMERA_WIDE, {}),
)


#: 出现这些词说明用户在否定或犹疑，绝不能按肯定语义触发动作。
#: 「取消返航」这类本身是否定语义的动作由精确安全短语表先行命中，不走这里。
#: 文本动作的精确匹配表（键已归一化）。
#: 曾经用子串包含，结果「请勿急停」被判成急停、「勿返航」「禁止返航」「免降落」同理——
#: 否定说法无法穷举，靠维护一张否定词表是打地鼠。精确匹配让它结构上不可能命中。
_TEXT_ACTION_BY_PHRASE: dict[str, tuple[FlightControlAction, dict[str, Any]]] = {
    normalize(marker): (action, dict(parameters))
    for marker, action, parameters in _TEXT_ACTIONS
}


def parse_flight_control_text(text: str, *, command_id: str, idempotency_key: str,
                              context_version: int | None = None) -> FlightControlActionRequest:
    """把有限自然语言转成显式 action；无法唯一映射时直接拒绝。

    只接受**精确**指令（标点与空白不敏感）。任何多余的字——包括「帮我」「请」
    这类客套前缀和一切否定/犹疑说法——一律拒绝，因为把用户的否定读成肯定在飞控上是灾难。
    需要理解自然语言时应该交给模型，而不是让这个正则层去猜。
    """
    cleaned = str(text or "").strip()
    if not cleaned:
        raise ValueError("control text is required")

    safety_call = _SAFETY_FAST_PATH.recognize(cleaned)
    if safety_call is not None:
        return FlightControlActionRequest(command_id, idempotency_key,
                                          FlightControlAction(safety_call.name), {}, context_version)

    hit = _TEXT_ACTION_BY_PHRASE.get(normalize(cleaned))
    if hit is not None:
        action, parameters = hit
        return FlightControlActionRequest(command_id, idempotency_key, action,
                                          dict(parameters), context_version)

    # 含有已知动作词但不是精确指令（「帮我拍照」「请勿急停」）：明确拒绝，绝不猜语义。
    if any(marker in cleaned for marker, _, _ in _TEXT_ACTIONS):
        raise ValueError("control text must match one action exactly")

    move = re.fullmatch(r"(?:向?前|前进|往前)\s*(\d+(?:\.\d+)?)\s*米", cleaned)
    if move:
        return FlightControlActionRequest(command_id, idempotency_key, FlightControlAction.MOVE_FORWARD,
                                           {"distance_m": float(move.group(1))}, context_version)
    move_map = ((r"(?:向?后|后退|往后)\s*(\d+(?:\.\d+)?)\s*米", FlightControlAction.MOVE_BACKWARD),
                (r"(?:向?左|左移)\s*(\d+(?:\.\d+)?)\s*米", FlightControlAction.MOVE_LEFT),
                (r"(?:向?右|右移)\s*(\d+(?:\.\d+)?)\s*米", FlightControlAction.MOVE_RIGHT),
                (r"(?:上升|向?上)\s*(\d+(?:\.\d+)?)\s*米", FlightControlAction.MOVE_UP),
                (r"(?:下降|向?下)\s*(\d+(?:\.\d+)?)\s*米", FlightControlAction.MOVE_DOWN))
    for pattern, action in move_map:
        match = re.fullmatch(pattern, cleaned)
        if match:
            return FlightControlActionRequest(command_id, idempotency_key, action,
                                               {"distance_m": float(match.group(1))}, context_version)
    raise ValueError("cannot map control text to one explicit action")


def action_tool_names() -> tuple[str, ...]:
    return tuple(item.value for item in FlightControlAction)


# ---------------------------------------------------------------------------
# 动作元数据：manifest、工具定义和模型注册表共用的单一真源。
# ---------------------------------------------------------------------------

_P_DISTANCE = ToolParameter("number", minimum=0, maximum=1000, exclusive_minimum=True)
_P_ANGLE = ToolParameter("number", minimum=0, maximum=360, exclusive_minimum=True)
_P_ALTITUDE = ToolParameter("number", minimum=1, maximum=500)
_P_SPEED_MOVE = ToolParameter("number", minimum=0.1, maximum=50)
_P_SPEED_ANGLE = ToolParameter("number", minimum=0.1, maximum=360)
_P_ZOOM = ToolParameter("number", minimum=1, maximum=30)
_P_TEMPERATURE = ToolParameter("number", minimum=-100, maximum=500)
_P_RADIUS = ToolParameter("number", minimum=1, maximum=5000)
_P_LONGITUDE = ToolParameter("number", minimum=-180, maximum=180)
_P_LATITUDE = ToolParameter("number", minimum=-90, maximum=90)
_P_INDEX = ToolParameter("number", minimum=0)
_P_TEXT = ToolParameter("string", min_length=1)
_P_BUTTON_ACTION = ToolParameter("string", min_length=1)
_P_ENABLED = ToolParameter("boolean")
_P_WAYPOINTS = ToolParameter("array")


@dataclass(frozen=True)
class ActionSpec:
    """一个驾驶舱动作对模型可见的契约：领域、描述、参数与必填项。"""

    domain: str
    description: str
    parameters: Mapping[str, ToolParameter] = field(default_factory=dict)
    required: tuple[str, ...] = ()


def _spec(domain: str, description: str, parameters: Mapping[str, ToolParameter] | None = None,
          required: tuple[str, ...] = ()) -> ActionSpec:
    properties = dict(parameters or {})
    missing = [name for name in required if name not in properties]
    if missing:
        raise ValueError(f"required parameters are not declared: {missing}")
    # 必填项排在前面，manifest 的 parameter_names 顺序才有意义。
    ordered = {name: properties[name] for name in required}
    ordered.update({k: v for k, v in properties.items() if k not in ordered})
    return ActionSpec(domain, description, ordered, tuple(required))


ACTION_SPECS: dict[FlightControlAction, ActionSpec] = {
    # 控制权
    FlightControlAction.CONTROL_ACQUIRE: _spec("control", "获取无人机控制权；执行任何飞行动作前必须先获取"),
    FlightControlAction.CONTROL_RELEASE: _spec("control", "释放当前持有的控制权"),
    FlightControlAction.REMOTE_REQUEST: _spec("control", "向远端发起接管请求"),
    FlightControlAction.REMOTE_APPROVE: _spec("control", "同意远端的接管请求"),
    FlightControlAction.REMOTE_REJECT: _spec("control", "拒绝远端的接管请求"),
    FlightControlAction.SET_KEYBOARD_MODE: _spec("control", "切换为键盘操作模式"),
    FlightControlAction.SET_JOYSTICK_MODE: _spec("control", "切换为摇杆操作模式"),
    FlightControlAction.SET_VIRTUAL_BUTTON: _spec(
        "control", "绑定虚拟按键", {"button": _P_TEXT, "action": _P_BUTTON_ACTION},
        ("button", "action")),
    # 飞行
    FlightControlAction.MOVE_FORWARD: _spec("flight", "向机头正前方水平平移，不改变航向",
                                            {"distance_m": _P_DISTANCE}, ("distance_m",)),
    FlightControlAction.MOVE_BACKWARD: _spec("flight", "向机尾后方水平平移，不改变航向",
                                             {"distance_m": _P_DISTANCE}, ("distance_m",)),
    FlightControlAction.MOVE_LEFT: _spec("flight", "向机身左侧水平平移，不改变航向",
                                         {"distance_m": _P_DISTANCE}, ("distance_m",)),
    FlightControlAction.MOVE_RIGHT: _spec("flight", "向机身右侧水平平移，不改变航向",
                                          {"distance_m": _P_DISTANCE}, ("distance_m",)),
    FlightControlAction.MOVE_UP: _spec("flight", "垂直上升",
                                       {"distance_m": _P_DISTANCE}, ("distance_m",)),
    FlightControlAction.MOVE_DOWN: _spec("flight", "垂直下降",
                                         {"distance_m": _P_DISTANCE}, ("distance_m",)),
    FlightControlAction.TURN_LEFT: _spec("flight", "原地向左偏航转向，只改变航向不产生位移",
                                         {"angle_deg": _P_ANGLE}, ("angle_deg",)),
    FlightControlAction.TURN_RIGHT: _spec("flight", "原地向右偏航转向，只改变航向不产生位移",
                                          {"angle_deg": _P_ANGLE}, ("angle_deg",)),
    FlightControlAction.EMERGENCY_STOP: _spec("flight", "紧急停止所有运动"),
    FlightControlAction.TAKEOFF: _spec("flight", "起飞至指定高度",
                                       {"altitude_m": _P_ALTITUDE}, ("altitude_m",)),
    FlightControlAction.LAND: _spec("flight", "在当前点垂直降落"),
    FlightControlAction.LAND_ALTERNATE: _spec("flight", "飞往备降点降落"),
    FlightControlAction.RETURN_HOME: _spec("flight", "返航回到起飞点"),
    FlightControlAction.CANCEL_RETURN_HOME: _spec("flight", "取消正在进行的返航"),
    # 航线
    FlightControlAction.ROUTE_PAUSE: _spec("route", "暂停当前航线任务"),
    FlightControlAction.ROUTE_RESUME: _spec("route", "恢复被暂停的航线任务"),
    FlightControlAction.CONTINUE_FROM_BREAKPOINT: _spec("route", "从航线断点继续执行",
                                                        {"index": _P_INDEX}),
    FlightControlAction.POINT_FLIGHT: _spec(
        "route", "飞往指定的经纬度与高度",
        {"longitude": _P_LONGITUDE, "latitude": _P_LATITUDE, "altitude_m": _P_ALTITUDE},
        ("longitude", "latitude", "altitude_m")),
    FlightControlAction.POINT_ORBIT: _spec(
        "route", "以指定经纬度为中心盘旋",
        {"longitude": _P_LONGITUDE, "latitude": _P_LATITUDE, "radius_m": _P_RADIUS},
        ("longitude", "latitude", "radius_m")),
    FlightControlAction.UPLOAD_ROUTE: _spec(
        "route", "上传航线到无人机", {"route_id": _P_TEXT, "waypoints": _P_WAYPOINTS},
        ("route_id", "waypoints")),
    FlightControlAction.EXECUTE_ROUTE: _spec("route", "执行已经上传的航线"),
    # 飞行参数
    FlightControlAction.SET_MOVE_SPEED: _spec("parameters", "设置水平移动速度（米/秒）",
                                              {"value": _P_SPEED_MOVE}, ("value",)),
    FlightControlAction.SET_ROLL_SPEED: _spec("parameters", "设置横滚角速度（度/秒）",
                                              {"value": _P_SPEED_ANGLE}, ("value",)),
    FlightControlAction.SET_PITCH_SPEED: _spec("parameters", "设置俯仰角速度（度/秒）",
                                               {"value": _P_SPEED_ANGLE}, ("value",)),
    FlightControlAction.SET_VERTICAL_SPEED: _spec("parameters", "设置垂直升降速度（米/秒）",
                                                  {"value": _P_SPEED_MOVE}, ("value",)),
    FlightControlAction.SET_YAW_SPEED: _spec("parameters", "设置偏航角速度（度/秒）",
                                             {"value": _P_SPEED_ANGLE}, ("value",)),
    FlightControlAction.RESET_PARAMETERS: _spec("parameters", "恢复默认飞行参数"),
    # 相机与云台
    FlightControlAction.CAMERA_ELECTRO_OPTICAL: _spec("camera", "切换为可见光相机"),
    FlightControlAction.CAMERA_ZOOM: _spec("camera", "设置光学变焦倍数",
                                           {"value": _P_ZOOM}, ("value",)),
    FlightControlAction.CAMERA_INFRARED: _spec("camera", "切换为红外热成像镜头"),
    FlightControlAction.CAMERA_WIDE: _spec("camera", "切换为广角镜头"),
    FlightControlAction.GIMBAL_UP: _spec("camera", "云台向上转动"),
    FlightControlAction.GIMBAL_DOWN: _spec("camera", "云台向下转动"),
    FlightControlAction.GIMBAL_LEFT: _spec("camera", "云台向左转动"),
    FlightControlAction.GIMBAL_RIGHT: _spec("camera", "云台向右转动"),
    FlightControlAction.GIMBAL_DIGITAL_ZOOM: _spec("camera", "设置云台数字变焦倍数",
                                                   {"value": _P_ZOOM}, ("value",)),
    FlightControlAction.GIMBAL_RESET: _spec("camera", "云台回中复位"),
    FlightControlAction.GIMBAL_45: _spec("camera", "云台俯仰至 -45 度"),
    FlightControlAction.GIMBAL_90: _spec("camera", "云台俯仰至 -90 度"),
    FlightControlAction.CAMERA_LOCK_FOCUS: _spec("camera", "锁定对焦"),
    FlightControlAction.CAMERA_PANORAMA: _spec("camera", "全景拍摄"),
    FlightControlAction.CAMERA_PHOTO: _spec("camera", "拍摄单张照片"),
    FlightControlAction.CAMERA_START_RECORDING: _spec("camera", "开始录像"),
    FlightControlAction.CAMERA_STOP_RECORDING: _spec("camera", "停止录像"),
    # 红外
    FlightControlAction.INFRARED_SET_LIMITS: _spec(
        "infrared", "设置红外测温的下限与上限（摄氏度）",
        {"min_c": _P_TEMPERATURE, "max_c": _P_TEMPERATURE}, ("min_c", "max_c")),
    # 载荷
    FlightControlAction.PAYLOAD_MEGAPHONE: _spec("payload", "控制喊话器开关",
                                                 {"enabled": _P_ENABLED}),
    FlightControlAction.PAYLOAD_SEARCHLIGHT: _spec("payload", "控制探照灯开关",
                                                   {"enabled": _P_ENABLED}),
    FlightControlAction.PAYLOAD_METHANE: _spec("payload", "控制甲烷检测开关",
                                               {"enabled": _P_ENABLED}),
    FlightControlAction.PAYLOAD_DROPPER: _spec("payload", "控制投放器",
                                               {"payload": _P_TEXT}),
    FlightControlAction.PAYLOAD_DYNAMIC: _spec(
        "payload", "控制动态载荷开关", {"payload": _P_TEXT, "enabled": _P_ENABLED},
        ("payload", "enabled")),
    # 状态
    FlightControlAction.READ_STATE: _spec("state", "读取当前飞行状态与遥测"),
}

if set(ACTION_SPECS) != set(FlightControlAction):  # pragma: no cover - 导入期自检
    raise RuntimeError("ACTION_SPECS must cover every FlightControlAction exactly once")


def action_tool_definitions() -> tuple[dict[str, Any], ...]:
    """Return serializable per-action tools; handlers never cross the model boundary."""
    return tuple({
        "name": action.value,
        "description": ACTION_SPECS[action].description,
        "parameters": {
            "type": "object",
            "properties": {name: parameter_json_schema(parameter)
                           for name, parameter in ACTION_SPECS[action].parameters.items()},
            "required": list(ACTION_SPECS[action].required),
            "additionalProperties": False,
        },
        "requires_confirmation": action != FlightControlAction.READ_STATE,
        "execution_mode": "simulator",
    } for action in FlightControlAction)


def build_full_control_tool_registry(
    simulator: MemoryFlightControlSimulator,
    *,
    command_id_factory: Callable[[], str] | None = None,
    idempotency_key_factory: Callable[[str, Mapping[str, Any]], str] | None = None,
) -> ToolRegistry:
    """把完整的 59 个驾驶舱动作注册成模型可调用的白名单工具。

    每个动作都经过 `preview` -> `confirm` 两阶段：模型给出的参数先被 `ToolRegistry`
    按 schema 校验，再由模拟器做上下文版本、幂等和预览/确认检查，任何一环不通过都不会改变状态。

    幂等键默认每次调用新生成。跨轮次的「恰好执行一次」需要调用方提供稳定的
    `idempotency_key_factory`（例如绑定上层业务的请求号）；模型自己无法提供可靠的幂等键。
    防「未经确认就执行」由 `BoundedAgentLoop` 的确认绑定负责，两者不是同一层保护。
    """
    new_command_id = command_id_factory or (lambda: uuid4().hex)
    new_idempotency_key = idempotency_key_factory or (lambda _action, _arguments: uuid4().hex)

    registry = ToolRegistry(runtime_context_provider=simulator.runtime_context)

    for action, spec in ACTION_SPECS.items():
        mutating = action != FlightControlAction.READ_STATE

        def handler(arguments: Mapping[str, Any], action: FlightControlAction = action) -> dict[str, Any]:
            binding = simulator.binding
            request = FlightControlActionRequest(
                command_id=new_command_id(),
                idempotency_key=new_idempotency_key(action.value, arguments),
                action=action,
                parameters=dict(arguments),
                context_version=binding.context_version,
            )
            receipt = simulator.preview(request)
            if receipt.status is not FlightControlStatus.PREVIEW_REQUIRED:
                # READ_STATE 直接返回；重复、过期、越权也在这里如实返回。
                return receipt.to_dict()
            return simulator.confirm(
                receipt.confirmation_id,
                command_id=request.command_id,
                idempotency_key=request.idempotency_key,
                confirmation_binding=receipt.confirmation_binding,
                context_version=binding.context_version,
            ).to_dict()

        registry.register(ToolDefinition(
            name=action.value,
            description=spec.description,
            schema=ToolSchema(properties=dict(spec.parameters), required=spec.required),
            handler=handler,
            side_effect=mutating,
            requires_confirmation=mutating,
        ))
    return registry



def action_manifest() -> dict[str, Any]:
    """Manifest consumed by the cockpit; every UI item has one tool entry."""
    tools = []
    for action, spec in ACTION_SPECS.items():
        tools.append({
            "action": action.value,
            "domain": spec.domain,
            "label": action.value,
            "parameter_names": list(spec.parameters),
            "requires_confirmation": action != FlightControlAction.READ_STATE,
            "execution_mode": "simulator",
        })
    return {"execution_mode": "simulator", "tools": tools}


def mounted_payloads() -> tuple[str, ...]:
    return ("megaphone", "searchlight", "methane", "dropper")


# ---- 观察回执投影：回放给模型时少喂用不上的字段 ------------------------------

#: 请求回声与循环内部字段：模型下一步既不需要它们，也没有地方用。
#: （确认握手由 `BoundedAgentLoop` 自己持有，模型从不经手 confirmation_*。）
_PROJECTION_ECHO_FIELDS = frozenset({
    "command_id", "idempotency_key", "confirmation_id", "confirmation_binding",
    "expires_at", "execution_mode",
})

#: 体积大、但只服务单一领域的 state 块。域 -> 该域要保留的块。
_PROJECTION_DOMAIN_BLOCKS: Mapping[str, tuple[str, ...]] = {
    "control": ("virtual_buttons",),
    "camera": ("camera",),
    "infrared": ("infrared",),
    "payload": ("payloads",),
    "parameters": ("parameters",),
    # read_state 的唯一目的就是把状态说全，只有它保留全部块。
    "state": ("parameters", "camera", "infrared", "payloads", "virtual_buttons"),
}
_PROJECTION_BLOCKS = frozenset(_PROJECTION_DOMAIN_BLOCKS["state"])
#: 纯循环记账字段，回放时没有信息量。
_PROJECTION_INTERNAL_STATE_FIELDS = frozenset({"last_command_id"})


def project_observation_result(action: Any, result: Any) -> Any:
    """把驾驶舱回执投影成模型下一步真正需要的字段。

    只丢掉**确定**没用的东西：请求回声、循环记账，以及不属于该领域的重块；
    连续遥测（控制权/高度/经纬度/电量/任务与航线状态……）任何域都保留。

    不认识的动作或形状**原样返回**：宁可多花 token，也不猜着删——投影出错的代价是
    模型看不见本该看见的状态。回执本身仍是驾驶舱 UI 的对外契约，这里只影响回放。
    """
    if not isinstance(result, Mapping) or "state" not in result:
        return result
    try:
        spec = ACTION_SPECS.get(FlightControlAction(action))
    except (TypeError, ValueError):
        return result
    state = result["state"]
    if spec is None or not isinstance(state, Mapping):
        return result

    keep_blocks = _PROJECTION_DOMAIN_BLOCKS.get(spec.domain, ())
    projected = {key: value for key, value in result.items()
                 if key not in _PROJECTION_ECHO_FIELDS}
    projected["state"] = {
        key: value for key, value in state.items()
        if (key not in _PROJECTION_BLOCKS and key not in _PROJECTION_INTERNAL_STATE_FIELDS)
        or key in keep_blocks
    }
    return projected
