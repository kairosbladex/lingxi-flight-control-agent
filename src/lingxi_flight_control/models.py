"""飞行执行与任务下发的边界数据模型。

飞行执行模型保持上一版公共接口；任务下发模型只作为兼容层保留，
两者不共享业务状态，也不连接真实平台。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
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
        if not self.execution_id or not self.drone_id:
            raise ValueError("execution_id and drone_id are required")
        if self.context_version < 0:
            raise ValueError("context_version must be non-negative")
        if not self.confirmed:
            raise ValueError("flight control requires explicit confirmation")


@dataclass(frozen=True)
class RelativeMove:
    direction: str
    distance_m: float
    idempotency_key: str

    def validate(self) -> None:
        if self.direction not in {"forward", "backward", "left", "right", "up", "down"}:
            raise ValueError("unsupported relative direction")
        if not 0 < self.distance_m <= 100:
            raise ValueError("distance_m must be greater than 0 and at most 100")
        if not self.idempotency_key:
            raise ValueError("idempotency_key is required")


@dataclass(frozen=True)
class HoverAction:
    """受限悬停动作；持续时间和幂等键由外部受控工具解释。"""

    duration_s: float
    idempotency_key: str

    def validate(self) -> None:
        if not 0 < self.duration_s <= 300:
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
        except ValueError as exc:
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
    handler: Any
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

    def __post_init__(self) -> None:
        if self.executed_tool_calls is None:
            self.executed_tool_calls = []

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
        }


@dataclass(frozen=True)
class AgentRunResult:
    status: LoopStatus
    checkpoint: AgentCheckpoint
    message: str = ""
    tool_call: ToolCall | None = None
    output: Any = None
    error: Mapping[str, Any] | None = None
