"""V1.4 设备回执：规范状态、HTTP 受理语义、unknown 禁止重试与绑定复核。"""
import json
from pathlib import Path
import tempfile
import unittest

from lingxi_flight_control.geometry import offset_target
from lingxi_flight_control.command_receipts import (
    RECEIPT_UNAVAILABLE, DeviceReceiptState, canonical_state, decode_command_receipt,
    device_completion_verified, is_terminal, retry_allowed, validate_receipt_binding,
)
from lingxi_flight_control.full_control import (
    FlightControlActionRequest, FlightControlStatus, RuntimeBinding,
)
from lingxi_flight_control.ktd_platform import (
    KtdHttpResponse, KtdPlatformError, KtdPlatformExecutor, KtdSnapshot, SqliteCommandJournal,
)

FIXTURE = Path(__file__).resolve().parent.parent / 'fixtures' / 'v14-receipt-states-v1.json'


def load_fixture():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def receipt_payload(status='submitted', **changes):
    data = {
        'schema_version': 'lingxi-command-receipt/v1', 'command_id': 'command-1',
        'trace_id': 'trace-1', 'idempotency_key': 'key-1',
        'actor_employee_id': 'actor-1', 'project_id': 'project-1', 'site_id': 'site-1',
        'hangar_id': 'hangar-1', 'drone_id': 'drone-1', 'task_id': None,
        'action': 'move_forward', 'parameters': {'distance_m': 10.0},
        'status': status, 'version': 1, 'submitted_at': 1000.0, 'updated_at': 1000.0,
        'expires_at': 1120.0, 'reported_at': None, 'received_at': None,
        'error_code': None, 'device_effect': 'not_verified',
        'dispatch_ids': {'bid': '10000000-0000-4000-8000-000000000001',
                         'tid': '20000000-0000-4000-8000-000000000001',
                         'fly_to_id': '30000000-0000-4000-8000-000000000001'},
    }
    if status in ('accepted', 'in_progress', 'completed', 'failed', 'cancelled'):
        data.update(reported_at=1000.0, received_at=1000.0)
    data.update(changes)
    return {'code': 200, 'success': True, 'data': data}


class CanonicalStateTests(unittest.TestCase):
    def test_every_wire_value_maps_to_a_documented_state(self):
        for item in load_fixture()['mapping']:
            with self.subTest(wire=item['wire']):
                state = canonical_state(item['wire'])
                self.assertIs(state, DeviceReceiptState(item['state']))
                self.assertEqual(is_terminal(state), item['terminal'])
                self.assertEqual(retry_allowed(state), item['retry_allowed'])

    def test_unknown_wire_values_are_rejected_not_guessed(self):
        for value in load_fixture()['invalid_wire_values']:
            with self.subTest(value=value):
                with self.assertRaises(KtdPlatformError):
                    canonical_state(value)

    def test_unknown_and_expired_never_allow_automatic_retry(self):
        for state in (DeviceReceiptState.UNKNOWN, DeviceReceiptState.EXPIRED,
                      DeviceReceiptState.CONFLICT, DeviceReceiptState.SUBMITTED,
                      DeviceReceiptState.ACCEPTED, DeviceReceiptState.IN_PROGRESS):
            with self.subTest(state=state):
                self.assertFalse(retry_allowed(state))

    def test_no_receipt_state_proves_device_completion(self):
        for state in DeviceReceiptState:
            with self.subTest(state=state):
                self.assertFalse(device_completion_verified(state))


