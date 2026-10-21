"""同源浏览器直连Python；身份与资源从原Java只读接口核验，不要求Java新增接口。"""
from dataclasses import replace
import json
import re
import time
from .ktd_legacy import LegacyKtdSnapshotProvider, _data, _hash
from .ktd_platform import KtdHttpTransport, KtdPlatformError
from .platform_gateway import PlatformGateway
from .platform_runtime import build_platform_cockpit


def require_task(transport, project, site, drone, task):
    data = _data(transport.read_legacy('task', project_id=project, query={'taskId': task}))
    if (not isinstance(data, dict) or data.get('taskId') != task or data.get('terminalId') != drone
            or data.get('siteId') != site or data.get('taskFlag') != '2'):
        raise KtdPlatformError('TASK_NOT_RUNNING_OR_CHANGED')


class BrowserSnapshotProvider(LegacyKtdSnapshotProvider):
    """原接口的明确能力和新鲜姿态才放行；不覆盖Java的距离模式false。"""
    def _require_binding(self, user):
        super()._require_binding(user)
        if not self.task_id:
            raise KtdPlatformError('TASK_REQUIRED')
        require_task(self._transport, self.binding.project_id, self.site_id, self.binding.drone_id, self.task_id)

    def _read_profile(self):
        profile = super()._read_profile()
        self.actual_capabilities = self._read('permissions', drone_id=self.binding.drone_id)
        return profile

    def __call__(self):
        snapshot = super().__call__()
        raw = self.actual_capabilities
        uav, load = raw.get('uav') or {}, raw.get('load') or {}
        actions = {'read_state'}
        # 原平台不同载荷以不同布尔字段暴露能力；仅接受已核验的明确true。
        if uav.get('UAV_MOVE_DISTANCE') is True:
            actions.update(('move_forward', 'move_backward', 'move_left', 'move_right', 'move_up', 'move_down'))
        if uav.get('UAV_MOVE_STICK') is True:
            actions.update(('turn_left', 'turn_right'))
        camera = (raw.get('loadInfoCode') or {}).get('1001') is True and bool(load.get('optoelecPodParamList'))
        if camera:
            actions.add('camera_photo')
        if camera:
            actions.update(('gimbal_up', 'gimbal_down', 'gimbal_left', 'gimbal_right'))
            actions.add('gimbal_reset')
            if raw.get('pitch45') is True: actions.add('gimbal_45')
            if raw.get('pitch90') is True: actions.add('gimbal_90')
        state = dict(snapshot.state)
        pose = self.pose()
        fresh = bool(pose and pose.get('observed_at') is not None
                     and 0 <= self._now() - pose['observed_at'] <= 5)
        complete = fresh and all(pose.get(k) is not None for k in
                                ('longitude', 'latitude', 'altitude_m', 'height_m', 'yaw_deg'))
        state['legacy_pose_verified'] = bool(complete)
        if not complete:
            actions.difference_update(('move_forward', 'move_backward', 'move_left', 'move_right', 'move_up', 'move_down'))
        if not fresh:
            actions.difference_update(('turn_left', 'turn_right'))
        permissions = snapshot.permissions.intersection(actions)
        capabilities = {key: value for key, value in snapshot.capabilities.items() if key in actions}
        capabilities['UAV_MOVE_DISTANCE'] = uav.get('UAV_MOVE_DISTANCE') is True
        availability = {action: {'available': action in permissions,
                                'reasons': [] if action in permissions else ['CAPABILITY_OR_POSE_UNAVAILABLE']}
                        for action in snapshot.action_availability}
        return replace(snapshot, state=state, permissions=frozenset(permissions), capabilities=capabilities,
                       action_availability=availability,
                       version=_hash({'original': snapshot.version, 'permissions': sorted(permissions),
                                      'capabilities': capabilities, 'pose_verified': complete}))


