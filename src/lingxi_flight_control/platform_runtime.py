"""服务端平台绑定与装配；配置中没有自动接管或自动启用写操作。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
import os
import time

from .full_control import RuntimeBinding
from .ktd_platform import KtdHttpTransport, _identifier


@dataclass(frozen=True)
class PlatformRuntimeConfig:
    base_url: str
    binding: RuntimeBinding
    site_id: str
    task_id: str | None
    journal_path: Path
    server_token: str = field(repr=False)
    gateway_token: str = field(repr=False)
    allow_insecure_http: bool = False

    def __post_init__(self):
        self.binding.validate()
        identities = (self.binding.actor_id, self.binding.project_id, self.binding.hangar_id,
                      self.binding.drone_id, self.binding.task_batch_id, self.site_id)
        if not all(_identifier(value) for value in identities):
            raise ValueError('平台资源标识无效')
        if self.task_id is not None and not _identifier(self.task_id):
            raise ValueError('KTD_TASK_ID 无效')
        if type(self.allow_insecure_http) is not bool:
            raise ValueError('KTD_ALLOW_INSECURE_HTTP 必须为布尔值')
        if (not isinstance(self.gateway_token, str) or not 16 <= len(self.gateway_token) <= 8192
                or any(ord(value) <= 32 or ord(value) >= 127 for value in self.gateway_token)):
            raise ValueError('FLIGHT_AGENT_GATEWAY_TOKEN 无效')
        if not isinstance(self.journal_path, Path) or not self.journal_path.is_absolute():
            raise ValueError('FLIGHT_AGENT_JOURNAL 必须为绝对路径')
        # 只验证固定服务端地址及凭据格式；构造传输对象不发送网络请求。
        KtdHttpTransport(self.base_url, token=self.server_token, actor_id=self.binding.actor_id,
                         allow_insecure_http=self.allow_insecure_http)

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> PlatformRuntimeConfig:
        def required(name):
            value = environment.get(name)
            if not isinstance(value, str) or not value:
                raise ValueError(f'缺少服务端配置 {name}')
            return value

        raw_version = environment.get('FLIGHT_AGENT_CONTEXT_VERSION', '1')
        if not isinstance(raw_version, str) or not raw_version.isascii() or not raw_version.isdigit():
            raise ValueError('FLIGHT_AGENT_CONTEXT_VERSION 必须为非负整数')
        raw_http = environment.get('KTD_ALLOW_INSECURE_HTTP', 'false')
        if raw_http not in {'true', 'false'}:
            raise ValueError('KTD_ALLOW_INSECURE_HTTP 必须为 true 或 false')
        return cls(
            base_url=required('KTD_BASE_URL'),
            binding=RuntimeBinding(required('KTD_ACTOR_EMPLOYEE_ID'), required('KTD_PROJECT_ID'),
                required('KTD_HANGAR_ID'), required('KTD_DRONE_ID'),
                required('FLIGHT_AGENT_TASK_BATCH_ID'), int(raw_version)),
            site_id=required('KTD_SITE_ID'), task_id=environment.get('KTD_TASK_ID') or None,
            journal_path=Path(required('FLIGHT_AGENT_JOURNAL')),
            server_token=required('KTD_SERVER_TOKEN'),
            gateway_token=required('FLIGHT_AGENT_GATEWAY_TOKEN'),
            allow_insecure_http=raw_http == 'true',
        )


def build_platform_cockpit(config: PlatformRuntimeConfig, decider_factory, *,
                           enable_control: bool = False, opener=None, clock=time.time):
    """将既有Agent会话接到Java适配器；构建本身不请求Java或模型。"""
    from .cockpit import CockpitService
    from .ktd_platform import KtdPlatformExecutor, SqliteCommandJournal
    from .ktd_snapshot import KtdSnapshotProvider

    if not isinstance(config, PlatformRuntimeConfig) or type(enable_control) is not bool:
        raise ValueError('平台运行配置或控制开关无效')
    if not callable(decider_factory) or not callable(clock):
        raise ValueError('模型工厂和时钟必须可调用')
    parent = config.journal_path.parent
    if (not parent.is_dir() or parent.is_symlink() or parent.stat().st_mode & 0o077
            or (hasattr(os, 'getuid') and parent.stat().st_uid != os.getuid())):
        raise ValueError('平台日志目录必须已存在且仅当前操作者可访问')
    transport = KtdHttpTransport(config.base_url, token=config.server_token,
        actor_id=config.binding.actor_id, allow_insecure_http=config.allow_insecure_http, opener=opener)
    provider = KtdSnapshotProvider(config.binding, site_id=config.site_id,
                                  task_id=config.task_id, transport=transport, clock=clock)
    journal = SqliteCommandJournal(config.journal_path)
    executor = KtdPlatformExecutor(config.binding, site_id=config.site_id, task_id=config.task_id,
        transport=transport, snapshot_provider=provider, journal=journal,
        enabled=enable_control, clock=clock)
    return CockpitService(config.binding, executor, decider_factory,
                          gateway_token=config.gateway_token, task_id=config.task_id, clock=clock)