class DecodeTests(unittest.TestCase):
    def setUp(self):
        self.binding = RuntimeBinding('actor-1', 'project-1', 'hangar-1', 'drone-1', 'batch-1', 7)
        self.request = FlightControlActionRequest('command-1', 'key-1', 'move_forward',
                                                  {'distance_m': 10}, 7)
        self.now = 1002.0

    def decode(self, packet=None):
        return decode_command_receipt(
            KtdHttpResponse(200, packet or receipt_payload()),
            binding=self.binding, site_id='site-1', task_id=None, request=self.request,
            trace_id='trace-1', now=self.now)

    def test_decoded_report_carries_canonical_state_and_not_verified_effect(self):
        report = self.decode()
        self.assertEqual(report['status'], 'submitted')
        self.assertEqual(report['state'], 'submitted')
        self.assertEqual(report['device_effect'], 'not_verified')
        self.assertFalse(device_completion_verified(canonical_state(report['status'])))

    def test_completed_wire_status_is_canonical_succeeded(self):
        report = self.decode(receipt_payload('completed'))
        self.assertEqual(report['state'], DeviceReceiptState.SUCCEEDED.value)
        self.assertTrue(is_terminal(DeviceReceiptState(report['state'])))

    def test_submitted_is_platform_acceptance_only(self):
        report = self.decode(receipt_payload('submitted'))
        state = canonical_state(report['status'])
        self.assertFalse(is_terminal(state))
        self.assertFalse(device_completion_verified(state))

    def test_failed_and_unknown_are_distinguishable(self):
        failed = self.decode(receipt_payload('failed', error_code='DEVICE_RESULT_1'))
        unknown = self.decode(receipt_payload('unknown'))
        self.assertEqual(failed['state'], 'failed')
        self.assertEqual(unknown['state'], 'unknown')
        self.assertTrue(retry_allowed(canonical_state(failed['status'])))
        self.assertFalse(retry_allowed(canonical_state(unknown['status'])))

    def test_binding_and_identity_are_still_enforced(self):
        for change, code in (
            ({'drone_id': 'drone-2'}, 'COMMAND_RECEIPT_BINDING_MISMATCH'),
            ({'task_id': 'other-task'}, 'COMMAND_RECEIPT_BINDING_MISMATCH'),
            ({'idempotency_key': 'key-2'}, 'COMMAND_RECEIPT_BINDING_MISMATCH'),
            ({'trace_id': 'trace-2'}, 'COMMAND_RECEIPT_BINDING_MISMATCH'),
            ({'device_effect': 'verified'}, 'INVALID_COMMAND_RECEIPT'),
            ({'status': 'succeeded'}, 'INVALID_COMMAND_RECEIPT'),
        ):
            with self.subTest(change=change):
                with self.assertRaises(KtdPlatformError) as caught:
                    self.decode(receipt_payload(**change))
                self.assertEqual(str(caught.exception), code)


class FakeTransport:
    def __init__(self, receipt=None):
        self.calls = []
        self.receipt = receipt or receipt_payload('submitted')
        self.fail = None

    def submit(self, command, context):
        self.calls.append(('submit', command.path))
        # track_commands=True 时 Java 在受理响应里返回同一份回执 v1；tracking 里的
        # trace_id 必须原样回显，否则解码会拒绝（这正是防止“换单”回执的绑定校验）。
        tracking = command.body.get('tracking') or {}
        payload = receipt_payload('submitted', trace_id=tracking.get('traceId'),
                                  idempotency_key=tracking.get('idempotencyKey'),
                                  command_id=tracking.get('commandId'))
        self.receipt = payload
        return KtdHttpResponse(200, payload)

    def read_receipt(self, binding, *, site_id, task_id, command_id):
        self.calls.append(('read_receipt', command_id))
        if self.fail is not None:
            raise self.fail
        return KtdHttpResponse(200, self.receipt)


class QueryReceiptTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.binding = RuntimeBinding('actor-1', 'project-1', 'hangar-1', 'drone-1', 'batch-1', 1)
        self.now = 1000.0
        self.transport = FakeTransport()
        self.snapshot = KtdSnapshot(
            actor_id='actor-1', project_id='project-1', hangar_id='hangar-1',
            drone_id='drone-1', site_id='site-1', task_id=None, context_version=1,
            version='a' * 64, observed_at=self.now, control_owner='actor-1',
            profile='DJI_HANGAR', control_lease_expires_at=self.now + 270,
            permissions=frozenset({'move_forward', 'read_state'}),
            capabilities={'move_forward': True, 'UAV_MOVE_DISTANCE': True},
            state={'online': True, 'airborne': True, 'longitude': 113.0, 'latitude': 23.0,
                   'altitude_m': 100.0, 'height_m': 50.0, 'yaw_deg': 90.0,
                   'flight_status': 'FLYING', 'route_status': 'idle'},
        )

    def executor(self, *, binding=None):
        return KtdPlatformExecutor(
            binding or self.binding, site_id='site-1', task_id=None, transport=self.transport,
            snapshot_provider=lambda: self.snapshot,
            journal=SqliteCommandJournal(self.directory.name + '/commands.sqlite3'),
            enabled=True, clock=lambda: self.now, track_commands=True)

    def dispatch(self, executor):
        request = FlightControlActionRequest('command-1', 'key-1', 'move_forward',
                                            {'distance_m': 10}, 1)
        card = executor.preview(request)
        self.assertEqual(card.status, FlightControlStatus.PREVIEW_REQUIRED, card.to_dict())
        return executor.confirm(card.confirmation_id, command_id='command-1',
                                idempotency_key='key-1',
                                confirmation_binding=card.confirmation_binding,
                                context_version=1)

    def test_receipt_query_returns_canonical_state_without_claiming_completion(self):
        executor = self.executor()
        receipt = self.dispatch(executor)
        self.assertEqual(receipt.status, 'submitted')
        report = executor.query_receipt('command-1', idempotency_key='key-1', context_version=1)
        self.assertEqual(report['status'], 'submitted')
        self.assertEqual(report['device_effect'], 'not_verified')
        self.assertEqual(report['command_id'], 'command-1')

    def test_receipt_channel_failure_is_typed_unavailable(self):
        executor = self.executor()
        self.dispatch(executor)
        self.transport.fail = TimeoutError('private transport detail')
        with self.assertRaises(KtdPlatformError) as caught:
            executor.query_receipt('command-1', idempotency_key='key-1', context_version=1)
        self.assertEqual(str(caught.exception), RECEIPT_UNAVAILABLE)
        self.assertNotIn('private transport detail', str(caught.exception))

    def test_receipt_query_after_rebinding_is_rejected(self):
        executor = self.executor()
        self.dispatch(executor)
        other = RuntimeBinding('actor-1', 'project-1', 'hangar-1', 'drone-2', 'batch-1', 1)
        rebound = self.executor(binding=other)
        with self.assertRaises(KtdPlatformError) as caught:
            rebound.query_receipt('command-1', idempotency_key='key-1', context_version=1)
        self.assertEqual(str(caught.exception), 'COMMAND_NOT_FOUND')

    def test_validate_receipt_binding_rejects_foreign_records(self):
        executor = self.executor()
        receipt = self.dispatch(executor)
        request = FlightControlActionRequest('command-1', 'key-1', 'move_forward',
                                             {'distance_m': 10}, 1)
        validate_receipt_binding(receipt, binding=self.binding, site_id='site-1',
                                 task_id=None, request=request, trace_id=receipt.trace_id)
        with self.assertRaises(KtdPlatformError):
            validate_receipt_binding(receipt, binding=self.binding, site_id='site-1',
                                     task_id=None,
                                     request=FlightControlActionRequest(
                                         'command-2', 'key-1', 'move_forward',
                                         {'distance_m': 10}, 1),
                                     trace_id=receipt.trace_id)


