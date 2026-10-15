"""默认关闭的 ktd_backend 适配候选；HTTP 受理不代表设备动作完成。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import copy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

from .full_control import ACTION_SPECS, FlightControlAction, FlightControlActionRequest, FlightControlStatus, RuntimeBinding
from .models import ExecutionContext


_MOVE_TYPES = {"move_forward": 202, "move_backward": 203, "move_left": 204,
               "move_right": 205, "move_up": 201, "move_down": -201}
_ROUTE_TYPES = {"route_pause": 110, "route_resume": 112, "emergency_stop": 113,
                "return_home": 114, "cancel_return_home": -114}
_CAMERA_TYPES = {"camera_photo": 100, "camera_start_recording": 101, "camera_stop_recording": 102}
SUPPORTED_ACTIONS = frozenset(_MOVE_TYPES) | frozenset(_ROUTE_TYPES) | frozenset(_CAMERA_TYPES) | {"gimbal_reset", "read_state"}
_PATHS = frozenset({"/screen/uav/operation/control", "/screen/uav/route/control",
                    "/screen/uav/camera/control", "/screen/uav/gimbal/control"})
_STATE_FIELDS = frozenset({"online", "airborne", "longitude", "latitude", "altitude_m",
                          "height_m", "yaw_deg", "battery_percent", "flight_status", "route_status"})


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 256 and value == value.strip() and all(ord(c) >= 32 and ord(c) != 127 for c in value)


def _finite(value: Any, low: float, high: float) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


class KtdPlatformError(ValueError):
    """仅携带稳定错误码，不携带上游响应或异常文本。"""


@dataclass(frozen=True)
class KtdSnapshot:
    """由服务端权威适配器提供；禁止从浏览器提交的数据构造。"""
    actor_id: str
    project_id: str
    hangar_id: str
    drone_id: str
    site_id: str
    task_id: str | None
    context_version: int
    version: int
    observed_at: float
    control_owner: str | None
    profile: str
    permissions: frozenset[str]
    capabilities: Mapping[str, bool]
    state: Mapping[str, Any]
    control_lease_expires_at: float | None = None


@dataclass(frozen=True)
class KtdCommand:
    path: str
    body: Mapping[str, Any]


@dataclass(frozen=True)
class KtdRequestContext:
    actor_id: str
    project_id: str
    trace_id: str
    idempotency_key: str


@dataclass(frozen=True)
class KtdHttpResponse:
    status_code: int
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class KtdReceipt:
    status: FlightControlStatus | str
    action: str
    command_id: str
    idempotency_key: str
    context_version: int
    state: Mapping[str, Any]
    confirmation_id: str | None = None
    confirmation_binding: str | None = None
    error_code: str | None = None
    message: str = ""
    expires_at: str | None = None
    trace_id: str | None = None
    execution_mode: str = "platform"
    device_effect_verified: bool = False

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["status"] = self.status.value if isinstance(self.status, FlightControlStatus) else self.status
        return result

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> KtdReceipt:
        values = copy.deepcopy(dict(data))
        try:
            values["status"] = FlightControlStatus(values["status"])
        except ValueError:
            pass
        return cls(**values)


class SqliteCommandJournal:
    """单次提交前持久记录 unknown；事务唯一键覆盖并发进程与进程重启。"""
    durable = True

    def __init__(self, path: str | Path):
        self.path = Path(path).absolute()
        if str(path) == ":memory:" or self.path.is_symlink() or not self.path.parent.is_dir():
            raise ValueError("journal requires an existing private directory and a regular file path")
        connection = self._connection()
        try:
            with connection:
                connection.execute("CREATE TABLE IF NOT EXISTS commands (scope TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, receipt TEXT NOT NULL)")
        finally:
            connection.close()
        self.path.chmod(0o600)

    def _connection(self):
        connection = sqlite3.connect(str(self.path), timeout=2.0)
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def read(self, scope: str) -> tuple[str, dict[str, Any]] | None:
        connection = self._connection()
        try:
            row = connection.execute("SELECT fingerprint, receipt FROM commands WHERE scope=?", (scope,)).fetchone()
            return (row[0], json.loads(row[1])) if row else None
        finally:
            connection.close()

    def claim(self, scope: str, fingerprint: str, receipt: Mapping[str, Any]) -> bool:
        connection = self._connection()
        try:
            with connection:
                cursor = connection.execute("INSERT OR IGNORE INTO commands VALUES (?, ?, ?)", (scope, fingerprint, _json(receipt)))
                return cursor.rowcount == 1
        finally:
            connection.close()

    def finish(self, scope: str, fingerprint: str, receipt: Mapping[str, Any]) -> None:
        connection = self._connection()
        try:
            with connection:
                connection.execute("UPDATE commands SET receipt=? WHERE scope=? AND fingerprint=?", (_json(receipt), scope, fingerprint))
        finally:
            connection.close()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class KtdHttpTransport:
    """固定服务端地址与登录身份；无重定向、代理继承或自动重试。"""

    def __init__(self, base_url: str, *, token: str, actor_id: str,
                 timeout_sec: float = 5.0, allow_insecure_http: bool = False,
                 opener: Any = None):
        parsed = urlsplit(base_url)
        if (parsed.scheme not in ({"https", "http"} if allow_insecure_http else {"https"})
                or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path.rstrip("/") not in ("", "/api")):
            raise ValueError("base_url must be a fixed trusted server origin with optional /api")
        if (not isinstance(token, str) or not 1 <= len(token) <= 8192
                or any(ord(character) <= 32 or ord(character) >= 127 for character in token)
                or not _identifier(actor_id) or not _finite(timeout_sec, 0.1, 30)):
            raise ValueError("invalid server transport configuration")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self.actor_id = actor_id
        self._timeout = timeout_sec
        # opener 仅为服务端网络边界注入点；模型与客户端不能指定。
        self._opener = opener if opener is not None else build_opener(ProxyHandler({}), _NoRedirect())

    def submit(self, command: KtdCommand, context: KtdRequestContext) -> KtdHttpResponse:
        if command.path not in _PATHS or context.actor_id != self.actor_id:
            raise KtdPlatformError("TRANSPORT_SCOPE_MISMATCH")
        if not all(_identifier(value) for value in (context.project_id, context.trace_id, context.idempotency_key)):
            raise KtdPlatformError("INVALID_REQUEST_CONTEXT")
        request = Request(self._base_url + command.path, data=_json(command.body).encode(), method="POST",
                          headers={"Authorization": "Bearer " + self._token, "Content-Type": "application/json",
                                   "projectId": context.project_id, "X-Trace-Id": context.trace_id,
                                   "X-Idempotency-Key": context.idempotency_key})
        try:
            response = self._opener.open(request, timeout=self._timeout)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read(65537)
            status = response.code
        if len(raw) > 65536:
            raise KtdPlatformError("INVALID_UPSTREAM_RESPONSE")
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            payload = {}
        return KtdHttpResponse(status, payload if isinstance(payload, dict) else {})


@dataclass(frozen=True)
class _Pending:
    request: FlightControlActionRequest
    receipt: KtdReceipt
    snapshot_hash: str
    command: KtdCommand
    deadline: float


class KtdPlatformExecutor:
    execution_mode = "platform"

    def __init__(self, binding: RuntimeBinding, *, site_id: str, task_id: str | None,
                 transport: Any, snapshot_provider: Callable[[], KtdSnapshot],
                 journal: SqliteCommandJournal, enabled: bool = False,
                 clock: Callable[[], float] = time.time, ttl_sec: float = 30,
                 snapshot_max_age_sec: float = 5):
        binding.validate()
        if (not all(_identifier(value) for key, value in binding.to_dict().items() if key != "context_version")
                or not _identifier(site_id) or (task_id is not None and not _identifier(task_id))
                or type(enabled) is not bool or not _finite(ttl_sec, 0.1, 120)
                or not _finite(snapshot_max_age_sec, 0.1, 30)):
            raise ValueError("invalid platform binding or budget")
        if getattr(journal, "durable", None) is not True:
            raise ValueError("a durable command journal is required")
        if isinstance(transport, KtdHttpTransport) and transport.actor_id != binding.actor_id:
            raise ValueError("transport login identity must match runtime actor")
        self.binding, self.site_id, self.task_id = binding, site_id, task_id
        self._transport, self._provider, self._journal = transport, snapshot_provider, journal
        self._enabled, self._clock, self._ttl, self._max_age = enabled, clock, ttl_sec, snapshot_max_age_sec
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.RLock()

    def runtime_context(self) -> ExecutionContext:
        return ExecutionContext(f"{self.binding.project_id}:{self.binding.hangar_id}:{self.binding.task_batch_id}",
                                self.binding.drone_id, self.binding.context_version, True)

    def _load_snapshot(self) -> KtdSnapshot:
        try:
            snapshot = copy.deepcopy(self._provider())
        except Exception:
            raise KtdPlatformError("AUTHORITATIVE_STATE_UNAVAILABLE") from None
        if not isinstance(snapshot, KtdSnapshot):
            raise KtdPlatformError("AUTHORITATIVE_STATE_UNAVAILABLE")
        return snapshot

    def _validate_snapshot(self, snapshot: KtdSnapshot, action: str) -> None:
        expected = self.binding.to_dict()
        expected.pop("task_batch_id")
        expected.update(site_id=self.site_id, task_id=self.task_id)
        if any(getattr(snapshot, key) != value for key, value in expected.items()):
            raise KtdPlatformError("RESOURCE_BINDING_MISMATCH")
        if (type(snapshot.context_version) is not int or type(snapshot.version) is not int or snapshot.version < 0
                or not _finite(snapshot.observed_at, 0, self._clock())
                or self._clock() - snapshot.observed_at > self._max_age):
            raise KtdPlatformError("AUTHORITATIVE_STATE_STALE")
        if (not isinstance(snapshot.permissions, frozenset) or not isinstance(snapshot.capabilities, Mapping)
                or not isinstance(snapshot.state, Mapping) or action not in snapshot.permissions):
            raise KtdPlatformError("ACTION_NOT_PERMITTED")
        if action != "read_state":
            if snapshot.control_owner != self.binding.actor_id:
                raise KtdPlatformError("CONTROL_NOT_OWNED")
            lease_expires_at = snapshot.control_lease_expires_at
            if not _finite(lease_expires_at, 0, float("inf")):
                raise KtdPlatformError("CONTROL_LEASE_UNAVAILABLE")
            if lease_expires_at <= self._clock():
                raise KtdPlatformError("CONTROL_LEASE_EXPIRED")
            if snapshot.capabilities.get(action) is not True:
                raise KtdPlatformError("CAPABILITY_UNAVAILABLE")
            if snapshot.state.get("online") is not True:
                raise KtdPlatformError("DEVICE_NOT_ONLINE")

    def _snapshot(self, action: str) -> KtdSnapshot:
        snapshot = self._load_snapshot()
        self._validate_snapshot(snapshot, action)
        return snapshot

    def available_actions(self) -> frozenset[str]:
        """仅列出当前权威资源、权限、能力和状态允许预览的已支持动作。"""
        try:
            snapshot = self._load_snapshot()
        except Exception:
            return frozenset()
        available = set()
        for action in SUPPORTED_ACTIONS:
            if action != "read_state" and not self._enabled:
                continue
            try:
                self._validate_snapshot(snapshot, action)
                if action != "read_state":
                    parameters = {"distance_m": 0.01} if action in _MOVE_TYPES else {}
                    request = FlightControlActionRequest("capability-check", "capability-check", action,
                                                         parameters, self.binding.context_version)
                    self._compile(request, snapshot)
                available.add(action)
            except Exception:
                continue
        return frozenset(available)

    def _state(self, snapshot: KtdSnapshot | None) -> dict[str, Any]:
        result = {"execution_mode": "platform", "context_version": self.binding.context_version,
                  "available": snapshot is not None}
        if snapshot is not None:
            result.update({key: copy.deepcopy(value) for key, value in snapshot.state.items() if key in _STATE_FIELDS})
            result.update(snapshot_version=snapshot.version, observed_at=snapshot.observed_at,
                          control_owner=snapshot.control_owner, control_lease_expires_at=snapshot.control_lease_expires_at)
        return result

    def read_state(self) -> Mapping[str, Any]:
        try:
            return self._state(self._snapshot("read_state"))
        except KtdPlatformError as error:
            return {**self._state(None), "error_code": str(error)}

    def _scope(self, key: str) -> str:
        return _hash({"actor": self.binding.actor_id, "project": self.binding.project_id,
                      "drone": self.binding.drone_id, "key": key})

    def _fingerprint(self, request: FlightControlActionRequest) -> str:
        return _hash({"binding": self.binding.to_dict(), "site": self.site_id, "task": self.task_id,
                      "request": request.to_dict()})

    @staticmethod
    def _snapshot_hash(snapshot: KtdSnapshot) -> str:
        values = asdict(snapshot)
        values.pop("observed_at")
        values["permissions"] = sorted(values["permissions"])
        return _hash(values)

    def _receipt(self, request, status, *, snapshot=None, error_code=None, message="", **fields):
        action = request.action.value if isinstance(request.action, FlightControlAction) else request.action
        return KtdReceipt(status, action, request.command_id, request.idempotency_key,
                          self.binding.context_version, self._state(snapshot), error_code=error_code,
                          message=message, **fields)

    def _reject(self, request, code):
        return self._receipt(request, FlightControlStatus.REJECTED, error_code=code,
                             message="请求未执行，请核对能力、权限和当前状态")

    def _compile(self, request: FlightControlActionRequest, snapshot: KtdSnapshot) -> KtdCommand:
        action = FlightControlAction(request.action).value
        body = {"terminalId": self.binding.drone_id, "siteId": self.site_id}
        if self.task_id is not None:
            body["taskId"] = self.task_id
        if action in _MOVE_TYPES:
            if snapshot.profile not in ("DJI_HANGAR", "PZ_HANGAR") or snapshot.capabilities.get("UAV_MOVE_DISTANCE") is not True:
                raise KtdPlatformError("DISTANCE_MODE_UNAVAILABLE")
            if not _finite(request.parameters["distance_m"], 0.01, 20):
                raise KtdPlatformError("DISTANCE_OUT_OF_RANGE")
            height_key = "height_m" if snapshot.profile == "PZ_HANGAR" else "altitude_m"
            if (snapshot.state.get("airborne") is not True
                    or any(not _finite(snapshot.state.get(key), low, high) for key, low, high in
                           (("longitude", -180, 180), ("latitude", -90, 90), (height_key, -500, 10000), ("yaw_deg", -180, 180)))):
                raise KtdPlatformError("FLIGHT_POSE_UNAVAILABLE")
            body.update(type=_MOVE_TYPES[action], mode=2, param=request.parameters["distance_m"])
            return KtdCommand("/screen/uav/operation/control", body)
        if snapshot.profile != "DJI_HANGAR":
            raise KtdPlatformError("PROFILE_UNSUPPORTED")
        if action in _ROUTE_TYPES:
            if action in ("route_pause", "route_resume", "cancel_return_home") and self.task_id is None:
                raise KtdPlatformError("JAVA_TASK_REQUIRED")
            if snapshot.state.get("airborne") is not True:
                raise KtdPlatformError("FLIGHT_STATE_UNAVAILABLE")
            body.update(type=_ROUTE_TYPES[action])
            if action == "return_home":
                body["interrupt"] = False
            return KtdCommand("/screen/uav/route/control", body)
        if action in _CAMERA_TYPES:
            body["type"] = _CAMERA_TYPES[action]
            return KtdCommand("/screen/uav/camera/control", body)
        if action == "gimbal_reset":
            body["type"] = 403
            return KtdCommand("/screen/uav/gimbal/control", body)
        raise KtdPlatformError("ACTION_UNSUPPORTED")

    def preview(self, request: FlightControlActionRequest) -> KtdReceipt:
        with self._lock:
            try:
                if not isinstance(request, FlightControlActionRequest):
                    raise ValueError("invalid request")
                request.validate()
                if not _identifier(request.command_id) or not _identifier(request.idempotency_key):
                    raise ValueError("invalid identifiers")
                action = FlightControlAction(request.action)
                spec = ACTION_SPECS[action]
                if set(request.parameters) - set(spec.parameters) or any(key not in request.parameters for key in spec.required):
                    raise ValueError("invalid schema")
            except (ValueError, TypeError, AttributeError, OverflowError):
                if not isinstance(request, FlightControlActionRequest):
                    request = FlightControlActionRequest("invalid", "invalid", "read_state")
                return self._reject(request, "INVALID_PARAMETERS")
            if request.context_version != self.binding.context_version:
                return self._reject(request, "STALE_CONTEXT")
            if action != FlightControlAction.READ_STATE and not self._enabled:
                return self._reject(request, "PLATFORM_DISABLED")
            if action.value not in SUPPORTED_ACTIONS:
                return self._reject(request, "ACTION_UNSUPPORTED")
            try:
                snapshot = self._snapshot(action.value)
                if action == FlightControlAction.READ_STATE:
                    return self._receipt(request, "observed", snapshot=snapshot, message="已读取权威状态")
                command = self._compile(request, snapshot)
                fingerprint = self._fingerprint(request)
                existing = self._journal.read(self._scope(request.idempotency_key))
                if existing:
                    return KtdReceipt.from_dict(existing[1]) if existing[0] == fingerprint else self._reject(request, "IDEMPOTENCY_CONFLICT")
                for pending in self._pending.values():
                    if pending.request.idempotency_key == request.idempotency_key:
                        return pending.receipt if self._fingerprint(pending.request) == fingerprint else self._reject(request, "IDEMPOTENCY_CONFLICT")
                self._pending = {key: value for key, value in self._pending.items() if value.deadline > self._clock()}
                if len(self._pending) >= 128:
                    return self._reject(request, "PENDING_LIMIT_REACHED")
                confirmation_id, trace_id = uuid4().hex, uuid4().hex
                deadline = self._clock() + self._ttl
                snapshot_hash = self._snapshot_hash(snapshot)
                receipt = self._receipt(request, FlightControlStatus.PREVIEW_REQUIRED, snapshot=snapshot,
                                        confirmation_id=confirmation_id, confirmation_binding=_hash({"request": fingerprint, "snapshot": snapshot_hash}),
                                        expires_at=datetime.fromtimestamp(deadline, timezone.utc).isoformat(), trace_id=trace_id,
                                        message="待确认平台指令，设备效果尚未验证")
                self._pending[confirmation_id] = _Pending(copy.deepcopy(request), receipt, snapshot_hash, command, deadline)
                return receipt
            except KtdPlatformError as error:
                return self._reject(request, str(error))
            except Exception:
                return self._reject(request, "JOURNAL_OR_STATE_UNAVAILABLE")

    def _matches(self, receipt, confirmation_id, command_id, idempotency_key, confirmation_binding, context_version):
        return (type(context_version) is int and receipt.confirmation_id == confirmation_id and receipt.command_id == command_id
                and receipt.idempotency_key == idempotency_key and receipt.confirmation_binding == confirmation_binding
                and receipt.context_version == context_version and context_version == self.binding.context_version)

    def confirm(self, confirmation_id: str, *, command_id: str, idempotency_key: str,
                confirmation_binding: str, context_version: int) -> KtdReceipt:
        with self._lock:
            fallback = FlightControlActionRequest(command_id, idempotency_key, "read_state", {}, context_version)
            if not self._enabled:
                return self._reject(fallback, "PLATFORM_DISABLED")
            pending = self._pending.get(confirmation_id)
            request = pending.request if pending else fallback
            try:
                previous = self._journal.read(self._scope(idempotency_key))
                if previous:
                    receipt = KtdReceipt.from_dict(previous[1])
                    if not self._matches(receipt, confirmation_id, command_id, idempotency_key, confirmation_binding, context_version):
                        return self._reject(request, "CONFIRMATION_MISMATCH")
                    return receipt
                if pending is None:
                    return self._reject(request, "CONFIRMATION_NOT_FOUND")
                if not self._matches(pending.receipt, confirmation_id, command_id, idempotency_key, confirmation_binding, context_version):
                    return self._reject(request, "CONFIRMATION_MISMATCH")
                if self._clock() >= pending.deadline:
                    self._pending.pop(confirmation_id)
                    return self._reject(request, "CONFIRMATION_EXPIRED")
                snapshot = self._snapshot(FlightControlAction(request.action).value)
                command = self._compile(request, snapshot)
                if self._snapshot_hash(snapshot) != pending.snapshot_hash or command != pending.command:
                    self._pending.pop(confirmation_id)
                    return self._reject(request, "AUTHORITATIVE_STATE_CHANGED")
                fingerprint, scope = self._fingerprint(request), self._scope(idempotency_key)
                fields = dict(snapshot=snapshot, confirmation_id=confirmation_id,
                              confirmation_binding=confirmation_binding, trace_id=pending.receipt.trace_id)
                unknown = self._receipt(request, "unknown", error_code="PLATFORM_OUTCOME_UNKNOWN",
                                        message="平台结果未知，禁止自动重发；需要人工核对", **fields)
                if not self._journal.claim(scope, fingerprint, unknown.to_dict()):
                    previous = self._journal.read(scope)
                    return KtdReceipt.from_dict(previous[1]) if previous and previous[0] == fingerprint else self._reject(request, "IDEMPOTENCY_CONFLICT")
                self._pending.pop(confirmation_id)
            except KtdPlatformError as error:
                self._pending.pop(confirmation_id, None)
                return self._reject(request, str(error))
            except Exception:
                return self._reject(request, "JOURNAL_OR_STATE_UNAVAILABLE")
            result = unknown
            try:
                context = KtdRequestContext(self.binding.actor_id, self.binding.project_id, pending.receipt.trace_id, idempotency_key)
                response = self._transport.submit(command, context)
                if isinstance(response, KtdHttpResponse) and isinstance(response.payload, Mapping):
                    code = response.payload.get("code")
                    if response.status_code == 200 and type(code) is int and code == 200:
                        result = self._receipt(request, "submitted", message="平台已受理，设备动作是否完成尚未验证", **fields)
                    elif (200 <= response.status_code < 500 and not 300 <= response.status_code < 400
                          and type(code) is int and 400 <= code < 500):
                        result = self._receipt(request, FlightControlStatus.REJECTED, error_code="PLATFORM_REJECTED",
                                               message="平台拒绝请求，未确认设备效果", **fields)
            except Exception:
                pass  # 已持久记录 unknown；不读取异常文本，不重试。
            try:
                self._journal.finish(scope, fingerprint, result.to_dict())
            except Exception:
                return unknown
            return result

    def cancel(self, confirmation_id: str, *, command_id: str, idempotency_key: str,
               confirmation_binding: str, context_version: int) -> KtdReceipt:
        with self._lock:
            pending = self._pending.get(confirmation_id)
            fallback = FlightControlActionRequest(command_id, idempotency_key, "read_state", {}, context_version)
            if pending is None:
                return self._reject(fallback, "CONFIRMATION_NOT_FOUND")
            if not self._matches(pending.receipt, confirmation_id, command_id, idempotency_key, confirmation_binding, context_version):
                return self._reject(pending.request, "CONFIRMATION_MISMATCH")
            self._pending.pop(confirmation_id)
            return self._receipt(pending.request, FlightControlStatus.CANCELLED, message="待确认指令已取消，未向平台发送")
