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
import re
import sqlite3
import threading
import time
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

from .full_control import ACTION_SPECS, FlightControlAction, FlightControlActionRequest, FlightControlStatus, RuntimeBinding
from .models import ExecutionContext


_MOVE_TYPES = {"move_forward": 202, "move_backward": 203, "move_left": 204,
               "move_right": 205, "move_up": 201, "move_down": -201}
_TURN_TYPES = {"turn_left": -206, "turn_right": 206}
#: gimbal action -> (type, controlMode, param, stepPercentage)
#: 相对俯仰用 5/3 的步进百分比得到一次 5°（KTD 默认 3°），并避开其连续发送线程；
#: 相对偏航保持 1 次 5° 后由 Agent 显式发送停止。
_GIMBAL_TYPES = {
    "gimbal_up": (401, 2, 1, 5.0 / 3.0),
    "gimbal_down": (401, 2, 2, 5.0 / 3.0),
    "gimbal_left": (402, 2, 2, 1.0),
    "gimbal_right": (402, 2, 1, 1.0),
    "gimbal_45": (404, 1, -45, None),
    "gimbal_90": (404, 1, -90, None),
    "gimbal_reset": (403, None, None, None),
}
_RELATIVE_GIMBAL = frozenset({"gimbal_up", "gimbal_down", "gimbal_left", "gimbal_right"})
_STICK_AXES = {"forward": 202, "backward": 203, "left": 204, "right": 205,
               "up": 201, "down": -201, "yaw_right": 206, "yaw_left": -206}
_ROUTE_TYPES = {"route_pause": 110, "route_resume": 112, "emergency_stop": 113,
                "return_home": 114, "cancel_return_home": -114}
_CAMERA_TYPES = {"camera_photo": 100, "camera_start_recording": 101, "camera_stop_recording": 102}
SUPPORTED_ACTIONS = (frozenset(_MOVE_TYPES) | frozenset(_TURN_TYPES)
                     | frozenset(_GIMBAL_TYPES) | frozenset(_ROUTE_TYPES)
                     | frozenset(_CAMERA_TYPES) | {"read_state"})
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
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and low <= value <= high
    except OverflowError:
        return False


def _valid_snapshot_version(value: Any) -> bool:
    return ((type(value) is int and value >= 0)
            or (isinstance(value, str) and re.fullmatch(r'[0-9a-fA-F]{64}', value) is not None))


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON member')
        result[key] = value
    return result


