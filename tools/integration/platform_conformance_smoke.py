#!/usr/bin/env python3
"""平台契约一致性冒烟（完全离线，不连接真实 Java / 围栏 / MQTT / 设备）。

用途：真实平台联调前，先把“平台应该返回什么、现有运行时怎么拒绝”逐项钉住。
所有断言走仓库现有只读路径：

- ``KtdPlatformAdapter.adapt_snapshot``：``route_status`` 为 null / 缺失时 fail-closed；
- ``KtdSnapshotProvider``：严格解码 v1 权威快照（控制权、租约、能力、版本、机型）；
- ``KtdPlatformExecutor``：只做 ``_compile`` 合同门禁与默认关闭检查，**不提交**任何命令；
- ``KtdPathChecker`` + ``flight_policy``：电子围栏五类结论与“不可用即跳过不阻断”；
- ``MemoryFlightControlSimulator``：挂载清单与算法启停事件；
- ``PlatformRuntimeConfig`` + ``build_platform_cockpit(enable_control=False)``：
  只读装配、零网络构建、`/state` 与 `/context` 可用性。

运行（不需要任何环境变量/凭据）：

    python3 tools/integration/platform_conformance_smoke.py

输出逐项 ``PASS`` / ``FAIL``；全部通过退出码 0，否则 1。脚本不会传
``--enable-platform-control``，也不会调用 ``confirm`` 或发送控制 POST。

边界：本脚本证明的是**契约形状与 fail-closed 行为**，不证明真实平台已按此返回、
不证明设备执行、不证明控制权/算法在真实环境可用。
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = REPO_ROOT / 'src'
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
if str(REPO_ROOT) not in sys.path:
    sys.path.append(str(REPO_ROOT))

from lingxi_flight_control.adapters.ktd_platform_adapter import (  # noqa: E402
    KtdPlatformAdapter, ROUTE_STATE_SOURCE_UNAVAILABLE, RouteStatusUnavailableError,
)
from lingxi_flight_control.flight_policy import (  # noqa: E402
    LENS_NOT_MOUNTED_MESSAGE, SEARCHLIGHT_NOT_MOUNTED_MESSAGE, PolicyError,
    evaluate_path, payload_mount_message,
)
from lingxi_flight_control.full_control import (  # noqa: E402
    FlightControlActionRequest, MemoryFlightControlSimulator, RuntimeBinding,
)
from lingxi_flight_control.ktd_path import KtdPathChecker  # noqa: E402
from lingxi_flight_control.ktd_platform import (  # noqa: E402
    KtdPlatformError, KtdPlatformExecutor, SqliteCommandJournal,
)
from lingxi_flight_control.platform_runtime import (  # noqa: E402
    PlatformRuntimeConfig, build_platform_cockpit,
)
from tools.integration.authoritative_snapshot_mock import (  # noqa: E402
    ABSENT, HEX64, JAVA_TASK_ID, SITE_ID, SnapshotOpener, adapter_view,
    build_snapshot_payload, make_provider,
)
from tools.integration.control_authority_mock import (  # noqa: E402
    ControlAuthorityError, ControlAuthorityMock,
)
from tools.integration.fence_mock import FenceContractMock, fence_state  # noqa: E402
from tools.integration.mount_algorithm_mock import (  # noqa: E402
    AlgorithmLifecycleEventMock, MountedCapabilityMock,
)
from tools.mocks.mqtt_telemetry_simulator import MqttTelemetrySimulator  # noqa: E402

NOW = 1_700_000_000.0
BINDING = RuntimeBinding('actor-1', 'project-1', 'hangar-1', 'drone-1', 'batch-1', 1)
GATEWAY_TOKEN = 'integration-gateway-credential'
HEADERS = {
    'X-Agent-Session': 'conformance-smoke',
    'X-Agent-Actor': BINDING.actor_id,
    'X-Agent-Gateway': GATEWAY_TOKEN,
}


@dataclass(frozen=True)
class Result:
    name: str
    passed: bool
    detail: str


def _expect(condition: Any, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _expect_error(expected: str, call: Callable[[], Any], *, error_type=KtdPlatformError) -> None:
    try:
        call()
    except error_type as error:
        actual = getattr(error, 'code', None) or str(error)
        _expect(actual == expected, f'expected {expected}, got {actual}')
        return
    raise AssertionError(f'expected {expected}, but call succeeded')


def _provider(payload: dict[str, Any], *, clock: Callable[[], float] = lambda: NOW):
    provider, opener = make_provider(BINDING, payload=payload, clock=clock)
    return provider, opener


def _executor(tmpdir: str, snapshot: Any) -> KtdPlatformExecutor:
    return KtdPlatformExecutor(
        BINDING, site_id=SITE_ID, task_id=JAVA_TASK_ID, transport=None,
        snapshot_provider=lambda: snapshot,
        journal=SqliteCommandJournal(str(Path(tmpdir) / 'conformance.sqlite3')),
        enabled=False, clock=lambda: NOW,
    )


def _request(action: str, parameters: dict[str, Any], key: str = 'conformance-key'):
    return FlightControlActionRequest('conformance-command', key, action, parameters,
                                      BINDING.context_version)


# ---------------------------------------------------------------------------
# A. 权威快照契约
# ---------------------------------------------------------------------------

def check_snapshot_readonly_decode(tmpdir: str) -> str:
    payload = build_snapshot_payload(BINDING, now=NOW, route_status='idle',
                                     permitted=('read_state',))
    provider, opener = _provider(payload)
    snapshot = provider()
    _expect(snapshot.observed_at == NOW - 1.0, f'observed_at={snapshot.observed_at}')
    _expect(snapshot.control_owner == BINDING.actor_id, 'owner mismatch')
    _expect(snapshot.control_lease_expires_at == NOW + 270.0, 'lease mismatch')
    _expect(snapshot.version == HEX64, 'snapshot version mismatch')
    _expect(opener.calls == [('GET', '/api/lingxi/agent/platform/snapshot')],
            f'unexpected transport calls: {opener.calls}')
    return f'observed_at={snapshot.observed_at}, lease={snapshot.control_lease_expires_at}'


def check_route_status_null_fail_closed(tmpdir: str) -> str:
    payload = build_snapshot_payload(BINDING, now=NOW, route_status=None,
                                     permitted=('read_state',))
    view = adapter_view(payload)
    adapter = KtdPlatformAdapter()
    blocked = ('move_forward', 'move_backward', 'move_left', 'move_right', 'move_up',
               'move_down', 'turn_left', 'turn_right', 'hover', 'point_flight', 'point_orbit')
    for action in blocked:
        try:
            adapter.adapt_snapshot(view, requested_action=action)
        except RouteStatusUnavailableError as error:
            _expect(error.code == ROUTE_STATE_SOURCE_UNAVAILABLE, 'unexpected adapter code')
            _expect(error.diagnostic.get('fail_closed') is True, 'fail_closed flag missing')
        else:
            raise AssertionError(f'{action} was not blocked on null route_status')
    degraded = adapter.adapt_snapshot(view, requested_action='read_state')
    _expect(degraded.get('_degraded_route_status') is True, 'read-only degrade marker missing')
    _expect(degraded.get('_route_status_unavailable_reason') == 'PLATFORM_SNAPSHOT_NULL',
            'unexpected degrade reason')
    snapshot = _provider(payload)[0]()
    _expect(snapshot.state['route_status'] is None, 'null route_status must stay None')
    return f'{len(blocked)} 个飞行动作被拦截，只读路径带降级标记'


def check_route_status_absent_fail_closed(tmpdir: str) -> str:
    payload = build_snapshot_payload(BINDING, now=NOW, route_status=ABSENT,
                                     permitted=('read_state',),
                                     state_overrides={'route_status': ABSENT})
    view = adapter_view(payload)
    _expect('route_status' not in view, 'fixture must omit route_status')
    try:
        KtdPlatformAdapter().adapt_snapshot(view, requested_action='move_forward')
    except RouteStatusUnavailableError:
        pass
    else:
        raise AssertionError('absent route_status must still fail closed')
    _expect_error('AUTHORITATIVE_STATE_INCOMPLETE', _provider(payload)[0])
    return '字段缺失（平台未知）在适配器与严格读取器两层都拒绝'


def check_control_owner_mismatch(tmpdir: str) -> str:
    payload = build_snapshot_payload(
        BINDING, now=NOW, route_status='idle', permitted=('read_state', 'move_forward'),
        control={'control_owner_employee_id': 'actor-2',
                 'control_owner_updated_at': NOW - 10.0,
                 'control_lease_expires_at': NOW + 290.0,
                 'control_owner_version': 'b' * 64})
    _expect_error('SNAPSHOT_AUTHORIZATION_CONFLICT', _provider(payload)[0])
    return '他人持有控制权时权威快照拒绝授权'


def check_control_lease_expired(tmpdir: str) -> str:
    payload = build_snapshot_payload(
        BINDING, now=NOW, route_status='idle', permitted=('read_state', 'move_forward'),
        control={'control_owner_employee_id': BINDING.actor_id,
                 'control_owner_updated_at': NOW - 301.0,
                 'control_lease_expires_at': NOW - 1.0,
                 'control_owner_version': 'b' * 64})
    _expect_error('SNAPSHOT_AUTHORIZATION_CONFLICT', _provider(payload)[0])
    return '租约到期（心跳停更）时权威快照拒绝授权'


def check_control_lease_missing(tmpdir: str) -> str:
    payload = build_snapshot_payload(
        BINDING, now=NOW, route_status='idle', permitted=('read_state', 'move_forward'),
        control={'control_owner_employee_id': BINDING.actor_id,
                 'control_owner_updated_at': None,
                 'control_lease_expires_at': None,
                 'control_owner_version': None})
    _expect_error('SNAPSHOT_AUTHORIZATION_CONFLICT', _provider(payload)[0])
    return '缺失租约不得按“当前时间+默认TTL”补齐'


def check_heartbeat_renewal(tmpdir: str) -> str:
    authority = ControlAuthorityMock(now=NOW)
    authority.obtain(BINDING.actor_id)
    first = authority.lease_expires_at
    authority.tick(120)
    authority.heartbeat(BINDING.actor_id)
    renewed = authority.lease_expires_at
    _expect(renewed == first + 120, f'lease not renewed: {first} -> {renewed}')
    payload = build_snapshot_payload(BINDING, now=authority.now, route_status='idle',
                                     control=authority,
                                     permitted=('read_state', 'move_forward'))
    snapshot = _provider(payload, clock=lambda: authority.now)[0]()
    _expect(snapshot.control_lease_expires_at == renewed, 'snapshot lease mismatch')
    authority.tick(301)
    expired = build_snapshot_payload(BINDING, now=authority.now, route_status='idle',
                                     control=authority,
                                     permitted=('read_state', 'move_forward'))
    _expect_error('SNAPSHOT_AUTHORIZATION_CONFLICT',
                  _provider(expired, clock=lambda: authority.now)[0])
    return f'心跳续租到 {renewed}，停更后拒绝'


def check_capability_conflict(tmpdir: str) -> str:
    payload = build_snapshot_payload(BINDING, now=NOW, route_status='idle',
                                     permitted=('read_state', 'move_forward'),
                                     capabilities={'move_forward': False})
    _expect_error('SNAPSHOT_AUTHORIZATION_CONFLICT', _provider(payload)[0])
    return 'availability=available 但 capability=false 被拒绝'


def check_distance_bounds(tmpdir: str) -> str:
    payload = build_snapshot_payload(BINDING, now=NOW, route_status='idle',
                                     permitted=('read_state', 'move_forward'))
    snapshot = _provider(payload)[0]()
    executor = _executor(tmpdir, snapshot)
    accepted = []
    for distance in (0.01, 20):
        command = executor._compile(_request('move_forward', {'distance_m': distance}), snapshot)
        _expect(command.path == '/screen/uav/operation/control', 'unexpected move path')
        _expect(command.body['param'] == distance, 'distance param mismatch')
        accepted.append(distance)
    for distance in (0.009, 20.001):
        _expect_error('DISTANCE_OUT_OF_RANGE',
                      lambda d=distance: executor._compile(
                          _request('move_forward', {'distance_m': d}), snapshot))
    return f'接受 {accepted}，拒绝 0.009 / 20.001'


def check_profile_whitelist(tmpdir: str) -> str:
    rejected = build_snapshot_payload(BINDING, now=NOW, route_status='idle',
                                      permitted=('read_state',), profile='D10_HANGAR')
    _expect_error('INVALID_SNAPSHOT_RESPONSE', _provider(rejected)[0])

    pz = build_snapshot_payload(BINDING, now=NOW, route_status='idle', profile='PZ_HANGAR',
                                permitted=('read_state', 'move_forward'))
    pz_snapshot = _provider(pz)[0]()
    command = _executor(tmpdir, pz_snapshot)._compile(
        _request('move_forward', {'distance_m': 10}), pz_snapshot)
    _expect(command.body['param'] == 10, 'PZ move should compile')

    dji_missing_altitude = build_snapshot_payload(
        BINDING, now=NOW, route_status='idle', permitted=('read_state', 'move_forward'),
        state_overrides={'altitude_m': None})
    dji_snapshot = _provider(dji_missing_altitude)[0]()
    _expect_error('FLIGHT_POSE_UNAVAILABLE',
                  lambda: _executor(tmpdir, dji_snapshot)._compile(
                      _request('move_forward', {'distance_m': 10}), dji_snapshot))
    return 'D10 拒绝；PZ 用 height_m；DJI 缺 altitude_m 拒绝'


def check_snapshot_version(tmpdir: str) -> str:
    accepted = build_snapshot_payload(BINDING, now=NOW, route_status='idle',
                                      permitted=('read_state',), snapshot_version=HEX64)
    _expect(_provider(accepted)[0]().version == HEX64, '64-hex version should decode')
    # 严格 v1 读取器只认真实输入内容的 64 位十六进制 SHA256；旧注入接口的
    # 非负整数由 legacy 读取器处理，不能在这条权威路径上放行。
    for invalid in ('not-a-version', 'a' * 63, -1, 7):
        payload = build_snapshot_payload(BINDING, now=NOW, route_status='idle',
                                         permitted=('read_state',),
                                         snapshot_version=invalid)
        _expect_error('INVALID_SNAPSHOT_RESPONSE', _provider(payload)[0])
    return '严格 v1 只接受 64 位十六进制；字面串/短哈希/负数/整数均拒绝'


# ---------------------------------------------------------------------------
# B. 电子围栏五类
# ---------------------------------------------------------------------------

def check_fence_clear(tmpdir: str) -> str:
    mock = FenceContractMock('clear')
    checker = KtdPathChecker(mock)
    outcome = evaluate_path(checker, BINDING, 'move_forward', {'distance_m': 10}, fence_state())
    _expect(outcome.check.status == 'clear', f'status={outcome.check.status}')
    _expect(outcome.warning is None, 'clear must not warn')
    _expect(len(mock.calls) == 1, 'fence transport should be called once')
    _expect(mock.calls[0]['timeout_sec'] == checker.deadline_sec, 'deadline not passed down')
    return f'clear, segments={outcome.check.segments}, timeout_sec={checker.deadline_sec}'


def check_fence_blocked(tmpdir: str) -> str:
    from lingxi_flight_control.flight_policy import check_path
    checker = KtdPathChecker(FenceContractMock('blocked'))
    try:
        check_path(checker, BINDING, 'move_forward', {'distance_m': 10}, fence_state())
    except PolicyError as error:
        _expect(error.code == 'PATH_BLOCKED', f'unexpected code={error.code}')
        return 'blocked → PolicyError PATH_BLOCKED'
    raise AssertionError('blocked path was not rejected')


def check_fence_invalid(tmpdir: str) -> str:
    from lingxi_flight_control.flight_policy import check_path
    mock = FenceContractMock('invalid')
    checker = KtdPathChecker(mock)
    try:
        check_path(checker, BINDING, 'move_forward', {'distance_m': 0.0}, fence_state())
    except PolicyError as error:
        _expect(error.code == 'INVALID_PATH_GEOMETRY', f'unexpected code={error.code}')
        _expect(mock.calls == [], 'invalid geometry must not call the fence service')
        return '几何非法 → INVALID_PATH_GEOMETRY，未发围栏请求'
    raise AssertionError('invalid geometry was not rejected')


def check_fence_timeout(tmpdir: str) -> str:
    checker = KtdPathChecker(FenceContractMock('timeout'))
    outcome = evaluate_path(checker, BINDING, 'move_forward', {'distance_m': 10}, fence_state())
    _expect(outcome.check.status == 'timeout', f'status={outcome.check.status}')
    _expect(outcome.check.degraded is True, 'timeout must be degraded')
    _expect(outcome.warning and '电子围栏检查超时' in outcome.warning, 'timeout warning missing')
    _expect(outcome.blocked is False, 'timeout must not block')
    return 'timeout → 降级警告，不阻断'


def check_fence_unavailable(tmpdir: str) -> str:
    checker = KtdPathChecker(FenceContractMock('unavailable'))
    outcome = evaluate_path(checker, BINDING, 'move_forward', {'distance_m': 10}, fence_state())
    _expect(outcome.check.status == 'unavailable', f'status={outcome.check.status}')
    _expect(outcome.check.degraded is True, 'unavailable must be degraded')
    _expect(outcome.warning and '电子围栏接口不可用' in outcome.warning,
            'unavailable warning missing')
    _expect(outcome.blocked is False, 'unavailable must not block')
    return 'unavailable → 降级警告，不阻断业务'


# ---------------------------------------------------------------------------
# C. 控制权中台
# ---------------------------------------------------------------------------

def check_control_obtain_release(tmpdir: str) -> str:
    authority = ControlAuthorityMock(now=NOW)
    granted = authority.obtain(BINDING.actor_id)
    _expect(granted['owner'] == BINDING.actor_id, 'obtain owner mismatch')
    _expect(granted['lease_expires_at'] == NOW + 300.0, 'obtain lease must be now+300')
    _expect(authority.valid(BINDING.actor_id) is True, 'granted lease should be valid')
    released = authority.release(BINDING.actor_id)
    _expect(released['owner'] is None, 'release did not clear owner')
    _expect(authority.valid() is False, 'released authority still valid')
    _expect_error('CONTROL_NOT_OWNED',
                  lambda: authority.release('actor-2'), error_type=ControlAuthorityError)
    return 'obtain → lease now+300；release → owner=None；他人不能代释放'


def check_control_preempt(tmpdir: str) -> str:
    authority = ControlAuthorityMock(now=NOW)
    authority.obtain(BINDING.actor_id)
    authority.tick(5)
    taken = authority.preempt('actor-2')
    _expect(taken['previous_owner'] == BINDING.actor_id, 'previous owner mismatch')
    _expect(authority.owner == 'actor-2', 'preempt owner mismatch')
    _expect(authority.valid('actor-2') is True, 'preempted lease should be valid')
    _expect(authority.valid(BINDING.actor_id) is False, 'old owner must lose control')
    _expect_error('CONTROL_HELD_BY_OTHER',
                  lambda: authority.obtain('actor-1'), error_type=ControlAuthorityError)
    return 'actor-2 抢占成功，原 owner 失效且不能直接 obtain'


def check_control_expire(tmpdir: str) -> str:
    authority = ControlAuthorityMock(now=NOW)
    authority.obtain(BINDING.actor_id)
    authority.tick(299)
    _expect(authority.valid(BINDING.actor_id) is True, 'lease should still be valid at 299s')
    authority.tick(2)
    _expect(authority.valid(BINDING.actor_id) is False, 'lease should expire after 301s')
    result = authority.expire()
    _expect(result['expired'] is True, 'expire should clear an expired owner')
    _expect(authority.owner is None, 'expired owner not cleared')
    return '299s 仍有效，301s 过期并被平台清理'


# ---------------------------------------------------------------------------
# D. 挂载能力与算法启停事件
# ---------------------------------------------------------------------------

def check_mounted_capabilities(tmpdir: str) -> str:
    mounts = MountedCapabilityMock(
        lenses=['electro_optical'], payloads=['megaphone'],
        configured_algorithms=['fire_smoke'])
    state = mounts.state_fields()
    _expect(payload_mount_message('camera_infrared', state) == LENS_NOT_MOUNTED_MESSAGE,
            'unmounted infrared lens not rejected')
    _expect(payload_mount_message('payload_searchlight', state) == SEARCHLIGHT_NOT_MOUNTED_MESSAGE,
            'unmounted searchlight not rejected')
    _expect(payload_mount_message('camera_electro_optical', state) is None,
            'mounted lens should pass')
    _expect(payload_mount_message('camera_infrared', {'mounted_lenses': None}) is None,
            'unknown mount state must not claim unmounted')
    return '未挂载镜头/探照灯按原文拒绝；清单缺失时保持未核验'


def check_algorithm_lifecycle_events(tmpdir: str) -> str:
    gateway = AlgorithmLifecycleEventMock()
    simulator = MemoryFlightControlSimulator(BINDING, algorithm_gateway=gateway)
    mounts = MountedCapabilityMock(lenses=['electro_optical'], payloads=['megaphone'],
                                   configured_algorithms=['fire_smoke'])
    mounts.apply(simulator, annotation_algorithms={'annotation-1': ['fire_smoke']})
    simulator.apply_task_event('annotation_entered', task_batch_id=BINDING.task_batch_id,
                               annotation_id='annotation-1')
    snapshot = gateway.snapshot()
    _expect(len(snapshot['starts']) == 1, f'starts={snapshot["starts"]}')
    _expect(snapshot['starts'][0]['algorithms'] == ['fire_smoke'], 'start algorithms mismatch')
    _expect(simulator.state.active_algorithms == ['fire_smoke'],
            f'active={simulator.state.active_algorithms}')
    lifecycle = simulator.algorithm_lifecycle()
    stage = lifecycle['annotations']['annotation-1']
    _expect(stage['started'] == ['fire_smoke'], 'started stage missing')
    _expect(stage['execution_mode'] == 'integration-mock', 'execution_mode mismatch')
    simulator.apply_task_event('annotation_left', task_batch_id=BINDING.task_batch_id,
                               annotation_id='annotation-1')
    snapshot = gateway.snapshot()
    _expect(len(snapshot['stops']) == 1, f'stops={snapshot["stops"]}')
    _expect(simulator.state.active_algorithms == [], 'algorithm should stop on annotation left')
    stage = simulator.algorithm_lifecycle()['annotations']['annotation-1']
    _expect(stage['stopped'] == ['fire_smoke'], 'stopped stage missing')
    _expect('fire_smoke' not in simulator.state.active_algorithms, 'stop not applied')
    return '到达→started 确认；离开→stopped 确认；六阶段分开记录'


# ---------------------------------------------------------------------------
# E. MQTT 遥测（复用 tools/mocks）
# ---------------------------------------------------------------------------

def check_mqtt_telemetry(tmpdir: str) -> str:
    simulator = MqttTelemetrySimulator(initial_battery=21)
    frames: list[dict[str, Any]] = []
    simulator.add_listener(frames.append)
    try:
        simulator.start()
        deadline = time.monotonic() + 3.0
        while len(frames) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        simulator.stop()
    _expect(len(frames) >= 2, f'expected >=2 telemetry frames, got {len(frames)}')
    latest = frames[-1]
    _expect(latest['drone_id'] == 'sim-uav-01', 'telemetry drone_id mismatch')
    _expect(latest['battery'] < 20, f'battery={latest["battery"]}')
    _expect(latest.get('alarm') == 'LOW_BATTERY_WARNING', 'low battery alarm missing')
    return f'收到 {len(frames)} 帧，电量 {latest["battery"]}%，低电量告警已触发'


# ---------------------------------------------------------------------------
# F. 运行时只读装配（不开启 --enable-platform-control）
# ---------------------------------------------------------------------------

def check_runtime_readonly_assembly(tmpdir: str) -> str:
    journal = Path(tmpdir) / 'runtime.sqlite3'
    environment = {
        'KTD_BASE_URL': 'https://platform.invalid/api',
        'KTD_SERVER_TOKEN': 'integration-server-token',
        'KTD_ACTOR_EMPLOYEE_ID': BINDING.actor_id,
        'KTD_PROJECT_ID': BINDING.project_id,
        'KTD_SITE_ID': SITE_ID,
        'KTD_HANGAR_ID': BINDING.hangar_id,
        'KTD_DRONE_ID': BINDING.drone_id,
        'FLIGHT_AGENT_TASK_BATCH_ID': BINDING.task_batch_id,
        'FLIGHT_AGENT_GATEWAY_TOKEN': GATEWAY_TOKEN,
        'FLIGHT_AGENT_JOURNAL': str(journal),
        'KTD_TASK_ID': JAVA_TASK_ID,
    }
    config = PlatformRuntimeConfig.from_environment(environment)
    _expect(config.task_id == JAVA_TASK_ID, 'runtime task binding mismatch')
    payload = build_snapshot_payload(BINDING, now=NOW, route_status='idle',
                                     permitted=('read_state', 'move_forward'))
    opener = SnapshotOpener(payload)
    service = build_platform_cockpit(config, lambda: None, opener=opener, clock=lambda: NOW)
    _expect(opener.calls == [], f'build must not request the platform: {opener.calls}')
    _expect(service.executor.execution_mode == 'platform', 'execution_mode mismatch')

    state = service.handle('GET', '/api/flight-control/state', headers=HEADERS)
    _expect(state.status_code == 200, f'/state http={state.status_code}')
    state_body = state.payload['state']
    _expect(state_body['execution_mode'] == 'platform', 'state execution_mode mismatch')
    _expect(state_body['authorization_quality'] == 'authoritative', 'quality mismatch')
    _expect(state_body['available'] is True, 'snapshot should be available')

    context = service.handle('GET', '/api/flight-control/context', headers=HEADERS)
    _expect(context.status_code == 200, f'/context http={context.status_code}')
    tools = {tool['action']: tool for tool in context.payload['tools']}
    _expect(tools['read_state']['available'] is True, 'read_state should be available')
    _expect(tools['move_forward']['available'] is False,
            'control action must stay unavailable without --enable-platform-control')

    before = len(opener.calls)
    receipt = service.executor.preview(_request('move_forward', {'distance_m': 10}))
    _expect(receipt.error_code == 'PLATFORM_DISABLED',
            f'default-off preview code={receipt.error_code}')
    _expect(len(opener.calls) == before, 'disabled preview must not read the platform')
    _expect(all(call == ('GET', '/api/lingxi/agent/platform/snapshot') for call in opener.calls),
            f'unexpected network calls: {opener.calls}')
    return (f'零网络构建；/state 与 /context 可用；控制动作不可用；'
            f'只读快照 GET x{len(opener.calls)}')


CHECKS: tuple[tuple[str, Callable[[str], str]], ...] = (
    ('A1 权威快照只读解码（观测时间/控制权/租约/版本）', check_snapshot_readonly_decode),
    ('A2 route_status=null：飞行动作 fail-closed，只读降级', check_route_status_null_fail_closed),
    ('A3 route_status 缺失（平台未知）：两层 fail-closed', check_route_status_absent_fail_closed),
    ('A4 控制权为他人：拒绝授权', check_control_owner_mismatch),
    ('A5 控制权租约过期：拒绝授权', check_control_lease_expired),
    ('A6 控制权租约缺失：拒绝授权', check_control_lease_missing),
    ('A7 心跳续租 / 停更过期', check_heartbeat_renewal),
    ('A8 能力标志与 availability 矛盾：拒绝', check_capability_conflict),
    ('A9 距离 0.01–20m 边界', check_distance_bounds),
    ('A10 机型白名单与高度基准', check_profile_whitelist),
    ('A11 快照版本格式', check_snapshot_version),
    ('B1 围栏 clear', check_fence_clear),
    ('B2 围栏 blocked：拒绝执行', check_fence_blocked),
    ('B3 围栏 invalid：几何非法拒绝且不发请求', check_fence_invalid),
    ('B4 围栏 timeout：降级跳过不阻断', check_fence_timeout),
    ('B5 围栏 unavailable：降级跳过不阻断', check_fence_unavailable),
    ('C1 控制权 obtain/release', check_control_obtain_release),
    ('C2 控制权抢占', check_control_preempt),
    ('C3 控制权过期清理', check_control_expire),
    ('D1 挂载清单（mounted_lenses/mounted_payloads）', check_mounted_capabilities),
    ('D2 算法启停事件（started/stopped）', check_algorithm_lifecycle_events),
    ('E1 MQTT 遥测与低电量告警（复用 tools/mocks）', check_mqtt_telemetry),
    ('F1 PlatformRuntime 只读装配与默认关闭', check_runtime_readonly_assembly),
)


def run() -> int:
    print('平台契约一致性冒烟（离线）：'
          '权威快照 / 电子围栏 / 控制权 / 挂载与算法 / 遥测 / 运行时只读装配')
    results: list[Result] = []
    with tempfile.TemporaryDirectory(prefix='lingxi-conformance-') as tmpdir:
        for name, check in CHECKS:
            # 现有审计 emit 会写 stdout；成功时收进缓冲，失败时附在 FAIL 明细里。
            audit = io.StringIO()
            try:
                with contextlib.redirect_stdout(audit):
                    detail = check(tmpdir)
            except Exception as error:  # noqa: BLE001 - 冒烟需要把失败串完整打印
                noise = audit.getvalue().strip()
                detail = f'{type(error).__name__}: {error}'
                if noise:
                    detail = f'{detail}\n       审计输出: {noise[-1500:]}'
                results.append(Result(name, False, detail))
            else:
                results.append(Result(name, True, str(detail)))
    print('-' * 96)
    for result in results:
        print(f'[{"PASS" if result.passed else "FAIL"}] {result.name}')
        if result.detail:
            print(f'       {result.detail}')
    passed = sum(1 for result in results if result.passed)
    failed = len(results) - passed
    print('-' * 96)
    print(f'PASS {passed}/{len(results)}  FAIL {failed}')
    if failed:
        print('冒烟失败：以上 FAIL 项未通过，禁止进入真实平台联调。')
        return 1
    print('冒烟通过：契约形状与现有只读路径的 fail-closed 行为一致（不代表真实平台已返回这些值）。')
    return 0


if __name__ == '__main__':
    sys.exit(run())
