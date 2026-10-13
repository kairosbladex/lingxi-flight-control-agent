"""理解用户意图并在确认后调用任务工具。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from .models import DispatchResult, DispatchStatus, ExecutionContext, RelativeMove, ToolCall


def parse_relative_move(text: str, *, idempotency_key: str) -> RelativeMove:
    """把单个受限中文控制句解析为相对移动。

    这里只解析方向和距离；执行上下文、确认、幂等和设备状态由执行层负责。
    """
    import re

    if not text or not text.strip():
        raise ValueError("control text is required")
    match = re.fullmatch(
        r"\s*(?:向?前|前进|往前)\s*(?P<distance>\d+(?:\.\d+)?)\s*米\s*", text
    )
    direction = "forward"
    if not match:
        patterns = {
            "backward": r"(?:向?后|后退|往后)",
            "left": r"(?:向?左|左移)",
            "right": r"(?:向?右|右移)",
            "up": r"(?:上升|向?上)",
            "down": r"(?:下降|向?下)",
        }
        for candidate, prefix in patterns.items():
            match = re.fullmatch(rf"\s*{prefix}\s*(?P<distance>\d+(?:\.\d+)?)\s*米\s*", text)
            if match:
                direction = candidate
                break
    if not match:
        raise ValueError("expected one direction and distance in meters")
    move = RelativeMove(direction, float(match.group("distance")), idempotency_key)
    move.validate()
    return move


class FlightExecutionAgent:
    """飞行执行文本闭环：解析后交给已绑定的内存/平台执行器。"""

    def __init__(self, context: ExecutionContext, executor: Any):
        context.validate()
        self._context = context
        self._executor = executor

    def handle(self, text: str, *, idempotency_key: str):
        move = parse_relative_move(text, idempotency_key=idempotency_key)
        return self._executor.relative_move(self._context, move)


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
