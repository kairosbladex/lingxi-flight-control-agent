"""与平台无关的飞行控制数据模型。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


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
