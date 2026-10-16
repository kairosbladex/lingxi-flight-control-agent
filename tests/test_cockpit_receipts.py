"""只读回执与续接：真实会话/执行器，外部 HTTP 和模型使用合成边界。"""
import json
from dataclasses import replace
from io import BytesIO
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

from lingxi_flight_control import DecisionKind, MemoryFlightControlSimulator, ModelDecision, RuntimeBinding, ToolCall
from lingxi_flight_control.cockpit import CockpitService
from lingxi_flight_control.ktd_platform import KtdHttpTransport, KtdPlatformExecutor, KtdSnapshot, SqliteCommandJournal
from lingxi_flight_control.session import MemoryCheckpointStore
from lingxi_flight_control.ollama_decider import OllamaDecider
from test_command_receipts import command_receipt_payload


class ReceiptJavaOpener:
    """仅 HTTP 边界：许可与回执均为合成数据，没有设备样本或真实网络。"""
    def __init__(self):
        self.calls, self.reports = [], {}
        self.failure = None
        self.post_failure = None
        self.entered, self.release = None, None

    def open(self, request, timeout):
        method, url = request.get_method(), urlsplit(request.full_url)
        body = json.loads(request.data) if request.data else None
        self.calls.append((method, url.path, body))
        if method == 'POST':
            assert url.path == '/api/screen/uav/operation/control'
            tracking = body.get('tracking')
            payload = {'code': 200, 'success': True}
            if tracking:
                payload = command_receipt_payload(command_id=tracking['commandId'],
                    idempotency_key=tracking['idempotencyKey'], trace_id=tracking['traceId'],
                    parameters={'distance_m': body['param']}, submitted_at=1000, updated_at=1000)
                self.reports[tracking['commandId']] = payload
            if self.post_failure:
                raise self.post_failure
        else:
            assert method == 'GET' and url.path == '/api/lingxi/agent/commands/receipt'
            if self.entered is not None:
                self.entered.set()
                if not self.release.wait(3):
                    raise TimeoutError('synthetic blocked read')
            if self.failure:
                raise self.failure
            command = parse_qs(url.query)['commandId'][0]
            payload = self.reports.get(command, {'code': 404, 'success': False})
        response = BytesIO(json.dumps(payload).encode())
        response.code = 200
        return response


class ReceiptBrain:
    def __init__(self):
        self.inputs, self.decisions = [], []

    def decide(self, text, checkpoint, tools):
        self.inputs.append(json.loads(text))
        return self.decisions.pop(0)


def move(action='move_forward', distance=10):
    return ModelDecision(DecisionKind.TOOL_CALL, tool_call=ToolCall(action, {'distance_m': distance}))


class CockpitReceiptRequestTests(unittest.TestCase):
    def setUp(self):
        self.binding = RuntimeBinding('actor-1', 'project-1', 'hangar-1', 'drone-1', 'batch-1', 1)
        self.simulator = MemoryFlightControlSimulator(self.binding)
        self.model_calls = []
        self.service = CockpitService(self.binding, self.simulator,
            lambda: self.model_calls.append(True), gateway_token='synthetic-gateway')
        self.headers = {'X-Agent-Gateway': 'synthetic-gateway', 'X-Agent-Actor': 'actor-1',
                        'X-Agent-Session': 'tab-1'}

    def query(self, query='command_id=command-1&context_version=1', body='', headers=None):
        return self.service.handle('GET', '/api/flight-control/agent/receipt?' + query,
                                   body, self.headers if headers is None else headers)

    def test_simulator_cannot_fabricate_a_platform_receipt(self):
        response = self.query()
        self.assertEqual(response.payload['error_code'], 'RECEIPT_UNSUPPORTED')
        self.assertEqual(self.model_calls, [])

    def test_receipt_get_requires_exactly_one_of_each_query_and_an_empty_body(self):
        for query in ('', 'command_id=command-1', 'command_id=&context_version=1',
                      'command_id=command-1&context_version=1&context_version=1',
                      'command_id=command-1&command_id=command-1&context_version=1',
                      'command_id=command-1&context_version=true',
                      'command_id=command-1&context_version=1.0',
                      'command_id=command-1&context_version=1&drone_id=other',
                      'command_id=../other&context_version=1'):
            with self.subTest(query=query):
                self.assertEqual(self.query(query).payload['error_code'], 'INVALID_REQUEST')
        for body in ('{}', ' ', '{"command_id":"other"}', b'{}'):
            with self.subTest(body=body):
                self.assertEqual(self.query(body=body).payload['error_code'], 'INVALID_REQUEST')
        self.assertEqual(self.model_calls, [])

    def test_gateway_actor_and_session_validation_precede_receipt_lookup(self):
        for headers, code in (({}, 'UNAUTHENTICATED'),
            ({**self.headers, 'X-Agent-Actor': 'other'}, 'FORBIDDEN'),
            ({**self.headers, 'X-Agent-Session': 'bad/session'}, 'INVALID_SESSION')):
            with self.subTest(code=code):
                self.assertEqual(self.query(headers=headers).payload['error_code'], code)
        self.assertEqual(self.model_calls, [])


class CockpitTrackedReceiptTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 1000.0
        self.binding = RuntimeBinding('actor-1', 'project-1', 'hangar-1', 'drone-1', 'batch-1', 1)
        self.snapshot = KtdSnapshot(actor_id='actor-1', project_id='project-1', hangar_id='hangar-1',
            drone_id='drone-1', site_id='site-1', task_id=None, context_version=1,
            version='a' * 64, observed_at=1000, control_owner='actor-1', profile='DJI_HANGAR',
            control_lease_expires_at=1300, permissions=frozenset({'read_state', 'move_forward', 'move_right'}),
            capabilities={'read_state': True, 'move_forward': True, 'move_right': True, 'UAV_MOVE_DISTANCE': True},
            state={'online': True, 'airborne': True, 'longitude': 113, 'latitude': 23,
                   'altitude_m': 100, 'height_m': 50, 'yaw_deg': 90})
        self.opener = ReceiptJavaOpener()
        self.brain = ReceiptBrain()
        self.journal_path = self.directory.name + '/commands.sqlite3'
        self.store = MemoryCheckpointStore(clock=lambda: self.now)
        self.executor = self.new_executor()
        self.service = self.new_service(self.executor)
        self.counter = 0

    def new_executor(self, *, track=True):
        transport = KtdHttpTransport('https://synthetic.invalid/api', token='synthetic-token',
                                    actor_id='actor-1', opener=self.opener)
        return KtdPlatformExecutor(self.binding, site_id='site-1', task_id=None,
            snapshot_provider=lambda: self.snapshot, transport=transport,
            journal=SqliteCommandJournal(self.journal_path), enabled=True, track_commands=track,
            clock=lambda: self.now, ttl_sec=120)

    def new_service(self, executor, store=None):
        return CockpitService(self.binding, executor, lambda: self.brain,
            gateway_token='synthetic-gateway', clock=lambda: self.now,
            store=self.store if store is None else store)

    def request(self, route, payload=None, *, method='POST', session='tab-1', service=None):
        return (service or self.service).handle(method, '/api/flight-control/agent/' + route,
            '' if method == 'GET' else json.dumps(payload or {}),
            {'X-Agent-Gateway': 'synthetic-gateway', 'X-Agent-Actor': 'actor-1', 'X-Agent-Session': session})

    def message(self, text='先向前飞10米再向右飞5米', **options):
        self.counter += 1
        return self.request('messages', {'text': text, 'request_id': 'request-' + str(self.counter),
                                         'context_version': 1}, **options)

    def confirm(self, card, **options):
        return self.request('confirm', {key: card[key] for key in ('confirmation_id',
            'confirmation_binding', 'command_id', 'idempotency_key', 'context_version')}, **options)

    def submit(self, **options):
        self.brain.decisions.append(move())
        proposal = self.message(**options)
        self.assertEqual(proposal.status_code, 200, proposal.payload)
        card = proposal.payload['card']
        self.assertIs(card.get('receipt_tracking'), True)
        self.assertEqual(card['command_id'], card['idempotency_key'])
        accepted = self.confirm(card, **options)
        self.assertIn(accepted.payload['status'], ('submitted', 'unknown'), accepted.payload)
        return card, accepted

    def report(self, card, status='completed', version=2):
        data = self.opener.reports[card['command_id']]['data']
        data.update(status=status, version=version, updated_at=1001)
        data.update(reported_at=1001 if status in {'accepted', 'in_progress', 'completed', 'failed', 'cancelled'} else None,
                    received_at=1001 if status in {'accepted', 'in_progress', 'completed', 'failed', 'cancelled'} else None)
        self.now = 1121 if status == 'expired' else 1002

    def query(self, card, **options):
        return self.request('receipt?command_id=' + card['command_id'] + '&context_version=1',
                            method='GET', **options)

    def posts(self):
        return [call for call in self.opener.calls if call[0] == 'POST']

    def test_completed_report_restores_intent_only_for_a_new_explicit_proposal(self):
        card, accepted = self.submit()
        self.assertEqual(accepted.payload['status'], 'submitted')
        self.assertNotIn('device_report', accepted.payload)
        self.assertNotIn('_dispatch_fingerprint', json.dumps(accepted.payload))
        self.assertEqual(self.message('继续').payload['status'], 'clarification')
        self.assertEqual(len(self.brain.inputs), 1)
        self.report(card)
        result = self.query(card)
        self.assertEqual(result.status_code, 200, result.payload)
        self.assertEqual(result.payload['schema_version'], 'flight-command-receipt/v1')
        self.assertEqual(result.payload['reported_status'], 'completed')
        self.assertTrue(result.payload['continuation_allowed'])
        self.assertEqual(result.payload['device_effect'], 'not_verified')
        self.assertEqual(result.payload['report_source'], 'ktd-command-journal')
        self.assertEqual(set(result.payload), {'success', 'schema_version', 'execution_mode',
            'command_id', 'context_version', 'reported_status', 'report_source', 'receipt_version',
            'reported_at', 'received_at', 'device_effect', 'continuation_allowed', 'trace_id'})
        self.assertNotIn('dispatch_ids', json.dumps(result.payload))
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(len(self.brain.inputs), 1)
        self.assertEqual(self.confirm(card).payload, accepted.payload)
        self.assertTrue(self.query(card).payload['continuation_allowed'])
        self.brain.decisions.append(move('move_right', 5))
        next_card = self.message('继续').payload['card']
        self.assertEqual(next_card['action'], 'move_right')
        self.assertNotEqual(next_card['command_id'], card['command_id'])
        self.assertEqual(len(self.posts()), 1)
        model_input = self.brain.inputs[-1]
        self.assertEqual(model_input['original_intent'], '先向前飞10米再向右飞5米')
        completions = [row for row in model_input['conversation'] if row.get('status') == 'completed']
        self.assertEqual(len(completions), 1)
        self.assertEqual(completions[0]['report_source'], 'ktd-command-journal')
        self.assertEqual(completions[0]['action'], 'move_forward')
        self.assertEqual(completions[0]['parameters'], {'distance_m': 10})
        self.assertEqual(model_input['state']['longitude'], 113)

    def model_service(self, replies, calls):
        def model_http(_url, payload, _timeout):
            calls.append(payload)
            action, distance = replies.pop(0)
            return {'message': {'tool_calls': [{'function': {
                'name': action, 'arguments': {'distance_m': distance}}}]}, 'done_reason': 'stop'}
        return CockpitService(self.binding, self.executor,
            lambda: OllamaDecider(model='synthetic-ordered-receipt', transport=model_http, max_attempts=1),
            gateway_token='synthetic-gateway', clock=lambda: self.now, store=self.store)

    def test_reported_completion_advances_order_once_and_repeated_model_step_is_blocked(self):
        calls = []
        self.service = self.model_service([('move_forward', 10), ('move_forward', 10), ('move_right', 5)], calls)
        card = self.message().payload['card']
        self.assertEqual(self.confirm(card).payload['status'], 'submitted')
        self.assertEqual(self.message('继续').payload['status'], 'clarification')
        self.assertEqual(len(calls), 1)
        self.report(card)
        self.assertTrue(self.query(card).payload['continuation_allowed'])
        self.assertTrue(self.query(card).payload['continuation_allowed'])
        wrong = self.message('继续').payload
        self.assertEqual(wrong['status'], 'clarification')
        self.assertNotIn('card', wrong)
        correct = self.message('继续').payload['card']
        self.assertEqual((correct['action'], correct['parameters']), ('move_right', {'distance_m': 5}))
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(len(calls), 3)

    def test_repeated_completed_receipt_does_not_consume_an_identical_second_step(self):
        calls = []
        self.service = self.model_service([('move_forward', 2), ('move_forward', 2)], calls)
        first = self.message('先向前飞2米，再向前飞2米').payload['card']
        self.assertEqual(self.confirm(first).payload['status'], 'submitted')
        self.report(first)
        self.assertTrue(self.query(first).payload['continuation_allowed'])
        self.assertTrue(self.query(first).payload['continuation_allowed'])
        second = self.message('继续').payload['card']
        self.assertNotEqual(first['command_id'], second['command_id'])
        self.assertEqual((second['action'], second['parameters']), ('move_forward', {'distance_m': 2}))
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(len(calls), 2)

    def test_platform_multiline_order_keeps_progress_after_clarification_and_parameter_answer(self):
        calls = []
        self.service = self.model_service([('move_forward', 2), ('move_forward', 2),
            ('move_forward', 2), ('move_right', 3)], calls)
        first = self.message('先向前飞2米，\n再向右飞3米').payload['card']
        self.assertEqual(self.confirm(first).payload['status'], 'submitted')
        self.report(first)
        self.assertTrue(self.query(first).payload['continuation_allowed'])
        self.assertTrue(self.query(first).payload['continuation_allowed'])
        self.assertEqual(self.message('继续').payload['status'], 'clarification')
        wrong = self.message('3米').payload
        self.assertEqual(wrong['status'], 'clarification')
        self.assertNotIn('card', wrong)
        next_card = self.message('继续').payload['card']
        self.assertEqual((next_card['action'], next_card['parameters']), ('move_right', {'distance_m': 3}))
        self.assertEqual(len(self.posts()), 1)

    def test_other_session_and_restart_can_read_but_cannot_resume_the_original_intent(self):
        card, _ = self.submit()
        self.report(card)
        self.assertFalse(self.query(card, session='tab-2').payload['continuation_allowed'])
        self.assertEqual(self.message('继续', session='tab-2').payload['status'], 'clarification')
        restarted = self.new_service(self.new_executor())
        report = self.query(card, service=restarted)
        self.assertEqual(report.payload['reported_status'], 'completed', report.payload)
        self.assertFalse(report.payload['continuation_allowed'])
        self.assertEqual(self.message('继续', service=restarted).payload['status'], 'clarification')
        self.assertTrue(self.query(card).payload['continuation_allowed'])
        self.assertEqual(len(self.posts()), 1)

    def test_each_noncompleted_report_stays_read_only_and_keeps_continuation_paused(self):
        for status in ('dispatching', 'submitted', 'accepted', 'in_progress', 'failed',
                       'cancelled', 'expired', 'unknown', 'conflict'):
            with self.subTest(status=status):
                self.now = 1000
                card, accepted = self.submit(session=status)
                self.report(card, status)
                result = self.query(card, session=status)
                self.assertEqual(result.status_code, 200, result.payload)
                self.assertEqual(result.payload['reported_status'], status)
                self.assertFalse(result.payload['continuation_allowed'])
                before = len(self.brain.inputs)
                self.assertEqual(self.message('继续', session=status).payload['status'], 'clarification')
                self.assertEqual(len(self.brain.inputs), before)
                self.assertEqual(self.confirm(card, session=status).payload, accepted.payload)
        self.assertEqual(len(self.posts()), 9)

    def test_unknown_submission_can_be_resolved_without_retransmitting(self):
        self.opener.post_failure = TimeoutError('private-upstream-detail')
        card, accepted = self.submit()
        self.assertEqual(accepted.payload['status'], 'unknown')
        self.report(card)
        result = self.query(card)
        self.assertTrue(result.payload['continuation_allowed'], result.payload)
        self.assertEqual(self.confirm(card).payload, accepted.payload)
        self.assertEqual(len(self.posts()), 1)

    def test_query_failure_revokes_continuation_and_retains_both_prior_reports(self):
        card, accepted = self.submit()
        self.report(card)
        self.assertTrue(self.query(card).payload['continuation_allowed'])
        before = self.store.load('tab-1').checkpoint.context['conversation']
        self.opener.failure = TimeoutError('private-upstream-detail')
        failed = self.query(card)
        self.assertEqual(failed.status_code, 503, failed.payload)
        self.assertNotIn('private-upstream-detail', json.dumps(failed.payload))
        self.assertEqual(self.message('继续').payload['status'], 'clarification')
        self.assertEqual(len(self.brain.inputs), 1)
        self.assertEqual(self.store.load('tab-1').checkpoint.context['conversation'], before)
        self.assertEqual(self.confirm(card).payload, accepted.payload)
        self.opener.failure = None
        self.assertTrue(self.query(card).payload['continuation_allowed'])
        self.assertEqual(len(self.posts()), 1)

    def test_refresh_suspends_continuation_until_the_read_finishes(self):
        card, _ = self.submit()
        self.report(card)
        self.assertTrue(self.query(card).payload['continuation_allowed'])
        self.opener.entered, self.opener.release = threading.Event(), threading.Event()
        result = []
        worker = threading.Thread(target=lambda: result.append(self.query(card)))
        worker.start()
        self.assertTrue(self.opener.entered.wait(1))
        try:
            self.assertEqual(self.message('继续').payload['status'], 'clarification')
            self.assertEqual(len(self.brain.inputs), 1)
            self.assertTrue(worker.is_alive())
        finally:
            self.opener.release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(result[0].payload['continuation_allowed'])
        self.assertEqual(len(self.posts()), 1)

    def test_completion_with_unavailable_state_is_readable_but_does_not_allow_continuation(self):
        card, _ = self.submit()
        self.report(card)
        self.snapshot = replace(self.snapshot, observed_at=900)
        result = self.query(card)
        self.assertEqual(result.payload['reported_status'], 'completed')
        self.assertFalse(result.payload['continuation_allowed'])
        self.assertEqual(self.message('继续').payload['status'], 'clarification')
        self.assertEqual(len(self.brain.inputs), 1)
        self.snapshot = replace(self.snapshot, observed_at=1002)
        self.assertTrue(self.query(card).payload['continuation_allowed'])

    def test_reset_during_receipt_read_does_not_wait_or_restore_old_intent(self):
        card, _ = self.submit()
        self.report(card)
        self.opener.entered, self.opener.release = threading.Event(), threading.Event()
        result = []
        worker = threading.Thread(target=lambda: result.append(self.query(card)))
        worker.start()
        self.assertTrue(self.opener.entered.wait(1))
        try:
            reset = self.request('reset', {'context_version': 1})
            self.assertEqual(reset.payload['status'], 'cancelled')
            self.assertTrue(worker.is_alive())
        finally:
            self.opener.release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0].payload['reported_status'], 'completed')
        self.assertFalse(result[0].payload['continuation_allowed'])
        self.assertFalse(self.query(card).payload['continuation_allowed'])
        self.assertEqual(self.message('继续').payload['status'], 'clarification')
        self.assertEqual(len(self.brain.inputs), 1)

    def test_new_intent_during_receipt_read_cannot_be_replaced_by_the_late_report(self):
        card, _ = self.submit()
        self.report(card)
        self.opener.entered, self.opener.release = threading.Event(), threading.Event()
        result = []
        worker = threading.Thread(target=lambda: result.append(self.query(card)))
        worker.start()
        self.assertTrue(self.opener.entered.wait(1))
        try:
            self.brain.decisions.append(ModelDecision(DecisionKind.CLARIFY, message='向右飞多少米？'))
            self.assertEqual(self.message('向右飞').payload['status'], 'clarification')
            self.assertTrue(worker.is_alive())
        finally:
            self.opener.release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(result[0].payload['continuation_allowed'])
        self.brain.decisions.append(move('move_right', 5))
        self.assertEqual(self.message('5米').payload['card']['action'], 'move_right')
        self.assertEqual(self.brain.inputs[-1]['original_intent'], '向右飞\n5米')
        self.assertEqual(len(self.posts()), 1)

    def test_missing_checkpoint_and_untracked_commands_cannot_restore_intent(self):
        card, _ = self.submit()
        self.report(card)
        self.store.delete('tab-1')
        result = self.query(card)
        self.assertEqual(result.payload['reported_status'], 'completed')
        self.assertFalse(result.payload['continuation_allowed'])
        self.assertEqual(self.message('继续').payload['status'], 'clarification')
        self.service = self.new_service(self.new_executor(track=False))
        self.brain.decisions.append(move())
        untracked_card = self.message().payload['card']
        self.assertFalse(untracked_card['receipt_tracking'])
        self.assertEqual(self.confirm(untracked_card).payload['status'], 'submitted')
        calls = len(self.opener.calls)
        self.assertEqual(self.query(untracked_card).payload['error_code'], 'RECEIPT_UNSUPPORTED')
        self.assertEqual(len(self.opener.calls), calls)

    def test_waiting_for_a_receipt_does_not_bypass_the_turn_budget(self):
        self.service = CockpitService(self.binding, self.executor, lambda: self.brain,
            gateway_token='synthetic-gateway', clock=lambda: self.now, store=self.store, max_turns=2)
        self.submit()
        self.assertEqual(self.message('继续').payload['status'], 'clarification')
        self.assertEqual(self.message('继续').payload['error_code'], 'TURN_LIMIT')
        self.assertEqual(len(self.brain.inputs), 1)


if __name__ == '__main__':
    unittest.main()
