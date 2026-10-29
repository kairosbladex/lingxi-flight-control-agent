"""V1.4 飞行中静态标注名称解析：只读 resolver + 模型参数 grounding。"""

import json
import unittest
from dataclasses import replace

from lingxi_flight_control import (
    DecisionKind, FlightControlStatus, MemoryFlightControlSimulator, ModelDecision,
    RuntimeBinding, ToolCall,
)
from lingxi_flight_control.cockpit import CockpitService
from lingxi_flight_control.flight_policy import CONTROL_MESSAGE, MAPPING_MESSAGE, ROUTE_MESSAGE
from lingxi_flight_control.target_resolver import (
    TargetCandidate, TargetGeometry, TargetResolverError,
)


class RecordingResolver:
    def __init__(self, candidates=(), error=None):
        self.candidates = list(candidates)
        self.error = error
        self.calls = []

    def resolve(self, query):
        self.calls.append(query)
        if self.error is not None:
            raise self.error
        return [candidate for candidate in self.candidates
                if (query.name is None or candidate.name == query.name)
                and (query.target_id is None or candidate.target_id == query.target_id)]


def point(target_id='target-1', name='唯一水塔', longitude=113.201, latitude=23.101,
          altitude=45.0, version='v1'):
    return TargetCandidate(target_id, name, TargetGeometry.POINT, longitude, latitude,
                           altitude, version, 'fixture', 'project-1')


