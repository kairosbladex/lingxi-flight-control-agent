"""权威快照 mock：复刻 Java ``lingxi-platform-snapshot/v1`` 的返回形状。

覆盖联调前置里必须逐项核对的五组事实：

1. ``route_status``：显式 null / 字段缺失（平台“不知道”）都必须 fail-closed；
2. 控制权与心跳：owner、``control_owner_updated_at``、租约（``+300s``）；
3. 能力标志：逐动作 ``capabilities`` 与 ``action_availability`` 必须一致，
   距离动作另有 ``UAV_MOVE_DISTANCE``；
4. 机型白名单：只接受 ``DJI_HANGAR`` / ``PZ_HANGAR``，高度基准不同；
5. 快照版本：64 位十六进制（新接口）或非负整数（旧注入接口）。

这个模块只产出内存 dict / 假 opener，不打开 socket，也不声称任何字段是
真实平台已提供的值；所有内容都是合成夹具。
"""

from __future__ import annotations

import json
from io import BytesIO
from typing import Any, Mapping
from urllib.parse import urlsplit

from lingxi_flight_control.full_control import ACTION_SPECS
from lingxi_flight_control.ktd_platform import KtdHttpTransport
from lingxi_flight_control.ktd_snapshot import KtdSnapshotProvider

SCHEMA_VERSION = 'lingxi-platform-snapshot/v1'
#: 明确的“字段不存在”哨兵；与 JSON null（平台声明“不知道”）分开。
ABSENT = object()
HEX64 = 'a' * 64
CONTROL_VERSION = 'b' * 64
TELEMETRY_VERSION = 'c' * 64
SITE_ID = 'site-1'
JAVA_TASK_ID = 'java-task-1'

#: 机型白名单来自 ``ktd_snapshot.py`` / ``ktd_platform.py`` 的既有判断。
PROFILE_WHITELIST = ('DJI_HANGAR', 'PZ_HANGAR')
#: 六向距离动作；距离范围 0.01–20 米由执行器编译阶段强制执行。
MOVE_ACTIONS = tuple(name for name in (
    'move_forward', 'move_backward', 'move_left', 'move_right', 'move_up', 'move_down'))


def _default_capabilities(permitted: set[str]) -> dict[str, bool]:
    values = {action.value: action.value in permitted for action in ACTION_SPECS}
    if any(name in permitted for name in MOVE_ACTIONS):
        values['UAV_MOVE_DISTANCE'] = True
    return values


def _default_state(profile: str, route_status: Any) -> dict[str, Any]:
    if profile == 'PZ_HANGAR':
        # PZ 的距离模式只认相对高 height_m；altitude_m 保持 null 以证明不混用基准。
        altitude: float | None = None
        height: float | None = 10.0
    else:
        altitude = 100.0
        height = 50.0
    return {
        'online': True, 'airborne': True, 'longitude': 113.0, 'latitude': 23.0,
        'altitude_m': altitude, 'height_m': height, 'yaw_deg': 90.0,
        'battery_percent': 90.0, 'flight_status': 'FLYING', 'route_status': route_status,
    }


