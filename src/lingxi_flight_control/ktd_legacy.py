"""Route A：只用 ktd 现有只读接口拼装测试授权快照。

该模块不访问 ktd 数据库或 MQTT，也不修改 ktd_backend。它把现有只读接口返回的
身份、资源绑定、控制权和在线/飞行状态拼成 ``KtdSnapshot``，并明确标注
``authorization_quality='legacy-read-api'``：

- 旧接口没有设备采样/接收时间，``observed_at`` 只能取本次读取时间；
- 旧接口不返回控制权租约，租约是短时合成值，不能当作厂商航权证明；
- 旧接口没有经纬度、高度和航向，Agent 无法重算位移目标或校验 20 米隐含爬升；
  位移只能由 Java 原控制接口用服务端实时姿态计算，需显式打开 pose-unverified。

因此该快照只用于已获授权的隔离测试环境，不能升级为生产授权。
"""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import math
import time

from .full_control import RuntimeBinding
from .ktd_platform import (
    KtdHttpResponse, KtdPlatformError, KtdSnapshot, _identifier,
    _valid_snapshot_version,
)

LEGACY_QUALITY = "legacy-read-api"
DJI_HANGAR_BRAND = "207601"
OFFLINE_STATES = frozenset({"-1", "14"})
BASE_WRITE_ACTIONS = frozenset({
    "move_forward", "move_backward", "move_left", "move_right", "move_up", "move_down",
    "camera_photo", "camera_start_recording", "camera_stop_recording", "gimbal_reset",
})
TURN_ACTIONS = frozenset({"turn_left", "turn_right"})
GIMBAL_ACTIONS = frozenset({
    "gimbal_up", "gimbal_down", "gimbal_left", "gimbal_right",
    "gimbal_45", "gimbal_90", "gimbal_reset",
})
#: 不需要姿态回读的写动作；转向只有在旧接口返回了经纬度/航向时才加入。
WRITE_ACTIONS = BASE_WRITE_ACTIONS | GIMBAL_ACTIONS
LEGACY_ACTIONS = WRITE_ACTIONS | {"read_state"}
LEGACY_CAPABILITIES = {action: True for action in WRITE_ACTIONS}
LEGACY_CAPABILITIES["UAV_MOVE_DISTANCE"] = True
_STATE_FIELDS = ("online", "airborne", "longitude", "latitude", "altitude_m",
                 "height_m", "yaw_deg", "battery_percent", "flight_status", "route_status")
_POSE_FIELDS = ("longitude", "latitude", "altitude_m", "height_m", "yaw_deg")


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _finite(value, low, high):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and low <= value <= high
    except OverflowError:
        return False


def _reject(code):
    raise KtdPlatformError(code)


def _data(response, *, auth_error="LEGACY_STATE_UNAVAILABLE"):
    if (not isinstance(response, KtdHttpResponse) or type(response.status_code) is not int
            or not isinstance(response.payload, Mapping)):
        _reject("LEGACY_STATE_UNAVAILABLE")
    if response.status_code == 401:
        _reject("AUTHENTICATION_REQUIRED")
    if response.status_code != 200:
        _reject("LEGACY_STATE_UNAVAILABLE")
    payload = response.payload
    if type(payload.get("code")) is not int or payload["code"] != 200:
        _reject("AUTHENTICATION_REQUIRED" if payload.get("code") in (401, 403)
                else auth_error)
    if "success" in payload and payload["success"] is not True:
        _reject(auth_error)
    return payload.get("data")


def _number(value, low, high):
    """只接受有限数值；布尔值、字符串和 NaN/Inf 一律视为未知。"""
    if type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and low <= number <= high else None


def _pose_time(data, now):
    """旧姿态接口的时间戳可能是毫秒或秒；无法确定时返回 None，调用方回退到读取时间。"""
    for key in ("timeStamp", "timestamp"):
        value = data.get(key)
        if isinstance(value, str) and value.isdigit():
            value = int(value)
        if type(value) in (int, float) and math.isfinite(value):
            seconds = float(value)
            if seconds > 1e12:
                seconds /= 1000.0
            if 0 < seconds <= now + 1:
                return seconds
    return None