def _reject_json_constant(_value):
    raise ValueError('non-finite JSON number')


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
    version: int | str
    observed_at: float
    control_owner: str | None
    profile: str | None
    permissions: frozenset[str]
    capabilities: Mapping[str, bool]
    state: Mapping[str, Any]
    control_lease_expires_at: float | None = None
    action_availability: Mapping[str, Any] | None = None
    # authoritative = 服务端权威快照；legacy-read-api = 仅用旧只读接口拼装的测试授权。
    authorization_quality: str = "authoritative"


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
    receipt_tracking: bool = False
    parameters: Mapping[str, Any] | None = None
    device_report: Mapping[str, Any] | None = None
    kind: str | None = None
    vendor_report: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["status"] = self.status.value if isinstance(self.status, FlightControlStatus) else self.status
        if self.kind is None:
            result.pop('kind')
        if self.vendor_report is None:
            result.pop('vendor_report')
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

    def finish(self, scope: str, fingerprint: str, receipt: Mapping[str, Any], *, now: float | None = None) -> None:
        connection = self._connection()
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT receipt FROM commands WHERE scope=? AND fingerprint=?", (scope, fingerprint)).fetchone()
                if row is None:
                    raise KtdPlatformError('JOURNAL_RECORD_MISSING')
                result = dict(receipt)
                stored = json.loads(row[0])
                if stored.get('kind') != result.get('kind'):
                    raise KtdPlatformError('JOURNAL_KIND_MISMATCH')
                report = stored.get('device_report')
                if report is not None:
                    incoming = result.get('device_report')
                    if incoming is not None:
                        from .command_receipts import validate_report_update
                        # A GET can finish before the POST that created the command.
                        earlier, later = ((incoming, report) if incoming['version'] <= report['version']
                                          else (report, incoming))
                        validate_report_update(earlier, later, now if now is not None else later['updated_at'])
                        result['device_report'] = later
                    else:
                        result['device_report'] = report
                report = stored.get('vendor_report')
                if report is not None:
                    incoming = result.get('vendor_report')
                    if incoming is not None:
                        from .vendor_control import validate_vendor_report_update
                        earlier, later = ((incoming, report) if incoming['version'] <= report['version']
                                          else (report, incoming))
                        validate_vendor_report_update(earlier, later,
                            now if now is not None else later['updated_at'])
                        result['vendor_report'] = later
                    else:
                        result['vendor_report'] = report
                connection.execute("UPDATE commands SET receipt=? WHERE scope=? AND fingerprint=?", (_json(result), scope, fingerprint))
        finally:
            connection.close()

    def record_report(self, scope: str, fingerprint: str, report: Mapping[str, Any], now: float) -> dict:
        from .command_receipts import validate_report_update
        connection = self._connection()
        try:
            with connection:
                connection.execute('BEGIN IMMEDIATE')
                row = connection.execute('SELECT receipt FROM commands WHERE scope=? AND fingerprint=?',
                                         (scope, fingerprint)).fetchone()
                if row is None:
                    raise KtdPlatformError('JOURNAL_RECORD_MISSING')
                receipt = json.loads(row[0])
                validate_report_update(receipt.get('device_report'), report, now)
                receipt['device_report'] = dict(report)
                connection.execute('UPDATE commands SET receipt=? WHERE scope=? AND fingerprint=?',
                                   (_json(receipt), scope, fingerprint))
                return dict(report)
        finally:
            connection.close()

    def record_vendor_report(self, scope: str, fingerprint: str, report: Mapping[str, Any], now: float) -> dict:
        from .vendor_control import VENDOR_KIND, validate_vendor_report_update
        connection = self._connection()
        try:
            with connection:
                connection.execute('BEGIN IMMEDIATE')
                row = connection.execute('SELECT receipt FROM commands WHERE scope=? AND fingerprint=?',
                                         (scope, fingerprint)).fetchone()
                if row is None:
                    raise KtdPlatformError('JOURNAL_RECORD_MISSING')
                receipt = json.loads(row[0])
                if receipt.get('kind') != VENDOR_KIND:
                    raise KtdPlatformError('JOURNAL_KIND_MISMATCH')
                validate_vendor_report_update(receipt.get('vendor_report'), report, now)
                receipt['vendor_report'] = copy.deepcopy(dict(report))
                connection.execute('UPDATE commands SET receipt=? WHERE scope=? AND fingerprint=?',
                                   (_json(receipt), scope, fingerprint))
                return copy.deepcopy(dict(report))
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
        # /screen/uav/operation/control 用 timestamp 头做同机库指令排序；同一毫秒内
        # 连续下发必须严格递增，否则旧接口会把后一条当作“其它指令已执行”拒绝。
        self._timestamp_lock = threading.Lock()
        self._last_timestamp = 0

    def _next_timestamp(self) -> int:
        with self._timestamp_lock:
            now = int(time.time() * 1000)
            self._last_timestamp = max(self._last_timestamp + 1, now)
            return self._last_timestamp

    def submit(self, command: KtdCommand, context: KtdRequestContext) -> KtdHttpResponse:
        if command.path not in _PATHS or context.actor_id != self.actor_id:
            raise KtdPlatformError("TRANSPORT_SCOPE_MISMATCH")
        if not all(_identifier(value) for value in (context.project_id, context.trace_id, context.idempotency_key)):
            raise KtdPlatformError("INVALID_REQUEST_CONTEXT")
        headers = {"Authorization": "Bearer " + self._token, "Content-Type": "application/json",
                   "projectId": context.project_id, "X-Trace-Id": context.trace_id,
                   "X-Idempotency-Key": context.idempotency_key}
        if command.path == "/screen/uav/operation/control":
            headers["timestamp"] = str(self._next_timestamp())
        request = Request(self._base_url + command.path, data=_json(command.body).encode(), method="POST",
                          headers=headers)
        return self._read_response(request, strict_json='tracking' in command.body)

    def read_snapshot(self, binding: RuntimeBinding, *, site_id: str,
                      task_id: str | None) -> KtdHttpResponse:
        """One server-bound read endpoint; neither callers nor tools supply a URL."""
        if not isinstance(binding, RuntimeBinding) or binding.actor_id != self.actor_id:
            raise KtdPlatformError("TRANSPORT_SCOPE_MISMATCH")
        if (not all(_identifier(value) for value in (binding.project_id, binding.hangar_id,
                                                    binding.drone_id, site_id))
                or (task_id is not None and not _identifier(task_id))):
            raise KtdPlatformError("INVALID_REQUEST_CONTEXT")
        query = {'siteId': site_id, 'hangarId': binding.hangar_id, 'droneId': binding.drone_id}
        if task_id is not None:
            query['taskId'] = task_id
        request = Request(self._base_url + '/lingxi/agent/platform/snapshot?' + urlencode(query),
                          method='GET', headers={'Authorization': 'Bearer ' + self._token,
                                                 'projectId': binding.project_id})
        try:
            return self._read_response(request, strict_json=True)
        except Exception:
            raise KtdPlatformError('SNAPSHOT_TRANSPORT_ERROR') from None

    def read_receipt(self, binding: RuntimeBinding, *, site_id: str,
                     task_id: str | None, command_id: str) -> KtdHttpResponse:
        if not isinstance(binding, RuntimeBinding) or binding.actor_id != self.actor_id:
            raise KtdPlatformError('TRANSPORT_SCOPE_MISMATCH')
        if (not all(_identifier(value) for value in (binding.project_id, binding.hangar_id,
                 binding.drone_id, site_id, command_id)) or (task_id is not None and not _identifier(task_id))):
            raise KtdPlatformError('INVALID_REQUEST_CONTEXT')
        query = {'commandId': command_id, 'siteId': site_id, 'hangarId': binding.hangar_id,
                 'droneId': binding.drone_id}
        if task_id is not None:
            query['taskId'] = task_id
        request = Request(self._base_url + '/lingxi/agent/commands/receipt?' + urlencode(query),
            method='GET', headers={'Authorization': 'Bearer ' + self._token, 'projectId': binding.project_id})
        try:
            return self._read_response(request, strict_json=True)
        except Exception:
            raise KtdPlatformError('COMMAND_RECEIPT_TRANSPORT_ERROR') from None

    def submit_vendor_request(self, command: KtdCommand, context: KtdRequestContext) -> KtdHttpResponse:
        from .vendor_control import VENDOR_METHODS
        required = {'schemaVersion', 'commandId', 'idempotencyKey', 'traceId', 'method',
                    'siteId', 'hangarId', 'droneId', 'snapshotVersion', 'expiresAt'}
        body = command.body
        if (command.path != '/screen/uav/operation/control/vendor-request'
                or context.actor_id != self.actor_id or not isinstance(body, Mapping)
                or set(body) not in (required, required | {'taskId'})
                or body.get('schemaVersion') != 'lingxi-vendor-control-request/v1'
                or body.get('method') not in VENDOR_METHODS
                or body.get('idempotencyKey') != context.idempotency_key
                or body.get('traceId') != context.trace_id
                or not isinstance(body.get('snapshotVersion'), str)
                or not _valid_snapshot_version(body['snapshotVersion'])
                or not _finite(body.get('expiresAt'), 0, float('inf'))
                or not all(_identifier(value) for value in (context.project_id, context.trace_id,
                    context.idempotency_key, body.get('commandId'), body.get('siteId'),
                    body.get('hangarId'), body.get('droneId')))
                or ('taskId' in body and not _identifier(body['taskId']))):
            raise KtdPlatformError('INVALID_VENDOR_REQUEST')
        request = Request(self._base_url + command.path, data=_json(body).encode(), method='POST',
            headers={'Authorization': 'Bearer ' + self._token, 'Content-Type': 'application/json',
                     'projectId': context.project_id, 'X-Trace-Id': context.trace_id,
                     'X-Idempotency-Key': context.idempotency_key})
        return self._read_response(request, strict_json=True)

    def read_vendor_receipt(self, binding: RuntimeBinding, *, site_id: str,
                            task_id: str | None, command_id: str, method: str) -> KtdHttpResponse:
        from .vendor_control import VENDOR_METHODS
        if not isinstance(binding, RuntimeBinding) or binding.actor_id != self.actor_id:
            raise KtdPlatformError('TRANSPORT_SCOPE_MISMATCH')
        if (method not in VENDOR_METHODS or not all(_identifier(value) for value in
                (binding.project_id, binding.hangar_id, binding.drone_id, site_id, command_id))
                or (task_id is not None and not _identifier(task_id))):
            raise KtdPlatformError('INVALID_REQUEST_CONTEXT')
        query = {'commandId': command_id, 'method': method, 'siteId': site_id,
                 'hangarId': binding.hangar_id, 'droneId': binding.drone_id}
        if task_id is not None:
            query['taskId'] = task_id
        request = Request(self._base_url + '/lingxi/agent/commands/vendor-receipt?' + urlencode(query),
            method='GET', headers={'Authorization': 'Bearer ' + self._token, 'projectId': binding.project_id})
        try:
            return self._read_response(request, strict_json=True)
        except Exception:
            raise KtdPlatformError('VENDOR_RECEIPT_TRANSPORT_ERROR') from None

    def read_visuals(self, binding: RuntimeBinding, *, site_id: str,
                     task_id: str | None) -> KtdHttpResponse:
        """Read stored display descriptions only; never fetch or start a stream."""
        if not isinstance(binding, RuntimeBinding) or binding.actor_id != self.actor_id:
            raise KtdPlatformError('TRANSPORT_SCOPE_MISMATCH')
        if (not all(_identifier(value) for value in (binding.project_id, binding.hangar_id,
                 binding.drone_id, site_id)) or (task_id is not None and not _identifier(task_id))):
            raise KtdPlatformError('INVALID_REQUEST_CONTEXT')
        query = {'siteId': site_id, 'hangarId': binding.hangar_id, 'droneId': binding.drone_id}
        if task_id is not None:
            query['taskId'] = task_id
        request = Request(self._base_url + '/lingxi/agent/platform/visuals?' + urlencode(query),
            method='GET', headers={'Authorization': 'Bearer ' + self._token, 'projectId': binding.project_id})
        try:
            return self._read_response(request, strict_json=True)
        except Exception:
            raise KtdPlatformError('VISUALS_TRANSPORT_ERROR') from None

    def read_legacy(self, resource: str, *, project_id: str, drone_id: str | None = None,
                    query: Mapping[str, str] | None = None) -> KtdHttpResponse:
        """读取旧只读接口；仅用于服务端 route A 测试授权。

        ``resource`` 只接受固定白名单；调用方不能提交任意路径。这些接口不发送控制
        指令、不主动写业务表，但个别旧展示接口可能触发其自身的缓存过期清理；旧接口
        不提供设备采样/接收时间或控制权租约，返回的事实由调用方标注为 legacy。
        """
        paths = {
            "user_info": "/user/info",
            "sites": "/uavSite/list",
            "owners": "/uavControlPower/list",
            "hangar_state": "/portal/task/hangar-view/state",
            "pose": "/test/getUdpData",
        }
        if resource == "permissions":
            if (not isinstance(drone_id, str)
                    or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", drone_id) is None):
                raise KtdPlatformError("INVALID_LEGACY_RESOURCE")
            path = "/screen/control/permissions/3/" + drone_id
        elif resource in paths:
            path = paths[resource]
        else:
            raise KtdPlatformError("INVALID_LEGACY_RESOURCE")
        if not _identifier(project_id):
            raise KtdPlatformError("INVALID_REQUEST_CONTEXT")
        if query is None:
            query = {}
        if (not isinstance(query, Mapping)
                or any(not isinstance(key, str) or not isinstance(value, str)
                       for key, value in query.items())):
            raise KtdPlatformError("INVALID_LEGACY_RESOURCE")
        suffix = "?" + urlencode(query) if query else ""
        request = Request(self._base_url + path + suffix, method="GET",
                          headers={"Authorization": "Bearer " + self._token,
                                   "projectId": project_id})
        try:
            return self._read_response(request, strict_json=True)
        except Exception:
            raise KtdPlatformError("LEGACY_STATE_TRANSPORT_ERROR") from None

    def _read_response(self, request: Request, *, strict_json: bool = False) -> KtdHttpResponse:
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
            payload = (json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_json_object,
                                  parse_constant=_reject_json_constant)
                       if strict_json else json.loads(raw))
        except (ValueError, UnicodeError):
            payload = {}
        return KtdHttpResponse(status, payload if isinstance(payload, dict) else {})


