"""明确方向与数值必须对应；仅在外部模型HTTP边界注入错误提议。"""
import json
import unittest

from lingxi_flight_control import MemoryFlightControlSimulator, RuntimeBinding
from lingxi_flight_control.cockpit import CockpitService
from lingxi_flight_control.ollama_decider import OllamaDecider


def model_action(name, **parameters):
    return {'message': {'tool_calls': [{'function': {'name': name, 'arguments': parameters}}]},
            'done_reason': 'stop'}


class CockpitMotionGroundingTests(unittest.TestCase):
    def setUp(self):
        self.binding = RuntimeBinding('actor', 'project', 'hangar', 'drone', 'batch', 1)
        self.simulator = MemoryFlightControlSimulator(self.binding, clock=lambda: 1000)
        self.simulator.state.control_owner = 'actor'
        self.simulator.state.longitude, self.simulator.state.latitude = 113, 23
        self.simulator.state.altitude_m, self.simulator.state.heading_deg = 50, 90
        self.replies, self.calls, self.sequence = [], [], 0

        def model_http(_url, payload, _timeout):
            self.calls.append(payload)
            return self.replies.pop(0)

        self.service = CockpitService(self.binding, self.simulator,
            lambda: OllamaDecider(model='synthetic-motion-evidence', transport=model_http,
                                  max_attempts=1), clock=lambda: 1000)

    def message(self, text):
        self.sequence += 1
        reply = self.service.handle('POST', '/api/flight-control/agent/messages', json.dumps({
            'text': text, 'request_id': f'motion-{self.sequence}', 'context_version': 1}),
            {'X-Agent-Session': 'motion-tab'})
        self.assertEqual(reply.status_code, 200)
        self.assertFalse(reply.payload['dispatched'])
        return reply.payload

    def confirm(self, card):
        reply = self.service.handle('POST', '/api/flight-control/agent/confirm', json.dumps({
            key: card[key] for key in ('confirmation_id', 'confirmation_binding', 'command_id',
                                       'idempotency_key', 'context_version')}),
            {'X-Agent-Session': 'motion-tab'})
        self.assertEqual(reply.status_code, 200)
        return reply.payload

    def test_opposite_direction_proposals_cannot_create_confirmation_cards(self):
        for text, name, parameters in (
            ('向左飞5米', 'move_right', {'distance_m': 5}),
            ('朝右平移六米', 'move_left', {'distance_m': 6}),
            ('向前飞十米', 'move_backward', {'distance_m': 10}),
            ('后退3米', 'move_forward', {'distance_m': 3}),
            ('上升两米', 'move_down', {'distance_m': 2}),
            ('下降4米', 'move_up', {'distance_m': 4}),
            ('向左偏航45度', 'turn_right', {'angle_deg': 45}),
            ('右转九十度', 'turn_left', {'angle_deg': 90}),
            ('Move left 5 metres', 'move_right', {'distance_m': 5}),
            ('Yaw right 30 degrees', 'turn_left', {'angle_deg': 30}),
        ):
            with self.subTest(text=text):
                self.setUp()
                before = self.simulator.state.to_dict()
                self.replies = [model_action(name, **parameters)]
                reply = self.message(text)
                self.assertEqual(reply['status'], 'clarification')
                self.assertNotIn('card', reply)
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(self.simulator.state.to_dict(), before)

    def test_distance_or_angle_from_another_action_cannot_be_reused(self):
        for text, name, parameters in (
            ('先向前飞10米，再向左飞5米', 'move_forward', {'distance_m': 5}),
            ('先右转90度，再左转45度', 'turn_right', {'angle_deg': 45}),
            ('先向前飞一百厘米，再向后飞两米', 'move_forward', {'distance_m': 2}),
            ('Move forward 10 metres, then move left 5 metres', 'move_forward', {'distance_m': 5}),
        ):
            with self.subTest(text=text):
                self.setUp()
                self.replies = [model_action(name, **parameters)]
                reply = self.message(text)
                self.assertEqual(reply['status'], 'clarification')
                self.assertNotIn('card', reply)

    def test_correct_explicit_direction_and_normalized_units_are_preserved(self):
        for text, name, parameters in (
            ('向左飞5米', 'move_left', {'distance_m': 5}),
            ('朝右平移六米', 'move_right', {'distance_m': 6}),
            ('向前飞一百厘米', 'move_forward', {'distance_m': 1}),
            ('后退一米半', 'move_backward', {'distance_m': 1.5}),
            ('上升两米', 'move_up', {'distance_m': 2}),
            ('下降4米', 'move_down', {'distance_m': 4}),
            ('向左偏航45度', 'turn_left', {'angle_deg': 45}),
            ('右转九十度', 'turn_right', {'angle_deg': 90}),
            ('Move left 5 metres', 'move_left', {'distance_m': 5}),
            ('Yaw right 30 degrees', 'turn_right', {'angle_deg': 30}),
        ):
            with self.subTest(text=text):
                self.setUp()
                self.replies = [model_action(name, **parameters)]
                card = self.message(text)['card']
                self.assertEqual((card['action'], card['parameters']), (name, parameters))

    def test_parameter_followup_keeps_the_original_direction_for_validation(self):
        self.replies = [{'message': {'content': '后退多少米？'}, 'done_reason': 'stop'},
                        model_action('move_forward', distance_m=6)]
        self.assertEqual(self.message('后退')['status'], 'clarification')
        result = self.message('六米')
        self.assertEqual(result['status'], 'clarification')
        self.assertNotIn('card', result)

    def test_matching_composite_steps_still_need_separate_confirmations(self):
        self.replies = [model_action('move_forward', distance_m=10),
                        model_action('move_left', distance_m=5)]
        first = self.message('先向前飞10米，再向左飞5米')['card']
        self.assertEqual(first['action'], 'move_forward')
        self.assertEqual(self.confirm(first)['status'], 'accepted')
        before_next = self.simulator.state.to_dict()
        second = self.message('继续')['card']
        self.assertEqual((second['action'], second['parameters']), ('move_left', {'distance_m': 5}))
        self.assertEqual(self.simulator.state.to_dict(), before_next)
        self.assertNotEqual(first['confirmation_id'], second['confirmation_id'])

    def test_unrecognized_motion_phrasing_remains_on_the_model_path(self):
        self.replies = [model_action('move_backward', distance_m=6)]
        card = self.message('朝机尾方向挪六米')['card']
        self.assertEqual((card['action'], card['parameters']), ('move_backward', {'distance_m': 6}))
        self.assertEqual(len(self.calls), 1)

    def test_other_actions_can_follow_a_confirmed_movement(self):
        self.replies = [model_action('move_forward', distance_m=10), model_action('camera_photo')]
        self.confirm(self.message('先向前飞10米再拍照')['card'])
        card = self.message('继续')['card']
        self.assertEqual(card['action'], 'camera_photo')
        self.assertEqual(self.simulator.state.camera['photo_count'], 0)

    def test_unknown_second_motion_phrasing_is_not_excluded_by_the_first_step(self):
        self.replies = [model_action('move_forward', distance_m=10),
                        model_action('move_backward', distance_m=6)]
        self.confirm(self.message('先向前飞10米，再朝机尾方向挪6米')['card'])
        card = self.message('继续')['card']
        self.assertEqual((card['action'], card['parameters']), ('move_backward', {'distance_m': 6}))

    def test_latest_parameter_revision_overrides_old_value_and_keeps_direction(self):
        for name, distance, expected in (
            ('move_forward', 6, 'preview_required'),
            ('move_backward', 6, 'clarification'),
            ('move_forward', 10, 'clarification'),
        ):
            with self.subTest(name=name, distance=distance):
                self.setUp()
                self.replies = [{'message': {'content': '前进距离仍然是10米吗？'}, 'done_reason': 'stop'},
                                model_action(name, distance_m=distance)]
                self.assertEqual(self.message('向前飞10米')['status'], 'clarification')
                reply = self.message('改为6米')
                self.assertEqual(reply['status'], expected)
                if expected == 'clarification':
                    self.assertNotIn('card', reply)

    def test_gimbal_direction_cannot_ground_body_yaw(self):
        self.replies = [model_action('turn_right', angle_deg=30)]
        reply = self.message('云台向右转30度，机身向左平移5米')
        self.assertEqual(reply['status'], 'clarification')
        self.assertNotIn('card', reply)

    def test_previous_command_does_not_ground_a_current_opposite_movement(self):
        for name, expected in (('move_right', 'clarification'), ('move_left', 'preview_required')):
            with self.subTest(name=name):
                self.setUp()
                self.replies = [model_action(name, distance_m=5)]
                reply = self.message('上一条指令是向右飞5米，这次向左飞5米')
                self.assertEqual(reply['status'], expected)
                if expected == 'clarification':
                    self.assertNotIn('card', reply)

    def test_english_per_second_is_speed_not_distance(self):
        self.replies = [model_action('move_forward', distance_m=5)]
        reply = self.message('Move forward 5 meters per second')
        self.assertEqual(reply['status'], 'clarification')
        self.assertNotIn('card', reply)
        self.replies = [model_action('set_move_speed', value=5)]
        card = self.message('Set move speed to 5 metres per second')['card']
        self.assertEqual((card['action'], card['parameters']), ('set_move_speed', {'value': 5}))

    def test_filling_the_second_step_does_not_replace_the_first_step_parameter(self):
        for text, question, answer, name, parameters in (
            ('先向前飞10米，再向左飞', '向左飞多少米？', '5米', 'move_forward', {'distance_m': 10}),
            ('先右转90度，再向左转', '向左转多少度？', '45度', 'turn_right', {'angle_deg': 90}),
        ):
            with self.subTest(text=text):
                self.setUp()
                self.replies = [{'message': {'content': question}, 'done_reason': 'stop'},
                                model_action(name, **parameters)]
                self.assertEqual(self.message(text)['status'], 'clarification')
                card = self.message(answer)['card']
                self.assertEqual((card['action'], card['parameters']), (name, parameters))

    def test_parameter_revision_with_half_a_unit_invalidates_the_old_value(self):
        for distance, expected in ((1.5, 'preview_required'), (10, 'clarification')):
            with self.subTest(distance=distance):
                self.setUp()
                self.replies = [{'message': {'content': '前进距离仍然是10米吗？'}, 'done_reason': 'stop'},
                                model_action('move_forward', distance_m=distance)]
                self.message('向前飞10米')
                reply = self.message('改为一米半')
                self.assertEqual(reply['status'], expected)
