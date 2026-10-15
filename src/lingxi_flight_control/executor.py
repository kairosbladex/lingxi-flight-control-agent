"""运行时执行器适配层：把驾驶舱动作接到具体执行后端。

驾驶舱的 59 个动作、参数契约与回执字段都不属于本模块——它们由
`full_control.ACTION_SPECS` 与 `FlightControlReceipt` 单一真源持有。本模块只回答
一个问题：**这些动作最终由谁执行、以什么模式执行**。

    RuntimeExecutor（协议）
      ├── SimulatorExecutor   包住 MemoryFlightControlSimulator，零行为变化
      │     └── LoopbackExecutor  可注入回环执行器，用来证明挂点真的通了
      └── FixtureExecutor     回放报文夹具；夹具里没有的请求一律拒绝

设计约束（与 `docs/platform-executor.md` 一致）：

- **execution_mode 由执行器提供。** 它必须同时出现在状态、回执和驾驶舱契约里，
  三处漂移会让 UI 与回放层互相矛盾。
- **模式名不能含安全边界禁用词。** 回执里回显的 ``execution_mode`` 会被
  `BoundedAgentLoop` 的越界内容判定检查；模式名里带 ``mqtt``/``凭据`` 这类词会让
  **每一个动作**都硬失败。因此在构建期就用同一张禁用词表把它拦下来。
- **夹具不是真机。** 回放严格按夹具内容：缺失即拒绝，录制是失败就返回失败，
  绝不合成成功回执。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import copy
import dataclasses
import json
import re
from typing import Any, Protocol, runtime_checkable

from .agent import contains_unsafe_content
from .full_control import (
    FlightControlAction,
    FlightControlActionRequest,
    FlightControlReceipt,
    FlightControlState,
    FlightControlStatus,
    MemoryFlightControlSimulator,
    RuntimeBinding,
    _status_receipt,
)
from .models import ExecutionContext

#: 夹具 JSON schema 版本；不匹配直接拒绝，避免读进来半个旧结构。
FIXTURE_SCHEMA_VERSION = 1

#: 夹具来源自证。``simulator-export`` = 由模拟器回执导出的报文形状；
#: ``platform-recording`` = 真机/MQTT 现场录制。仓库里的夹具只允许前者。
FIXTURE_SOURCES = ("simulator-export", "platform-recording")

#: 执行模式：小写简单标识符，允许 ``. _ - : /``。
_MODE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._:/-]*$")

_STATE_FIELDS = frozenset(field.name for field in dataclasses.fields(FlightControlState))
_RUNTIME_FIELDS = frozenset(field.name for field in dataclasses.fields(RuntimeBinding))
#: 记录条目允许的键；多出来的键说明夹具形状变了，宁可拒绝。
_RECORDING_FIELDS = frozenset({"action", "parameters", "status", "error_code", "message", "state"})


class FixtureError(ValueError):
    """夹具本身不合法（schema、来源、状态字段或动作名）。"""


def validate_execution_mode(mode: Any) -> str:
    """校验执行模式名，并挡住会被安全边界判为越界的模式名。

    ``execution_mode`` 会随每个回执当作工具输出回给模型，而
    `BoundedAgentLoop` 对工具输出跑 ``contains_unsafe_content``。所以像 ``mqtt-live``
    这样的模式名会让**所有**动作硬失败——错在配置，就必须在构建执行器时炸掉，
    而不是等到线上第一个动作变成 TOOL_ERROR 才发现。
    """
    if not isinstance(mode, str) or not mode.strip():
        raise ValueError("execution_mode must be a non-empty string")
    if mode != mode.strip():
        raise ValueError("execution_mode must not have surrounding whitespace")
    if not _MODE_PATTERN.fullmatch(mode):
        raise ValueError(f"execution_mode {mode!r} must be a simple lowercase token")
    if contains_unsafe_content(mode):
        raise ValueError(
            f"execution_mode {mode!r} contains a term the safety boundary forbids; it is echoed "
            "back inside every receipt, so the agent would fail closed on every action. "
            "Pick a token without credentials/vendor-protocol wording (e.g. 'live', 'recorded')."
        )
    return mode


@runtime_checkable
class RuntimeExecutor(Protocol):
    """驾驶舱动作的执行后端：预览/确认两阶段 + 状态读取。

    协议故意不含任何平台细节（连接、重试、报文编解码）。真实平台适配器只要实现
    这四个成员，就能原样塞进 `build_full_control_tool_registry`，工具契约零改动。
    """

    @property
    def execution_mode(self) -> str:
        """回执与契约里对外声明的执行模式（如 ``simulator``/``loopback``/``recorded``）。"""
        ...

    def runtime_context(self) -> ExecutionContext:
        """当前运行时绑定；循环用它做检查点过期与确认绑定校验。"""
        ...

    def preview(self, request: FlightControlActionRequest) -> FlightControlReceipt:
        """预览：校验上下文/幂等/权限，返回待确认卡或直接拒绝，绝不改变状态。"""
        ...

    def confirm(self, confirmation_id: str, *, command_id: str, idempotency_key: str,
                confirmation_binding: str, context_version: int) -> FlightControlReceipt:
        """确认：绑定校验通过后执行一次；同一确认重复提交不得重复执行。"""
        ...

    def read_state(self) -> Mapping[str, Any]:
        """读取当前平台状态（遥测/健康检查/持续状态回放用）。"""
        ...


class SimulatorExecutor:
    """把现有 `MemoryFlightControlSimulator` 包成 RuntimeExecutor。

    不传 ``execution_mode`` 时是**零行为变化**的透传层：回执、状态与契约都还是
    ``simulator``。传了就表示这个后端对外声明另一种模式（例如回环），此时状态、
    回执、契约三处的模式一起改，避免漂移。
    """

    def __init__(self, simulator: MemoryFlightControlSimulator, *,
                 execution_mode: str | None = None):
        if not isinstance(simulator, MemoryFlightControlSimulator):
            raise TypeError("SimulatorExecutor wraps MemoryFlightControlSimulator")
        if execution_mode is not None:
            simulator.state.execution_mode = validate_execution_mode(execution_mode)
        self._simulator = simulator

    @property
    def simulator(self) -> MemoryFlightControlSimulator:
        return self._simulator

    @property
    def execution_mode(self) -> str:
        return self._simulator.state.execution_mode

    def runtime_context(self) -> ExecutionContext:
        return self._simulator.runtime_context()

    def preview(self, request: FlightControlActionRequest) -> FlightControlReceipt:
        return self._simulator.preview(request)

    def confirm(self, confirmation_id: str, *, command_id: str, idempotency_key: str,
                confirmation_binding: str, context_version: int) -> FlightControlReceipt:
        return self._simulator.confirm(
            confirmation_id, command_id=command_id, idempotency_key=idempotency_key,
            confirmation_binding=confirmation_binding, context_version=context_version)

    def cancel(self, confirmation_id: str, *, command_id: str, idempotency_key: str,
               confirmation_binding: str, context_version: int) -> FlightControlReceipt:
        return self._simulator.cancel(
            confirmation_id, command_id=command_id, idempotency_key=idempotency_key,
            confirmation_binding=confirmation_binding, context_version=context_version)

    def read_state(self) -> Mapping[str, Any]:
        return dict(self._simulator.state.to_dict())


class LoopbackExecutor(SimulatorExecutor):
    """可注入回环执行器：本机闭环，用另一种模式名跑同一套握手。

    它的价值是**证明挂点真的可用**——同一份 registry、同一份握手语义，只换一个
    执行后端。它不连接任何平台，也不冒充平台：默认模式名就叫 ``loopback``。
    """

    def __init__(self, binding: RuntimeBinding, *, execution_mode: str = "loopback",
                 clock=None, ttl_sec: float = 120.0):
        super().__init__(
            MemoryFlightControlSimulator(binding, clock=clock, ttl_sec=ttl_sec),
            execution_mode=execution_mode)


def canonical_parameters(parameters: Mapping[str, Any] | None) -> str:
    """参数规范化：整数与浮点统一成浮点，保证 ``10`` 与 ``10.0`` 命中同一条录制。"""
    def normalize(value: Any) -> Any:
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, Mapping):
            return {str(key): normalize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [normalize(item) for item in value]
        return value

    return json.dumps(normalize(dict(parameters or {})), ensure_ascii=False,
                      sort_keys=True, allow_nan=False)


def state_from_recorded(recorded: Mapping[str, Any]) -> FlightControlState:
    """从录制报文里的状态快照重建 `FlightControlState`；不认识字段直接拒绝。

    必须先深拷贝：内层执行器会就地修改 ``camera``/``parameters`` 这类嵌套块，
    如果直接引用录制里的对象，回放一个动作就会改写夹具里**另一个**动作的录制内容。
    """
    if not isinstance(recorded, Mapping):
        raise FixtureError("recorded state must be an object")
    unknown = sorted(set(recorded) - _STATE_FIELDS)
    if unknown:
        raise FixtureError(f"recorded state has unknown fields: {unknown}")
    defaults = FlightControlState()
    values = {field.name: getattr(defaults, field.name)
              for field in dataclasses.fields(FlightControlState)}
    values.update(copy.deepcopy(dict(recorded)))
    try:
        return FlightControlState(**values)
    except (TypeError, ValueError) as exc:  # pragma: no cover - 仅在夹具字段类型错时触发
        raise FixtureError(f"recorded state cannot be rebuilt: {exc}") from exc


def runtime_binding_from_recorded(payload: Mapping[str, Any]) -> RuntimeBinding:
    if not isinstance(payload, Mapping):
        raise FixtureError("fixture runtime must be an object")
    missing = sorted(_RUNTIME_FIELDS - set(payload))
    unknown = sorted(set(payload) - _RUNTIME_FIELDS)
    if missing or unknown:
        raise FixtureError(f"fixture runtime fields mismatch: missing={missing} unknown={unknown}")
    try:
        binding = RuntimeBinding(
            actor_id=payload["actor_id"], project_id=payload["project_id"],
            hangar_id=payload["hangar_id"], drone_id=payload["drone_id"],
            task_batch_id=payload["task_batch_id"], context_version=payload["context_version"])
        binding.validate()
    except (TypeError, ValueError) as exc:
        raise FixtureError(f"fixture runtime binding is invalid: {exc}") from exc
    return binding

def validate_fixture(fixture: Mapping[str, Any]) -> dict[str, Any]:
    """校验夹具整体结构并返回归一化后的副本。"""
    if not isinstance(fixture, Mapping):
        raise FixtureError("fixture must be an object")
    version = fixture.get("schema_version")
    if version != FIXTURE_SCHEMA_VERSION:
        raise FixtureError(
            f"fixture schema_version must be {FIXTURE_SCHEMA_VERSION}, got {version!r}")
    source = fixture.get("source")
    if source not in FIXTURE_SOURCES:
        raise FixtureError(f"fixture source must be one of {list(FIXTURE_SOURCES)}, got {source!r}")
    execution_mode = validate_execution_mode(fixture.get("execution_mode"))
    runtime_binding_from_recorded(fixture.get("runtime"))
    recordings = fixture.get("recordings")
    if not isinstance(recordings, Sequence) or isinstance(recordings, (str, bytes)) or not recordings:
        raise FixtureError("fixture recordings must be a non-empty array")
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(recordings):
        if not isinstance(item, Mapping):
            raise FixtureError(f"recording #{index} must be an object")
        unknown = sorted(set(item) - _RECORDING_FIELDS)
        if unknown:
            raise FixtureError(f"recording #{index} has unknown fields: {unknown}")
        try:
            action = FlightControlAction(item.get("action"))
        except (TypeError, ValueError) as exc:
            raise FixtureError(f"recording #{index} has unknown action: {item.get('action')!r}") from exc
        try:
            status = FlightControlStatus(item.get("status"))
        except (TypeError, ValueError) as exc:
            raise FixtureError(f"recording #{index} has unknown status: {item.get('status')!r}") from exc
        if status not in {FlightControlStatus.ACCEPTED, FlightControlStatus.REJECTED}:
            raise FixtureError(
                f"recording #{index} status must be accepted or rejected, got {status.value!r}")
        state = state_from_recorded(item.get("state"))
        if state.execution_mode != execution_mode:
            raise FixtureError(
                f"recording #{index} state.execution_mode is {state.execution_mode!r}, but the "
                f"fixture declares {execution_mode!r}; state and receipts must agree")
        key = (action.value, canonical_parameters(item.get("parameters")))
        if key in seen:
            raise FixtureError(f"recording #{index} duplicates {key[0]} with the same parameters")
        seen.add(key)
    return dict(fixture)


def load_fixture(path: str) -> dict[str, Any]:
    """读取并校验夹具 JSON；任何结构问题都以 `FixtureError` 失败。"""
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError as exc:
        raise FixtureError(f"fixture file not found: {path}") from exc
    except (OSError, ValueError) as exc:
        raise FixtureError(f"fixture file is not valid JSON: {exc}") from exc
    return validate_fixture(payload)


def export_fixture(binding: RuntimeBinding, sequence: Iterable[tuple[Any, Mapping[str, Any]]], *,
                   execution_mode: str = "recorded", source: str = "simulator-export",
                   clock=None) -> dict[str, Any]:
    """用模拟器把一串动作跑一遍，把回执导出成夹具文件内容。

    **这不是真机录制。** 导出的报文形状与驾驶舱回执契约一致，用于在没有真机/凭据
    的环境里验证适配层；``source`` 会如实写成 ``simulator-export``。接入真机后，
    用真实平台报文替换 ``recordings`` 并把 ``source`` 改成 ``platform-recording``。
    """
    if source not in FIXTURE_SOURCES:
        raise FixtureError(f"source must be one of {list(FIXTURE_SOURCES)}, got {source!r}")
    execution_mode = validate_execution_mode(execution_mode)
    simulator = MemoryFlightControlSimulator(binding, clock=clock)
    recordings: list[dict[str, Any]] = []
    for index, (action, parameters) in enumerate(sequence, start=1):
        request = FlightControlActionRequest(
            command_id=f"fixture-{index}", idempotency_key=f"fixture-key-{index}",
            action=action, parameters=dict(parameters), context_version=binding.context_version)
        receipt = simulator.preview(request)
        if receipt.status is FlightControlStatus.PREVIEW_REQUIRED:
            receipt = simulator.confirm(
                receipt.confirmation_id, command_id=request.command_id,
                idempotency_key=request.idempotency_key,
                confirmation_binding=receipt.confirmation_binding,
                context_version=binding.context_version)
        recordings.append({
            "action": FlightControlAction(request.action).value,
            "parameters": dict(parameters),
            "status": receipt.status.value,
            "error_code": receipt.error_code,
            "message": receipt.message,
            # 状态里的 execution_mode 跟着夹具声明走：状态、回执、契约三处必须一致。
            "state": {**dict(receipt.state), "execution_mode": execution_mode},
        })
    return {
        "schema_version": FIXTURE_SCHEMA_VERSION,
        "source": source,
        "execution_mode": execution_mode,
        "runtime": binding.to_dict(),
        "recordings": recordings,
    }


class FixtureExecutor:
    """回放报文夹具的执行器。夹具里没有的请求一律拒绝，绝不合成结果。

    两阶段握手（上下文版本、幂等键、确认绑定、TTL、控制权）**不再重写一遍**，而是
    委托给内部 `MemoryFlightControlSimulator`——那套语义是单一真源，复制一份就会漂移。
    本执行器只做两件事：

    1. 预览时要求夹具里存在对应录制；没有就撤掉待确认卡并返回
       ``FIXTURE_NOT_FOUND``，于是 ``confirm`` 结构上不可能跑到没有依据的动作；
    2. 确认时**状态与回执都以录制报文为准**（``accepted`` 用录制的状态快照覆盖内层
       状态；非 ``accepted`` 回滚内层状态），不做任何本地合成。
    """

    def __init__(self, fixture: Mapping[str, Any], *, clock=None, ttl_sec: float = 120.0):
        validated = validate_fixture(fixture)
        self._fixture = validated
        self._execution_mode = validated["execution_mode"]
        self._source = validated["source"]
        self._binding = runtime_binding_from_recorded(validated["runtime"])
        self._recordings = {
            (FlightControlAction(item["action"]).value,
             canonical_parameters(item.get("parameters"))): dict(item)
            for item in validated["recordings"]
        }
        self._pending_recordings: dict[str, dict[str, Any]] = {}
        self._inner = MemoryFlightControlSimulator(self._binding, clock=clock, ttl_sec=ttl_sec)
        self._inner.state.execution_mode = self._execution_mode

    @property
    def execution_mode(self) -> str:
        return self._execution_mode

    @property
    def fixture_source(self) -> str:
        """夹具来源：``simulator-export`` 或 ``platform-recording``，用于对外自证。"""
        return self._source

    def runtime_context(self) -> ExecutionContext:
        return self._inner.runtime_context()

    def read_state(self) -> Mapping[str, Any]:
        return copy.deepcopy(dict(self._inner.state.to_dict()))

    def _lookup(self, request: FlightControlActionRequest) -> dict[str, Any] | None:
        key = (FlightControlAction(request.action).value, canonical_parameters(request.parameters))
        return self._recordings.get(key)

    def preview(self, request: FlightControlActionRequest) -> FlightControlReceipt:
        request.validate()
        recording = self._lookup(request)
        receipt = self._inner.preview(request)
        if receipt.status is not FlightControlStatus.PREVIEW_REQUIRED:
            return receipt
        if recording is None:
            # 先撤掉待确认卡：没有录制就不该存在可确认的卡。
            self._inner.cancel(
                receipt.confirmation_id, command_id=request.command_id,
                idempotency_key=request.idempotency_key,
                confirmation_binding=receipt.confirmation_binding,
                context_version=self._inner.runtime_context().context_version)
            return _status_receipt(
                FlightControlStatus.REJECTED, request, self._binding, self._inner.state,
                error_code="FIXTURE_NOT_FOUND",
                message="夹具中没有该动作的录制报文，拒绝执行（回放不会合成结果）")
        self._pending_recordings[receipt.confirmation_id] = recording
        return receipt

    def confirm(self, confirmation_id: str, *, command_id: str, idempotency_key: str,
                confirmation_binding: str, context_version: int) -> FlightControlReceipt:
        recorded = self._pending_recordings.pop(confirmation_id, None)
        before = copy.deepcopy(self._inner.state)
        receipt = self._inner.confirm(
            confirmation_id, command_id=command_id, idempotency_key=idempotency_key,
            confirmation_binding=confirmation_binding, context_version=context_version)
        if receipt.status is not FlightControlStatus.ACCEPTED or recorded is None:
            # 握手没过（绑定不符/过期/重复/上下文过期）：保持内层语义，原样返回。
            self._inner.state = before
            return receipt
        status = FlightControlStatus(recorded["status"])
        if status is not FlightControlStatus.ACCEPTED:
            # 录制就是失败：状态回滚，绝不把拒绝升级成成功。
            self._inner.state = before
        else:
            # 录制是唯一真源：状态以录制快照为准。
            self._inner.state = state_from_recorded(recorded["state"])
        return FlightControlReceipt(
            status=status,
            execution_mode=self._execution_mode,
            action=receipt.action,
            command_id=command_id,
            idempotency_key=idempotency_key,
            confirmation_id=confirmation_id,
            confirmation_binding=receipt.confirmation_binding,
            context_version=receipt.context_version,
            state=copy.deepcopy(dict(recorded["state"])),
            error_code=recorded.get("error_code"),
            message=recorded.get("message", ""),
        )

    def cancel(self, confirmation_id: str, *, command_id: str, idempotency_key: str,
               confirmation_binding: str, context_version: int) -> FlightControlReceipt:
        self._pending_recordings.pop(confirmation_id, None)
        return self._inner.cancel(
            confirmation_id, command_id=command_id, idempotency_key=idempotency_key,
            confirmation_binding=confirmation_binding, context_version=context_version)


def coerce_executor(backend: Any) -> RuntimeExecutor:
    """把入参归一成 RuntimeExecutor。

    裸 `MemoryFlightControlSimulator` 继续被接受（包成 `SimulatorExecutor`），
    这样既有调用点零改动；其它形状一律 `TypeError`，不做鸭子类型猜测。
    """
    if isinstance(backend, RuntimeExecutor):
        return backend
    if isinstance(backend, MemoryFlightControlSimulator):
        return SimulatorExecutor(backend)
    raise TypeError(
        "executor must implement RuntimeExecutor "
        "(execution_mode / runtime_context / preview / confirm / read_state) "
        f"or be a MemoryFlightControlSimulator, got {type(backend).__name__}")
