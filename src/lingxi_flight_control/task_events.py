"""V1.4 任务事件的 typed 契约、幂等账本与确定性归约。

模拟器与真实平台事件桥共用同一份契约：

- 事件必须绑定 ``project/hangar/drone/task_batch``；
- 事件必须有 ``event_id`` 与单调 ``sequence``，重复投递幂等、旧序号拒绝；
- 低电量返航优先级最高，返航期间不接受临时任务替换/完成；
- 旧临时任务完成事件按 ``temporary_task_id`` 拒绝；
- ``all_tasks_ended`` 清空主线断点，``mapping_invalidated`` 之后永不恢复航线。

事件入口**不注册为模型工具**：模型只能提议飞行动作，不能直接制造平台事件。
真实平台事件源尚未取得可核验契约，因此 ``UnavailableTaskEventBridge`` 是默认
实现，读取时返回 typed ``TASK_EVENT_SOURCE_UNAVAILABLE``，而不是空列表
（空列表会被误读成“没有事件，所以状态没变”）。
"""
from __future__ import annotations

import math
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

CONTRACT_VERSION = "lingxi-task-events/v1"

#: 稳定错误码
INVALID_TASK_EVENT = "INVALID_TASK_EVENT"
TASK_EVENT_BINDING_MISMATCH = "TASK_EVENT_BINDING_MISMATCH"
STALE_TASK_EVENT = "STALE_TASK_EVENT"
DUPLICATE_TASK_EVENT = "DUPLICATE_TASK_EVENT"
STALE_TEMPORARY_TASK_EVENT = "STALE_TEMPORARY_TASK_EVENT"
STALE_ANNOTATION_VERSION = "STALE_ANNOTATION_VERSION"
LOW_BATTERY_RETURN_PRIORITY = "LOW_BATTERY_RETURN_PRIORITY"
UNKNOWN_ANNOTATION = "UNKNOWN_ANNOTATION"
UNEXPECTED_RECHARGE_EVENT = "UNEXPECTED_RECHARGE_EVENT"
TASK_EVENT_SOURCE_UNAVAILABLE = "TASK_EVENT_SOURCE_UNAVAILABLE"
#: 临时任务完成后必须由用户显式选择，不允许系统猜。
TEMPORARY_DECISION_REQUIRED = "TEMPORARY_DECISION_REQUIRED"
TEMPORARY_RESUME_UNAVAILABLE = "TEMPORARY_RESUME_UNAVAILABLE"
INVALID_TEMPORARY_DECISION = "INVALID_TEMPORARY_DECISION"
#: 4.1.3：超时未选择后不再接受用户选择，直接按低电量规则处理。
TEMPORARY_DECISION_TIMED_OUT = "TEMPORARY_DECISION_TIMED_OUT"

#: V1.4 4.1.3 的两种显式选择；没有默认值。
POST_TEMPORARY_OPTIONS = ("resume_main", "end_all")

#: V1.4 4.1.3：临时任务完成后「超时未选择」的计时兜底。
#:
#: **假设**：规范只规定结果（按低电量规则处理），没有给时长。这里显式取 60 秒，
#: 可由 ``post_temporary_decision(..., timeout_sec=...)`` 覆盖；它不是规范数值。
POST_TEMPORARY_DECISION_TIMEOUT_SEC = 60.0
#: 超时后显式转交 4.2 低电量规则（返航 + 保留主线断点），不做第三种结局。
TEMPORARY_DECISION_TIMEOUT_ACTION = "low_battery_return"
#: V1.4 4.2：返航时正在执行临时任务，充电重启后恢复原始主线任务的前端提示。
TEMPORARY_TASK_UNFINISHED_RECHARGE_MESSAGE = "临时任务未完成，充电重启后将恢复原始任务。"


class TaskEventKind(str, Enum):
    TEMPORARY_TASK_REPLACED = "temporary_task_replaced"
    TEMPORARY_TASK_COMPLETED = "temporary_task_completed"
    LOW_BATTERY_RETURN = "low_battery_return"
    RECHARGED_ONLINE = "recharged_online"
    ANNOTATION_ENTERED = "annotation_entered"
    ANNOTATION_LEFT = "annotation_left"
    ALL_TASKS_ENDED = "all_tasks_ended"
    MAPPING_INVALIDATED = "mapping_invalidated"