@dataclass(frozen=True)
class _Pending:
    request: Any
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
                 snapshot_max_age_sec: float = 5, track_commands: bool = False,
                 enable_vendor_requests: bool = False, allowed_vendor_methods: tuple[str, ...] = (),
                 legacy_read_api: bool = False, allow_unverified_pose: bool = False,
                 pose_reader: Callable[[], Mapping[str, Any] | None] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 turn_tolerance_deg: float = 3.0, turn_max_sec: float = 30.0,
                 turn_max_pulses: int = 80, turn_gain: float = 1.5,
                 stick_max_sec: float = 5.0):
        binding.validate()
        if (not all(_identifier(value) for key, value in binding.to_dict().items() if key != "context_version")
                or not _identifier(site_id) or (task_id is not None and not _identifier(task_id))
                or type(enabled) is not bool or type(track_commands) is not bool or not _finite(ttl_sec, 0.1, 120)
                or not _finite(snapshot_max_age_sec, 0.1, 30)
                or type(legacy_read_api) is not bool or type(allow_unverified_pose) is not bool
                or (allow_unverified_pose and not legacy_read_api)):
            raise ValueError("invalid platform binding or budget")
        if (not _finite(turn_tolerance_deg, 0.1, 45) or not _finite(turn_max_sec, 1, 120)
                or isinstance(turn_max_pulses, bool) or not isinstance(turn_max_pulses, int)
                or not 1 <= turn_max_pulses <= 500 or not _finite(turn_gain, 0.1, 10)
                or not _finite(stick_max_sec, 0.2, 30)
                or not callable(sleep)
                or (pose_reader is not None and not callable(pose_reader))):
            raise ValueError("invalid attitude control budget")
        if getattr(journal, "durable", None) is not True:
            raise ValueError("a durable command journal is required")
        if isinstance(transport, KtdHttpTransport) and transport.actor_id != binding.actor_id:
            raise ValueError("transport login identity must match runtime actor")
        from .vendor_control import VENDOR_METHODS
        if (type(enable_vendor_requests) is not bool or not isinstance(allowed_vendor_methods, (tuple, list))
                or any(not isinstance(method, str) or method not in VENDOR_METHODS for method in allowed_vendor_methods)
                or len(set(allowed_vendor_methods)) != len(allowed_vendor_methods)):
            raise ValueError('invalid vendor request configuration')
        self.binding, self.site_id, self.task_id = binding, site_id, task_id
        self._transport, self._provider, self._journal = transport, snapshot_provider, journal
        self._enabled, self._clock, self._ttl, self._max_age = enabled, clock, ttl_sec, snapshot_max_age_sec
        self._track_commands = track_commands
        self._enable_vendor_requests = enable_vendor_requests
        self._allowed_vendor_methods = tuple(allowed_vendor_methods)
        self._legacy_read_api = legacy_read_api
        self._allow_unverified_pose = allow_unverified_pose
        self._pose_reader = pose_reader
        if self._pose_reader is None:
            candidate = getattr(snapshot_provider, "pose", None)
            self._pose_reader = candidate if callable(candidate) else None
        self._sleep = sleep
        self._turn_tolerance = float(turn_tolerance_deg)
        self._turn_max_sec = float(turn_max_sec)
        self._turn_max_pulses = turn_max_pulses
        self._turn_gain = float(turn_gain)
        self._stick_max_sec = float(stick_max_sec)
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.RLock()

    @property
    def vendor_request_methods(self) -> tuple[str, ...]:
        return self._allowed_vendor_methods if self._enable_vendor_requests else ()

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
        expected_quality = "legacy-read-api" if self._legacy_read_api else "authoritative"
        if snapshot.authorization_quality != expected_quality:
            raise KtdPlatformError("AUTHORIZATION_QUALITY_MISMATCH")
        if (type(snapshot.context_version) is not int or not _valid_snapshot_version(snapshot.version)
                or not _finite(snapshot.observed_at, 0, self._clock())
                or self._clock() - snapshot.observed_at > self._max_age):
            raise KtdPlatformError("AUTHORITATIVE_STATE_STALE")
        if (not isinstance(snapshot.permissions, frozenset) or not isinstance(snapshot.capabilities, Mapping)
                or not isinstance(snapshot.state, Mapping) or action not in snapshot.permissions):
            raise KtdPlatformError("ACTION_NOT_PERMITTED")
        if snapshot.action_availability is not None:
            entry = snapshot.action_availability.get(action) if isinstance(snapshot.action_availability, Mapping) else None
            if (not isinstance(entry, Mapping) or entry.get('available') is not True
                    or entry.get('reasons') != []):
                raise KtdPlatformError('ACTION_UNAVAILABLE')
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

    def _validate_vendor_snapshot(self, snapshot: KtdSnapshot) -> None:
        self._validate_snapshot(snapshot, 'read_state')
        if self._clock() - snapshot.observed_at > min(self._max_age, 5):
            raise KtdPlatformError('AUTHORITATIVE_STATE_STALE')
        if snapshot.profile != 'DJI_HANGAR':
            raise KtdPlatformError('PROFILE_UNSUPPORTED')
        if not isinstance(snapshot.version, str) or not _valid_snapshot_version(snapshot.version):
            raise KtdPlatformError('TRACKED_SNAPSHOT_VERSION_REQUIRED')
        if snapshot.control_owner != self.binding.actor_id:
            raise KtdPlatformError('CONTROL_NOT_OWNED')
        if not _finite(snapshot.control_lease_expires_at, 0, float('inf')):
            raise KtdPlatformError('CONTROL_LEASE_UNAVAILABLE')
        if snapshot.control_lease_expires_at <= self._clock():
            raise KtdPlatformError('CONTROL_LEASE_EXPIRED')
        if snapshot.state.get('online') is not True:
            raise KtdPlatformError('DEVICE_NOT_ONLINE')

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
                    if action in _MOVE_TYPES:
                        parameters = {"distance_m": 0.01}
                    elif action in _TURN_TYPES:
                        parameters = {"angle_deg": 1}
                    else:
                        parameters = {}
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
            # legacy 快照的租约是合成值，不向界面/回执伪装成权威租约。
            lease = (snapshot.control_lease_expires_at
                     if snapshot.authorization_quality == "authoritative" else None)
            result.update(snapshot_version=snapshot.version, observed_at=snapshot.observed_at,
                          control_owner=snapshot.control_owner, control_lease_expires_at=lease,
                          authorization_quality=snapshot.authorization_quality)
            if snapshot.action_availability is not None:
                result['action_availability'] = copy.deepcopy(dict(snapshot.action_availability))
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
        if values.get("authorization_quality") != "authoritative":
            # legacy 只读接口没有真实租约，合成租约不能进入确认绑定；
            # 姿态是实时遥测，确认前会漂移，也不能把预览瞬间的经纬度/航向锁进绑定。
            # owner、在线/飞行状态等可核验事实仍然参与哈希，变化即拒绝旧确认卡。
            values.pop("control_lease_expires_at", None)
            state = dict(values.get("state") or {})
            for key in ("longitude", "latitude", "altitude_m", "height_m", "yaw_deg"):
                state.pop(key, None)
            values["state"] = state
        values["permissions"] = sorted(values["permissions"])
        return _hash(values)

    def _receipt(self, request, status, *, snapshot=None, error_code=None, message="", **fields):
        from .vendor_control import VendorControlRequest, VENDOR_KIND
        action = request.action.value if isinstance(request.action, FlightControlAction) else request.action
        vendor = isinstance(request, VendorControlRequest)
        if vendor:
            fields.setdefault('kind', VENDOR_KIND)
        return KtdReceipt(status, action, request.command_id, request.idempotency_key,
                          self.binding.context_version, self._state(snapshot), error_code=error_code,
                          message=message, parameters=copy.deepcopy(dict(request.parameters)),
                          receipt_tracking=vendor or self._track_commands and action in _MOVE_TYPES, **fields)

    def _reject(self, request, code):
        return self._receipt(request, FlightControlStatus.REJECTED, error_code=code,
                             message="请求未执行，请核对能力、权限和当前状态")

    def _compile(self, request: FlightControlActionRequest, snapshot: KtdSnapshot) -> KtdCommand:
        action = FlightControlAction(request.action).value
        if self._track_commands:
            if action not in _MOVE_TYPES or snapshot.profile != 'DJI_HANGAR':
                raise KtdPlatformError('TRACKING_UNSUPPORTED')
            if not isinstance(snapshot.version, str) or not _valid_snapshot_version(snapshot.version):
                raise KtdPlatformError('TRACKED_SNAPSHOT_VERSION_REQUIRED')
        body = {"terminalId": self.binding.drone_id, "siteId": self.site_id}
        if self.task_id is not None:
            body["taskId"] = self.task_id
        if action in _MOVE_TYPES:
            if snapshot.profile not in ("DJI_HANGAR", "PZ_HANGAR") or snapshot.capabilities.get("UAV_MOVE_DISTANCE") is not True:
                raise KtdPlatformError("DISTANCE_MODE_UNAVAILABLE")
            if not _finite(request.parameters["distance_m"], 0.01, 20):
                raise KtdPlatformError("DISTANCE_OUT_OF_RANGE")
            if self._legacy_read_api:
                # 旧只读接口没有机头坐标和高度，无法在 Agent 侧重算目标点或
                # 证明 20 米隐含爬升边界；这里只允许测试环境显式打开的路径，
                # 由 Java 原控制接口用服务端实时姿态计算目标。
                if not self._allow_unverified_pose:
                    raise KtdPlatformError("LEGACY_POSE_UNVERIFIED_DISABLED")
                if snapshot.state.get("airborne") is not True:
                    raise KtdPlatformError("FLIGHT_STATE_UNAVAILABLE")
                body.update(type=_MOVE_TYPES[action], mode=2, param=request.parameters["distance_m"])
                return KtdCommand("/screen/uav/operation/control", body)
            height_key = "height_m" if snapshot.profile == "PZ_HANGAR" else "altitude_m"
            if (snapshot.state.get("airborne") is not True
                    or any(not _finite(snapshot.state.get(key), low, high) for key, low, high in
                           (("longitude", -180, 180), ("latitude", -90, 90), (height_key, -500, 10000), ("yaw_deg", -180, 180)))):
                raise KtdPlatformError("FLIGHT_POSE_UNAVAILABLE")
            if snapshot.profile == "DJI_HANGAR":
                relative_height = snapshot.state.get("height_m")
                if not _finite(relative_height, -500, 10000):
                    raise KtdPlatformError("RELATIVE_HEIGHT_UNAVAILABLE")
                # 厂商flyto在当前相对高度低于20米时会先爬升，超出单一相对移动意图。
                if relative_height < 20:
                    raise KtdPlatformError("DJI_FLYTO_IMPLICIT_CLIMB")
                vertical = request.parameters["distance_m"] * (1 if action == "move_up" else -1 if action == "move_down" else 0)
                # 目标低于20米的实际行为尚未核验；这是项目保守边界，不冒称厂商禁令。
                if relative_height + vertical < 20:
                    raise KtdPlatformError("DJI_LOW_TARGET_UNVERIFIED")
                if not _finite(snapshot.state["altitude_m"] + vertical, 2, 10000):
                    raise KtdPlatformError("DJI_TARGET_ALTITUDE_OUT_OF_RANGE")
            body.update(type=_MOVE_TYPES[action], mode=2, param=request.parameters["distance_m"])
            return KtdCommand("/screen/uav/operation/control", body)
        if snapshot.profile != "DJI_HANGAR":
            raise KtdPlatformError("PROFILE_UNSUPPORTED")
        if action in _TURN_TYPES:
            # 新Java飞控服务尚未声明转向能力；只在旧控制接口的 legacy 路径执行。
            if self._track_commands:
                raise KtdPlatformError("TRACKING_UNSUPPORTED")
            if not _finite(request.parameters.get("angle_deg"), 0.1, 360):
                raise KtdPlatformError("ANGLE_OUT_OF_RANGE")
            if snapshot.state.get("airborne") is not True:
                raise KtdPlatformError("FLIGHT_STATE_UNAVAILABLE")
            if not _finite(snapshot.state.get("yaw_deg"), -360, 360):
                raise KtdPlatformError("POSE_UNAVAILABLE")
            # 首个脉冲；确认后的闭环会按航向误差继续脉冲，并在结束时回中、停止记录。
            body.update(type=_TURN_TYPES[action], mode=3, param=10)
            return KtdCommand("/screen/uav/operation/control", body)
        if action in _GIMBAL_TYPES:
            if self._track_commands and action != "gimbal_reset":
                raise KtdPlatformError("TRACKING_UNSUPPORTED")
            gimbal_type, control_mode, param, step = _GIMBAL_TYPES[action]
            body["type"] = gimbal_type
            if control_mode is not None:
                body["controlMode"] = control_mode
            if param is not None:
                body["param"] = param
            if step is not None:
                body["stepPercentage"] = step
            return KtdCommand("/screen/uav/gimbal/control", body)
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
        raise KtdPlatformError("ACTION_UNSUPPORTED")

    # ---- 姿态控制：旧控制接口 + 姿态回读闭环 -----------------------------

    @staticmethod
    def _normalize_heading(value: float) -> float:
        return value % 360.0

    @staticmethod
    def _signed_error(target: float, current: float) -> float:
        return (target - current + 180.0) % 360.0 - 180.0

    def _read_pose(self) -> Mapping[str, Any] | None:
        """优先用注入的姿态回读；没有时退回最近一次快照里的姿态。"""
        try:
            if self._pose_reader is not None:
                pose = self._pose_reader()
            else:
                snapshot = self._load_snapshot()
                pose = {**dict(snapshot.state), "observed_at": snapshot.observed_at}
        except Exception:
            return None
        if not isinstance(pose, Mapping):
            return None
        yaw_value = pose.get("yaw_deg")
        if not _finite(yaw_value, -360, 360):
            return None
        yaw = float(yaw_value)
        observed = pose.get("observed_at")
        if not _finite(observed, 0, self._clock() + 1):
            observed = None
        return {"yaw_deg": float(yaw), "observed_at": observed,
                "longitude": pose.get("longitude"), "latitude": pose.get("latitude"),
                "altitude_m": pose.get("altitude_m"), "height_m": pose.get("height_m")}

    def _control_body(self, *, type_: int, mode: int, param: float) -> dict[str, Any]:
        body = {"terminalId": self.binding.drone_id, "siteId": self.site_id,
                "type": type_, "mode": mode, "param": param}
        if self.task_id is not None:
            body["taskId"] = self.task_id
        return body

    def _submit_body(self, path: str, body: Mapping[str, Any], *, trace_id: str,
                     idempotency_key: str, counter: int) -> bool:
        context = KtdRequestContext(self.binding.actor_id, self.binding.project_id, trace_id,
                                    _hash({"key": idempotency_key, "n": counter}))
        try:
            response = self._transport.submit(KtdCommand(path, dict(body)), context)
        except Exception:
            return False
        return (isinstance(response, KtdHttpResponse) and response.status_code == 200
                and isinstance(response.payload, Mapping)
                and type(response.payload.get("code")) is int
                and response.payload.get("code") == 200)

    def _send_operation(self, body: Mapping[str, Any], *, trace_id: str,
                        idempotency_key: str, counter: int) -> bool:
        return self._submit_body("/screen/uav/operation/control", body, trace_id=trace_id,
                                 idempotency_key=idempotency_key, counter=counter)

    def _neutralize(self, *, trace_id: str, idempotency_key: str, counter: int) -> None:
        """回中后删除 Java 侧的运动指令记录；两步都 best-effort。"""
        self._send_operation(self._control_body(type_=206, mode=3, param=0),
                             trace_id=trace_id, idempotency_key=idempotency_key,
                             counter=counter)
        self._send_operation(self._control_body(type_=207, mode=3, param=0),
                             trace_id=trace_id, idempotency_key=idempotency_key,
                             counter=counter + 1)

    def _run_turn(self, request, snapshot, *, trace_id: str) -> tuple[str, str]:
        action = FlightControlAction(request.action).value
        angle = float(request.parameters["angle_deg"])
        sign = 1.0 if action == "turn_right" else -1.0
        start_yaw = self._normalize_heading(float(snapshot.state["yaw_deg"]))
        target = self._normalize_heading(start_yaw + sign * angle)
        started, counter = self._clock(), 0
        previous_error = previous_yaw = None
        stagnant = 0
        try:
            for pulse in range(self._turn_max_pulses):
                pose = self._read_pose()
                if pose is None:
                    return "POSE_UNAVAILABLE", "无法回读姿态，已停止转向"
                if (pose["observed_at"] is not None
                        and self._clock() - pose["observed_at"] > min(self._max_age, 5.0)):
                    return "POSE_STALE", "姿态回读过期，已停止转向"
                error = self._signed_error(target, pose["yaw_deg"])
                absolute = abs(error)
                if previous_error is not None:
                    # 方向约定错误或姿态读数不跟随都会让误差变大；宁可停，不继续盲转。
                    if absolute > previous_error + 5.0:
                        return "TURN_DIVERGING", "航向误差增大，已停止转向"
                    if abs(pose["yaw_deg"] - previous_yaw) < 0.2:
                        stagnant += 1
                        if stagnant >= 6:
                            return "TURN_NOT_RESPONDING", "航向未变化，已停止转向"
                    else:
                        stagnant = 0
                previous_error, previous_yaw = absolute, pose["yaw_deg"]
                if absolute <= self._turn_tolerance:
                    return "OK", f"航向 {pose['yaw_deg']:.1f}°，目标 {target:.1f}°"
                if self._clock() - started > self._turn_max_sec:
                    return ("TURN_TIMEOUT",
                            f"已转向 {pose['yaw_deg']:.1f}°，未到达目标 {target:.1f}°")
                percent = min(50.0, max(8.0, absolute * self._turn_gain))
                type_ = 206 if error > 0 else -206
                if not self._send_operation(self._control_body(type_=type_, mode=3, param=percent),
                                            trace_id=trace_id,
                                            idempotency_key=request.idempotency_key,
                                            counter=counter):
                    return "PLATFORM_REJECTED", "平台拒绝了转向脉冲"
                counter += 1
                self._sleep(0.3)
                if not self._send_operation(self._control_body(type_=206, mode=3, param=0),
                                            trace_id=trace_id,
                                            idempotency_key=request.idempotency_key,
                                            counter=counter):
                    return "PLATFORM_REJECTED", "平台拒绝了回中指令"
                counter += 1
                self._sleep(0.15)
                if pulse % 4 == 3:
                    current = self._load_snapshot()
                    if current.control_owner != self.binding.actor_id:
                        return "CONTROL_NOT_OWNED", "控制权已变化，已停止转向"
            return "TURN_TOLERANCE_NOT_REACHED", "转向脉冲次数已耗尽"
        finally:
            self._neutralize(trace_id=trace_id, idempotency_key=request.idempotency_key,
                             counter=counter + 1)

    def _finish_gimbal(self, request, *, trace_id: str) -> bool:
        """相对云台控制可能启动 Java 侧连续发送线程；这里显式发停止。"""
        action = FlightControlAction(request.action).value
        gimbal_type, control_mode, _param, _step = _GIMBAL_TYPES[action]
        if control_mode != 2:
            return True
        self._sleep(0.3)
        body = {"terminalId": self.binding.drone_id, "siteId": self.site_id,
                "type": gimbal_type, "controlMode": 2, "param": 0}
        if self.task_id is not None:
            body["taskId"] = self.task_id
        return self._submit_body("/screen/uav/gimbal/control", body, trace_id=trace_id,
                                 idempotency_key=request.idempotency_key, counter=1)

    def stick(self, axis: str, percent: float, duration_sec: float, *,
              idempotency_key: str | None = None) -> dict[str, Any]:
        """有界摇杆脉冲；不是模型工具，只供测试环境操作者或脚本直接调用。

        旧接口一次只能携带一个轴，因此这里按单轴下发；结束时总会回中并删除指令记录。
        """
        if axis not in _STICK_AXES:
            return {"status": "rejected", "error_code": "INVALID_STICK_AXIS"}
        if not _finite(percent, 1, 100) or not _finite(duration_sec, 0.2, self._stick_max_sec):
            return {"status": "rejected", "error_code": "INVALID_STICK_PARAMETERS"}
        if not self._enabled:
            return {"status": "rejected", "error_code": "PLATFORM_DISABLED"}
        try:
            snapshot = self._load_snapshot()
            self._validate_snapshot(snapshot, "read_state")
            if snapshot.control_owner != self.binding.actor_id:
                return {"status": "rejected", "error_code": "CONTROL_NOT_OWNED"}
            if (snapshot.state.get("online") is not True
                    or snapshot.state.get("airborne") is not True):
                return {"status": "rejected", "error_code": "FLIGHT_STATE_UNAVAILABLE"}
            start = self._read_pose()
            if start is None:
                return {"status": "rejected", "error_code": "POSE_UNAVAILABLE"}
        except KtdPlatformError as error:
            return {"status": "rejected", "error_code": str(error)}
        key = idempotency_key or uuid4().hex
        trace_id = uuid4().hex
        type_ = _STICK_AXES[axis]
        deadline, counter = self._clock() + float(duration_sec), 0
        status, error_code = "accepted", None
        try:
            while True:
                if not self._send_operation(self._control_body(type_=type_, mode=3,
                                                               param=float(percent)),
                                            trace_id=trace_id, idempotency_key=key,
                                            counter=counter):
                    status, error_code = "unknown", "PLATFORM_REJECTED"
                    break
                counter += 1
                remaining = deadline - self._clock()
                if remaining <= 0:
                    break
                self._sleep(min(0.8, remaining))
                if counter >= int(duration_sec / 0.5) + 2:
                    break
        finally:
            self._neutralize(trace_id=trace_id, idempotency_key=key, counter=counter + 1)
        return {"status": status, "error_code": error_code, "axis": axis,
                "percent": float(percent), "duration_sec": float(duration_sec),
                "start_pose": start, "end_pose": self._read_pose()}

    def _compile_vendor_request(self, request, snapshot, trace_id, deadline) -> KtdCommand:
        if not _finite(deadline, 0, float('inf')) or deadline <= self._clock():
            raise KtdPlatformError('CONFIRMATION_EXPIRED')
        body = {'schemaVersion': 'lingxi-vendor-control-request/v1',
            'commandId': request.command_id, 'idempotencyKey': request.idempotency_key,
            'traceId': trace_id, 'method': request.method, 'siteId': self.site_id,
            'hangarId': self.binding.hangar_id, 'droneId': self.binding.drone_id,
            'snapshotVersion': snapshot.version, 'expiresAt': deadline}
        if self.task_id is not None:
            body['taskId'] = self.task_id
        return KtdCommand('/screen/uav/operation/control/vendor-request', body)

    def preview_vendor_request(self, request) -> KtdReceipt:
        from .vendor_control import VendorControlRequest
        with self._lock:
            try:
                if not isinstance(request, VendorControlRequest):
                    raise ValueError('invalid request')
                request.validate()
            except (ValueError, TypeError, AttributeError, OverflowError):
                fallback = VendorControlRequest('invalid', 'invalid', 'flight_authority_grab',
                                                self.binding.context_version)
                return self._reject(fallback, 'INVALID_PARAMETERS')
            if request.context_version != self.binding.context_version:
                return self._reject(request, 'STALE_CONTEXT')
            if not self._enable_vendor_requests:
                return self._reject(request, 'VENDOR_REQUESTS_DISABLED')
            if request.method not in self._allowed_vendor_methods:
                return self._reject(request, 'VENDOR_METHOD_NOT_ALLOWED')
            try:
                snapshot = self._load_snapshot()
                self._validate_vendor_snapshot(snapshot)
                fingerprint = self._fingerprint(request)
                existing = self._journal.read(self._scope(request.idempotency_key))
                if existing:
                    return KtdReceipt.from_dict(existing[1]) if existing[0] == fingerprint else self._reject(request, 'IDEMPOTENCY_CONFLICT')
                for pending in self._pending.values():
                    if pending.request.idempotency_key == request.idempotency_key:
                        return pending.receipt if self._fingerprint(pending.request) == fingerprint else self._reject(request, 'IDEMPOTENCY_CONFLICT')
                self._pending = {key: value for key, value in self._pending.items() if value.deadline > self._clock()}
                if len(self._pending) >= 128:
                    return self._reject(request, 'PENDING_LIMIT_REACHED')
                confirmation_id, trace_id = uuid4().hex, uuid4().hex
                deadline = min(self._clock() + self._ttl, snapshot.observed_at + min(self._max_age, 5),
                               snapshot.control_lease_expires_at)
                command = self._compile_vendor_request(request, snapshot, trace_id, deadline)
                snapshot_hash = self._snapshot_hash(snapshot)
                receipt = self._receipt(request, FlightControlStatus.PREVIEW_REQUIRED, snapshot=snapshot,
                    confirmation_id=confirmation_id, confirmation_binding=_hash({'kind': request.kind,
                        'request': fingerprint, 'snapshot': snapshot_hash, 'command': asdict(command), 'deadline': deadline}),
                    expires_at=datetime.fromtimestamp(deadline, timezone.utc).isoformat(), trace_id=trace_id,
                    message='待确认独立厂商请求；不代表厂商权限、DRC连接或设备动作已生效')
                self._pending[confirmation_id] = _Pending(copy.deepcopy(request), receipt, snapshot_hash, command, deadline)
                return receipt
            except KtdPlatformError as error:
                return self._reject(request, str(error))
            except Exception:
                return self._reject(request, 'JOURNAL_OR_STATE_UNAVAILABLE')

    @staticmethod
    def _request_from_receipt(receipt):
        from .vendor_control import VendorControlRequest, VENDOR_ACTION, VENDOR_KIND
        if not isinstance(receipt.parameters, Mapping):
            raise KtdPlatformError('CONFIRMATION_MISMATCH')
        if receipt.kind == VENDOR_KIND:
            if receipt.action != VENDOR_ACTION or set(receipt.parameters) != {'method'}:
                raise KtdPlatformError('CONFIRMATION_MISMATCH')
            request = VendorControlRequest(receipt.command_id, receipt.idempotency_key,
                                           receipt.parameters['method'], receipt.context_version)
        elif receipt.kind is None:
            request = FlightControlActionRequest(receipt.command_id, receipt.idempotency_key,
                receipt.action, dict(receipt.parameters), receipt.context_version)
        else:
            raise KtdPlatformError('CONFIRMATION_MISMATCH')
        request.validate()
        return request

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
        from .vendor_control import VendorControlRequest, VENDOR_KIND
        with self._lock:
            fallback = FlightControlActionRequest(command_id, idempotency_key, "read_state", {}, context_version)
            pending = self._pending.get(confirmation_id)
            request = pending.request if pending else fallback
            try:
                previous = self._journal.read(self._scope(idempotency_key))
                vendor = isinstance(request, VendorControlRequest) or bool(previous and previous[1].get('kind') == VENDOR_KIND)
                if not vendor and not self._enabled:
                    return self._reject(request, 'PLATFORM_DISABLED')
                if previous:
                    receipt = KtdReceipt.from_dict(previous[1])
                    if not self._matches(receipt, confirmation_id, command_id, idempotency_key, confirmation_binding, context_version):
                        return self._reject(request, "CONFIRMATION_MISMATCH")
                    stored_request = self._request_from_receipt(receipt)
                    if self._fingerprint(stored_request) != previous[0]:
                        return self._reject(request, "CONFIRMATION_MISMATCH")
                    return receipt
                if pending is None:
                    return self._reject(request, "CONFIRMATION_NOT_FOUND")
                if not self._matches(pending.receipt, confirmation_id, command_id, idempotency_key, confirmation_binding, context_version):
                    return self._reject(request, "CONFIRMATION_MISMATCH")
                if vendor and not self._enable_vendor_requests:
                    self._pending.pop(confirmation_id)
                    return self._reject(request, 'VENDOR_REQUESTS_DISABLED')
                if vendor and request.method not in self._allowed_vendor_methods:
                    self._pending.pop(confirmation_id)
                    return self._reject(request, 'VENDOR_METHOD_NOT_ALLOWED')
                if self._clock() >= pending.deadline:
                    self._pending.pop(confirmation_id)
                    return self._reject(request, "CONFIRMATION_EXPIRED")
                if vendor:
                    snapshot = self._load_snapshot()
                    self._validate_vendor_snapshot(snapshot)
                    command = self._compile_vendor_request(request, snapshot, pending.receipt.trace_id, pending.deadline)
                else:
                    snapshot = self._snapshot(FlightControlAction(request.action).value)
                    command = self._compile(request, snapshot)
                if self._snapshot_hash(snapshot) != pending.snapshot_hash or command != pending.command:
                    self._pending.pop(confirmation_id)
                    return self._reject(request, "AUTHORITATIVE_STATE_CHANGED")
                fingerprint, scope = self._fingerprint(request), self._scope(idempotency_key)
                fields = dict(snapshot=snapshot, confirmation_id=confirmation_id,
                              confirmation_binding=confirmation_binding, trace_id=pending.receipt.trace_id)
                if vendor:
                    fields['expires_at'] = pending.receipt.expires_at
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
                if self._track_commands and not vendor:
                    command = KtdCommand(command.path, {**command.body, 'tracking': {
                        'schemaVersion': 'lingxi-command-request/v1', 'commandId': request.command_id,
                        'idempotencyKey': idempotency_key, 'traceId': pending.receipt.trace_id,
                        'snapshotVersion': snapshot.version,
                        'expiresAt': min(pending.deadline, snapshot.observed_at + self._max_age),
                        'hangarId': self.binding.hangar_id}})
                response = (self._transport.submit_vendor_request(command, context) if vendor
                            else self._transport.submit(command, context))
                if isinstance(response, KtdHttpResponse) and isinstance(response.payload, Mapping):
                    code = response.payload.get("code")
                    if response.status_code == 200 and type(code) is int and code == 200:
                        status = 'submitted'
                        report = None
                        if vendor:
                            from .vendor_control import decode_vendor_receipt
                            report = decode_vendor_receipt(response, binding=self.binding,
                                site_id=self.site_id, task_id=self.task_id, request=request,
                                trace_id=pending.receipt.trace_id, now=self._clock())
                            if report['status'] in {'dispatching', 'unknown', 'conflict', 'expired'}:
                                status = 'unknown'
                        elif self._track_commands:
                            from .command_receipts import decode_command_receipt
                            report = decode_command_receipt(response, binding=self.binding,
                                site_id=self.site_id, task_id=self.task_id, request=request,
                                trace_id=pending.receipt.trace_id, now=self._clock())
                            if report['status'] in {'dispatching', 'unknown', 'conflict', 'expired'}:
                                status = 'unknown'
                        action_value = (request.action.value
                                        if isinstance(request.action, FlightControlAction)
                                        else str(request.action))
                        message = ("平台已受理，设备动作是否完成尚未验证" if status == 'submitted'
                                   else '平台结果未知，请查询回执，勿重复提交')
                        error_code = None
                        if not vendor and status == 'submitted':
                            if action_value in _TURN_TYPES:
                                outcome, detail = self._run_turn(
                                    request, snapshot, trace_id=pending.receipt.trace_id)
                                if outcome == "OK":
                                    status, message = ("accepted",
                                                       f"转向完成：{detail}；设备效果以姿态回读为准")
                                elif outcome in {"TURN_TOLERANCE_NOT_REACHED", "TURN_TIMEOUT",
                                                 "TURN_DIVERGING", "TURN_NOT_RESPONDING"}:
                                    status, error_code = "rejected", outcome
                                    message = f"转向停止：{detail}"
                                else:
                                    status, error_code = "unknown", outcome
                                    message = "转向结果未知，禁止自动重试"
                            elif action_value in _RELATIVE_GIMBAL:
                                if not self._finish_gimbal(
                                        request, trace_id=pending.receipt.trace_id):
                                    status, error_code = "unknown", "GIMBAL_STOP_FAILED"
                                    message = "云台停止指令未确认，禁止重复提交"
                                else:
                                    message = "云台相对步进已下发并发送停止；设备效果未验证"
                        report_fields = ({'vendor_report': report} if vendor
                                         else {'device_report': report})
                        if status == "accepted":
                            result = self._receipt(request, FlightControlStatus.ACCEPTED,
                                                   message=message, **report_fields, **fields)
                        elif status == "rejected":
                            result = self._receipt(request, FlightControlStatus.REJECTED,
                                                   error_code=error_code, message=message,
                                                   **report_fields, **fields)
                        elif status == "unknown":
                            result = self._receipt(request, "unknown", error_code=error_code,
                                                   message=message, **report_fields, **fields)
                        else:
                            result = self._receipt(request, status, message=message,
                                                   **report_fields, **fields)
                    elif (200 <= response.status_code < 500 and not 300 <= response.status_code < 400
                          and type(code) is int and 400 <= code < 500):
                        result = self._receipt(request, FlightControlStatus.REJECTED, error_code="PLATFORM_REJECTED",
                                               message="平台拒绝请求，未确认设备效果", **fields)
            except Exception:
                pass  # 已持久记录 unknown；不读取异常文本，不重试。
            try:
                self._journal.finish(scope, fingerprint, result.to_dict(), now=self._clock())
            except Exception:
                return unknown
            return result

    def query_receipt(self, command_id: str, *, idempotency_key: str, context_version: int) -> dict:
        from .command_receipts import decode_command_receipt
        if (not _identifier(command_id) or not _identifier(idempotency_key)
                or type(context_version) is not int or context_version != self.binding.context_version):
            raise KtdPlatformError('INVALID_RECEIPT_REQUEST')
        with self._lock:
            try:
                scope = self._scope(idempotency_key)
                entry = self._journal.read(scope)
                if entry is None:
                    raise KtdPlatformError('COMMAND_NOT_FOUND')
                receipt = KtdReceipt.from_dict(entry[1])
                if receipt.kind is not None:
                    raise KtdPlatformError('COMMAND_RECEIPT_BINDING_MISMATCH')
                if receipt.command_id != command_id or receipt.context_version != context_version:
                    raise KtdPlatformError('COMMAND_RECEIPT_BINDING_MISMATCH')
                if receipt.receipt_tracking is not True or not callable(getattr(self._transport, 'read_receipt', None)):
                    raise KtdPlatformError('RECEIPT_UNSUPPORTED')
                if receipt.status not in {'submitted', 'unknown'} or not isinstance(receipt.parameters, Mapping):
                    raise KtdPlatformError('COMMAND_NOT_DISPATCHED')
                request = FlightControlActionRequest(receipt.command_id, receipt.idempotency_key,
                    receipt.action, dict(receipt.parameters), receipt.context_version)
                request.validate()
                if self._fingerprint(request) != entry[0]:
                    raise KtdPlatformError('COMMAND_RECEIPT_BINDING_MISMATCH')
                response = self._transport.read_receipt(self.binding, site_id=self.site_id,
                    task_id=self.task_id, command_id=command_id)
                report = decode_command_receipt(response, binding=self.binding,
                    site_id=self.site_id, task_id=self.task_id, request=request,
                    trace_id=receipt.trace_id, now=self._clock())
                report = self._journal.record_report(scope, entry[0], report, self._clock())
                return {**report, 'command_id': command_id, 'context_version': context_version}
            except KtdPlatformError:
                raise
            except Exception:
                raise KtdPlatformError('COMMAND_RECEIPT_UNAVAILABLE') from None

    def query_vendor_receipt(self, command_id: str, *, idempotency_key: str, context_version: int) -> dict:
        from .vendor_control import VENDOR_KIND, decode_vendor_receipt
        if (not _identifier(command_id) or not _identifier(idempotency_key)
                or type(context_version) is not int or context_version != self.binding.context_version):
            raise KtdPlatformError('INVALID_VENDOR_RECEIPT_REQUEST')
        with self._lock:
            try:
                scope = self._scope(idempotency_key)
                entry = self._journal.read(scope)
                if entry is None:
                    raise KtdPlatformError('COMMAND_NOT_FOUND')
                receipt = KtdReceipt.from_dict(entry[1])
                if (receipt.kind != VENDOR_KIND or receipt.command_id != command_id
                        or receipt.context_version != context_version or receipt.idempotency_key != idempotency_key):
                    raise KtdPlatformError('VENDOR_RECEIPT_BINDING_MISMATCH')
                if receipt.receipt_tracking is not True or not callable(getattr(self._transport, 'read_vendor_receipt', None)):
                    raise KtdPlatformError('RECEIPT_UNSUPPORTED')
                if receipt.status not in {'submitted', 'unknown'}:
                    raise KtdPlatformError('COMMAND_NOT_DISPATCHED')
                request = self._request_from_receipt(receipt)
                if self._fingerprint(request) != entry[0]:
                    raise KtdPlatformError('VENDOR_RECEIPT_BINDING_MISMATCH')
                response = self._transport.read_vendor_receipt(self.binding, site_id=self.site_id,
                    task_id=self.task_id, command_id=command_id, method=request.method)
                report = decode_vendor_receipt(response, binding=self.binding, site_id=self.site_id,
                    task_id=self.task_id, request=request, trace_id=receipt.trace_id, now=self._clock())
                report = self._journal.record_vendor_report(scope, entry[0], report, self._clock())
                return {**report, 'context_version': context_version}
            except KtdPlatformError:
                raise
            except Exception:
                raise KtdPlatformError('VENDOR_RECEIPT_UNAVAILABLE') from None

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
