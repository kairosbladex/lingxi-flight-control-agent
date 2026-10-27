"""V1.4 4.1.1 / 4.1.2 测绘中断原子契约。

V1.4 要求测绘/高精度建模任务默认不可中断；用户确认强制干预后，必须在**一个
可审计的原子业务操作**里同时完成六件事，任何一步不能保证就不得下发后续飞控动作：

1. 停止自动拼图（``stop_auto_stitching``）
2. 清空或作废 ``rebuildType``（``void_rebuild_type``）
3. 清空或作废 ``breakInfo``（``void_break_info``）
4. 作废原测绘任务断点（``invalidate_task_breakpoint``）
5. 关闭恢复航线能力（``disable_route_resume``）
6. 禁止恢复原测绘任务（``block_task_resume``）

顺序不可颠倒，也不允许「先发飞控动作、再补做测绘作废」。

本模块只定义契约与模拟器实现。真实平台是否提供这样的原子接口**未经验证**，
因此 ``UnavailableMappingInterruptionGateway`` 是默认实现：它返回
``MAPPING_INTERRUPTION_UNAVAILABLE``，执行器据此拒绝且不下发任何飞控指令。

跨进程「最多调用一次」不靠进程内锁，而靠 SQLite command journal 里的持久化
reservation（见 ``RESERVATION_*``）。状态机只有四态：

``mapping_reservation_pending``
    已取得 reservation，**尚未调用**网关。别的进程看到 pending 只等不调；
    租约过期后可由恢复方 CAS 成 unknown 并调用一次。
``mapping_interruption_unknown``
    调用即将开始/已经开始，结果未知。此状态是**终态**（对自动流程而言）：
    不重试网关、不重试飞控 submit，只能人工核对或凭权威回执显式恢复。
``mapping_interruption_succeeded``
    网关返回 ``atomic=True`` 且 ``confirmed=True``；允许继续写命令账本并 submit。
``mapping_interruption_failed``
    网关给出了明确的失败回执（unavailable/partial/not confirmed）；同一幂等键
    不得再调用网关，也不得 submit。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol, Sequence, runtime_checkable

CONTRACT_VERSION = "lingxi-mapping-interruption/v1"
RESERVATION_SCHEMA = "lingxi-mapping-reservation/v1"

#: 稳定错误码
MAPPING_INTERRUPTION_UNAVAILABLE = "MAPPING_INTERRUPTION_UNAVAILABLE"
MAPPING_INTERRUPTION_NOT_CONFIRMED = "MAPPING_INTERRUPTION_NOT_CONFIRMED"
MAPPING_INTERRUPTION_PARTIAL = "MAPPING_INTERRUPTION_PARTIAL"
#: 网关结果未知：既不是 unavailable，也不是 success，禁止自动重试。
MAPPING_INTERRUPTION_UNKNOWN = "MAPPING_INTERRUPTION_UNKNOWN"
#: 人工核对判定「没有作废」时写入的失败码；该幂等键到此为止。
MAPPING_INTERRUPTION_FAILED = "MAPPING_INTERRUPTION_FAILED"
INVALID_MAPPING_INTERRUPTION = "INVALID_MAPPING_INTERRUPTION"

#: 持久化 reservation 状态（存于 SQLite command journal）。
RESERVATION_PENDING = "mapping_reservation_pending"
RESERVATION_UNKNOWN = "mapping_interruption_unknown"
RESERVATION_SUCCEEDED = "mapping_interruption_succeeded"
RESERVATION_FAILED = "mapping_interruption_failed"
RESERVATION_STATES = frozenset({RESERVATION_PENDING, RESERVATION_UNKNOWN,
                                RESERVATION_SUCCEEDED, RESERVATION_FAILED})
#: 不允许再有任何自动外部调用的状态。
RESERVATION_TERMINAL_STATES = frozenset({RESERVATION_UNKNOWN, RESERVATION_SUCCEEDED,
                                         RESERVATION_FAILED})

#: 原子操作的固定步骤序列；审计与测试都以此为准。
INTERRUPTION_STEPS: tuple[str, ...] = (
    "stop_auto_stitching",
    "void_rebuild_type",
    "void_break_info",
    "invalidate_task_breakpoint",
    "disable_route_resume",
    "block_task_resume",
)

#: 会触发测绘中断判定的动作。载荷类（拍照/录像/喊话/灯光/云台）不在其中：
#: V1.4 4.3.2 明确载荷控制免暂停航线，不能把拍照误判成测绘中断。
MAPPING_INTERRUPTING_ACTIONS = frozenset({
    "move_forward", "move_backward", "move_left", "move_right", "move_up", "move_down",
    "turn_left", "turn_right",
    "point_flight", "point_orbit",
    "route_pause", "hover", "emergency_stop", "return_home", "land", "land_alternate",
    "task_end_current", "task_end_all",
})

#: 载荷与云台动作：显式声明为「不触发测绘中断」，用于契约自检。
NON_INTERRUPTING_PAYLOAD_ACTIONS = frozenset({
    "camera_photo", "camera_start_recording", "camera_stop_recording", "camera_panorama",
    "camera_zoom", "camera_infrared", "camera_wide", "camera_electro_optical",
    "camera_lock_focus", "gimbal_up", "gimbal_down", "gimbal_left", "gimbal_right",
    "gimbal_reset", "gimbal_45", "gimbal_90", "gimbal_digital_zoom",
    "speaker_speak", "speaker_volume", "searchlight_brightness",
    "payload_megaphone", "payload_searchlight", "payload_methane", "payload_dropper",
    "payload_dynamic", "infrared_set_limits", "ai_algorithm_set",
    "route_resume", "continue_from_breakpoint", "read_state",
})


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 128 and value.strip() == value


class MappingInterruptionError(ValueError):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class MappingInterruptionRequest:
    """一次已确认的强制干预；``confirmed`` 必须为真才允许进入原子操作。

    ``idempotency_key`` / ``trace_id`` / ``context_version`` / ``snapshot_version`` /
    ``fingerprint`` 是本地 reservation 的同一组标识，随请求一起交给网关，
    以便网关（如果它真的支持）按同一幂等键收敛。它们为空时表示调用方没有这些
    事实，网关不得据此假设平台侧幂等。
    """

    command_id: str
    action: str
    task_batch_id: str
    confirmed: bool = False
    route_id: str | None = None
    task_type: str | None = None
    idempotency_key: str | None = None
    trace_id: str | None = None
    context_version: int | None = None
    snapshot_version: int | str | None = None
    fingerprint: str | None = None

    def validate(self) -> None:
        if not _identifier(self.command_id):
            raise MappingInterruptionError(INVALID_MAPPING_INTERRUPTION, "命令标识不合法。")
        if not isinstance(self.action, str) or not self.action:
            raise MappingInterruptionError(INVALID_MAPPING_INTERRUPTION, "动作不合法。")
        if not _identifier(self.task_batch_id):
            raise MappingInterruptionError(INVALID_MAPPING_INTERRUPTION, "任务批次标识不合法。")
        if type(self.confirmed) is not bool:
            raise MappingInterruptionError(INVALID_MAPPING_INTERRUPTION, "确认标志不合法。")
        if self.route_id is not None and not _identifier(self.route_id):
            raise MappingInterruptionError(INVALID_MAPPING_INTERRUPTION, "航线标识不合法。")
        if self.idempotency_key is not None and not _identifier(self.idempotency_key):
            raise MappingInterruptionError(INVALID_MAPPING_INTERRUPTION, "幂等键不合法。")
        if self.trace_id is not None and not _identifier(self.trace_id):
            raise MappingInterruptionError(INVALID_MAPPING_INTERRUPTION, "trace 标识不合法。")
        if self.context_version is not None and (type(self.context_version) is not int
                                                 or self.context_version < 0):
            raise MappingInterruptionError(INVALID_MAPPING_INTERRUPTION, "上下文版本不合法。")
        if self.snapshot_version is not None and not (
                (type(self.snapshot_version) is int and self.snapshot_version >= 0)
                or (isinstance(self.snapshot_version, str)
                    and len(self.snapshot_version) == 64)):
            raise MappingInterruptionError(INVALID_MAPPING_INTERRUPTION, "快照版本不合法。")
        if self.fingerprint is not None and (
                not isinstance(self.fingerprint, str) or len(self.fingerprint) != 64):
            raise MappingInterruptionError(INVALID_MAPPING_INTERRUPTION, "指纹不合法。")

    def to_dict(self) -> dict[str, Any]:
        return {"command_id": self.command_id, "action": self.action,
                "task_batch_id": self.task_batch_id, "confirmed": self.confirmed,
                "route_id": self.route_id, "task_type": self.task_type,
                "idempotency_key": self.idempotency_key, "trace_id": self.trace_id,
                "context_version": self.context_version,
                "snapshot_version": self.snapshot_version, "fingerprint": self.fingerprint}


@dataclass(frozen=True)
class MappingInterruptionReceipt:
    """原子操作回执；只有 ``atomic=True`` 且 ``confirmed=True`` 才允许继续下发飞控动作。"""

    atomic: bool
    execution_mode: str
    completed_steps: tuple[str, ...] = ()
    failed_step: str | None = None
    audit_id: str = ""
    error_code: str | None = None
    message: str = ""
    task_resumable: bool = False
    #: 平台是否明确确认这次作废已经生效。缺失即为未确认，执行器一律不放行。
    confirmed: bool = False
    contract_version: str = CONTRACT_VERSION

    @property
    def blocked(self) -> bool:
        return not self.atomic

    @property
    def effective(self) -> bool:
        """只有「原子完成 + 明确确认 + 无错误码」才构成测绘已作废。"""
        return self.atomic is True and self.confirmed is True and self.error_code is None

    def to_dict(self) -> dict[str, Any]:
        return {"atomic": self.atomic, "execution_mode": self.execution_mode,
                "completed_steps": list(self.completed_steps), "failed_step": self.failed_step,
                "audit_id": self.audit_id, "error_code": self.error_code,
                "message": self.message, "task_resumable": self.task_resumable,
                "confirmed": self.confirmed, "contract_version": self.contract_version}


@runtime_checkable
class MappingInterruptionGateway(Protocol):
    """平台侧的「测绘中断 + 作废」原子业务操作。

    ``supports_idempotency_key`` 必须如实声明平台是否真的按幂等键收敛；默认
    ``False``。执行器无论如何都把幂等键放进请求，但**不会**因为本地说有键就
    宣称平台侧幂等——跨进程最多一次靠的是本地 reservation。
    """

    execution_mode: str
    supports_idempotency_key: bool

    def interrupt(self, request: MappingInterruptionRequest) -> MappingInterruptionReceipt:
        ...


class UnavailableMappingInterruptionGateway:
    """默认实现：没有可核验的原子契约时，明确不可用且不产生任何副作用。"""

    execution_mode = "unavailable"
    supports_idempotency_key = False

    def interrupt(self, request: MappingInterruptionRequest) -> MappingInterruptionReceipt:
        request.validate()
        return MappingInterruptionReceipt(
            atomic=False, execution_mode=self.execution_mode, completed_steps=(),
            error_code=MAPPING_INTERRUPTION_UNAVAILABLE,
            message=("平台尚未提供「停止拼图 + 作废重建配置/断点 + 关闭恢复」的原子契约，"
                     "本次不下发任何飞控动作。"))


class HttpMappingInterruptionGateway:
    """固定平台边界的测绘中断原子事务适配器。

    网络异常一律按结果未知处理，reservation 会阻止自动重试和后续飞控下发。
    """

    execution_mode = "platform"
    supports_idempotency_key = True

    def __init__(self, transport: Any, binding: Any, *, site_id: str, task_id: str | None = None):
        if not callable(getattr(transport, "interrupt_mapping", None)):
            raise ValueError("mapping transport must provide interrupt_mapping")
        if (not _identifier(site_id) or (task_id is not None and not _identifier(task_id))
                or not callable(getattr(binding, "validate", None))):
            raise ValueError("invalid mapping gateway scope")
        binding.validate()
        self._transport = transport
        self._binding = binding
        self._site_id = site_id
        self._task_id = task_id

    def interrupt(self, request: MappingInterruptionRequest) -> MappingInterruptionReceipt:
        request.validate()
        try:
            response = self._transport.interrupt_mapping(
                self._binding, request, site_id=self._site_id, task_id=self._task_id)
        except Exception:
            return MappingInterruptionReceipt(
                atomic=False, execution_mode=self.execution_mode,
                error_code=MAPPING_INTERRUPTION_UNKNOWN,
                message="测绘中断平台事务结果未知，未下发后续飞控动作。")
        payload = getattr(response, "payload", None)
        data = payload.get("data") if isinstance(payload, dict) else None
        if (getattr(response, "status_code", None) != 200
                or not isinstance(payload, dict) or payload.get("code") != 200
                or not isinstance(data, dict)):
            return MappingInterruptionReceipt(
                atomic=False, execution_mode=self.execution_mode,
                error_code=MAPPING_INTERRUPTION_UNAVAILABLE,
                message="平台尚未提供可核验的测绘中断原子回执，未下发后续飞控动作。")
        completed = data.get("completed_steps", data.get("completedSteps", ()))
        if (type(data.get("atomic")) is not bool or type(data.get("confirmed")) is not bool
                or not isinstance(completed, list)
                or any(item not in INTERRUPTION_STEPS for item in completed)):
            return MappingInterruptionReceipt(
                atomic=False, execution_mode=self.execution_mode,
                error_code=MAPPING_INTERRUPTION_UNKNOWN,
                message="平台测绘中断回执字段无法核验，未下发后续飞控动作。")
        return MappingInterruptionReceipt(
            atomic=data["atomic"], execution_mode=self.execution_mode,
            completed_steps=tuple(completed),
            failed_step=data.get("failed_step", data.get("failedStep")),
            audit_id=data.get("audit_id", data.get("auditId", "")),
            error_code=data.get("error_code", data.get("errorCode")),
            message=data.get("message", ""),
            task_resumable=data.get("task_resumable", data.get("taskResumable", False)),
            confirmed=data["confirmed"],
        )


def apply_atomic_steps(state: Any, *, action: str, audit_id: str) -> MappingInterruptionReceipt:
    """在状态对象上按固定顺序执行六步；任一步异常即整体失败并报告断点。"""
    completed: list[str] = []
    for step in INTERRUPTION_STEPS:
        try:
            if step == "stop_auto_stitching":
                state.auto_stitching = False
            elif step == "void_rebuild_type":
                state.rebuild_type = None
            elif step == "void_break_info":
                state.break_info = None
            elif step == "invalidate_task_breakpoint":
                state.breakpoint_index = None
            elif step == "disable_route_resume":
                state.route_resume_disabled = True
            elif step == "block_task_resume":
                state.mapping_invalidated = True
            completed.append(step)
        except Exception:  # pragma: no cover - 防御性分支，状态对象不支持写时触发
            return MappingInterruptionReceipt(
                atomic=False, execution_mode="simulator", completed_steps=tuple(completed),
                failed_step=step, audit_id=audit_id,
                error_code=MAPPING_INTERRUPTION_PARTIAL,
                message=f"测绘中断在 {step} 失败，已中止，未下发飞控动作。")
    if action in {"move_forward", "move_backward", "move_left", "move_right",
                  "move_up", "move_down", "turn_left", "turn_right"}:
        state.route_status = "interrupted"
    return MappingInterruptionReceipt(
        atomic=True, execution_mode="simulator", completed_steps=tuple(completed),
        audit_id=audit_id, message="测绘成果已作废，原任务不可恢复。", task_resumable=False,
        confirmed=True)


class SimulatorMappingInterruptionGateway:
    """模拟器 seam：完整执行六步，回执标 ``execution_mode=simulator``。"""

    execution_mode = "simulator"
    #: 模拟器不落任何真实平台副作用，也就没有平台侧幂等键可言。
    supports_idempotency_key = False

    def __init__(self, state_provider: Callable[[], Any], *, clock: Callable[[], float] | None = None):
        if not callable(state_provider):
            raise ValueError("state_provider must be callable")
        self._state = state_provider
        self._clock = clock
        self._counter = 0
        self.receipts: list[MappingInterruptionReceipt] = []

    def interrupt(self, request: MappingInterruptionRequest) -> MappingInterruptionReceipt:
        request.validate()
        self._counter += 1
        audit_id = f"sim-mapping-{self._counter}"
        if not request.confirmed:
            receipt = MappingInterruptionReceipt(
                atomic=False, execution_mode=self.execution_mode, completed_steps=(),
                audit_id=audit_id, error_code=MAPPING_INTERRUPTION_NOT_CONFIRMED,
                message="用户尚未确认成果失效，未执行任何动作。")
            self.receipts.append(receipt)
            return receipt
        receipt = apply_atomic_steps(self._state(), action=request.action, audit_id=audit_id)
        self.receipts.append(receipt)
        return receipt

    def snapshot(self) -> dict[str, Any]:
        return {"contract_version": CONTRACT_VERSION, "execution_mode": self.execution_mode,
                "operations": len(self.receipts),
                "last": self.receipts[-1].to_dict() if self.receipts else None}


def interruption_required(action: str, state: Any) -> bool:
    """与 ``flight_policy.mapping_interruption`` 同源判定，但显式排除载荷动作。"""
    from .flight_policy import MAPPING_TASKS, mapping_interruption
    if action in NON_INTERRUPTING_PAYLOAD_ACTIONS:
        return False
    payload = state if isinstance(state, dict) else None
    if payload is not None:
        return mapping_interruption(action, payload)
    return (getattr(state, "task_type", None) in MAPPING_TASKS
            and getattr(state, "mapping_invalidated", False) is not True
            and getattr(state, "route_status", None) in
            {"running", "paused", "point_flight", "point_orbit"}
            and action in MAPPING_INTERRUPTING_ACTIONS)


def audit_summary(receipts: Sequence[MappingInterruptionReceipt]) -> dict[str, Any]:
    """只保留计数与状态码，不记录航线、坐标或任务业务数据。"""
    return {"contract_version": CONTRACT_VERSION, "operations": len(receipts),
            "atomic": sum(1 for item in receipts if item.atomic),
            "blocked": sum(1 for item in receipts if not item.atomic),
            "error_codes": sorted({item.error_code for item in receipts if item.error_code})}