def build_snapshot_payload(binding, *, now: float, route_status: Any = 'idle',
                           control: Mapping[str, Any] | Any | None = None,
                           permitted: tuple[str, ...] | set[str] = ('read_state',),
                           capabilities: Mapping[str, bool] | None = None,
                           profile: str = 'DJI_HANGAR', snapshot_version: Any = HEX64,
                           state_overrides: Mapping[str, Any] | None = None,
                           sampled_offset: float = -1.0,
                           received_offset: float = -0.5,
                           generated_offset: float = 0.0,
                           expires_offset: float | None = None) -> dict[str, Any]:
    """构造一份 v1 快照信封；参数即“平台可能返回的差异点”。

    ``control`` 可以是 :class:`tools.integration.control_authority_mock.ControlAuthorityMock`
    （读 ``snapshot_fields()``）或直接给字段映射；缺省为 actor 本人、租约 270 秒。
    """
    permitted_set = set(permitted)
    if control is None:
        lease = now + 270.0
        control_fields = {
            'control_owner_employee_id': binding.actor_id,
            'control_owner_updated_at': now - 30.0,
            'control_lease_expires_at': lease,
            'control_owner_version': CONTROL_VERSION,
        }
    elif hasattr(control, 'snapshot_fields'):
        control_fields = dict(control.snapshot_fields())
    else:
        control_fields = dict(control)

    sampled = now + sampled_offset
    received = now + received_offset
    observed = min(sampled, received)
    expires = observed + 5.0 if expires_offset is None else now + expires_offset

    state = _default_state(profile, route_status)
    for key, value in (state_overrides or {}).items():
        if value is ABSENT:
            state.pop(key, None)
        else:
            state[key] = value

    caps = _default_capabilities(permitted_set)
    if capabilities:
        caps.update({key: bool(value) for key, value in capabilities.items()})

    availability = {}
    for action in ACTION_SPECS:
        available = action.value in permitted_set
        availability[action.value] = {
            'available': available,
            'reasons': [] if available else ['CAPABILITY_UNAVAILABLE'],
        }

    data: dict[str, Any] = {
        'schema_version': SCHEMA_VERSION,
        'snapshot_version': snapshot_version,
        'generated_at': now + generated_offset,
        'expires_at': expires,
        'actor_employee_id': binding.actor_id,
        'project_id': binding.project_id,
        'site_id': SITE_ID,
        'hangar_id': binding.hangar_id,
        'drone_id': binding.drone_id,
        'task_id': JAVA_TASK_ID,
        **control_fields,
        'profile': profile,
        'telemetry': {
            'sampled_at': sampled, 'received_at': received, 'source': 'java-udp-cache',
            'sequence': None, 'version': TELEMETRY_VERSION,
        },
        'state': state,
        'capabilities': caps,
        'permissions': sorted(permitted_set),
        'action_availability': availability,
    }
    return {'code': 200, 'success': True, 'data': data}


def adapter_view(payload: Mapping[str, Any]) -> dict[str, Any]:
    """把 v1 快照摊平成 ``KtdPlatformAdapter`` 吃的扁平快照。

    适配器只看 ``route_status`` 一个事实；字段缺失时它拿到的仍是 ``None``，
    因此“平台没返回”与“平台返回 null”都走同一条 fail-closed 分支。
    """
    data = payload['data']
    view = dict(data['state'])
    view.setdefault('drone_id', data['drone_id'])
    view.setdefault('control_owner', data.get('control_owner_employee_id'))
    return view


class SnapshotOpener:
    """``KtdHttpTransport`` 的假网络边界；只响应固定 GET 快照路径。

    记录每次请求的 ``(method, path)``，用于断言“构建/预览不产生网络请求”。
    """

    def __init__(self, payload: Mapping[str, Any]):
        self.payload = payload
        self.calls: list[tuple[str, str]] = []

    def open(self, request, *, timeout):  # noqa: ANN001 - 与 urllib opener 签名一致
        path = urlsplit(request.full_url).path
        self.calls.append((request.get_method(), path))
        if request.get_method() != 'GET' or not path.endswith('/lingxi/agent/platform/snapshot'):
            raise AssertionError(f'unexpected mock request: {request.get_method()} {path}')
        response = BytesIO(json.dumps(self.payload).encode('utf-8'))
        response.code = 200
        return response


def make_transport(binding, opener: SnapshotOpener, *,
                   base_url: str = 'https://platform.invalid/api') -> KtdHttpTransport:
    """真实传输 + 假 opener：保留请求头/路径/JSON 解码路径，不打开 socket。"""
    return KtdHttpTransport(base_url, token='integration-fixture-token',
                            actor_id=binding.actor_id, opener=opener)


def make_provider(binding, *, payload: Mapping[str, Any], clock,
                  site_id: str = SITE_ID, task_id: str | None = JAVA_TASK_ID):
    """返回 ``(provider, opener)``；provider 是现有严格读取器，不做任何替换。"""
    opener = SnapshotOpener(payload)
    transport = make_transport(binding, opener)
    provider = KtdSnapshotProvider(binding, site_id=site_id, task_id=task_id,
                                   transport=transport, clock=clock)
    return provider, opener
