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