class NamedTargetServiceTests(unittest.TestCase):
    def setUp(self):
        self.binding = RuntimeBinding('actor-1', 'project-1', 'hangar-1', 'drone-1', 'batch-1', 1)
        self.simulator = MemoryFlightControlSimulator(self.binding, clock=lambda: 1000.0)
        self.simulator.state.control_owner = 'actor-1'
        self.simulator.state.route_id = 'route-1'
        self.simulator.state.route_status = 'paused'
        self.simulator.state.breakpoint_index = 2
        self.counter = 0

    def make_service(self, resolver, decision):
        self.brain_calls = []

        class Brain:
            def decide(inner, text, checkpoint, tools):
                self.brain_calls.append(json.loads(text))
                return decision

        return CockpitService(self.binding, self.simulator, lambda: Brain(),
                              target_resolver=resolver, clock=lambda: 1000.0)

    def message(self, service, text):
        self.counter += 1
        response = service.handle(
            'POST', '/api/flight-control/agent/messages',
            json.dumps({'text': text, 'request_id': f'request-{self.counter}', 'context_version': 1}),
            {'X-Agent-Session': 'named-target-tab'})
        self.assertEqual(response.status_code, 200)
        return response.payload

    def test_unique_orbit_uses_resolved_coordinates_and_adds_target_card(self):
        resolver = RecordingResolver([point()])
        service = self.make_service(
            resolver,
            ModelDecision(DecisionKind.TOOL_CALL,
                          tool_call=ToolCall('point_orbit', {
                              'longitude': 113.201, 'latitude': 23.101, 'radius_m': 30})))
        reply = self.message(service, '围绕唯一水塔绕一圈，半径30米')
        self.assertEqual(reply['status'], 'preview_required')
        self.assertEqual(reply['card']['action'], 'point_orbit')
        self.assertEqual(reply['card']['parameters'], {
            'longitude': 113.201, 'latitude': 23.101, 'radius_m': 30})
        self.assertEqual(reply['card']['target']['target_id'], 'target-1')
        self.assertEqual(reply['card']['target']['version'], 'v1')
        self.assertEqual(len(resolver.calls), 1)
        self.assertEqual(self.brain_calls[0]['resolved_target']['target_id'], 'target-1')

    def test_ambiguous_target_never_calls_model(self):
        resolver = RecordingResolver([point('target-1', '1号水塔'), point('target-2', '1号水塔', 113.209, 23.109, 48, 'v2')])
        service = self.make_service(resolver, ModelDecision(DecisionKind.FINAL, message='不应调用'))
        reply = self.message(service, '围绕1号水塔绕一圈，半径30米')
        self.assertEqual((reply['status'], reply['error_code']), ('clarification', 'TARGET_AMBIGUOUS'))
        self.assertIn('经度113.201', reply['message'])
        self.assertEqual(self.brain_calls, [])

    def test_line_and_volume_are_rejected_before_model(self):
        for candidate, text, message in (
                (TargetCandidate('line-1', 'B河道', TargetGeometry.LINE, 113.3, 23.2, 12, 'v1', 'fixture', 'project-1'),
                 '去B河道', '单点指点飞行或环绕'),
                (TargetCandidate('volume-1', '立体大楼', TargetGeometry.VOLUME, 113.5, 23.4, 60, 'v1', 'fixture', 'project-1'),
                 '去立体大楼', '当前版本不支持立体目标规划')):
            with self.subTest(text=text):
                resolver = RecordingResolver([candidate])
                service = self.make_service(resolver, ModelDecision(DecisionKind.FINAL, message='不应调用'))
                reply = self.message(service, text)
                self.assertEqual(reply['status'], 'rejected')
                self.assertIn(message, reply['message'])
                self.assertEqual(self.brain_calls, [])

    def test_missing_target_and_resolver_failure_do_not_let_model_guess(self):
        for resolver in (RecordingResolver([]), RecordingResolver(error=RuntimeError('private detail'))):
            service = self.make_service(resolver, ModelDecision(DecisionKind.FINAL, message='不应调用'))
            reply = self.message(service, '去不存在的塔，高50米')
            self.assertEqual(reply['status'], 'clarification')
            self.assertEqual(self.brain_calls, [])

    def test_model_coordinates_must_match_resolved_target(self):
        resolver = RecordingResolver([point()])
        service = self.make_service(
            resolver,
            ModelDecision(DecisionKind.TOOL_CALL,
                          tool_call=ToolCall('point_flight', {
                              'longitude': 113.999, 'latitude': 23.101, 'altitude_m': 50})))
        reply = self.message(service, '去唯一水塔，高50米')
        self.assertEqual((reply['status'], reply['error_code']), ('rejected', 'TARGET_BINDING_MISMATCH'))
        self.assertEqual(self.simulator.state.last_action, None)

    def test_missing_radius_or_flight_height_is_a_clarification(self):
        cases = (
            ('围绕唯一水塔绕一圈，半径30米', ToolCall('point_orbit', {'longitude': 113.201, 'latitude': 23.101}), '环绕半径'),
            ('去唯一水塔', ToolCall('point_flight', {'longitude': 113.201, 'latitude': 23.101}), '飞行高度'),
        )
        for text, call, expected in cases:
            with self.subTest(text=text):
                resolver = RecordingResolver([point()])
                service = self.make_service(resolver, ModelDecision(DecisionKind.TOOL_CALL, tool_call=call))
                reply = self.message(service, text)
                self.assertEqual(reply['status'], 'clarification')
                self.assertIn(expected, reply['message'])

    def test_confirm_rechecks_target_version_and_keeps_state_unchanged(self):
        target = point()
        resolver = RecordingResolver([target])
        service = self.make_service(
            resolver,
            ModelDecision(DecisionKind.TOOL_CALL,
                          tool_call=ToolCall('point_orbit', {
                              'longitude': 113.201, 'latitude': 23.101, 'radius_m': 30})))
        reply = self.message(service, '围绕唯一水塔绕一圈，半径30米')
        card = reply['card']
        resolver.candidates = [replace(target, version='v2')]
        payload = {key: card[key] for key in (
            'confirmation_id', 'confirmation_binding', 'command_id', 'idempotency_key', 'context_version')}
        confirmed = service.handle('POST', '/api/flight-control/agent/confirm', json.dumps(payload),
                                   {'X-Agent-Session': 'named-target-tab'})
        self.assertEqual((confirmed.payload['status'], confirmed.payload['error_code']),
                         ('rejected', 'TARGET_VERSION_CHANGED'))
        self.assertIsNone(self.simulator.state.last_action)

    def test_negative_and_conditional_sentences_do_not_call_resolver(self):
        resolver = RecordingResolver([point()])
        service = self.make_service(resolver, ModelDecision(DecisionKind.FINAL, message='不应调用'))
        for text in ('不要去唯一水塔', '如果有风就去唯一水塔'):
            with self.subTest(text=text):
                reply = self.message(service, text)
                self.assertIn(reply['status'], {'final', 'clarification'})
        self.assertEqual(resolver.calls, [])
        self.assertEqual(self.brain_calls, [])

    def test_control_and_route_gates_precede_resolver_and_model(self):
        resolver = RecordingResolver([point()])
        service = self.make_service(resolver, ModelDecision(DecisionKind.FINAL, message='不应调用'))
        self.simulator.state.control_owner = None
        reply = self.message(service, '去唯一水塔，高50米')
        self.assertEqual((reply['status'], reply['error_code']), ('rejected', 'CONTROL_NOT_OWNED'))
        self.assertEqual(resolver.calls, [])
        self.simulator.state.control_owner = 'actor-1'
        self.simulator.state.route_status = 'running'
        reply = self.message(service, '去唯一水塔，高50米')
        self.assertEqual((reply['status'], reply['error_code']), ('rejected', 'ROUTE_PAUSE_REQUIRED'))
        self.assertEqual(resolver.calls, [])
        self.assertEqual(self.brain_calls, [])

    def test_mapping_route_resolves_and_keeps_mapping_warning(self):
        resolver = RecordingResolver([point()])
        self.simulator.state.route_status = 'running'
        self.simulator.state.task_type = 'mapping'
        self.simulator.state.auto_stitching = True
        service = self.make_service(
            resolver,
            ModelDecision(DecisionKind.TOOL_CALL,
                          tool_call=ToolCall('point_orbit', {
                              'longitude': 113.201, 'latitude': 23.101, 'radius_m': 30})))
        reply = self.message(service, '围绕唯一水塔绕一圈，半径30米')
        self.assertEqual(reply['status'], 'preview_required')
        self.assertIn(MAPPING_MESSAGE, reply['card']['warnings'])

    def test_resolver_none_keeps_model_input_without_resolved_target(self):
        brain_inputs = []

        class Brain:
            def decide(self, text, checkpoint, tools):
                brain_inputs.append(json.loads(text))
                return ModelDecision(DecisionKind.CLARIFY, message='请提供坐标')

        service = CockpitService(self.binding, self.simulator, lambda: Brain(), clock=lambda: 1000.0)
        reply = self.message(service, '去唯一水塔')
        self.assertEqual(reply['status'], 'clarification')
        self.assertNotIn('resolved_target', brain_inputs[0])


if __name__ == '__main__':
    unittest.main()
