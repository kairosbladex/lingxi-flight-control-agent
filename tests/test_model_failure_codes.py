"""Model failures at the HTTP boundary; no real network or device is contacted."""
import io
import json
import traceback
import unittest
import urllib.error
from unittest import mock

from lingxi_flight_control import BoundedAgentLoop, MemoryFlightControlSimulator, RuntimeBinding, ToolRegistry
from lingxi_flight_control.cockpit import CockpitService
from lingxi_flight_control.models import AgentCheckpoint
from lingxi_flight_control.ollama_decider import (
    ModelOutputTruncatedError, ModelTimeoutError, OllamaDecider, http_transport,
)
from lingxi_flight_control.session import MemoryCheckpointStore
from lingxi_flight_control.telemetry import MemorySink, Telemetry


PRIVATE_DETAIL = 'fixture-private-upstream-detail'


def tool_response(*, done_reason='stop', distance=10):
    return {'done': True, 'done_reason': done_reason, 'message': {
        'role': 'assistant', 'tool_calls': [{'function': {
            'name': 'move_forward', 'arguments': {'distance_m': distance}}}]}}


def http_response(payload):
    return io.BytesIO(json.dumps(payload).encode('utf-8'))


class ModelFailureTypeTests(unittest.TestCase):
    def test_http_error_preserves_status_without_reading_body_or_exposing_exception_chain(self):
        for status in (404, 503):
            error = urllib.error.HTTPError('http://model.invalid', status, PRIVATE_DETAIL, {},
                                           io.BytesIO(PRIVATE_DETAIL.encode()))
            with self.subTest(status=status), mock.patch('urllib.request.urlopen', side_effect=error), \
                    mock.patch.object(error, 'read') as read_error_body:
                with self.assertRaises(ValueError) as raised:
                    http_transport('http://model.invalid/api/chat', {}, 30)
            self.assertEqual(str(raised.exception), f'model server rejected the request: HTTP {status}')
            self.assertNotIn(PRIVATE_DETAIL, ''.join(traceback.format_exception(raised.exception)))
            read_error_body.assert_not_called()

    def test_http_timeout_is_typed_and_remains_a_value_error(self):
        for cause in (TimeoutError(PRIVATE_DETAIL),
                      urllib.error.URLError(TimeoutError(PRIVATE_DETAIL))):
            with self.subTest(cause=type(cause).__name__), mock.patch(
                    'urllib.request.urlopen', side_effect=cause) as transport:
                with self.assertRaises(ValueError) as raised:
                    http_transport('http://model.invalid/api/chat', {}, 30)
                self.assertIsInstance(raised.exception, ModelTimeoutError)
                self.assertNotIn(PRIVATE_DETAIL, str(raised.exception))
                self.assertEqual(transport.call_count, 1)

    def test_complete_tool_in_truncated_http_response_is_never_a_decision(self):
        response = tool_response(done_reason='length')
        decider = OllamaDecider(model='fixture', base_url='http://model.invalid', max_attempts=3)
        checkpoint = AgentCheckpoint('向前飞10米', {}, [], [], [])
        with mock.patch('urllib.request.urlopen', return_value=http_response(response)) as transport:
            with self.assertRaises(ValueError) as raised:
                decider.decide('向前飞10米', checkpoint, ())
        self.assertIsInstance(raised.exception, ModelOutputTruncatedError)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(decider.empty_responses, 0)
        self.assertEqual(decider.last_generation['done_reason'], 'length')


class LegacyLoopErrorRedactionTests(unittest.TestCase):
    def run_failure(self, failure):
        sink = MemorySink()
        telemetry = Telemetry(sink)
        decider = OllamaDecider(model='fixture', base_url='http://model.invalid',
                                max_attempts=3, telemetry=telemetry)
        effect = failure if isinstance(failure, Exception) else (
            lambda *args, **kwargs: http_response(failure))
        with mock.patch('urllib.request.urlopen', side_effect=effect) as transport:
            result = BoundedAgentLoop(decider, ToolRegistry(), telemetry=telemetry).run('读取当前状态')
        self.assertEqual(result.status.value, 'tool_error')
        self.assertIsNone(result.tool_call)
        self.assertIsNone(result.checkpoint.confirmation_binding)
        self.assertEqual(result.checkpoint.executed_tool_calls or [], [])
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(decider.empty_responses, 0)
        self.assertNotIn(PRIVATE_DETAIL, json.dumps(result.checkpoint.to_dict()))
        self.assertNotIn(PRIVATE_DETAIL, json.dumps(result.error))
        self.assertNotIn(PRIVATE_DETAIL, json.dumps(sink.dicts()))
        self.assertIsNone(decider.last_raw_response)
        return result

    def test_http_error_is_redacted_before_the_legacy_loop_records_it(self):
        error = urllib.error.HTTPError('http://model.invalid', 503, PRIVATE_DETAIL, {},
                                       io.BytesIO(PRIVATE_DETAIL.encode()))
        result = self.run_failure(error)
        self.assertEqual(result.error['message'], 'model server rejected the request: HTTP 503')

    def test_network_failure_reason_is_redacted_before_the_legacy_loop_records_it(self):
        result = self.run_failure(urllib.error.URLError(ConnectionRefusedError(PRIVATE_DETAIL)))
        self.assertEqual(result.error['message'], 'model server is unreachable')

    def test_error_envelope_is_not_saved_as_raw_response_or_legacy_checkpoint(self):
        # Even an envelope containing a plausible tool must remain a failure.
        result = self.run_failure({**tool_response(), 'error': PRIVATE_DETAIL})
        self.assertEqual(result.error['message'], 'model server returned an error response')


