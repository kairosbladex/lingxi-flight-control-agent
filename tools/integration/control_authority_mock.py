"""控制权中台 mock：obtain / release / 抢占 / 过期 / 心跳续租。

真实平台的控制权是带心跳的数据库记录（``updateTime + 300s`` 清理），
HTTP 读取本身不续租，MQTT 心跳才更新。这里用纯内存状态机复刻这套语义，
只服务离线断言；它**不**申请任何真实控制权。

用法：

    authority = ControlAuthorityMock(now=1000.0)
    authority.obtain('actor-1')          # 获得控制权，租约 = now + 300
    authority.heartbeat('actor-1')       # 续租（模拟 MQTT 心跳）
    authority.tick(301)                  # 时间流逝，租约过期
    authority.snapshot_fields()          # 供权威快照 mock 填写控制权字段
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any


class ControlAuthorityError(ValueError):
    """控制权状态机的 typed 拒绝；调用方按 code 分支，不解析文案。"""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


DEFAULT_LEASE_SEC = 300.0


@dataclass
class ControlAuthorityMock:
    """进程内控制权账本；时间由 ``now`` 显式推进，不读系统时钟。"""

    now: float
    lease_sec: float = DEFAULT_LEASE_SEC
    owner: str | None = None
    updated_at: float | None = None
    version: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if type(self.now) not in (int, float) or self.now < 0:
            raise ValueError('now must be a non-negative Unix second')
        if type(self.lease_sec) not in (int, float) or self.lease_sec <= 0:
            raise ValueError('lease_sec must be positive')

    # -- 读取 ---------------------------------------------------------
    @property
    def lease_expires_at(self) -> float | None:
        if self.owner is None or self.updated_at is None:
            return None
        return float(self.updated_at) + float(self.lease_sec)

    def valid(self, actor: str | None = None) -> bool:
        """租约存在、未过期；给了 ``actor`` 时还要求 owner 一致。"""
        if self.owner is None or self.lease_expires_at is None:
            return False
        if actor is not None and self.owner != actor:
            return False
        return self.lease_expires_at > self.now

    def snapshot_fields(self) -> dict[str, Any]:
        return {
            'control_owner_employee_id': self.owner,
            'control_owner_updated_at': self.updated_at,
            'control_lease_expires_at': self.lease_expires_at,
            'control_owner_version': self.version,
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            'now': self.now,
            'owner': self.owner,
            'updated_at': self.updated_at,
            'lease_expires_at': self.lease_expires_at,
            'valid': self.valid(),
            'events': list(self.events),
        }

    # -- 写 -----------------------------------------------------------
    def obtain(self, actor: str) -> dict[str, Any]:
        """新申请或同一 owner 续租；被他人持有时拒绝，必须先 release/抢占。"""
        self._require_actor(actor)
        if self.owner is not None and self.owner != actor:
            raise ControlAuthorityError('CONTROL_HELD_BY_OTHER')
        return self._grant(actor, reason='obtain' if self.owner is None else 'renew')

    def heartbeat(self, actor: str) -> dict[str, Any]:
        """MQTT 心跳续租；只有当前 owner 能续。"""
        self._require_actor(actor)
        if self.owner != actor:
            raise ControlAuthorityError('CONTROL_NOT_OWNED')
        return self._grant(actor, reason='heartbeat')

    def preempt(self, actor: str) -> dict[str, Any]:
        """抢占：真实平台里任务创建者可能自动夺权，这里显式建模。"""
        self._require_actor(actor)
        previous = self.owner
        result = self._grant(actor, reason='preempt')
        result['previous_owner'] = previous
        return result

    def release(self, actor: str) -> dict[str, Any]:
        """释放控制权；非 owner 不能替别人释放。"""
        self._require_actor(actor)
        if self.owner != actor:
            raise ControlAuthorityError('CONTROL_NOT_OWNED')
        previous = self.owner
        self.owner = None
        self.updated_at = None
        self.version = None
        self._record('release', actor, previous_owner=previous)
        return {'owner': None, 'previous_owner': previous}

    def expire(self) -> dict[str, Any]:
        """模拟平台清理任务：租约过期后清空 owner（真实平台按 300s 扫描）。"""
        if self.lease_expires_at is None or self.lease_expires_at > self.now:
            return {'expired': False, 'owner': self.owner}
        previous = self.owner
        self.owner = None
        self.updated_at = None
        self.version = None
        self._record('expire', None, previous_owner=previous)
        return {'expired': True, 'previous_owner': previous}

    def tick(self, seconds: float) -> float:
        """推进 mock 时间，返回新的 now。"""
        if type(seconds) not in (int, float) or seconds < 0:
            raise ValueError('seconds must be non-negative')
        self.now = float(self.now) + float(seconds)
        return self.now

    # -- 内部 ---------------------------------------------------------
    def _grant(self, actor: str, *, reason: str) -> dict[str, Any]:
        self.owner = actor
        self.updated_at = float(self.now)
        digest = hashlib.sha256(f'{actor}:{self.updated_at}'.encode('utf-8')).hexdigest()
        self.version = digest
        self._record(reason, actor)
        return {
            'owner': self.owner,
            'updated_at': self.updated_at,
            'lease_expires_at': self.lease_expires_at,
            'version': self.version,
        }

    def _record(self, kind: str, actor: str | None, **extra: Any) -> None:
        self.events.append({'kind': kind, 'actor': actor, 'at': float(self.now), **extra})

    @staticmethod
    def _require_actor(actor: str) -> None:
        if not isinstance(actor, str) or not actor.strip() or len(actor) > 256:
            raise ControlAuthorityError('INVALID_ACTOR')
