"""无外部副作用的飞行执行模拟器。"""

from __future__ import annotations

from .models import CommandStatus, ExecutionContext, RelativeMove, Vector3


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
