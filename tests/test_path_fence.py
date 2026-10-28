"""V1.4 围栏有界调用与环绕曲线校验。

全部用例离线：假传输只验证协议形状、超时与状态机，**不代表真实路径安全**，
也没有连接任何 GIS、电子围栏或设备接口。
"""

import json
import math
import multiprocessing
import threading
import unittest

from lingxi_flight_control import MemoryFlightControlSimulator, RuntimeBinding
from lingxi_flight_control.flight_policy import (
    DEFAULT_PROVIDER_DEADLINE_SEC, CooperativePathProvider, HardBoundedPathProvider,
    PathCheck, PolicyError, UnboundedPathProvider, check_path, classify_path_provider,
    evaluate_path,
)
from lingxi_flight_control.full_control import FlightControlActionRequest
from lingxi_flight_control.ktd_path import KtdPathChecker
from lingxi_flight_control.ktd_platform import (
    KtdHttpResponse, KtdHttpTransport, KtdPlatformError, KtdPlatformExecutor,
    KtdSnapshot, SqliteCommandJournal,
)
from lingxi_flight_control.mapping_interruption import (
    INTERRUPTION_STEPS, MappingInterruptionReceipt,
)
from lingxi_flight_control.geometry import offset_target
from lingxi_flight_control.path_geometry import (
    ORBIT_SEGMENTS, PathGeometryError, PathUnavailable, micro_move_path, orbit_path,
    point_flight_path,
)
from fence_support import (
    BlockingFenceProvider, HardBoundedFenceProvider, StaticFenceProvider,
    TimeoutKwargFenceProvider,
)

BINDING = RuntimeBinding('actor', 'project', 'hangar', 'drone', 'batch', 1)
POSE = {'longitude': 113.0, 'latitude': 23.0, 'height_m': 50.0, 'yaw_deg': 0.0}


def haversine(lng1, lat1, lng2, lat2):
    radius = 6_371_393.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


class FenceTransport:
    """按请求回显折线；只用于验证协议和状态机。"""

    def __init__(self):
        self.status = 'ok'
        self.points = None
        self.invalid = []
        self.segments_override = None
        self.converge = 'ln_outside'
        self.suggest = [{'longitude': 0.0, 'latitude': 0.0}]
        self.error = None
        self.calls = 0
        self.timeouts = []

    def check_path(self, binding, points, *, timeout_sec=None):
        self.calls += 1
        self.timeouts.append(timeout_sec)
        if self.error is not None:
            raise self.error
        self.points = points
        segments = self.segments_override
        if segments is None:
            segments = [{'status': self.status, 'converge': self.converge} for _ in points[1:]]
        return KtdHttpResponse(200, {'code': 200, 'data': {
            'projectId': 'project', 'operatorId': 'actor', 'snapshot': points,
            'invalid': self.invalid, 'segment': segments, 'suggest': self.suggest}})


class FenceProviderTests(unittest.TestCase):
    """provider 层的 clear / blocked / unavailable / timeout / 异常 / malformed。

    这组用例走模拟器语义（``allow_cooperative=True``）：协作式 provider 只在
    模拟器与离线测试里被允许；平台执行路径的拒绝行为见 ``ProviderContractTests``。
    """

    def call(self, provider, action='move_forward', parameters=None, clock=None):
        options = {} if clock is None else {'clock': clock}
        return evaluate_path(provider, BINDING, action,
                             {'distance_m': 10} if parameters is None else parameters, POSE,
                             allow_cooperative=True, **options)

    def test_clear_is_the_only_status_that_passes(self):
        outcome = self.call(StaticFenceProvider(PathCheck('clear')))
        self.assertEqual(outcome.check.status, 'clear')
        self.assertIsNone(outcome.warning)
        self.assertIsNone(check_path(StaticFenceProvider(PathCheck('clear')), BINDING, 'move_forward',
                                     {'distance_m': 10}, POSE, allow_cooperative=True))

    def test_blocked_rejects_and_never_returns_a_warning(self):
        with self.assertRaises(PolicyError) as caught:
            check_path(StaticFenceProvider(PathCheck('blocked')), BINDING, 'move_forward',
                       {'distance_m': 10}, POSE, allow_cooperative=True)
        self.assertEqual(caught.exception.code, 'PATH_BLOCKED')
        with self.assertRaises(PolicyError) as orbit:
            check_path(StaticFenceProvider(PathCheck('blocked')), BINDING, 'point_orbit',
                       {'longitude': 113.0, 'latitude': 23.0, 'radius_m': 30}, POSE,
                       allow_cooperative=True)
        self.assertIn('环绕路径', str(orbit.exception))

    def test_unavailable_degrades_with_explicit_warning(self):
        outcome = self.call(StaticFenceProvider(PathCheck('unavailable')))
        self.assertEqual(outcome.check.status, 'unavailable')
        self.assertIn('电子围栏接口不可用', outcome.warning)
        self.assertIn('不代表路径安全或设备避障已验证', outcome.warning)

    def test_missing_provider_degrades_but_still_reports(self):
        outcome = self.call(None)
        self.assertEqual(outcome.check.status, 'unavailable')
        self.assertEqual(outcome.check.reason, 'PATH_CHECKER_UNAVAILABLE')
        self.assertIn('不代表路径安全', outcome.warning)

    def test_provider_without_bounded_contract_is_never_called(self):
        calls = []

        def unbounded(*args):
            calls.append(args)
            return PathCheck('clear')

        outcome = self.call(unbounded)
        self.assertEqual(outcome.check.status, 'unavailable')
        self.assertEqual(outcome.check.reason, 'PATH_CHECKER_UNBOUNDED')
        self.assertIn('电子围栏接口不可用', outcome.warning)
        self.assertIn('不代表路径安全', outcome.warning)
        self.assertEqual(calls, [])

    def test_declared_but_unusable_budget_is_unbounded_and_never_called(self):
        for value in (0, -1, float('inf'), float('nan'), 60, '3', True):
            provider = StaticFenceProvider(PathCheck('clear'), deadline_sec=value)
            outcome = self.call(provider)
            self.assertEqual(outcome.check.status, 'unavailable', value)
            self.assertEqual(outcome.check.reason, 'PATH_CHECKER_UNBOUNDED', value)
            self.assertEqual(provider.calls, [], value)

    def test_timeout_kwarg_provider_receives_the_budget(self):
        provider = TimeoutKwargFenceProvider(PathCheck('clear'))
        outcome = self.call(provider)
        self.assertEqual(outcome.check.status, 'clear')
        self.assertEqual(provider.calls, ['move_forward'])
        self.assertEqual(provider.timeouts, [DEFAULT_PROVIDER_DEADLINE_SEC])

    def test_declared_deadline_provider_keeps_its_own_budget(self):
        provider = StaticFenceProvider(PathCheck('clear'), deadline_sec=2.0)
        self.assertEqual(self.call(provider).check.status, 'clear')
        self.assertEqual(provider.calls, ['move_forward'])
        self.assertEqual(classify_path_provider(provider).deadline_sec, 2.0)

    def test_plain_deadline_callable_is_cooperative_not_hard_bounded(self):
        provider = StaticFenceProvider(PathCheck('clear'))
        contract = classify_path_provider(provider)
        self.assertIsInstance(contract, CooperativePathProvider)
        self.assertFalse(contract.stops_waiting)
        # 平台默认（allow_cooperative=False）不调用协作式 provider。
        outcome = evaluate_path(provider, BINDING, 'move_forward', {'distance_m': 10}, POSE)
        self.assertEqual(outcome.check.status, 'unavailable')
        self.assertEqual(outcome.check.reason, 'PATH_CHECKER_UNAVAILABLE')
        self.assertEqual(provider.calls, [])

    def test_timeout_stops_waiting_and_is_never_reported_as_clear(self):
        def slow(*_):
            raise TimeoutError('socket timed out')
        outcome = self.call(StaticFenceProvider(slow))
        self.assertEqual(outcome.check.status, 'timeout')
        self.assertIn('电子围栏检查超时', outcome.warning)
        self.assertIn('不代表路径安全', outcome.warning)

    def test_provider_exception_degrades_without_leaking_details(self):
        def broken(*_):
            raise RuntimeError('upstream secret token=abc')
        outcome = self.call(StaticFenceProvider(broken))
        self.assertEqual(outcome.check.status, 'unavailable')
        self.assertEqual(outcome.check.reason, 'PATH_CHECK_ERROR')
        self.assertNotIn('token', outcome.warning)

    def test_malformed_response_is_not_clear(self):
        for value in ({'status': 'clear'}, 'clear', PathCheck('made_up')):
            outcome = self.call(StaticFenceProvider(value))
            self.assertEqual(outcome.check.status, 'malformed')
            self.assertIn('电子围栏响应无法解析', outcome.warning)

    def test_late_clear_beyond_declared_budget_becomes_timeout(self):
        ticks = iter([0.0, 0.5])
        outcome = self.call(StaticFenceProvider(PathCheck('clear'), deadline_sec=0.1),
                            clock=lambda: next(ticks))
        self.assertEqual(outcome.check.status, 'timeout')
        self.assertEqual(outcome.check.reason, 'PATH_CHECK_DEADLINE_EXCEEDED')
        self.assertIn('PATH_CHECK_DEADLINE_EXCEEDED', outcome.warning)
        self.assertIn('不代表路径安全', outcome.warning)

    def test_late_blocked_is_reclassified_as_timeout(self):
        ticks = iter([0.0, 0.5])
        outcome = self.call(StaticFenceProvider(PathCheck('blocked'), deadline_sec=0.1),
                            clock=lambda: next(ticks))
        self.assertEqual(outcome.check.status, 'timeout')
        self.assertEqual(outcome.check.reason, 'PATH_CHECK_DEADLINE_EXCEEDED')

    def test_late_local_geometry_rejection_is_never_reclassified_as_pass(self):
        # 几何非法是本仓确定性结论，不来自 provider 响应，不能被迟到改判成可降级。
        ticks = iter([0.0, 0.5])
        outcome = self.call(StaticFenceProvider(PathCheck('invalid', 'INVALID_PATH_GEOMETRY'),
                                                deadline_sec=0.1),
                            clock=lambda: next(ticks))
        self.assertEqual(outcome.check.status, 'invalid')

    def test_non_path_actions_never_call_the_provider(self):
        provider = StaticFenceProvider(PathCheck('clear'))
        self.assertIsNone(evaluate_path(provider, BINDING, 'camera_photo', {}, POSE))
        self.assertEqual(provider.calls, [])


