"""已认证 Java 网关的多设备入口；复用 CockpitService，不接受浏览器自报身份。"""
from collections import OrderedDict
from dataclasses import replace
import hashlib
import hmac
import json
import threading
import time

from .full_control import RuntimeBinding
from .platform_runtime import build_platform_cockpit
from .standalone_preview import StandaloneResponse


class PlatformGateway:
    """Java 每次核验登录、项目、设备和任务后注入上下文；确认仍重新读取权威快照。"""

    def __init__(self, config, decider_factory, *, enable_control=False,
                 factory=build_platform_cockpit, clock=time.monotonic,
                 max_contexts=32, ttl=1800):
        if config.legacy_read_api or config.allow_legacy_pose_unverified:
            raise ValueError('真实网关不接受旧接口授权降级')
        if not 1 <= max_contexts <= 128 or not 60 <= ttl <= 3600:
            raise ValueError('上下文容量或有效期无效')
        self.config, self.decider_factory = config, decider_factory
        self.enabled, self.factory, self.clock = enable_control, factory, clock
        self.max_contexts, self.ttl = max_contexts, ttl
        self.contexts = OrderedDict()
        self.lock = threading.RLock()

    @staticmethod
    def failure(status, code):
        return StandaloneResponse(status, {'success': False, 'execution_mode': 'platform',
            'dispatched': False, 'device_effect': 'not_verified', 'error_code': code,
            'message': '真实飞控上下文校验失败，未执行。'})

    def handle(self, method, path, body=b'', headers=None):
        headers = {key.lower(): value for key, value in (headers or {}).items()}
        if method == 'GET' and path in ('/healthz', '/api/flight-control/healthz'):
            return StandaloneResponse(200, {'success': True, 'execution_mode': 'platform',
                'control_enabled': self.enabled, 'dispatched': False})
        if not hmac.compare_digest(str(headers.get('x-agent-gateway', '')), self.config.gateway_token):
            return self.failure(401, 'UNAUTHENTICATED')
        try:
            scope = json.loads(headers.get('x-agent-context', ''))
            keys = {'actor_id', 'project_id', 'site_id', 'hangar_id', 'drone_id', 'task_id'}
            if not isinstance(scope, dict) or set(scope) != keys:
                raise ValueError()
            import re
            if any(not isinstance(scope[k], str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', scope[k]) for k in keys):
                raise ValueError()
            token = headers.get('x-agent-platform-token', '')
            if not isinstance(token, str) or not 1 <= len(token) <= 8192 or any(ord(c) <= 32 or ord(c) >= 127 for c in token):
                raise ValueError()
            if headers.get('x-agent-actor') != scope['actor_id']:
                return self.failure(403, 'ACTOR_BINDING_MISMATCH')
        except (ValueError, TypeError):
            return self.failure(400, 'INVALID_PLATFORM_CONTEXT')
        # 凭据摘要仅用于进程内隔离，不写入日志、路径或持久账本。
        resource_key = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
        cache_key = (resource_key, hashlib.sha256(token.encode()).hexdigest())
        with self.lock:
            now = self.clock()
            for key, (used, _) in list(self.contexts.items()):
                if now - used >= self.ttl:
                    del self.contexts[key]
            entry = self.contexts.get(cache_key)
            if entry is None:
                if len(self.contexts) >= self.max_contexts:
                    return self.failure(503, 'PLATFORM_CONTEXT_CAPACITY')
                binding = RuntimeBinding(scope['actor_id'], scope['project_id'], scope['hangar_id'],
                                         scope['drone_id'], scope['task_id'], 1)
                config = replace(self.config, binding=binding, site_id=scope['site_id'],
                    task_id=scope['task_id'], server_token=token,
                    journal_path=self.config.journal_path.with_name('commands-' + resource_key + '.sqlite'))
                try:
                    service = self.factory(config, self.decider_factory, enable_control=self.enabled)
                except (ValueError, OSError):
                    return self.failure(503, 'PLATFORM_RUNTIME_UNAVAILABLE')
            else:
                service = entry[1]
            self.contexts[cache_key] = (now, service)
            # 在请求完成前不允许过期/替换同一上下文，避免两个内存确认账本并行。
            forwarded = {k: v for k, v in headers.items()
                         if k in {'x-agent-gateway', 'x-agent-actor', 'x-agent-session', 'x-trace-id'}}
            return service.handle(method, path, body, forwarded)