#: 兼容旧调用方使用的短名（模拟器历史接口）。
LEGACY_ALIASES = {
    "temporary_completed": TaskEventKind.TEMPORARY_TASK_COMPLETED,
}


class TaskEventError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 128 and value.strip() == value


@dataclass(frozen=True)
class TaskEvent:
    event_id: str
    kind: TaskEventKind
    project_id: str
    hangar_id: str
    drone_id: str
    task_batch_id: str
    sequence: int
    occurred_at: float
    temporary_task_id: str | None = None
    annotation_id: str | None = None
    state_version: str | None = None
    annotation_version: int | None = None

    def validate(self) -> None:
        if not _identifier(self.event_id):
            raise TaskEventError(INVALID_TASK_EVENT)
        if not isinstance(self.kind, TaskEventKind):
            raise TaskEventError(INVALID_TASK_EVENT)
        if not all(_identifier(value) for value in
                   (self.project_id, self.hangar_id, self.drone_id, self.task_batch_id)):
            raise TaskEventError(TASK_EVENT_BINDING_MISMATCH)
        if type(self.sequence) is not int or self.sequence < 0:
            raise TaskEventError(INVALID_TASK_EVENT)
        if type(self.occurred_at) not in (int, float) or not math.isfinite(self.occurred_at):
            raise TaskEventError(INVALID_TASK_EVENT)
        if self.state_version is not None and not _identifier(self.state_version):
            raise TaskEventError(INVALID_TASK_EVENT)
        if self.annotation_version is not None and (
                type(self.annotation_version) is not int or self.annotation_version < 0):
            raise TaskEventError(INVALID_TASK_EVENT)
        if self.kind in {TaskEventKind.TEMPORARY_TASK_REPLACED, TaskEventKind.TEMPORARY_TASK_COMPLETED}:
            if not _identifier(self.temporary_task_id):
                raise TaskEventError(INVALID_TASK_EVENT)
        if self.kind in {TaskEventKind.ANNOTATION_ENTERED, TaskEventKind.ANNOTATION_LEFT}:
            if not _identifier(self.annotation_id):
                raise TaskEventError(INVALID_TASK_EVENT)
        if self.kind is not TaskEventKind.ANNOTATION_ENTERED and self.kind is not TaskEventKind.ANNOTATION_LEFT:
            if self.annotation_id is not None:
                raise TaskEventError(INVALID_TASK_EVENT)
            if self.annotation_version is not None:
                raise TaskEventError(INVALID_TASK_EVENT)

    def binding(self) -> tuple[str, str, str, str]:
        return (self.project_id, self.hangar_id, self.drone_id, self.task_batch_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id, "kind": self.kind.value,
            "project_id": self.project_id, "hangar_id": self.hangar_id,
            "drone_id": self.drone_id, "task_batch_id": self.task_batch_id,
            "sequence": self.sequence, "occurred_at": self.occurred_at,
            "temporary_task_id": self.temporary_task_id,
            "annotation_id": self.annotation_id, "state_version": self.state_version,
            "annotation_version": self.annotation_version,
            "contract_version": CONTRACT_VERSION,
        }


def coerce_kind(value: Any) -> TaskEventKind:
    if isinstance(value, TaskEventKind):
        return value
    if isinstance(value, str):
        if value in LEGACY_ALIASES:
            return LEGACY_ALIASES[value]
        try:
            return TaskEventKind(value)
        except ValueError:
            pass
    raise TaskEventError(INVALID_TASK_EVENT)


