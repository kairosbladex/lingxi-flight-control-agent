"""Numeric bounds and alternatives are constraints, not exact flight parameters."""
import json
import unittest

from lingxi_flight_control import MemoryFlightControlSimulator, RuntimeBinding
from lingxi_flight_control.cockpit import CockpitService
from lingxi_flight_control.ollama_decider import OllamaDecider


class CockpitQuantityBoundsTests(unittest.TestCase):
    def propose(self, text, action, parameters):
        binding = RuntimeBinding('actor', 'project', 'hangar', 'drone', 'batch', 1)
        simulator = MemoryFlightControlSimulator(binding, clock=lambda: 1000)
        simulator.state.control_owner = 'actor'
        simulator.state.longitude, simulator.state.latitude = 113, 23
        simulator.state.altitude_m, simulator.state.heading_deg = 50, 90
        before = simulator.state.to_dict()
        calls = []

        def model_http(_url, payload, _timeout):
            calls.append(payload)
            return {'message': {'tool_calls': [{'function': {
                'name': action, 'arguments': parameters}}]}, 'done_reason': 'stop'}

        service = CockpitService(binding, simulator, lambda: OllamaDecider(
            model='synthetic-boundary-evidence', transport=model_http, max_attempts=1),
            clock=lambda: 1000)
        response = service.handle('POST', '/api/flight-control/agent/messages', json.dumps({
            'text': text, 'request_id': 'bounds-1', 'context_version': 1}),
            {'X-Agent-Session': 'bounds-tab'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.payload['dispatched'])
        self.assertEqual(simulator.state.to_dict(), before)
        self.assertLessEqual(len(calls), 1)
        return response.payload

    def test_model_cannot_choose_a_distance_interval_endpoint(self):
        for text, value in (
            ('向前飞5到10米', 10), ('向前飞5米到10米', 5),
            ('向前飞5至10米', 10), ('向前飞5~10米', 10),
            ('向前飞5-10米', 10), ('向前飞五到十米', 10),
            ('Move forward between 5 and 10 metres', 10),
            ('Move forward 5 to 10 meters', 10),
        ):
            with self.subTest(text=text):
                reply = self.propose(text, 'move_forward', {'distance_m': value})
                self.assertEqual(reply['status'], 'clarification')
                self.assertNotIn('card', reply)

    def test_model_cannot_convert_a_distance_bound_to_an_exact_distance(self):
        for text in (
            '向前飞不超过10米', '向前飞最多10米', '向前飞至少10米',
            '向前飞10米以内', '向前飞10米以上',
            'Move forward at most 10 metres', 'Move forward at least 10 metres',
            'Move forward up to 10 metres', 'Move forward less than 10 metres',
        ):
            with self.subTest(text=text):
                reply = self.propose(text, 'move_forward', {'distance_m': 10})
                self.assertEqual(reply['status'], 'clarification')
                self.assertNotIn('card', reply)

    def test_english_approximation_and_chinese_adjacent_number_choices_need_clarification(self):
        for text, value in (
            ('Move forward about 10 metres', 10),
            ('Move forward roughly 10 metres', 10),
            ('Move forward approximately 10 metres', 10),
            ('Move forward 10 metres or so', 10),
            ('向前飞一两米', 2), ('向前飞两三米', 3),
        ):
            with self.subTest(text=text):
                reply = self.propose(text, 'move_forward', {'distance_m': value})
                self.assertEqual(reply['status'], 'clarification')
                self.assertNotIn('card', reply)

    def test_explicit_alternatives_need_the_user_to_choose(self):
        for text in ('向前飞5米或10米', '向前飞5米或者10米',
                     'Move forward 5 or 10 metres'):
            with self.subTest(text=text):
                reply = self.propose(text, 'move_forward', {'distance_m': 10})
                self.assertEqual(reply['status'], 'clarification')
                self.assertNotIn('card', reply)

    def test_angle_and_speed_bounds_do_not_authorize_their_endpoint(self):
        for text, action, parameters in (
            ('右转30到60度', 'turn_right', {'angle_deg': 60}),
            ('右转最多60度', 'turn_right', {'angle_deg': 60}),
            ('水平速度设为不超过5米/秒', 'set_move_speed', {'value': 5}),
            ('Set move speed to between 3 and 5 metres per second', 'set_move_speed', {'value': 5}),
        ):
            with self.subTest(text=text):
                reply = self.propose(text, action, parameters)
                self.assertEqual(reply['status'], 'clarification')
                self.assertNotIn('card', reply)

    def test_an_exact_distance_survives_an_independent_speed_constraint(self):
        for text in ('向前飞10米，速度不超过5米/秒',
                     'Move forward 10 metres, at a speed of at most 5 m/s'):
            with self.subTest(text=text):
                reply = self.propose(text, 'move_forward', {'distance_m': 10})
                self.assertEqual(reply['status'], 'preview_required')
                self.assertEqual(reply['card']['parameters'], {'distance_m': 10})

    def test_bound_cannot_borrow_an_equal_exact_value_from_another_motion(self):
        for text, action, parameters in (
            ('向左飞10米，向右飞最多10米', 'move_right', {'distance_m': 10}),
            ('先右转60度，再左转最多60度', 'turn_left', {'angle_deg': 60}),
            ('Move left 10 metres, then move right at most 10 metres', 'move_right', {'distance_m': 10}),
        ):
            with self.subTest(text=text):
                reply = self.propose(text, action, parameters)
                self.assertEqual(reply['status'], 'clarification')
                self.assertNotIn('card', reply)

    def test_bound_copula_short_approximation_and_unsupported_unit_cannot_ground_distance(self):
        for text, action, parameters in (
            ('向前飞距离上限为12米', 'move_forward', {'distance_m': 12}),
            ('Move right approx. 6 metres', 'move_right', {'distance_m': 6}),
            ('向前飞约5米', 'move_forward', {'distance_m': 5}),
            ('Move forward 1 mile', 'move_forward', {'distance_m': 1}),
        ):
            with self.subTest(text=text):
                reply = self.propose(text, action, parameters)
                self.assertEqual(reply['status'], 'clarification')
                self.assertNotIn('card', reply)

    def test_exact_values_and_explicit_unit_conversions_still_create_cards(self):
        for text, action, parameters in (
            ('向前飞10米', 'move_forward', {'distance_m': 10}),
            ('向前飞一米半', 'move_forward', {'distance_m': 1.5}),
            ('向前飞100厘米', 'move_forward', {'distance_m': 1}),
            ('Move forward 0.01 km', 'move_forward', {'distance_m': 10}),
            ('右转60度', 'turn_right', {'angle_deg': 60}),
            ('水平速度设为5米/秒', 'set_move_speed', {'value': 5}),
        ):
            with self.subTest(text=text):
                reply = self.propose(text, action, parameters)
                self.assertEqual(reply['status'], 'preview_required')
                self.assertEqual(reply['card']['parameters'], parameters)


if __name__ == '__main__':
    unittest.main()
