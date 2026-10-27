"""Authoritative snapshot contract through a fake HTTP opener only."""
from dataclasses import replace
from io import BytesIO
import json
from pathlib import Path
import tempfile
import traceback
import unittest
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from unittest import mock

from lingxi_flight_control.full_control import ACTION_SPECS, FlightControlActionRequest, RuntimeBinding
from lingxi_flight_control.ktd_platform import (
    KtdHttpTransport, KtdPlatformError, KtdPlatformExecutor, KtdSnapshot, SqliteCommandJournal,
)


def snapshot_payload(now=1000.0, *, writable=False, task_id='java-task-1'):
    """Synthetic v1 fixture; writable=True is not a claim about Java capabilities."""
    actions = {action.value for action in ACTION_SPECS}
    permitted = {'read_state', 'move_forward'} if writable else {'read_state'}
    return {'code': 200, 'success': True, 'data': {
        'schema_version': 'lingxi-platform-snapshot/v1', 'snapshot_version': 'a' * 64,
        'generated_at': now, 'expires_at': now + 4,
        'actor_employee_id': 'actor-1', 'project_id': 'project-1', 'site_id': 'site-1',
        'hangar_id': 'hangar-1', 'drone_id': 'drone-1', 'task_id': task_id,
        'control_owner_employee_id': 'actor-1', 'control_owner_updated_at': now - 30,
        'control_lease_expires_at': now + 270, 'control_owner_version': 'b' * 64,
        'profile': 'DJI_HANGAR', 'telemetry': {'sampled_at': now - 1,
            'received_at': now - .5, 'source': 'java-udp-cache', 'sequence': None, 'version': 'c' * 64},
        'state': {'online': True, 'airborne': True, 'longitude': 113.2, 'latitude': 23.1,
            'altitude_m': 50.0, 'height_m': None, 'yaw_deg': 90.0, 'battery_percent': 90.0,
            'flight_status': 'FLYING', 'route_status': None},
        'capabilities': {**{action: action in permitted for action in actions},
                         'UAV_MOVE_DISTANCE': writable},
        'permissions': sorted(permitted),
        'action_availability': {action: {'available': action in permitted,
            'reasons': [] if action in permitted else ['CAPABILITY_UNAVAILABLE']} for action in actions},
    }}


def raw_snapshot_payload(*, profile='DJI_HANGAR', writable=False):
    """Synthetic single-frame evidence; no vendor authority or device claim."""
    payload = snapshot_payload(writable=writable)
    data = payload['data']
    data['profile'] = profile
    data['telemetry'].update(source='java-raw-frame', frame={
        'schema_version': 'lingxi-telemetry-frame/v1', 'profile': profile,
        'payload_sha256': 'd' * 64, 'complete': True,
    })
    data['state']['flight_status'] = '3'
    if profile == 'PZ_HANGAR':
        data['state'].update(altitude_m=None, height_m=10)
    return payload


class FakeOpener:
    def __init__(self, payload=None):
        self.payload = snapshot_payload() if payload is None else payload
        self.requests = []
        self.status = 200
        self.failure = None
        self.raw = None

    def open(self, request, *, timeout):
        self.requests.append((request, timeout))
        if self.failure is not None:
            raise self.failure
        response = BytesIO(self.raw if self.raw is not None else json.dumps(self.payload).encode())
        response.code = self.status
        return response


class KtdSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.binding = RuntimeBinding('actor-1', 'project-1', 'hangar-1', 'drone-1', 'batch-1', 7)
        self.opener = FakeOpener()
        self.transport = KtdHttpTransport('https://platform.test/api', token='fixture-token',
                                         actor_id='actor-1', opener=self.opener)

    def provider(self, **changes):
        from lingxi_flight_control.ktd_snapshot import KtdSnapshotProvider
        values = dict(binding=self.binding, site_id='site-1', task_id='java-task-1',
                      transport=self.transport, clock=lambda: self.now)
        values.update(changes)
        return KtdSnapshotProvider(**values)

    def assert_rejected(self):
        with self.assertRaises(KtdPlatformError) as error:
            self.provider()()
        self.assertRegex(str(error.exception), r'^[A-Z][A-Z0-9_]+$')
        self.assertNotIn('fixture-private-detail', str(error.exception))
        self.assertNotIn('fixture-private-detail', ''.join(traceback.format_exception(error.exception)))

    def test_valid_readonly_snapshot_uses_server_context_and_actual_observation_time(self):
        snapshot = self.provider()()
        self.assertEqual(snapshot.context_version, 7)
        self.assertEqual(snapshot.version, 'a' * 64)
        self.assertEqual(snapshot.observed_at, 999.0)
        self.assertEqual(snapshot.permissions, frozenset({'read_state'}))
        self.assertIsNone(snapshot.state['route_status'])
        self.assertFalse(snapshot.action_availability['move_forward']['available'])
        self.assertEqual(snapshot.action_availability['move_forward']['reasons'], ['CAPABILITY_UNAVAILABLE'])
        request, timeout = self.opener.requests[0]
        self.assertEqual(request.get_method(), 'GET')
        self.assertIsNone(request.data)
        self.assertEqual(urlsplit(request.full_url).path, '/api/lingxi/agent/platform/snapshot')
        self.assertEqual(parse_qs(urlsplit(request.full_url).query), {
            'siteId': ['site-1'], 'hangarId': ['hangar-1'], 'droneId': ['drone-1'], 'taskId': ['java-task-1']})
        self.assertEqual(request.get_header('Authorization'), 'Bearer fixture-token')
        self.assertEqual(request.get_header('Projectid'), 'project-1')
        self.assertEqual(timeout, 5.0)
        self.assertEqual(len(self.opener.requests), 1)

    def test_optional_endurance_fact_is_preserved_for_battery_estimation(self):
        payload = snapshot_payload()
        payload['data']['state']['endurance_mileage'] = 12_000.0
        self.opener.payload = payload
        snapshot = self.provider()()
        self.assertEqual(snapshot.state['endurance_mileage'], 12_000.0)

    def test_java_task_and_route_source_fields_are_preserved(self):
        payload = snapshot_payload()
        payload['data']['state'].update(task_state='paused', route_status_source='task_state',
                                       route_status_reason=None)
        self.opener.payload = payload
        snapshot = self.provider()()
        self.assertEqual(snapshot.state['task_state'], 'paused')
        self.assertEqual(snapshot.state['route_status_source'], 'task_state')
        self.assertIsNone(snapshot.state['route_status_reason'])

    def test_raw_frame_profiles_preserve_times_state_and_write_denials(self):
        for profile in ('DJI_HANGAR', 'PZ_HANGAR'):
            with self.subTest(profile=profile):
                self.opener.payload = raw_snapshot_payload(profile=profile)
                snapshot = self.provider()()
                self.assertEqual(snapshot.profile, profile)
                self.assertEqual(snapshot.observed_at, 999.0)
                self.assertEqual(snapshot.permissions, frozenset({'read_state'}))
                self.assertEqual(snapshot.state, self.opener.payload['data']['state'])
                self.assertFalse(snapshot.capabilities['UAV_MOVE_DISTANCE'])

    def test_raw_frame_metadata_is_strict_and_cannot_disguise_legacy_cache(self):
        valid = raw_snapshot_payload()['data']['telemetry']['frame']
        cases = [None, {}, {**valid, 'schema_version': 'unknown'},
                 {**valid, 'profile': 'PZ_HANGAR'}, {**valid, 'profile': None},
                 {**valid, 'payload_sha256': 'x' * 64}, {**valid, 'payload_sha256': 1},
                 {**valid, 'complete': 'true'}, {**valid, 'complete': 1},
                 {**valid, 'raw_payload': 'fixture-private-detail'}]
        for metadata in cases:
            with self.subTest(metadata=metadata):
                self.opener.payload = raw_snapshot_payload()
                self.opener.payload['data']['telemetry']['frame'] = metadata
                self.assert_rejected()
        self.opener.payload = raw_snapshot_payload()
        del self.opener.payload['data']['telemetry']['frame']
        self.assert_rejected()
        self.opener.payload = raw_snapshot_payload()
        self.opener.payload['data']['telemetry']['source'] = 'java-udp-cache'
        self.assert_rejected()

    def test_complete_frame_requires_same_frame_pose_and_known_flight_state(self):
        for profile, field in (('DJI_HANGAR', 'longitude'), ('DJI_HANGAR', 'latitude'),
                               ('DJI_HANGAR', 'yaw_deg'), ('DJI_HANGAR', 'altitude_m'),
                               ('PZ_HANGAR', 'height_m'), ('DJI_HANGAR', 'flight_status'),
                               ('DJI_HANGAR', 'airborne'), ('DJI_HANGAR', 'online')):
            with self.subTest(profile=profile, field=field):
                self.opener.payload = raw_snapshot_payload(profile=profile)
                self.opener.payload['data']['state'][field] = None
                self.assert_rejected()
        self.opener.payload = raw_snapshot_payload()
        self.opener.payload['data']['state']['online'] = False
        self.assert_rejected()

    def test_partial_raw_frame_stays_partial_and_cannot_claim_write_permission(self):
        self.opener.payload = raw_snapshot_payload()
        data = self.opener.payload['data']
        data['telemetry']['frame']['complete'] = False
        data['state']['longitude'] = None
        snapshot = self.provider()()
        self.assertIsNone(snapshot.state['longitude'])
        self.assertEqual(snapshot.permissions, frozenset({'read_state'}))
        self.opener.payload = raw_snapshot_payload(writable=True)
        self.opener.payload['data']['telemetry']['frame']['complete'] = False
        self.assert_rejected()

    def test_nullable_task_is_omitted_from_query_and_still_checked(self):
        self.opener.payload = snapshot_payload(task_id=None)
        snapshot = self.provider(task_id=None)()
        self.assertIsNone(snapshot.task_id)
        self.assertNotIn('taskId', parse_qs(urlsplit(self.opener.requests[0][0].full_url).query))
        self.assert_rejected()

    def test_every_identity_and_resource_must_match_server_binding(self):
        for field in ('actor_employee_id', 'project_id', 'site_id', 'hangar_id', 'drone_id', 'task_id'):
            for value in ('other', None, 1, True):
                with self.subTest(field=field, value=value):
                    self.opener.payload = snapshot_payload()
                    self.opener.payload['data'][field] = value
                    self.assert_rejected()

    def test_http_status_and_envelope_code_are_strict(self):
        for status, code in ((302, 200), (401, 200), (500, 200), (200, True),
                             (200, '200'), (200, 200.0), (200, 403), (200, None)):
            with self.subTest(status=status, code=code):
                self.opener.status = status
                self.opener.payload = {'code': code, 'message': 'fixture-private-detail',
                                       'data': snapshot_payload()['data']}
                self.assert_rejected()

    def test_conflicting_json_members_or_explicit_failure_are_not_success(self):
        self.opener.raw = json.dumps(snapshot_payload()).replace('"code": 200', '"code": 403, "code": 200', 1).encode()
        self.assert_rejected()
        self.opener.raw = None
        for value in (False, 'true', 1):
            with self.subTest(success=value):
                self.opener.payload = snapshot_payload()
                self.opener.payload['success'] = value
                self.assert_rejected()

    def test_schema_and_fact_versions_are_not_coerced(self):
        for field in ('schema_version', 'snapshot_version', 'control_owner_version'):
            for value in (True, 7, {}, 'fixture-private-detail', 'x' * 64):
                with self.subTest(field=field, value=value):
                    self.opener.payload = snapshot_payload()
                    self.opener.payload['data'][field] = value
                    self.assert_rejected()
        for field, value in (('source', 'request-time'), ('version', 1), ('sequence', 1)):
            with self.subTest(telemetry_field=field):
                self.opener.payload = snapshot_payload()
                self.opener.payload['data']['telemetry'][field] = value
                self.assert_rejected()

    def test_unknown_state_and_missing_lease_remain_readonly(self):
        data = self.opener.payload['data']
        data.update(profile=None, control_owner_employee_id=None, control_owner_updated_at=None,
                    control_lease_expires_at=None, control_owner_version=None)
        data['state'] = {key: None for key in data['state']}
        snapshot = self.provider()()
        self.assertIsNone(snapshot.profile)
        self.assertIsNone(snapshot.control_lease_expires_at)
        self.assertTrue(all(value is None for value in snapshot.state.values()))
        self.assertEqual(snapshot.permissions, frozenset({'read_state'}))

    def test_unknown_future_stale_and_non_numeric_times_cannot_refresh_state(self):
        cases = [('generated_at', value) for value in (None, True, '1000', 1001, -1)]
        cases += [('expires_at', value) for value in (None, True, '1004', 1000, 2000)]
        cases += [('sampled_at', value) for value in (None, True, '999', 1001, 990)]
        cases += [('received_at', value) for value in (None, True, '999', 1001, 990)]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                self.opener.payload = snapshot_payload()
                target = self.opener.payload['data']['telemetry'] if field in {'sampled_at', 'received_at'} else self.opener.payload['data']
                target[field] = value
                self.assert_rejected()

    def test_observed_time_is_earlier_of_both_valid_source_times(self):
        self.opener.payload['data']['telemetry'].update(sampled_at=999.5, received_at=999)
        self.assertEqual(self.provider()().observed_at, 999)

    def test_lease_must_be_bounded_by_owner_update_time_and_known_version(self):
        for changes in ({'control_owner_updated_at': 1001}, {'control_lease_expires_at': 5000},
                        {'control_lease_expires_at': True}, {'control_owner_updated_at': None},
                        {'control_owner_version': None}, {'control_owner_employee_id': None}):
            with self.subTest(changes=changes):
                self.opener.payload = snapshot_payload()
                self.opener.payload['data'].update(changes)
                self.assert_rejected()

    def test_readonly_permissions_cannot_be_upgraded_by_capabilities_or_availability(self):
        self.opener.payload['data']['capabilities'].update(move_forward=True, UAV_MOVE_DISTANCE=True)
        snapshot = self.provider()()
        self.assertNotIn('move_forward', snapshot.permissions)
        self.opener.payload['data']['action_availability']['move_forward'] = {'available': True, 'reasons': []}
        self.assert_rejected()

    def test_malformed_permission_capability_and_availability_data_is_rejected(self):
        for field, value in (('permissions', ['read_state', 'read_state']), ('permissions', ['shell']),
                             ('permissions', [True]), ('capabilities', {'read_state': 1}),
                             ('action_availability', {})):
            with self.subTest(field=field, value=value):
                self.opener.payload = snapshot_payload()
                self.opener.payload['data'][field] = value
                self.assert_rejected()
        for patch in ({'available': 'true', 'reasons': []}, {'available': True, 'reasons': ['DENIED']},
                      {'available': False, 'reasons': []}, {'available': False, 'reasons': ['fixture-private-detail']}):
            with self.subTest(availability=patch):
                self.opener.payload = snapshot_payload()
                self.opener.payload['data']['action_availability']['move_forward'] = patch
                self.assert_rejected()

    def test_write_claim_conflicts_with_unknown_lease_owner_profile_or_online_state(self):
        for changes in ({'control_lease_expires_at': None}, {'control_owner_employee_id': 'other'},
                        {'profile': None}, {'state': {**snapshot_payload()['data']['state'], 'online': None}}):
            with self.subTest(changes=changes):
                self.opener.payload = snapshot_payload(writable=True)
                self.opener.payload['data'].update(changes)
                self.assert_rejected()

    def test_state_fields_reject_wrong_types_and_impossible_values(self):
        for field, value in (('online', 1), ('airborne', 'true'), ('longitude', 181),
                             ('latitude', -91), ('altitude_m', True), ('battery_percent', 101),
                             ('flight_status', {}), ('route_status', [])):
            with self.subTest(field=field, value=value):
                self.opener.payload = snapshot_payload()
                self.opener.payload['data']['state'][field] = value
                self.assert_rejected()

    def test_transport_errors_and_oversized_bodies_do_not_leak_or_retry(self):
        for failure in (TimeoutError('fixture-private-detail'), OSError('fixture-private-detail'),
                        HTTPError('https://platform.test', 403, 'fixture-private-detail', {}, BytesIO(b'fixture-private-detail'))):
            with self.subTest(failure=type(failure).__name__):
                self.opener.failure = failure
                before = len(self.opener.requests)
                self.assert_rejected()
                self.assertEqual(len(self.opener.requests), before + 1)
        self.opener.failure = None
        for body in (b'fixture-private-detail', b'[]', b'x' * 65537):
            with self.subTest(size=len(body)):
                self.opener.raw = body
                self.assert_rejected()

    def test_public_provider_and_http_transport_suppress_exception_context(self):
        class UnexpectedTransport:
            actor_id = 'actor-1'
            def read_snapshot(self, *args, **kwargs):
                raise RuntimeError('fixture-private-detail')
        self.opener.failure = RuntimeError('fixture-private-detail')
        calls = (
            self.provider(transport=UnexpectedTransport()),
            lambda: self.transport.read_snapshot(self.binding, site_id='site-1', task_id=None),
        )
        for call in calls:
            with self.subTest(boundary=type(call).__name__), self.assertRaises(KtdPlatformError) as raised:
                call()
            rendered = ''.join(traceback.format_exception(raised.exception))
            self.assertNotIn('fixture-private-detail', rendered)
            self.assertTrue(raised.exception.__suppress_context__)

    def test_transport_identity_mismatch_and_invalid_resources_fail_before_open(self):
        for changes in ({'binding': replace(self.binding, actor_id='other')},
                        {'site_id': 'bad\r\nheader'}, {'task_id': True}):
            with self.subTest(changes=changes), self.assertRaises((ValueError, KtdPlatformError)):
                self.provider(**changes)()
        self.assertEqual(self.opener.requests, [])

    def test_fixed_transport_has_no_arbitrary_path_argument_and_no_proxy_inheritance(self):
        with self.assertRaises(TypeError):
            self.transport.read_snapshot(self.binding, site_id='site-1', task_id=None, path='https://evil')
        with mock.patch('lingxi_flight_control.ktd_platform.build_opener') as builder:
            KtdHttpTransport('https://platform.test', token='fixture-token', actor_id='actor-1')
        handlers = builder.call_args.args
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsNone(handlers[1].redirect_request(None, None, 302, '', {}, 'https://evil'))
        self.assertEqual(self.opener.requests, [])

    def test_hash_versions_work_with_existing_executor_without_enabling_control(self):
        # This covers the pre-existing executor's int-only version gate as well.
        data = snapshot_payload()['data']
        snapshot = KtdSnapshot('actor-1', 'project-1', 'hangar-1', 'drone-1', 'site-1',
            'java-task-1', 7, 'a' * 64, 999, 'actor-1', 'DJI_HANGAR', frozenset({'read_state'}),
            data['capabilities'], data['state'], 1270)
        with tempfile.TemporaryDirectory() as directory:
            executor = KtdPlatformExecutor(self.binding, site_id='site-1', task_id='java-task-1',
                transport=self.transport, snapshot_provider=lambda: snapshot,
                journal=SqliteCommandJournal(directory + '/commands.db'), clock=lambda: self.now)
            self.assertTrue(executor.read_state()['available'])
            self.assertEqual(executor.available_actions(), frozenset({'read_state'}))
            self.assertEqual(executor.preview(FlightControlActionRequest('c', 'k', 'move_forward',
                {'distance_m': 10}, 7)).error_code, 'PLATFORM_DISABLED')
        self.assertEqual(self.opener.requests, [])

    def test_actual_java_serialized_synthetic_fixture_keeps_write_denials(self):
        fixture = Path(__file__).resolve().parents[1] / 'fixtures' / 'ktd-platform-snapshot-v1.json'
        self.opener.raw = fixture.read_bytes()
        self.binding = replace(self.binding, actor_id='employee-1')
        self.transport = KtdHttpTransport('https://platform.test/api', token='fixture-token',
                                         actor_id='employee-1', opener=self.opener)
        self.now = 1700000000.0
        provider = self.provider(task_id='task-1')
        snapshot = provider()
        self.assertEqual(snapshot.observed_at, 1699999999.0)
        self.assertEqual(snapshot.permissions, frozenset({'read_state'}))
        self.assertFalse(snapshot.capabilities['UAV_MOVE_DISTANCE'])
        self.assertFalse(snapshot.action_availability['route_resume']['available'])
        self.assertIn('VENDOR_AUTHORITY_UNAVAILABLE', snapshot.action_availability['route_resume']['reasons'])
        self.assertIsNone(snapshot.state['route_status'])
        with tempfile.TemporaryDirectory() as directory:
            executor = KtdPlatformExecutor(self.binding, site_id='site-1', task_id='task-1',
                transport=self.transport, snapshot_provider=provider, enabled=True,
                journal=SqliteCommandJournal(directory + '/commands.db'), clock=lambda: self.now)
            self.assertTrue(executor.read_state()['available'])
            self.assertEqual(executor.available_actions(), frozenset({'read_state'}))
            result = executor.preview(FlightControlActionRequest('c', 'k', 'route_resume', {}, 7))
            self.assertEqual(result.error_code, 'ACTION_NOT_PERMITTED')
        self.assertTrue(all(request.get_method() == 'GET' for request, _ in self.opener.requests))

    def test_actual_java_raw_frame_fixture_clears_only_the_field_freshness_denial(self):
        fixture = Path(__file__).resolve().parents[1] / 'fixtures' / 'ktd-platform-raw-snapshot-v1.json'
        self.opener.raw = fixture.read_bytes()
        self.binding = replace(self.binding, actor_id='employee-1')
        self.transport = KtdHttpTransport('https://platform.test/api', token='fixture-token',
                                         actor_id='employee-1', opener=self.opener)
        self.now = 1700000000.0
        provider = self.provider(task_id='task-1')
        snapshot = provider()
        self.assertEqual(snapshot.observed_at, 1699999999.0)
        self.assertEqual(snapshot.state['altitude_m'], 60.0)
        self.assertEqual(snapshot.state['height_m'], 50.0)
        self.assertEqual(snapshot.permissions, frozenset({'read_state'}))
        self.assertFalse(snapshot.capabilities['UAV_MOVE_DISTANCE'])
        reasons = snapshot.action_availability['route_resume']['reasons']
        self.assertNotIn('TELEMETRY_FIELD_FRESHNESS_UNAVAILABLE', reasons)
        self.assertIn('VENDOR_AUTHORITY_UNAVAILABLE', reasons)
        self.assertIn('CONTROL_LINK_FRESHNESS_UNAVAILABLE', reasons)
        with tempfile.TemporaryDirectory() as directory:
            executor = KtdPlatformExecutor(self.binding, site_id='site-1', task_id='task-1',
                transport=self.transport, snapshot_provider=provider, enabled=True,
                journal=SqliteCommandJournal(directory + '/commands.db'), clock=lambda: self.now)
            self.assertEqual(executor.available_actions(), frozenset({'read_state'}))
            self.assertEqual(executor.preview(FlightControlActionRequest('c', 'k', 'route_resume', {}, 7)).error_code,
                             'ACTION_NOT_PERMITTED')
        self.assertTrue(all(request.get_method() == 'GET' for request, _ in self.opener.requests))


    def test_java_contract_block_is_parsed_and_surfaced_as_a_typed_gap(self):
        self.opener.payload['data']['contract'] = {
            'schema_version': 'lingxi-platform-contract/v1',
            'unavailable': {'route_status': 'ROUTE_STATE_SOURCE_UNAVAILABLE',
                            'target_binding': 'STATIC_ANNOTATION_SOURCE_UNAVAILABLE'},
        }
        snapshot = self.provider()()
        self.assertEqual(dict(snapshot.platform_unavailable),
                         {'route_status': 'ROUTE_STATE_SOURCE_UNAVAILABLE',
                          'target_binding': 'STATIC_ANNOTATION_SOURCE_UNAVAILABLE'})

    def test_declared_capability_gap_wins_over_a_generic_incomplete_state(self):
        self.opener.payload['data']['contract'] = {
            'schema_version': 'lingxi-platform-contract/v1',
            'unavailable': {'route_status': 'ROUTE_STATE_SOURCE_UNAVAILABLE'},
        }
        self.opener.payload['data']['permissions'] = ['read_state', 'hover']
        self.opener.payload['data']['capabilities']['hover'] = True
        self.opener.payload['data']['action_availability']['hover'] = {'available': True, 'reasons': []}
        provider = self.provider()
        with tempfile.TemporaryDirectory() as directory:
            executor = KtdPlatformExecutor(self.binding, site_id='site-1', task_id='java-task-1',
                transport=self.transport, snapshot_provider=provider, enabled=True,
                journal=SqliteCommandJournal(directory + '/commands.db'), clock=lambda: self.now)
            receipt = executor.preview(FlightControlActionRequest('c', 'k', 'hover', {}, 7))
        # route_status 是 null，但平台已声明“拿不到”，应返回该能力的错误码而不是笼统的不完整。
        self.assertEqual(receipt.error_code, 'ROUTE_STATE_SOURCE_UNAVAILABLE')

    def test_unknown_route_status_blocks_micro_actions_before_any_compile(self):
        # V1.4 2.2.3：微操放行取决于航线状态。平台把 route_status 留空时不能
        # 默认成“没在自动航线”；已声明不可用时返回该能力自己的错误码。
        self.opener.payload = snapshot_payload(writable=True)
        self.opener.payload['data']['contract'] = {
            'schema_version': 'lingxi-platform-contract/v1',
            'unavailable': {'route_status': 'ROUTE_STATE_SOURCE_UNAVAILABLE'},
        }
        provider = self.provider()
        with tempfile.TemporaryDirectory() as directory:
            executor = KtdPlatformExecutor(self.binding, site_id='site-1', task_id='java-task-1',
                transport=self.transport, snapshot_provider=provider, enabled=True,
                journal=SqliteCommandJournal(directory + '/commands.db'), clock=lambda: self.now)
            receipt = executor.preview(
                FlightControlActionRequest('c', 'k', 'move_forward', {'distance_m': 5}, 7))
        self.assertEqual(receipt.error_code, 'ROUTE_STATE_SOURCE_UNAVAILABLE')
        self.assertIsNone(receipt.confirmation_id)
        # 拒绝发生在编译前：没有任何写请求被发出。
        self.assertTrue(all(request.get_method() == 'GET' for request, _ in self.opener.requests))

    def test_malformed_contract_block_rejects_the_snapshot(self):
        for block in (
            {'schema_version': 'unknown', 'unavailable': {}},
            {'schema_version': 'lingxi-platform-contract/v1', 'unavailable': {'': 'X'}},
            {'schema_version': 'lingxi-platform-contract/v1', 'unavailable': {'route_status': 'unavailable'}},
            {'schema_version': 'lingxi-platform-contract/v1', 'unavailable': {}, 'extra': 1},
        ):
            with self.subTest(block=block):
                self.opener.payload = snapshot_payload()
                self.opener.payload['data']['contract'] = block
                with self.assertRaises(KtdPlatformError) as error:
                    self.provider()()
                self.assertEqual(str(error.exception), 'INVALID_PLATFORM_CONTRACT')

    def test_newer_capability_name_does_not_invalidate_the_snapshot(self):
        self.opener.payload['data']['contract'] = {
            'schema_version': 'lingxi-platform-contract/v1',
            'unavailable': {'future_capability': 'FUTURE_CAPABILITY_UNAVAILABLE'},
        }
        snapshot = self.provider()()
        self.assertEqual(dict(snapshot.platform_unavailable),
                         {'future_capability': 'FUTURE_CAPABILITY_UNAVAILABLE'})

    def test_snapshot_without_contract_block_makes_no_unavailability_claim(self):
        snapshot = self.provider()()
        self.assertEqual(dict(snapshot.platform_unavailable), {})


if __name__ == '__main__':
    unittest.main()