@dataclass
class TaskEventLedger:
    """按绑定维护“已处理到哪里”，重复幂等、旧序号拒绝。"""

    max_event_ids: int = 512
    last_sequence: int = -1
    _event_ids: deque = field(default_factory=deque, repr=False)
    _seen: set[str] = field(default_factory=set, repr=False)
    _annotation_versions: dict[str, int] = field(default_factory=dict, repr=False)
    applied: int = 0
    duplicates: int = 0

    def accept(self, event: TaskEvent, binding: Any) -> str:
        event.validate()
        expected = (getattr(binding, "project_id", None), getattr(binding, "hangar_id", None),
                    getattr(binding, "drone_id", None), getattr(binding, "task_batch_id", None))
        if event.binding() != expected:
            raise TaskEventError(TASK_EVENT_BINDING_MISMATCH)
        if event.event_id in self._seen:
            self.duplicates += 1
            return DUPLICATE_TASK_EVENT
        if event.sequence <= self.last_sequence:
            # 乱序或旧版本事件：不能让它回滚已经生效的更高序号状态。
            raise TaskEventError(STALE_TASK_EVENT)
        if event.annotation_version is not None:
            previous = self._annotation_versions.get(event.annotation_id or "", -1)
            if event.annotation_version < previous:
                # 标注配置已被更高版本取代，旧版本的到离事件一律拒绝。
                raise TaskEventError(STALE_ANNOTATION_VERSION)
            self._annotation_versions[event.annotation_id or ""] = event.annotation_version
        self._seen.add(event.event_id)
        self._event_ids.append(event.event_id)
        if len(self._event_ids) > self.max_event_ids:
            self._seen.discard(self._event_ids.popleft())
        self.last_sequence = event.sequence
        self.applied += 1
        return "applied"

    def snapshot(self) -> dict[str, Any]:
        return {"contract_version": CONTRACT_VERSION, "last_sequence": self.last_sequence,
                "applied": self.applied, "duplicates": self.duplicates,
                "tracked_event_ids": len(self._seen),
                "annotation_versions": dict(sorted(self._annotation_versions.items()))}


def _flag(state: Any, name: str) -> bool:
    """布尔标志读取；平台快照（Mapping）与模拟器对象态都要支持。"""
    if isinstance(state, Mapping):
        return state.get(name) is True
    return getattr(state, name, False) is True


def _remember(state: Any, name: str, value: Any) -> None:
    """记录 4.1.3 的计时锚点/提示标志；只加在可变状态上，不改既有字段语义。"""
    try:
        setattr(state, name, value)
    except AttributeError:  # pragma: no cover - 冻结/只读状态不记录投影标志
        return


def _active_route(state: Any) -> bool:
    return bool(getattr(state, "route_id", None)) and getattr(state, "route_status", None) in {
        "running", "paused", "uploaded", "point_flight", "point_orbit", "temporary_completed"}