class ActionReceiptSemanticsTests(unittest.TestCase):
    """悬停/指点/环绕/急停没有逐条设备回执契约时必须显式不可用。"""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.binding = RuntimeBinding('actor-1', 'project-1', 'hangar-1', 'drone-1', 'batch-1', 1)
        self.now = 1000.0
        self.transport = FakeTransport()
        actions = ('hover', 'point_flight', 'point_orbit', 'emergency_stop')
        self.snapshot = KtdSnapshot(
            actor_id='actor-1', project_id='project-1', hangar_id='hangar-1',
            drone_id='drone-1', site_id='site-1', task_id='java-task-1', context_version=1,
            version='a' * 64, observed_at=self.now, control_owner='actor-1',
            profile='DJI_HANGAR', control_lease_expires_at=self.now + 270,
            permissions=frozenset({'read_state', *actions}),
            capabilities={action: True for action in actions},
            state={'online': True, 'airborne': True, 'longitude': 113.0, 'latitude': 23.0,
                   'altitude_m': 100.0, 'height_m': 50.0, 'yaw_deg': 90.0,
                   'flight_status': 'FLYING', 'route_status': 'paused',
                   'route_id': 'route-main-1', 'breakpoint_index': 3},
            action_availability={action: {'available': True, 'reasons': []}
                                 for action in ('read_state', *actions)},
        )

    def executor(self, *, track_commands=True):
        return KtdPlatformExecutor(
            self.binding, site_id='site-1', task_id='java-task-1', transport=self.transport,
            snapshot_provider=lambda: self.snapshot,
            journal=SqliteCommandJournal(self.directory.name + '/commands.sqlite3'),
            enabled=True, clock=lambda: self.now, track_commands=track_commands)

    CASES = {
        'hover': {},
        'point_flight': {'longitude': 113.001, 'latitude': 23.001, 'altitude_m': 100},
        # 飞机在 113,23；环绕中心取正南 30 米，使当前距离等于请求半径。
        'point_orbit': {'longitude': offset_target(113.0, 23.0, 180.0, 30.0)[0],
                        'latitude': offset_target(113.0, 23.0, 180.0, 30.0)[1],
                        'radius_m': 30},
        'emergency_stop': {},
    }

    def test_tracking_mode_rejects_actions_without_a_receipt_contract(self):
        executor = self.executor()
        for index, (action, parameters) in enumerate(self.CASES.items()):
            with self.subTest(action=action):
                card = executor.preview(FlightControlActionRequest(f'cmd-{index}', f'key-{index}',
                                                                   action, parameters, 1))
                self.assertEqual(card.status, FlightControlStatus.REJECTED, card.to_dict())
                self.assertEqual(card.error_code, 'TRACKING_UNSUPPORTED')
        self.assertEqual(self.transport.calls, [])

    def test_untracked_actions_never_claim_device_completion(self):
        executor = self.executor(track_commands=False)
        for index, (action, parameters) in enumerate(self.CASES.items()):
            with self.subTest(action=action):
                request = FlightControlActionRequest(f'cmd-{index}', f'key-{index}', action,
                                                     parameters, 1)
                card = executor.preview(request)
                self.assertEqual(card.status, FlightControlStatus.PREVIEW_REQUIRED, card.to_dict())
                self.assertFalse(card.receipt_tracking, action)
                self.assertFalse(card.device_effect_verified, action)
                receipt = executor.confirm(card.confirmation_id, command_id=request.command_id,
                                           idempotency_key=request.idempotency_key,
                                           confirmation_binding=card.confirmation_binding,
                                           context_version=1)
                self.assertEqual(receipt.status, 'submitted', receipt.to_dict())
                self.assertFalse(receipt.device_effect_verified, action)
                # 没有逐条设备回执契约：查询必须显式不可用，而不是编一个 accepted。
                with self.assertRaises(KtdPlatformError) as caught:
                    executor.query_receipt(request.command_id,
                                           idempotency_key=request.idempotency_key,
                                           context_version=1)
                self.assertEqual(str(caught.exception), 'RECEIPT_UNSUPPORTED')


if __name__ == '__main__':
    unittest.main()
