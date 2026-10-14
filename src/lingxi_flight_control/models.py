"""飞行执行与任务下发的边界数据模型。

飞行执行模型保持上一版公共接口；任务下发模型只作为兼容层保留，
两者不共享业务状态，也不连接真实平台。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Any, Mapping


class CommandStatus(str, Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    STALE_CONTEXT = "stale_context"


@dataclass(frozen=True)
class Vector3:
    x_m: float = 0.0
    y_m: float = 0.0
    z_m: float = 0.0


@dataclass(frozen=True)
class ExecutionContext:
    execution_id: str
    drone_id: str
    context_version: int
    confirmed: bool

    def validate(self) -> None:
        if not isinstance(self.execution_id, str) or not self.execution_id:
            raise ValueError("execution_id and drone_id are required")
        if not isinstance(self.drone_id, str) or not self.drone_id:
            raise ValueError("execution_id and drone_id are required")
        if isinstance(self.context_version, bool) or not isinstance(self.context_version, int):
            raise ValueError("context_version must be an integer")
        if self.context_version < 0:
            raise ValueError("context_version must be non-negative")
        if self.confirmed is not True:
            raise ValueError("flight control requires explicit confirmation")

    def to_dict(self) -> dict[str, Any]:
        """Return the validated, JSON-shaped runtime binding."""
        self.validate()
        return {
            "execution_id": self.execution_id,
            "drone_id": self.drone_id,
            "context_version": self.context_version,
            "confirmed": self.confirmed,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ExecutionContext":
        if not isinstance(value, Mapping):
            raise ValueError("runtime context must be an object")
        try:
            context = cls(
                execution_id=value["execution_id"],
                drone_id=value["drone_id"],
                context_version=value["context_version"],
                confirmed=value["confirmed"],
            )
        except KeyError as exc:
            raise ValueError(f"runtime context is missing {exc.args[0]}") from exc
        context.validate()
        return context

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExecutionContext":
        return cls.from_mapping(value)


@dataclass(frozen=True)
class RelativeMove:
    direction: str
    distance_m: float
    idempotency_key: str

    def validate(self) -> None:
        if self.direction not in {"forward", "backward", "left", "right", "up", "down"}:
            raise ValueError("unsupported relative direction")
        if isinstance(self.distance_m, bool) or not isinstance(self.distance_m, (int, float)):
            raise ValueError("distance_m must be a finite number")
        try:
            finite = math.isfinite(self.distance_m)
        except (OverflowError, TypeError):
            finite = False
        if not finite or not 0 < self.distance_m <= 100:
            raise ValueError("distance_m must be greater than 0 and at most 100")
        if not self.idempotency_key:
            raise ValueError("idempotency_key is required")


@dataclass(frozen=True)
class HoverAction:
    """受限悬停动作；持续时间和幂等键由外部受控工具解释。"""

    duration_s: float
    idempotency_key: str

    def validate(self) -> None:
        if isinstance(self.duration_s, bool) or not isinstance(self.duration_s, (int, float)):
            raise ValueError("duration_s must be a finite number")
        try:
            finite = math.isfinite(self.duration_s)
        except (OverflowError, TypeError):
            finite = False
        if not finite or not 0 < self.duration_s <= 300:
            raise ValueError("duration_s must be greater than 0 and at most 300")
        if not self.idempotency_key:
            raise ValueError("idempotency_key is required")


class DispatchStatus(str, Enum):
    CONFIRMATION_REQUIRED = "confirmation_required"
    DISPATCHED = "dispatched"
    REJECTED = "rejected"


@dataclass(frozen=True)
class ToolCall:
    """由意图解释器产生的受限工具调用。"""

    name: str
    arguments: Mapping[str, Any]

    def validate(self) -> None:
        if not self.name or not self.name.replace("_", "").isalnum():
            raise ValueError("tool name must be a simple identifier")
        if not isinstance(self.arguments, Mapping):
            raise ValueError("tool arguments must be an object")


@dataclass(frozen=True)
class DispatchResult:
    status: DispatchStatus
    tool_call: ToolCall
    output: Any = None


class DecisionKind(str, Enum):
    FINAL = "final"
    CLARIFY = "clarify"
    TOOL_CALL = "tool_call"


class LoopStatus(str, Enum):
    TOOL_CALL = "tool_call"
    OBSERVATION = "observation"
    FINAL = "final"
    CLARIFICATION = "clarification"
    TOOL_ERROR = "tool_error"
    MAX_STEPS_EXCEEDED = "max_steps_exceeded"


@dataclass(frozen=True)
class ModelDecision:
    """模型只能返回受限的结构化决定，不能返回可执行函数或平台报文。"""

    kind: DecisionKind | str
    message: str = ""
    tool_call: ToolCall | None = None

    def validate(self) -> None:
        try:
            kind = DecisionKind(self.kind)
        except (TypeError, ValueError) as exc:
            raise ValueError("unsupported model decision") from exc
        if kind in {DecisionKind.FINAL, DecisionKind.CLARIFY}:
            if not self.message.strip() or self.tool_call is not None:
                raise ValueError("final or clarify requires message only")
        elif self.tool_call is None or self.message:
            raise ValueError("tool_call requires a tool call and no message")
        self.tool_call.validate() if self.tool_call else None


@dataclass(frozen=True)
class ToolParameter:
    type: str
    enum: tuple[Any, ...] | None = None
    minimum: float | None = None
    maximum: float | None = None
    exclusive_minimum: bool = False
    min_length: int | None = None


@dataclass(frozen=True)
class ToolSchema:
    properties: Mapping[str, ToolParameter]
    required: tuple[str, ...] = ()
    additional_properties: bool = False


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    schema: ToolSchema
    handler: Any = None
    side_effect: bool = False
    requires_confirmation: bool = False


@dataclass
class AgentCheckpoint:
    user_input: str
    context: Mapping[str, Any]
    messages: list[Mapping[str, Any]]
    tool_calls: list[Mapping[str, Any]]
    observations: list[Mapping[str, Any]]
    step_count: int = 0
    max_steps: int = 8
    status: LoopStatus = LoopStatus.TOOL_CALL
    confirmed: bool = False
    last_error: Mapping[str, Any] | None = None
    executed_tool_calls: list[Mapping[str, Any]] | None = None
    confirmation_binding: str | None = None
    runtime_context: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.executed_tool_calls is None:
            self.executed_tool_calls = []
        if self.max_steps <= 0:
            raise ValueError("max_steps must be positive")
        if self.runtime_context is not None:
            self.runtime_context = ExecutionContext.from_mapping(self.runtime_context).to_dict()

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_input": self.user_input,
            "context": dict(self.context),
            "messages": list(self.messages),
            "tool_calls": list(self.tool_calls),
            "observations": list(self.observations),
            "step_count": self.step_count,
            "max_steps": self.max_steps,
            "status": self.status.value,
            "confirmed": self.confirmed,
            "last_error": dict(self.last_error) if self.last_error else None,
            "executed_tool_calls": list(self.executed_tool_calls or []),
            "confirmation_binding": self.confirmation_binding,
            "runtime_context": dict(self.runtime_context) if self.runtime_context else None,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AgentCheckpoint":
        """从 JSON 对象恢复检查点；恢复后仍应重新校验上下文新鲜度。"""
        return cls(
            user_input=value["user_input"], context=dict(value.get("context", {})),
            messages=list(value.get("messages", [])), tool_calls=list(value.get("tool_calls", [])),
            observations=list(value.get("observations", [])), step_count=int(value.get("step_count", 0)),
            max_steps=int(value.get("max_steps", 8)),
            status=LoopStatus(value.get("status", LoopStatus.TOOL_CALL.value)),
            confirmed=bool(value.get("confirmed", False)), last_error=value.get("last_error"),
            executed_tool_calls=list(value.get("executed_tool_calls", [])),
            confirmation_binding=value.get("confirmation_binding"),
            runtime_context=value.get("runtime_context"),
        )


@dataclass(frozen=True)
class AgentRunResult:
    status: LoopStatus
    checkpoint: AgentCheckpoint
    message: str = ""
    tool_call: ToolCall | None = None
    output: Any = None
    error: Mapping[str, Any] | None = None