def reduce_event(state: Any, event: TaskEvent) -> bool:
    """把一个已通过账本校验的事件归约到状态上；不合法顺序一律抛错。

    返回 ``True`` 表示状态确实变化；``False`` 表示事件合法但已是幂等重复
    （例如同一标注的重复 entered/left），此时调用方不应推进 revision。
    """
    kind = event.kind
    mapping_invalidated = getattr(state, "mapping_invalidated", False) is True
    low_battery = getattr(state, "low_battery_return", False) is True

    if kind is TaskEventKind.LOW_BATTERY_RETURN:
        # 最高优先级：保留可续飞断点，丢弃未完成临时任务，进入返航。
        had_temporary = getattr(state, "temporary_task_id", None) is not None
        if _active_route(state) and not mapping_invalidated and getattr(state, "breakpoint_index", None) is None:
            state.breakpoint_index = 0
        state.temporary_task_id = None
        state.low_battery_return = True
        state.task_status = "returning_home"
        if had_temporary:
            # 记录「这次返航中断了一个未完成临时任务」，4.2 的前端提示据此展示。
            _remember(state, "temporary_task_abandoned", True)
        _remember(state, "temporary_decision_deadline", None)
        return True

    if kind is TaskEventKind.RECHARGED_ONLINE:
        if not low_battery:
            raise TaskEventError(UNEXPECTED_RECHARGE_EVENT)
        state.low_battery_return = False
        state.temporary_task_id = None
        # 「临时任务未完成」提示只属于这次返航；重启上线后提示已完成使命。
        _remember(state, "temporary_task_abandoned", False)
        _remember(state, "temporary_decision_deadline", None)
        _remember(state, "temporary_decision_timed_out", False)
        # 只恢复「仍然有效」的巡检主线：测绘已作废或结束全部任务后断点已清空。
        resumable = bool(getattr(state, "route_id", None)) and not mapping_invalidated \
            and getattr(state, "breakpoint_index", None) is not None \
            and getattr(state, "route_resume_disabled", False) is not True
        state.route_status = "running" if resumable else "ended"
        state.task_status = "route_executing" if resumable else "ended"
        return True

    if kind is TaskEventKind.MAPPING_INVALIDATED:
        # 测绘成果作废是独立事实，返航中也可能发生；它必须记录，并永久禁止恢复。
        state.mapping_invalidated = True
        state.auto_stitching = False
        state.breakpoint_index = None
        state.temporary_task_id = None
        _remember(state, "temporary_decision_deadline", None)
        _remember(state, "temporary_decision_timed_out", False)
        if hasattr(state, "rebuild_type"):
            state.rebuild_type = None
        if hasattr(state, "break_info"):
            state.break_info = None
        if hasattr(state, "route_resume_disabled"):
            state.route_resume_disabled = True
        if _active_route(state):
            state.route_status = "interrupted"
        else:
            state.route_status = "ended"
        state.task_status = "ended"
        return True

    if low_battery:
        # 返航中不接受临时任务替换/完成，避免覆盖平台返航流程。
        raise TaskEventError(LOW_BATTERY_RETURN_PRIORITY)

    if kind is TaskEventKind.TEMPORARY_TASK_REPLACED:
        # 主线断点始终保留；临时任务不独立保留断点。
        if _active_route(state) and not mapping_invalidated and getattr(state, "breakpoint_index", None) is None:
            state.breakpoint_index = 0
        state.temporary_task_id = event.temporary_task_id
        # 新的临时任务接管后，上一轮「完成后选择」的计时作废。
        _remember(state, "temporary_decision_deadline", None)
        _remember(state, "temporary_decision_timed_out", False)
        return True

    if kind is TaskEventKind.TEMPORARY_TASK_COMPLETED:
        if event.temporary_task_id != getattr(state, "temporary_task_id", None):
            raise TaskEventError(STALE_TEMPORARY_TASK_EVENT)
        state.temporary_task_id = None
        # 4.1.3 计时锚点取平台事件时间；超时时长见 POST_TEMPORARY_DECISION_TIMEOUT_SEC。
        _remember(state, "temporary_completed_at", float(event.occurred_at))
        _remember(state, "temporary_decision_deadline",
                  float(event.occurred_at) + POST_TEMPORARY_DECISION_TIMEOUT_SEC)
        _remember(state, "temporary_decision_timed_out", False)
        if mapping_invalidated:
            state.route_status = "ended"
            state.task_status = "ended"
        else:
            state.route_status = "temporary_completed"
        return True

    if kind is TaskEventKind.ALL_TASKS_ENDED:
        state.breakpoint_index = None
        state.temporary_task_id = None
        state.route_status = "ended"
        state.task_status = "ended"
        # 结束全部任务后不再存在「临时任务完成后怎么选」的问题。
        _remember(state, "temporary_decision_deadline", None)
        _remember(state, "temporary_decision_timed_out", False)
        annotations = getattr(state, "active_annotations", None)
        if isinstance(annotations, list):
            annotations.clear()
        return True

    if kind in {TaskEventKind.ANNOTATION_ENTERED, TaskEventKind.ANNOTATION_LEFT}:
        algorithms = getattr(state, "annotation_algorithms", None)
        if not isinstance(algorithms, Mapping) or event.annotation_id not in algorithms:
            raise TaskEventError(UNKNOWN_ANNOTATION)
        active = getattr(state, "active_annotations", None)
        if not isinstance(active, list):
            raise TaskEventError(INVALID_TASK_EVENT)
        if kind is TaskEventKind.ANNOTATION_ENTERED:
            if event.annotation_id in active:
                # 重复到达：幂等，不改状态、不推进版本。
                return False
            active.append(event.annotation_id)
            return True
        if event.annotation_id not in active:
            # 重复离开：幂等。
            return False
        active.remove(event.annotation_id)
        return True

    raise TaskEventError(INVALID_TASK_EVENT)


def apply_task_event(state: Any, ledger: TaskEventLedger, event: TaskEvent, binding: Any) -> dict[str, Any]:
    """校验 → 归约 → 推进版本；返回可安全记录的结果摘要。"""
    outcome = ledger.accept(event, binding)
    if outcome == DUPLICATE_TASK_EVENT:
        # 幂等：重复投递不重复改状态，也不推进 revision。
        return {"outcome": outcome, "event_id": event.event_id, "kind": event.kind.value,
                "revision": getattr(state, "revision", 0), "changed": False}
    changed = reduce_event(state, event)
    if changed:
        state.revision = int(getattr(state, "revision", 0)) + 1
    return {"outcome": outcome, "event_id": event.event_id, "kind": event.kind.value,
            "revision": getattr(state, "revision", 0), "changed": changed,
            "state_version": event.state_version}


