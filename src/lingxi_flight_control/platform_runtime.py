"""服务端平台绑定与装配；配置中没有自动接管或自动启用写操作。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
import json
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
    media_allowed_origins: tuple[str, ...] = ()
    media_allow_insecure_http: bool = False
    allowed_vendor_methods: tuple[str, ...] = ()
    legacy_read_api: bool = False
    allow_legacy_pose_unverified: bool = False

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
        from .ktd_visuals import validate_media_origins
        object.__setattr__(self, 'media_allowed_origins', validate_media_origins(
            self.media_allowed_origins, allow_insecure_http=self.media_allow_insecure_http))
        vendor_methods = self.allowed_vendor_methods
        if (not isinstance(vendor_methods, tuple)
                or any(not isinstance(value, str) or value not in {
                    'flight_authority_grab', 'drc_mode_enter', 'drc_mode_exit'} for value in vendor_methods)
                or len(set(vendor_methods)) != len(vendor_methods)):
            raise ValueError('FLIGHT_AGENT_VENDOR_METHODS 必须为无重复的厂商方法白名单')
        if (type(self.legacy_read_api) is not bool
                or type(self.allow_legacy_pose_unverified) is not bool):
            raise ValueError('FLIGHT_AGENT_LEGACY_READ_API 配置无效')

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
        media_http = environment.get('FLIGHT_AGENT_MEDIA_ALLOW_INSECURE_HTTP', 'false')
        if media_http not in {'true', 'false'}:
            raise ValueError('FLIGHT_AGENT_MEDIA_ALLOW_INSECURE_HTTP 必须为 true 或 false')
        try:
            media_origins = json.loads(environment.get('FLIGHT_AGENT_MEDIA_ALLOWED_ORIGINS', '[]'))
        except (ValueError, TypeError):
            raise ValueError('FLIGHT_AGENT_MEDIA_ALLOWED_ORIGINS 必须是JSON数组') from None
        try:
            vendor_methods = json.loads(environment.get('FLIGHT_AGENT_VENDOR_METHODS', '[]'))
        except (ValueError, TypeError):
            raise ValueError('FLIGHT_AGENT_VENDOR_METHODS 必须是JSON数组') from None
        if not isinstance(vendor_methods, list):
            raise ValueError('FLIGHT_AGENT_VENDOR_METHODS 必须是JSON数组')
        legacy = environment.get('FLIGHT_AGENT_LEGACY_READ_API', 'false')
        if legacy not in {'true', 'false'}:
            raise ValueError('FLIGHT_AGENT_LEGACY_READ_API 必须为 true 或 false')
        legacy_pose = environment.get('FLIGHT_AGENT_LEGACY_ALLOW_POSE_UNVERIFIED', 'false')
        if legacy_pose not in {'true', 'false'}:
            raise ValueError('FLIGHT_AGENT_LEGACY_ALLOW_POSE_UNVERIFIED 必须为 true 或 false')
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
            media_allowed_origins=media_origins,
            media_allow_insecure_http=media_http == 'true',
            allowed_vendor_methods=tuple(vendor_methods),
            legacy_read_api=legacy == 'true',
            allow_legacy_pose_unverified=legacy_pose == 'true',
        )


def build_platform_cockpit(config: PlatformRuntimeConfig, decider_factory, *,
                           enable_control: bool = False, enable_vendor_requests: bool = False,
                           enable_legacy_control: bool | None = None,
                           allow_legacy_pose_unverified: bool | None = None,
                           opener=None, clock=time.time):
    """将既有Agent会话接到Java适配器；构建本身不请求Java或模型。"""
    from .cockpit import CockpitService
    from .ktd_platform import KtdPlatformExecutor, SqliteCommandJournal
    from .ktd_snapshot import KtdSnapshotProvider
    from .ktd_visuals import KtdVisualsProvider
    from .ktd_legacy import LegacyKtdSnapshotProvider

    if (not isinstance(config, PlatformRuntimeConfig) or type(enable_control) is not bool
            or type(enable_vendor_requests) is not bool):
        raise ValueError('平台运行配置或控制开关无效')
    if enable_legacy_control is None:
        enable_legacy_control = config.legacy_read_api
    if allow_legacy_pose_unverified is None:
        allow_legacy_pose_unverified = config.allow_legacy_pose_unverified
    if (type(enable_legacy_control) is not bool
            or type(allow_legacy_pose_unverified) is not bool
            or (allow_legacy_pose_unverified and not enable_legacy_control)):
        raise ValueError('route A legacy 开关无效')
    if enable_legacy_control and enable_vendor_requests:
        raise ValueError('route A legacy 模式不能同时启用厂商请求')
    if not callable(decider_factory) or not callable(clock):
        raise ValueError('模型工厂和时钟必须可调用')
    parent = config.journal_path.parent
    if (not parent.is_dir() or parent.is_symlink() or parent.stat().st_mode & 0o077
            or (hasattr(os, 'getuid') and parent.stat().st_uid != os.getuid())):
        raise ValueError('平台日志目录必须已存在且仅当前操作者可访问')
    transport = KtdHttpTransport(config.base_url, token=config.server_token,
        actor_id=config.binding.actor_id, allow_insecure_http=config.allow_insecure_http, opener=opener)
    if enable_legacy_control:
        provider = LegacyKtdSnapshotProvider(config.binding, site_id=config.site_id,
                                             task_id=config.task_id, transport=transport, clock=clock)
    else:
        provider = KtdSnapshotProvider(config.binding, site_id=config.site_id,
                                       task_id=config.task_id, transport=transport, clock=clock)
    journal = SqliteCommandJournal(config.journal_path)
    executor = KtdPlatformExecutor(config.binding, site_id=config.site_id, task_id=config.task_id,
        transport=transport, snapshot_provider=provider, journal=journal,
        enabled=enable_control, track_commands=not enable_legacy_control, clock=clock,
        enable_vendor_requests=enable_vendor_requests, allowed_vendor_methods=config.allowed_vendor_methods,
        legacy_read_api=enable_legacy_control,
        allow_unverified_pose=allow_legacy_pose_unverified)
    visuals = KtdVisualsProvider(config.binding, site_id=config.site_id, task_id=config.task_id,
        transport=transport, allowed_origins=config.media_allowed_origins,
        allow_insecure_http=config.media_allow_insecure_http, clock=clock)
    return CockpitService(config.binding, executor, decider_factory,
                          gateway_token=config.gateway_token, task_id=config.task_id, clock=clock,
                          visuals_provider=visuals)