class BrowserGateway:
    def __init__(self, config, decider_factory, *, enable_control=False, opener=None, clock=time.time,
                 allowed_drone_sns=()):
        if not isinstance(allowed_drone_sns, (tuple, list)) or not allowed_drone_sns or any(
                not isinstance(s, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', s) for s in allowed_drone_sns):
            raise ValueError('必须配置明确的设备范围')
        self.allowed_drone_sns = frozenset(allowed_drone_sns)
        self.config, self.opener = config, opener
        def factory(bound, decider, **kwargs):
            return build_platform_cockpit(replace(bound, legacy_read_api=True), decider,
                opener=opener, clock=clock, legacy_snapshot_factory=BrowserSnapshotProvider, **kwargs)
        self.runtime = PlatformGateway(config, decider_factory, enable_control=enable_control, factory=factory)

    def handle(self, method, path, body=b'', headers=None):
        started = time.monotonic()
        response = self._handle(method, path, body, headers)
        operation = path.split('?', 1)[0]
        if not re.fullmatch(r'/api/flight-control/(context|state|agent/(messages|confirm|cancel|reset|receipt))', operation):
            operation = 'other'
        print(json.dumps({'event': 'flight_gateway', 'operation': operation,
              'status': response.status_code, 'error_code': response.payload.get('error_code'),
              'distance_mode_available': response.payload.get('distance_mode_available'),
              'trace_id': response.payload.get('trace_id'),
              'elapsed_ms': round((time.monotonic() - started) * 1000)}), flush=True)
        return response

    def _handle(self, method, path, body=b'', headers=None):
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        if method == 'GET' and path in ('/healthz', '/api/flight-control/healthz'):
            return self.runtime.handle(method, path)
        try:
            auth = headers.get('authorization', '')
            if re.fullmatch(r'Bearer [!-~]{1,8192}', auth, re.IGNORECASE):
                token = auth[7:]
            elif re.fullmatch(r'[!-~]{1,8192}', auth):
                token = auth
            else:
                return self.runtime.failure(401, 'AUTHENTICATION_REQUIRED')
            values = {k: headers.get(h, '') for k, h in (
                ('project_id', 'projectid'), ('site_id', 'x-flight-site'),
                ('drone_id', 'x-flight-drone'), ('task_id', 'x-flight-task'))}
            if any(not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', v) for k, v in values.items() if k != 'task_id'):
                return self.runtime.failure(400, 'INVALID_PLATFORM_CONTEXT')
            session = headers.get('x-agent-session', '')
            if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', session):
                return self.runtime.failure(400, 'INVALID_SESSION')
            transport = KtdHttpTransport(self.config.base_url, token=token, actor_id='identity-lookup',
                allow_insecure_http=self.config.allow_insecure_http, opener=self.opener)
            user = _data(transport.read_legacy('user_info', project_id=values['project_id']))
            actor = user.get('empId') if isinstance(user, dict) else None
            if not isinstance(actor, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', actor):
                return self.runtime.failure(401, 'AUTHENTICATION_REQUIRED')
            sites = _data(transport.read_legacy('sites', project_id=values['project_id'],
                                               query={'projectId': values['project_id']}))
            matches = [s for s in sites if isinstance(s, dict) and s.get('id') == values['site_id']] if isinstance(sites, list) else []
            if len(matches) != 1 or matches[0].get('uavId') != values['drone_id']:
                return self.runtime.failure(403, 'RESOURCE_BINDING_MISMATCH')
            # 原/uavSite/list投影只返回uavCode；UAVSite.getUavSn也优先使用该字段。
            if (matches[0].get('uavCode') or matches[0].get('uavSn')) not in self.allowed_drone_sns:
                return self.runtime.failure(403, 'DEVICE_OUT_OF_SCOPE')
            if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', values['task_id']):
                capabilities = _data(transport.read_legacy('permissions', project_id=values['project_id'],
                                                          drone_id=values['drone_id']))
                response = self.runtime.failure(409, 'TASK_REQUIRED')
                response.payload['connection_verified'] = True
                response.payload['distance_mode_available'] = bool(isinstance(capabilities, dict)
                    and isinstance(capabilities.get('uav'), dict)
                    and capabilities['uav'].get('UAV_MOVE_DISTANCE') is True)
                return response
            require_task(transport, values['project_id'], values['site_id'], values['drone_id'], values['task_id'])
            scope = {**values, 'actor_id': actor, 'hangar_id': matches[0].get('hangarId')}
            trusted = {'X-Agent-Gateway': self.config.gateway_token, 'X-Agent-Actor': actor,
                'X-Agent-Context': json.dumps(scope), 'X-Agent-Platform-Token': token,
                'X-Agent-Session': session}
            return self.runtime.handle(method, path, body, trusted)
        except KtdPlatformError as error:
            code = str(error)
            return self.runtime.failure(403 if code in ('AUTHENTICATION_REQUIRED', 'TASK_NOT_RUNNING_OR_CHANGED') else 503, code)
        except Exception:
            if method == 'POST' and path.rstrip('/').endswith('/confirm'):
                response = self.runtime.failure(503, 'PLATFORM_OUTCOME_UNKNOWN')
                response.payload.pop('dispatched', None)
                response.payload['message'] = '提交结果未知，请核对设备；不要重复确认或重发。'
                return response
            return self.runtime.failure(503, 'PLATFORM_SOURCE_UNAVAILABLE')