@runtime_checkable
class TaskEventBridge(Protocol):
    """真实平台事件桥必须实现的只读接口。"""

    def read_events(self, binding: Any, *, after_sequence: int) -> Sequence[TaskEvent]:
        ...


class UnavailableTaskEventBridge:
    """默认实现：没有可核验的平台事件契约时显式不可用。"""

    def __init__(self, code: str = TASK_EVENT_SOURCE_UNAVAILABLE):
        self.code = code

    def read_events(self, binding: Any, *, after_sequence: int = -1) -> Sequence[TaskEvent]:
        raise TaskEventError(self.code)


def event_from_legacy(kind: str, *, event_id: str, sequence: int, binding: Any,
                      occurred_at: float, temporary_task_id: str | None = None,
                      annotation_id: str | None = None,
                      state_version: str | None = None,
                      annotation_version: int | None = None) -> TaskEvent:
    """把模拟器旧接口的字符串事件升级成 typed 事件。"""
    return TaskEvent(event_id=event_id, kind=coerce_kind(kind),
                     project_id=getattr(binding, "project_id", ""),
                     hangar_id=getattr(binding, "hangar_id", ""),
                     drone_id=getattr(binding, "drone_id", ""),
                     task_batch_id=getattr(binding, "task_batch_id", ""),
                     sequence=sequence, occurred_at=occurred_at,
                     temporary_task_id=temporary_task_id, annotation_id=annotation_id,
                     state_version=state_version, annotation_version=annotation_version)


# ---------------------------------------------------------------------------
# V1.4 4.1.3：临时任务完成后的显式选择
# ---------------------------------------------------------------------------


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def temporary_decision_deadline(state: Any, *, timeout_sec: float | None = None) -> float | None:
    """4.1.3 计时截止点；锚点缺失时返回 ``None``（不猜、不补默认时间）。"""
    timeout = POST_TEMPORARY_DECISION_TIMEOUT_SEC if timeout_sec is None else timeout_sec
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise TaskEventError(INVALID_TEMPORARY_DECISION)
    stored = _as_float(getattr(state, "temporary_decision_deadline", None))
    if stored is not None:
        return stored
    completed_at = _as_float(getattr(state, "temporary_completed_at", None))
    if completed_at is None:
        return None
    return completed_at + float(timeout)


def post_temporary_decision(state: Any, *, now: float | None = None,
                            timeout_sec: float | None = None) -> dict[str, Any]:
    """临时任务完成后必须由用户显式选择「恢复主线」或「结束全部任务」。

    返回 ``required=True`` 时调用方**不得**替用户猜：V1.4 明确两种结果都合法，
    唯一允许的自动行为是超时后按低电量规则处理（4.1.3）；低电量返航本身仍由平台
    事件驱动，这里只给出「是否已到超时」的确定性判定。

    ``now`` 与设备时钟无关，由调用方传入；缺省时只报告 ``required``，不判定超时。
    ``timeout_sec`` 缺省取 :data:`POST_TEMPORARY_DECISION_TIMEOUT_SEC`（60 秒，**假设**）。
    """
    timeout = POST_TEMPORARY_DECISION_TIMEOUT_SEC if timeout_sec is None else timeout_sec
    deadline = temporary_decision_deadline(state, timeout_sec=timeout)
    clock = _as_float(now)
    completed = getattr(state, "route_status", None) == "temporary_completed"
    # 只有「仍在等用户选择」的临时任务完成态才计时；用户已作过选择、或平台返航
    # 已接管后，残留的 deadline 不得再把任务误判成超时。
    decision_pending = completed and not _flag(state, "low_battery_return")
    remaining = None
    timed_out = False
    if decision_pending and deadline is not None and clock is not None:
        remaining = max(0.0, deadline - clock)
        timed_out = clock >= deadline
    base = {"timeout_sec": float(timeout), "deadline_at": deadline,
            "remaining_sec": remaining, "timed_out": timed_out,
            "timeout_action": TEMPORARY_DECISION_TIMEOUT_ACTION,
            "timeout_error_code": TEMPORARY_DECISION_TIMED_OUT if timed_out else None}
    if _flag(state, "low_battery_return"):
        # 4.2 优先级：平台返航已接管时，「临时任务完成后怎么选」不再成立。
        return {"required": False, "options": [], "default": None,
                "error_code": None, "resumable": False, **base}
    if not completed:
        return {"required": False, "options": [], "default": None,
                "error_code": None, "resumable": False, **base}
    mapping_invalidated = getattr(state, "mapping_invalidated", False) is True
    resumable = (bool(getattr(state, "route_id", None)) and not mapping_invalidated
                 and getattr(state, "breakpoint_index", None) is not None
                 and getattr(state, "route_resume_disabled", False) is not True)
    return {"required": True, "options": list(POST_TEMPORARY_OPTIONS), "default": None,
            "error_code": TEMPORARY_DECISION_REQUIRED, "resumable": resumable, **base}


