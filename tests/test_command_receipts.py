"""Device-report contracts use synthetic HTTP responses, never device connections."""
import unittest
import json
from pathlib import Path

from lingxi_flight_control.full_control import FlightControlActionRequest, RuntimeBinding
from lingxi_flight_control.ktd_platform import KtdHttpResponse, KtdPlatformError
from lingxi_flight_control.command_receipts import decode_command_receipt


def command_receipt_payload(status='submitted', **changes):
    data = {
        'schema_version': 'lingxi-command-receipt/v1', 'command_id': 'command-1',
        'trace_id': 'trace-1', 'idempotency_key': 'key-1',
        'actor_employee_id': 'actor-1', 'project_id': 'project-1', 'site_id': 'site-1',
        'hangar_id': 'hangar-1', 'drone_id': 'drone-1', 'task_id': None,
        'action': 'move_forward', 'parameters': {'distance_m': 10.0},
        'status': status, 'version': 1, 'submitted_at': 1000.0, 'updated_at': 1001.0,
        'expires_at': 1120.0, 'reported_at': None, 'received_at': None,
        'error_code': None, 'device_effect': 'not_verified',
        'dispatch_ids': {'bid': '10000000-0000-4000-8000-000000000001',
                         'tid': '20000000-0000-4000-8000-000000000001',
                         'fly_to_id': '30000000-0000-4000-8000-000000000001'},
    }
    if status in ('accepted', 'in_progress', 'completed', 'failed', 'cancelled'):
        data.update(reported_at=1000.5, received_at=1001.0)
    data.update(changes)
    return {'code': 200, 'success': True, 'data': data}


class CommandReceiptContractTests(unittest.TestCase):
    def setUp(self):
        self.binding = RuntimeBinding('actor-1', 'project-1', 'hangar-1', 'drone-1', 'batch-1', 7)
        self.request = FlightControlActionRequest('command-1', 'key-1', 'move_forward', {'distance_m': 10}, 7)
        self.now = 1002.0

    def decode(self, packet=None, *, status_code=200):
        return decode_command_receipt(KtdHttpResponse(status_code, packet or command_receipt_payload()),
            binding=self.binding, site_id='site-1', task_id=None, request=self.request,
            trace_id='trace-1', now=self.now)

    def rejected(self, packet):
        with self.assertRaises(KtdPlatformError) as raised:
            self.decode(packet)
        self.assertRegex(str(raised.exception), '^[A-Z][A-Z0-9_]+$')
        self.assertNotIn('private-upstream-detail', str(raised.exception))

    def test_all_report_states_remain_distinct_from_independent_physical_verification(self):
        for status in ('dispatching', 'submitted', 'accepted', 'in_progress', 'completed',
                       'failed', 'cancelled', 'expired', 'unknown', 'conflict'):
            with self.subTest(status=status):
                if status == 'expired':
                    self.now = 1121
                else:
                    self.now = 1002
                result = self.decode(command_receipt_payload(status))
                self.assertEqual(result['status'], status)
                self.assertEqual(result['device_effect'], 'not_verified')
                self.assertNotIn('dispatch_ids', result)
                self.assertNotIn('actor_employee_id', result)

    def test_every_identity_action_and_parameter_is_bound_to_the_actual_send_record(self):
        fields = ('command_id', 'trace_id', 'idempotency_key', 'actor_employee_id',
                  'project_id', 'site_id', 'hangar_id', 'drone_id', 'task_id', 'action')
        for field in fields:
            with self.subTest(field=field):
                self.rejected(command_receipt_payload(**{field: 'other'}))
        for parameters in ({'distance_m': 11}, {'distance_m': True}, {'distance_m': '10'},
                           {'distance_m': 10, 'execute': True}, {}):
            with self.subTest(parameters=parameters):
                self.rejected(command_receipt_payload(parameters=parameters))

    def test_completed_requires_an_in_window_device_report_and_receive_time(self):
        for field in ('reported_at', 'received_at'):
            for value in (None, True, '1001', 999, 1003, float('nan'), float('inf')):
                with self.subTest(field=field, value=value):
                    self.rejected(command_receipt_payload('completed', **{field: value}))
        self.rejected(command_receipt_payload('completed', reported_at=1001.1, received_at=1001))
        self.rejected(command_receipt_payload('completed', error_code='RESULT_CONFLICT'))
        self.now = 1200
        self.rejected(command_receipt_payload('completed', reported_at=1121, received_at=1122, updated_at=1122))
        # Historical completion remains valid when its actual report was in the window.
        self.assertEqual(self.decode(command_receipt_payload('completed'))['status'], 'completed')

    def test_schema_version_enum_and_time_types_cannot_be_coerced(self):
        cases = [('schema_version', 'other'), ('status', 'wayline_ok'), ('status', []),
                 ('version', True), ('version', '1'), ('version', -1), ('version', 2**53),
                 ('submitted_at', None), ('submitted_at', 1003), ('updated_at', 999),
                 ('submitted_at', 10**1000),
                 ('expires_at', 1000), ('expires_at', True), ('error_code', 'private-upstream-detail'),
                 ('device_effect', 'verified')]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                self.rejected(command_receipt_payload(**{field: value}))
        packet = command_receipt_payload()
        del packet['data']['task_id']
        self.rejected(packet)

    def test_http_and_business_failures_cannot_become_device_success(self):
        for status, code in ((302, 200), (500, 200), (200, True), (200, '200'), (200, 404)):
            with self.subTest(status=status, code=code), self.assertRaises(KtdPlatformError):
                packet = command_receipt_payload('completed')
                packet['code'] = code
                self.decode(packet, status_code=status)
        self.rejected({**command_receipt_payload('completed'), 'success': False})

    def test_dispatch_identifiers_are_not_synthesized_from_missing_or_malformed_fields(self):
        for value in (None, {}, {'bid': 'one'}, {'bid': True, 'tid': 'two', 'fly_to_id': 'three'}):
            with self.subTest(value=value):
                self.rejected(command_receipt_payload(dispatch_ids=value))

    def test_expiry_is_unknown_outcome_not_success_or_a_confirm_card_timeout(self):
        self.rejected(command_receipt_payload('expired'))
        self.now = 1121
        self.rejected(command_receipt_payload('in_progress'))
        expired = self.decode(command_receipt_payload('expired'))
        self.assertEqual(expired['expires_at'], 1120)
        self.assertEqual(expired['status'], 'expired')

    def test_java_converter_fixture_retains_nulls_and_decodes_device_report(self):
        fixture = Path(__file__).resolve().parents[1] / 'fixtures/ktd-command-receipt-v1.json'
        payload = json.loads(fixture.read_bytes())
        binding = RuntimeBinding('employee-1', 'project-1', 'hangar-1', 'drone-1', 'fixture-batch', 7)
        result = decode_command_receipt(KtdHttpResponse(200, payload), binding=binding,
            site_id='site-1', task_id='task-1', request=self.request,
            trace_id='trace-command-1', now=1700000001)
        self.assertEqual(result['status'], 'completed')
        self.assertIsNone(result['error_code'])
        self.assertEqual(result['device_effect'], 'not_verified')
        self.assertEqual(result['submitted_at'], 1700000000)
        self.assertEqual(result['expires_at'], 1700000120)
        self.assertNotIn('dispatch_ids', result)


if __name__ == '__main__':
    unittest.main()