class ProviderContractTests(unittest.TestCase):
    """provider 契约分层：hard bounded / cooperative / unbounded 的行为边界。

    本仓不创建线程、进程或 asyncio 任务去中断调用：只有底层 transport（socket
    超时）能真正结束等待；协作式 provider 只允许模拟器与离线测试。
    """

    def snapshot(self, now=1000.0):
        return KtdSnapshot(
            actor_id='actor', project_id='project', hangar_id='hangar', drone_id='drone',
            site_id='site', task_id='java-task', context_version=1, version=7,
            observed_at=now, control_owner='actor', profile='DJI_HANGAR',
            control_lease_expires_at=now + 60,
            permissions=frozenset({'move_forward', 'read_state'}),
            capabilities={'move_forward': True, 'UAV_MOVE_DISTANCE': True},
            state={'online': True, 'airborne': True, 'longitude': 113.0, 'latitude': 23.0,
                   'altitude_m': 100.0, 'height_m': 50.0, 'yaw_deg': 90.0,
                   'route_status': 'hover'},
        )

    def executor(self, provider, directory, now=1000.0):
        return KtdPlatformExecutor(
            BINDING, site_id='site', task_id='java-task', transport=_NoSubmit(),
            snapshot_provider=lambda: self.snapshot(now),
            journal=SqliteCommandJournal(directory + '/commands.sqlite3'),
            enabled=True, clock=lambda: now, path_checker=provider)

    def test_hard_bounded_provider_is_called_and_receives_the_budget(self):
        provider = HardBoundedFenceProvider(PathCheck('clear'))
        outcome = evaluate_path(provider, BINDING, 'move_forward', {'distance_m': 10}, POSE)
        self.assertEqual(outcome.check.status, 'clear')
        self.assertEqual(provider.calls, ['move_forward'])
        self.assertEqual(provider.timeouts, [provider.deadline_sec])
        self.assertIsInstance(classify_path_provider(provider), HardBoundedPathProvider)

    def test_provider_without_a_budget_keyword_is_unbounded_and_not_called(self):
        calls = []

        def provider(*args):
            calls.append(args)
            return PathCheck('clear')

        self.assertIsInstance(classify_path_provider(provider), UnboundedPathProvider)
        outcome = evaluate_path(provider, BINDING, 'move_forward', {'distance_m': 10}, POSE)
        self.assertEqual(outcome.check.reason, 'PATH_CHECKER_UNBOUNDED')
        self.assertEqual(calls, [])

    def test_cooperative_provider_is_rejected_at_platform_assembly(self):
        import tempfile
        provider = StaticFenceProvider(PathCheck('clear'))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError) as caught:
                self.executor(provider, directory)
        self.assertIn('PATH_CHECKER_UNAVAILABLE', str(caught.exception))
        self.assertIn('cooperative-only', str(caught.exception))
        self.assertEqual(provider.calls, [])

    def test_zhuoyi_platform_assembly_also_rejects_cooperative_provider(self):
        from lingxi_flight_control.zhuoyi_platform import ZhuoyiPlatformExecutor
        provider = StaticFenceProvider(PathCheck('clear'))
        with self.assertRaises(ValueError) as caught:
            ZhuoyiPlatformExecutor(
                BINDING, base_url='https://platform.test', gateway_token='token',
                site_id='site', transport=lambda *_args: (200, {}), path_checker=provider)
        self.assertIn('PATH_CHECKER_UNAVAILABLE', str(caught.exception))
        self.assertEqual(provider.calls, [])

    def test_permanently_blocking_callable_never_enters_the_platform_path(self):
        import tempfile
        import time
        provider = BlockingFenceProvider(block_sec=2.0)
        with tempfile.TemporaryDirectory() as directory:
            # 装配期就拒绝：不会走到任何 preview/confirm 调用。
            with self.assertRaises(ValueError):
                self.executor(provider, directory)
            self.assertEqual(provider.calls, 0)
            executor = self.executor(None, directory)
            # 绕过装配直接换入（模拟外部误用）：执行期仍不调用，也不等待它返回。
            executor._path_checker = provider
            started = time.monotonic()
            card = executor.preview(FlightControlActionRequest(
                'command', 'key', 'move_forward', {'distance_m': 10}, 1))
            elapsed = time.monotonic() - started
        self.assertEqual(card.status.value, 'preview_required')
        self.assertTrue(any('PATH_CHECKER_UNAVAILABLE' in warning for warning in card.warnings))
        self.assertEqual(provider.calls, 0)
        self.assertLess(elapsed, 1.0)

    def test_platform_without_a_provider_degrades_to_unavailable(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            executor = self.executor(None, directory)
            card = executor.preview(FlightControlActionRequest(
                'command', 'key', 'move_forward', {'distance_m': 10}, 1))
        self.assertEqual(card.status.value, 'preview_required')
        self.assertTrue(any('PATH_CHECKER_UNAVAILABLE' in warning for warning in card.warnings))
        self.assertTrue(any('不代表路径安全' in warning for warning in card.warnings))

    def test_ktd_path_checker_is_hard_bounded_only_with_a_bounded_transport(self):
        self.assertTrue(KtdPathChecker(FenceTransport()).hard_bounded)
        self.assertIsInstance(classify_path_provider(KtdPathChecker(FenceTransport())),
                              HardBoundedPathProvider)

        class Unbounded:
            def check_path(self, binding, points):
                raise AssertionError('unbounded transport must not be called')

        unbounded = KtdPathChecker(Unbounded())
        self.assertFalse(unbounded.hard_bounded)
        self.assertIsInstance(classify_path_provider(unbounded), UnboundedPathProvider)

    def test_ktd_http_transport_timeout_really_returns(self):
        import time
        opener = _RecordingOpener(error=TimeoutError('socket timed out'))
        transport = KtdHttpTransport('https://platform.example/api', token='t' * 16,
                                     actor_id='actor', timeout_sec=5.0, opener=opener)
        checker = KtdPathChecker(transport, deadline_sec=1.0)
        started = time.monotonic()
        result = checker(BINDING, 'move_forward', {'distance_m': 10}, POSE)
        elapsed = time.monotonic() - started
        self.assertEqual(result.status, 'timeout')
        self.assertEqual(result.reason, 'PATH_CHECK_TIMEOUT')
        # 预算真的下到了 urllib socket timeout，而不是只在 checker 里事后计时。
        self.assertEqual(opener.timeouts, [1.0])
        self.assertLess(elapsed, 1.0)

    def test_simulator_accepts_cooperative_fake_but_keeps_simulator_mode(self):
        provider = StaticFenceProvider(PathCheck('clear'))
        sim = MemoryFlightControlSimulator(BINDING, path_checker=provider)
        sim.state.control_owner = 'actor'
        sim.state.altitude_m = 50
        card = sim.preview(FlightControlActionRequest('command', 'key', 'move_forward',
                                                      {'distance_m': 10}, 1))
        self.assertEqual(card.status.value, 'preview_required')
        self.assertEqual(card.execution_mode, 'simulator')
        receipt = sim.confirm(card.confirmation_id, command_id=card.command_id,
                              idempotency_key=card.idempotency_key,
                              confirmation_binding=card.confirmation_binding, context_version=1)
        self.assertEqual(receipt.status.value, 'accepted')
        self.assertEqual(receipt.execution_mode, 'simulator')
        self.assertEqual(provider.calls, ['move_forward', 'move_forward'])


class PathGeometryTests(unittest.TestCase):
    """六向微操 / 指点飞行 / 环绕的确定性几何。"""

    def test_horizontal_directions_are_relative_to_the_heading(self):
        north = micro_move_path('move_forward', {'distance_m': 10}, POSE)
        self.assertGreater(north.points[1]['latitude'], 23.0)
        self.assertAlmostEqual(north.points[1]['longitude'], 113.0, places=6)
        backward = micro_move_path('move_backward', {'distance_m': 10}, POSE)
        self.assertLess(backward.points[1]['latitude'], 23.0)
        right = micro_move_path('move_right', {'distance_m': 10}, POSE)
        self.assertGreater(right.points[1]['longitude'], 113.0)
        left = micro_move_path('move_left', {'distance_m': 10}, POSE)
        self.assertLess(left.points[1]['longitude'], 113.0)

    def test_heading_changes_where_forward_points(self):
        east = micro_move_path('move_forward', {'distance_m': 10}, {**POSE, 'yaw_deg': 90.0})
        self.assertGreater(east.points[1]['longitude'], 113.0)
        self.assertAlmostEqual(east.points[1]['latitude'], 23.0, places=6)
        # 机头朝东时“向右”是朝南，不是朝东。
        south = micro_move_path('move_right', {'distance_m': 10}, {**POSE, 'yaw_deg': 90.0})
        self.assertLess(south.points[1]['latitude'], 23.0)

    def test_dji_move_uses_absolute_altitude_not_relative_height(self):
        pose = {**POSE, 'profile': 'DJI_HANGAR', 'altitude_m': 100.0}
        forward = micro_move_path('move_forward', {'distance_m': 10}, pose)
        self.assertEqual(forward.vertical_field, 'altitude_m')
        self.assertEqual(forward.points[0]['altitude'], 100.0)
        self.assertEqual(forward.points[1]['altitude'], 100.0)
        up = micro_move_path('move_up', {'distance_m': 10}, pose)
        self.assertEqual(up.points[1]['altitude'], 110.0)
        with self.assertRaises(PathUnavailable):
            micro_move_path('move_forward', {'distance_m': 10},
                            {**POSE, 'profile': 'DJI_HANGAR'})

    def test_pz_move_keeps_relative_height_when_absolute_altitude_is_also_present(self):
        pose = {**POSE, 'profile': 'PZ_HANGAR', 'altitude_m': 100.0}
        forward = micro_move_path('move_forward', {'distance_m': 10}, pose)
        self.assertEqual(forward.vertical_field, 'height_m')
        self.assertEqual(forward.points[0]['altitude'], 50.0)

    def test_vertical_moves_only_change_relative_height(self):
        up = micro_move_path('move_up', {'distance_m': 10}, POSE)
        self.assertEqual(up.points[1]['altitude'], 60.0)
        self.assertEqual(up.points[1]['longitude'], 113.0)
        self.assertEqual(up.points[1]['latitude'], 23.0)
        self.assertEqual(up.vertical_field, 'height_m')
        down = micro_move_path('move_down', {'distance_m': 10}, POSE)
        self.assertEqual(down.points[1]['altitude'], 40.0)

    def test_invalid_geometry_is_rejected_instead_of_guessed(self):
        for action, parameters, pose in (
            ('move_forward', {'distance_m': 0}, POSE),
            ('move_forward', {'distance_m': -5}, POSE),
            ('move_forward', {'distance_m': 10}, {**POSE, 'yaw_deg': None}),
            ('move_down', {'distance_m': 80}, POSE),
            ('move_up', {'distance_m': 20000}, POSE),
        ):
            with self.assertRaises((PathGeometryError, PathUnavailable)):
                micro_move_path(action, parameters, pose)
        with self.assertRaises(PathGeometryError):
            micro_move_path('move_down', {'distance_m': 80}, POSE)

    def test_missing_pose_is_unavailable_not_a_guess(self):
        with self.assertRaises(PathUnavailable):
            micro_move_path('move_forward', {'distance_m': 10}, {'height_m': 50, 'yaw_deg': 0})
        with self.assertRaises(PathUnavailable):
            micro_move_path('move_forward', {'distance_m': 10}, {'longitude': 113, 'latitude': 23,
                                                                 'yaw_deg': 0})

    def test_point_flight_uses_the_fly_height_field(self):
        geometry = point_flight_path({'longitude': 113.01, 'latitude': 23.01, 'altitude_m': 120.0},
                                     {**POSE, 'altitude_m': 100.0})
        self.assertEqual(geometry.segments, 1)
        self.assertEqual(geometry.points[1]['altitude'], 120.0)
        self.assertEqual(geometry.points[0]['altitude'], 100.0)
        self.assertEqual(geometry.vertical_field, 'altitude_m')

    def test_point_flight_refuses_to_mix_height_datums(self):
        with self.assertRaises(PathUnavailable) as caught:
            point_flight_path({'longitude': 113.01, 'latitude': 23.01, 'altitude_m': 120.0}, POSE)
        self.assertEqual(caught.exception.reason, 'PATH_ALTITUDE_DATUM_MISMATCH')
    def test_point_flight_rejects_out_of_range_targets(self):
        state = {**POSE, 'altitude_m': 100.0}
        for parameters in ({'longitude': 181.0, 'latitude': 23.0, 'altitude_m': 120.0},
                           {'longitude': 113.0, 'latitude': 91.0, 'altitude_m': 120.0},
                           {'longitude': 113.0, 'latitude': 23.0, 'altitude_m': 1.0}):
            with self.assertRaises(PathGeometryError):
                point_flight_path(parameters, state)

    def test_orbit_is_a_closed_curve_with_36_segments_and_entry(self):
        geometry = orbit_path({'longitude': 113.0, 'latitude': 23.0, 'radius_m': 30.0}, POSE)
        self.assertTrue(geometry.closed)
        self.assertTrue(geometry.entry_segment)
        self.assertEqual(len(geometry.points), ORBIT_SEGMENTS + 2)
        self.assertEqual(geometry.segments, ORBIT_SEGMENTS + 1)
        ring = geometry.points[1:]
        self.assertEqual(ring[0], ring[-1])  # 闭合边回到起点
        for point in ring:
            self.assertAlmostEqual(haversine(113.0, 23.0, point['longitude'], point['latitude']),
                                   30.0, delta=0.5)
        # 相邻环点确实是圆弧上的下一段，不是把整圈退化成一条弦。
        step = haversine(ring[0]['longitude'], ring[0]['latitude'],
                         ring[1]['longitude'], ring[1]['latitude'])
        self.assertAlmostEqual(step, 2 * 30 * math.sin(math.pi / ORBIT_SEGMENTS), delta=0.05)
        self.assertGreater(step, 5.0)

    def test_orbit_already_on_the_radius_does_not_invent_a_leg_to_north(self):
        longitude, latitude = offset_target(113.0, 23.0, 90.0, 30.0)
        geometry = orbit_path({'longitude': 113.0, 'latitude': 23.0, 'radius_m': 30.0},
                              {**POSE, 'longitude': longitude, 'latitude': latitude})
        self.assertFalse(geometry.entry_segment)
        self.assertEqual(geometry.segments, ORBIT_SEGMENTS)
        self.assertEqual(geometry.points[0], geometry.points[-1])

    def test_orbit_without_pose_skips_only_the_entry_segment(self):
        geometry = orbit_path({'longitude': 113.0, 'latitude': 23.0, 'radius_m': 30.0},
                              {'height_m': 50.0})
        self.assertFalse(geometry.entry_segment)
        self.assertEqual(geometry.segments, ORBIT_SEGMENTS)
        self.assertEqual(geometry.points[0], geometry.points[-1])

    def test_orbit_rejects_out_of_range_inputs(self):
        for parameters in ({'longitude': 113.0, 'latitude': 23.0, 'radius_m': 0},
                           {'longitude': 113.0, 'latitude': 23.0, 'radius_m': 5001},
                           {'longitude': 181.0, 'latitude': 23.0, 'radius_m': 30},
                           {'longitude': 113.0, 'latitude': 95.0, 'radius_m': 30}):
            with self.assertRaises((PathGeometryError, PathUnavailable)):
                orbit_path(parameters, POSE)
        with self.assertRaises(PathGeometryError):
            orbit_path({'longitude': 113.0, 'latitude': 23.0, 'radius_m': 5001}, POSE)
        with self.assertRaises(PathUnavailable):
            orbit_path({'longitude': 113.0, 'latitude': 23.0, 'radius_m': 30}, {'longitude': 113.0,
                                                                               'latitude': 23.0})


class CheckerContractTests(unittest.TestCase):
    """KtdPathChecker：平台响应必须逐项对得上请求。"""

    def test_clear_requires_matching_snapshot_and_segments(self):
        transport = FenceTransport()
        checker = KtdPathChecker(transport)
        result = checker(BINDING, 'move_forward', {'distance_m': 10}, POSE)
        self.assertEqual(result.status, 'clear')
        self.assertEqual(result.segments, 1)
        self.assertEqual(transport.points[1]['altitude'], 50.0)

    def test_segment_count_mismatch_is_malformed(self):
        transport = FenceTransport()
        transport.segments_override = []
        result = KtdPathChecker(transport)(BINDING, 'move_forward', {'distance_m': 10}, POSE)
        self.assertEqual(result.status, 'malformed')

    def test_snapshot_echo_mismatch_is_malformed(self):
        class Echo(FenceTransport):
            def check_path(self, binding, points, *, timeout_sec=None):
                response = super().check_path(binding, points, timeout_sec=timeout_sec)
                payload = json.loads(json.dumps(response.payload))
                payload['data']['snapshot'] = payload['data']['snapshot'][::-1]
                return KtdHttpResponse(200, payload)
        result = KtdPathChecker(Echo())(BINDING, 'move_forward', {'distance_m': 10}, POSE)
        self.assertEqual(result.status, 'malformed')

    def test_other_project_or_operator_is_not_accepted(self):
        class Foreign(FenceTransport):
            def check_path(self, binding, points, *, timeout_sec=None):
                response = super().check_path(binding, points, timeout_sec=timeout_sec)
                payload = json.loads(json.dumps(response.payload))
                payload['data']['operatorId'] = 'someone-else'
                return KtdHttpResponse(200, payload)
        result = KtdPathChecker(Foreign())(BINDING, 'move_forward', {'distance_m': 10}, POSE)
        self.assertEqual(result.status, 'unavailable')

    def test_invalid_list_blocks(self):
        transport = FenceTransport()
        transport.invalid = [{'index': 0}]
        result = KtdPathChecker(transport)(BINDING, 'move_forward', {'distance_m': 10}, POSE)
        self.assertEqual(result.status, 'blocked')
        self.assertEqual(result.segment_index, 0)

    def test_unverified_converge_never_becomes_clear(self):
        transport = FenceTransport()
        transport.converge = 'ln_inside'
        result = KtdPathChecker(transport)(BINDING, 'move_forward', {'distance_m': 10}, POSE)
        self.assertEqual(result.status, 'unavailable')
        self.assertEqual(result.reason, 'PATH_CONVERGE_UNVERIFIED')

    def test_suggested_reroute_is_ignored(self):
        transport = FenceTransport()
        result = KtdPathChecker(transport)(BINDING, 'move_forward', {'distance_m': 10}, POSE)
        self.assertEqual(result.status, 'clear')
        self.assertEqual(transport.suggest, [{'longitude': 0.0, 'latitude': 0.0}])
        self.assertEqual(transport.points[1]['source'], 'new')

    def test_orbit_entry_segment_blocked_is_reported_as_segment_zero(self):
        transport = FenceTransport()
        transport.segments_override = None

        class EntryBlocked(FenceTransport):
            def check_path(self, binding, points, *, timeout_sec=None):
                self.points = points
                return KtdHttpResponse(200, {'code': 200, 'data': {
                    'projectId': 'project', 'operatorId': 'actor', 'snapshot': points,
                    'invalid': [], 'suggest': [],
                    'segment': [{'status': 'ng' if index == 0 else 'ok', 'converge': 'ln_outside'}
                                for index in range(len(points) - 1)]}})
        result = KtdPathChecker(EntryBlocked())(BINDING, 'point_orbit',
                                                {'longitude': 113.0, 'latitude': 23.0, 'radius_m': 30},
                                                POSE)
        self.assertEqual(result.status, 'blocked')
        self.assertEqual(result.segment_index, 0)
        self.assertEqual(result.segments, ORBIT_SEGMENTS + 1)

    def test_transport_timeout_becomes_timeout_status(self):
        transport = FenceTransport()
        transport.error = TimeoutError('timed out')
        result = KtdPathChecker(transport)(BINDING, 'move_forward', {'distance_m': 10}, POSE)
        self.assertEqual(result.status, 'timeout')

    def test_transport_without_a_single_call_budget_is_never_called(self):
        class Unbounded:
            def __init__(self):
                self.calls = 0

            def check_path(self, binding, points):
                self.calls += 1
                raise AssertionError('unbounded transport must not be called')

        transport = Unbounded()
        result = KtdPathChecker(transport)(BINDING, 'move_forward', {'distance_m': 10}, POSE)
        self.assertEqual(result.status, 'unavailable')
        self.assertEqual(result.reason, 'PATH_TRANSPORT_UNBOUNDED')
        self.assertEqual(transport.calls, 0)

    def test_late_blocked_conclusion_is_reclassified_as_timeout(self):
        ticks = iter([0.0, 5.0])
        transport = FenceTransport()
        transport.status = 'ng'
        result = KtdPathChecker(transport, deadline_sec=0.1,
                                clock=lambda: next(ticks))(BINDING, 'move_forward',
                                                           {'distance_m': 10}, POSE)
        self.assertEqual(result.status, 'timeout')
        self.assertEqual(result.reason, 'PATH_CHECK_DEADLINE_EXCEEDED')

    def test_deadline_is_forwarded_to_the_transport(self):
        seen = {}

        class Bounded(FenceTransport):
            def check_path(self, binding, points, *, timeout_sec=None):
                seen['timeout_sec'] = timeout_sec
                return super().check_path(binding, points, timeout_sec=timeout_sec)
        checker = KtdPathChecker(Bounded(), deadline_sec=1.5)
        checker(BINDING, 'move_forward', {'distance_m': 10}, POSE)
        self.assertEqual(seen['timeout_sec'], 1.5)
        self.assertEqual(checker.deadline_sec, 1.5)

    def test_invalid_deadline_is_refused(self):
        for value in (0, -1, 0.05, 60, None):
            with self.assertRaises(ValueError):
                KtdPathChecker(FenceTransport(), deadline_sec=value)


class _RawResponse:
    def __init__(self, status, body):
        self.code = status
        self._body = body

    def read(self, _size=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class _RecordingOpener:
    def __init__(self, payload=None, error=None):
        self.payload = payload if payload is not None else {'code': 200, 'data': {}}
        self.error = error
        self.timeouts = []
        self.bodies = []

    def open(self, request, timeout=None):
        self.timeouts.append(timeout)
        self.bodies.append(json.loads(request.data))
        if self.error is not None:
            raise self.error
        return _RawResponse(200, json.dumps(self.payload).encode())


class BoundedTransportTests(unittest.TestCase):
    """KtdHttpTransport 的折线约束与单次 deadline。"""

    def transport(self, opener):
        return KtdHttpTransport('https://platform.example/api', token='t' * 16, actor_id='actor',
                                timeout_sec=5.0, opener=opener)

    def test_multi_segment_polyline_is_accepted_and_deadline_is_capped(self):
        opener = _RecordingOpener()
        points = [{'longitude': 113.0, 'latitude': 23.0, 'altitude': 50.0, 'source': 'old'},
                  {'longitude': 113.001, 'latitude': 23.0, 'altitude': 50.0, 'source': 'new'}]
        self.transport(opener).check_path(BINDING, points, timeout_sec=1.0)
        self.assertEqual(opener.timeouts, [1.0])
        self.assertEqual(opener.bodies[0]['points'], points)

    def test_polyline_shape_is_validated_before_any_request(self):
        opener = _RecordingOpener()
        transport = self.transport(opener)
        bad = [
            [{'longitude': 113.0, 'latitude': 23.0, 'altitude': 50.0, 'source': 'old'}],
            [{'longitude': 113.0, 'latitude': 23.0, 'altitude': 50.0, 'source': 'old'}] * 130,
            [{'longitude': 113.0, 'latitude': 23.0, 'altitude': 50.0, 'source': 'old'},
             {'longitude': 200.0, 'latitude': 23.0, 'altitude': 50.0, 'source': 'new'}],
            [{'longitude': 113.0, 'latitude': 23.0, 'altitude': 50.0, 'source': 'old'},
             {'longitude': 113.1, 'latitude': 23.0, 'altitude': 50.0, 'source': 'mid'}],
            [{'longitude': 113.0, 'latitude': 23.0, 'altitude': 50.0, 'source': 'old'},
             {'longitude': 113.1, 'latitude': 23.0, 'altitude': 50.0}],
        ]
        for points in bad:
            with self.assertRaises(KtdPlatformError):
                transport.check_path(BINDING, points)
        self.assertEqual(opener.timeouts, [])

    def test_deadline_argument_is_bounded(self):
        transport = self.transport(_RecordingOpener())
        points = [{'longitude': 113.0, 'latitude': 23.0, 'altitude': 50.0, 'source': 'old'},
                  {'longitude': 113.001, 'latitude': 23.0, 'altitude': 50.0, 'source': 'new'}]
        for value in (0, 0.01, 60, float('nan')):
            with self.assertRaises(KtdPlatformError):
                transport.check_path(BINDING, points, timeout_sec=value)


class SimulatorFenceTests(unittest.TestCase):
    """模拟器闭环：降级不绕过控制权与确认，blocked 撤销待确认卡。"""

    def setUp(self):
        self.path = PathCheck('clear')
        self.provider = StaticFenceProvider(lambda *_: self.path)
        self.sim = MemoryFlightControlSimulator(BINDING, path_checker=self.provider)
        self.sim.state.control_owner = 'actor'
        self.sim.state.altitude_m = 50
        self.counter = 0

    def preview(self, action, **parameters):
        self.counter += 1
        return self.sim.preview(FlightControlActionRequest(
            str(self.counter), str(self.counter), action, parameters, 1))

    def confirm(self, card):
        return self.sim.confirm(card.confirmation_id, command_id=card.command_id,
            idempotency_key=card.idempotency_key,
            confirmation_binding=card.confirmation_binding, context_version=1)

    def test_preview_and_confirm_both_check_the_fence(self):
        card = self.preview('move_forward', distance_m=10)
        self.assertEqual(self.provider.calls, ['move_forward'])
        receipt = self.confirm(card)
        self.assertEqual(receipt.status.value, 'accepted')
        self.assertEqual(self.provider.calls, ['move_forward', 'move_forward'])

    def test_unbounded_provider_degrades_and_is_never_called(self):
        calls = []

        def unbounded(*args):
            calls.append(args)
            return PathCheck('clear')

        self.sim._path_checker = unbounded
        card = self.preview('move_forward', distance_m=10)
        self.assertEqual(card.status.value, 'preview_required')
        self.assertTrue(any('不代表路径安全' in warning for warning in card.warnings))
        receipt = self.confirm(card)
        self.assertEqual(receipt.status.value, 'accepted')
        self.assertTrue(any('不代表路径安全' in warning for warning in receipt.warnings))
        self.assertEqual(calls, [])

    def test_blocked_at_confirm_withdraws_the_pending_card(self):
        card = self.preview('move_forward', distance_m=10)
        self.path = PathCheck('blocked')
        receipt = self.confirm(card)
        self.assertEqual(receipt.error_code, 'PATH_BLOCKED')
        self.assertEqual(self.sim.state.longitude, 0.0)
        self.assertEqual(self.confirm(card).error_code, 'CONFIRMATION_NOT_FOUND')

    def test_orbit_blocked_at_preview_never_registers_a_card(self):
        self.path = PathCheck('blocked')
        card = self.preview('point_orbit', longitude=113.0, latitude=23.0, radius_m=30)
        self.assertEqual(card.error_code, 'PATH_BLOCKED')
        self.assertIsNone(card.confirmation_id)
        self.assertEqual(self.sim.state.route_status, 'idle')

    def test_degraded_fence_still_requires_control_and_confirmation(self):
        self.path = PathCheck('unavailable')
        self.sim.state.control_owner = None
        self.assertEqual(self.preview('move_forward', distance_m=10).error_code, 'CONTROL_NOT_OWNED')
        self.sim.state.control_owner = 'actor'
        card = self.preview('move_forward', distance_m=10)
        self.assertEqual(card.status.value, 'preview_required')
        self.assertTrue(any('不代表路径安全' in warning for warning in card.warnings))
        self.assertEqual(self.sim.state.longitude, 0.0)  # 预览不执行
        receipt = self.confirm(card)
        self.assertEqual(receipt.status.value, 'accepted')
        self.assertTrue(any('不代表路径安全' in warning for warning in receipt.warnings))
        self.assertNotEqual(self.sim.state.latitude, 0.0)  # 确认后才真的位移

    def test_pose_change_invalidates_the_path_card(self):
        card = self.preview('move_forward', distance_m=10)
        self.sim.state.longitude = 113.5
        self.assertEqual(self.confirm(card).error_code, 'POLICY_STATE_CHANGED')

    def test_invalid_geometry_rejects_without_moving(self):
        self.path = PathCheck('invalid', 'PATH_ALTITUDE_OUT_OF_RANGE')
        card = self.preview('move_down', distance_m=10)
        self.assertEqual(card.error_code, 'PATH_ALTITUDE_OUT_OF_RANGE')
        self.assertEqual(self.sim.state.altitude_m, 50)


class PlatformFenceTests(unittest.TestCase):
    """平台执行器：preview 与 confirm 都检查，blocked 撤销卡片，环绕显示语义差异。"""

    def setUp(self):
        import tempfile
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.fence = FenceTransport()
        self.now = 1000.0
        self.snapshot = KtdSnapshot(
            actor_id='actor', project_id='project', hangar_id='hangar', drone_id='drone',
            site_id='site', task_id='java-task', context_version=1, version=7,
            observed_at=self.now, control_owner='actor', profile='DJI_HANGAR',
            control_lease_expires_at=self.now + 60,
            permissions=frozenset({'move_forward', 'point_orbit', 'point_flight', 'read_state'}),
            capabilities={'move_forward': True, 'point_orbit': True, 'point_flight': True,
                          'UAV_MOVE_DISTANCE': True},
            state={'online': True, 'airborne': True, 'longitude': 113.0, 'latitude': 23.0,
                   'altitude_m': 100.0, 'height_m': 50.0, 'yaw_deg': 90.0,
                   'route_status': 'hover'},
        )
        self.transport = _NoSubmit()
        self.executor = KtdPlatformExecutor(
            BINDING, site_id='site', task_id='java-task', transport=self.transport,
            snapshot_provider=lambda: self.snapshot,
            journal=SqliteCommandJournal(self.directory.name + '/commands.sqlite3'),
            enabled=True, clock=lambda: self.now,
            path_checker=KtdPathChecker(self.fence))

    def preview(self, action, parameters):
        return self.executor.preview(FlightControlActionRequest('command', action + '-1', action,
                                                                parameters, 1))

    def confirm(self, card):
        return self.executor.confirm(card.confirmation_id, command_id=card.command_id,
            idempotency_key=card.idempotency_key,
            confirmation_binding=card.confirmation_binding, context_version=1)

    def test_preview_and_confirm_both_check_and_blocked_withdraws(self):
        card = self.preview('move_forward', {'distance_m': 10})
        self.assertEqual(card.status.value, 'preview_required')
        self.assertEqual(self.fence.points[0]['altitude'], 100.0)
        self.assertEqual(self.fence.points[1]['altitude'], 100.0)
        self.assertEqual(self.fence.calls, 1)
        self.assertEqual(self.fence.timeouts, [3.0])  # 单次预算已下发到传输层
        self.fence.status = 'ng'
        receipt = self.confirm(card)
        self.assertEqual(receipt.error_code, 'PATH_BLOCKED')
        self.assertEqual(self.fence.calls, 2)
        self.assertEqual(self.transport.calls, 0)  # 拦截动作不产生平台写调用
        self.assertEqual(self.confirm(card).error_code, 'CONFIRMATION_NOT_FOUND')

    def test_unbounded_provider_degrades_on_the_card_without_being_called(self):
        calls = []

        def unbounded(*args):
            calls.append(args)
            return PathCheck('clear')

        self.executor._path_checker = unbounded
        card = self.preview('move_forward', {'distance_m': 10})
        self.assertEqual(card.status.value, 'preview_required')
        self.assertTrue(any('不代表路径安全' in warning for warning in card.warnings))
        self.assertEqual(calls, [])
        self.assertEqual(self.fence.calls, 0)

    def test_degraded_fence_warns_on_the_card_but_keeps_the_flow(self):
        self.fence.error = RuntimeError('fence down')
        card = self.preview('move_forward', {'distance_m': 10})
        self.assertEqual(card.status.value, 'preview_required')
        self.assertTrue(any('不代表路径安全' in warning for warning in card.warnings))

    def test_orbit_radius_mismatch_is_rejected_before_fence_or_write(self):
        card = self.preview('point_orbit', {'longitude': 113.01, 'latitude': 23.01, 'radius_m': 30})
        self.assertEqual(card.error_code, 'ORBIT_RADIUS_NOT_COMMANDED')
        self.assertIsNone(card.confirmation_id)
        self.assertIn('未下发', card.message)
        self.assertEqual(self.fence.calls, 0)
        self.assertEqual(self.transport.calls, 0)

    def test_orbit_on_the_requested_radius_checks_that_circle_only(self):
        longitude, latitude = offset_target(113.0, 23.0, 90.0, 30.0)
        state = dict(self.snapshot.state, longitude=longitude, latitude=latitude)
        self.snapshot = KtdSnapshot(**{**self.snapshot.__dict__, 'state': state})
        card = self.preview('point_orbit', {'longitude': 113.0, 'latitude': 23.0, 'radius_m': 30})
        self.assertEqual(card.status.value, 'preview_required')
        self.assertTrue(any('一致' in warning and '不接收半径' in warning for warning in card.warnings))
        self.assertEqual(len(self.fence.points), ORBIT_SEGMENTS + 1)

    def test_orbit_without_pose_is_rejected_before_write(self):
        state = {key: value for key, value in self.snapshot.state.items()
                 if key not in {'longitude', 'latitude'}}
        self.snapshot = KtdSnapshot(**{**self.snapshot.__dict__, 'state': state})
        card = self.preview('point_orbit', {'longitude': 113.0, 'latitude': 23.0, 'radius_m': 30})
        self.assertEqual(card.error_code, 'ORBIT_POSE_UNAVAILABLE')
        self.assertEqual(self.fence.calls, 0)
        self.assertEqual(self.transport.calls, 0)

    def test_orbit_drift_off_the_radius_withdraws_the_card(self):
        longitude, latitude = offset_target(113.0, 23.0, 0.0, 30.0)
        state = dict(self.snapshot.state, longitude=longitude, latitude=latitude)
        self.snapshot = KtdSnapshot(**{**self.snapshot.__dict__, 'state': state})
        card = self.preview('point_orbit', {'longitude': 113.0, 'latitude': 23.0, 'radius_m': 30})
        self.assertEqual(card.status.value, 'preview_required')
        drifted = dict(state, longitude=113.0, latitude=23.0)
        self.snapshot = KtdSnapshot(**{**self.snapshot.__dict__, 'state': drifted})
        receipt = self.confirm(card)
        self.assertEqual(receipt.error_code, 'ORBIT_RADIUS_NOT_COMMANDED')
        self.assertEqual(self.transport.calls, 0)
        self.assertEqual(self.confirm(card).error_code, 'CONFIRMATION_NOT_FOUND')

    def test_point_flight_checks_the_target_point(self):
        card = self.preview('point_flight',
                            {'longitude': 113.01, 'latitude': 23.01, 'altitude_m': 100.0})
        self.assertEqual(card.status.value, 'preview_required')
        self.assertEqual(self.fence.points[1]['altitude'], 100.0)
        self.fence.status = 'ng'
        self.assertEqual(self.confirm(card).error_code, 'PATH_BLOCKED')

    def test_diagonal_point_flight_is_rejected_before_the_fence(self):
        card = self.preview('point_flight',
                            {'longitude': 113.01, 'latitude': 23.01, 'altitude_m': 120.0})
        self.assertEqual(card.error_code, 'FLYTO_TRAJECTORY_UNVERIFIED')
        self.assertEqual(self.fence.calls, 0)
        self.assertEqual(self.transport.calls, 0)

    def test_snapshot_version_or_pose_change_invalidates_the_card(self):
        card = self.preview('move_forward', {'distance_m': 10})
        self.snapshot = KtdSnapshot(**{**self.snapshot.__dict__, 'version': 8})
        self.assertEqual(self.confirm(card).error_code, 'AUTHORITATIVE_STATE_CHANGED')
        card = self.preview('move_forward', {'distance_m': 10})
        state = dict(self.snapshot.state, longitude=113.0002)
        self.snapshot = KtdSnapshot(**{**self.snapshot.__dict__, 'state': state})
        self.assertEqual(self.confirm(card).error_code, 'AUTHORITATIVE_STATE_CHANGED')


class _OrderedGateway:
    """假测绘中断网关：只记录调用顺序，不做任何真实作废。"""

    execution_mode = 'fake-atomic'

    def __init__(self, order, receipt=None):
        self.order = order
        self.calls = []
        self.receipt = receipt

    def interrupt(self, request):
        self.calls.append(request)
        self.order.append('mapping')
        if self.receipt is not None:
            return self.receipt
        return MappingInterruptionReceipt(
            atomic=True, execution_mode=self.execution_mode, confirmed=True,
            completed_steps=INTERRUPTION_STEPS, audit_id='fake-1')


class _OrderedSubmit:
    """记录提交顺序的假传输；只回 200/{"code":200}，不代表设备已执行。"""

    def __init__(self, order):
        self.order = order
        self.calls = 0

    def submit(self, command, context):
        self.calls += 1
        self.order.append('submit')
        return KtdHttpResponse(200, {'code': 200})


class PlatformMappingFenceTests(unittest.TestCase):
    """V1.4 P1：confirm 阶段围栏必须先于测绘原子作废。

    blocked 不得触碰网关与平台；timeout/unavailable 只降级并保留 warning，
    顺序始终是「围栏检查 → 测绘作废 → 平台提交」。
    """

    def setUp(self):
        import tempfile
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.fence = FenceTransport()
        self.now = 1000.0
        self.order = []
        self.snapshot = KtdSnapshot(
            actor_id='actor', project_id='project', hangar_id='hangar', drone_id='drone',
            site_id='site', task_id='java-task', context_version=1, version=7,
            observed_at=self.now, control_owner='actor', profile='DJI_HANGAR',
            control_lease_expires_at=self.now + 60,
            permissions=frozenset({'move_forward', 'read_state'}),
            capabilities={'move_forward': True, 'UAV_MOVE_DISTANCE': True},
            state={'online': True, 'airborne': True, 'longitude': 113.0, 'latitude': 23.0,
                   'altitude_m': 100.0, 'height_m': 50.0, 'yaw_deg': 90.0,
                   'task_type': 'mapping', 'route_status': 'running', 'route_id': 'route-main'},
        )
        self.transport = _OrderedSubmit(self.order)
        self.gateway = _OrderedGateway(self.order)
        self.executor = KtdPlatformExecutor(
            BINDING, site_id='site', task_id='java-task', transport=self.transport,
            snapshot_provider=lambda: self.snapshot,
            journal=SqliteCommandJournal(self.directory.name + '/commands.sqlite3'),
            enabled=True, clock=lambda: self.now,
            path_checker=KtdPathChecker(self.fence), mapping_gateway=self.gateway)

    def preview(self, action='move_forward', parameters=None, key='key-1'):
        return self.executor.preview(FlightControlActionRequest(
            'command-' + key, key, action, parameters or {'distance_m': 10}, 1))

    def confirm(self, card):
        return self.executor.confirm(card.confirmation_id, command_id=card.command_id,
            idempotency_key=card.idempotency_key,
            confirmation_binding=card.confirmation_binding, context_version=1)

    def test_blocked_at_confirm_never_reaches_the_mapping_gateway(self):
        card = self.preview()
        self.assertEqual(card.to_dict()['status'], 'preview_required')
        self.assertEqual(self.gateway.calls, [])          # preview 不作废测绘
        self.fence.status = 'ng'
        receipt = self.confirm(card)
        self.assertEqual(receipt.error_code, 'PATH_BLOCKED')
        self.assertEqual(self.gateway.calls, [])          # blocked 不触碰不可逆操作
        self.assertEqual(self.transport.calls, 0)
        self.assertNotIn('mapping_interruption', receipt.to_dict())
        self.assertEqual(self.confirm(card).error_code, 'CONFIRMATION_NOT_FOUND')

    def test_timeout_at_confirm_degrades_warns_and_still_dispatches(self):
        card = self.preview()
        self.fence.error = TimeoutError('fence timed out')
        receipt = self.confirm(card)
        self.assertEqual(receipt.to_dict()['status'], 'submitted')
        self.assertEqual(len(self.gateway.calls), 1)
        self.assertEqual(self.transport.calls, 1)
        self.assertEqual(self.order, ['mapping', 'submit'])
        warnings = [warning.lower() for warning in receipt.warnings]
        self.assertTrue(any('超时' in warning and 'timeout' in warning for warning in warnings))
        # 降级话术必须让人看出「检查没做成」，不能读成路径安全或 clear。
        self.assertTrue(any('不代表路径安全' in warning for warning in warnings))
        self.assertFalse(any('clear' in warning for warning in warnings))
        self.assertTrue(receipt.to_dict()['mapping_interruption']['atomic'])

    def test_unavailable_at_confirm_degrades_but_keeps_control_and_confirmation_checks(self):
        card = self.preview()
        self.fence.error = RuntimeError('fence down')
        receipt = self.confirm(card)
        self.assertEqual(receipt.to_dict()['status'], 'submitted')
        self.assertEqual(self.order, ['mapping', 'submit'])
        self.assertTrue(any('电子围栏接口不可用' in warning for warning in receipt.warnings))
        self.assertTrue(any('不代表路径安全' in warning for warning in receipt.warnings))

        # 降级不绕过控制权：控制权丢失时仍在围栏之前拒绝，且不触碰网关。
        card = self.preview(key='key-2')
        self.fence.error = RuntimeError('fence down')
        self.snapshot = KtdSnapshot(**{**self.snapshot.__dict__, 'control_owner': 'other'})
        rejected = self.confirm(card)
        self.assertEqual(rejected.error_code, 'CONTROL_NOT_OWNED')
        self.assertEqual(len(self.gateway.calls), 1)
        self.assertEqual(self.transport.calls, 1)

    def test_degraded_fence_does_not_skip_the_confirmation_binding(self):
        card = self.preview()
        self.fence.error = RuntimeError('fence down')
        wrong = self.executor.confirm(card.confirmation_id, command_id=card.command_id,
                                      idempotency_key=card.idempotency_key,
                                      confirmation_binding='wrong', context_version=1)
        self.assertEqual(wrong.error_code, 'CONFIRMATION_MISMATCH')
        self.assertEqual(self.gateway.calls, [])
        self.assertEqual(self.transport.calls, 0)


class _NoSubmit:
    """任何写请求都必须失败：本组用例只验证 preview 与围栏，不下发平台指令。"""

    def __init__(self):
        self.calls = 0

    def submit(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError('platform write must not happen in fence tests')

    def submit_vendor_request(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError('vendor write must not happen in fence tests')


class ResourceHygieneTests(unittest.TestCase):
    """超时后不得留下线程、子进程或额外监听端口。"""

    def test_timeout_leaves_no_threads_or_children(self):
        before = set(threading.enumerate())
        children_before = len(multiprocessing.active_children())
        transport = FenceTransport()
        transport.error = TimeoutError('timed out')
        checker = KtdPathChecker(transport, deadline_sec=0.2)
        for _ in range(3):
            self.assertEqual(checker(BINDING, 'point_orbit',
                                     {'longitude': 113.0, 'latitude': 23.0, 'radius_m': 30},
                                     POSE).status, 'timeout')
        # 无界 provider 与迟到结论都不允许用后台线程换取表面超时。
        calls = []
        self.assertEqual(evaluate_path(lambda *args: calls.append(args), BINDING, 'move_forward',
                                       {'distance_m': 10}, POSE).check.status, 'unavailable')
        ticks = iter([0.0, 5.0])
        self.assertEqual(evaluate_path(StaticFenceProvider(PathCheck('clear'), deadline_sec=0.1),
                                       BINDING, 'move_forward', {'distance_m': 10}, POSE,
                                       clock=lambda: next(ticks),
                                       allow_cooperative=True).check.status, 'timeout')
        self.assertEqual(calls, [])
        self.assertEqual(set(threading.enumerate()), before)
        self.assertEqual(len(multiprocessing.active_children()), children_before)

    def test_no_socket_is_bound_or_listened_on(self):
        import socket
        bound, listened = [], []
        original_bind, original_listen = socket.socket.bind, socket.socket.listen

        def blocked_bind(self, address):
            bound.append(address)
            raise AssertionError(f'fence check must not bind {address!r}')

        def blocked_listen(self, *args):
            listened.append(args)
            raise AssertionError('fence check must not listen')

        socket.socket.bind, socket.socket.listen = blocked_bind, blocked_listen
        try:
            transport = FenceTransport()
            checker = KtdPathChecker(transport)
            for action, parameters in (('move_forward', {'distance_m': 10}),
                                       ('point_orbit', {'longitude': 113.0, 'latitude': 23.0,
                                                        'radius_m': 30})):
                self.assertEqual(checker(BINDING, action, parameters, POSE).status, 'clear')
            transport.error = TimeoutError('timed out')
            self.assertEqual(checker(BINDING, 'move_forward', {'distance_m': 10}, POSE).status, 'timeout')
        finally:
            socket.socket.bind, socket.socket.listen = original_bind, original_listen
        self.assertEqual((bound, listened), ([], []))

    def test_path_modules_do_not_spawn_background_work(self):
        import pathlib
        import lingxi_flight_control.flight_policy as flight_policy
        import lingxi_flight_control.ktd_path as ktd_path
        import lingxi_flight_control.path_geometry as path_geometry
        for module in (flight_policy, ktd_path, path_geometry):
            source = pathlib.Path(module.__file__).read_text(encoding='utf-8')
            for forbidden in ('threading', 'multiprocessing', 'subprocess', 'ThreadPoolExecutor',
                              'asyncio', 'fork('):
                self.assertNotIn(forbidden, source, f'{module.__name__} must stay synchronous')


class AuditTests(unittest.TestCase):
    """审计只记录状态与计数，不记录坐标、半径或响应原文。"""

    def capture(self, checker, action, parameters):
        import contextlib
        import io
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            checker(BINDING, action, parameters, POSE)
        records = [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]
        return [record for record in records if record.get('event') == 'flight_path_check']

    def test_blocked_timeout_and_unavailable_are_audited_without_coordinates(self):
        transport = FenceTransport()
        transport.status = 'ng'
        blocked = self.capture(KtdPathChecker(transport), 'point_orbit',
                               {'longitude': 113.123456, 'latitude': 23.654321, 'radius_m': 30})
        self.assertEqual(blocked[-1]['status'], 'blocked')
        self.assertEqual(blocked[-1]['segments'], ORBIT_SEGMENTS + 1)
        self.assertIsNotNone(blocked[-1]['segment_index'])
        transport.error = TimeoutError('timed out')
        timeout = self.capture(KtdPathChecker(transport), 'move_forward', {'distance_m': 10})
        self.assertEqual(timeout[-1]['status'], 'timeout')
        unavailable = self.capture(KtdPathChecker(FenceTransport()),
                                   'point_flight',
                                   {'longitude': 113.0, 'latitude': 23.0, 'altitude_m': 120.0})
        self.assertEqual(unavailable[-1]['status'], 'unavailable')
        self.assertEqual(unavailable[-1]['reason'], 'PATH_ALTITUDE_DATUM_MISMATCH')
        for record in blocked + timeout + unavailable:
            self.assertNotIn('113.123456', json.dumps(record))
            self.assertNotIn('radius_m', record)
            self.assertNotIn('longitude', record)


if __name__ == '__main__':
    unittest.main()