def temporary_decision_timed_out(state: Any, *, now: float, timeout_sec: float | None = None) -> bool:
    """只回答「此刻是否已超时未选择」；不改变状态。"""
    return post_temporary_decision(state, now=now, timeout_sec=timeout_sec)["timed_out"]


def apply_temporary_decision_timeout(state: Any, *, now: float,
                                     timeout_sec: float | None = None) -> dict[str, Any]:
    """4.1.3 兜底：超时未选择 → 按 4.2 低电量规则处理。

    与 ``LOW_BATTERY_RETURN`` 归约共用同一套语义：保留可续飞的主线断点、丢弃未完成
    临时任务、进入返航；充电重启后走 ``RECHARGED_ONLINE`` 恢复原始主线任务。
    未到超时或已不处于待选择状态时一律不改状态（``applied=False``），可安全重放。
    """
    if _flag(state, "low_battery_return"):
        # 已按低电量规则返航：超时兜底已生效，重放不再改状态。
        return {"applied": False, "reason": "low_battery_return",
                **post_temporary_decision(state, now=now, timeout_sec=timeout_sec)}
    decision = post_temporary_decision(state, now=now, timeout_sec=timeout_sec)
    if not decision["required"]:
        return {"applied": False, "reason": "not_required", **decision}
    if not decision["timed_out"]:
        return {"applied": False, "reason": "not_timed_out", **decision}
    mapping_invalidated = getattr(state, "mapping_invalidated", False) is True
    if _active_route(state) and not mapping_invalidated and getattr(state, "breakpoint_index", None) is None:
        state.breakpoint_index = 0
    state.temporary_task_id = None
    state.low_battery_return = True
    state.task_status = "returning_home"
    _remember(state, "temporary_decision_timed_out", True)
    state.revision = int(getattr(state, "revision", 0)) + 1
    return {"applied": True, "reason": "timed_out",
            "action": TEMPORARY_DECISION_TIMEOUT_ACTION,
            "route_status": getattr(state, "route_status", None),
            "task_status": getattr(state, "task_status", None),
            **decision}


def validate_post_temporary_choice(state: Any, choice: Any, *, now: float | None = None,
                                   timeout_sec: float | None = None) -> str:
    """校验用户的选择；``None``、超时后、未知取值一律拒绝，绝不回落到默认分支。"""
    decision = post_temporary_decision(state, now=now, timeout_sec=timeout_sec)
    if not decision["required"]:
        raise TaskEventError(TEMPORARY_DECISION_REQUIRED)
    if decision["timed_out"]:
        # 4.1.3：超时未选择后已按低电量规则处理，不再接受事后挑选。
        raise TaskEventError(TEMPORARY_DECISION_TIMED_OUT)
    if choice is None:
        raise TaskEventError(TEMPORARY_DECISION_REQUIRED)
    if choice not in POST_TEMPORARY_OPTIONS:
        raise TaskEventError(INVALID_TEMPORARY_DECISION)
    if choice == "resume_main" and not decision["resumable"]:
        # 测绘已作废或断点已清空：没有可恢复的主线，只能结束全部任务。
        raise TaskEventError(TEMPORARY_RESUME_UNAVAILABLE)
    return str(choice)


def temporary_task_return_notice(state: Any) -> str | None:
    """4.2：返航时正在执行临时任务 → 前端提示充电重启后恢复原始任务。

    只读取归约阶段记录的 ``low_battery_return`` / ``temporary_task_abandoned`` 事实；
    两个标志都缺失（例如平台快照未投影）时返回 ``None``，不凭空提示。
    """
    if _flag(state, "low_battery_return") and _flag(state, "temporary_task_abandoned"):
        return TEMPORARY_TASK_UNFINISHED_RECHARGE_MESSAGE
    return None
