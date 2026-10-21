import unittest
from lingxi_flight_control import RuntimeBinding
from lingxi_flight_control.ktd_path import KtdPathChecker
from lingxi_flight_control.ktd_platform import KtdHttpResponse


class KtdPathTests(unittest.TestCase):
    def test_existing_platform_contract_checked_without_using_suggested_reroute(self):
        binding = RuntimeBinding('actor', 'project', 'hangar', 'drone', 'batch', 1)
        state = {'longitude': 113, 'latitude': 23, 'height_m': 50, 'yaw_deg': 90}
        class Transport:
            status = 'ok'
            actor = 'actor'
            def check_path(self, binding, points):
                self.points = points
                return KtdHttpResponse(200, {'code': 200, 'data': {'projectId': 'project',
                    'operatorId': self.actor, 'snapshot': points, 'invalid': [],
                    'segment': [{'status': self.status, 'converge': 'ln_outside'}],
                    'suggest': [{'longitude': 0, 'latitude': 0}]}})
        transport = Transport()
        checker = KtdPathChecker(transport)
        self.assertEqual(checker(binding, 'move_forward', {'distance_m': 10}, state).status, 'clear')
        self.assertGreater(transport.points[1]['longitude'], 113)
        self.assertEqual(transport.points[1]['altitude'], 50)
        transport.status = 'ng'
        self.assertEqual(checker(binding, 'move_forward', {'distance_m': 10}, state).status, 'blocked')
        transport.actor = 'other'
        self.assertEqual(checker(binding, 'move_forward', {'distance_m': 10}, state).status, 'unavailable')
        self.assertEqual(checker(binding, 'point_orbit', {}, state).status, 'unavailable')

    def test_unknown_altitude_datum_does_not_become_relative_height(self):
        binding = RuntimeBinding('actor', 'project', 'hangar', 'drone', 'batch', 1)
        checker = KtdPathChecker(None)
        self.assertEqual(checker(binding, 'move_forward', {'distance_m': 10},
            {'longitude': 113, 'latitude': 23, 'altitude_m': 50, 'yaw_deg': 90}).status, 'unavailable')

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
