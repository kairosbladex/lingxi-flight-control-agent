"""完整驾驶舱模拟控制面。

本模块只维护进程内状态，所有会改变状态的动作都经过预览/确认，且
Runtime binding 同时绑定 actor、项目、机库、无人机、任务批次和上下文版本。
它不包含 MQTT、HTTP、真实飞控或生产任务适配器。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from copy import deepcopy
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import math
import re
from typing import TYPE_CHECKING, Any, Callable, Mapping
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
from .geometry import offset_target, relative_target
from .task_events import (
    TEMPORARY_DECISION_TIMED_OUT, TaskEventError, TaskEventKind, TaskEventLedger,
    apply_task_event as task_events_apply, apply_temporary_decision_timeout,
    coerce_kind, event_from_legacy, post_temporary_decision, temporary_decision_timed_out,
    validate_post_temporary_choice,
)
from .ai_lifecycle import (
    ALGORITHM_LIFECYCLE_UNAVAILABLE, AlgorithmLifecycleReceipt, AlgorithmStage,
    SimulatorAlgorithmLifecycleGateway, annotation_active_set, apply_plan, plan_selection,
)
from .mapping_interruption import (
    MAPPING_INTERRUPTION_UNAVAILABLE, MappingInterruptionRequest,
    SimulatorMappingInterruptionGateway, interruption_required,
)
from .flight_policy import (
    ADJUSTABLE, CONTROL_MESSAGE, MAPPING_MESSAGE, PATH_ACTIONS, POLICY_VERSION, MICRO_ACTIONS,
    TEMPORARY_ACTIONS, INTERRUPTING_ACTIONS, RESUME_ACTIONS, PolicyError,
    check_flight_policy, check_path, mapping_interruption, normalize_parameters,
)

if TYPE_CHECKING:  # 运行时避免与 executor 成环（executor 依赖本模块的类型）
    from .executor import RuntimeExecutor

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
    HOVER = "hover"
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
    SPEAKER_SPEAK = "speaker_speak"
    SPEAKER_VOLUME = "speaker_volume"
    SEARCHLIGHT_BRIGHTNESS = "searchlight_brightness"
    TASK_END_CURRENT = "task_end_current"
    TASK_END_ALL = "task_end_all"
    AI_ALGORITHM_SET = "ai_algorithm_set"
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
    #: 已发出但平台没有给出可判定的结果；禁止自动重试，也不代表未执行。
    UNKNOWN = "unknown"


_MOVES = {
    FlightControlAction.MOVE_FORWARD, FlightControlAction.MOVE_BACKWARD,
    FlightControlAction.MOVE_LEFT, FlightControlAction.MOVE_RIGHT,
    FlightControlAction.MOVE_UP, FlightControlAction.MOVE_DOWN,
}

# V1.3: only ownership management is exempt; even emergency stop needs control.
_CONTROL_FREE_ACTIONS = frozenset({
    FlightControlAction.CONTROL_ACQUIRE,
    FlightControlAction.REMOTE_REQUEST,
    FlightControlAction.REMOTE_APPROVE,
    FlightControlAction.REMOTE_REJECT,
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
    task_type: str = "inspection"
    mapping_invalidated: bool = False
    auto_stitching: bool = False
    #: V1.4 4.1.2 的测绘重建/断点配置。模拟器侧只做占位与作废记录，
    #: 不声称真实 Java 持久化字段的形状；真实平台是否如此未验证。
    rebuild_type: str | None = None
    break_info: dict[str, Any] | None = None
    #: 测绘中断后「恢复航线」被永久置灰（V1.4 4.1.2）。
    route_resume_disabled: bool = False
    temporary_task_id: str | None = None
    low_battery_return: bool = False
    #: V1.4 4.1.3：临时任务完成后的计时锚点/截止点；由 task_events 归约写入。
    temporary_completed_at: float | None = None
    temporary_decision_deadline: float | None = None
    temporary_decision_timed_out: bool = False
    #: V1.4 4.2：本次返航中断了一个未完成临时任务（前端据此提示充电重启后续飞）。
    temporary_task_abandoned: bool = False
    #: V1.4 4.3.2：设备已挂载能力清单。``None`` 表示**未核验**，不得默认成已挂载。
    mounted_lenses: list[str] | None = None
    mounted_payloads: list[str] | None = None
    configured_algorithms: list[str] = field(default_factory=list)
    global_algorithms: list[str] = field(default_factory=list)
    annotation_algorithms: dict[str, list[str]] = field(default_factory=dict)
    active_annotations: list[str] = field(default_factory=list)
    active_algorithms: list[str] = field(default_factory=list)
    device_limits: dict[str, Any] = field(default_factory=lambda: {
        **{action: {"min": low, "max": high} for action, (_, low, high) in ADJUSTABLE.items()},
        "gimbal_pitch_deg": {"min": -90, "max": 90},
    })
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
            "execution_mode": self.execution_mode,
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
            "task_type": self.task_type,
            "mapping_invalidated": self.mapping_invalidated,
            "auto_stitching": self.auto_stitching,
            "rebuild_type": self.rebuild_type,
            "break_info": deepcopy(self.break_info),
            "route_resume_disabled": self.route_resume_disabled,
            "temporary_task_id": self.temporary_task_id,
            "low_battery_return": self.low_battery_return,
            # 计时锚点/截止点只服务任务事件状态机，属于对象内部状态；
            # 对外只投影「是否超时」与「返航是否中断了临时任务」两个判断结果。
            "temporary_decision_timed_out": self.temporary_decision_timed_out,
            "temporary_task_abandoned": self.temporary_task_abandoned,
            "mounted_lenses": None if self.mounted_lenses is None else list(self.mounted_lenses),
            "mounted_payloads": None if self.mounted_payloads is None else list(self.mounted_payloads),
            "device_limits": deepcopy(self.device_limits),
            "configured_algorithms": list(self.configured_algorithms),
            "global_algorithms": list(self.global_algorithms),
            "annotation_algorithms": deepcopy(self.annotation_algorithms),
            "active_annotations": list(self.active_annotations),
            "active_algorithms": list(self.active_algorithms),
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
    parameters: Mapping[str, Any] = field(default_factory=dict)
    adjustments: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: 测绘中断的原子操作回执（仅在该动作触发测绘中断时出现）。
    mapping_interruption: Mapping[str, Any] | None = None
    #: 平台位移/转向只有在回执明确 completed 时才为真。缺省 False，不能把受理当成到位。
    arrival_verified: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "execution_mode": self.execution_mode,
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
            "parameters": deepcopy(dict(self.parameters)),
            "adjustments": deepcopy(self.adjustments),
            "warnings": list(self.warnings),
            "mapping_interruption": deepcopy(dict(self.mapping_interruption))
                if self.mapping_interruption else None,
            "arrival_verified": self.arrival_verified,
            "policy_version": POLICY_VERSION,
        }


@dataclass
class _Pending:
    request: FlightControlActionRequest
    binding: RuntimeBinding
    confirmation_id: str
    confirmation_binding: str
    expires_at: float
    status: FlightControlStatus = FlightControlStatus.PREVIEW_REQUIRED
    route_context: tuple[int, str | None, str, int | None] | None = None
    policy_context: str = ""
    adjustments: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


_ROUTE_REQUIRED_STATES = {
    FlightControlAction.ROUTE_PAUSE: "running",
    FlightControlAction.ROUTE_RESUME: "paused",
    FlightControlAction.EXECUTE_ROUTE: "uploaded",
}
_ROUTE_INVALIDATING_ACTIONS = frozenset(_ROUTE_REQUIRED_STATES) | {
    FlightControlAction.UPLOAD_ROUTE,
    FlightControlAction.CONTINUE_FROM_BREAKPOINT,
    FlightControlAction.POINT_FLIGHT,
    FlightControlAction.POINT_ORBIT,
    # These replace the flight intent or end the current authorization. A card
    # from another conversation must not revive the earlier route afterward.
    FlightControlAction.EMERGENCY_STOP,
    FlightControlAction.TAKEOFF,
    FlightControlAction.LAND,
    FlightControlAction.LAND_ALTERNATE,
    FlightControlAction.RETURN_HOME,
    FlightControlAction.CANCEL_RETURN_HOME,
    FlightControlAction.CONTROL_RELEASE,
    FlightControlAction.TASK_END_CURRENT,
    FlightControlAction.TASK_END_ALL,
}


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
        FlightControlAction.SPEAKER_SPEAK: ("text",),
        FlightControlAction.SPEAKER_VOLUME: ("value",),
        FlightControlAction.SEARCHLIGHT_BRIGHTNESS: ("value",),
        FlightControlAction.AI_ALGORITHM_SET: ("algorithm", "enabled"),
    }
    optional: dict[FlightControlAction, tuple[str, ...]] = {
        FlightControlAction.GIMBAL_UP: ("angle_deg",),
        FlightControlAction.GIMBAL_DOWN: ("angle_deg",),
        FlightControlAction.GIMBAL_LEFT: ("angle_deg",),
        FlightControlAction.GIMBAL_RIGHT: ("angle_deg",),
        FlightControlAction.CONTINUE_FROM_BREAKPOINT: ("index",),
        FlightControlAction.PAYLOAD_DROPPER: ("payload",),
        FlightControlAction.PAYLOAD_MEGAPHONE: ("enabled",),
        FlightControlAction.PAYLOAD_SEARCHLIGHT: ("enabled",),
        FlightControlAction.PAYLOAD_METHANE: ("enabled",),
    }
    allowed = set(required.get(action, ())) | set(optional.get(action, ()))
    if action in {FlightControlAction.GIMBAL_UP, FlightControlAction.GIMBAL_DOWN,
                  FlightControlAction.GIMBAL_LEFT, FlightControlAction.GIMBAL_RIGHT} and 'angle_deg' in parameters:
        _number(parameters['angle_deg'], 'angle_deg', 0.1, 90)
    extras = sorted(set(parameters) - allowed)
    if extras:
        raise ValueError(f"{action.value} does not accept parameters: {extras}")
    for field_name in required.get(action, ()):
        if field_name not in parameters:
            raise ValueError(f"{action.value} requires {field_name}")
    if action == FlightControlAction.SPEAKER_SPEAK:
        text = parameters['text']
        if not isinstance(text, str) or not text.strip() or len(text) > 500 or any(ord(c) < 32 for c in text):
            raise ValueError('speaker text must contain 1-500 printable characters')
    if action in {FlightControlAction.SPEAKER_VOLUME, FlightControlAction.SEARCHLIGHT_BRIGHTNESS}:
        value = _number(parameters['value'], 'value', 0, 100)
        if value != int(value):
            raise ValueError('payload percentage must be an integer')
    if action == FlightControlAction.AI_ALGORITHM_SET:
        if not isinstance(parameters['algorithm'], str) or not parameters['algorithm'].strip() or len(parameters['algorithm']) > 128 or type(parameters['enabled']) is not bool:
            raise ValueError('explicit algorithm and boolean enabled required')
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
        execution_mode=state.execution_mode,
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
        parameters=deepcopy(dict(request.parameters)),
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

    def __init__(self, binding: RuntimeBinding, *, clock=None, ttl_sec: float = 120.0,
                 path_checker=None, algorithm_gateway=None, mapping_gateway=None):
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
        self._route_revision = 0
        self._path_checker = path_checker
        # V1.4：任务事件走统一 typed 账本，重复幂等、旧序号拒绝。
        self._task_events = TaskEventLedger()
        # V1.4 2.1.2：算法启停走可注入网关。模拟器默认提供完整 seam，
        # 但每条回执都带 execution_mode=simulator，不冒充真实平台运行状态。
        self._algorithm_gateway = algorithm_gateway or SimulatorAlgorithmLifecycleGateway()
        self._algorithm_running: dict[str, list[str]] = {}
        self._algorithm_stages: dict[str, dict[str, Any]] = {}
        self._algorithm_suppressed: set[str] = set()
        # V1.4 4.1.2：测绘中断是一个原子业务操作，确认后才执行，失败即不下发。
        self._mapping_gateway = mapping_gateway or SimulatorMappingInterruptionGateway(
            lambda: self.state, clock=self._clock)

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
            "execution_mode": self.state.execution_mode,
            "binding": self.binding.to_dict(),
            "state": self.state.to_dict(),
        }

    def bind(self, binding: RuntimeBinding) -> None:
        binding.validate()
        if binding == self.binding:
            return
        self._pending.clear()
        self.binding = binding
        # 换绑重置状态，但执行模式是执行后端的属性，不能跟着掉回 "simulator"。
        execution_mode = self.state.execution_mode
        self.state = FlightControlState(context_version=binding.context_version)
        self.state.execution_mode = execution_mode
        self._route_revision = 0
        # 换绑后算法运行集合必须重来：新绑定上没有任何已确认的启动回执。
        self._algorithm_running.clear()
        self._algorithm_stages.clear()
        self._algorithm_suppressed.clear()
        # 同步父类持有的执行上下文，保持两套 API 一致。
        self.context = _context_for(binding)

    def _route_context(self) -> tuple[int, str | None, str, int | None]:
        # The generation also detects replacing an upload with the same id and
        # pause/resume or release/acquire cycles returning to the same state.
        return (self._route_revision, self.state.route_id,
                self.state.route_status, self.state.breakpoint_index)

    def _route_precondition_error(self, action: FlightControlAction) -> tuple[str, str] | None:
        if action.value in RESUME_ACTIONS and (self.state.mapping_invalidated
                                              or self.state.route_resume_disabled):
            return "MAPPING_NOT_RESUMABLE", "原测绘任务已失效，不可恢复航线。"
        if action == FlightControlAction.CONTINUE_FROM_BREAKPOINT:
            if not self.state.route_id or self.state.breakpoint_index is None:
                return "BREAKPOINT_UNAVAILABLE", "没有有效的原始任务断点。"
            if self.state.route_status not in {"paused", "point_flight", "point_orbit", "temporary_completed"}:
                return "ROUTE_STATE_MISMATCH", "当前状态不能恢复原始任务断点。"
            return None
        if (action == FlightControlAction.ROUTE_RESUME and self.state.route_id
                and self.state.breakpoint_index is not None
                and self.state.route_status == 'temporary_completed'):
            return None
        expected = _ROUTE_REQUIRED_STATES.get(action)
        if expected is None:
            return None
        if not isinstance(self.state.route_id, str) or not self.state.route_id.strip():
            return "ROUTE_NOT_LOADED", "没有可操作的模拟航线，请先上传航线"
        if self.state.route_status != expected:
            return "ROUTE_STATE_MISMATCH", "当前模拟航线状态不支持此动作，请核对启动、暂停或恢复意图"
        return None

    def _precise_gimbal_error(self, request):
        if 'angle_deg' not in request.parameters or request.action not in {
                'gimbal_up','gimbal_down','gimbal_left','gimbal_right'}:
            return None
        if self.state.control_owner != self.binding.actor_id:
            return 'CONTROL_NOT_OWNED', '模拟控制权已变化，请重新检查'
        if request.action in {'gimbal_up','gimbal_down'}:
            target = self.state.gimbal_pitch_deg + request.parameters['angle_deg'] * (
                1 if request.action == 'gimbal_up' else -1)
            if not -90 <= target <= 90:
                return 'GIMBAL_TARGET_OUT_OF_RANGE', '目标超出模拟云台角度范围，不截断请求角度'
        return None

    def preview(self, request: FlightControlActionRequest) -> FlightControlReceipt:
        try:
            parameters, adjustments = normalize_parameters(request.action, request.parameters, self.state.to_dict())
            request = replace(request, parameters=parameters)
        except PolicyError as error:
            return _status_receipt(FlightControlStatus.REJECTED, request, self.binding, self.state,
                                   error_code=error.code, message=str(error))
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
                                   error_code="CONTROL_NOT_OWNED", message=CONTROL_MESSAGE)
        try:
            check_flight_policy(action.value, self.state.to_dict())
            if action == FlightControlAction.AI_ALGORITHM_SET and parameters['algorithm'] not in self.state.configured_algorithms:
                raise PolicyError('ALGORITHM_NOT_CONFIGURED', '当前项目未配置该AI算法。')
            # 模拟器允许协作式 provider（离线 fake），但每条回执都带
            # execution_mode=simulator，不代表真实平台或设备已执行围栏检查。
            warning = check_path(self._path_checker, self.binding, action.value, parameters,
                                 self.state.to_dict(), allow_cooperative=True)
        except PolicyError as error:
            return _status_receipt(FlightControlStatus.REJECTED, request, self.binding, self.state,
                                   error_code=error.code, message=str(error))
        error = self._precise_gimbal_error(request) or self._route_precondition_error(action)
        if error is not None:
            return _status_receipt(FlightControlStatus.REJECTED, request, self.binding, self.state,
                                   error_code=error[0], message=error[1])
        route_context = self._route_context() if action in _ROUTE_REQUIRED_STATES or action.value in MICRO_ACTIONS | TEMPORARY_ACTIONS | RESUME_ACTIONS | {'task_end_current', 'task_end_all', 'hover'} else None
        confirmation_id = uuid4().hex
        binding_data = {"binding": self.binding.to_dict(), "request": request.to_dict()}
        if route_context is not None:
            binding_data["route_context"] = route_context
        binding_hash = _json_hash(binding_data)
        pending = _Pending(request, self.binding, confirmation_id, binding_hash,
                           float(self._clock()) + self._ttl_sec, route_context=route_context,
                           policy_context=self._policy_context(action), adjustments=adjustments,
                           warnings=([warning] if warning else []) +
                           ([MAPPING_MESSAGE] if mapping_interruption(action.value, self.state.to_dict()) else []))
        self._pending[confirmation_id] = pending
        receipt = _status_receipt(FlightControlStatus.PREVIEW_REQUIRED, request, self.binding, self.state,
                               confirmation_id=confirmation_id, confirmation_binding=binding_hash,
                               message="动作已预览，等待确认", expires_at=pending.expires_at)
        return replace(receipt, adjustments=adjustments, warnings=pending.warnings)

    def _policy_context(self, action):
        state = self.state.to_dict()
        keys = ["task_type", "mapping_invalidated", "route_resume_disabled", "low_battery_return"]
        if action in ADJUSTABLE:
            keys += ["device_limits"]
        if action in {"gimbal_up", "gimbal_down"}:
            keys += ["gimbal_pitch_deg"]
        if action == 'ai_algorithm_set':
            keys += ['configured_algorithms', 'global_algorithms', 'annotation_algorithms', 'active_annotations']
        if action in INTERRUPTING_ACTIONS:
            keys += ["route_status", "auto_stitching"]
        if action in PATH_ACTIONS:
            # 围栏结论依赖当前位姿与机头航向；位姿变了，旧确认卡上的“路径已检查”
            # 就不再成立，必须重新生成。
            keys += ["longitude", "latitude", "altitude_m", "yaw_deg"]
        return _json_hash({key: state[key] for key in keys})

    def apply_task_event(self, event, *, task_batch_id, temporary_task_id=None, annotation_id=None,
                         event_id=None, sequence=None, occurred_at=None, state_version=None,
                         annotation_version=None):
        """Simulator-only platform event seam; never exposed as an LLM tool.

        事件契约、幂等与顺序判定在 ``task_events`` 中实现，这里只负责把
        模拟器状态与账本接上，并在归约后刷新算法激活集合。
        """
        if task_batch_id != self.binding.task_batch_id:
            raise ValueError("task binding mismatch")
        kind = coerce_kind(event)
        if sequence is None:
            sequence = self._task_events.last_sequence + 1
        if event_id is None:
            # 内部调用没有平台事件 ID；生成一次性 ID，重复语义由归约阶段按
            # temporary_task_id / 状态前置条件拒绝。
            event_id = f"sim-{uuid4().hex}"
        typed = event_from_legacy(kind.value, event_id=event_id, sequence=sequence,
                                  binding=self.binding, occurred_at=self._clock() if occurred_at is None else occurred_at,
                                  temporary_task_id=temporary_task_id, annotation_id=annotation_id,
                                  state_version=state_version, annotation_version=annotation_version)
        outcome = task_events_apply(self.state, self._task_events, typed, self.binding)
        self._route_revision += 1
        if kind in {TaskEventKind.ANNOTATION_ENTERED, TaskEventKind.ANNOTATION_LEFT,
                    TaskEventKind.ALL_TASKS_ENDED}:
            self._refresh_algorithms(reason=kind.value, annotation_id=annotation_id)
        return self.state.to_dict()

    def task_event_ledger(self) -> dict[str, Any]:
        """事件账本摘要；只读，供排障与验收使用。"""
        return self._task_events.snapshot()

    def _stage(self, annotation_id: str) -> dict[str, Any]:
        return self._algorithm_stages.setdefault(annotation_id, {
            "annotation_id": annotation_id,
            "expected": [], "dispatched": [],
            "start_requested": [], "started": [],
            "stop_requested": [], "stopped": [],
            "execution_mode": getattr(self._algorithm_gateway, "execution_mode", "unavailable"),
            "error_code": None,
        })

    def _refresh_algorithms(self, *, reason: str = "", annotation_id: str | None = None):
        """把「应当运行」与「平台已确认运行」分开，并只按来源关闭算法。

        - 期望集合由全局算法与**仍在活动**的标注共同决定，天然去重；
        - 离开标注时只请求关闭「该标注来源且不再被全局或其他活动标注引用」的算法；
        - ``active_algorithms`` 只包含已确认启动的算法；平台不可用时标注算法
          不会出现在这里，不会被冒充成运行中。
        """
        s = self.state
        configured = set(s.configured_algorithms)
        suppressed = self._algorithm_suppressed
        desired: dict[str, list[str]] = {}
        for annotation in s.active_annotations:
            wanted = [item for item in s.annotation_algorithms.get(annotation, [])
                      if item in configured and item not in suppressed]
            desired[annotation] = wanted
            stage = self._stage(annotation)
            stage["expected"] = list(s.annotation_algorithms.get(annotation, []))
        # 阶段 2：任务下发时写入的算法配置（去重、限定项目配置集合）。
        dispatched = [item for item in s.global_algorithms if item in configured]
        for items in desired.values():
            for item in items:
                if item not in dispatched:
                    dispatched.append(item)
        for annotation in desired:
            self._stage(annotation)["dispatched"] = list(dispatched)

        # 阶段 5/6：先关闭不再需要的来源，再启动新增来源，避免同一算法被反复开关。
        for annotation in list(self._algorithm_running):
            current = list(self._algorithm_running.get(annotation, ()))
            wanted = desired.get(annotation, [])
            candidates = [item for item in current if item not in wanted]
            still_referenced = set(s.global_algorithms)
            for other, items in desired.items():
                if other != annotation:
                    still_referenced.update(items)
            to_stop = [item for item in candidates if item not in still_referenced]
            if not to_stop:
                if not wanted:
                    self._algorithm_running.pop(annotation, None)
                else:
                    self._algorithm_running[annotation] = list(wanted)
                continue
            stage = self._stage(annotation)
            stage["stop_requested"] = list(to_stop)
            try:
                receipt = self._algorithm_gateway.stop(
                    self.binding, annotation, to_stop,
                    request_id=f"stop:{annotation}:{reason or 'sync'}")
            except Exception:
                receipt = AlgorithmLifecycleReceipt(
                    stage=AlgorithmStage.STOP_REQUESTED, annotation_id=annotation,
                    algorithms=tuple(to_stop),
                    execution_mode=getattr(self._algorithm_gateway, "execution_mode", "unavailable"),
                    confirmed=False, error_code=ALGORITHM_LIFECYCLE_UNAVAILABLE,
                    message="算法停止请求失败。")
            stage["execution_mode"] = receipt.execution_mode
            stage["error_code"] = receipt.error_code
            if receipt.confirmed:
                stage["stopped"] = list(to_stop)
                remaining = [item for item in current if item not in to_stop]
                if remaining:
                    self._algorithm_running[annotation] = remaining
                else:
                    self._algorithm_running.pop(annotation, None)
            else:
                # 停止未被确认：仍按「可能在运行」保留，绝不假装已关闭。
                self._algorithm_running[annotation] = current

        # 阶段 3/4：启动新增来源。
        for annotation, wanted in desired.items():
            current = list(self._algorithm_running.get(annotation, ()))
            to_start = [item for item in wanted if item not in current]
            if not to_start:
                continue
            stage = self._stage(annotation)
            stage["start_requested"] = list(to_start)
            try:
                receipt = self._algorithm_gateway.start(
                    self.binding, annotation, to_start,
                    request_id=f"start:{annotation}:{reason or 'sync'}")
            except Exception:
                receipt = AlgorithmLifecycleReceipt(
                    stage=AlgorithmStage.START_REQUESTED, annotation_id=annotation,
                    algorithms=tuple(to_start),
                    execution_mode=getattr(self._algorithm_gateway, "execution_mode", "unavailable"),
                    confirmed=False, error_code=ALGORITHM_LIFECYCLE_UNAVAILABLE,
                    message="算法启动请求失败。")
            stage["execution_mode"] = receipt.execution_mode
            stage["error_code"] = receipt.error_code
            if receipt.confirmed:
                stage["started"] = list(dict.fromkeys(current + to_start))
                self._algorithm_running[annotation] = list(dict.fromkeys(current + to_start))

        running: list[str] = [item for item in s.global_algorithms if item in configured]
        for items in self._algorithm_running.values():
            for item in items:
                if item in configured and item not in running:
                    running.append(item)
        s.active_algorithms = [item for item in running if item not in suppressed]

    def algorithm_lifecycle(self) -> dict[str, Any]:
        """只读生命周期审计：六阶段分开记录，不把配置或请求当成运行中。"""
        return {
            "contract_version": "lingxi-ai-lifecycle/v1",
            "execution_mode": getattr(self._algorithm_gateway, "execution_mode", "unavailable"),
            "configured_algorithms": list(self.state.configured_algorithms),
            "global_algorithms": list(self.state.global_algorithms),
            "suppressed_algorithms": sorted(self._algorithm_suppressed),
            "active_annotations": list(self.state.active_annotations),
            "active_algorithms": list(self.state.active_algorithms),
            "annotations": {key: {stage: list(value) if isinstance(value, list) else value
                                  for stage, value in record.items()}
                            for key, record in sorted(self._algorithm_stages.items())},
        }

    def select_algorithms(self, requested, *, mode=None):
        """自然语言 AI 选择：冲突必须追问，多算法整批生效，未知算法整批拒绝。"""
        plan = plan_selection(requested, configured=self.state.configured_algorithms,
                              global_algorithms=self.state.global_algorithms,
                              active_annotations=self.state.active_annotations,
                              annotation_algorithms=self.state.annotation_algorithms,
                              mode=mode)
        if plan.requires_clarification:
            return plan
        globals_now, suppressed = apply_plan(plan, self.state)
        self._algorithm_suppressed = set(suppressed)
        self.state.revision += 1
        self._refresh_algorithms(reason="selection")
        return plan

    def mapping_interruption_audit(self) -> dict[str, Any]:
        snapshot = getattr(self._mapping_gateway, "snapshot", None)
        return snapshot() if callable(snapshot) else {
            "execution_mode": getattr(self._mapping_gateway, "execution_mode", "unavailable")}

    def resolve_temporary_outcome(self, choice=None):
        """临时任务完成后由用户显式选择；不提供默认分支。

        ``resume_main`` 只恢复仍有效的巡检主线断点，``end_all`` 清空断点；
        两者都必须由用户明说，系统不猜。

        V1.4 4.1.3：这是**模拟器/测试入口**的计时兜底（HTTP / agent 生产路径尚未
        接线，不得据此声称生产已可达）。时钟来自构造时注入的确定性 ``clock``，
        不读真实时间；deadline 已过时先按 4.2 低电量规则处理（保留主线断点 +
        返航），事后再来的选择一律拒绝（``TEMPORARY_DECISION_TIMED_OUT``）。
        """
        now = float(self._clock())
        # 计时兜底先判定再归约：超时后不再接受事后选择，直接走 4.2 低电量规则。
        if temporary_decision_timed_out(self.state, now=now):
            timeout = apply_temporary_decision_timeout(self.state, now=now)
            if timeout.get("applied"):
                # 归约确实改变了路线状态才推进 generation；重放不推进。
                self._route_revision += 1
            if choice is not None:
                raise TaskEventError(TEMPORARY_DECISION_TIMED_OUT)
            return {"required": False, "choice": None, "timed_out": True,
                    "action": timeout.get("action"), "applied": timeout.get("applied"),
                    "route_status": self.state.route_status,
                    "task_status": self.state.task_status,
                    "low_battery_return": self.state.low_battery_return,
                    "resumable": (bool(self.state.route_id)
                                 and not self.state.mapping_invalidated
                                 and self.state.breakpoint_index is not None
                                 and not self.state.route_resume_disabled)}
        if choice is None:
            return post_temporary_decision(self.state, now=now)
        selected = validate_post_temporary_choice(self.state, choice, now=now)
        if selected == "end_all":
            self.apply_task_event("all_tasks_ended", task_batch_id=self.binding.task_batch_id)
            return {"required": False, "choice": selected,
                    "route_status": self.state.route_status, "resumable": False}
        self.state.route_status = "running"
        self.state.task_status = "route_executing"
        # 用户已作出选择：计时兜底到此为止，残留 deadline 不得再触发返航。
        self.state.temporary_decision_deadline = None
        self.state.temporary_decision_timed_out = False
        self.state.temporary_completed_at = None
        self.state.revision += 1
        self._route_revision += 1
        return {"required": False, "choice": selected,
                "route_status": self.state.route_status, "resumable": True}

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
        action = FlightControlAction(request.action)
        if action not in _CONTROL_FREE_ACTIONS and self.state.control_owner != self.binding.actor_id:
            self._pending.pop(confirmation_id, None)
            return _status_receipt(FlightControlStatus.REJECTED, request, self.binding, self.state,
                                   error_code="CONTROL_NOT_OWNED", message=CONTROL_MESSAGE)
        if pending.route_context is not None:
            if self.state.control_owner != self.binding.actor_id:
                error = ("CONTROL_NOT_OWNED", "模拟控制权已变化，请重新检查")
            elif pending.route_context != self._route_context():
                error = ("ROUTE_STATE_CHANGED", "模拟航线已变化，请重新生成确认卡")
            else:
                error = self._route_precondition_error(action)
            if error is not None:
                self._pending.pop(confirmation_id, None)
                return _status_receipt(FlightControlStatus.REJECTED, request, self.binding, self.state,
                                       confirmation_id=confirmation_id,
                                       confirmation_binding=pending.confirmation_binding,
                                       error_code=error[0], message=error[1])
        try:
            check_flight_policy(action.value, self.state.to_dict())
            if pending.policy_context != self._policy_context(action):
                raise PolicyError("POLICY_STATE_CHANGED", "设备参数或任务状态已变化，请重新生成确认卡。")
            # 同 preview：模拟器允许协作式 provider，回执 execution_mode=simulator。
            warning = check_path(self._path_checker, self.binding, action.value,
                                 request.parameters, self.state.to_dict(),
                                 allow_cooperative=True)
        except PolicyError as policy_error:
            self._pending.pop(confirmation_id, None)
            return _status_receipt(FlightControlStatus.REJECTED, request, self.binding, self.state,
                                   error_code=policy_error.code, message=str(policy_error))
        error = self._precise_gimbal_error(request)
        if error is not None:
            self._pending.pop(confirmation_id, None)
            return _status_receipt(FlightControlStatus.REJECTED, request, self.binding, self.state,
                                   error_code=error[0], message=error[1])
        # V1.4 4.1.2：测绘中断必须在同一个原子操作里完成，失败即不下发飞控动作。
        interruption = None
        if interruption_required(action.value, self.state):
            interruption = self._mapping_gateway.interrupt(MappingInterruptionRequest(
                command_id=request.command_id, action=action.value,
                task_batch_id=self.binding.task_batch_id, confirmed=True,
                route_id=self.state.route_id, task_type=self.state.task_type,
                idempotency_key=request.idempotency_key))
            # 只有「原子完成 + 平台明确确认」才算测绘已作废；模拟器不落平台副作用。
            if not interruption.effective:
                self._pending.pop(confirmation_id, None)
                return _status_receipt(
                    FlightControlStatus.REJECTED, request, self.binding, self.state,
                    confirmation_id=confirmation_id,
                    confirmation_binding=pending.confirmation_binding,
                    error_code=interruption.error_code or MAPPING_INTERRUPTION_UNAVAILABLE,
                    message=interruption.message or MAPPING_MESSAGE)
        result = self._apply(request)
        result = _status_receipt(FlightControlStatus.ACCEPTED, request, self.binding, self.state,
                                 confirmation_id=confirmation_id, confirmation_binding=pending.confirmation_binding,
                                 message="模拟动作已执行")
        result = replace(result, adjustments=pending.adjustments,
                         warnings=list(dict.fromkeys(pending.warnings + ([warning] if warning else []))),
                         mapping_interruption=(interruption.to_dict() if interruption else None))
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
        if mapping_interruption(action.value, s.to_dict()):
            # 兜底：正常情况下原子网关已在 confirm 之前完成这六步；这里保证
            # 自定义网关没有落地时状态仍然一致，且顺序与契约相同。
            from .mapping_interruption import apply_atomic_steps
            apply_atomic_steps(s, action=action.value, audit_id="sim-fallback")
        if action.value in TEMPORARY_ACTIONS:
            if not s.mapping_invalidated and s.route_id and s.breakpoint_index is None:
                s.breakpoint_index = 0
            s.temporary_task_id = request.command_id
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
            if action in {FlightControlAction.MOVE_FORWARD, FlightControlAction.MOVE_BACKWARD,
                          FlightControlAction.MOVE_LEFT, FlightControlAction.MOVE_RIGHT}:
                s.longitude, s.latitude = relative_target(
                    s.longitude, s.latitude, s.heading_deg, action.value.removeprefix("move_"), distance)
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
        elif action == FlightControlAction.HOVER:
            # 悬停只清速度并保持任务；航线执行中等价于暂停并保留断点（V1.3 2.4）。
            # 它绝不改 task_status，因此不能当作急停或结束任务使用。
            s.ground_speed_mps = 0.0
            s.vertical_speed_mps = 0.0
            s.air_speed_mps = 0.0
            if s.route_status == "running":
                s.route_status = "paused"
                if not s.mapping_invalidated and s.breakpoint_index is None:
                    s.breakpoint_index = 0
        elif action == FlightControlAction.TAKEOFF:
            s.altitude_m = float(p["altitude_m"])
            s.task_status = "flying"
            s.cycle_count += 1
        elif action in {FlightControlAction.LAND, FlightControlAction.LAND_ALTERNATE}:
            s.altitude_m = 0.0
            s.task_status = "landed" if action == FlightControlAction.LAND else "alternate_landed"
        elif action == FlightControlAction.RETURN_HOME:
            s.task_status = "returning_home"
            s.temporary_task_id = None
        elif action == FlightControlAction.CANCEL_RETURN_HOME:
            s.task_status = "flying"
        elif action == FlightControlAction.ROUTE_PAUSE:
            s.route_status = "paused"
            if not s.mapping_invalidated and s.breakpoint_index is None:
                s.breakpoint_index = 0
        elif action == FlightControlAction.ROUTE_RESUME:
            s.route_status = "running"
            s.temporary_task_id = None
        elif action == FlightControlAction.CONTINUE_FROM_BREAKPOINT:
            s.route_status = "running"
            s.breakpoint_index = int(p.get("index", s.breakpoint_index or 0))
            s.temporary_task_id = None
        elif action == FlightControlAction.POINT_FLIGHT:
            s.longitude, s.latitude, s.altitude_m = float(p["longitude"]), float(p["latitude"]), float(p["altitude_m"])
            s.route_status = "point_flight"
        elif action == FlightControlAction.POINT_ORBIT:
            # 落在半径圆的正北点，和围栏折线的 0 度起点一致。绝不能落到环绕中心。
            s.longitude, s.latitude = offset_target(
                float(p["longitude"]), float(p["latitude"]), 0.0, float(p["radius_m"]))
            s.route_status = "point_orbit"
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
        elif action == FlightControlAction.GIMBAL_UP: s.gimbal_pitch_deg = min(90.0, s.gimbal_pitch_deg + p.get('angle_deg', 5))
        elif action == FlightControlAction.GIMBAL_DOWN: s.gimbal_pitch_deg = max(-90.0, s.gimbal_pitch_deg - p.get('angle_deg', 5))
        elif action == FlightControlAction.GIMBAL_LEFT: s.gimbal_yaw_deg -= p.get('angle_deg', 5)
        elif action == FlightControlAction.GIMBAL_RIGHT: s.gimbal_yaw_deg += p.get('angle_deg', 5)
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
        elif action == FlightControlAction.SPEAKER_SPEAK:
            s.payloads['speaker_text'] = p['text']
            s.payloads['megaphone'] = True
        elif action == FlightControlAction.SPEAKER_VOLUME:
            s.payloads['speaker_volume'] = int(p['value'])
        elif action == FlightControlAction.SEARCHLIGHT_BRIGHTNESS:
            s.payloads['searchlight_brightness'] = int(p['value'])
        elif action == FlightControlAction.AI_ALGORITHM_SET:
            selected = set(s.global_algorithms)
            if p['enabled']:
                selected.add(p['algorithm'])
            else:
                selected.discard(p['algorithm'])
            s.global_algorithms = sorted(selected)
            # 用户显式打开为全局后，本会话的「仅替换」抑制不再成立。
            self._algorithm_suppressed -= selected
            self._refresh_algorithms(reason="global_toggle")
        elif action == FlightControlAction.TASK_END_CURRENT:
            if s.temporary_task_id:
                s.temporary_task_id = None
                s.route_status = 'ended' if s.mapping_invalidated else 'temporary_completed'
            else:
                self.apply_task_event('all_tasks_ended', task_batch_id=self.binding.task_batch_id)
        elif action == FlightControlAction.TASK_END_ALL:
            self.apply_task_event('all_tasks_ended', task_batch_id=self.binding.task_batch_id)
        elif action == FlightControlAction.PAYLOAD_DROPPER:
            s.payloads["dropper"] = {"last_drop": True, "payload": p.get("payload", "default")}
        elif action in {FlightControlAction.PAYLOAD_MEGAPHONE, FlightControlAction.PAYLOAD_SEARCHLIGHT, FlightControlAction.PAYLOAD_METHANE}:
            s.payloads[action.value.removeprefix("payload_")] = p.get("enabled", True)
        if action in _ROUTE_INVALIDATING_ACTIONS:
            self._route_revision += 1
        s.revision += 1
        s.last_action = action.value
        s.last_command_id = request.command_id


_TEXT_ACTIONS: tuple[tuple[str, FlightControlAction, dict[str, Any]], ...] = (
    ("急停", FlightControlAction.EMERGENCY_STOP, {}),
    ("悬停", FlightControlAction.HOVER, {}),
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
    FlightControlAction.HOVER: _spec(
        "flight", "在当前点保持位置不动；自动航线执行中等价于暂停航线并保留断点，"
                  "与紧急停止不同，不终止任务"),
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
    FlightControlAction.GIMBAL_UP: _spec("camera", "云台相对向上调整指定角度；请提供angle_deg；不是调到绝对角度",
        {"angle_deg": ToolParameter("number", minimum=0.1, maximum=90)}),
    FlightControlAction.GIMBAL_DOWN: _spec("camera", "云台相对向下调整指定角度；请提供angle_deg；不是调到绝对角度",
        {"angle_deg": ToolParameter("number", minimum=0.1, maximum=90)}),
    FlightControlAction.GIMBAL_LEFT: _spec("camera", "云台相对向左调整指定角度；请提供angle_deg；不是调到绝对角度",
        {"angle_deg": ToolParameter("number", minimum=0.1, maximum=90)}),
    FlightControlAction.GIMBAL_RIGHT: _spec("camera", "云台相对向右调整指定角度；请提供angle_deg；不是调到绝对角度",
        {"angle_deg": ToolParameter("number", minimum=0.1, maximum=90)}),
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
    FlightControlAction.SPEAKER_SPEAK: _spec('payload', '立即播报明确的喊话文本',
        {'text': ToolParameter('string', min_length=1)}, ('text',)),
    FlightControlAction.SPEAKER_VOLUME: _spec('payload', '设置喊话器音量百分比',
        {'value': ToolParameter('number', minimum=0, maximum=100)}, ('value',)),
    FlightControlAction.SEARCHLIGHT_BRIGHTNESS: _spec('payload', '设置探照灯亮度百分比',
        {'value': ToolParameter('number', minimum=0, maximum=100)}, ('value',)),
    FlightControlAction.TASK_END_CURRENT: _spec('route', '结束临时任务；没有临时任务时结束主线任务'),
    FlightControlAction.TASK_END_ALL: _spec('route', '结束全部任务并清空原始主线断点，不能续飞'),
    FlightControlAction.AI_ALGORITHM_SET: _spec('payload', '启停项目已配置的全局AI算法',
        {'algorithm': ToolParameter('string', min_length=1), 'enabled': _P_ENABLED}, ('algorithm', 'enabled')),
}

if set(ACTION_SPECS) != set(FlightControlAction):  # pragma: no cover - 导入期自检
    raise RuntimeError("ACTION_SPECS must cover every FlightControlAction exactly once")


def action_tool_definitions(execution_mode: str = "simulator") -> tuple[dict[str, Any], ...]:
    """Return serializable per-action tools; handlers never cross the model boundary.

    ``execution_mode`` 由执行器提供（见 ``executor.RuntimeExecutor``），默认值保持
    驾驶舱 UI 现有的 ``simulator`` 契约不变。
    """
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
        "execution_mode": execution_mode,
    } for action in FlightControlAction)


def build_full_control_tool_registry(
    executor: "RuntimeExecutor | MemoryFlightControlSimulator",
    *,
    command_id_factory: Callable[[], str] | None = None,
    idempotency_key_factory: Callable[[str, Mapping[str, Any]], str] | None = None,
) -> ToolRegistry:
    """把完整的 59 个驾驶舱动作注册成模型可调用的白名单工具。

    每个动作都经过 `preview` -> `confirm` 两阶段：模型给出的参数先被 `ToolRegistry`
    按 schema 校验，再由执行器做上下文版本、幂等和预览/确认检查，任何一环不通过都不会改变状态。

    ``executor`` 可以是任何 `RuntimeExecutor`（真实平台适配器、回环执行器、夹具回放器），
    也可以是裸 `MemoryFlightControlSimulator`（自动包成 `SimulatorExecutor`，行为不变）。
    工具名、参数契约与回执字段都来自单一真源，换执行器不改契约：真正变化的只有
    ``execution_mode`` 与动作实际打到的后端。

    幂等键默认每次调用新生成。跨轮次的「恰好执行一次」需要调用方提供稳定的
    `idempotency_key_factory`（例如绑定上层业务的请求号）；模型自己无法提供可靠的幂等键。
    防「未经确认就执行」由 `BoundedAgentLoop` 的确认绑定负责，两者不是同一层保护。
    """
    # 局部导入：executor 模块依赖本模块的类型，顶层导入会成环。
    from .executor import coerce_executor

    runtime = coerce_executor(executor)
    new_command_id = command_id_factory or (lambda: uuid4().hex)
    new_idempotency_key = idempotency_key_factory or (lambda _action, _arguments: uuid4().hex)

    registry = ToolRegistry(runtime_context_provider=runtime.runtime_context)

    for action, spec in ACTION_SPECS.items():
        mutating = action != FlightControlAction.READ_STATE

        def handler(arguments: Mapping[str, Any], action: FlightControlAction = action) -> dict[str, Any]:
            # 上下文版本一律从执行器当前绑定派生，避免与执行器持有的事实漂移。
            context_version = runtime.runtime_context().context_version
            request = FlightControlActionRequest(
                command_id=new_command_id(),
                idempotency_key=new_idempotency_key(action.value, arguments),
                action=action,
                parameters=dict(arguments),
                context_version=context_version,
            )
            receipt = runtime.preview(request)
            if receipt.status is not FlightControlStatus.PREVIEW_REQUIRED:
                # READ_STATE 直接返回；重复、过期、越权与执行器的拒绝也在这里如实返回。
                return receipt.to_dict()
            if receipt.adjustments or MAPPING_MESSAGE in receipt.warnings:
                # The legacy loop confirmed the raw call, not these new effects.
                runtime.cancel(receipt.confirmation_id, command_id=request.command_id,
                    idempotency_key=request.idempotency_key,
                    confirmation_binding=receipt.confirmation_binding, context_version=context_version)
                return replace(receipt, status=FlightControlStatus.REJECTED,
                    confirmation_id=None, confirmation_binding=None,
                    error_code='POLICY_CONFIRMATION_REQUIRED',
                    message='设备参数调整或测绘中断风险需在交互驾驶舱重新确认。').to_dict()
            return runtime.confirm(
                receipt.confirmation_id,
                command_id=request.command_id,
                idempotency_key=request.idempotency_key,
                confirmation_binding=receipt.confirmation_binding,
                context_version=context_version,
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



def action_manifest(execution_mode: str = "simulator") -> dict[str, Any]:
    """Manifest consumed by the cockpit; every UI item has one tool entry."""
    tools = []
    for action, spec in ACTION_SPECS.items():
        tools.append({
            "action": action.value,
            "domain": spec.domain,
            "label": action.value,
            "parameter_names": list(spec.parameters),
            "requires_confirmation": action != FlightControlAction.READ_STATE,
            "execution_mode": execution_mode,
        })
    return {"execution_mode": execution_mode, "tools": tools}


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
