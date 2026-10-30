"""V1.4 T8：完整超限指令在模型前解析，仍由策略层统一取极值。"""

import json
import unittest
from unittest.mock import Mock

from lingxi_flight_control import MemoryFlightControlSimulator, RuntimeBinding
from lingxi_flight_control.cockpit import CockpitService
from lingxi_flight_control.flight_policy import CAMERA_ZOOM_MAX_INTERCEPT_MESSAGE


class ParamClampFastPathTests(unittest.TestCase):
    def setUp(self):
        self.binding = RuntimeBinding('actor', 'project', 'hangar', 'drone', 'batch', 1)
        self.simulator = MemoryFlightControlSimulator(self.binding)
        self.simulator.state.control_owner = 'actor'
        self.model = Mock(side_effect=RuntimeError('超限完整指令不应调用模型'))
        self.service = CockpitService(self.binding, self.simulator, self.model)

    def ask(self, text, request_id):
        return self.service.handle(
            'POST', '/api/flight-control/agent/messages',
            json.dumps({'text': text, 'request_id': request_id, 'context_version': 1}),
            {'X-Agent-Session': 't8'}).payload

    def test_limits_01_to_06_use_one_deterministic_path(self):
        cases = (
            ('向前飞21米', 'move_forward', {'distance_m': 21}, {'distance_m': 20}),
            ('向后飞负5米', 'move_backward', {'distance_m': -5}, {'distance_m': 0.01}),
            ('原地向左转361度', 'turn_left', {'angle_deg': 361}, {'angle_deg': 360}),
            ('起飞到501米高度', 'takeoff', {'altitude_m': 501}, {'altitude_m': 500}),
            ('光学变焦设为31倍', 'camera_zoom', {'value': 31}, {'value': 30}),
            ('水平速度调到每秒51米', 'set_move_speed', {'value': 51}, {'value': 50}),
        )
        for index, (text, action, requested, effective) in enumerate(cases):
            with self.subTest(text=text):
                response = self.ask(text, f'limit-{index}')
                self.assertEqual(response['status'], 'preview_required', response)
                self.assertEqual(response['interpretation_source'], 'deterministic-parameter-clamp')
                self.assertEqual(response['card']['action'], action)
                self.assertEqual(response['card']['requested_parameters'], requested)
                self.assertEqual(response['card']['parameters'], effective)
                self.assertTrue(response['card']['adjustments'])
        self.model.assert_not_called()

    def test_inexact_or_in_range_sentence_keeps_original_model_path(self):
        for index, text in enumerate(('向前飞约21米', '向前飞10米')):
            with self.subTest(text=text):
                response = self.ask(text, f'model-{index}')
                self.assertEqual(response['status'], 'error')
                self.assertEqual(response['error_code'], 'MODEL_ERROR')
        self.assertEqual(self.model.call_count, 2)

    def test_missing_device_limits_are_user_readable_and_do_not_use_intercept_phrase(self):
        self.simulator.state.device_limits = {}
        response = self.ask('光学变焦设为31倍', 'missing-limits')
        self.assertEqual(response['status'], 'rejected')
        self.assertEqual(response['error_code'], 'DEVICE_LIMITS_UNAVAILABLE')
        self.assertIn('暂未获取到当前设备的参数上限', response['message'])
        self.assertNotIn(CAMERA_ZOOM_MAX_INTERCEPT_MESSAGE, json.dumps(response, ensure_ascii=False))
        self.model.assert_not_called()


if __name__ == '__main__':
    unittest.main()
