"""真实模型协议→会话→确认卡；仅HTTP模型边界使用独立合成回复。"""
import json
import unittest

from lingxi_flight_control import MemoryFlightControlSimulator, RuntimeBinding
from lingxi_flight_control.cockpit import CockpitService
from lingxi_flight_control.ollama_decider import OllamaDecider


def tool(name, **arguments):
    return {'message': {'tool_calls': [{'function': {'name': name, 'arguments': arguments}}]},
            'done_reason': 'stop'}


class CockpitIntentGuardTests(unittest.TestCase):
    def setUp(self):
        self.binding = RuntimeBinding('actor-1', 'project-1', 'hangar-1', 'drone-1', 'batch-1', 1)
        self.simulator = MemoryFlightControlSimulator(self.binding, clock=lambda: 1000)
        self.simulator.state.control_owner = 'actor-1'
        self.simulator.state.longitude = 113
        self.simulator.state.latitude = 23
        self.simulator.state.altitude_m = 50
        self.simulator.state.heading_deg = 90
        self.responses, self.calls, self.counter = [], [], 0

        def transport(_url, payload, _timeout):
            self.calls.append(payload)
            return self.responses.pop(0)

        self.service = CockpitService(self.binding, self.simulator,
            lambda: OllamaDecider(model='synthetic-clarification', transport=transport, max_attempts=1),
            clock=lambda: 1000)

    def message(self, text):
        self.counter += 1
        response = self.service.handle('POST', '/api/flight-control/agent/messages', json.dumps({
            'text': text, 'request_id': f'request-{self.counter}', 'context_version': 1}),
            {'X-Agent-Session': 'intent-guard-tab'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.payload['dispatched'])
        return response.payload

    def test_natural_parameter_questions_keep_the_followup_direction(self):
        for question in ('后退多少米？', '需要飞多少米？', '请先说距离。', 'What distance should the drone move?'):
            with self.subTest(question=question):
                self.setUp()
                before = self.simulator.state.to_dict()
                self.responses = [{'message': {'content': question}, 'done_reason': 'stop'},
                                  tool('move_backward', distance_m=6)]
                reply = self.message('朝后退')
                self.assertEqual(reply['status'], 'clarification')
                self.assertEqual(reply['message'], question)
                card = self.message('六米')['card']
                self.assertEqual((card['action'], card['parameters']), ('move_backward', {'distance_m': 6}))
                self.assertEqual(len(self.calls), 2)
                self.assertEqual(self.simulator.state.to_dict(), before)

    def test_short_direction_question_is_preserved_instead_of_generic_final(self):
        self.responses = [{'message': {'content': '转向哪个方向？'}, 'done_reason': 'stop'}]
        reply = self.message('转动30度')
        self.assertEqual(reply['status'], 'clarification')
        self.assertEqual(reply['message'], '转向哪个方向？')

    def test_explanation_without_a_question_still_finishes(self):
        self.responses = [{'message': {'content': '当前没有新的飞行动作。'}, 'done_reason': 'stop'}]
        self.assertEqual(self.message('介绍一下你自己')['status'], 'final')

    def test_explicit_action_with_no_motion_or_no_yaw_constraint_can_be_proposed(self):
        for text, name, arguments in (
            ('向右偏航45度，不要产生位移', 'turn_right', {'angle_deg': 45}),
            ('向左转三十度，机身不要移动', 'turn_left', {'angle_deg': 30}),
            ('机头方向不要改变，向左平移4米', 'move_left', {'distance_m': 4}),
            ('Move forward 4 metres, do not change heading', 'move_forward', {'distance_m': 4}),
        ):
            with self.subTest(text=text):
                self.setUp()
                before = self.simulator.state.to_dict()
                self.responses = [tool(name, **arguments)]
                reply = self.message(text)
                self.assertEqual(reply['status'], 'preview_required')
                self.assertEqual((reply['card']['action'], reply['card']['parameters']), (name, arguments))
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(self.simulator.state.to_dict(), before)

    def test_constraint_cannot_authorize_a_contradicting_model_action(self):
        for text, response in (
            ('向前飞4米，不要产生位移', tool('move_forward', distance_m=4)),
            ('向右转45度，机头方向不要改变', tool('turn_right', angle_deg=45)),
            ('返航，不要产生位移', tool('return_home')),
        ):
            with self.subTest(text=text):
                self.setUp()
                before = self.simulator.state.to_dict()
                self.responses = [response]
                reply = self.message(text)
                self.assertEqual(reply['status'], 'clarification')
                self.assertNotIn('card', reply)
                self.assertEqual(self.simulator.state.to_dict(), before)

    def test_deferred_trigger_cannot_create_an_immediate_card_or_survive_as_pending_work(self):
        for text in ('等我说开始以后再向前飞10米', '收到我的口令后再下降6米',
                     '待我准备好再起飞到30米', 'When I say go, move forward 10 metres'):
            with self.subTest(text=text):
                self.setUp()
                self.responses = [tool('move_forward', distance_m=10)]
                reply = self.message(text)
                self.assertEqual(reply['status'], 'clarification')
                self.assertNotIn('card', reply)
                self.assertEqual(self.message('继续')['status'], 'clarification')
                self.assertEqual(self.calls, [])

    def test_standalone_constraints_questions_and_other_negations_stay_non_action(self):
        for text in ('不要产生位移', '机头方向不要改变', '不要产生位移是什么意思？',
                     '不要向左转45度，向前飞4米', '如果没有障碍，向右转45度，不要产生位移'):
            with self.subTest(text=text):
                self.setUp()
                self.responses = [tool('turn_right', angle_deg=45)]
                self.assertEqual(self.message(text)['status'], 'clarification')
                self.assertEqual(self.calls, [])

    def test_constraints_cannot_turn_explanations_or_examples_into_control_requests(self):
        for text, response in (
            ('不要移动，拍照功能怎么用？', tool('camera_photo')),
            ('不要产生位移，请解释控制权申请流程', tool('control_acquire')),
            ('不要移动，向右转45度这句话只做示例', tool('turn_right', angle_deg=45)),
            ('保持机头方向不变，如何前进4米？', tool('move_forward', distance_m=4)),
            ('Do not change heading, explain how to move forward 4 metres.', tool('move_forward', distance_m=4)),
        ):
            with self.subTest(text=text):
                self.setUp()
                self.responses = [response]
                self.assertEqual(self.message(text)['status'], 'clarification')
                self.assertEqual(self.calls, [])
