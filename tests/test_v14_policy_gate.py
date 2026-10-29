"""FLIGHT-V14-POLICY-GATE：模型前确定性策略门禁、坐标/红外 grounding 与航线中悬停说明。

模型 HTTP 边界使用合成回复并计数，其余走真实 CockpitService 与内存模拟器；
“模型调用 0 次”以传输层计数为准，不以返回文本推断。
"""
import json
import unittest

from lingxi_flight_control import MemoryFlightControlSimulator, RuntimeBinding
from lingxi_flight_control.cockpit import PROMPT_VERSION, CockpitService, grounded_parameters
from lingxi_flight_control.flight_policy import CONTROL_MESSAGE, ROUTE_HOVER_NOTICE, ROUTE_MESSAGE
from lingxi_flight_control.ollama_decider import OllamaDecider


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

    def message(self, text):
        self.counter += 1
        response = self.service.handle('POST', '/api/flight-control/agent/messages', json.dumps({
            'text': text, 'request_id': f'request-{self.counter}', 'context_version': 1}),
            {'X-Agent-Session': 'policy-gate-tab'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.payload['dispatched'])
        return response.payload

    def assert_gate_rejected(self, reply, code, message):
        self.assertEqual(reply['status'], 'rejected')
        self.assertEqual(reply['error_code'], code)
        self.assertEqual(reply['message'], message)
        self.assertEqual(reply['interpretation_source'], 'deterministic-policy-gate')
        self.assertNotIn('card', reply)
        self.assertEqual(self.calls, [])

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
