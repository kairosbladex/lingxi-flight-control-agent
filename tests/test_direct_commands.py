"""Exact commands may skip the model, never authorization or confirmation."""
import json
import unittest
from unittest.mock import Mock

from lingxi_flight_control import MemoryFlightControlSimulator, RuntimeBinding
from lingxi_flight_control.cockpit import CockpitService
import test_browser_gateway


class DirectCommandTests(unittest.TestCase):
    def setUp(self):
        self.binding = RuntimeBinding('actor', 'project', 'hangar', 'drone', 'batch', 1)
        self.sim = MemoryFlightControlSimulator(self.binding)
        self.sim.state.control_owner = 'actor'
        self.factory = Mock(side_effect=RuntimeError('model unavailable'))
        self.service = CockpitService(self.binding, self.sim, self.factory, enable_direct_commands=True)

    def ask(self, text, index=0):
        return self.service.handle('POST', '/api/flight-control/agent/messages', json.dumps({
            'text': text, 'request_id': f'r{index}', 'context_version': 1}),
            {'X-Agent-Session': 'direct'}).payload

    def test_exact_commands_have_correct_cards_without_model_or_execution(self):
        cases = [('拍一张照片', 'camera_photo', {}), ('帮我拍张照', 'camera_photo', {}),
            ('拍照', 'camera_photo', {}), ('向前飞2米', 'move_forward', {'distance_m': 2}),
            ('往后退两米', 'move_backward', {'distance_m': 2}),
            ('向右平移2.5米', 'move_right', {'distance_m': 2.5}),
            ('向左平移半米', 'move_left', {'distance_m': .5}),
            ('向前飞十二米', 'move_forward', {'distance_m': 12}),
            ('请向后飞20米。', 'move_backward', {'distance_m': 20}),
            ('向前飞零点五米', 'move_forward', {'distance_m': .5}),
            ('向上飞2米', 'move_up', {'distance_m': 2}),
            ('向下飞2米', 'move_down', {'distance_m': 2})]
        before = self.sim.state.to_dict()
        for i, (text, action, params) in enumerate(cases):
            with self.subTest(text=text):
                result = self.ask(text, i)
                self.assertEqual(result['status'], 'preview_required', result)
                self.assertEqual(result['card']['action'], action)
                self.assertEqual(result['card']['parameters'], params)
                self.assertEqual(result['interpretation_source'], 'exact-command-v1')
                self.assertFalse(result['dispatched'])
                self.assertEqual(self.ask(text, i), result)
        self.factory.assert_not_called()
        self.assertEqual(before, self.sim.state.to_dict())

    def test_non_exact_commands_still_use_original_policy_or_model(self):
        cases = ['不要拍照', '如果到达后拍照', '拍一张照片？', '“拍一张照片”',
            '请问能拍照吗', '向前飞2米然后拍照', '向前飞', '向前飞约2米',
            '向前飞2到3米',
            '向前飞2米，速度1米/秒', '向前飞一二米', '向前飞零二米',
            '向前飞两十米', '向前飞2公里', '云台向前飞2米',
            '向左转2度', '按之前的方向飞2米', '向前飞2米不用确认', '继续']
        for i, text in enumerate(cases):
            with self.subTest(text=text):
                result = self.ask(text, i)
                self.assertNotIn('card', result)
                self.assertFalse(result['dispatched'])

    def test_disabled_by_default(self):
        self.service = CockpitService(self.binding, self.sim, self.factory)
        self.assertEqual(self.ask('拍一张照片')['error_code'], 'MODEL_ERROR')
        self.factory.assert_called_once()

    def test_missing_control_cannot_be_bypassed(self):
        self.sim.state.control_owner = None
        result = self.ask('向前飞2米')
        self.assertNotIn('card', result)
        self.assertFalse(result['dispatched'])
        self.factory.assert_not_called()

    def test_parser_boundary_matrix(self):
        from lingxi_flight_control.cockpit import _direct_command
        for motion, action in [('向前飞', 'move_forward'), ('向后飞', 'move_backward'),
                               ('向左平移', 'move_left'), ('向右平移', 'move_right')]:
            for number, value in [('0.1', .1), ('半', .5), ('两', 2), ('十', 10),
                                  ('十一', 11), ('十九点九', 19.9), ('二十', 20)]:
                with self.subTest(motion=motion, number=number):
                    call = _direct_command(motion + number + '米')
                    self.assertEqual(call.name, action)
                    self.assertEqual(call.arguments, {'distance_m': value})
            for number in ['一二', '零二', 'NaN', '1e1', '2～3', '两十']:
                self.assertIsNone(_direct_command(motion + number + '米'))

    def test_fast_photo_does_not_inherit_prior_motion_clarification(self):
        session = self.service._session('direct')
        session.active_intent, session.clarification = '向前飞', True
        session.motion_source = 'move_forward'
        result = self.ask('拍一张照片')
        self.assertEqual(result['card']['action'], 'camera_photo')
        self.assertEqual(session.active_intent, '拍一张照片')
        self.assertFalse(result['dispatched'])
        self.factory.assert_not_called()

    def test_stale_context_rejected_without_model(self):
        result = self.service.handle('POST', '/api/flight-control/agent/messages', json.dumps({
            'text': '拍一张照片', 'request_id': 'stale', 'context_version': 2}),
            {'X-Agent-Session': 'direct'})
        self.assertEqual(result.status_code, 409)
        self.factory.assert_not_called()

    def test_platform_binding_confirmation_and_exactly_once_still_apply(self):
        from lingxi_flight_control.browser_gateway import BrowserGateway
        fixture = test_browser_gateway.BrowserGatewayTests()
        fixture.setUp()
        try:
            fixture.gateway = BrowserGateway(fixture.gateway.config, self.factory,
                enable_control=True, enable_direct_commands=True, opener=fixture.java,
                clock=lambda: 1000, allowed_drone_sns=['uav-sn-1'])
            result = fixture.call('/agent/messages', {'text': '向前飞2米',
                'request_id': 'fast', 'context_version': 1}).payload
            self.assertEqual(result['card']['parameters'], {'distance_m': 2})
            self.assertEqual(fixture.java.posts, [])
            body = fixture.confirmation(result['card'])
            first = fixture.call('/agent/confirm', body)
            self.assertEqual(first.payload['status'], 'submitted')
            fixture.call('/agent/confirm', body)
            self.assertEqual(len(fixture.java.posts), 1)
            self.factory.assert_not_called()
        finally:
            fixture.doCleanups()

    def test_platform_task_change_after_preview_blocks_confirmation(self):
        from lingxi_flight_control.browser_gateway import BrowserGateway
        fixture = test_browser_gateway.BrowserGatewayTests()
        fixture.setUp()
        try:
            fixture.gateway = BrowserGateway(fixture.gateway.config, self.factory,
                enable_control=True, enable_direct_commands=True, opener=fixture.java,
                clock=lambda: 1000, allowed_drone_sns=['uav-sn-1'])
            result = fixture.proposal().payload
            self.assertEqual(result['card']['action'], 'camera_photo')
            fixture.java.source.responses['task'].payload['data']['taskFlag'] = '3'
            self.assertEqual(fixture.call('/agent/confirm', fixture.confirmation(result['card'])).status_code, 403)
            self.assertEqual(fixture.java.posts, [])
            self.factory.assert_not_called()
        finally:
            fixture.doCleanups()


if __name__ == '__main__':
    unittest.main()
