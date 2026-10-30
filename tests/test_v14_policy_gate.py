"""FLIGHT-V14-POLICY-GATE：模型前确定性策略门禁、坐标/红外 grounding 与航线中悬停说明。

模型 HTTP 边界使用合成回复并计数，其余走真实 CockpitService 与内存模拟器；
“模型调用 0 次”以传输层计数为准，不以返回文本推断。
"""
import json
import tempfile
import unittest
from pathlib import Path

from lingxi_flight_control import MemoryFlightControlSimulator, RuntimeBinding
from lingxi_flight_control.cockpit import PROMPT_VERSION, CockpitService, grounded_parameters
from lingxi_flight_control.full_control import ACTION_SPECS, FlightControlAction
from lingxi_flight_control.flight_policy import CONTROL_MESSAGE, ROUTE_HOVER_NOTICE, ROUTE_MESSAGE
from lingxi_flight_control.ollama_decider import OllamaDecider
from lingxi_flight_control.ktd_platform import (
    KtdPlatformError, KtdSnapshot, KtdPlatformExecutor, SqliteCommandJournal,
)
from lingxi_flight_control.zhuoyi_platform import STATE_SCHEMA, ZhuoyiPlatformExecutor


def tool(name, **arguments):
    return {'message': {'tool_calls': [{'function': {'name': name, 'arguments': arguments}}]},
            'done_reason': 'stop'}


