import unittest
from lingxi_flight_control import RuntimeBinding
from lingxi_flight_control.ktd_path import KtdPathChecker
from lingxi_flight_control.ktd_platform import KtdHttpResponse


BINDING = RuntimeBinding('actor', 'project', 'hangar', 'drone', 'batch', 1)
STATE = {'longitude': 113, 'latitude': 23, 'height_m': 50, 'yaw_deg': 90}


class Transport:
    """假传输：只验证协议与状态机，不代表真实围栏结论。

    必须接受单次预算：``KtdPathChecker`` 只调用能声明 socket 超时的传输层。
    """

    def __init__(self):
        self.status = 'ok'
        self.actor = 'actor'
        self.points = None
        self.suggest = [{'longitude': 0, 'latitude': 0}]
        self.timeouts = []

    def check_path(self, binding, points, *, timeout_sec=None):
        self.timeouts.append(timeout_sec)
        self.points = points
        return KtdHttpResponse(200, {'code': 200, 'data': {'projectId': 'project',
            'operatorId': self.actor, 'snapshot': points, 'invalid': [],
            'segment': [{'status': self.status, 'converge': 'ln_outside'} for _ in points[1:]],
            'suggest': self.suggest}})


class KtdPathTests(unittest.TestCase):
    def test_existing_platform_contract_checked_without_using_suggested_reroute(self):
        transport = Transport()
        checker = KtdPathChecker(transport)
        self.assertEqual(checker(BINDING, 'move_forward', {'distance_m': 10}, STATE).status, 'clear')
        self.assertGreater(transport.points[1]['longitude'], 113)
        self.assertEqual(transport.points[1]['altitude'], 50)
        transport.status = 'ng'
        self.assertEqual(checker(BINDING, 'move_forward', {'distance_m': 10}, STATE).status, 'blocked')
        transport.actor = 'other'
        self.assertEqual(checker(BINDING, 'move_forward', {'distance_m': 10}, STATE).status, 'unavailable')

    def test_orbit_requires_radius_instead_of_falling_back_to_a_chord(self):
        checker = KtdPathChecker(Transport())
        # 缺半径是几何输入非法：确定性拒绝，不退化成两点直线假装检查过曲线。
        self.assertEqual(checker(BINDING, 'point_orbit', {}, STATE).status, 'invalid')
        result = checker(BINDING, 'point_orbit', {'longitude': 113, 'latitude': 23, 'radius_m': 30}, STATE)
        self.assertEqual(result.status, 'clear')
        self.assertEqual(result.segments, 37)  # 36 段环绕 + 1 段进入
        self.assertEqual(result.vertical_field, 'height_m')

    def test_unknown_altitude_datum_does_not_become_relative_height(self):
        checker = KtdPathChecker(None)
        self.assertEqual(checker(BINDING, 'move_forward', {'distance_m': 10},
            {'longitude': 113, 'latitude': 23, 'altitude_m': 50, 'yaw_deg': 90}).status, 'unavailable')
        # 只有 altitude_m 时按该字段自己的基准校验，并如实记录用了哪个字段。
        result = KtdPathChecker(Transport())(BINDING, 'move_forward', {'distance_m': 10},
            {'longitude': 113, 'latitude': 23, 'altitude_m': 50, 'yaw_deg': 90})
        self.assertEqual(result.vertical_field, 'altitude_m')

    def test_deadline_is_forwarded_to_the_transport(self):
        transport = Transport()
        checker = KtdPathChecker(transport, deadline_sec=1.5)
        self.assertEqual(checker(BINDING, 'move_forward', {'distance_m': 10}, STATE).status, 'clear')
        self.assertEqual(transport.timeouts, [1.5])

    def test_transport_without_a_single_call_budget_is_not_called(self):
        class Unbounded:
            def __init__(self):
                self.calls = 0

            def check_path(self, binding, points):
                self.calls += 1
                raise AssertionError('unbounded transport must not be called')

        transport = Unbounded()
        result = KtdPathChecker(transport)(BINDING, 'move_forward', {'distance_m': 10}, STATE)
        self.assertEqual(result.status, 'unavailable')
        self.assertEqual(result.reason, 'PATH_TRANSPORT_UNBOUNDED')
        self.assertEqual(transport.calls, 0)

    def test_policy_snapshot_extensions_are_typed_not_arbitrary_fields(self):
        from lingxi_flight_control.ktd_snapshot import KtdSnapshotProvider
        from lingxi_flight_control.ktd_platform import KtdPlatformError
        from test_ktd_snapshot import snapshot_payload
        state = snapshot_payload()['data']['state']
        state.update(task_type='mapping', mapping_invalidated=True,
            device_limits={'speaker_volume': {'min': 0, 'max': 100}})
        self.assertEqual(KtdSnapshotProvider._state(state)['task_type'], 'mapping')
        for patch in ({'mapping_invalidated': 'true'}, {'task_type': 'made_up'},
                      {'device_limits': {'speaker_volume': {'min': True, 'max': 100}}},
                      {'arbitrary_url': 'http://unused.invalid'}):
            with self.assertRaises(KtdPlatformError):
                KtdSnapshotProvider._state({**state, **patch})
