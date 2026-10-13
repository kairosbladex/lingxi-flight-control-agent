"""仅内存、仅回环开发使用的飞行控制模拟器。"""

from __future__ import annotations

from dataclasses import dataclass

from .models import CommandStatus, ExecutionContext, RelativeMove, Vector3


@dataclass(frozen=True)
class CommandResult:
    status: CommandStatus
    position: Vector3


class MemoryFlightSimulator:
    """拒绝未确认、过期和重复命令，不产生外部副作用。"""

    def __init__(self, context: ExecutionContext):
        context.validate()
        self.context = context
        self.position = Vector3()
        self._seen: set[str] = set()

    def relative_move(self, context: ExecutionContext, move: RelativeMove) -> CommandResult:
        move.validate()
        context.validate()
        if context != self.context:
            return CommandResult(CommandStatus.STALE_CONTEXT, self.position)
        if move.idempotency_key in self._seen:
            return CommandResult(CommandStatus.DUPLICATE, self.position)
        self._seen.add(move.idempotency_key)
        delta = {
            "forward": Vector3(y_m=move.distance_m),
            "backward": Vector3(y_m=-move.distance_m),
            "left": Vector3(x_m=-move.distance_m),
            "right": Vector3(x_m=move.distance_m),
            "up": Vector3(z_m=move.distance_m),
            "down": Vector3(z_m=-move.distance_m),
        }[move.direction]
        self.position = Vector3(
            self.position.x_m + delta.x_m,
            self.position.y_m + delta.y_m,
            self.position.z_m + delta.z_m,
        )
        return CommandResult(CommandStatus.ACCEPTED, self.position)


def main() -> int:
    context = ExecutionContext("sim-execution", "sim-drone", 1, True)
    simulator = MemoryFlightSimulator(context)
    result = simulator.relative_move(context, RelativeMove("forward", 10, "sim-1"))
    print(f"{result.status.value}: position={result.position}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