class LegacyKtdSnapshotProvider:
    """用五个现有只读接口 + 姿态调试接口生成 route A 的测试授权快照。

    该 provider 每次 ``__call__`` 都重新读取；确认阶段会再次读取并比较可核验事实。
    控制权、资源绑定或在线状态变化会使旧确认卡失效；合成租约和实时姿态不进入确认哈希。
    """

    def __init__(self, binding: RuntimeBinding, *, site_id: str, task_id: str | None,
                 transport, clock=time.time, lease_ttl_sec: float = 300.0):
        if not isinstance(binding, RuntimeBinding):
            _reject("INVALID_LEGACY_BINDING")
        try:
            binding.validate()
        except ValueError:
            _reject("INVALID_LEGACY_BINDING")
        if (not all(_identifier(value) for key, value in binding.to_dict().items()
                    if key != "context_version")
                or not _identifier(site_id)
                or (task_id is not None and not _identifier(task_id))
                or not callable(clock)
                or not callable(getattr(transport, "read_legacy", None))
                or getattr(transport, "actor_id", None) != binding.actor_id
                or not _finite(lease_ttl_sec, 30, 600)):
            _reject("INVALID_LEGACY_BINDING")
        self.binding = binding
        self.site_id = site_id
        self.task_id = task_id
        self._transport = transport
        self._clock = clock
        self._lease_ttl = float(lease_ttl_sec)
        # 旧站点列表/机库视角接口会返回无人机 SN；姿态回读需要它。
        self.uav_sn = None

    def _read(self, resource, *, drone_id=None, query=None):
        try:
            response = self._transport.read_legacy(
                resource, project_id=self.binding.project_id,
                drone_id=drone_id, query=query)
        except KtdPlatformError:
            raise
        except Exception:
            _reject("LEGACY_STATE_UNAVAILABLE")
        return _data(response)

    def __call__(self) -> KtdSnapshot:
        user = self._read_user()
        self._require_binding(user)
        owner = self._read_owner()
        profile = self._read_profile()
        online, airborne, uav_state = self._read_state()
        pose = self.pose()
        can_write = bool(isinstance(user.get("permission"), list)
                         and "taskManage:Issued" in user["permission"])
        writes = set(BASE_WRITE_ACTIONS) | set(GIMBAL_ACTIONS)
        # 转向必须能回读航向；读不到姿态时不授予转向权限，而不是让模型猜。
        if pose is not None and pose.get("yaw_deg") is not None:
            writes |= set(TURN_ACTIONS)
        permissions = (writes | {"read_state"}) if can_write else {"read_state"}
        availability = {action: {"available": True, "reasons": []}
                        for action in permissions}
        capabilities = {}
        if can_write:
            capabilities = {action: True for action in writes}
            capabilities["UAV_MOVE_DISTANCE"] = True
        now = self._now()
        facts = {
            "schema": "lingxi-legacy-ktd-snapshot/v1",
            "authorization_quality": LEGACY_QUALITY,
            "actor_id": self.binding.actor_id,
            "project_id": self.binding.project_id,
            "site_id": self.site_id,
            "hangar_id": self.binding.hangar_id,
            "drone_id": self.binding.drone_id,
            "task_id": self.task_id,
            "uav_sn": self.uav_sn,
            "control_owner": owner,
            "profile": "DJI_HANGAR",
            "online": online,
            "airborne": airborne,
            "uav_state": uav_state,
            # 姿态是实时遥测，确认前会漂移；版本只绑定授权事实（权限/能力/owner/
            # 在线状态），姿态值单独放在 state 里供闭环使用。
            "permissions": sorted(permissions),
            "capabilities": {key: value for key, value in capabilities.items() if value is True},
        }
        version = _hash(facts)
        if not _valid_snapshot_version(version):
            _reject("INVALID_LEGACY_SNAPSHOT")
        state = {field: None for field in _STATE_FIELDS}
        state.update(online=online, airborne=airborne, flight_status=uav_state)
        observed_at = now
        if pose is not None:
            for key in _POSE_FIELDS:
                state[key] = pose.get(key)
            if pose.get("observed_at") is not None:
                observed_at = pose["observed_at"]
        return KtdSnapshot(
            actor_id=self.binding.actor_id, project_id=self.binding.project_id,
            hangar_id=self.binding.hangar_id, drone_id=self.binding.drone_id,
            site_id=self.site_id, task_id=self.task_id,
            context_version=self.binding.context_version,
            version=version, observed_at=observed_at, control_owner=owner,
            profile="DJI_HANGAR", permissions=frozenset(permissions),
            capabilities=capabilities, state=state,
            control_lease_expires_at=now + self._lease_ttl,
            action_availability=availability,
            authorization_quality=LEGACY_QUALITY,
        )

    def _now(self):
        try:
            now = self._clock()
        except Exception:
            _reject("INVALID_LEGACY_CLOCK")
        if not _finite(now, 0, float("inf")):
            _reject("INVALID_LEGACY_CLOCK")
        return float(now)

    def _read_user(self):
        data = self._read("user_info")
        if not isinstance(data, Mapping):
            _reject("AUTHENTICATION_REQUIRED")
        return data

    def _require_binding(self, user):
        if user.get("empId") != self.binding.actor_id:
            _reject("AUTHENTICATION_REQUIRED")
        if user.get("permission") is not None:
            permission = user.get("permission")
            if (not isinstance(permission, list)
                    or any(not isinstance(item, str) for item in permission)):
                _reject("INVALID_LEGACY_PERMISSION")
        data = self._read("sites", query={"projectId": self.binding.project_id})
        if not isinstance(data, list):
            _reject("RESOURCE_BINDING_MISMATCH")
        matches = [row for row in data if isinstance(row, Mapping)
                   and row.get("id") == self.site_id]
        if len(matches) != 1:
            _reject("RESOURCE_BINDING_MISMATCH")
        row = matches[0]
        if (row.get("uavId") != self.binding.drone_id
                or row.get("hangarId") != self.binding.hangar_id):
            _reject("RESOURCE_BINDING_MISMATCH")
        uav_sn = row.get("uavSn")
        if isinstance(uav_sn, str) and _identifier(uav_sn):
            self.uav_sn = uav_sn

    def _read_owner(self):
        data = self._read("owners", query={"uavId": self.binding.drone_id})
        if not isinstance(data, list):
            _reject("CONTROL_OWNER_UNAVAILABLE")
        employees = []
        for row in data:
            if not isinstance(row, Mapping):
                _reject("CONTROL_OWNER_SOURCE_INVALID")
            employee = row.get("employeeId")
            if not isinstance(employee, str) or not _identifier(employee):
                _reject("CONTROL_OWNER_SOURCE_INVALID")
            employees.append(employee)
        # 旧 list 接口不返回租约/版本；多 owner 一律视为冲突，不取第一条。
        return employees[0] if len(set(employees)) == 1 and len(employees) == 1 else None

    def _read_profile(self):
        data = self._read("permissions", drone_id=self.binding.drone_id)
        if not isinstance(data, Mapping) or data.get("isHangar") is not True:
            _reject("PROFILE_UNSUPPORTED")
        hangar = data.get("hangarInfo")
        if not isinstance(hangar, Mapping) or hangar.get("brand") != DJI_HANGAR_BRAND:
            _reject("PROFILE_UNSUPPORTED")
        return "DJI_HANGAR"

    def pose(self) -> Mapping | None:
        """读取旧调试接口的实时姿态；失败一律返回 None。

        该接口没有凭证以外的写副作用，但它是调试接口，生产环境可能不开放；调用方
        必须把“读不到姿态”当作未知，不能回退到默认值或导航授权。
        """
        if not isinstance(self.uav_sn, str) or not _identifier(self.uav_sn):
            return None
        try:
            data = self._read("pose", query={"sn": self.uav_sn})
        except KtdPlatformError:
            return None
        if not isinstance(data, Mapping):
            return None
        now = self._now()
        values = {}
        for key, source, low, high in (
                ("longitude", "longitude", -180, 180),
                ("latitude", "latitude", -90, 90),
                ("altitude_m", "altitude", -500, 10000),
                ("height_m", "height", -500, 10000),
                ("yaw_deg", "yaw", -360, 360)):
            values[key] = _number(data.get(source), low, high)
        values["observed_at"] = _pose_time(data, now)
        uav_state = data.get("uavState")
        values["uav_state"] = uav_state if isinstance(uav_state, str) else None
        values["uav_sn"] = self.uav_sn
        if values["longitude"] is None and values["latitude"] is None and values["yaw_deg"] is None:
            return None
        return values

    def _read_state(self):
        data = self._read("hangar_state", query={"uavId": self.binding.drone_id})
        if not isinstance(data, Mapping) or data.get("uavId") != self.binding.drone_id:
            _reject("LEGACY_STATE_UNAVAILABLE")
        if self.uav_sn is None:
            uav_sn = data.get("uavSn")
            if isinstance(uav_sn, str) and _identifier(uav_sn):
                self.uav_sn = uav_sn
        uav_state = data.get("uavState")
        if not isinstance(uav_state, str) or not 0 < len(uav_state) <= 32:
            _reject("LEGACY_STATE_UNAVAILABLE")
        online = uav_state not in OFFLINE_STATES
        airborne = data.get("inFlight") if type(data.get("inFlight")) is bool else None
        return online, airborne, uav_state
