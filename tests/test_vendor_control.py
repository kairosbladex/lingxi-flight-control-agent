"""Vendor attempts use real snapshot/HTTP/executor/journal, only HTTP/time injected."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from lingxi_flight_control.full_control import ACTION_SPECS, FlightControlActionRequest, RuntimeBinding
from lingxi_flight_control.ktd_platform import KtdHttpTransport, KtdPlatformError, KtdPlatformExecutor, SqliteCommandJournal
from lingxi_flight_control.ktd_snapshot import KtdSnapshotProvider
from lingxi_flight_control.vendor_control import VendorControlRequest
from test_ktd_snapshot import snapshot_payload


class VendorHttpBoundary:
    def __init__(self, clock):
        self.clock = clock
        self.snapshot = snapshot_payload()
        self.requests = []
        self.reports = {}
        self.raw_receipt = None
        self.failure = None
        self.after_post = None

    def open(self, request, *, timeout):
        self.requests.append(request)
        parts = urlsplit(request.full_url)
        if parts.path.endswith('/snapshot'):
            payload = self.snapshot
        elif parts.path.endswith('/vendor-request') and request.get_method() == 'POST':
            if self.failure is not None:
                raise self.failure
            body = json.loads(request.data)
            now = self.clock()
            report = {'schema_version': 'lingxi-vendor-control-result/v1', 'kind': 'vendor_service_result',
                'command_id': body['commandId'], 'idempotency_key': body['idempotencyKey'],
                'method': body['method'], 'trace_id': body['traceId'], 'actor_employee_id': 'actor-1',
                'project_id': 'project-1', 'site_id': body['siteId'], 'hangar_id': body['hangarId'],
                'drone_id': body['droneId'], 'task_id': body.get('taskId'), 'status': 'submitted',
                'result_code': None, 'version': 1, 'submitted_at': now, 'updated_at': now,
                'expires_at': now + 120, 'reported_at': None, 'received_at': None, 'error_code': None,
                'vendor_authority': 'unknown', 'drc_link': 'unknown', 'device_effect': 'not_verified',
                'dispatch_ids': {'bid': '11111111-1111-4111-8111-111111111111',
                                 'tid': '22222222-2222-4222-8222-222222222222'}}
            self.reports[body['commandId']] = report
            payload = {'code': 200, 'success': True, 'data': report}
            if self.after_post is not None:
                # Freeze the POST's earlier body while another executor may finish a GET.
                payload = deepcopy(payload)
                self.after_post(body)
        elif parts.path.endswith('/vendor-receipt') and request.get_method() == 'GET':
            query = parse_qs(parts.query)
            payload = {'code': 200, 'success': True, 'data': self.reports[query['commandId'][0]]}
        else:
            raise AssertionError('unexpected HTTP request')
        response = BytesIO(self.raw_receipt if parts.path.endswith('/vendor-receipt')
                           and self.raw_receipt is not None else json.dumps(payload).encode())
        response.code = 200
        return response


class VendorExecutorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 1000.0
        self.binding = RuntimeBinding('actor-1', 'project-1', 'hangar-1', 'drone-1', 'batch-1', 7)
        self.http = VendorHttpBoundary(lambda: self.now)
        self.transport = KtdHttpTransport('https://platform.test/api', token='synthetic-token',
                                         actor_id='actor-1', opener=self.http)
        self.provider = KtdSnapshotProvider(self.binding, site_id='site-1', task_id='java-task-1',
                                            transport=self.transport, clock=lambda: self.now)
        self.journal_path = Path(self.directory.name) / 'commands.sqlite3'

    def executor(self, **options):
        return KtdPlatformExecutor(self.binding, site_id='site-1', task_id='java-task-1',
            transport=self.transport, snapshot_provider=self.provider, journal=SqliteCommandJournal(self.journal_path),
            clock=lambda: self.now, **options)

    def active(self, **options):
        return self.executor(enable_vendor_requests=True,
            allowed_vendor_methods=('flight_authority_grab', 'drc_mode_enter', 'drc_mode_exit'), **options)

    def card(self, executor, *, command='vendor-one', key='key-one', method='drc_mode_enter'):
        return executor.preview_vendor_request(VendorControlRequest(command, key, method, 7))

    def confirm(self, executor, card):
        values = card.to_dict()
        return executor.confirm(**{key: values[key] for key in (
            'confirmation_id', 'command_id', 'idempotency_key', 'confirmation_binding', 'context_version')})

    def query(self, executor, *, command='vendor-one', key='key-one'):
        return executor.query_vendor_receipt(command, idempotency_key=key, context_version=7)

    def submitted(self):
        executor = self.active()
        card = self.card(executor)
        self.assertEqual(self.confirm(executor, card).status, 'submitted')
        return executor, card

    def writes(self):
        return [request for request in self.http.requests if request.get_method() == 'POST']

    def test_default_vendor_method_property_is_closed_and_never_fetches(self):
        executor = self.executor(enable_vendor_requests=False, allowed_vendor_methods=())
        self.assertEqual(executor.vendor_request_methods, ())
        self.assertEqual(len(ACTION_SPECS), 66)
        self.assertEqual(self.http.requests, [])

    def test_vendor_preview_confirm_and_query_use_separate_fixed_contract(self):
        executor = self.executor(enable_vendor_requests=True,
                                 allowed_vendor_methods=('drc_mode_enter',))
        request = VendorControlRequest('vendor-one', 'key-one', 'drc_mode_enter', 7)
        card = executor.preview_vendor_request(request).to_dict()
        self.assertEqual(card['status'], 'preview_required', card)
        self.assertEqual(card['kind'], 'vendor_control_request')
        self.assertTrue(card['receipt_tracking'])
        self.assertEqual(card['expires_at'], '1970-01-01T00:16:44+00:00')
        self.assertEqual([r.get_method() for r in self.http.requests], ['GET'])
        self.now = 1001
        confirmed = executor.confirm(**{key: card[key] for key in (
            'confirmation_id', 'command_id', 'idempotency_key', 'confirmation_binding', 'context_version')})
        self.assertEqual(confirmed.status, 'submitted', confirmed.to_dict())
        self.assertFalse(confirmed.device_effect_verified)
        writes = [r for r in self.http.requests if r.get_method() == 'POST']
        self.assertEqual(len(writes), 1)
        self.assertEqual(urlsplit(writes[0].full_url).path, '/api/screen/uav/operation/control/vendor-request')
        self.assertEqual(json.loads(writes[0].data), {
            'schemaVersion': 'lingxi-vendor-control-request/v1', 'commandId': 'vendor-one',
            'idempotencyKey': 'key-one', 'traceId': card['trace_id'], 'method': 'drc_mode_enter',
            'siteId': 'site-1', 'hangarId': 'hangar-1', 'droneId': 'drone-1', 'taskId': 'java-task-1',
            'snapshotVersion': 'a' * 64, 'expiresAt': 1004})
        report = executor.query_vendor_receipt('vendor-one', idempotency_key='key-one', context_version=7)
        self.assertEqual(report['status'], 'submitted')
        self.assertEqual(report['context_version'], 7)
        self.assertEqual(parse_qs(urlsplit(self.http.requests[-1].full_url).query), {
            'commandId': ['vendor-one'], 'method': ['drc_mode_enter'], 'siteId': ['site-1'],
            'hangarId': ['hangar-1'], 'droneId': ['drone-1'], 'taskId': ['java-task-1']})

    def test_higher_version_cannot_rewrite_terminal_result_or_regress_update_time(self):
        executor, card = self.submitted()
        report = self.http.reports[card.command_id]
        self.now = 1003
        report.update(status='failed', result_code=-1, error_code='DEVICE_RESULT_-1',
                      reported_at=1001, received_at=1002, updated_at=1002, version=2)
        self.assertEqual(self.query(executor)['result_code'], -1)
        baseline = deepcopy(report)
        for change in ({'result_code': 1, 'error_code': 'DEVICE_RESULT_1'},
                       {'reported_at': 1001.5}, {'received_at': 1002.5, 'updated_at': 1002.5},
                       {'updated_at': 1001.5}):
            with self.subTest(change=change):
                report.clear()
                report.update(baseline, version=3, **change)
                with self.assertRaises(KtdPlatformError):
                    self.query(executor)
        report.clear()
        report.update(baseline, version=3, updated_at=1003)
        self.assertEqual(self.query(executor)['version'], 3)
        repeated = self.confirm(executor, card)
        self.assertEqual(repeated.status, 'submitted')
        self.assertEqual(repeated.vendor_report['result_code'], -1)

    def test_switch_method_schema_and_context_reject_before_http(self):
        request = VendorControlRequest('vendor-one', 'key-one', 'drc_mode_enter', 7)
        self.assertEqual(self.executor().preview_vendor_request(request).error_code,
                         'VENDOR_REQUESTS_DISABLED')
        self.assertEqual(self.executor(enable_vendor_requests=True).preview_vendor_request(request).error_code,
                         'VENDOR_METHOD_NOT_ALLOWED')
        for method, version in (('control_acquire', 7), ('drc_mode_enter ', 7), (True, 7),
                                ('drc_mode_enter', True), ('drc_mode_enter', -1), ('drc_mode_enter', 8)):
            with self.subTest(method=method, version=version):
                result = self.active().preview_vendor_request(VendorControlRequest('v', 'k', method, version))
                self.assertEqual(result.status, 'rejected')
        self.assertEqual(self.http.requests, [])

    def test_fresh_snapshot_permissions_owner_profile_and_lease_are_independent_gates(self):
        original = deepcopy(self.http.snapshot)
        cases = (
            {'permissions': []},
            {'action_availability': {**original['data']['action_availability'],
                'read_state': {'available': False, 'reasons': ['PERMISSION_DENIED']}}},
            {'control_owner_employee_id': 'other'}, {'control_owner_employee_id': None},
            {'control_lease_expires_at': None}, {'control_lease_expires_at': 1000},
            {'profile': 'PZ_HANGAR'}, {'profile': None},
            {'state': {**original['data']['state'], 'online': False}},
            {'snapshot_version': 1}, {'actor_employee_id': 'other'}, {'drone_id': 'other'},
        )
        for change in cases:
            with self.subTest(change=change):
                self.http.snapshot = deepcopy(original)
                self.http.snapshot['data'].update(change)
                self.assertEqual(self.card(self.active()).status, 'rejected')
        self.assertEqual(self.writes(), [])
        self.http.snapshot = original
        executor = self.active(enabled=True)
        self.assertEqual(executor.available_actions(), frozenset({'read_state'}))
        self.assertEqual(self.card(executor).status, 'preview_required')
        self.assertFalse(self.http.snapshot['data']['capabilities']['UAV_MOVE_DISTANCE'])

    def test_card_deadline_is_shortest_budget_and_expired_or_changed_state_never_posts(self):
        executor = self.active(ttl_sec=1)
        card = self.card(executor)
        self.assertEqual(card.expires_at, '1970-01-01T00:16:41+00:00')
        self.now = 1001
        self.assertEqual(self.confirm(executor, card).error_code, 'CONFIRMATION_EXPIRED')
        self.now = 1000
        for field, value in (('snapshot_version', 'b' * 64), ('control_owner_employee_id', 'other'),
                             ('control_lease_expires_at', 1001)):
            with self.subTest(field=field):
                executor = self.active()
                card = self.card(executor)
                old = self.http.snapshot['data'][field]
                self.http.snapshot['data'][field] = value
                self.assertEqual(self.confirm(executor, card).status, 'rejected')
                self.http.snapshot['data'][field] = old
        self.assertEqual(self.writes(), [])

    def test_durable_replay_and_get_work_after_restart_with_write_switch_off(self):
        executor, card = self.submitted()
        self.now = 1300
        self.http.snapshot['data']['control_owner_employee_id'] = 'other'
        self.http.reports[card.command_id].update(status='unknown', version=2,
            updated_at=1001, error_code='DISPATCH_OUTCOME_UNKNOWN')
        restarted = self.executor()
        self.assertEqual(self.confirm(restarted, card).status, 'submitted')
        count = len(self.http.requests)
        self.assertEqual(self.query(restarted)['status'], 'unknown')
        self.assertEqual(len(self.http.requests), count + 1)
        self.assertTrue(urlsplit(self.http.requests[-1].full_url).path.endswith('/vendor-receipt'))
        self.assertEqual(len(self.writes()), 1)

    def test_timeout_is_durable_unknown_and_duplicate_confirm_never_reposts(self):
        executor = self.active()
        card = self.card(executor)
        self.http.failure = TimeoutError('private fixture detail')
        result = self.confirm(executor, card)
        self.assertEqual(result.status, 'unknown')
        self.assertNotIn('private fixture detail', json.dumps(result.to_dict()))
        self.assertEqual(self.confirm(self.active(), card).to_dict(), result.to_dict())
        self.assertEqual(len(self.writes()), 1)

    def test_cancel_and_kind_or_method_key_conflicts_never_post(self):
        self.http.snapshot = snapshot_payload(writable=True)
        self.http.snapshot['data']['state']['height_m'] = 30
        # V1.4 2.2.3：微操是否放行取决于航线状态；合成夹具必须给出已知状态，
        # 不能用 null 冒充“没在自动航线”。'idle' 是本仓模拟器取值，不是平台承诺。
        self.http.snapshot['data']['state']['route_status'] = 'hover'
        executor = self.active(enabled=True)
        card = self.card(executor)
        self.assertEqual(self.card(executor, method='drc_mode_exit').error_code, 'IDEMPOTENCY_CONFLICT')
        movement = FlightControlActionRequest('movement', 'key-one', 'move_forward', {'distance_m': 1}, 7)
        self.assertEqual(executor.preview(movement).error_code, 'IDEMPOTENCY_CONFLICT')
        args = {key: card.to_dict()[key] for key in ('confirmation_id', 'command_id',
            'idempotency_key', 'confirmation_binding', 'context_version')}
        self.assertEqual(executor.cancel(**args).status, 'cancelled')
        self.assertEqual(self.confirm(executor, card).error_code, 'CONFIRMATION_NOT_FOUND')
        self.assertEqual(self.writes(), [])
        card = executor.preview(movement)
        self.assertEqual(self.card(executor).error_code, 'IDEMPOTENCY_CONFLICT')

    def test_concurrent_executors_share_one_submission_and_cross_kind_queries_fail(self):
        first, second = self.active(), self.active()
        card = self.card(first)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda exe: self.confirm(exe, card), (first, second)))
        self.assertEqual(len(self.writes()), 1)
        self.assertTrue(all(result.status in {'submitted', 'unknown', 'rejected'} for result in results))
        self.assertEqual(self.confirm(second, card).status, 'submitted')
        count = len(self.http.requests)
        with self.assertRaises(KtdPlatformError):
            second.query_receipt(card.command_id, idempotency_key=card.idempotency_key, context_version=7)
        with self.assertRaises(KtdPlatformError):
            self.query(second, command='other')
        self.assertEqual(len(self.http.requests), count)

    def test_query_winning_before_post_preserves_its_newer_private_fact(self):
        executor, query_executor = self.active(), self.executor()
        card = self.card(executor)
        def complete_before_post_returns(body):
            report = self.http.reports[body['commandId']]
            report.update(status='request_succeeded', result_code=0, version=2,
                reported_at=1000, received_at=1000, updated_at=1000)
            self.assertEqual(self.query(query_executor)['status'], 'request_succeeded')
        self.http.after_post = complete_before_post_returns
        self.assertEqual(self.confirm(executor, card).status, 'submitted')
        replay = self.confirm(query_executor, card)
        self.assertEqual(replay.status, 'submitted')
        self.assertEqual(replay.vendor_report['status'], 'request_succeeded')
        self.assertEqual(replay.vendor_report['version'], 2)
        self.assertEqual(len(self.writes()), 1)

    def test_strict_receipt_schema_binding_and_integer_contract_rejects_forged_facts(self):
        executor, card = self.submitted()
        original = deepcopy(self.http.reports[card.command_id])
        changes = [
            {'schema_version': 'other/v1'}, {'kind': 'flight_control'}, {'method': 'drc_mode_exit'},
            {'command_id': 'other'}, {'idempotency_key': 'other'}, {'trace_id': 'other'},
            {'actor_employee_id': 'other'}, {'project_id': 'other'}, {'site_id': 'other'},
            {'hangar_id': 'other'}, {'drone_id': 'other'}, {'task_id': None},
            {'status': 'accepted'}, {'version': True}, {'version': 1.0},
            {'vendor_authority': 'owned'}, {'drc_link': 'connected'}, {'device_effect': 'verified'},
            {'expires_at': 1004}, {'submitted_at': True}, {'updated_at': 1001},
            {'dispatch_ids': {'bid': 'uuid', 'tid': 'uuid'}},
            {'dispatch_ids': {**original['dispatch_ids'], 'fly_to_id': 'private'}},
            {'action': 'move_forward'}, {'parameters': {}}, {'broker': 'private'},
        ]
        for change in changes:
            with self.subTest(change=change):
                self.http.reports[card.command_id] = {**original, **change}
                with self.assertRaises(KtdPlatformError):
                    self.query(executor)
        for field in original:
            with self.subTest(missing=field):
                self.http.reports[card.command_id] = {key: value for key, value in original.items() if key != field}
                with self.assertRaises(KtdPlatformError):
                    self.query(executor)
        terminal = {**original, 'status': 'failed', 'version': 2, 'updated_at': 1000,
                    'reported_at': 1000, 'received_at': 1000}
        for result, error in ((True, 'DEVICE_RESULT_True'), (1.0, 'DEVICE_RESULT_1'),
                              ('-1', 'DEVICE_RESULT_-1'), (0, 'DEVICE_RESULT_0'),
                              (-2147483649, 'DEVICE_RESULT_-2147483649'),
                              (2147483648, 'DEVICE_RESULT_2147483648'), (-1, 'DEVICE_RESULT_1')):
            with self.subTest(result=result):
                self.http.reports[card.command_id] = {**terminal, 'result_code': result, 'error_code': error}
                with self.assertRaises(KtdPlatformError):
                    self.query(executor)
        self.assertEqual(len(self.writes()), 1)

    def test_receipt_json_rejects_duplicate_members_nonfinite_and_non_utf8(self):
        executor, card = self.submitted()
        payload = {'code': 200, 'success': True, 'data': self.http.reports[card.command_id]}
        body = json.dumps(payload)
        for raw in (body.replace('"version": 1', '"version": 1, "version": 2').encode(),
                    body.replace('"version": 1', '"version": NaN').encode(),
                    body.encode('utf-16')):
            with self.subTest(raw=raw[:20]):
                self.http.raw_receipt = raw
                with self.assertRaises(KtdPlatformError):
                    self.query(executor)

    def test_unknown_never_expires_but_waiting_projects_expired_at_the_same_version(self):
        executor, card = self.submitted()
        report = self.http.reports[card.command_id]
        self.now = 1120
        report['status'] = 'expired'
        self.assertEqual(self.query(executor)['status'], 'expired')
        executor = self.active()
        self.now = 1000
        unknown_card = self.card(executor, command='vendor-two', key='key-two')
        self.assertEqual(self.confirm(executor, unknown_card).status, 'submitted')
        unknown = self.http.reports[unknown_card.command_id]
        unknown.update(status='unknown', error_code='AUTHORIZATION_CHANGED_BEFORE_PUBLISH', version=2)
        self.now = 1120
        self.assertEqual(self.query(executor, command='vendor-two', key='key-two')['status'], 'unknown')
        unknown.update(status='expired', error_code=None, version=3)
        with self.assertRaises(KtdPlatformError):
            self.query(executor, command='vendor-two', key='key-two')

    def test_signed_java_results_conflict_and_terminal_deadline_are_preserved(self):
        executor, card = self.submitted()
        report = self.http.reports[card.command_id]
        self.now = 1002
        report.update(status='failed', result_code=-2147483648, version=2,
            error_code='DEVICE_RESULT_-2147483648', reported_at=1001, received_at=1001, updated_at=1001)
        self.assertEqual(self.query(executor)['result_code'], -2147483648)
        report.update(status='conflict', result_code=None, version=3,
            error_code='DEVICE_TERMINAL_CONFLICT', reported_at=1002, received_at=1002, updated_at=1002)
        self.assertEqual(self.query(executor)['status'], 'conflict')
        report.update(status='request_succeeded', result_code=0, version=4, error_code=None)
        with self.assertRaises(KtdPlatformError):
            self.query(executor)
        report.update(status='conflict', result_code=None, version=4, error_code='DEVICE_TERMINAL_CONFLICT')
        self.now = 1200
        self.assertEqual(self.query(executor)['status'], 'conflict')
        self.assertEqual(self.confirm(executor, card).status, 'submitted')

    def test_conflict_needs_report_times_and_expiry_cannot_invent_device_evidence(self):
        executor, card = self.submitted()
        original = deepcopy(self.http.reports[card.command_id])
        self.http.reports[card.command_id] = {**original, 'status': 'conflict', 'version': 2,
            'error_code': 'DEVICE_TERMINAL_CONFLICT'}
        with self.assertRaises(KtdPlatformError):
            self.query(executor)
        self.now = 1120
        for changes in ({'error_code': 'DEVICE_RESULT_1'}, {'reported_at': 1001, 'received_at': 1001,
                                                          'updated_at': 1001}):
            with self.subTest(changes=changes):
                self.http.reports[card.command_id] = {**original, 'status': 'expired', **changes}
                with self.assertRaises(KtdPlatformError):
                    self.query(executor)


if __name__ == '__main__':
    unittest.main()