class CockpitModelFailureTests(unittest.TestCase):
    def setUp(self):
        self.binding = RuntimeBinding('actor-1', 'project-1', 'hangar-1', 'drone-1', 'batch-1', 1)
        self.simulator = MemoryFlightControlSimulator(self.binding)
        self.simulator.state.control_owner = 'actor-1'
        self.simulator.state.longitude = 113
        self.simulator.state.latitude = 23
        self.simulator.state.altitude_m = 50
        self.simulator.state.heading_deg = 90
        self.initial_state = self.simulator.state.to_dict()
        self.decider = OllamaDecider(model='fixture', base_url='http://model.invalid', max_attempts=3)
        self.configure()

    def configure(self, *, factory=None, **budgets):
        self.store = MemoryCheckpointStore()
        self.service = CockpitService(self.binding, self.simulator,
            factory or (lambda: self.decider), store=self.store, **budgets)

    def message(self, text='向前飞10米'):
        return self.service.handle('POST', '/api/flight-control/agent/messages',
            json.dumps({'text': text, 'request_id': 'r1', 'context_version': 1}),
            {'X-Agent-Session': 'failure-tab', 'X-Trace-Id': 'failure-trace'})

    def assert_failure(self, response, code):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.payload['status'], 'error')
        self.assertEqual(response.payload['error_code'], code)
        self.assertNotIn('card', response.payload)
        self.assertFalse(response.payload['dispatched'])
        self.assertEqual(response.payload['device_effect'], 'not_verified')
        self.assertNotIn(PRIVATE_DETAIL, json.dumps(response.payload))
        self.assertEqual(self.simulator.state.to_dict(), self.initial_state)

    def test_http_timeout_has_stable_code_without_retry_or_detail_leak(self):
        for cause in (TimeoutError(PRIVATE_DETAIL),
                      urllib.error.URLError(TimeoutError(PRIVATE_DETAIL))):
            self.configure()
            with self.subTest(cause=type(cause).__name__), mock.patch(
                    'urllib.request.urlopen', side_effect=cause) as transport:
                result = self.message()
                self.assert_failure(result, 'MODEL_TIMEOUT')
                self.assertEqual(self.message().payload, result.payload)
                self.assertEqual(transport.call_count, 1)
                self.assertNotIn(PRIVATE_DETAIL, json.dumps(
                    self.store.load('failure-tab').checkpoint.to_dict()))

    def test_length_stop_rejects_tools_text_and_empty_output_without_retry(self):
        for message in (tool_response()['message'], {'content': '请说明距离。'},
                        {'content': '', 'thinking': PRIVATE_DETAIL}):
            self.configure()
            with self.subTest(message=message), mock.patch('urllib.request.urlopen',
                    side_effect=lambda *args, **kw: http_response({
                        'done': True, 'done_reason': 'length', 'message': message})) as transport:
                result = self.message()
                self.assert_failure(result, 'MODEL_OUTPUT_TRUNCATED')
                self.assertEqual(self.message().payload, result.payload)
                self.assertEqual(transport.call_count, 1)

    def test_other_http_and_protocol_failures_remain_model_error_and_are_redacted(self):
        failures = (
            urllib.error.HTTPError('http://model.invalid', 500, PRIVATE_DETAIL, {},
                                   io.BytesIO(PRIVATE_DETAIL.encode())),
            urllib.error.URLError(ConnectionRefusedError(PRIVATE_DETAIL)),
            {'error': PRIVATE_DETAIL},
            {'message': {'tool_calls': PRIVATE_DETAIL}},
        )
        for failure in failures:
            self.configure()
            effect = failure if isinstance(failure, Exception) else (
                lambda *args, **kw: http_response(failure))
            with self.subTest(failure_type=type(failure).__name__), mock.patch(
                    'urllib.request.urlopen', side_effect=effect) as transport:
                result = self.message()
                self.assert_failure(result, 'MODEL_ERROR')
                self.assertEqual(self.message().payload, result.payload)
                self.assertEqual(transport.call_count, 1)
                self.assertNotIn(PRIVATE_DETAIL, json.dumps(
                    self.store.load('failure-tab').checkpoint.to_dict()))

    def test_model_initialization_failure_is_stable_on_first_response_and_replay(self):
        calls = []
        def unavailable():
            calls.append(True)
            raise RuntimeError(PRIVATE_DETAIL)
        self.configure(factory=unavailable)
        with mock.patch('urllib.request.urlopen') as transport:
            result = self.message()
            self.assert_failure(result, 'MODEL_ERROR')
            self.assertEqual(self.message().payload, result.payload)
        self.assertEqual(len(calls), 1)
        transport.assert_not_called()

    def test_model_step_exhaustion_is_a_run_budget_failure(self):
        self.configure(max_model_steps=1)
        invalid = tool_response(distance=10)
        invalid['message']['tool_calls'][0]['function']['arguments']['extra'] = 1
        with mock.patch('urllib.request.urlopen',
                        return_value=http_response(invalid)) as transport:
            self.assert_failure(self.message(), 'MODEL_STEP_LIMIT')
        self.assertEqual(transport.call_count, 1)

    def test_late_complete_tool_is_a_run_budget_failure_without_a_card(self):
        self.configure(timeout_sec=1)
        with mock.patch('lingxi_flight_control.cockpit.time.monotonic', return_value=10) as clock:
            def late_response(*args, **kwargs):
                clock.return_value = 12
                return http_response(tool_response())
            with mock.patch('urllib.request.urlopen', side_effect=late_response) as transport:
                self.assert_failure(self.message(), 'RUN_TIMEOUT')
        self.assertEqual(transport.call_count, 1)

    def test_budget_exhausted_before_model_call_does_not_contact_transport(self):
        with mock.patch('lingxi_flight_control.cockpit.time.monotonic', return_value=10) as clock:
            def slow_factory():
                clock.return_value = 12
                return self.decider
            self.configure(factory=slow_factory, timeout_sec=1)
            with mock.patch('urllib.request.urlopen') as transport:
                self.assert_failure(self.message(), 'RUN_TIMEOUT')
        transport.assert_not_called()

    def test_prompt_budget_exhaustion_does_not_contact_transport(self):
        self.decider = OllamaDecider(model='fixture', base_url='http://model.invalid',
                                    num_ctx=32, output_reserve=0)
        with mock.patch('urllib.request.urlopen') as transport:
            self.assert_failure(self.message(), 'MODEL_CONTEXT_LIMIT')
        transport.assert_not_called()

    def test_complete_tool_still_requires_explicit_confirmation(self):
        with mock.patch('urllib.request.urlopen', return_value=http_response(tool_response())) as transport:
            result = self.message()
        self.assertEqual(result.payload['status'], 'preview_required')
        self.assertEqual(result.payload['card']['parameters'], {'distance_m': 10})
        self.assertEqual(self.simulator.state.to_dict(), self.initial_state)
        self.assertEqual(transport.call_count, 1)

    def test_photo_and_short_move_timeouts_never_offer_card_or_retry(self):
        for text, action, arguments in (
                ('拍一张照片', 'camera_photo', {}),
                ('向前飞2米', 'move_forward', {'distance_m': 2})):
            for late in (False, True):
                with self.subTest(text=text, late=late):
                    self.configure(timeout_sec=1)
                    with mock.patch('lingxi_flight_control.cockpit.time.monotonic', return_value=10) as clock:
                        def response(*args, **kwargs):
                            if not late:
                                raise TimeoutError('synthetic timeout')
                            clock.return_value = 12
                            return http_response({'done_reason': 'stop', 'message': {'tool_calls': [
                                {'function': {'name': action, 'arguments': arguments}}]}})
                        with mock.patch('urllib.request.urlopen', side_effect=response) as transport:
                            result = self.message(text)
                            self.assert_failure(result, 'RUN_TIMEOUT' if late else 'MODEL_TIMEOUT')
                            self.assertEqual(self.message(text).payload, result.payload)
                            self.assertEqual(transport.call_count, 1)


if __name__ == '__main__':
    unittest.main()
