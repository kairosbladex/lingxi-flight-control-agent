"""默认关闭的 ktd_backend 适配候选；HTTP 受理不代表设备动作完成。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import copy
from dataclasses import asdict, dataclass, field, replace
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
from .authority_contract import AuthorityContractError, audit_snapshot, require_authority
from .java_tools import TOOLS as JAVA_TOOLS, JavaActionRequest, encode as encode_java_tool
from .flight_policy import (ADJUSTABLE, CONTROL_MESSAGE, MAPPING_MESSAGE, POLICY_VERSION,
                            PolicyError, check_flight_policy, check_path,
                            mapping_interruption, normalize_parameters,
                            platform_path_provider_rejection)
from .mapping_interruption import (
    MAPPING_INTERRUPTION_FAILED, MAPPING_INTERRUPTION_NOT_CONFIRMED,
    MAPPING_INTERRUPTION_PARTIAL, MAPPING_INTERRUPTION_UNAVAILABLE,
    MAPPING_INTERRUPTION_UNKNOWN, RESERVATION_FAILED, RESERVATION_PENDING,
    RESERVATION_SCHEMA, RESERVATION_SUCCEEDED, RESERVATION_UNKNOWN,
    MappingInterruptionReceipt, MappingInterruptionRequest,
    UnavailableMappingInterruptionGateway,
)
from .ai_lifecycle import HttpAlgorithmLifecycleGateway, HttpAnnotationAlgorithmGateway


#: 进程内锁只在同一执行器实例内串行化确认卡字典，**不**承担跨进程去重。
#: 跨进程「最多调用一次测绘网关」由 SQLite command journal 的 reservation 事务
#: 唯一键保证（见 ``SqliteCommandJournal.reserve_mapping`` / ``advance_mapping``）。


_MOVE_TYPES = {"move_forward": 202, "move_backward": 203, "move_left": 204,
               "move_right": 205, "move_up": 201, "move_down": -201}
DJI_FLYTO_MESSAGES = {
    "RELATIVE_HEIGHT_UNAVAILABLE": "缺少当前相对高度，无法判断大疆飞向目标点时会不会先爬升，本次未下发。",
    "DJI_FLYTO_IMPLICIT_CLIMB": "当前相对高度低于20米。大疆飞向目标点会先爬升到20米，该爬升段不在已检查路径内，本次未下发。",
    "DJI_LOW_TARGET_UNVERIFIED": "目标相对高度低于20米，该高度的飞行行为尚未核验，本次未下发。",
    "DJI_TARGET_ALTITUDE_OUT_OF_RANGE": "目标绝对高度超出大疆飞向目标点允许范围，本次未下发。",
    "FLYTO_TRAJECTORY_UNVERIFIED": "大疆飞向目标点只下发一个终点。已检查直线和实际轨迹对不上，同时改变水平位置和高度时不能证明沿该直线飞行，本次未下发。",
}
_TURN_TYPES = {"turn_left": -206, "turn_right": 206}
#: gimbal action -> (type, controlMode, param, stepPercentage)
#: 原Java把stepPercentage换算为deg/s，不是角位移；仅保留旧手动步进兼容。
#: 指定精确角度的请求不可使用这些默认值。
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
#: 指点飞行/环绕走 /screen/uav/fly/control：119 进入指点、120 退出；121 进入环绕、122 退出。
#: 这两个接口在 Java 侧由 UavFlyDTO 承载，经纬度/高度字段名与 operation/control 不同。
_FLY_TYPES = {"point_flight": 119, "point_orbit": 121}
#: 悬停在各运动模式下的落点。平台没有统一的“悬停”操作码，只能按当前运动方式取消它。
_HOVER_STOP_TYPES = {"displacement": 120, "orbit": 122}
#: Java 自己的位移实现用固定 5 m/s 调 fly_to_point（DJHangarOperation 的 mode=2 分支），
#: 这里沿用同一档，不新增速度猜测。
_FLY_SPEED_MPS = 5
#: 平台高度字段是 WGS84 椭球高，官方范围 2–10000 米。
_MIN_ELLIPSOID_HEIGHT_M, _MAX_ELLIPSOID_HEIGHT_M = 2.0, 10000.0
_CAMERA_TYPES = {"camera_photo": 100, "camera_start_recording": 101, "camera_stop_recording": 102}
_LENS_MODES = {"camera_electro_optical": 6, "camera_infrared": 2, "camera_wide": 5}
_EXTRA_CAMERA = frozenset(_LENS_MODES) | {"camera_zoom", "camera_panorama", "infrared_set_limits"}
_PAYLOAD_TYPES = {'speaker_speak': ('/screen/speaker/control', 201),
                  'speaker_volume': ('/screen/speaker/control', 109),
                  'searchlight_brightness': ('/screen/lighting/control', 103)}
SUPPORTED_ACTIONS = (frozenset(_MOVE_TYPES) | frozenset(_TURN_TYPES)
                     | frozenset(_GIMBAL_TYPES) | frozenset(_ROUTE_TYPES)
                     | frozenset(_CAMERA_TYPES) | _EXTRA_CAMERA | frozenset(JAVA_TOOLS) | frozenset(_PAYLOAD_TYPES) | frozenset(_FLY_TYPES) | {"read_state", 'task_end_current', 'task_end_all', 'hover'})
_PATHS = frozenset({"/screen/uav/operation/control", "/screen/uav/route/control",
                    "/screen/uav/camera/control", "/screen/uav/gimbal/control", "/screen/uav/fly/control",
                    '/screen/speaker/control', '/screen/lighting/control'})
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


_PATH_POINT_FIELDS = frozenset({'longitude', 'latitude', 'altitude', 'source'})


def _path_point(point: Any) -> bool:
    """围栏折线点：字段集合固定，坐标与高度必须是有限且在范围内的数字。"""
    return (isinstance(point, dict) and set(point) == _PATH_POINT_FIELDS
            and _finite(point['longitude'], -180, 180) and _finite(point['latitude'], -90, 90)
            and _finite(point['altitude'], -500, 10000) and point['source'] in ('old', 'new'))


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
    """仅携带稳定错误码。detail 是本仓写给操作者的说明，不是上游原文。"""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.detail = detail


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
    #: Agent 会话批次。平台可选回显；缺失时由绑定提供，绝不冒充 Java taskId。
    task_batch_id: str | None = None
    #: V1.4：Java 在快照顶层 ``contract.unavailable`` 显式声明的能力缺口。
    #: 空映射表示平台没有声明缺口；None 表示该快照没有这份契约（旧版本）。
    platform_unavailable: Mapping[str, str] | None = None


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
    adjustments: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: 测绘中断原子操作的回执证据；只在该动作触发测绘中断时出现。
    #: 失败时同样携带，便于审计「中断未完成」而不是只看到一句通用拒绝。
    mapping_interruption: Mapping[str, Any] | None = None
    #: 持久化 reservation 的审计摘要（状态、幂等键、指纹、尝试次数等），
    #: 用于证明「网关只被调用一次」以及 unknown 状态没有被自动重试。
    mapping_reservation: Mapping[str, Any] | None = None
    policy_version: str = POLICY_VERSION

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["status"] = self.status.value if isinstance(self.status, FlightControlStatus) else self.status
        if self.kind is None:
            result.pop('kind')
        if self.vendor_report is None:
            result.pop('vendor_report')
        if self.mapping_interruption is None:
            result.pop('mapping_interruption')
        if self.mapping_reservation is None:
            result.pop('mapping_reservation')
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
    """单次提交前持久记录 unknown；事务唯一键覆盖并发进程与进程重启。

    两张表，两种用途：

    - ``commands``：平台命令的「先记 unknown 再 submit」账本，主键是幂等作用域；
    - ``mapping_reservations``：测绘中断网关的**持久化 reservation**，主键同样是
      幂等作用域，``fingerprint`` 用来区分「同一幂等键的不同命令」。

    两张表的写入都用 ``BEGIN IMMEDIATE`` + 唯一主键：SQLite 的写事务在文件级
    串行化，因此「谁先插入谁调用网关」在跨进程下也是确定的；不依赖任何进程内锁。
    """
    durable = True

    def __init__(self, path: str | Path):
        self.path = Path(path).absolute()
        if str(path) == ":memory:" or self.path.is_symlink() or not self.path.parent.is_dir():
            raise ValueError("journal requires an existing private directory and a regular file path")
        connection = self._connection()
        try:
            with connection:
                connection.execute("CREATE TABLE IF NOT EXISTS commands (scope TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, receipt TEXT NOT NULL)")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS mapping_reservations ("
                    "scope TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, state TEXT NOT NULL, record TEXT NOT NULL)")
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


    # ------------------------------------------------------------------
    # 测绘中断 reservation：先于外部网关调用落库，跨进程唯一。
    # ------------------------------------------------------------------

    def read_mapping(self, scope: str) -> tuple[str, dict[str, Any]] | None:
        connection = self._connection()
        try:
            row = connection.execute(
                "SELECT fingerprint, record FROM mapping_reservations WHERE scope=?", (scope,)).fetchone()
            return (row[0], json.loads(row[1])) if row else None
        finally:
            connection.close()

    def reserve_mapping(self, scope: str, fingerprint: str,
                        record: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        """原子取得 reservation。

        返回 ``(verdict, stored)``：

        - ``acquired``：本次调用方是新 reservation 的唯一持有者，可以继续调用网关；
        - ``existing``：同一幂等作用域已有 reservation（同指纹），调用方不得再调网关；
        - ``conflict``：同一幂等作用域但指纹不同，调用方必须返回 IDEMPOTENCY_CONFLICT。

        跨进程互斥来自 ``BEGIN IMMEDIATE`` 写事务 + ``scope`` 主键，不依赖进程内锁。
        """
        payload = dict(record)
        payload['state'] = RESERVATION_PENDING
        connection = self._connection()
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT fingerprint, record FROM mapping_reservations WHERE scope=?", (scope,)).fetchone()
                if row is not None:
                    stored = json.loads(row[1])
                    return ('existing' if row[0] == fingerprint else 'conflict', stored)
                connection.execute(
                    "INSERT INTO mapping_reservations (scope, fingerprint, state, record) VALUES (?, ?, ?, ?)",
                    (scope, fingerprint, payload['state'], _json(payload)))
                return ('acquired', payload)
        finally:
            connection.close()

    def advance_mapping(self, scope: str, fingerprint: str, *, from_states,
                        to_state: str, record: Mapping[str, Any]) -> bool:
        """状态机 CAS：只有当前状态在 ``from_states`` 内且指纹一致才写入。

        返回 ``True`` 表示本次是唯一一次成功迁移；``False`` 表示别的进程已经
        推进过（或指纹不符），调用方必须按既有状态处理，不得继续外部调用。
        """
        if to_state not in {RESERVATION_PENDING, RESERVATION_UNKNOWN,
                            RESERVATION_SUCCEEDED, RESERVATION_FAILED}:
            raise ValueError("unknown reservation state")
        payload = dict(record)
        payload['state'] = to_state
        states = tuple(from_states)
        if not states:
            raise ValueError("from_states must not be empty")
        placeholders = ",".join("?" for _ in states)
        connection = self._connection()
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "UPDATE mapping_reservations SET state=?, record=? "
                    f"WHERE scope=? AND fingerprint=? AND state IN ({placeholders})",
                    (to_state, _json(payload), scope, fingerprint, *states))
                return cursor.rowcount == 1
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
                or parsed.path.rstrip("/") not in ("", "/api", "/gis_server_a")):
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
        task_end = re.fullmatch(r'/portal/task/overTask/([A-Za-z0-9_.:-]{1,128})', command.path)
        if (command.path not in _PATHS and not task_end) or context.actor_id != self.actor_id:
            raise KtdPlatformError("TRANSPORT_SCOPE_MISMATCH")
        if task_end and command.body:
            raise KtdPlatformError('INVALID_REQUEST_CONTEXT')
        if not all(_identifier(value) for value in (context.project_id, context.trace_id, context.idempotency_key)):
            raise KtdPlatformError("INVALID_REQUEST_CONTEXT")
        headers = {"Authorization": "Bearer " + self._token, "Content-Type": "application/json",
                   "projectId": context.project_id, "X-Trace-Id": context.trace_id,
                   "X-Idempotency-Key": context.idempotency_key}
        if command.path == "/screen/uav/operation/control":
            headers["timestamp"] = str(self._next_timestamp())
        request = Request(self._base_url + command.path, data=_json(command.body).encode(), method="POST",
                          headers=headers)
        from .audit import emit, digest, numbers
        fields = dict(trace_id=context.trace_id, request_ref=digest(context.idempotency_key),
            task_ref=digest(command.body.get('taskId')), drone_ref=digest(command.body.get('terminalId')),
            endpoint=command.path, parameters=numbers(dict(command.body)))
        emit('flight_java', phase='attempt', **fields)
        try:
            response = self._read_response(request, strict_json='tracking' in command.body)
        except Exception:
            emit('flight_java', phase='outcome_unknown', **fields)
            raise
        code = response.payload.get('code') if isinstance(response.payload, Mapping) else None
        emit('flight_java', phase='response', http_status=response.status_code,
             java_code=code if type(code) is int else None, device_effect='not_verified', **fields)
        return response

    def task_end_check(self, binding: RuntimeBinding, task_id: str) -> KtdHttpResponse:
        if binding.actor_id != self.actor_id or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', task_id):
            raise KtdPlatformError('TRANSPORT_SCOPE_MISMATCH')
        request = Request(self._base_url + '/portal/task/overTask/safetyCheck',
            data=_json({'taskId': task_id}).encode(), method='POST',
            headers={'Authorization': 'Bearer ' + self._token, 'projectId': binding.project_id,
                     'Content-Type': 'application/json'})
        return self._read_response(request, strict_json=True)

    def check_path(self, binding: RuntimeBinding, points: list, *, timeout_sec: float | None = None) -> KtdHttpResponse:
        """一次有界围栏请求；折线长度、点结构和单次 deadline 都在这里收敛。

        多点折线用于环绕闭合曲线与起始段。平台是否真的支持多段由响应里的
        ``segment`` 数量校验兜底，对不上就按 malformed 降级，不猜。
        """
        if (binding.actor_id != self.actor_id or not _identifier(binding.project_id)
                or not isinstance(points, list) or not 2 <= len(points) <= 129
                or not all(_path_point(point) for point in points)):
            raise KtdPlatformError('TRANSPORT_SCOPE_MISMATCH')
        timeout = self._timeout
        if timeout_sec is not None:
            if not _finite(timeout_sec, 0.1, 30):
                raise KtdPlatformError('INVALID_REQUEST_CONTEXT')
            timeout = min(timeout, timeout_sec)
        request = Request(self._base_url + '/electronicFence/path-route/with-clean',
            data=_json({'points': points}).encode(), method='POST',
            headers={'Authorization': 'Bearer ' + self._token, 'projectId': binding.project_id,
                     'Content-Type': 'application/json'})
        from .audit import emit, digest
        fields = dict(endpoint='/electronicFence/path-route/with-clean',
                      drone_ref=digest(binding.drone_id), segments=len(points) - 1,
                      timeout_ms=int(timeout * 1000))
        emit('flight_fence', phase='attempt', **fields)
        started = time.monotonic()
        try:
            response = self._read_response(request, strict_json=True, timeout=timeout)
        except Exception:
            emit('flight_fence', phase='failed', elapsed_ms=int((time.monotonic() - started) * 1000), **fields)
            raise
        emit('flight_fence', phase='response', http_status=response.status_code,
             elapsed_ms=int((time.monotonic() - started) * 1000), **fields)
        return response

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

    def read_annotation_algorithms(self, binding: RuntimeBinding, *, site_id: str,
                                   task_id: str | None, annotation_id: str) -> KtdHttpResponse:
        """读取一个静态标注的 AI 绑定；路径和资源范围固定在平台适配器内。"""
        if (not isinstance(binding, RuntimeBinding) or binding.actor_id != self.actor_id
                or not all(_identifier(value) for value in
                           (binding.project_id, binding.hangar_id, binding.drone_id,
                            site_id, annotation_id))
                or (task_id is not None and not _identifier(task_id))):
            raise KtdPlatformError('INVALID_REQUEST_CONTEXT')
        query = {'projectId': binding.project_id, 'siteId': site_id,
                 'hangarId': binding.hangar_id, 'droneId': binding.drone_id,
                 'annotationId': annotation_id}
        if task_id is not None:
            query['taskId'] = task_id
        request = Request(self._base_url + '/lingxi/agent/annotations/ai?' + urlencode(query),
                          method='GET', headers={'Authorization': 'Bearer ' + self._token,
                                                 'projectId': binding.project_id})
        try:
            return self._read_response(request, strict_json=True)
        except Exception:
            raise KtdPlatformError('ANNOTATION_ALGORITHM_TRANSPORT_ERROR') from None

    def set_annotation_algorithm_state(self, binding: RuntimeBinding, *, site_id: str,
                                       task_id: str | None, annotation_id: str,
                                       algorithms: tuple[str, ...], operation: str,
                                       request_id: str) -> KtdHttpResponse:
        """按算法粒度请求进入/离开标注区域的启停回执。"""
        if (not isinstance(binding, RuntimeBinding) or binding.actor_id != self.actor_id
                or not all(_identifier(value) for value in
                           (binding.project_id, binding.hangar_id, binding.drone_id,
                            site_id, annotation_id, request_id))
                or (task_id is not None and not _identifier(task_id))
                or operation not in {'start', 'stop'}
                or not isinstance(algorithms, tuple) or not algorithms
                or any(not _identifier(item) for item in algorithms)):
            raise KtdPlatformError('INVALID_REQUEST_CONTEXT')
        body = {'schema_version': 'lingxi-ai-lifecycle/v1', 'operation': operation,
                'request_id': request_id, 'project_id': binding.project_id,
                'site_id': site_id, 'hangar_id': binding.hangar_id,
                'drone_id': binding.drone_id, 'annotation_id': annotation_id,
                'algorithms': list(algorithms)}
        if task_id is not None:
            body['task_id'] = task_id
        request = Request(self._base_url + '/lingxi/agent/annotations/ai/lifecycle',
                          data=_json(body).encode(), method='POST',
                          headers={'Authorization': 'Bearer ' + self._token,
                                   'Content-Type': 'application/json',
                                   'projectId': binding.project_id,
                                   'X-Idempotency-Key': request_id})
        try:
            return self._read_response(request, strict_json=True)
        except Exception:
            raise KtdPlatformError('ALGORITHM_LIFECYCLE_TRANSPORT_ERROR') from None

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
            "task": "/inspectionResult/task/terminal/detail",
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

    def _read_response(self, request: Request, *, strict_json: bool = False,
                       timeout: float | None = None) -> KtdHttpResponse:
        # timeout 由调用方按单次预算传入；socket 超时是这条链路唯一的有界手段，
        # 不为此新建线程或进程。DNS 解析不受 socket 超时约束，属于已知残余风险。
        effective = self._timeout if timeout is None else min(self._timeout, timeout)
        try:
            response = self._opener.open(request, timeout=effective)
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
                 stick_max_sec: float = 5.0, path_checker=None,
                 mapping_gateway=None, mapping_reservation_lease_sec: float = 30.0,
                 annotation_gateway=None, algorithm_gateway=None):
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
                or not _finite(mapping_reservation_lease_sec, 0.1, 3600)
                or not callable(sleep)
                or (pose_reader is not None and not callable(pose_reader))):
            raise ValueError("invalid attitude control budget")
        if getattr(journal, "durable", None) is not True:
            raise ValueError("a durable command journal is required")
        if isinstance(transport, KtdHttpTransport) and transport.actor_id != binding.actor_id:
            raise ValueError("transport login identity must match runtime actor")
        # 围栏 provider 装配期校验：只声明 deadline_sec 的普通同步 callable 会被拒绝，
        # 避免把协作式承诺当成平台执行路径上的硬超时。
        rejection = platform_path_provider_rejection(path_checker)
        if rejection is not None:
            raise ValueError(rejection)
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
        self._java_tools_enabled = getattr(snapshot_provider, 'java_tools_enabled', False) is True
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
        #: 最近一次成功下发的运动方式：displacement（位移/指点）| stick（杆量/转向）| orbit（环绕）。
        #: 平台没有统一悬停操作码，悬停只能按这个方式选择对应的“取消运动”指令；
        #: 进程重启后为 None，此时只发 207（对没有运动指令的无人机是安全空操作）。
        self._last_motion: str | None = None
        self._path_checker = path_checker
        #: V1.4 4.1.2：测绘中断原子操作的平台边界。默认显式不可用——
        #: 没有可核验的 Java 原子接口时不伪造成功，也不下发后续飞控动作。
        self._mapping_gateway = mapping_gateway or UnavailableMappingInterruptionGateway()
        #: 平台是否真的按幂等键收敛；只有网关自己声明才算，缺省一律按「不支持」。
        self._mapping_gateway_idempotent = (
            getattr(self._mapping_gateway, 'supports_idempotency_key', False) is True)
        self._mapping_lease = float(mapping_reservation_lease_sec)
        self._annotation_gateway = annotation_gateway
        if annotation_gateway is None and callable(getattr(transport, 'read_annotation_algorithms', None)):
            annotation_gateway = HttpAnnotationAlgorithmGateway(
                transport, site_id=site_id, task_id=task_id)
        self._annotation_gateway = annotation_gateway
        self._algorithm_gateway = algorithm_gateway
        if algorithm_gateway is None and callable(getattr(transport, 'set_annotation_algorithm_state', None)):
            self._algorithm_gateway = HttpAlgorithmLifecycleGateway(
                transport, site_id=site_id, task_id=task_id)
        self._last_interruption: dict[str, Any] | None = None
        self._last_reservation: dict[str, Any] | None = None
        self._lock = threading.RLock()

    @property
    def vendor_request_methods(self) -> tuple[str, ...]:
        return self._allowed_vendor_methods if self._enable_vendor_requests else ()

    @property
    def annotation_gateway(self):
        """真实静态标注配置适配器；不可用时为 ``None``，调用方必须降级。"""
        return self._annotation_gateway

    @property
    def algorithm_gateway(self):
        """算法粒度生命周期适配器；平台未声明接口时为 ``None``。"""
        return self._algorithm_gateway

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
        if snapshot.task_batch_id is not None and snapshot.task_batch_id != self.binding.task_batch_id:
            # 平台可选回显的 Agent 批次与当前会话不一致时，不能把这份快照用于本会话。
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
        # V1.4：平台显式声明“这个能力我没有”时，先返回该能力自己的错误码，
        # 而不是让调用方从 null 字段猜原因。
        from .handoff_contract import unavailable_code_for
        declared = unavailable_code_for(action, snapshot.platform_unavailable or {})
        if declared:
            raise KtdPlatformError(declared)
        # V1.4：平台把已声明字段留空（null）时不能当成安全或可执行。
        try:
            require_authority(snapshot, action)
        except AuthorityContractError as error:
            raise KtdPlatformError(error.code) from None
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

    @property
    def java_tool_definitions(self):
        if not self._java_tools_enabled or self._track_commands:
            return ()
        return tuple(spec.definition(name) for name,spec in JAVA_TOOLS.items())

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
                    if action in JAVA_TOOLS:
                        parameters = dict(JAVA_TOOLS[action].arguments)
                    elif action in _MOVE_TYPES:
                        parameters = {"distance_m": 0.01}
                    elif action in _TURN_TYPES:
                        parameters = {"angle_deg": 1}
                    elif action == 'camera_zoom':
                        parameters = {'value': 1}
                    elif action == 'infrared_set_limits':
                        parameters = {'min_c': 0, 'max_c': 50}
                    elif action in _PAYLOAD_TYPES:
                        parameters = {'text': '能力校验'} if action == 'speaker_speak' else {'value': 50}
                    else:
                        parameters = {}
                    request_type = JavaActionRequest if action in JAVA_TOOLS else FlightControlActionRequest
                    request = request_type("capability-check", "capability-check", action,
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
            result.update({key: copy.deepcopy(snapshot.state[key]) for key in (
                'task_type', 'mapping_invalidated', 'auto_stitching', 'low_battery_return',
                'device_limits', 'gimbal_pitch_deg', 'breakpoint_index', 'temporary_task_id',
                'route_id') if key in snapshot.state})
            # legacy 快照的租约是合成值，不向界面/回执伪装成权威租约。
            lease = (snapshot.control_lease_expires_at
                     if snapshot.authorization_quality == "authoritative" else None)
            result.update(snapshot_version=snapshot.version, observed_at=snapshot.observed_at,
                          control_owner=snapshot.control_owner, control_lease_expires_at=lease,
                          authorization_quality=snapshot.authorization_quality,
                          task_batch_id=snapshot.task_batch_id)
            if snapshot.action_availability is not None:
                result['action_availability'] = copy.deepcopy(dict(snapshot.action_availability))
        return result

    def authority_contract(self, action: str | None = None) -> dict[str, Any]:
        """字段级权威契约审计；只读，不授予权限，不代替 preview。

        返回 ``fields``（present/unknown/absent/unbound）、``incomplete`` 以及
        当前动作是否被 ``AUTHORITATIVE_STATE_INCOMPLETE`` 阻断。
        """
        try:
            snapshot = self._load_snapshot()
        except KtdPlatformError as error:
            return {"contract_version": "lingxi-authority-contract/v1", "available": False,
                    "error_code": str(error), "fields": {}, "incomplete": []}
        audit = audit_snapshot(snapshot)
        report = {"available": True, **audit.as_dict()}
        if isinstance(action, str) and action:
            try:
                require_authority(snapshot, action)
                report["blocked"] = False
            except AuthorityContractError as error:
                report["blocked"] = True
                report["error_code"] = error.code
                report["unknown_fields"] = list(error.fields)
        return report

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

    def _reject(self, request, code, **fields):
        detail = fields.pop("detail", None)
        message = ("已理解指定的云台调整角度，但原Java接口发送的是转动速度；"
                   "缺少可靠角度设定及停止闭环，本次未下发，不会替换成默认步进。"
                   if code == 'GIMBAL_EXACT_ANGLE_UNAVAILABLE' else
                   "请求未执行，请核对能力、权限和当前状态")
        if code == 'CONTROL_NOT_OWNED':
            message = CONTROL_MESSAGE
        elif code == 'ROUTE_PAUSE_REQUIRED':
            from .flight_policy import ROUTE_MESSAGE
            message = ROUTE_MESSAGE
        elif code == 'MAPPING_INTERRUPTION_UNAVAILABLE':
            message = MAPPING_MESSAGE + '平台尚未提供关闭拼图及作废断点的原子契约，本次不下发。'
        elif code in {'MAPPING_INTERRUPTION_PARTIAL', 'MAPPING_INTERRUPTION_NOT_CONFIRMED'}:
            message = (MAPPING_MESSAGE + '测绘中断未完成（只执行了部分步骤或未获确认），'
                       '本次不下发飞控动作；已完成的步骤不构成本次动作的授权，'
                       '也不代表测绘成果已经按原子契约作废。')
        elif code == 'MAPPING_INTERRUPTION_UNKNOWN':
            message = (MAPPING_MESSAGE + '测绘中断网关结果无法确认，本地 reservation 已保留为 '
                       'mapping_interruption_unknown；不会自动重试网关或飞控下发，'
                       '需人工核对或凭权威回执显式恢复。')
        elif code == 'ALGORITHM_LIFECYCLE_UNAVAILABLE':
            message = ('平台尚未提供算法粒度的启动/停止接口，任务级AI总开关不能代替算法运行回执，'
                       '本次不下发，也不声称算法已运行。')
        elif code == 'PATH_BLOCKED':
            message = '指令已拦截，平台检测到路径存在障碍物、禁飞区或限高，未执行移动。'
        elif code == 'DEVICE_LIMITS_UNAVAILABLE':
            message = '未取得当前设备参数表，不能猜测设备极值。'
        elif code == 'ORBIT_RADIUS_NOT_COMMANDED':
            message = detail or ('当前水平距离与请求的环绕半径不一致。'
                                 '大疆进入环绕不接收半径，本次未下发。')
        elif code == 'ORBIT_POSE_UNAVAILABLE':
            message = detail or '缺少当前经纬度，无法确认进入环绕时的实际半径，本次未下发。'
        elif code in DJI_FLYTO_MESSAGES:
            message = DJI_FLYTO_MESSAGES[code]
        return self._receipt(request, FlightControlStatus.REJECTED, error_code=code,
                             message=message, **fields)

    # ------------------------------------------------------------------
    # V1.4 4.1.2：持久化 reservation + 至多一次网关调用
    # ------------------------------------------------------------------

    @staticmethod
    def _mapping_evidence(record: Mapping[str, Any]) -> dict[str, Any]:
        """reservation 的审计摘要：只留标识、状态与计数，不含坐标/航线业务数据。"""
        return {key: copy.deepcopy(record.get(key)) for key in (
            'schema', 'state', 'command_id', 'idempotency_key', 'trace_id',
            'context_version', 'snapshot_version', 'task_batch_id', 'fingerprint',
            'attempts', 'created_at', 'lease_expires_at', 'gateway_execution_mode',
            'gateway_idempotency_supported', 'gateway_idempotency_key',
            'error_code', 'resolution', 'authority_ref')}

    def _reservation_record(self, request, snapshot: KtdSnapshot, *, scope: str,
                            fingerprint: str, trace_id: str, action_value: str,
                            attempts: int = 0) -> dict[str, Any]:
        return {
            'schema': RESERVATION_SCHEMA,
            'state': RESERVATION_PENDING,
            'command_id': request.command_id,
            'idempotency_key': request.idempotency_key,
            'trace_id': trace_id,
            'context_version': self.binding.context_version,
            'snapshot_version': snapshot.version,
            'task_batch_id': self.binding.task_batch_id,
            'fingerprint': fingerprint,
            'scope': scope,
            'action': action_value,
            'created_at': self._clock(),
            'lease_expires_at': self._clock() + self._mapping_lease,
            'attempts': attempts,
            'gateway_execution_mode': getattr(self._mapping_gateway, 'execution_mode', 'unavailable'),
            'gateway_idempotency_supported': self._mapping_gateway_idempotent,
            'gateway_idempotency_key': request.idempotency_key,
            'receipt': None,
            'error_code': None,
            'resolution': None,
            'authority_ref': None,
        }

    def _mapping_interruption_reserved(self, request, snapshot: KtdSnapshot, *, scope: str,
                                       fingerprint: str, trace_id: str):
        """**所有确定性预检查通过之后**，用持久化 reservation 收敛测绘原子作废。

        调用方必须已经完成：确认卡/幂等键/上下文版本/确认有效期、最新权威快照、
        命令编译、任务结束安全检查、confirm 阶段围栏复检，以及 snapshot_hash /
        编译结果复比。

        - 返回 ``(record, None)``：本次（或更早的某次）已完成原子作废，可以继续
          写命令账本并 submit；
        - 返回 ``(None, receipt)``：同一幂等键已被另一执行器结算，调用方直接返回
          该回执，不得再次作废、也不得再次下发；
        - 抛 ``KtdPlatformError``：网关不可用/部分完成/未确认（失败），或结果未知
          （``MAPPING_INTERRUPTION_UNKNOWN``）。两者都不会提交任何飞控指令。

        顺序固定：读既有 reservation（有则只按状态处理）→ 原子取 reservation →
        CAS pending→unknown → 调用网关 → CAS unknown→succeeded/failed。
        也就是说「调用网关」永远发生在 unknown 之后、succeeded 之前。
        """
        action_value = (request.action.value if isinstance(request.action, FlightControlAction)
                        else str(request.action))
        entry = self._journal.read_mapping(scope)
        if entry is not None:
            stored_fingerprint, stored = entry
            self._last_reservation = stored
            if stored_fingerprint != fingerprint:
                # 同一幂等作用域里的另一条命令：绝不共用作废结果。
                raise KtdPlatformError('IDEMPOTENCY_CONFLICT')
            state = stored.get('state')
            if state == RESERVATION_SUCCEEDED:
                # 作废已完成（可能是上一个进程做的）：不得重复调用网关，继续走账本。
                self._last_interruption = copy.deepcopy(stored.get('receipt'))
                return copy.deepcopy(stored.get('receipt')), None
            if state == RESERVATION_FAILED:
                self._last_interruption = copy.deepcopy(stored.get('receipt'))
                raise KtdPlatformError(stored.get('error_code') or MAPPING_INTERRUPTION_UNAVAILABLE)
            if state == RESERVATION_UNKNOWN:
                # 结果未知是终态：不重试网关，也不 submit。
                self._last_interruption = copy.deepcopy(stored.get('receipt'))
                raise KtdPlatformError(MAPPING_INTERRUPTION_UNKNOWN)
            if state != RESERVATION_PENDING:
                raise KtdPlatformError(MAPPING_INTERRUPTION_UNKNOWN)
            if not self._mapping_lease_expired(stored):
                # 另一进程正在 pending→unknown→gateway 之间：只等不调。
                raise KtdPlatformError(MAPPING_INTERRUPTION_UNKNOWN)
            # 租约已过期且仍是 pending：按定义「尚未调用网关」，允许恢复方接管一次。
            record = {**stored,
                      'attempts': int(stored.get('attempts') or 0) + 1,
                      'lease_expires_at': self._clock() + self._mapping_lease}
            return self._invoke_mapping_gateway(
                request, snapshot, scope=scope, fingerprint=fingerprint, trace_id=trace_id,
                record=record, action_value=action_value)
        if not mapping_interruption(action_value, snapshot.state):
            return None, None
        record = self._reservation_record(request, snapshot, scope=scope, fingerprint=fingerprint,
                                          trace_id=trace_id, action_value=action_value)
        verdict, stored = self._journal.reserve_mapping(scope, fingerprint, record)
        if verdict == 'conflict':
            self._last_reservation = stored
            raise KtdPlatformError('IDEMPOTENCY_CONFLICT')
        if verdict != 'acquired':
            # 竞态窗口：另一个进程刚刚插入。按既有状态处理，绝不调用网关。
            self._last_reservation = stored
            self._last_interruption = copy.deepcopy(stored.get('receipt'))
            if stored.get('state') == RESERVATION_SUCCEEDED:
                return copy.deepcopy(stored.get('receipt')), None
            if stored.get('state') == RESERVATION_FAILED:
                raise KtdPlatformError(stored.get('error_code') or MAPPING_INTERRUPTION_UNAVAILABLE)
            raise KtdPlatformError(MAPPING_INTERRUPTION_UNKNOWN)
        return self._invoke_mapping_gateway(
            request, snapshot, scope=scope, fingerprint=fingerprint, trace_id=trace_id,
            record=stored, action_value=action_value)

    def _mapping_lease_expired(self, record: Mapping[str, Any]) -> bool:
        lease = record.get('lease_expires_at')
        return _finite(lease, 0, float('inf')) and lease <= self._clock()

    def _invoke_mapping_gateway(self, request, snapshot: KtdSnapshot, *, scope: str,
                                fingerprint: str, trace_id: str, record: Mapping[str, Any],
                                action_value: str):
        # 调用前先把状态推进到 unknown：此后崩溃也不会被误判成「还没调用」。
        invoking = {**record, 'state': RESERVATION_UNKNOWN,
                    'invoked_at': self._clock(), 'lease_expires_at': None}
        if not self._journal.advance_mapping(
                scope, fingerprint, from_states=(RESERVATION_PENDING,),
                to_state=RESERVATION_UNKNOWN, record=invoking):
            # 另一个进程已经接管（或已推进），本次不得调用网关。
            entry = self._journal.read_mapping(scope)
            if entry is None:
                raise KtdPlatformError(MAPPING_INTERRUPTION_UNKNOWN)
            self._last_reservation = entry[1]
            if entry[1].get('state') == RESERVATION_SUCCEEDED:
                self._last_interruption = copy.deepcopy(entry[1].get('receipt'))
                return copy.deepcopy(entry[1].get('receipt')), None
            if entry[1].get('state') == RESERVATION_FAILED:
                self._last_interruption = copy.deepcopy(entry[1].get('receipt'))
                raise KtdPlatformError(entry[1].get('error_code') or MAPPING_INTERRUPTION_UNAVAILABLE)
            raise KtdPlatformError(MAPPING_INTERRUPTION_UNKNOWN)
        self._last_reservation = invoking
        try:
            receipt = self._mapping_gateway.interrupt(MappingInterruptionRequest(
                command_id=request.command_id, action=action_value,
                task_batch_id=self.binding.task_batch_id, confirmed=True,
                route_id=snapshot.state.get('route_id'),
                task_type=snapshot.state.get('task_type'),
                idempotency_key=request.idempotency_key, trace_id=trace_id,
                context_version=self.binding.context_version,
                snapshot_version=snapshot.version, fingerprint=fingerprint))
        except Exception:
            # 网关抛错 = 结果未知。reservation 留在 unknown，禁止自动重试。
            self._last_interruption = None
            raise KtdPlatformError(MAPPING_INTERRUPTION_UNKNOWN) from None
        if not isinstance(receipt, MappingInterruptionReceipt):
            # 无法解释的回执同样按「未知」处理，不猜成成功或不可用。
            self._last_interruption = None
            raise KtdPlatformError(MAPPING_INTERRUPTION_UNKNOWN)
        settled = {**invoking, 'receipt': receipt.to_dict(), 'completed_at': self._clock()}
        self._last_interruption = receipt.to_dict()
        if receipt.effective:
            succeeded = {**settled, 'state': RESERVATION_SUCCEEDED}
            self._last_reservation = succeeded
            # 只有 atomic + confirmed + 无错误码，才允许把 unknown 推进到 succeeded。
            if not self._journal.advance_mapping(
                    scope, fingerprint, from_states=(RESERVATION_UNKNOWN,),
                    to_state=RESERVATION_SUCCEEDED, record=succeeded):
                raise KtdPlatformError(MAPPING_INTERRUPTION_UNKNOWN)
            return dict(self._last_interruption), None
        error_code = (MAPPING_INTERRUPTION_NOT_CONFIRMED
                      if receipt.atomic is True and receipt.confirmed is not True
                      else receipt.error_code or MAPPING_INTERRUPTION_PARTIAL)
        failed = {**settled, 'state': RESERVATION_FAILED, 'error_code': error_code}
        self._last_reservation = failed
        self._journal.advance_mapping(
            scope, fingerprint, from_states=(RESERVATION_UNKNOWN,),
            to_state=RESERVATION_FAILED, record=failed)
        raise KtdPlatformError(error_code)

    def mapping_reservation_status(self, *, command_id: str, idempotency_key: str,
                                   context_version: int) -> dict[str, Any]:
        """只读恢复入口：查该幂等键的 reservation，不调用网关、不下发飞控。"""
        if (not _identifier(command_id) or not _identifier(idempotency_key)
                or type(context_version) is not int):
            raise KtdPlatformError('INVALID_RECEIPT_REQUEST')
        if context_version != self.binding.context_version:
            raise KtdPlatformError('STALE_CONTEXT')
        entry = self._journal.read_mapping(self._scope(idempotency_key))
        if entry is None:
            return {'schema': RESERVATION_SCHEMA, 'found': False, 'state': None}
        record = entry[1]
        if record.get('command_id') != command_id:
            raise KtdPlatformError('COMMAND_RECEIPT_BINDING_MISMATCH')
        return {'found': True, **self._mapping_evidence(record)}

    def resolve_mapping_interruption(self, *, command_id: str, idempotency_key: str,
                                     context_version: int, resolution: str,
                                     authority_ref: str) -> dict[str, Any]:
        """显式人工/权威回执恢复：只改 reservation 状态，不调用网关、不下发飞控。

        ``resolution='voided'``（人工或权威回执确认测绘已作废）把 unknown 推进到
        succeeded；``resolution='not_voided'`` 推进到 failed。两者都必须带可追溯的
        ``authority_ref``（工单号或权威回执 id）。恢复本身不重试网关：需要继续下发
        时由调用方重新 preview → confirm，由账本保证 submit 至多一次。
        """
        if resolution not in {'voided', 'not_voided'} or not _identifier(authority_ref):
            raise KtdPlatformError('INVALID_MAPPING_RESOLUTION')
        if (not _identifier(command_id) or not _identifier(idempotency_key)
                or type(context_version) is not int):
            raise KtdPlatformError('INVALID_RECEIPT_REQUEST')
        if context_version != self.binding.context_version:
            raise KtdPlatformError('STALE_CONTEXT')
        scope = self._scope(idempotency_key)
        entry = self._journal.read_mapping(scope)
        if entry is None:
            raise KtdPlatformError('MAPPING_RESERVATION_NOT_FOUND')
        fingerprint, stored = entry
        if stored.get('command_id') != command_id:
            raise KtdPlatformError('COMMAND_RECEIPT_BINDING_MISMATCH')
        if stored.get('state') != RESERVATION_UNKNOWN:
            raise KtdPlatformError('MAPPING_RESERVATION_NOT_UNKNOWN')
        resolved = {**stored, 'resolution': resolution, 'authority_ref': authority_ref,
                    'resolved_at': self._clock(),
                    'error_code': None if resolution == 'voided' else MAPPING_INTERRUPTION_FAILED}
        target = RESERVATION_SUCCEEDED if resolution == 'voided' else RESERVATION_FAILED
        resolved['state'] = target
        if not self._journal.advance_mapping(
                scope, fingerprint, from_states=(RESERVATION_UNKNOWN,),
                to_state=target, record=resolved):
            raise KtdPlatformError('MAPPING_RESERVATION_CONFLICT')
        self._last_reservation = resolved
        return self._mapping_evidence(resolved)

    def mapping_interruption_audit(self) -> dict[str, Any]:
        """只读：最近一次测绘中断原子操作的结论，不含坐标或业务数据。"""
        snapshot = getattr(self._mapping_gateway, 'snapshot', None)
        base = snapshot() if callable(snapshot) else {
            'execution_mode': getattr(self._mapping_gateway, 'execution_mode', 'unavailable')}
        return {**base,
                'idempotency_supported': self._mapping_gateway_idempotent,
                'last': dict(self._last_interruption) if self._last_interruption else None,
                'reservation': (self._mapping_evidence(self._last_reservation)
                                if self._last_reservation else None)}

    def _compile(self, request: FlightControlActionRequest, snapshot: KtdSnapshot,
                 *, interruption_pending: bool = False) -> KtdCommand:
        try:
            check_flight_policy(request.action, snapshot.state)
            if mapping_interruption(request.action, snapshot.state) and not interruption_pending:
                # 能力探测等路径不允许触发中断，直接按不可用处理。
                raise PolicyError('MAPPING_INTERRUPTION_UNAVAILABLE', MAPPING_MESSAGE)
            effective, _ = normalize_parameters(request.action, request.parameters, snapshot.state)
            if effective != request.parameters:
                raise PolicyError('DEVICE_LIMITS_CHANGED', '设备极值已变化，请重新预览。')
        except PolicyError as exc:
            raise KtdPlatformError(exc.code) from None
        if isinstance(request, JavaActionRequest):
            if not self._java_tools_enabled or self._track_commands or snapshot.profile != 'DJI_HANGAR':
                raise KtdPlatformError('JAVA_TOOL_UNAVAILABLE')
            if request.action in {'point_flight_stop','point_orbit_stop'} and snapshot.state.get('airborne') is not True:
                raise KtdPlatformError('FLIGHT_STATE_UNAVAILABLE')
            endpoint, values = encode_java_tool(request)
            return KtdCommand(endpoint, {'terminalId':self.binding.drone_id,'siteId':self.site_id,
                **({'taskId':self.task_id} if self.task_id is not None else {}),**values})
        action = FlightControlAction(request.action).value
        if action in {'task_end_current', 'task_end_all'}:
            if self.task_id is None or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', self.task_id):
                raise KtdPlatformError('JAVA_TASK_REQUIRED')
            if snapshot.state.get('temporary_task_id') is not None or snapshot.state.get('task_scope') != 'bound_main_only':
                raise KtdPlatformError('TASK_SCOPE_UNAVAILABLE')
            return KtdCommand('/portal/task/overTask/' + self.task_id, {})
        if action in _PAYLOAD_TYPES:
            if self._track_commands:
                raise KtdPlatformError('TRACKING_UNSUPPORTED')
            path, operation = _PAYLOAD_TYPES[action]
            if action == 'speaker_speak':
                # Existing 201 may invoke paid external TTS. Require explicit server capability.
                if snapshot.capabilities.get('LOCAL_SPEAKER_TEXT') is not True:
                    raise KtdPlatformError('LOCAL_SPEAKER_TEXT_UNAVAILABLE')
                value = request.parameters['text']
            else:
                value = request.parameters['value']
                if int(value) != value:
                    raise KtdPlatformError('INTEGER_PERCENT_REQUIRED')
                value = str(int(value)) if action == 'speaker_volume' else int(value)
            body = {'uavId': self.binding.drone_id, 'siteId': self.site_id,
                    'type': operation, 'param': value}
            if self.task_id is not None:
                body['taskId'] = self.task_id
            return KtdCommand(path, body)
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
            if self._legacy_read_api and snapshot.state.get('legacy_pose_verified') is not True:
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
                # 高度门槛通过后，再用 Java 回放核对终点。对不上就拒绝，不改 20 米门槛。
                enforce_dji_flyto_replay(action, request.parameters, snapshot.state,
                                         profile=snapshot.profile)
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
            if action in _RELATIVE_GIMBAL and 'angle_deg' in request.parameters:
                # DJUtil.camera_screen_drag使用deg/s；stepPercentage不是目标角度。
                # param=0仅停Java循环，未发送零速度，不能以时间乘速度冒充精确角度。
                raise KtdPlatformError('GIMBAL_EXACT_ANGLE_UNAVAILABLE')
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
        if action == "hover":
            return self._compile_hover(snapshot, body)
        if action in _FLY_TYPES:
            return self._compile_fly(action, request, snapshot, body)
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
        if action in _EXTRA_CAMERA:
            if action in _LENS_MODES:
                body.update(type=105, cameraMode=_LENS_MODES[action])
            elif action == "camera_zoom":
                if int(request.parameters['value']) != request.parameters['value']:
                    raise KtdPlatformError('INTEGER_ZOOM_REQUIRED')
                body.update(type=103, cameraMode=6, param=request.parameters["value"])
            elif action == "camera_panorama":
                if snapshot.state.get("legacy_pose_verified") is not True:
                    raise KtdPlatformError("FLIGHT_POSE_UNAVAILABLE")
                body.update(type=125)
            elif action == "infrared_set_limits":
                low, high = request.parameters["min_c"], request.parameters["max_c"]
                # 原Java DTO以Integer读取，拒绝静默截断模型的小数温度。
                if int(low) != low or int(high) != high:
                    raise KtdPlatformError("INTEGER_TEMPERATURE_REQUIRED")
                body.update(type=127, jsonParams=_json({"openIsotherm": True,
                    "lowerLimit": int(low), "upperLimit": int(high)}))
            return KtdCommand("/screen/uav/camera/control", body)
        raise KtdPlatformError("ACTION_UNSUPPORTED")

    def _compile_hover(self, snapshot, body):
        """把“悬停”折算成当前运动方式对应的取消指令。

        平台没有统一的悬停操作码（`UavOpeConstant` 只有 110 暂停航线、111 停止航线、
        113 紧急停止、207 移动停止）。207 只清 Java 侧的重发记录，对 mode=2 的
        一次性 fly_to_point 无效，所以必须先看最近一次运动方式再选指令；
        绝不用 113 顶替——那是紧急停止，会改变任务状态。
        """
        if snapshot.state.get("airborne") is not True:
            raise KtdPlatformError("FLIGHT_STATE_UNAVAILABLE")
        route_status = snapshot.state.get("route_status")
        if not isinstance(route_status, str) or not route_status:
            # 不知道航线状态就无法判断该暂停还是该停运动；宁可拒绝也不发一条无效指令。
            raise KtdPlatformError("ROUTE_STATE_UNAVAILABLE")
        if route_status == "running":
            if self.task_id is None:
                raise KtdPlatformError("JAVA_TASK_REQUIRED")
            body.update(type=_ROUTE_TYPES["route_pause"])
            return KtdCommand("/screen/uav/route/control", body)
        if self._last_motion in _HOVER_STOP_TYPES:
            body.update(type=_HOVER_STOP_TYPES[self._last_motion])
            return KtdCommand("/screen/uav/fly/control", body)
        body.update(type=207, mode=3, param=0)
        return KtdCommand("/screen/uav/operation/control", body)

    def _compile_fly(self, action, request, snapshot, body):
        """指点飞行（119）与进入环绕（121），都走 /screen/uav/fly/control。"""
        if snapshot.profile != "DJI_HANGAR":
            raise KtdPlatformError("PROFILE_UNSUPPORTED")
        if snapshot.state.get("airborne") is not True:
            raise KtdPlatformError("FLIGHT_STATE_UNAVAILABLE")
        p = request.parameters
        if action == "point_flight":
            height = p["altitude_m"]
            # 119 和距离微操一样调用 fly_to_point。当前相对高度低于 20 米时会先爬升，
            # 这段不在指点折线里。目标高度是否低于 20 米要等厂商范围校验之后再看。
            self._require_no_implicit_climb(snapshot.state)
            body.update(type=_FLY_TYPES[action], longitude=float(p["longitude"]),
                        latitude=float(p["latitude"]), speed=_FLY_SPEED_MPS)
        else:
            # DJI 的 poi_mode_enter 只收经纬度和高度：半径由进入时的水平距离决定。
            # 请求半径和当前距离不一致时，围栏检查的圆不会被飞到，必须拒绝而不是只警告。
            from .path_geometry import PathGeometryError, assess_orbit_command
            try:
                orbit = assess_orbit_command(p, snapshot.state)
            except PathGeometryError as error:
                raise KtdPlatformError(error.code) from None
            if not orbit.aligned:
                raise KtdPlatformError(orbit.code, orbit.message)
            height = snapshot.state.get("altitude_m")
            body.update(type=_FLY_TYPES[action], longitude=float(p["longitude"]),
                        latitude=float(p["latitude"]), speed=-float(_FLY_SPEED_MPS))
        if not _finite(height, _MIN_ELLIPSOID_HEIGHT_M, _MAX_ELLIPSOID_HEIGHT_M):
            raise KtdPlatformError("TARGET_ALTITUDE_OUT_OF_RANGE")
        if action == "point_flight":
            self._require_verified_relative_target(snapshot.state, float(height))
            # 同高或纯升降才和单点指令一致；斜向变高对不上已检查直线，拒绝。
            enforce_dji_flyto_replay(action, p, snapshot.state, profile=snapshot.profile)
        body["height"] = float(height)
        return KtdCommand("/screen/uav/fly/control", body)

    @staticmethod
    def _require_no_implicit_climb(state):
        """当前相对高度低于 20 米时，fly_to_point 会先爬升到 20 米。"""
        relative = state.get("height_m")
        if not _finite(relative, -500, 10000):
            raise KtdPlatformError("RELATIVE_HEIGHT_UNAVAILABLE")
        if relative < 20:
            raise KtdPlatformError("DJI_FLYTO_IMPLICIT_CLIMB")

    @staticmethod
    def _require_verified_relative_target(state, target_altitude):
        """目标落到相对 20 米以下时，厂商行为未核验，不能靠椭球高范围放行。"""
        relative = state.get("height_m")
        current = state.get("altitude_m")
        if not _finite(relative, -500, 10000) or not _finite(current, -500, 10000):
            raise KtdPlatformError("FLIGHT_POSE_UNAVAILABLE")
        if relative + (float(target_altitude) - float(current)) < 20:
            raise KtdPlatformError("DJI_LOW_TARGET_UNVERIFIED")

    @staticmethod
    def _platform_warnings(action, parameters):
        """确认卡上必须可见的平台语义差异；不改变用户参数，只如实说明。"""
        if action == "point_orbit":
            return ["当前水平距离与请求半径一致。大疆进入环绕不接收半径参数，"
                    "实际半径等于进入时的水平距离；围栏按该半径检查，"
                    "设备是否沿该圆飞行尚未验证。"]
        if action == "hover":
            return ["悬停通过取消当前运动指令实现，平台只返回受理，"
                    "设备是否已保持位置未经回执验证。"]
        return []

    def _record_motion(self, action, status):
        """只在平台确认受理后更新最近运动方式，供后续悬停选择取消指令。"""
        if status not in {"submitted", "accepted", "unknown"}:
            return
        if action in _MOVE_TYPES or action == "point_flight":
            self._last_motion = "displacement"
        elif action in _TURN_TYPES or action == "stick":
            self._last_motion = "stick"
        elif action == "point_orbit":
            self._last_motion = "orbit"
        elif action in {"hover", "emergency_stop", "route_pause", "point_flight_stop",
                        "point_orbit_stop", "land", "land_alternate", "return_home"}:
            self._last_motion = None

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
        # 回中并停止重发之后没有持续运动，后续悬停不应再取消已经不存在的运动。
        self._last_motion = None

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
            request_type = JavaActionRequest if receipt.action in JAVA_TOOLS else FlightControlActionRequest
            request = request_type(receipt.command_id, receipt.idempotency_key,
                receipt.action, dict(receipt.parameters), receipt.context_version)
        else:
            raise KtdPlatformError('CONFIRMATION_MISMATCH')
        request.validate()
        return request

    def preview(self, request: FlightControlActionRequest) -> KtdReceipt:
        with self._lock:
            adjustments = []
            try:
                if not isinstance(request, FlightControlActionRequest):
                    raise ValueError("invalid request")
                if request.action in ADJUSTABLE and request.action in SUPPORTED_ACTIONS:
                    if not self._enabled:
                        return self._reject(request, 'PLATFORM_DISABLED')
                    policy_snapshot = self._snapshot(request.action)
                    effective, adjustments = normalize_parameters(request.action, request.parameters, policy_snapshot.state)
                    request = replace(request, parameters=effective)
                request.validate()
                if not _identifier(request.command_id) or not _identifier(request.idempotency_key):
                    raise ValueError("invalid identifiers")
                if isinstance(request, JavaActionRequest):
                    action = request.action
                    if not self._java_tools_enabled:
                        raise ValueError('Java tools disabled')
                else:
                    action = FlightControlAction(request.action)
                    spec = ACTION_SPECS[action]
                    if set(request.parameters) - set(spec.parameters) or any(key not in request.parameters for key in spec.required):
                        raise ValueError("invalid schema")
            except (PolicyError, KtdPlatformError) as exc:
                return self._reject(request, exc.code if isinstance(exc, PolicyError) else str(exc))
            except (ValueError, TypeError, AttributeError, OverflowError):
                if not isinstance(request, FlightControlActionRequest):
                    request = FlightControlActionRequest("invalid", "invalid", "read_state")
                return self._reject(request, "INVALID_PARAMETERS")
            if request.context_version != self.binding.context_version:
                return self._reject(request, "STALE_CONTEXT")
            if action != FlightControlAction.READ_STATE and not self._enabled:
                return self._reject(request, "PLATFORM_DISABLED")
            action_value = action.value if isinstance(action, FlightControlAction) else action
            if action_value == 'ai_algorithm_set':
                # V1.4 2.1.2：平台只有任务级总开关，没有算法粒度的启停契约，
                # 不能把总开关冒充成算法生命周期。
                return self._reject(request, 'ALGORITHM_LIFECYCLE_UNAVAILABLE')
            if action_value not in SUPPORTED_ACTIONS:
                return self._reject(request, "ACTION_UNSUPPORTED")
            try:
                snapshot = self._snapshot(action_value)
                if action == FlightControlAction.READ_STATE:
                    return self._receipt(request, "observed", snapshot=snapshot, message="已读取权威状态")
                command = self._compile(request, snapshot, interruption_pending=True)
                self._task_end_precheck(request)
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
                                        message="待确认平台指令，设备效果尚未验证", adjustments=adjustments)
                warning = check_path(self._path_checker, self.binding, action_value, request.parameters,
                                     self._path_state(snapshot))
                receipt = replace(receipt, warnings=([warning] if warning else [])
                                  + ([MAPPING_MESSAGE] if mapping_interruption(action_value, snapshot.state) else [])
                                  + self._platform_warnings(action_value, request.parameters))
                self._pending[confirmation_id] = _Pending(copy.deepcopy(request), receipt, snapshot_hash, command, deadline)
                return receipt
            except PolicyError as error:
                return self._reject(request, error.code)
            except KtdPlatformError as error:
                return self._reject(request, str(error), detail=error.detail)
            except Exception:
                return self._reject(request, "JOURNAL_OR_STATE_UNAVAILABLE")


    @staticmethod
    def _path_state(snapshot):
        """把机型放进围栏输入。不写回快照，避免机型字段改变确认哈希。"""
        state = dict(snapshot.state)
        state["profile"] = snapshot.profile
        return state

    def _task_end_precheck(self, request):
        if request.action not in {'task_end_current', 'task_end_all'}:
            return
        checker = getattr(self._transport, 'task_end_check', None)
        if not callable(checker):
            raise KtdPlatformError('TASK_END_CHECK_UNAVAILABLE')
        response = checker(self.binding, self.task_id)
        data = response.payload.get('data') if isinstance(response, KtdHttpResponse) else None
        if (not isinstance(response, KtdHttpResponse) or response.status_code != 200
                or response.payload.get('code') != 200 or not isinstance(data, Mapping)
                or data.get('pass') is not True or data.get('needConfirm') is not True):
            raise KtdPlatformError('TASK_END_SAFETY_REJECTED')

    def _matches(self, receipt, confirmation_id, command_id, idempotency_key, confirmation_binding, context_version):
        return (type(context_version) is int and receipt.confirmation_id == confirmation_id and receipt.command_id == command_id
                and receipt.idempotency_key == idempotency_key and receipt.confirmation_binding == confirmation_binding
                and receipt.context_version == context_version and context_version == self.binding.context_version)

    def confirm(self, confirmation_id: str, *, command_id: str, idempotency_key: str,
                confirmation_binding: str, context_version: int) -> KtdReceipt:
        from .vendor_control import VendorControlRequest, VENDOR_KIND
        with self._lock:
            # 每次 confirm 重新收集本轮的测绘证据，避免把上一条命令的 reservation
            # 误挂到这一条回执上。
            self._last_interruption = None
            self._last_reservation = None
            fallback = FlightControlActionRequest(command_id, idempotency_key, "read_state", {}, context_version)
            pending = self._pending.get(confirmation_id)
            request = pending.request if pending else fallback
            snapshot = None
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
                    action_value = (request.action.value if isinstance(request.action, FlightControlAction)
                                    else request.action)
                    # 2–3：读取最新权威快照，校验资源绑定、控制权、设备能力与状态版本。
                    snapshot = self._snapshot(action_value)
                    # 4：只做确定性编译；编译失败不产生任何外部副作用。
                    command = self._compile(request, snapshot, interruption_pending=True)
                    # 5：任务结束安全检查（仅 task_end_* 需要）。
                    self._task_end_precheck(request)
                    # 6：confirm 阶段重新执行围栏/路径检查。blocked 直接拒绝；
                    # timeout/unavailable/malformed 按 V1.4 2.2.2 降级并保留 warning，
                    # 绝不改写成 clear，也不跳过后续控制权与确认校验。
                    try:
                        warning = check_path(self._path_checker, self.binding, action_value,
                                             request.parameters, self._path_state(snapshot))
                    except PolicyError as error:
                        raise KtdPlatformError(error.code) from None
                # 7：重新比较快照哈希与编译结果；任何漂移都作废这张确认卡。
                if self._snapshot_hash(snapshot) != pending.snapshot_hash or command != pending.command:
                    self._pending.pop(confirmation_id)
                    return self._reject(request, "AUTHORITATIVE_STATE_CHANGED")
                fingerprint, scope = self._fingerprint(request), self._scope(idempotency_key)
                # 8–9：以上确定性预检查全部通过，才允许触碰不可逆的测绘原子中断。
                # 顺序固定：持久化 reservation（跨进程唯一）→ 网关 → 记 succeeded；
                # 网关结果未知一律停在 mapping_interruption_unknown，不重试、不 submit。
                interruption, settled = ((None, None) if vendor
                                         else self._mapping_interruption_reserved(
                                             request, snapshot, scope=scope,
                                             fingerprint=fingerprint,
                                             trace_id=pending.receipt.trace_id))
                if settled is not None:
                    # 同一幂等键已由另一执行器结算：直接返回既有回执，不重复作废/下发。
                    self._pending.pop(confirmation_id, None)
                    return settled
                fields = dict(snapshot=snapshot, confirmation_id=confirmation_id,
                              confirmation_binding=confirmation_binding, trace_id=pending.receipt.trace_id,
                              adjustments=pending.receipt.adjustments,
                              warnings=list(dict.fromkeys(pending.receipt.warnings +
                                  ([warning] if not vendor and warning else []))))
                if vendor:
                    fields['expires_at'] = pending.receipt.expires_at
                if interruption is not None:
                    fields['mapping_interruption'] = interruption
                if self._last_reservation is not None:
                    fields['mapping_reservation'] = self._mapping_evidence(self._last_reservation)
                unknown = self._receipt(request, "unknown", error_code="PLATFORM_OUTCOME_UNKNOWN",
                                        message="平台结果未知，禁止自动重发；需要人工核对", **fields)
                # 10：幂等账本先落 unknown 再提交；结果未知时不再重试。
                if not self._journal.claim(scope, fingerprint, unknown.to_dict()):
                    previous = self._journal.read(scope)
                    return KtdReceipt.from_dict(previous[1]) if previous and previous[0] == fingerprint else self._reject(request, "IDEMPOTENCY_CONFLICT")
                self._pending.pop(confirmation_id)
            except KtdPlatformError as error:
                self._pending.pop(confirmation_id, None)
                rejected_code = str(error)
                evidence = (dict(self._last_interruption)
                            if rejected_code.startswith('MAPPING_INTERRUPTION') and self._last_interruption
                            else None)
                if rejected_code == MAPPING_INTERRUPTION_UNKNOWN:
                    # 网关结果未知：既不是拒绝也不是成功，保留 unknown 语义与证据。
                    unknown_fields = dict(snapshot=snapshot)
                    if evidence is not None:
                        unknown_fields['mapping_interruption'] = evidence
                    if self._last_reservation is not None:
                        unknown_fields['mapping_reservation'] = self._mapping_evidence(
                            self._last_reservation)
                    return self._receipt(
                        request, 'unknown', error_code=MAPPING_INTERRUPTION_UNKNOWN,
                        message=("测绘中断网关结果无法确认，本地 reservation 已保留为 "
                                 "mapping_interruption_unknown；不会自动重试网关或飞控下发，"
                                 "需人工核对或凭权威回执显式恢复。"), **unknown_fields)
                if evidence is not None:
                    # 把「中断未完成」的证据留在拒绝回执里，而不是只给一句通用拒绝。
                    fields = {'mapping_interruption': evidence}
                    if self._last_reservation is not None:
                        fields['mapping_reservation'] = self._mapping_evidence(self._last_reservation)
                    return self._reject(request, rejected_code, detail=error.detail, **fields)
                return self._reject(request, rejected_code, detail=error.detail)
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
                        # 结果未知也要记：指令可能已经发出，后续悬停必须按运动方式取消。
                        self._record_motion(action_value, status)
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
        from .command_receipts import RECEIPT_UNAVAILABLE, decode_command_receipt, validate_receipt_binding
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
                # 查询前再核一次设备/任务绑定：换绑或换任务后不能拿旧回执当本次结果。
                validate_receipt_binding(receipt, binding=self.binding, site_id=self.site_id,
                    task_id=self.task_id, request=request, trace_id=receipt.trace_id)
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
                # 回执通道不可用时明确报错，绝不用 accepted 顶替。
                raise KtdPlatformError(RECEIPT_UNAVAILABLE) from None

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


#: 只吸收双精度残差。本仓与 GeoUtil 在 20 米内的实测差小于 1e-8 米，
#: 超过 1 毫米就视为不是同一点，不能把容差放大到放行另一条路径。
_FLYTO_MATCH_M = 0.001


def _java_move_bearing(yaw, action):
    """DJHangarOperation 距离模式的方位角，保持它的正负分支，不改成另一套归一化。"""
    if action == "move_forward":
        return yaw
    if action == "move_backward":
        return (yaw - 180.0) if yaw > 0.0 else (yaw + 180.0)
    if action == "move_left":
        return (yaw + 270.0) if (yaw - 90.0) < -180.0 else (yaw - 90.0)
    if action == "move_right":
        return (yaw - 270.0) if (yaw + 90.0) > 180.0 else (yaw + 90.0)
    return None


def _java_calc_lat_lon(longitude, latitude, bearing_deg, distance_m):
    """GeoUtil.calcLatAndlon 的回放。

    注释写的是千米，但半径是米，调用处传入的也是米。按千米解释会飞出去约 1000 倍，
    和已检查终点对不上，因此这里按米回放。
    """
    arc = 6371.393 * 1000.0
    lon1 = math.radians(longitude)
    lat1 = math.radians(latitude)
    bearing = math.radians(bearing_deg)
    lat2 = math.asin(math.sin(lat1) * math.cos(distance_m / arc)
                     + math.cos(lat1) * math.sin(distance_m / arc) * math.cos(bearing))
    lon2 = lon1 + math.atan2(
        math.sin(bearing) * math.sin(distance_m / arc) * math.cos(lat1),
        math.cos(distance_m / arc) - math.sin(lat1) * math.sin(lat2))
    return math.degrees(lon2), math.degrees(lat2)


def dji_commanded_flyto_point(action, parameters, state):
    """Java 实际交给 fly_to_point 的那一个点。算不出来就返回 None，不能猜。"""
    if not isinstance(parameters, Mapping) or not isinstance(state, Mapping):
        return None
    longitude = state.get("longitude")
    latitude = state.get("latitude")
    altitude = state.get("altitude_m")
    if (not _finite(longitude, -180, 180) or not _finite(latitude, -90, 90)
            or not _finite(altitude, -500, 10000)):
        return None
    if action == "point_flight":
        target_longitude = parameters.get("longitude")
        target_latitude = parameters.get("latitude")
        target_altitude = parameters.get("altitude_m")
        if (not _finite(target_longitude, -180, 180) or not _finite(target_latitude, -90, 90)
                or not _finite(target_altitude, _MIN_ELLIPSOID_HEIGHT_M, _MAX_ELLIPSOID_HEIGHT_M)):
            return None
        return float(target_longitude), float(target_latitude), float(target_altitude)
    distance = parameters.get("distance_m")
    if not _finite(distance, 0.01, 1000):
        return None
    if action == "move_up":
        return float(longitude), float(latitude), float(altitude) + float(distance)
    if action == "move_down":
        return float(longitude), float(latitude), float(altitude) - float(distance)
    yaw = state.get("yaw_deg")
    if not _finite(yaw, -180, 180):
        return None
    bearing = _java_move_bearing(float(yaw), action)
    if bearing is None:
        return None
    try:
        end_longitude, end_latitude = _java_calc_lat_lon(
            float(longitude), float(latitude), bearing, float(distance))
    except (ValueError, OverflowError):
        return None
    return end_longitude, end_latitude, float(altitude)


def _flyto_points_match(left, right):
    from .geometry import horizontal_distance_m
    try:
        gap = horizontal_distance_m(left[0], left[1], right[0], right[1])
    except (TypeError, ValueError):
        return False
    return gap <= _FLYTO_MATCH_M and abs(float(left[2]) - float(right[2])) <= _FLYTO_MATCH_M


def dji_flyto_replay(action, parameters, state, profile=None):
    """核对 fly_to_point 回放和已检查折线。

    返回 None 表示可以沿用现有下发：单点终点与折线终点一致，且没有同时改水平位置和高度。
    相对高度低于 20 米时仍返回隐含爬升，直线折线不能把这次爬升放行。
    其他机型和非飞向动作返回 None，由原逻辑处理。
    """
    if not isinstance(state, Mapping):
        return "FLYTO_TRAJECTORY_UNVERIFIED"
    resolved = profile if profile is not None else state.get("profile")
    if resolved != "DJI_HANGAR":
        return None
    if action not in _MOVE_TYPES and action != "point_flight":
        return None
    relative = state.get("height_m")
    if not _finite(relative, -500, 10000):
        return "RELATIVE_HEIGHT_UNAVAILABLE"
    if relative < 20:
        return "DJI_FLYTO_IMPLICIT_CLIMB"
    from .path_geometry import PathGeometryError, PathUnavailable, build_path
    pose = dict(state)
    pose["profile"] = resolved
    try:
        geometry = build_path(action, parameters, pose)
    except (PathGeometryError, PathUnavailable):
        return "FLYTO_TRAJECTORY_UNVERIFIED"
    if geometry is None or geometry.segments != 1 or len(geometry.points) != 2:
        return "FLYTO_TRAJECTORY_UNVERIFIED"
    if geometry.vertical_field != "altitude_m":
        return "FLYTO_TRAJECTORY_UNVERIFIED"
    commanded = dji_commanded_flyto_point(action, parameters, state)
    if commanded is None:
        return "FLYTO_TRAJECTORY_UNVERIFIED"
    start, end = geometry.points
    current = (state.get("longitude"), state.get("latitude"), state.get("altitude_m"))
    if not _flyto_points_match((start["longitude"], start["latitude"], start["altitude"]), current):
        return "FLYTO_TRAJECTORY_UNVERIFIED"
    if not _flyto_points_match((end["longitude"], end["latitude"], end["altitude"]), commanded):
        return "FLYTO_TRAJECTORY_UNVERIFIED"
    from .geometry import horizontal_distance_m
    try:
        horizontal = horizontal_distance_m(
            start["longitude"], start["latitude"], end["longitude"], end["latitude"]) > _FLYTO_MATCH_M
    except (TypeError, ValueError):
        return "FLYTO_TRAJECTORY_UNVERIFIED"
    vertical = abs(float(end["altitude"]) - float(start["altitude"])) > _FLYTO_MATCH_M
    # 斜向变高的报文仍然只有终点。没有采样能证明飞机会沿这条三维直线走，不能放行。
    if horizontal and vertical:
        return "FLYTO_TRAJECTORY_UNVERIFIED"
    return None


def enforce_dji_flyto_replay(action, parameters, state, profile=None):
    code = dji_flyto_replay(action, parameters, state, profile=profile)
    if code:
        raise KtdPlatformError(code)


def guard_dji_flyto(action, parameters, state):
    """大疆 fly_to_point 的下发前提。其他机型、其他动作直接返回。

    距离微操和指点飞行在 Java 里都调用 fly_to_point。相对高度低于 20 米时会先爬升，
    下降后的目标若低于 20 米则行为未核验。高度门槛通过后，再回放实际单点轨迹；
    和已检查直线对不上就拒绝，不用更宽的直线假设换取下发。
    """
    if not isinstance(state, Mapping) or state.get("profile") != "DJI_HANGAR":
        return
    if action not in _MOVE_TYPES and action != "point_flight":
        return
    KtdPlatformExecutor._require_no_implicit_climb(state)
    if action == "point_flight":
        target = parameters.get("altitude_m")
        if not _finite(target, _MIN_ELLIPSOID_HEIGHT_M, _MAX_ELLIPSOID_HEIGHT_M):
            raise KtdPlatformError("TARGET_ALTITUDE_OUT_OF_RANGE")
        KtdPlatformExecutor._require_verified_relative_target(state, float(target))
    elif action in {"move_up", "move_down"}:
        current = state.get("altitude_m")
        distance = parameters.get("distance_m")
        if not _finite(current, -500, 10000) or not _finite(distance, 0.01, 1000):
            raise KtdPlatformError("FLIGHT_POSE_UNAVAILABLE")
        delta = float(distance) if action == "move_up" else -float(distance)
        target = float(current) + delta
        if not _finite(target, _MIN_ELLIPSOID_HEIGHT_M, _MAX_ELLIPSOID_HEIGHT_M):
            raise KtdPlatformError("DJI_TARGET_ALTITUDE_OUT_OF_RANGE")
        KtdPlatformExecutor._require_verified_relative_target(state, target)
    enforce_dji_flyto_replay(action, parameters, state)
