"""任务下发 Agent 的最小数据模型。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


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
