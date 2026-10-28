"""结构化遥测：把内存读数变成可 grep 的事件流。

设计取舍：

- **事件字段名是契约。** 五个内置事件（``decide``/``compressed``/``fail_closed``/
  ``unknown_tool``/``stale_context``）的字段集合在 `EVENT_FIELDS` 里集中定义，
  文档见 ``docs/observability.md``。字段集合不符的事件会被计数并丢弃（不是抛错）。
- **遥测绝不影响决策。** sink 抛异常、字段不合契约、值不可 JSON 序列化——一律吞掉并
  计数（``sink_errors``/``schema_errors``），绝不向上传播，也绝不改变任何返回结果。
  这不是「顺手加个 try」，而是硬性要求：fail-closed 语义不能因为日志后端挂了而改变。
- **默认安静，默认 sink 是 stdout JSON line。** 不注入 ``telemetry`` 时循环与解码器
  一个事件都不发（库不应该意外往 stdout 写东西）；`Telemetry()` 不传 sink 时用
  `StdoutSink`。
- **不写时间戳。** 时间戳会破坏「同一次 run 的事件序列可精确复现」。需要时间戳的
  调用方自己包一层 sink（把 ``time.time()`` 加进去）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, TextIO
import json
import sys

TELEMETRY_SCHEMA_VERSION = 1

EVENT_DECIDE = "decide"
EVENT_COMPRESSED = "compressed"
EVENT_FAIL_CLOSED = "fail_closed"
EVENT_UNKNOWN_TOOL = "unknown_tool"
EVENT_STALE_CONTEXT = "stale_context"

#: 每个内置事件的字段契约（含 ``schema_version`` 与 ``event``）。稳定，别改字段名。
EVENT_FIELDS: Mapping[str, tuple[str, ...]] = {
    EVENT_DECIDE: (
        "schema_version", "event", "attempt", "kind", "tool", "empty_response",
        "prompt_estimate", "prompt_budget", "num_ctx", "output_reserve",
        "budget_used_ratio", "near_budget", "omitted_observations", "compressed",
        "done_reason", "eval_count", "prompt_eval_count", "generation_truncated",
    ),
    EVENT_COMPRESSED: (
        "schema_version", "event", "omitted_observations", "kept_observations",
        "omission_mode", "prompt_estimate", "prompt_budget", "num_ctx", "output_reserve",
        "compressed_payloads", "omitted_observation_turns",
    ),
    EVENT_FAIL_CLOSED: ("schema_version", "event", "reason", "message", "tool", "step"),
    EVENT_UNKNOWN_TOOL: ("schema_version", "event", "name", "message", "step"),
    EVENT_STALE_CONTEXT: ("schema_version", "event", "reason", "message", "step"),
}

#: ``fail_closed.reason`` 的稳定词表：一个取值对应一条硬失败代码路径。
FAIL_CLOSED_REASONS = (
    "model_error",            # 模型返回不可用响应，或输出越过安全边界
    "invalid_arguments",      # 工具参数不是 JSON 对象 / schema 本身有问题 / 无法安全绑定
    "unknown_tool",           # 模型调用了白名单外的工具
    "unsafe_output",          # 工具回执含越界内容或不可序列化
    "runtime_context_error",  # 运行时上下文非法
    "checkpoint_mismatch",    # 检查点的 user_input 与本次请求不一致
    "tool_exception",         # handler 抛异常（含执行器异常）
    "stale_context",          # 检查点绑定的运行时上下文已过期
    "tool_error",             # 其它硬失败（保守兜底）
)

#: 压缩时省略说明的降级档位，从信息量最多到最少。
OMISSION_MODES = ("summary", "compact", "none")

#: 事件的枚举字段约束（取值必须落在词表里，否则算协议违约）。
EVENT_ENUMS: Mapping[str, Mapping[str, tuple[str, ...]]] = {
    EVENT_FAIL_CLOSED: {"reason": FAIL_CLOSED_REASONS},
    EVENT_STALE_CONTEXT: {"reason": ("stale_context",)},
    EVENT_COMPRESSED: {"omission_mode": OMISSION_MODES},
}

#: 事件里的消息字段截断长度：一条超长模型/平台输出不该把日志打爆。
MAX_MESSAGE_CHARS = 500

#: 默认「接近预算」阈值：30720 的 90% = 27648。实测峰值 9169（约 30%），
#: 所以 90% 意味着真的快撞墙了，不是正常波动。
DEFAULT_NEAR_BUDGET_RATIO = 0.9


@dataclass(frozen=True)
class TelemetryEvent:
    """一个遥测事件：``name`` + 稳定字段集合。"""

    name: str
    fields: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": TELEMETRY_SCHEMA_VERSION, "event": self.name, **self.fields}


class EventSink(Protocol):  # pragma: no cover - 结构声明
    def __call__(self, event: TelemetryEvent) -> None: ...


class StdoutSink:
    """默认 sink：一行一个 JSON 对象，``sort_keys`` 保证同一事件字节级可复现。"""

    def __init__(self, stream: TextIO | None = None):
        self._stream = stream

    def __call__(self, event: TelemetryEvent) -> None:
        target = self._stream if self._stream is not None else sys.stdout
        target.write(json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True,
                                default=str) + "\n")
        target.flush()


class MemorySink:
    """把事件留在内存里，供测试与断言使用。"""

    def __init__(self) -> None:
        self.events: list[TelemetryEvent] = []

    def __call__(self, event: TelemetryEvent) -> None:
        self.events.append(event)

    @property
    def names(self) -> list[str]:
        return [event.name for event in self.events]

    def dicts(self) -> list[dict[str, Any]]:
        return [event.to_dict() for event in self.events]


def _truncate(value: Any) -> str:
    text = value if isinstance(value, str) else str(value)
    return text[:MAX_MESSAGE_CHARS]


class Telemetry:
    """事件发射器：字段校验 + sink 隔离 + 计数。

    校验失败或 sink 抛异常都只会增加计数，不会抛错、不会改变调用方结果。
    """

    def __init__(self, sink: EventSink | None = None, *, enabled: bool = True,
                 near_budget_ratio: float = DEFAULT_NEAR_BUDGET_RATIO):
        if sink is not None and not callable(sink):
            raise ValueError("telemetry sink must be callable")
        if isinstance(near_budget_ratio, bool) or not isinstance(near_budget_ratio, (int, float)):
            raise ValueError("near_budget_ratio must be a number in (0, 1]")
        if not 0 < float(near_budget_ratio) <= 1:
            raise ValueError("near_budget_ratio must be in (0, 1]")
        self._sink: EventSink = sink if sink is not None else StdoutSink()
        self._enabled = bool(enabled)
        self._near_budget_ratio = float(near_budget_ratio)
        #: 交付给 emit 的事件数（含被校验拦下的）。
        self.submitted = 0
        #: 真正写进 sink 的事件数。
        self.emitted = 0
        #: sink 抛异常的次数——它不为 0 说明日志后端有问题，但决策不受影响。
        self.sink_errors = 0
        #: 字段契约不满足的事件数（编程错误，不会到 sink）。
        self.schema_errors = 0
        #: emit 内部意外异常的次数。理论上应该恒为 0；留这个计数器是为了让
        #: 「遥测绝不影响决策」这条硬约束在最坏情况下依然成立且可观测。
        self.internal_errors = 0

    @classmethod
    def disabled(cls) -> "Telemetry":
        """不产生任何事件的遥测（库默认）。"""
        return cls(enabled=False)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def sink(self) -> EventSink:
        return self._sink

    @property
    def near_budget_ratio(self) -> float:
        return self._near_budget_ratio

    def counters(self) -> dict[str, int]:
        return {"submitted": self.submitted, "emitted": self.emitted,
                "sink_errors": self.sink_errors, "schema_errors": self.schema_errors,
                "internal_errors": self.internal_errors}

    def emit(self, event_name: str, /, **fields: Any) -> None:
        """发射一个事件。任何内部问题都只记数，绝不抛给调用方。

        ``event_name`` 是**位置-only**参数：否则字段里出现 ``name``/``event_name``
        这样的键就会和形参撞名，把 TypeError 抛进决策路径——那正是遥测最不该做的事。
        整个函数体也在 try 里：契约违约、字段撞名、sink 异常一律记账不抛出。
        """
        if not self._enabled:
            return
        self.submitted += 1
        try:
            contract = EVENT_FIELDS.get(event_name)
            event = TelemetryEvent(
                event_name, {"schema_version": TELEMETRY_SCHEMA_VERSION, **fields})
            if contract is not None:
                if set(event.to_dict()) != set(contract):
                    self.schema_errors += 1
                    return
                for field_name, allowed in EVENT_ENUMS.get(event_name, {}).items():
                    if fields.get(field_name) not in allowed:
                        self.schema_errors += 1
                        return
        except Exception:  # 构造/校验本身出问题也只记数
            self.internal_errors += 1
            return
        self._deliver(event)

    def _deliver(self, event: TelemetryEvent) -> None:
        try:
            self._sink(event)
        except Exception:  # 遥测后端绝不能影响决策
            self.sink_errors += 1
            return
        self.emitted += 1

    # ---- 语义化入口：字段名在这里集中定义 -------------------------------------

    def decide(self, *, attempt: int, kind: str, tool: str | None, empty_response: bool,
               prompt_estimate: int | None, prompt_budget: int, num_ctx: int,
               output_reserve: int, omitted_observations: int,
               generation: Mapping[str, Any] | None = None) -> None:
        """一次模型决策请求的结果（预算 + 生成读数）。"""
        generation = generation or {}
        ratio = None
        if prompt_estimate is not None and prompt_budget > 0:
            ratio = round(prompt_estimate / prompt_budget, 4)
        done_reason = generation.get("done_reason")
        self.emit(
            EVENT_DECIDE,
            attempt=int(attempt), kind=str(kind), tool=tool,
            empty_response=bool(empty_response),
            prompt_estimate=None if prompt_estimate is None else int(prompt_estimate),
            prompt_budget=int(prompt_budget), num_ctx=int(num_ctx),
            output_reserve=int(output_reserve),
            budget_used_ratio=ratio,
            near_budget=bool(ratio is not None and ratio >= self._near_budget_ratio),
            omitted_observations=int(omitted_observations),
            compressed=bool(omitted_observations),
            done_reason=done_reason,
            eval_count=generation.get("eval_count"),
            prompt_eval_count=generation.get("prompt_eval_count"),
            generation_truncated=done_reason == "length",
        )

    def compressed(self, *, omitted_observations: int, kept_observations: int,
                   omission_mode: str, prompt_estimate: int, prompt_budget: int,
                   num_ctx: int, output_reserve: int, compressed_payloads: int,
                   omitted_observation_turns: int) -> None:
        """预算闸门真的压了观察时发一次（``omission_mode`` 说明压到什么档）。"""
        self.emit(
            EVENT_COMPRESSED,
            omitted_observations=int(omitted_observations),
            kept_observations=int(kept_observations),
            omission_mode=str(omission_mode),
            prompt_estimate=int(prompt_estimate), prompt_budget=int(prompt_budget),
            num_ctx=int(num_ctx), output_reserve=int(output_reserve),
            compressed_payloads=int(compressed_payloads),
            omitted_observation_turns=int(omitted_observation_turns),
        )

    def fail_closed(self, *, reason: str, message: Any, tool: str | None = None,
                    step: int = 0) -> None:
        """任何硬失败的总闸门事件；``reason`` 取自 `FAIL_CLOSED_REASONS`。"""
        self.emit(EVENT_FAIL_CLOSED, reason=str(reason), message=_truncate(message),
                  tool=tool, step=int(step))

    def unknown_tool(self, *, name: str, message: Any = "", step: int = 0) -> None:
        """模型调用白名单外工具的专用事件（fail_closed 之外额外发一条）。"""
        self.emit(EVENT_UNKNOWN_TOOL, name=str(name), message=_truncate(message), step=int(step))

    def stale_context(self, *, reason: str, message: Any, step: int = 0) -> None:
        """检查点绑定的运行时上下文过期（fail_closed 之外额外发一条）。"""
        self.emit(EVENT_STALE_CONTEXT, reason=str(reason), message=_truncate(message),
                  step=int(step))
