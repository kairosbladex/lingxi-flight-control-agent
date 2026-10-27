"""V1.4 静态标注 AI 算法生命周期：typed 契约、可注入网关与确定性选择规则。

设计边界（与 AGENTS.md 一致）：

- 本模块**不识别图像**，也不包含任何模型推理；它只描述「哪个算法应当在哪个
  标注上运行」以及「平台是否确认它真的在运行」。
- 六个阶段必须分开记录，不能互相冒充：

  1. ``expected``          标注配置的期望值（来自地图标注库）
  2. ``dispatched``        任务下发时写入任务 AI 配置的算法集合
  3. ``start_requested``   到达标注后发出的算法启动请求
  4. ``started``           平台确认算法已启动的真实回执
  5. ``stop_requested``    离开标注后发出的算法停止请求
  6. ``stopped``           平台确认算法已停止的真实回执

  只有 ``started`` 才允许把算法展示为「运行中」。请求被受理不等于运行中，
  配置写入也不等于运行中。

- 真实平台若没有算法粒度的启停接口，网关返回
  ``ALGORITHM_LIFECYCLE_UNAVAILABLE``，执行器必须如实拒绝，不得伪造运行成功。
  模拟器提供完整测试 seam，但每条回执都带 ``execution_mode=simulator``。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Protocol, Sequence, runtime_checkable

CONTRACT_VERSION = "lingxi-ai-lifecycle/v1"

#: 稳定错误码
ALGORITHM_LIFECYCLE_UNAVAILABLE = "ALGORITHM_LIFECYCLE_UNAVAILABLE"
ANNOTATION_ALGORITHM_SOURCE_UNAVAILABLE = "ANNOTATION_ALGORITHM_SOURCE_UNAVAILABLE"
ALGORITHM_NOT_CONFIGURED = "ALGORITHM_NOT_CONFIGURED"
ALGORITHM_CONFLICT_CLARIFICATION_REQUIRED = "ALGORITHM_CONFLICT_CLARIFICATION_REQUIRED"
INVALID_ALGORITHM_SELECTION = "INVALID_ALGORITHM_SELECTION"
BODY_ANNOTATION_AI_UNSUPPORTED = "BODY_ANNOTATION_AI_UNSUPPORTED"
UNKNOWN_ANNOTATION = "UNKNOWN_ANNOTATION"
ANNOTATION_BINDING_MISMATCH = "ANNOTATION_BINDING_MISMATCH"

#: V1.4 3.3 冲突追问的固定话术。
CONFLICT_QUESTION = "是同时开启还是仅替换？"
CONFLICT_MESSAGE = f"该AI与当前标注已配置的AI冲突：{CONFLICT_QUESTION}"


class AlgorithmError(ValueError):
    """带稳定错误码的算法契约错误；调用方按 code 分支，不解析文案。"""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


class AnnotationGeometry(str, Enum):
    POINT = "point"
    LINE = "line"
    FACE = "face"
    BODY = "body"


class AlgorithmStage(str, Enum):
    """V1.4 2.1.2 生命周期六阶段；顺序即时间顺序。"""

    EXPECTED = "expected"
    DISPATCHED = "dispatched"
    START_REQUESTED = "start_requested"
    STARTED = "started"
    STOP_REQUESTED = "stop_requested"
    STOPPED = "stopped"


#: 允许绑定 AI 的几何类型；体标注在前后端都必须拒绝。
AI_BINDABLE_GEOMETRIES = frozenset({
    AnnotationGeometry.POINT, AnnotationGeometry.LINE, AnnotationGeometry.FACE,
})


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 128 and value.strip() == value


def _coerce_geometry(value: Any) -> AnnotationGeometry:
    if isinstance(value, AnnotationGeometry):
        return value
    if isinstance(value, str):
        try:
            return AnnotationGeometry(value.strip().lower())
        except ValueError:
            pass
    raise AlgorithmError(INVALID_ALGORITHM_SELECTION, "标注几何类型不合法。")


def normalize_algorithms(values: Iterable[Any], *, configured: Sequence[str] | None = None,
                         allow_empty: bool = True) -> tuple[str, ...]:
    """去重并保序；未知算法整批拒绝，绝不静默忽略或替换成近似算法。"""
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise AlgorithmError(INVALID_ALGORITHM_SELECTION, "算法集合必须是字符串列表。")
    ordered: list[str] = []
    for value in values:
        if not _identifier(value):
            raise AlgorithmError(INVALID_ALGORITHM_SELECTION, "算法标识不合法。")
        if value not in ordered:
            ordered.append(value)
    if configured is not None:
        allowed = {str(item) for item in configured}
        unknown = [item for item in ordered if item not in allowed]
        if unknown:
            # 不挑近似算法、不丢弃未知项：整批拒绝，调用方必须重新澄清。
            raise AlgorithmError(ALGORITHM_NOT_CONFIGURED, "当前项目未配置该AI算法。")
    if not ordered and not allow_empty:
        raise AlgorithmError(INVALID_ALGORITHM_SELECTION, "未选择任何算法。")
    return tuple(ordered)


@dataclass(frozen=True)
class AnnotationAlgorithmConfig:
    """阶段 1：地图标注库里配置的期望值（只读投影）。"""

    annotation_id: str
    geometry: AnnotationGeometry
    algorithms: tuple[str, ...] = ()
    version: int = 0
    project_id: str | None = None

    def validate(self, *, configured: Sequence[str] | None = None) -> None:
        if not _identifier(self.annotation_id):
            raise AlgorithmError(INVALID_ALGORITHM_SELECTION, "标注标识不合法。")
        if self.geometry not in AI_BINDABLE_GEOMETRIES:
            # V1.4 2.1.1 / 3.3：体标注不参与 AI 绑定，前后端都必须拒绝。
            raise AlgorithmError(BODY_ANNOTATION_AI_UNSUPPORTED, "体标注不支持AI配置。")
        if type(self.version) is not int or self.version < 0:
            raise AlgorithmError(INVALID_ALGORITHM_SELECTION, "标注版本不合法。")
        normalize_algorithms(self.algorithms, configured=configured)

    def to_dict(self) -> dict[str, Any]:
        return {"annotation_id": self.annotation_id, "geometry": self.geometry.value,
                "algorithms": list(self.algorithms), "version": self.version,
                "project_id": self.project_id}


@dataclass(frozen=True)
class AlgorithmLifecycleReceipt:
    """一次生命周期调用的结果；``confirmed`` 为真才代表平台已确认该阶段。"""

    stage: AlgorithmStage
    annotation_id: str
    algorithms: tuple[str, ...]
    execution_mode: str
    confirmed: bool
    request_id: str = ""
    platform_receipt_id: str | None = None
    error_code: str | None = None
    message: str = ""
    contract_version: str = CONTRACT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage.value,
            "annotation_id": self.annotation_id,
            "algorithms": list(self.algorithms),
            "execution_mode": self.execution_mode,
            "confirmed": self.confirmed,
            "request_id": self.request_id,
            "platform_receipt_id": self.platform_receipt_id,
            "error_code": self.error_code,
            "message": self.message,
            "contract_version": self.contract_version,
        }


@runtime_checkable
class AnnotationAlgorithmGateway(Protocol):
    """读取标注配置期望值（阶段 1）的只读边界。"""

    def expected(self, binding: Any, annotation_id: str) -> AnnotationAlgorithmConfig:
        ...


class UnavailableAnnotationAlgorithmGateway:
    """默认实现：没有可核验的地图标注接口时显式不可用，不返回空配置。"""

    def expected(self, binding: Any, annotation_id: str) -> AnnotationAlgorithmConfig:
        raise AlgorithmError(ANNOTATION_ALGORITHM_SOURCE_UNAVAILABLE,
                             "未接入静态标注算法配置服务，无法读取标注AI期望值。")


@runtime_checkable
class AlgorithmLifecycleGateway(Protocol):
    """算法粒度的启停边界（阶段 3–6）。"""

    execution_mode: str

    def start(self, binding: Any, annotation_id: str, algorithms: Sequence[str], *,
              request_id: str = "") -> AlgorithmLifecycleReceipt:
        ...

    def stop(self, binding: Any, annotation_id: str, algorithms: Sequence[str], *,
             request_id: str = "") -> AlgorithmLifecycleReceipt:
        ...


class UnavailableAlgorithmLifecycleGateway:
    """默认实现：平台没有算法级启停接口时，任何启停都返回 typed unavailable。"""

    execution_mode = "unavailable"

    def _receipt(self, stage: AlgorithmStage, annotation_id: str, algorithms: Sequence[str],
                 request_id: str) -> AlgorithmLifecycleReceipt:
        return AlgorithmLifecycleReceipt(
            stage=stage, annotation_id=annotation_id, algorithms=tuple(algorithms),
            execution_mode=self.execution_mode, confirmed=False, request_id=request_id,
            error_code=ALGORITHM_LIFECYCLE_UNAVAILABLE,
            message="平台尚未提供算法粒度的启动/停止接口，不能把配置或总开关当作算法已运行。")

    def start(self, binding: Any, annotation_id: str, algorithms: Sequence[str], *,
              request_id: str = "") -> AlgorithmLifecycleReceipt:
        return self._receipt(AlgorithmStage.START_REQUESTED, annotation_id, algorithms, request_id)

    def stop(self, binding: Any, annotation_id: str, algorithms: Sequence[str], *,
             request_id: str = "") -> AlgorithmLifecycleReceipt:
        return self._receipt(AlgorithmStage.STOP_REQUESTED, annotation_id, algorithms, request_id)


class SimulatorAlgorithmLifecycleGateway:
    """模拟器 seam：完整六阶段，但每条回执都标 ``execution_mode=simulator``。

    它只维护进程内的「哪些标注上哪些算法在运行」，不做任何图像识别，也不代表
    真实平台会这样响应。
    """

    execution_mode = "simulator"

    def __init__(self) -> None:
        self._running: dict[str, list[str]] = {}
        self._counter = 0
        self.receipts: list[AlgorithmLifecycleReceipt] = []

    def _next_id(self, stage: AlgorithmStage) -> str:
        self._counter += 1
        return f"sim-{stage.value}-{self._counter}"

    def start(self, binding: Any, annotation_id: str, algorithms: Sequence[str], *,
              request_id: str = "") -> AlgorithmLifecycleReceipt:
        if not _identifier(annotation_id):
            raise AlgorithmError(INVALID_ALGORITHM_SELECTION, "标注标识不合法。")
        ordered = normalize_algorithms(algorithms)
        current = self._running.setdefault(annotation_id, [])
        for item in ordered:
            if item not in current:
                current.append(item)
        receipt = AlgorithmLifecycleReceipt(
            stage=AlgorithmStage.STARTED, annotation_id=annotation_id, algorithms=ordered,
            execution_mode=self.execution_mode, confirmed=True, request_id=request_id,
            platform_receipt_id=self._next_id(AlgorithmStage.STARTED),
            message="模拟器已记录算法启动；不代表真实平台运行状态。")
        self.receipts.append(receipt)
        return receipt

    def stop(self, binding: Any, annotation_id: str, algorithms: Sequence[str], *,
             request_id: str = "") -> AlgorithmLifecycleReceipt:
        if not _identifier(annotation_id):
            raise AlgorithmError(INVALID_ALGORITHM_SELECTION, "标注标识不合法。")
        ordered = normalize_algorithms(algorithms)
        current = self._running.get(annotation_id, [])
        remaining = [item for item in current if item not in set(ordered)]
        if remaining:
            self._running[annotation_id] = remaining
        else:
            self._running.pop(annotation_id, None)
        receipt = AlgorithmLifecycleReceipt(
            stage=AlgorithmStage.STOPPED, annotation_id=annotation_id, algorithms=ordered,
            execution_mode=self.execution_mode, confirmed=True, request_id=request_id,
            platform_receipt_id=self._next_id(AlgorithmStage.STOPPED),
            message="模拟器已记录算法停止；不代表真实平台运行状态。")
        self.receipts.append(receipt)
        return receipt

    def running(self, annotation_id: str) -> tuple[str, ...]:
        return tuple(self._running.get(annotation_id, ()))

    def snapshot(self) -> dict[str, Any]:
        return {"contract_version": CONTRACT_VERSION, "execution_mode": self.execution_mode,
                "running": {key: list(value) for key, value in sorted(self._running.items())},
                "receipts": len(self.receipts)}


# ---------------------------------------------------------------------------
# 确定性集合运算：全局与标注去重、离开只移除本标注来源
# ---------------------------------------------------------------------------


def annotation_active_set(global_algorithms: Iterable[str],
                          active_annotations: Iterable[str],
                          annotation_algorithms: Mapping[str, Sequence[str]],
                          configured: Sequence[str] | None = None,
                          suppressed: Iterable[str] = ()) -> tuple[str, ...]:
    """当前应当处于运行状态的算法集合（去重，保序，限定在项目已配置集合内）。"""
    blocked = {str(item) for item in suppressed}
    ordered: list[str] = []
    for item in global_algorithms:
        if item not in ordered:
            ordered.append(item)
    for annotation in active_annotations:
        for item in annotation_algorithms.get(annotation, ()):
            if item not in ordered:
                ordered.append(item)
    if configured is not None:
        allowed = {str(item) for item in configured}
        ordered = [item for item in ordered if item in allowed]
    return tuple(item for item in ordered if item not in blocked)


def algorithms_to_stop(annotation_id: str, *, annotation_algorithms: Mapping[str, Sequence[str]],
                       global_algorithms: Iterable[str],
                       active_annotations: Iterable[str],
                       configured: Sequence[str] | None = None) -> tuple[str, ...]:
    """离开该标注时真正需要关闭的算法。

    只移除「该标注来源」：若全局勾选、或其他仍在活动的标注仍引用同一算法，
    则不得关闭（V1.4 2.1.2 去重与防误关）。
    """
    own = list(annotation_algorithms.get(annotation_id, ()))
    if not own:
        return ()
    keep: set[str] = {str(item) for item in global_algorithms}
    for other in active_annotations:
        if other == annotation_id:
            continue
        keep.update(str(item) for item in annotation_algorithms.get(other, ()))
    # ``configured`` 只用于确认算法仍属于本项目，不参与「是否需要关闭」的判断。
    allowed = None if configured is None else {str(item) for item in configured}
    return tuple(item for item in own
                 if item not in keep and (allowed is None or item in allowed))


@dataclass(frozen=True)
class AlgorithmPlan:
    """一次 AI 选择请求的确定性结论：要么可直接应用，要么必须先追问。"""

    kind: str  # 'apply' | 'clarification'
    requested: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    suppressed: tuple[str, ...] = ()
    resulting_global: tuple[str, ...] = ()
    resulting_active: tuple[str, ...] = ()
    question: str = ""
    error_code: str | None = None

    @property
    def requires_clarification(self) -> bool:
        return self.kind == "clarification"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "requested": list(self.requested),
                "conflicts": list(self.conflicts), "suppressed": list(self.suppressed),
                "resulting_global": list(self.resulting_global),
                "resulting_active": list(self.resulting_active),
                "question": self.question, "error_code": self.error_code}


def plan_selection(requested: Sequence[str], *, configured: Sequence[str],
                   global_algorithms: Iterable[str] = (),
                   active_annotations: Iterable[str] = (),
                   annotation_algorithms: Mapping[str, Sequence[str]] | None = None,
                   mode: str | None = None) -> AlgorithmPlan:
    """把「开启某算法」变成可执行计划，或在冲突时要求追问。

    - ``requested`` 里出现未配置算法：整批 ``ALGORITHM_NOT_CONFIGURED``，不部分绑定。
    - 请求算法与**当前活动标注**已配置算法冲突且未给出 ``mode``：
      返回追问，调用方不得生成确认卡。
    - ``mode='add'``（同时开启）：全局并集，标注算法继续有效。
    - ``mode='replace'``（仅替换）：本会话抑制冲突的标注算法，只保留请求集合。
    """
    if mode not in (None, "add", "replace"):
        raise AlgorithmError(INVALID_ALGORITHM_SELECTION, "AI选择模式必须是 add 或 replace。")
    ordered = normalize_algorithms(requested, configured=configured, allow_empty=False)
    mapping = dict(annotation_algorithms or {})
    active = list(active_annotations)
    annotation_union: list[str] = []
    for annotation in active:
        for item in mapping.get(annotation, ()):
            if item not in annotation_union:
                annotation_union.append(item)
    globals_now = tuple(dict.fromkeys(str(item) for item in global_algorithms))
    conflicts = tuple(item for item in ordered
                      if item in annotation_union and item not in globals_now)
    if conflicts and mode is None:
        return AlgorithmPlan(kind="clarification", requested=ordered, conflicts=conflicts,
                             question=CONFLICT_QUESTION,
                             error_code=ALGORITHM_CONFLICT_CLARIFICATION_REQUIRED)
    suppressed: tuple[str, ...] = ()
    if mode == "replace":
        suppressed = conflicts
    resulting_global = tuple(dict.fromkeys(globals_now + ordered))
    resulting_active = annotation_active_set(
        resulting_global, active, mapping, configured, suppressed)
    return AlgorithmPlan(kind="apply", requested=ordered, conflicts=conflicts,
                         suppressed=suppressed, resulting_global=resulting_global,
                         resulting_active=resulting_active)


def apply_plan(plan: AlgorithmPlan, state: Any) -> tuple[list[str], list[str]]:
    """把 ``apply`` 计划原子写入状态；``clarification`` 计划一律拒绝。

    返回 ``(global_algorithms, suppressed_algorithms)``。多算法要么全部生效，
    要么整批不动——不存在「部分绑定后返回成功」。
    """
    if not isinstance(plan, AlgorithmPlan):
        raise AlgorithmError(INVALID_ALGORITHM_SELECTION, "计划类型不合法。")
    if plan.requires_clarification:
        raise AlgorithmError(ALGORITHM_CONFLICT_CLARIFICATION_REQUIRED,
                             "AI冲突尚未完成追问，不能应用选择。")
    current = getattr(state, "global_algorithms", None)
    if not isinstance(current, list):
        raise AlgorithmError(INVALID_ALGORITHM_SELECTION, "状态缺少全局算法集合。")
    configured = getattr(state, "configured_algorithms", ())
    allowed = {str(item) for item in configured}
    target = [item for item in plan.resulting_global]
    if allowed and any(item not in allowed for item in target):
        raise AlgorithmError(ALGORITHM_NOT_CONFIGURED, "当前项目未配置该AI算法。")
    state.global_algorithms = sorted(set(target))
    return list(state.global_algorithms), list(plan.suppressed)


# ---------------------------------------------------------------------------
# 自然语言入口：冲突追问
# ---------------------------------------------------------------------------


def conflict_clarification(requested: Sequence[str], state: Mapping[str, Any] | Any,
                           *, mode: str | None = None) -> str | None:
    """自然语言入口的冲突判定；需要追问时返回固定问句，否则返回 None。

    ``mode='add'`` 表示用户已回答「同时开启」，此时不再追问。
    ``state`` 可以是状态对象或状态字典。缺少标注字段时返回 None——没有可核验
    的标注配置就不编造冲突，也不把「没有数据」当成「没有冲突」用于放行，
    因为该路径只影响是否追问，真正执行仍由执行器的算法白名单把关。
    """
    getter = state.get if isinstance(state, Mapping) else (lambda key, default=None: getattr(state, key, default))
    configured = getter("configured_algorithms")
    mapping = getter("annotation_algorithms")
    active = getter("active_annotations")
    globals_now = getter("global_algorithms") or []
    if not isinstance(mapping, Mapping) or not isinstance(active, (list, tuple)):
        return None
    if not configured:
        return None
    try:
        plan = plan_selection(requested, configured=configured, global_algorithms=globals_now,
                              active_annotations=active, annotation_algorithms=mapping,
                              mode=mode)
    except AlgorithmError:
        return None
    return CONFLICT_MESSAGE if plan.requires_clarification else None


#: 追问后的两种显式回答；只接受这两句，不做模糊匹配。
CONFLICT_ANSWERS = {
    "同时开启": "add",
    "同时开启。": "add",
    "同时开启AI": "add",
    "仅替换": "replace",
    "仅替换。": "replace",
    "仅替换为": "replace",
}

#: 「仅替换」需要改写标注自身的 AI 配置，本仓没有该写权限。
ALGORITHM_REPLACE_UNSUPPORTED = "ALGORITHM_REPLACE_UNSUPPORTED"
REPLACE_UNSUPPORTED_MESSAGE = (
    "「仅替换」需要修改该标注自身的AI配置，属于地图标注库的写操作，飞控助手不越权改写；"
    "本次未生成确认卡。请改为「同时开启」，或先在标注配置中调整该标注的AI。")
