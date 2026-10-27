"""V1.4 跨仓交接契约：地面规划 Agent → 飞控 Agent。

本模块是**唯一**的交接规则真源。``lingxi-agent`` 的产出方与飞控侧的接收方
都调用这里，避免两仓各写一份白名单/几何规则后逐渐漂移。

它只做三件事：

1. 定义交接信封的字段白名单、版本与绑定要求；
2. 把信封转成飞控侧已有的 ``RuntimeBinding`` / ``FlightControlActionRequest``；
3. 把「平台受理」「已提交」「结果未知」「设备回执」分开表达。

它不读取网络、不查地图标注库、不复制任务规划状态机，也不创建第二个任务：
``task_dispatch`` 由任务下发链路负责，``flight_control`` 只消费已下发任务的绑定。
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any

from .full_control import FlightControlAction, FlightControlActionRequest, RuntimeBinding
from .target_resolver import FLIGHT_INTENTS, TargetGeometry

CONTRACT_VERSION = "lingxi-flight-handoff/v1"

# --- 稳定错误码 -------------------------------------------------------------
SCHEMA_UNSUPPORTED = "HANDOFF_SCHEMA_UNSUPPORTED"
FIELD_NOT_ALLOWED = "HANDOFF_FIELD_NOT_ALLOWED"
BINDING_INCOMPLETE = "HANDOFF_BINDING_INCOMPLETE"
BINDING_MISMATCH = "HANDOFF_BINDING_MISMATCH"
STALE_CONTEXT = "STALE_CONTEXT"
SNAPSHOT_STALE = "HANDOFF_SNAPSHOT_STALE"
IDEMPOTENCY_CONFLICT = "HANDOFF_IDEMPOTENCY_CONFLICT"
DRAFT_NOT_DISPATCHABLE = "HANDOFF_DRAFT_NOT_DISPATCHABLE"
TARGET_REQUIRED = "HANDOFF_TARGET_REQUIRED"
TARGET_GEOMETRY_UNSUPPORTED = "HANDOFF_TARGET_GEOMETRY_UNSUPPORTED"
MIXED_TARGET_UNSUPPORTED = "HANDOFF_MIXED_TARGET_UNSUPPORTED"
MULTI_GEOMETRY_UNSUPPORTED = "HANDOFF_MULTI_GEOMETRY_UNSUPPORTED"
TARGET_ORDER_INVALID = "HANDOFF_TARGET_ORDER_INVALID"
TARGET_AMBIGUOUS = "HANDOFF_TARGET_AMBIGUOUS"
TARGET_VERSION_UNAVAILABLE = "HANDOFF_TARGET_VERSION_UNAVAILABLE"
MAPPING_NOT_RESUMABLE = "MAPPING_NOT_RESUMABLE"
CROSS_TYPE_FORBIDDEN = "HANDOFF_CROSS_TYPE_FORBIDDEN"
ACTION_UNSUPPORTED = "HANDOFF_ACTION_UNSUPPORTED"
ALGORITHM_NOT_CONFIGURED = "HANDOFF_ALGORITHM_NOT_CONFIGURED"
PLATFORM_CAPABILITY_UNAVAILABLE = "HANDOFF_PLATFORM_CAPABILITY_UNAVAILABLE"

#: 契约错误 → 业务 HTTP 状态。409 只用于“同一次请求在旧上下文/旧草稿上重放”。
_HTTP_STATUS = MappingProxyType({
    SCHEMA_UNSUPPORTED: 400, FIELD_NOT_ALLOWED: 400, BINDING_INCOMPLETE: 400,
    BINDING_MISMATCH: 409, STALE_CONTEXT: 409, SNAPSHOT_STALE: 409,
    IDEMPOTENCY_CONFLICT: 409, DRAFT_NOT_DISPATCHABLE: 409,
    TARGET_REQUIRED: 422, TARGET_GEOMETRY_UNSUPPORTED: 422,
    MIXED_TARGET_UNSUPPORTED: 422, MULTI_GEOMETRY_UNSUPPORTED: 422,
    TARGET_ORDER_INVALID: 422, TARGET_AMBIGUOUS: 422,
    TARGET_VERSION_UNAVAILABLE: 422, MAPPING_NOT_RESUMABLE: 409,
    CROSS_TYPE_FORBIDDEN: 409, ACTION_UNSUPPORTED: 422,
    ALGORITHM_NOT_CONFIGURED: 422, PLATFORM_CAPABILITY_UNAVAILABLE: 503,
})

# --- 字段白名单 -------------------------------------------------------------
ENVELOPE_FIELDS = frozenset({
    "schema_version", "kind", "trace_id", "command_id", "idempotency_key",
    "binding", "intent", "task", "targets", "action", "algorithms", "draft",
})
BINDING_FIELDS = frozenset({
    "actor_id", "project_id", "site_id", "hangar_id", "drone_id",
    "task_id", "task_batch_id", "context_version", "snapshot_version",
})
TARGET_FIELDS = frozenset({
    "target_id", "name", "geometry_type", "longitude", "latitude", "altitude_m",
    "version", "source", "order", "project_id",
})
TASK_FIELDS = frozenset({
    "task_type", "route_status", "breakpoint_index", "temporary_task_id",
    "mapping_invalidated", "auto_stitching", "route_resume_disabled", "task_batch_id",
})
ACTION_FIELDS = frozenset({"name", "parameters"})
ALGORITHM_FIELDS = frozenset({"configured", "global", "annotation"})
DRAFT_FIELDS = frozenset({"confirmed", "dispatch_id", "revision"})

KINDS = frozenset({"task_dispatch", "flight_control"})
TASK_TYPES = frozenset({"inspection", "mapping", "orthophoto", "oblique", "high_precision_modeling"})
#: 测绘/建模类。巡检执行期间不得用自然语言新起这类任务（V1.4 4.1.2 跨类型限制）。
MAPPING_TASK_TYPES = frozenset({"mapping", "orthophoto", "oblique", "high_precision_modeling"})
#: 恢复类动作在测绘作废后必须拒绝，不允许“先试一下”。
RESUME_ACTIONS = frozenset({"route_resume", "continue_from_breakpoint"})

_IDENTIFIER = re.compile(r"[^\s]{1,128}")
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")


class HandoffError(ValueError):
    """只携带稳定错误码、字段名与 HTTP 状态，不携带上游原文或坐标。"""

    def __init__(self, code: str, *, fields: Sequence[str] = (), detail: str = ""):
        super().__init__(code)
        self.code = code
        self.fields = tuple(fields)
        self.detail = detail
        self.http_status = _HTTP_STATUS.get(code, 400)

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "http_status": self.http_status,
                "fields": list(self.fields), "detail": self.detail}


def http_status_for(code: str) -> int:
    return _HTTP_STATUS.get(code, 400)


class HandoffKind(str, Enum):
    TASK_DISPATCH = "task_dispatch"
    FLIGHT_CONTROL = "flight_control"


class DeliveryStage(str, Enum):
    """交付阶段。只有 ``DEVICE_RECEIPT`` 来自设备回执，其余都是平台/链路状态。"""

    DRAFT = "draft"
    ACCEPTED = "accepted"
    SUBMITTED = "submitted"
    UNKNOWN = "unknown"
    DEVICE_RECEIPT = "device_receipt"


#: 平台受理/已提交都不等于设备完成；这里保持与 ``command_receipts`` 一致的口径。
_STAGE_IS_DEVICE_EFFECT = MappingProxyType({
    DeliveryStage.DRAFT: False, DeliveryStage.ACCEPTED: False,
    DeliveryStage.SUBMITTED: False, DeliveryStage.UNKNOWN: False,
    DeliveryStage.DEVICE_RECEIPT: False,
})


def device_effect_verified(stage: DeliveryStage | str) -> bool:
    """任何交付阶段都不构成设备效果证明；设备回执只是设备自己的状态声明。"""
    try:
        return _STAGE_IS_DEVICE_EFFECT[DeliveryStage(stage)]
    except (TypeError, ValueError):
        return False


def delivery_stage(wire_status: Any) -> DeliveryStage:
    """线状态 → 交付阶段。未知线格式固定为 ``UNKNOWN``，不猜成成功。"""
    if wire_status in {"accepted", "already_accepted"}:
        return DeliveryStage.ACCEPTED
    if wire_status in {"submitted", "dispatching"}:
        return DeliveryStage.SUBMITTED
    if wire_status in {"completed", "failed", "cancelled", "expired", "device_receipt"}:
        return DeliveryStage.DEVICE_RECEIPT
    if wire_status == "draft" or wire_status == "preview_required":
        return DeliveryStage.DRAFT
    return DeliveryStage.UNKNOWN


# --- 解析后的结构 -----------------------------------------------------------
@dataclass(frozen=True)
class TargetRef:
    target_id: str
    name: str
    geometry_type: TargetGeometry
    longitude: float | None
    latitude: float | None
    altitude_m: float | None
    version: str
    source: str
    order: int
    project_id: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id, "name": self.name,
            "geometry_type": self.geometry_type.value,
            "longitude": self.longitude, "latitude": self.latitude,
            "altitude_m": self.altitude_m, "version": self.version,
            "source": self.source, "order": self.order, "project_id": self.project_id,
        }


@dataclass(frozen=True)
class TaskFacts:
    task_type: str
    route_status: str | None = None
    breakpoint_index: int | None = None
    temporary_task_id: str | None = None
    mapping_invalidated: bool = False
    auto_stitching: bool = False
    route_resume_disabled: bool = False
    task_batch_id: str | None = None


@dataclass(frozen=True)
class HandoffEnvelope:
    kind: HandoffKind
    trace_id: str
    command_id: str
    idempotency_key: str
    binding: Mapping[str, Any]
    intent: str
    task: TaskFacts
    targets: tuple[TargetRef, ...] = ()
    action: Mapping[str, Any] | None = None
    algorithms: Mapping[str, Any] = field(default_factory=dict)
    draft_confirmed: bool = False
    dispatch_id: str | None = None
    contract_version: str = CONTRACT_VERSION

    def creates_task(self) -> bool:
        """只有 ``task_dispatch`` 建立任务；飞行控制绝不隐式创建第二个任务。"""
        return self.kind is HandoffKind.TASK_DISPATCH

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version, "kind": self.kind.value,
            "trace_id": self.trace_id, "command_id": self.command_id,
            "idempotency_key": self.idempotency_key, "binding": dict(self.binding),
            "intent": self.intent,
            "task": {
                "task_type": self.task.task_type, "route_status": self.task.route_status,
                "breakpoint_index": self.task.breakpoint_index,
                "temporary_task_id": self.task.temporary_task_id,
                "mapping_invalidated": self.task.mapping_invalidated,
                "auto_stitching": self.task.auto_stitching,
                "route_resume_disabled": self.task.route_resume_disabled,
                "task_batch_id": self.task.task_batch_id,
            },
            "targets": [item.as_dict() for item in self.targets],
            "action": dict(self.action) if self.action else None,
            "algorithms": {key: (dict(value) if isinstance(value, Mapping) else list(value))
                           for key, value in self.algorithms.items()},
            "draft": {"confirmed": self.draft_confirmed, "dispatch_id": self.dispatch_id},
        }


# --- 校验工具 ---------------------------------------------------------------
def _reject(code: str, fields: Sequence[str] = (), detail: str = "") -> None:
    raise HandoffError(code, fields=fields, detail=detail)


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def _whitelist(value: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    extra = sorted(set(value) - allowed)
    if extra:
        _reject(FIELD_NOT_ALLOWED, extra, where)


def _mapping(value: Any, code: str, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _reject(code, (where,))
    return value


def _number(value: Any) -> bool:
    return type(value) in (int, float) and value == value and abs(value) != float("inf")


def _geometry(value: Any) -> TargetGeometry:
    if isinstance(value, TargetGeometry):
        return value
    try:
        return TargetGeometry(str(value))
    except (TypeError, ValueError):
        _reject(TARGET_GEOMETRY_UNSUPPORTED, ("geometry_type",))


def _parse_target(raw: Mapping[str, Any], *, index: int) -> TargetRef:
    _whitelist(raw, TARGET_FIELDS, f"targets[{index}]")
    target_id = raw.get("target_id")
    name = raw.get("name")
    if not _identifier(target_id) or not isinstance(name, str) or not name.strip():
        _reject(TARGET_REQUIRED, ("target_id", "name"))
    geometry = _geometry(raw.get("geometry_type"))
    version = raw.get("version")
    if not _identifier(version):
        # 没有版本就无法判断标注是否被改过，确认卡也无法失效。
        _reject(TARGET_VERSION_UNAVAILABLE, ("version",))
    source = raw.get("source")
    if not _identifier(source):
        _reject(TARGET_REQUIRED, ("source",))
    project_id = raw.get("project_id")
    if not _identifier(project_id):
        _reject(BINDING_INCOMPLETE, ("targets.project_id",))
    order = raw.get("order")
    if type(order) is not int or order < 1:
        _reject(TARGET_ORDER_INVALID, ("order",))
    longitude, latitude, altitude = raw.get("longitude"), raw.get("latitude"), raw.get("altitude_m")
    for value in (longitude, latitude, altitude):
        if value is not None and not _number(value):
            _reject(TARGET_GEOMETRY_UNSUPPORTED, ("coordinates",))
    return TargetRef(target_id, name.strip(), geometry, longitude, latitude, altitude,
                     version, source, order, project_id)


def _parse_targets(kind: HandoffKind, intent: str, raw_targets: Any) -> tuple[TargetRef, ...]:
    if not isinstance(raw_targets, Sequence) or isinstance(raw_targets, (str, bytes)):
        _reject(TARGET_REQUIRED, ("targets",))
    if not raw_targets:
        _reject(TARGET_REQUIRED, ("targets",))
    targets = tuple(_parse_target(_mapping(item, TARGET_REQUIRED, "targets"), index=index)
                    for index, item in enumerate(raw_targets))

    if any(item.geometry_type is TargetGeometry.VOLUME for item in targets):
        # V1.4 3.2.2：体目标拒绝，且体标注不参与 AI 配置。
        _reject(TARGET_GEOMETRY_UNSUPPORTED, ("targets.geometry_type",), "volume")
    geometries = {item.geometry_type for item in targets}
    if len(geometries) > 1:
        _reject(MIXED_TARGET_UNSUPPORTED, ("targets.geometry_type",))

    if kind is HandoffKind.FLIGHT_CONTROL:
        if intent not in FLIGHT_INTENTS:
            _reject(ACTION_UNSUPPORTED, ("intent",))
        if len(targets) > 1:
            _reject(MULTI_GEOMETRY_UNSUPPORTED, ("targets",), "flight-single-target")
        if targets[0].geometry_type is not TargetGeometry.POINT:
            # 线/面属于地面规划，绝不能进入飞行中的单目标指点接口。
            _reject(TARGET_GEOMETRY_UNSUPPORTED, ("targets.geometry_type",), "flight-point-only")
        return targets

    # 地面规划：点可多点串联，线/面单条单个，顺序必须与用户表述一致。
    if targets[0].geometry_type is TargetGeometry.POINT:
        orders = [item.order for item in targets]
        if orders != list(range(1, len(targets) + 1)):
            _reject(TARGET_ORDER_INVALID, ("order",), "points-must-keep-user-order")
        return targets
    if len(targets) > 1:
        _reject(MULTI_GEOMETRY_UNSUPPORTED, ("targets",), "single-line-or-polygon-only")
    if targets[0].order != 1:
        _reject(TARGET_ORDER_INVALID, ("order",))
    return targets


def _parse_task(raw: Any, *, kind: HandoffKind) -> TaskFacts:
    if raw is None:
        if kind is HandoffKind.TASK_DISPATCH:
            _reject(TARGET_REQUIRED, ("task",))
        return TaskFacts(task_type="")
    task = _mapping(raw, TARGET_REQUIRED, "task")
    _whitelist(task, TASK_FIELDS, "task")
    task_type = task.get("task_type")
    if task_type is not None and task_type not in TASK_TYPES:
        _reject(ACTION_UNSUPPORTED, ("task.task_type",))
    if kind is HandoffKind.TASK_DISPATCH and task_type is None:
        _reject(TARGET_REQUIRED, ("task.task_type",))
    route_status = task.get("route_status")
    if route_status is not None and not isinstance(route_status, str):
        _reject(ACTION_UNSUPPORTED, ("task.route_status",))
    breakpoint = task.get("breakpoint_index")
    if breakpoint is not None and (type(breakpoint) is not int or breakpoint < 0):
        _reject(ACTION_UNSUPPORTED, ("task.breakpoint_index",))
    flags = {}
    for key in ("mapping_invalidated", "auto_stitching", "route_resume_disabled"):
        value = task.get(key, False)
        if type(value) is not bool:
            _reject(ACTION_UNSUPPORTED, (f"task.{key}",))
        flags[key] = value
    temporary = task.get("temporary_task_id")
    if temporary is not None and not _identifier(temporary):
        _reject(ACTION_UNSUPPORTED, ("task.temporary_task_id",))
    batch = task.get("task_batch_id")
    if batch is not None and not _identifier(batch):
        _reject(BINDING_INCOMPLETE, ("task.task_batch_id",))
    return TaskFacts(task_type=task_type or "", route_status=route_status,
                     breakpoint_index=breakpoint, temporary_task_id=temporary,
                     task_batch_id=batch, **flags)


def _parse_algorithms(raw: Any, *, targets: Sequence[TargetRef]) -> Mapping[str, Any]:
    if raw is None:
        return MappingProxyType({})
    algorithms = _mapping(raw, ALGORITHM_NOT_CONFIGURED, "algorithms")
    _whitelist(algorithms, ALGORITHM_FIELDS, "algorithms")
    configured = algorithms.get("configured", [])
    global_set = algorithms.get("global", [])
    annotation = algorithms.get("annotation", {})
    for name, value in (("configured", configured), ("global", global_set)):
        if not isinstance(value, list) or any(not _identifier(item) for item in value):
            _reject(ALGORITHM_NOT_CONFIGURED, (f"algorithms.{name}",))
    if not isinstance(annotation, Mapping):
        _reject(ALGORITHM_NOT_CONFIGURED, ("algorithms.annotation",))
    known_targets = {item.target_id for item in targets}
    for annotation_id, items in annotation.items():
        if not _identifier(annotation_id) or annotation_id not in known_targets:
            # 只允许给本次交接涉及的目标配置算法，避免顺手改别人的标注。
            _reject(ALGORITHM_NOT_CONFIGURED, ("algorithms.annotation",))
        if not isinstance(items, list) or any(not _identifier(item) for item in items):
            _reject(ALGORITHM_NOT_CONFIGURED, ("algorithms.annotation",))
    requested = set(configured) | set(global_set)
    for items in annotation.values():
        requested |= set(items)
    if requested - set(configured):
        _reject(ALGORITHM_NOT_CONFIGURED, ("algorithms",), "algorithm-not-in-project-config")
    return MappingProxyType({"configured": list(configured), "global": list(global_set),
                             "annotation": {key: list(value) for key, value in annotation.items()}})


def validate_envelope(payload: Any, *, expected_binding: RuntimeBinding,
                      site_id: str, task_id: str | None,
                      current_task_type: str | None = None) -> HandoffEnvelope:
    """按白名单、版本、绑定、幂等与几何规则校验交接信封。

    ``expected_binding`` 是飞控 Agent 当前持有的运行上下文；信封必须落在同一
    绑定上，否则按 409 拒绝，而不是把旧草稿执行到新无人机上。
    ``current_task_type`` 是当前权威任务类型，用于拦截「巡检执行中新起测绘任务」。
    """
    if not isinstance(expected_binding, RuntimeBinding):
        _reject(BINDING_INCOMPLETE, ("expected_binding",))
    expected_binding.validate()
    envelope = _mapping(payload, SCHEMA_UNSUPPORTED, "envelope")
    _whitelist(envelope, ENVELOPE_FIELDS, "envelope")

    if envelope.get("schema_version") != CONTRACT_VERSION:
        _reject(SCHEMA_UNSUPPORTED, ("schema_version",))
    try:
        kind = HandoffKind(envelope.get("kind"))
    except (TypeError, ValueError):
        _reject(SCHEMA_UNSUPPORTED, ("kind",))

    trace_id, command_id, key = (envelope.get(name) for name in ("trace_id", "command_id", "idempotency_key"))
    for name, value in (("trace_id", trace_id), ("command_id", command_id), ("idempotency_key", key)):
        if not _identifier(value):
            _reject(BINDING_INCOMPLETE, (name,))

    binding = _mapping(envelope.get("binding"), BINDING_INCOMPLETE, "binding")
    _whitelist(binding, BINDING_FIELDS, "binding")
    missing = [name for name in ("actor_id", "project_id", "site_id", "hangar_id", "drone_id",
                                 "task_batch_id", "context_version", "snapshot_version")
               if name not in binding]
    if missing:
        _reject(BINDING_INCOMPLETE, missing)
    if binding.get("task_id") is not None and not _identifier(binding.get("task_id")):
        _reject(BINDING_INCOMPLETE, ("task_id",))
    for name in ("actor_id", "project_id", "site_id", "hangar_id", "drone_id", "task_batch_id"):
        if not _identifier(binding.get(name)):
            _reject(BINDING_INCOMPLETE, (name,))
    context_version = binding.get("context_version")
    if type(context_version) is not int or context_version < 0:
        _reject(BINDING_INCOMPLETE, ("context_version",))
    if not _identifier(binding.get("snapshot_version")):
        _reject(SNAPSHOT_STALE, ("snapshot_version",))

    expected = expected_binding.to_dict()
    for name in ("actor_id", "project_id", "hangar_id", "drone_id", "task_batch_id"):
        if binding[name] != expected[name]:
            _reject(BINDING_MISMATCH, (name,))
    if binding["site_id"] != site_id:
        _reject(BINDING_MISMATCH, ("site_id",))
    if (binding.get("task_id") or None) != (task_id or None):
        _reject(BINDING_MISMATCH, ("task_id",))
    if context_version != expected["context_version"]:
        # 业务 409：上下文已经换绑，旧信封不能在新绑定上执行。
        _reject(STALE_CONTEXT, ("context_version",))

    draft = envelope.get("draft")
    confirmed, dispatch_id = False, None
    if draft is not None:
        draft = _mapping(draft, DRAFT_NOT_DISPATCHABLE, "draft")
        _whitelist(draft, DRAFT_FIELDS, "draft")
        confirmed = draft.get("confirmed")
        if type(confirmed) is not bool:
            _reject(DRAFT_NOT_DISPATCHABLE, ("draft.confirmed",))
        dispatch_id = draft.get("dispatch_id")
        if dispatch_id is not None and not _identifier(dispatch_id):
            _reject(DRAFT_NOT_DISPATCHABLE, ("draft.dispatch_id",))
        revision = draft.get("revision")
        if revision is not None and (type(revision) is not int or revision < 0):
            _reject(DRAFT_NOT_DISPATCHABLE, ("draft.revision",))
    if kind is HandoffKind.TASK_DISPATCH and (not confirmed or not _identifier(dispatch_id)):
        # 规划 Agent 不得把未确认草稿当成已下发任务。
        _reject(DRAFT_NOT_DISPATCHABLE, ("draft",))

    intent = envelope.get("intent")
    if intent not in FLIGHT_INTENTS and intent != "ground_plan":
        _reject(ACTION_UNSUPPORTED, ("intent",))

    targets = _parse_targets(kind, intent, envelope.get("targets"))
    task = _parse_task(envelope.get("task"), kind=kind)
    if task.task_batch_id is not None and task.task_batch_id != expected["task_batch_id"]:
        _reject(BINDING_MISMATCH, ("task.task_batch_id",))
    if task.mapping_invalidated and kind is HandoffKind.TASK_DISPATCH:
        _reject(MAPPING_NOT_RESUMABLE, ("task.mapping_invalidated",))
    if current_task_type == "inspection" and task.task_type in MAPPING_TASK_TYPES:
        # V1.4 4.1.2：巡检执行期间禁止通过自然语言发起测绘类任务。
        _reject(CROSS_TYPE_FORBIDDEN, ("task.task_type",), "inspection-in-progress")

    action = envelope.get("action")
    if kind is HandoffKind.FLIGHT_CONTROL:
        action = _mapping(action, ACTION_UNSUPPORTED, "action")
        _whitelist(action, ACTION_FIELDS, "action")
        name = action.get("name")
        try:
            FlightControlAction(name)
        except (TypeError, ValueError):
            _reject(ACTION_UNSUPPORTED, ("action.name",))
        if name in RESUME_ACTIONS and (task.mapping_invalidated or task.route_resume_disabled):
            _reject(MAPPING_NOT_RESUMABLE, ("action.name",))
        parameters = action.get("parameters", {})
        if not isinstance(parameters, Mapping):
            _reject(ACTION_UNSUPPORTED, ("action.parameters",))
        # 复用既有参数白名单与数值范围，避免这里长出第二套参数规则。
        try:
            FlightControlActionRequest(command_id, key, name, dict(parameters), context_version).validate()
        except ValueError:
            _reject(ACTION_UNSUPPORTED, ("action.parameters",))
        action = MappingProxyType({"name": name, "parameters": dict(parameters)})
    elif action is not None:
        _reject(FIELD_NOT_ALLOWED, ("action",), "task_dispatch-must-not-carry-flight-action")

    algorithms = _parse_algorithms(envelope.get("algorithms"), targets=targets)

    return HandoffEnvelope(kind=kind, trace_id=trace_id, command_id=command_id,
                           idempotency_key=key, binding=MappingProxyType(dict(binding)),
                           intent=intent, task=task, targets=targets, action=action,
                           algorithms=algorithms, draft_confirmed=confirmed,
                           dispatch_id=dispatch_id)


def to_runtime_binding(envelope: HandoffEnvelope) -> RuntimeBinding:
    """信封绑定 → 飞控运行绑定。``task_batch_id`` 来自规划侧，不在这里新生成。"""
    binding = envelope.binding
    return RuntimeBinding(actor_id=binding["actor_id"], project_id=binding["project_id"],
                          hangar_id=binding["hangar_id"], drone_id=binding["drone_id"],
                          task_batch_id=binding["task_batch_id"],
                          context_version=binding["context_version"])


def to_action_request(envelope: HandoffEnvelope) -> FlightControlActionRequest:
    """只有 ``flight_control`` 能转成飞控动作；任务下发不产生飞控指令。"""
    if envelope.kind is not HandoffKind.FLIGHT_CONTROL or envelope.action is None:
        _reject(ACTION_UNSUPPORTED, ("kind",), "not-a-flight-control-handoff")
    return FlightControlActionRequest(envelope.command_id, envelope.idempotency_key,
                                      envelope.action["name"], dict(envelope.action["parameters"]),
                                      envelope.binding["context_version"])


def verify_snapshot(envelope: HandoffEnvelope, snapshot: Any) -> None:
    """交接信封声明的快照版本必须就是执行器当前读到的权威快照。"""
    observed = getattr(snapshot, "version", None)
    if observed != envelope.binding["snapshot_version"]:
        _reject(SNAPSHOT_STALE, ("snapshot_version",))
    if getattr(snapshot, "context_version", None) != envelope.binding["context_version"]:
        _reject(STALE_CONTEXT, ("context_version",))


# --- 幂等登记 ---------------------------------------------------------------
class HandoffLedger:
    """进程内幂等登记：同键同指纹幂等，同键不同指纹冲突。

    只记录 ``command_id``/``idempotency_key`` 与请求指纹，不保存坐标、原文或凭据。
    """

    def __init__(self) -> None:
        self._seen: dict[tuple[str, str], str] = {}

    @staticmethod
    def _fingerprint(envelope: HandoffEnvelope) -> str:
        import hashlib
        import json
        payload = {key: value for key, value in envelope.as_dict().items()
                   if key not in {"trace_id", "command_id", "idempotency_key"}}
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def admit(self, envelope: HandoffEnvelope) -> str:
        """返回 ``new`` / ``duplicate``；同键不同内容直接拒绝。"""
        scope = (envelope.command_id, envelope.idempotency_key)
        fingerprint = self._fingerprint(envelope)
        previous = self._seen.get(scope)
        if previous is None:
            self._seen[scope] = fingerprint
            return "new"
        if previous != fingerprint:
            _reject(IDEMPOTENCY_CONFLICT, ("idempotency_key",))
        return "duplicate"


# --- Java 侧显式 unavailable 契约 -------------------------------------------
#: Java 在快照顶层 ``contract.unavailable`` 中声明它没有的能力；值必须是稳定错误码。
#: 这是“明确不可用”，不是“字段恰好为空”。
CONTRACT_BLOCK = "contract"
UNAVAILABLE_SCHEMA = "lingxi-platform-contract/v1"
#: 能力 → 受影响动作。Python 用这张表把 Java 的声明变成具体动作的 typed 拒绝。
CAPABILITY_ACTIONS = MappingProxyType({
    # 微操依赖航线状态（V1.4 2.2.3）；指点/环绕的缺口由 target_binding 单独声明，
    # 不在这里重复，避免同一动作出现两个“不可用”原因。
    "route_status": ("route_pause", "route_resume", "hover", "continue_from_breakpoint",
                     "move_forward", "move_backward", "move_left", "move_right",
                     "move_up", "move_down", "turn_left", "turn_right"),
    "target_binding": ("point_flight", "point_orbit"),
    "algorithm_lifecycle": ("ai_algorithm_set",),
    "mapping_transaction": ("route_resume", "continue_from_breakpoint"),
    "device_receipt": (),
})


def parse_platform_unavailable(data: Mapping[str, Any]) -> Mapping[str, str]:
    """读取并校验 Java 的显式不可用声明；格式非法即拒绝整份快照。"""
    block = data.get(CONTRACT_BLOCK)
    if block is None:
        # 返回普通 dict：快照会被 copy.deepcopy，MappingProxyType 不能深拷贝。
        return {}
    if not isinstance(block, Mapping) or set(block) - {"schema_version", "unavailable"}:
        _reject(SCHEMA_UNSUPPORTED, ("contract",))
    if block.get("schema_version") != UNAVAILABLE_SCHEMA:
        _reject(SCHEMA_UNSUPPORTED, ("contract.schema_version",))
    unavailable = block.get("unavailable")
    if not isinstance(unavailable, Mapping):
        _reject(SCHEMA_UNSUPPORTED, ("contract.unavailable",))
    for capability, code in unavailable.items():
        # 新增能力名不阻断整份快照：那只是“本端还不认识的一条缺口”，
        # 本端自己的权威字段校验仍然照常执行。错误码格式必须合法。
        if not isinstance(capability, str) or not capability:
            _reject(SCHEMA_UNSUPPORTED, ("contract.unavailable",))
        if not isinstance(code, str) or _CODE.fullmatch(code) is None:
            _reject(SCHEMA_UNSUPPORTED, ("contract.unavailable",), str(capability))
    return dict(unavailable)


def unavailable_code_for(action: str, unavailable: Mapping[str, str]) -> str | None:
    """动作 → Java 声明的不可用错误码；没有声明返回 None。"""
    for capability, actions in CAPABILITY_ACTIONS.items():
        if action in actions and capability in unavailable:
            return unavailable[capability]
    return None
