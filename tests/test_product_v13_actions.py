import json
import unittest
from dataclasses import replace

from lingxi_flight_control import MemoryFlightControlSimulator, RuntimeBinding, FlightControlActionRequest
from lingxi_flight_control.cockpit import CockpitService
from lingxi_flight_control.flight_policy import PathCheck
from fence_support import StaticFenceProvider


class ProductActionsTests(unittest.TestCase):
    def setUp(self):
        self.binding = RuntimeBinding('actor', 'project', 'hangar', 'drone', 'batch', 1)
        self.sim = MemoryFlightControlSimulator(self.binding,
                                                         path_checker=StaticFenceProvider(PathCheck('clear')))
        self.sim.state.control_owner = 'actor'
        self.sim.state.configured_algorithms = ['烟火', '车辆']
        self.service = CockpitService(self.binding, self.sim, lambda: None,
            enable_plan_execution=True, plan_decider_factory=lambda: None)
        self.count = 0

    def send(self, text):
        self.count += 1
        return self.service.handle('POST', '/api/flight-control/agent/messages', json.dumps({
            'text': text, 'request_id': str(self.count), 'context_version': 1}),
            {'X-Agent-Session': 'product-actions'}).payload

    def confirm(self, card):
        payload = {key: card[key] for key in ('confirmation_id', 'confirmation_binding',
            'command_id', 'idempotency_key', 'context_version')}
        return self.service.handle('POST', '/api/flight-control/agent/confirm', json.dumps(payload),
            {'X-Agent-Session': 'product-actions'}).payload

    def test_payload_text_and_percentages_execute_only_after_confirmation(self):
        for text, action, effective in [('开始喊话：交通安全', 'speaker_speak', {'text': '交通安全'}),
            ('喊话音量调到150%', 'speaker_volume', {'value': 100}),
            ('亮度调到200%', 'searchlight_brightness', {'value': 100})]:
            before = self.sim.state.to_dict()
            reply = self.send(text)
            self.assertEqual(reply['status'], 'preview_required', reply)
            self.assertEqual(reply['card']['action'], action)
            self.assertEqual(reply['card']['parameters'], effective)
            self.assertEqual(self.sim.state.to_dict(), before)
            result = self.confirm(reply['card'])
            self.assertEqual(result['status'], 'accepted')
            after = self.sim.state.to_dict()
            self.assertEqual(self.confirm(reply['card']), result)
            self.assertEqual(self.sim.state.to_dict(), after)

    def test_decimal_payload_percentage_is_normalized_before_validation(self):
        reply = self.send('喊话音量调到55.5%')
        self.assertEqual(reply['status'], 'preview_required', reply)
        self.assertEqual(reply['card']['parameters'], {'value': 56})
        self.assertEqual(reply['card']['adjustments'][0]['effective'], 56)
        self.assertEqual(self.confirm(reply['card'])['status'], 'accepted')

    def test_ai_arrival_departure_keeps_global_and_other_annotation_algorithms(self):
        s = self.sim.state
        s.annotation_algorithms = {'A': ['烟火'], 'B': ['烟火', '车辆']}
        self.confirm(self.send('打开AI识别烟火')['card'])
        for annotation in ('A', 'B'):
            self.sim.apply_task_event('annotation_entered', task_batch_id='batch', annotation_id=annotation)
        self.sim.apply_task_event('annotation_left', task_batch_id='batch', annotation_id='A')
        self.assertEqual(s.active_algorithms, ['烟火', '车辆'])
        self.sim.apply_task_event('annotation_left', task_batch_id='batch', annotation_id='B')
        self.assertEqual(s.active_algorithms, ['烟火'])
        self.confirm(self.send('关闭AI识别烟火')['card'])
        self.assertEqual(s.active_algorithms, [])
        self.assertEqual(self.send('打开AI识别未知')['error_code'], 'ALGORITHM_NOT_CONFIGURED')

    def test_end_current_preserves_main_but_end_all_clears_it(self):
        s = self.sim.state
        s.route_id, s.breakpoint_index, s.temporary_task_id = 'main', 4, 'temporary'
        s.route_status = 'point_flight'
        self.confirm(self.send('结束当前任务')['card'])
        self.assertEqual(s.breakpoint_index, 4)
        self.assertIsNone(s.temporary_task_id)
        self.confirm(self.send('结束全部任务')['card'])
        self.assertIsNone(s.breakpoint_index)
        self.assertEqual(s.task_status, 'ended')

    def test_parallel_motion_and_speech_are_explicit_and_replayed_once(self):
        reply = self.send('边向前飞10米边喊话：交通安全')
        self.assertEqual(reply['status'], 'preview_required', reply)
        self.assertEqual(reply['card']['execution_order'], 'parallel')
        result = self.confirm(reply['card'])
        self.assertEqual(result['plan_status'], 'completed', result)
        self.assertEqual(len(result['steps']), 2)
        self.assertEqual(self.sim.state.payloads['speaker_text'], '交通安全')
        self.assertEqual(self.confirm(reply['card']), result)

    def test_parallel_preflight_failure_dispatches_neither(self):
        card = self.send('边向前飞10米边喊话：交通安全')['card']
        self.sim._path_checker = StaticFenceProvider(PathCheck('blocked'))
        before = self.sim.state.to_dict()
        result = self.confirm(card)
        self.assertEqual(result['error_code'], 'PARALLEL_PREFLIGHT_FAILED')
        self.assertEqual(self.sim.state.to_dict(), before)

    def test_new_actions_require_owner_and_explicit_intent(self):
        self.sim.state.control_owner = None
        for text in ('开始喊话：交通安全', '喊话音量调到50%', '结束全部任务', '打开AI识别烟火'):
            self.assertEqual(self.send(text)['error_code'], 'CONTROL_NOT_OWNED')
        for text in ('不要结束全部任务', '如果没车就喊话：交通安全', '音量调到50%左右'):
            self.assertNotIn('card', self.send(text))

    def test_platform_payload_encoding_uses_existing_dto_not_camera(self):
        from test_ktd_platform import PlatformExecutorTests
        f = PlatformExecutorTests()
        f.setUp()
        try:
            f.snapshot = replace(f.snapshot, permissions=f.snapshot.permissions | {'speaker_volume', 'searchlight_brightness'},
                capabilities={**f.snapshot.capabilities, 'speaker_volume': True, 'searchlight_brightness': True},
                state={**f.snapshot.state, 'device_limits': {
                    'speaker_volume': {'min': 0, 'max': 100}, 'searchlight_brightness': {'min': 1, 'max': 100}}})
            executor = f.executor()
            for action, path, operation, value in [('speaker_volume', '/screen/speaker/control', 109, '100'),
                ('searchlight_brightness', '/screen/lighting/control', 103, 100)]:
                card = executor.preview(f.request(action, {'value': 150}, key=action))
                f.confirm(executor, card)
                command = f.transport.calls[-1][0]
                self.assertEqual(command.path, path)
                self.assertEqual(command.body['type'], operation)
                self.assertEqual(command.body['param'], value)
                self.assertEqual(command.body['uavId'], 'drone-1')
                self.assertNotIn('terminalId', command.body)
        finally:
            f.doCleanups()

    def test_task_end_platform_safety_check_repeats_before_send(self):
        from test_ktd_platform import PlatformExecutorTests
        from lingxi_flight_control.ktd_platform import KtdHttpResponse
        f = PlatformExecutorTests()
        f.setUp()
        try:
            f.snapshot = replace(f.snapshot, permissions=f.snapshot.permissions | {'task_end_all'},
                capabilities={**f.snapshot.capabilities, 'task_end_all': True},
                state={**f.snapshot.state, 'task_scope': 'bound_main_only'})
            checks = []
            allowed = [True]
            def check(binding, task_id):
                checks.append(task_id)
                return KtdHttpResponse(200, {'code': 200, 'data': {'pass': allowed[0], 'needConfirm': True}})
            f.transport.task_end_check = check
            executor = f.executor()
            card = executor.preview(f.request('task_end_all', {}))
            self.assertEqual(card.status.value, 'preview_required')
            allowed[0] = False
            self.assertEqual(f.confirm(executor, card).error_code, 'TASK_END_SAFETY_REJECTED')
            self.assertEqual(len(checks), 2)
            self.assertEqual(f.transport.calls, [])
        finally:
            f.doCleanups()

    def test_parallel_partial_failure_retains_success_and_does_not_retry(self):
        card = self.send('边向前飞10米边喊话：交通安全')['card']
        original = self.sim.confirm
        calls = []
        def confirm(confirmation_id, **kwargs):
            request = self.sim._pending[confirmation_id].request
            calls.append(request.action)
            if request.action == 'speaker_speak':
                raise TimeoutError('synthetic timeout')
            return original(confirmation_id, **kwargs)
        self.sim.confirm = confirm
        result = self.confirm(card)
        self.assertEqual(result['plan_status'], 'stopped')
        self.assertEqual([row['status'] for row in result['steps']], ['accepted', 'unknown'])
        self.assertNotIn('后续步骤未执行', result['message'])
        self.assertEqual(self.confirm(card), result)
        self.assertEqual(len(calls), 2)

    def test_end_task_cannot_be_invented_by_model(self):
        from lingxi_flight_control.cockpit import grounded_parameters
        self.assertFalse(grounded_parameters('task_end_all', {}, '拍张照'))
        self.assertFalse(grounded_parameters('task_end_all', {}, '结束当前任务'))
        self.assertTrue(grounded_parameters('task_end_all', {}, '结束全部任务'))
