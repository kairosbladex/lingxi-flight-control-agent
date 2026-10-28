"""V1.4 权威快照的 typed 字段契约边界。

本模块只描述“服务端权威快照必须证明什么”，不读取网络、不构造快照、不授予写权限。
它把 V1.4 要求的字段分组显式化，使调用方可以区分：

- ``present``：字段存在且类型合法；
- ``unknown``：字段存在但值为 ``None``，即平台没有给出真值；
- ``absent``：字段根本不存在（例如旧 v1 快照没有 ``route_id``）；
- ``unbound``：字段属于 Agent 会话而非平台投影（``task_batch_id``）。

``unknown`` 与 ``absent`` 都不是安全值。需要该字段的动作必须显式失败，
错误码固定为 ``AUTHORITATIVE_STATE_INCOMPLETE``，不退回默认高度、默认航线状态或
“看起来没在飞所以可以动”。
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

CONTRACT_VERSION = "lingxi-authority-contract/v1"

#: 字段缺失/未知时的稳定错误码。
INCOMPLETE = "AUTHORITATIVE_STATE_INCOMPLETE"
INVALID = "AUTHORITY_FIELD_INVALID"
BINDING_INCOMPLETE = "AUTHORITY_BINDING_INCOMPLETE"

FIELD_GROUPS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    # 身份与绑定
    "identity": ("project_id", "actor_id", "site_id", "hangar_id", "drone_id", "task_batch_id"),
    "version": ("snapshot_version", "context_version"),
    "control": ("control_owner", "control_lease_expires_at"),
    # 飞行事实
    "flight": ("online", "airborne", "flight_status", "route_status"),
    "task": ("task_type",),
    "route": ("route_id", "breakpoint_index"),
    "mapping": ("mapping_invalidated", "auto_stitching"),
    "temporary": ("temporary_task_id",),
    "battery": ("low_battery_return",),
    "pose": ("longitude", "latitude", "altitude_m", "yaw_deg"),
    "device": ("device_limits",),
    "capability": ("permissions", "capabilities", "action_availability"),
})

#: 快照 ``state`` 里允许出现的可选策略字段（与 ktd_snapshot 的解析白名单一致）。
STATE_POLICY_FIELDS = frozenset({
    "task_type", "mapping_invalidated", "auto_stitching", "low_battery_return",
    "device_limits", "gimbal_pitch_deg", "breakpoint_index", "temporary_task_id",
    "task_scope", "route_id",
})

TASK_TYPES = frozenset({"inspection", "mapping", "orthophoto", "oblique", "high_precision_modeling"})

#: 明确“在地面即可执行”的动作，不要求空中与位姿。
GROUND_ACTIONS = frozenset({
    "read_state", "control_acquire", "control_release", "remote_request",
    "remote_approve", "remote_reject", "set_keyboard_mode", "set_joystick_mode",
    "set_virtual_button", "upload_route", "takeoff",
})

#: 需要“已知在空中 + 有效位姿”的动作。
AIRBORNE_POSE_ACTIONS = frozenset({
    "move_forward", "move_backward", "move_left", "move_right", "move_up", "move_down",
    "turn_left", "turn_right", "hover", "emergency_stop", "land", "land_alternate",
    "return_home", "cancel_return_home", "route_pause", "route_resume",
    "continue_from_breakpoint", "point_flight", "point_orbit", "execute_route",
    "point_flight_stop", "point_orbit_stop",
})

#: 依赖航线事实的动作：没有 route_status / route_id 时不得猜测“可以恢复/暂停”。
ROUTE_FACT_ACTIONS = frozenset({
    "route_pause", "route_resume", "continue_from_breakpoint", "execute_route", "hover",
})

#: 受“自动航线执行中”门禁约束的飞行动作（V1.4 2.2.3）。
#: 不知道航线状态就无法证明“当前不在自动航线中”，因此同样 fail-closed。
ROUTE_GATED_ACTIONS = frozenset({
    "move_forward", "move_backward", "move_left", "move_right", "move_up", "move_down",
    "turn_left", "turn_right", "point_flight", "point_orbit",
})

#: 依赖测绘作废状态的动作：不知道是否作废时不能允许恢复。
MAPPING_FACT_ACTIONS = frozenset({"route_resume", "continue_from_breakpoint"})

#: 依赖设备参数表的动作：没有参数表不得套用默认极值。
DEVICE_LIMIT_ACTIONS = frozenset({
    "camera_zoom", "gimbal_digital_zoom", "set_move_speed", "set_vertical_speed",
    "set_roll_speed", "set_pitch_speed", "set_yaw_speed", "speaker_volume",
    "searchlight_brightness", "gimbal_up", "gimbal_down",
})


class AuthorityContractError(ValueError):
    """只携带稳定错误码和缺失字段名，不携带上游原文。"""

    def __init__(self, code: str, fields: tuple[str, ...] = ()):
        super().__init__(code)
        self.code = code
        self.fields = tuple(fields)


def _is_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _finite(value: Any, low: float, high: float) -> bool:
    return _is_number(value) and low <= value <= high


@dataclass(frozen=True)
class FieldStatus:
    name: str
    status: str  # present / unknown / absent / unbound / invalid
    detail: str = ""


@dataclass(frozen=True)
class AuthorityAudit:
    """一次快照的字段级判定结果；不缓存、不参与授权。"""

    version: str
    fields: Mapping[str, FieldStatus] = field(default_factory=dict)

    def status(self, name: str) -> str:
        entry = self.fields.get(name)
        return entry.status if entry else "absent"

    def missing(self, names: tuple[str, ...] | frozenset[str]) -> tuple[str, ...]:
        return tuple(name for name in names if self.status(name) in {"unknown", "absent", "invalid"})

    def unknown(self, names: tuple[str, ...] | frozenset[str]) -> tuple[str, ...]:
        """字段存在但为 null（或类型非法）：平台明确说“不知道”。"""
        return tuple(name for name in names if self.status(name) in {"unknown", "invalid"})

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.version,
            "fields": {name: entry.status for name, entry in self.fields.items()},
            # unbound 表示该事实来自 Agent 会话而非平台投影，同样不能算“平台已证明”。
            "incomplete": sorted(name for name, entry in self.fields.items()
                                 if entry.status in {"unknown", "absent", "invalid", "unbound"}),
        }


def _state_field(state: Mapping[str, Any], name: str) -> tuple[str, Any]:
    if name in state:
        value = state[name]
        return ("unknown" if value is None else "present"), value
    return "absent", None


def audit_snapshot(snapshot: Any) -> AuthorityAudit:
    """按 V1.4 字段分组审计一个 ``KtdSnapshot`` 形状的对象。

    只读取属性，不校验授权，不改变快照。用于测试、排障与“为什么拒绝”的可解释回执。
    """
    state = getattr(snapshot, "state", None)
    if not isinstance(state, Mapping):
        raise AuthorityContractError(INVALID, ("state",))
    fields: dict[str, FieldStatus] = {}

    def record(name: str, status: str, detail: str = "") -> None:
        fields[name] = FieldStatus(name, status, detail)

    # identity
    for name in ("project_id", "actor_id", "site_id", "hangar_id", "drone_id"):
        value = getattr(snapshot, name, None)
        record(name, "present" if isinstance(value, str) and value.strip() else "absent")
    # 平台不返回 Agent 批次时，只能标记为 Agent 侧绑定，不能冒充平台投影。
    batch = getattr(snapshot, "task_batch_id", None)
    record("task_batch_id", "present" if isinstance(batch, str) and batch.strip() else "unbound")

    version = getattr(snapshot, "version", None)
    record("snapshot_version", "present" if isinstance(version, (str, int)) and version != "" else "absent")
    context = getattr(snapshot, "context_version", None)
    record("context_version", "present" if type(context) is int and context >= 0 else "absent")

    owner = getattr(snapshot, "control_owner", None)
    record("control_owner", "present" if isinstance(owner, str) and owner.strip() else "unknown")
    lease = getattr(snapshot, "control_lease_expires_at", None)
    record("control_lease_expires_at", "present" if _finite(lease, 0, float("inf")) else "unknown")

    for name in ("online", "airborne", "flight_status", "route_status"):
        status, _ = _state_field(state, name)
        record(name, status)

    for name in ("task_type", "route_id", "breakpoint_index", "temporary_task_id",
                 "mapping_invalidated", "auto_stitching", "low_battery_return", "device_limits"):
        status, _ = _state_field(state, name)
        record(name, status)

    # 位姿：DJI 用 altitude_m，PZ 用 height_m，缺哪个都要显式未知。
    for name in ("longitude", "latitude", "yaw_deg"):
        status, _ = _state_field(state, name)
        record(name, status)
    altitude = "present" if _finite(state.get("altitude_m"), -500, 10000) else "unknown"
    if state.get("height_m") is not None:
        altitude = "present" if _finite(state.get("height_m"), -500, 10000) else altitude
    record("altitude_m", altitude)

    permissions = getattr(snapshot, "permissions", None)
    record("permissions", "present" if permissions is not None else "absent")
    capabilities = getattr(snapshot, "capabilities", None)
    record("capabilities", "present" if isinstance(capabilities, Mapping) and capabilities else "absent")
    availability = getattr(snapshot, "action_availability", None)
    record("action_availability", "present" if isinstance(availability, Mapping) and availability else "absent")

    return AuthorityAudit(CONTRACT_VERSION, fields)


@dataclass(frozen=True)
class Requirement:
    """``core`` 是旧 v1 快照已经声称返回的字段，缺失即拒绝。

    ``extension`` 是 V1.4 新字段。它们尚未被证明由平台返回，因此只在快照
    **已经携带该字段却给了 null**（自相矛盾）时拒绝；字段整体缺失按
    “该平台没有这个能力”处理，由执行器现有的状态检查决定可否继续。
    """

    core: tuple[str, ...] = ()
    extension: tuple[str, ...] = ()


def requirements_for(action: str) -> Requirement:
    if not isinstance(action, str) or not action:
        raise AuthorityContractError(INVALID, ("action",))
    if action == "read_state":
        # 只读展示是观测路径，不是执行路径：设备离线时也要能显示“状态未知”，
        # 因此不要求任何字段，也不授予任何写权限。
        return Requirement()
    if action in GROUND_ACTIONS:
        return Requirement(("online",))
    if action in AIRBORNE_POSE_ACTIONS:
        core = ("online", "airborne", "flight_status")
        extension: tuple[str, ...] = ()
        if action in ROUTE_FACT_ACTIONS:
            core += ("route_status",)
            extension += ("route_id",)
        elif action in ROUTE_GATED_ACTIONS:
            # V1.4 2.2.3：微操/指点/环绕是否放行取决于航线状态。
            # 平台把 route_status 留空时不能当成“没在自动航线”，一律拒绝。
            core += ("route_status",)
        if action in MAPPING_FACT_ACTIONS:
            extension += ("mapping_invalidated",)
        return Requirement(core, extension)
    if action in DEVICE_LIMIT_ACTIONS:
        return Requirement(("online",), ("device_limits",))
    return Requirement(("online",))


def require_authority(snapshot: Any, action: str, *, quality: str | None = None) -> AuthorityAudit:
    """校验动作所需的权威字段；``unknown`` 一律 fail-closed。

    判定口径：

    - ``unknown``（字段在但为 null）→ 拒绝 ``AUTHORITATIVE_STATE_INCOMPLETE``。
      平台明确说“不知道”时，不能当成安全或可执行。
    - ``absent``（字段根本不在快照里）→ 不在这里拒绝。旧 v1 快照本来就没有
      ``route_id``/``task_type`` 等字段，是否允许该动作由执行器的状态检查决定
      （例如 ``ROUTE_STATE_UNAVAILABLE``）；provider 边界另以
      ``AUTHORITATIVE_STATE_INCOMPLETE`` 拒绝缺少基础 state 键的响应。
    - ``quality='legacy-read-api'``：旧只读接口拼装的测试授权本来就不提供
      ``flight_status``/``route_status``/``mapping_invalidated``，这些字段只作为
      缺失记录，不升级成“已验证”，也不冒充权威拒绝原因。

    这里不判断“值是否允许该动作”（例如 route_status 必须为 running），
    那是执行器编译阶段的事；本函数只保证“平台没有把已声明的字段留空”。
    """
    audit = audit_snapshot(snapshot)
    requirement = requirements_for(action)
    core, extension = requirement.core, requirement.extension
    if (quality or getattr(snapshot, "authorization_quality", "authoritative")) == "legacy-read-api":
        legacy_unverifiable = {"flight_status", "route_status", "route_id", "mapping_invalidated"}
        core = tuple(name for name in core if name not in legacy_unverifiable)
        extension = tuple(name for name in extension if name not in legacy_unverifiable)
    unknown = audit.unknown(core + extension)
    if unknown:
        raise AuthorityContractError(INCOMPLETE, unknown)
    return audit


def require_binding(binding: Any, *, site_id: str, task_id: str | None) -> None:
    """身份字段必须齐全；不构造默认值。"""
    missing = []
    for name in ("actor_id", "project_id", "hangar_id", "drone_id", "task_batch_id"):
        value = getattr(binding, name, None)
        if not isinstance(value, str) or not value.strip():
            missing.append(name)
    if not isinstance(site_id, str) or not site_id.strip():
        missing.append("site_id")
    if task_id is not None and (not isinstance(task_id, str) or not task_id.strip()):
        missing.append("task_id")
    if missing:
        raise AuthorityContractError(BINDING_INCOMPLETE, tuple(missing))
