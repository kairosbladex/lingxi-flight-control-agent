"""无外部副作用的飞行执行模拟器。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .agent import ToolRegistry
from .models import (
    CommandStatus,
    ExecutionContext,
    HoverAction,
    RelativeMove,
    ToolDefinition,
    ToolParameter,
    ToolSchema,
    Vector3,
)


class ExecutionResult:
    def __init__(self, status: CommandStatus, position: Vector3):
        self.status = status
        self.position = position


class MemoryFlightSimulator:
    """按执行上下文执行相对移动，并提供幂等和过期保护。"""

    def __init__(self, context: ExecutionContext):
        context.validate()
        self.context = context
        self.position = Vector3()
        self._seen_keys: set[str] = set()

    def relative_move(self, context: ExecutionContext, move: RelativeMove) -> ExecutionResult:
        move.validate()
        if context != self.context:
            return ExecutionResult(CommandStatus.STALE_CONTEXT, self.position)
        if move.idempotency_key in self._seen_keys:
            return ExecutionResult(CommandStatus.DUPLICATE, self.position)
        self._seen_keys.add(move.idempotency_key)
        deltas = {
            "forward": (0, move.distance_m, 0),
            "backward": (0, -move.distance_m, 0),
            "left": (-move.distance_m, 0, 0),
            "right": (move.distance_m, 0, 0),
            "up": (0, 0, move.distance_m),
            "down": (0, 0, -move.distance_m),
        }
        dx, dy, dz = deltas[move.direction]
        self.position = Vector3(
            self.position.x_m + dx,
            self.position.y_m + dy,
            self.position.z_m + dz,
        )
        return ExecutionResult(CommandStatus.ACCEPTED, self.position)

    def hover(self, context: ExecutionContext, action: HoverAction) -> ExecutionResult:
        """在当前位置悬停指定时长；动作本身不改变坐标。"""
        action.validate()
        if context != self.context:
            return ExecutionResult(CommandStatus.STALE_CONTEXT, self.position)
        if action.idempotency_key in self._seen_keys:
            return ExecutionResult(CommandStatus.DUPLICATE, self.position)
        self._seen_keys.add(action.idempotency_key)
        return ExecutionResult(CommandStatus.ACCEPTED, self.position)

    def tool_registry(self) -> ToolRegistry:
        """返回只绑定本模拟器的两个安全、可序列化工具。"""
        return build_memory_flight_tool_registry(self)


def _execution_observation(result: ExecutionResult) -> dict[str, Any]:
    return {
        "status": result.status.value,
        "position": {
            "x_m": result.position.x_m,
            "y_m": result.position.y_m,
            "z_m": result.position.z_m,
        },
    }


def build_memory_flight_tool_registry(simulator: MemoryFlightSimulator) -> ToolRegistry:
    """把模拟器能力注册成 Runtime 可调用的白名单工具。

    handlers 捕获已校验的执行上下文，模型只能看到工具描述，不能获得 Python handler。
    """
    context = simulator.context
    context.validate()

    def relative_move(arguments: Mapping[str, Any]) -> dict[str, Any]:
        move = RelativeMove(
            direction=arguments["direction"],
            distance_m=arguments["distance_m"],
            idempotency_key=arguments["idempotency_key"],
        )
        return _execution_observation(simulator.relative_move(context, move))

    def hover(arguments: Mapping[str, Any]) -> dict[str, Any]:
        action = HoverAction(
            duration_s=arguments["duration_s"],
            idempotency_key=arguments["idempotency_key"],
        )
        return _execution_observation(simulator.hover(context, action))

    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="relative_move",
        description="在已绑定的内存飞行模拟器中执行一次受限相对移动。",
        schema=ToolSchema(
            properties={
                "direction": ToolParameter(
                    "string", enum=("forward", "backward", "left", "right", "up", "down")
                ),
                "distance_m": ToolParameter("number", minimum=0, maximum=100, exclusive_minimum=True),
                "idempotency_key": ToolParameter("string", min_length=1),
            },
            required=("direction", "distance_m", "idempotency_key"),
        ),
        handler=relative_move,
    ))
    registry.register(ToolDefinition(
        name="hover",
        description="在当前位置执行指定时长的受限内存悬停动作。",
        schema=ToolSchema(
            properties={
                "duration_s": ToolParameter("number", minimum=0, maximum=300, exclusive_minimum=True),
                "idempotency_key": ToolParameter("string", min_length=1),
            },
            required=("duration_s", "idempotency_key"),
        ),
        handler=hover,
    ))
    return registry
