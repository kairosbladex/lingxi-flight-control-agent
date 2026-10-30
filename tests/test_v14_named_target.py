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
    TARGET_NOT_FOUND, TARGET_RESOLVER_UNAVAILABLE, TargetCandidate, TargetGeometry,
    TargetResolverError,
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
        self.assertEqual(reply['card']['target']['reference_altitude_m'], 45.0)
        self.assertTrue(any('不接收半径参数' in warning for warning in reply['card']['warnings']))
        self.assertEqual(len(resolver.calls), 1)
        self.assertEqual(self.brain_calls, [])

    def test_unique_flight_fills_parameters_without_model_and_hides_altitude_from_model(self):
        resolver = RecordingResolver([point()])
        service = self.make_service(
            resolver,
            ModelDecision(DecisionKind.FINAL, message='不应调用'))
        reply = self.message(service, '去唯一水塔，50米高度')
        self.assertEqual(reply['status'], 'preview_required')
        self.assertEqual(reply['card']['action'], 'point_flight')
        self.assertEqual(reply['card']['parameters'], {
            'longitude': 113.201, 'latitude': 23.101, 'altitude_m': 50})
        self.assertEqual(reply['card']['target']['reference_altitude_m'], 45.0)
        self.assertEqual(self.brain_calls, [])

    def test_ambiguous_target_never_calls_model(self):
        resolver = RecordingResolver([point('target-1', '1号水塔'), point('target-2', '1号水塔', 113.209, 23.109, 48, 'v2')])
        service = self.make_service(resolver, ModelDecision(DecisionKind.FINAL, message='不应调用'))
        reply = self.message(service, '围绕1号水塔绕一圈，半径30米')
        self.assertEqual((reply['status'], reply['error_code']), ('clarification', 'TARGET_AMBIGUOUS'))
        self.assertIn('经度113.201', reply['message'])
        self.assertEqual(self.brain_calls, [])

    def test_multi_target_is_rejected_only_in_single_action_mode(self):
        resolver = RecordingResolver([point(name='A塔')])
        service = self.make_service(resolver, ModelDecision(DecisionKind.FINAL, message='不应调用'))
        reply = self.message(service, '去A塔，再去B塔')
        self.assertEqual((reply['status'], reply['error_code']),
                         ('rejected', 'MULTI_TARGET_UNSUPPORTED'))
        self.assertEqual(resolver.calls, [])
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

    def test_not_found_falls_back_to_the_existing_model_path(self):
        resolver = RecordingResolver([])
        service = self.make_service(resolver, ModelDecision(DecisionKind.CLARIFY, message='请提供坐标'))
        named = self.message(service, '去不存在的塔，高50米')
        named_calls = len(self.brain_calls)

        fallback = self.make_service(None, ModelDecision(DecisionKind.CLARIFY, message='请提供坐标'))
        old = self.message(fallback, '去不存在的塔，高50米')
        self.assertEqual(named['status'], old['status'])
        self.assertEqual(named.get('error_code'), old.get('error_code'))
        self.assertEqual(named_calls, 1)
        self.assertEqual(len(self.brain_calls), 1)
        self.assertEqual(len(resolver.calls), 1)

    def test_resolver_unavailable_still_clarifies_without_model_guess(self):
        resolver = RecordingResolver(error=RuntimeError('private detail'))
        service = self.make_service(resolver, ModelDecision(DecisionKind.FINAL, message='不应调用'))
        reply = self.message(service, '去不存在的塔，高50米')
        self.assertEqual((reply['status'], reply['error_code']),
                         ('clarification', TARGET_RESOLVER_UNAVAILABLE))
        self.assertEqual(self.brain_calls, [])

    def test_non_target_phrases_match_resolver_disabled_path(self):
        phrases = ('去拍张照', '绕过前面的障碍物', '绕过障碍物', '去1号水塔拍照', '去吧',
                   '飞到上面去', '前往下一个航点')
        for text in phrases:
            with self.subTest(text=text):
                resolver = RecordingResolver([point(name='1号水塔')])
                named = self.make_service(resolver, ModelDecision(DecisionKind.CLARIFY, message='请重新说明'))
                named_reply = self.message(named, text)
                named_calls = len(self.brain_calls)
                fallback = self.make_service(None, ModelDecision(DecisionKind.CLARIFY, message='请重新说明'))
                fallback_reply = self.message(fallback, text)
                self.assertEqual(named_reply['status'], fallback_reply['status'])
                self.assertEqual(named_reply.get('error_code'), fallback_reply.get('error_code'))
                self.assertEqual(named_calls, 1)
                self.assertEqual(len(self.brain_calls), 1)
                self.assertEqual(resolver.calls, [])

    def test_power_station_names_with_action_characters_inside_are_resolved(self):
        cases = (
            ('去1号升压站，高50米', '1号升压站', 'point_flight',
             {'longitude': 113.31, 'latitude': 23.21, 'altitude_m': 50}),
            ('围绕2号降压站绕一圈，半径30米', '2号降压站', 'point_orbit',
             {'longitude': 113.31, 'latitude': 23.21, 'radius_m': 30}),
            ('去转运站，高40米', '转运站', 'point_flight',
             {'longitude': 113.31, 'latitude': 23.21, 'altitude_m': 40}),
            ('绕飞来峰绕一圈，半径50米', '飞来峰', 'point_orbit',
             {'longitude': 113.31, 'latitude': 23.21, 'radius_m': 50}),
        )
        for text, name, action, arguments in cases:
            with self.subTest(text=text):
                resolver = RecordingResolver([point(target_id='station-1', name=name,
                                                    longitude=113.31, latitude=23.21)])
                service = self.make_service(
                    resolver, ModelDecision(DecisionKind.TOOL_CALL, tool_call=ToolCall(action, arguments)))
                reply = self.message(service, text)
                self.assertEqual([query.name for query in resolver.calls], [name])
                self.assertEqual(reply['status'], 'preview_required')
                self.assertEqual(reply['card']['target']['target_id'], 'station-1')
                self.assertEqual(reply['card']['requested_parameters'], arguments)

    def test_deterministic_coordinates_use_resolved_target(self):
        resolver = RecordingResolver([point()])
        service = self.make_service(
            resolver,
            ModelDecision(DecisionKind.TOOL_CALL,
                          tool_call=ToolCall('point_flight', {
                              'longitude': 113.999, 'latitude': 23.101, 'altitude_m': 50})))
        reply = self.message(service, '去唯一水塔，高50米')
        self.assertEqual(reply['status'], 'preview_required')
        self.assertEqual(reply['card']['parameters']['longitude'], 113.201)
        self.assertEqual(self.brain_calls, [])

    def test_missing_radius_or_flight_height_is_a_clarification(self):
        cases = (
            ('围绕唯一水塔', ToolCall('point_orbit', {'longitude': 113.201, 'latitude': 23.101}), '环绕半径'),
            ('去唯一水塔', ToolCall('point_flight', {'longitude': 113.201, 'latitude': 23.101}), '飞行高度'),
        )
        for text, call, expected in cases:
            with self.subTest(text=text):
                resolver = RecordingResolver([point()])
                service = self.make_service(resolver, ModelDecision(DecisionKind.TOOL_CALL, tool_call=call))
                reply = self.message(service, text)
                self.assertEqual(reply['status'], 'clarification')
                self.assertIn(expected, reply['message'])
                self.assertEqual(self.brain_calls, [])

    def test_nearby_target_phrase_falls_back_to_model(self):
        resolver = RecordingResolver([point()])
        service = self.make_service(resolver, ModelDecision(DecisionKind.CLARIFY, message='请提供坐标'))
        reply = self.message(service, '去唯一水塔附近看看')
        self.assertEqual(reply['status'], 'clarification')
        self.assertEqual(len(self.brain_calls), 1)
        self.assertEqual(resolver.calls, [])

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

    def test_confirm_revalidation_distinguishes_service_unavailable_and_deleted_target(self):
        for error, expected_code, expected_message in (
                (TargetResolverError(TARGET_RESOLVER_UNAVAILABLE), TARGET_RESOLVER_UNAVAILABLE,
                 '暂时无法核验静态标注，未执行。'),
                (TargetResolverError(TARGET_NOT_FOUND), TARGET_NOT_FOUND,
                 '静态标注已删除或无法找到，未执行。')):
            with self.subTest(expected_code=expected_code):
                target = point()
                resolver = RecordingResolver([target])
                service = self.make_service(
                    resolver,
                    ModelDecision(DecisionKind.TOOL_CALL,
                                  tool_call=ToolCall('point_orbit', {
                                      'longitude': 113.201, 'latitude': 23.101, 'radius_m': 30})))
                reply = self.message(service, '围绕唯一水塔绕一圈，半径30米')
                card = reply['card']
                resolver.error = error
                payload = {key: card[key] for key in (
                    'confirmation_id', 'confirmation_binding', 'command_id', 'idempotency_key',
                    'context_version')}
                confirmed = service.handle('POST', '/api/flight-control/agent/confirm',
                                           json.dumps(payload),
                                           {'X-Agent-Session': 'named-target-tab'})
                self.assertEqual((confirmed.payload['status'], confirmed.payload['error_code']),
                                 ('rejected', expected_code))
                self.assertEqual(confirmed.payload['message'], expected_message)
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

    def test_resolver_disabled_input_matches_v12_shape_for_unresolved_text(self):
        first = []

        class FirstBrain:
            def decide(self, text, checkpoint, tools):
                first.append(text)
                return ModelDecision(DecisionKind.CLARIFY, message='请提供坐标')

        service = CockpitService(self.binding, self.simulator, lambda: FirstBrain(), clock=lambda: 1000.0)
        self.message(service, '去拍张照')

        second = []

        class SecondBrain:
            def decide(self, text, checkpoint, tools):
                second.append(text)
                return ModelDecision(DecisionKind.CLARIFY, message='请提供坐标')

        resolver = RecordingResolver([point(name='1号水塔')])
        service = CockpitService(self.binding, self.simulator, lambda: SecondBrain(),
                                 target_resolver=resolver, clock=lambda: 1000.0)
        self.message(service, '去拍张照')
        self.assertEqual(first, second)


if __name__ == '__main__':
    unittest.main()
