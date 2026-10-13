"""理解用户意图并在确认后调用任务工具。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from .models import DispatchResult, DispatchStatus, ToolCall


class IntentInterpreter(Protocol):
    def __call__(self, text: str) -> ToolCall: ...


class TaskTool(Protocol):
    def __call__(self, arguments: Mapping[str, Any]) -> Any: ...


class TaskDispatchAgent:
    """把自然语言意图路由到允许的任务工具，工具由调用方注入。"""

    def __init__(self, interpreter: IntentInterpreter, tools: Mapping[str, TaskTool]):
        self._interpreter = interpreter
        self._tools = dict(tools)

    def handle(self, text: str, *, confirmed: bool = False) -> DispatchResult:
        if not text or not text.strip():
            raise ValueError("user text is required")
        tool_call = self._interpreter(text)
        tool_call.validate()
        tool = self._tools.get(tool_call.name)
        if tool is None:
            return DispatchResult(DispatchStatus.REJECTED, tool_call)
        if not confirmed:
            return DispatchResult(DispatchStatus.CONFIRMATION_REQUIRED, tool_call)
        return DispatchResult(
            DispatchStatus.DISPATCHED,
            tool_call,
            output=tool(tool_call.arguments),
        )