class PolicyGateTests(unittest.TestCase):
    """覆盖航线中微操、无控制权、载荷放行、否定句与悬停卡片说明。"""

    def build(self, *, owned=True, route_status='running', direct=False):
        self.binding = RuntimeBinding('actor-1', 'project-1', 'hangar-1', 'drone-1', 'batch-1', 1)
        self.simulator = MemoryFlightControlSimulator(self.binding, clock=lambda: 1000)
        state = self.simulator.state
        state.control_owner = 'actor-1' if owned else None
        state.longitude, state.latitude, state.altitude_m = 113, 23, 60
        state.task_status = 'flying'
        state.route_status = route_status
        state.route_id = 'route-1' if route_status != 'idle' else None
        state.breakpoint_index = 2 if route_status != 'idle' else None
        self.responses, self.calls, self.counter = [], [], 0

        def transport(_url, payload, _timeout):
            self.calls.append(payload)
            return self.responses.pop(0)

        self.service = CockpitService(self.binding, self.simulator,
            lambda: OllamaDecider(model='synthetic-gate', transport=transport, max_attempts=1),
            clock=lambda: 1000, enable_direct_commands=direct)

    def test_control_acquire_description_is_neutral_and_prompt_version_is_bumped(self):
        description = ACTION_SPECS[FlightControlAction.CONTROL_ACQUIRE].description
        self.assertEqual(description, '申请或获取本无人机控制权；仅在用户明确要求申请或获取控制权时使用')
        self.assertNotIn('执行任何飞行动作前必须先获取', description)
        self.assertEqual(PROMPT_VERSION, 'cockpit-prompt-v1.6')

    def message(self, text):
        self.counter += 1
        response = self.service.handle('POST', '/api/flight-control/agent/messages', json.dumps({
            'text': text, 'request_id': f'request-{self.counter}', 'context_version': 1}),
            {'X-Agent-Session': 'policy-gate-tab'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.payload['dispatched'])
        return response.payload

    def assert_gate_rejected(self, reply, code, message, *, calls=0):
        self.assertEqual(reply['status'], 'rejected')
        self.assertEqual(reply['error_code'], code)
        self.assertEqual(reply['message'], message)
        self.assertIn(reply['interpretation_source'], ('deterministic-policy-gate', 'model-proposal-policy-gate'))
        self.assertNotIn('card', reply)
        self.assertEqual(len(self.calls), calls)

    def test_micro_motion_during_route_is_rejected_without_model(self):
        for direct in (False, True):
            for text in ('向左飞10米', '向右平移5米', '上升3米', '左转90度', '向右偏航45度'):
                with self.subTest(direct=direct, text=text):
                    self.build(direct=direct)
                    before = self.simulator.state.to_dict()
                    self.assert_gate_rejected(self.message(text), 'ROUTE_PAUSE_REQUIRED', ROUTE_MESSAGE)
                    self.assertEqual(self.simulator.state.to_dict(), before)

    def test_flight_actions_without_control_are_rejected_without_model(self):
        for text in ('向前飞10米', '一键返航', '急停', '悬停'):
            with self.subTest(text=text):
                self.build(owned=False, route_status='paused')
                self.assert_gate_rejected(self.message(text), 'CONTROL_NOT_OWNED', CONTROL_MESSAGE)

    def test_control_check_precedes_route_check(self):
        self.build(owned=False, route_status='running')
        self.assert_gate_rejected(self.message('向左飞10米'), 'CONTROL_NOT_OWNED', CONTROL_MESSAGE)

    def test_payload_during_route_still_reaches_preview(self):
        self.build()
        self.responses = [tool('gimbal_down', angle_deg=30)]
        reply = self.message('云台向下看30度')
        self.assertEqual(reply['status'], 'preview_required')
        self.assertEqual((reply['card']['action'], reply['card']['requested_parameters']),
                         ('gimbal_down', {'angle_deg': 30}))
        self.assertEqual(len(self.calls), 1)

    def test_pause_and_return_during_route_are_not_gate_rejected(self):
        self.build()
        reply = self.message('一键返航')
        self.assertEqual(reply['status'], 'preview_required')
        self.assertEqual(reply['card']['action'], 'return_home')
        self.build()
        self.responses = [tool('route_pause')]
        reply = self.message('暂停航线')
        self.assertEqual((reply['status'], reply['card']['action']), ('preview_required', 'route_pause'))

    def test_negated_or_conditional_motion_is_not_rejected_as_action(self):
        self.build()
        reply = self.message('不要向左飞10米')
        self.assertEqual((reply['status'], reply.get('kind')), ('final', 'no_action'))
        self.assertNotIn('error_code', reply)
        reply = self.message('如果有风就向左飞10米')
        self.assertEqual(reply['status'], 'clarification')
        self.assertNotIn('error_code', reply)
        self.assertEqual(self.calls, [])

    def test_allowed_motion_still_uses_model_when_direct_commands_disabled(self):
        self.build(route_status='paused')
        self.responses = [tool('move_left', distance_m=10)]
        reply = self.message('向左飞10米')
        self.assertEqual((reply['status'], reply['card']['action']), ('preview_required', 'move_left'))
        self.assertEqual(reply['interpretation_source'], 'model')
        self.assertEqual(len(self.calls), 1)

    def test_route_hover_card_explains_pause_and_manual_hover_does_not(self):
        self.build(route_status='running')
        reply = self.message('悬停')
        self.assertEqual((reply['status'], reply['card']['action']), ('preview_required', 'hover'))
        self.assertIn(ROUTE_HOVER_NOTICE, reply['card']['warnings'])
        self.assertIn(ROUTE_HOVER_NOTICE, reply['message'])
        self.assertEqual(reply['card']['prompt_version'], PROMPT_VERSION)
        self.build(route_status='idle')
        reply = self.message('悬停')
        self.assertEqual((reply['status'], reply['card']['action']), ('preview_required', 'hover'))
        self.assertNotIn(ROUTE_HOVER_NOTICE, reply['card']['warnings'])
        self.assertNotIn(ROUTE_HOVER_NOTICE, reply['message'])
        self.assertEqual(self.calls, [])

    def test_coordinate_flight_spec_sentence_reaches_preview(self):
        self.build(route_status='paused')
        self.responses = [tool('point_flight', longitude=118, latitude=34, altitude_m=50)]
        reply = self.message('去东经118，北纬34，高50')
        self.assertEqual(reply['status'], 'preview_required')
        self.assertEqual(reply['card']['requested_parameters'], {'longitude': 118, 'latitude': 34, 'altitude_m': 50})

    def test_model_control_acquire_fallback_uses_policy_message_without_card(self):
        for text in ('往左边挪10米', '云台向下30度', '向左侧移动十米'):
            with self.subTest(text=text):
                self.build(owned=False, route_status='paused')
                before = self.simulator.state.to_dict()
                self.responses = [tool('control_acquire', mode='apply', reason='模型自行申请')]
                reply = self.message(text)
                self.assert_gate_rejected(reply, 'CONTROL_NOT_OWNED', CONTROL_MESSAGE, calls=1)
                self.assertEqual(reply['interpretation_source'], 'model-proposal-policy-gate')
                self.assertEqual(self.simulator.state.to_dict(), before)
                self.assertEqual(self.simulator.state.control_owner, None)

    def test_model_control_acquire_fallback_keeps_existing_clarification_when_owned(self):
        self.build(owned=True, route_status='paused')
        self.responses = [tool('control_acquire', mode='apply', reason='模型自行申请')]
        reply = self.message('往左边挪10米')
        self.assertEqual(reply['status'], 'clarification')
        self.assertEqual(reply['message'], '本次没有明确请求这项控制权操作，请重新说明动作；我不会替你申请或释放控制权。')
        self.assertNotIn('card', reply)

    def test_explicit_control_request_still_previews_acquire(self):
        self.build(owned=False, route_status='paused')
        self.responses = [tool('control_acquire', mode='apply', reason='灵犀 Agent 申请无人机控制权')]
        reply = self.message('申请控制权')
        self.assertEqual(reply['status'], 'preview_required')
        self.assertEqual(reply['card']['action'], 'control_acquire')

    def test_unknown_control_owner_does_not_claim_not_owned(self):
        self.build(owned=False, route_status='paused')
        original = self.service.executor.read_state
        self.service.executor.read_state = lambda: {
            key: value for key, value in original().items() if key != 'control_owner'
        }
        self.responses = [tool('control_acquire', mode='apply', reason='模型自行申请')]
        reply = self.message('往左边挪10米')
        self.assertEqual(reply['status'], 'clarification')
        self.assertNotEqual(reply.get('error_code'), 'CONTROL_NOT_OWNED')

    def test_model_proposal_uses_state_read_after_model_call(self):
        self.build(owned=False, route_status='paused')
        self.responses = [tool('control_acquire', mode='apply', reason='模型自行申请')]

        def transport(_url, _payload, _timeout):
            self.simulator.state.control_owner = 'actor-1'
            self.calls.append('model')
            return self.responses.pop(0)

        self.service._factory = lambda: OllamaDecider(
            model='synthetic-gate', transport=transport, max_attempts=1)
        reply = self.message('往左边挪10米')
        self.assertEqual(reply['status'], 'clarification')
        self.assertEqual(reply['message'], '本次没有明确请求这项控制权操作，请重新说明动作；我不会替你申请或释放控制权。')
        self.assertEqual(self.calls, ['model'])


class PlatformGateTests(unittest.TestCase):
    """平台快照门禁只消费可证明的权威控制权与航线状态。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.binding = RuntimeBinding('actor-1', 'project-1', 'hangar-1', 'drone-1', 'batch-1', 1)
        self.calls = 0

    def decider_factory(self):
        self.calls += 1
        response = getattr(self, 'model_response', tool('move_forward', distance_m=10))
        return OllamaDecider(model='synthetic-gate', transport=lambda *_: response,
                             max_attempts=1)

    def service(self, executor):
        return CockpitService(self.binding, executor, self.decider_factory,
                              gateway_token='synthetic-gateway', clock=lambda: 1000, task_id='task-1')

    @staticmethod
    def headers(session):
        return {'X-Agent-Gateway': 'synthetic-gateway', 'X-Agent-Actor': 'actor-1',
                'X-Agent-Session': session}

    def ktd(self, *, owner='other', route_status='paused', unavailable=False,
            quality='authoritative', legacy_read_api=False):
        now = 1000.0
        snapshot = KtdSnapshot(
            actor_id='actor-1', project_id='project-1', hangar_id='hangar-1', drone_id='drone-1',
            site_id='site-1', task_id='task-1', context_version=1, version=7, observed_at=now,
            control_owner=owner, profile='DJI_HANGAR', permissions=frozenset({'read_state', 'move_forward'}),
            capabilities={'move_forward': True}, control_lease_expires_at=now + 60,
            state={'online': True, 'airborne': True, 'route_status': route_status},
            authorization_quality=quality)
        provider = (lambda: (_ for _ in ()).throw(KtdPlatformError('AUTHORITATIVE_STATE_UNAVAILABLE'))
                    if unavailable else snapshot)
        return KtdPlatformExecutor(
            self.binding, site_id='site-1', task_id='task-1', transport=object(),
            snapshot_provider=provider,
            journal=SqliteCommandJournal(str(Path(self.temp.name) / 'commands.sqlite3')),
            enabled=False, clock=lambda: now, legacy_read_api=legacy_read_api)

    def test_ktd_other_owner_is_rejected_before_model(self):
        reply = self.service(self.ktd(owner='other')).handle(
            'POST', '/api/flight-control/agent/messages',
            json.dumps({'text': '向前飞10米', 'request_id': 'ktd-owner', 'context_version': 1}),
            self.headers('ktd-owner'))
        self.assertEqual((reply.payload['status'], reply.payload['error_code']), ('rejected', 'CONTROL_NOT_OWNED'))
        self.assertEqual(self.calls, 0)

    def test_ktd_legacy_owner_is_left_to_executor(self):
        executor = self.ktd(owner='other', quality='legacy-read-api', legacy_read_api=True)
        state = executor.read_state()
        self.assertTrue(state['available'])
        self.assertEqual(state['authorization_quality'], 'legacy-read-api')
        self.assertEqual(state['control_owner'], 'other')
        reply = self.service(executor).handle(
            'POST', '/api/flight-control/agent/messages',
            json.dumps({'text': '向前飞10米', 'request_id': 'ktd-legacy', 'context_version': 1}),
            self.headers('ktd-legacy'))
        self.assertNotEqual(reply.payload.get('error_code'), 'CONTROL_NOT_OWNED')
        self.assertEqual(self.calls, 1)

    def test_ktd_legacy_model_acquire_keeps_clarification(self):
        executor = self.ktd(owner='other', quality='legacy-read-api', legacy_read_api=True)
        state = executor.read_state()
        self.assertTrue(state['available'])
        self.assertEqual(state['authorization_quality'], 'legacy-read-api')
        self.assertEqual(state['control_owner'], 'other')
        self.model_response = tool('control_acquire', mode='apply', reason='模型自行申请')
        reply = self.service(executor).handle(
            'POST', '/api/flight-control/agent/messages',
            json.dumps({'text': '往左边挪10米', 'request_id': 'ktd-legacy-acquire', 'context_version': 1}),
            self.headers('ktd-legacy-acquire'))
        self.assertEqual(reply.payload['status'], 'clarification')
        self.assertEqual(reply.payload['message'], '本次没有明确请求这项控制权操作，请重新说明动作；我不会替你申请或释放控制权。')
        self.assertNotEqual(reply.payload.get('error_code'), 'CONTROL_NOT_OWNED')
        self.assertNotIn('card', reply.payload)
        self.assertEqual(self.calls, 1)

    def test_ktd_running_route_is_rejected_before_model(self):
        reply = self.service(self.ktd(owner='actor-1', route_status='running')).handle(
            'POST', '/api/flight-control/agent/messages',
            json.dumps({'text': '向左飞10米', 'request_id': 'ktd-route', 'context_version': 1}),
            self.headers('ktd-route'))
        self.assertEqual((reply.payload['status'], reply.payload['error_code']), ('rejected', 'ROUTE_PAUSE_REQUIRED'))
        self.assertEqual(self.calls, 0)

    def test_ktd_unavailable_state_is_not_misreported_as_not_owned(self):
        reply = self.service(self.ktd(unavailable=True)).handle(
            'POST', '/api/flight-control/agent/messages',
            json.dumps({'text': '向前飞10米', 'request_id': 'ktd-unavailable', 'context_version': 1}),
            self.headers('ktd-unavailable'))
        self.assertNotEqual(reply.payload.get('error_code'), 'CONTROL_NOT_OWNED')
        self.assertEqual(self.calls, 1)

    def test_zhuoyi_running_route_is_rejected_before_model(self):
        state = {
            'schema_version': STATE_SCHEMA, 'snapshot_version': 'f' * 64,
            'project_id': 'project-1', 'site_id': 'site-1', 'hangar_id': 'hangar-1',
            'drone_id': 'drone-1', 'task_id': 'task-1', 'profile': 'DJI_HANGAR',
            'online': True, 'airborne': True, 'route_status': 'running', 'route_id': 'route-1',
            'task_type': 'inspection', 'mapping_invalidated': False, 'route_resume_disabled': False,
            'task_status': 'flying', 'low_battery_return': False,
            'control_owner_employee_id': 'actor-1', 'permissions': ['move_forward'],
            'action_availability': {'move_forward': {'available': True, 'reasons': []}},
        }
        def transport(method, url, body, headers, timeout):
            return 200, state
        executor = ZhuoyiPlatformExecutor(self.binding, base_url='http://127.0.0.1:18091',
                                          gateway_token='gateway', site_id='site-1', task_id='task-1',
                                          transport=transport, clock=lambda: 1000)
        reply = self.service(executor).handle(
            'POST', '/api/flight-control/agent/messages',
            json.dumps({'text': '向左飞10米', 'request_id': 'zhuoyi-route', 'context_version': 1}),
            self.headers('zhuoyi-route'))
        self.assertEqual((reply.payload['status'], reply.payload['error_code']), ('rejected', 'ROUTE_PAUSE_REQUIRED'))
        self.assertEqual(self.calls, 0)


class GroundingUnitTests(unittest.TestCase):
    """只放开坐标飞行高度/环绕半径的省略单位；其他参数仍不补猜。"""

    def test_point_altitude_and_orbit_radius_accept_labelled_unitless_values(self):
        point = {'longitude': 118, 'latitude': 34, 'altitude_m': 50}
        self.assertTrue(grounded_parameters('point_flight', point, '去东经118，北纬34，高50'))
        self.assertTrue(grounded_parameters('point_flight', point, '去东经118，北纬34，高度50'))
        self.assertTrue(grounded_parameters('point_flight', point, '去东经118，北纬34，高50米'))
        self.assertTrue(grounded_parameters('point_orbit', {'longitude': 118, 'latitude': 34, 'radius_m': 30},
                                            '绕东经118，北纬34环绕，半径30'))

    def test_unlabelled_or_other_unitless_values_are_still_rejected(self):
        self.assertFalse(grounded_parameters('point_flight', {'longitude': 118, 'latitude': 34, 'altitude_m': 50},
                                             '去东经118，北纬34，50'))
        self.assertFalse(grounded_parameters('move_forward', {'distance_m': 10}, '向前飞10'))
        self.assertFalse(grounded_parameters('move_up', {'distance_m': 10}, '高10'))
        self.assertFalse(grounded_parameters('turn_left', {'angle_deg': 30}, '左转30'))

    def test_infrared_limits_accept_below_zero_and_keep_role_binding(self):
        text = '红外测温下限设为零下10度，上限设为80摄氏度'
        self.assertTrue(grounded_parameters('infrared_set_limits', {'min_c': -10, 'max_c': 80}, text))
        self.assertFalse(grounded_parameters('infrared_set_limits', {'min_c': 10, 'max_c': 80}, text))
        self.assertFalse(grounded_parameters('infrared_set_limits', {'min_c': 80, 'max_c': -10}, text))


if __name__ == '__main__':
    unittest.main()
