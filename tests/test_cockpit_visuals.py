"""独立展示路由：真实会话/执行器/HTTP transport，仅外部边界为合成数据。"""
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import urlsplit

from lingxi_flight_control import MemoryFlightControlSimulator, OllamaDecider, RuntimeBinding
from lingxi_flight_control.cockpit import CockpitService
from lingxi_flight_control.ktd_platform import KtdHttpTransport, KtdPlatformExecutor, SqliteCommandJournal
from lingxi_flight_control.ktd_snapshot import KtdSnapshotProvider
from lingxi_flight_control.session import MemoryCheckpointStore
from lingxi_flight_control.standalone_preview import build_standalone_server
from test_ktd_visuals import SIGNED_URL, VisualsHttpBoundary
from test_platform_runtime import FakeJavaOpener


PUBLIC_FIELDS = {'success', 'schema_version', 'execution_mode', 'context_version',
                 'resource_binding', 'generated_at', 'streams', 'trace_id'}


class CombinedBoundary(VisualsHttpBoundary):
    def __init__(self):
        super().__init__()
        self.snapshot = FakeJavaOpener()
        self.entered = self.release = None

    def open(self, request, *, timeout):
        path = urlsplit(request.full_url).path
        if path == '/api/lingxi/agent/platform/snapshot':
            return self.snapshot.open(request, timeout)
        assert path == '/api/lingxi/agent/platform/visuals' and request.get_method() == 'GET'
        if self.entered is not None:
            self.entered.set()
            if not self.release.wait(3): raise TimeoutError('synthetic test barrier')
        return super().open(request, timeout=timeout)


class CockpitVisualsTests(unittest.TestCase):
    def setUp(self):
        from lingxi_flight_control.ktd_visuals import KtdVisualsProvider
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.binding = RuntimeBinding('actor-1', 'project-1', 'hangar-1', 'drone-1', 'batch-1', 7)
        self.boundary = CombinedBoundary()
        self.transport = KtdHttpTransport('https://platform.invalid/api', token='synthetic-platform-key',
            actor_id='actor-1', opener=self.boundary)
        self.provider = KtdVisualsProvider(self.binding, site_id='site-1', task_id=None,
            transport=self.transport, allowed_origins=('https://media.example',), clock=lambda: 1000)
        self.executor = KtdPlatformExecutor(self.binding, site_id='site-1', task_id=None,
            transport=self.transport, snapshot_provider=KtdSnapshotProvider(self.binding,
                site_id='site-1', task_id=None, transport=self.transport, clock=lambda: 1000),
            journal=SqliteCommandJournal(Path(self.directory.name) / 'commands.sqlite3'), clock=lambda: 1000)
        self.model_inputs = []
        def model_transport(url, payload, timeout):
            self.model_inputs.append(payload)
            return {'done_reason': 'stop', 'message': {'tool_calls': [
                {'function': {'name': 'move_forward', 'arguments': {'distance_m': 10}}}]}}
        self.factory = lambda: OllamaDecider(model='synthetic', base_url='http://model.invalid',
                                            transport=model_transport, max_attempts=1)
        self.store = MemoryCheckpointStore(clock=lambda: 1000)
        self.service = CockpitService(self.binding, self.executor, self.factory, store=self.store,
            gateway_token='synthetic-gateway-key', clock=lambda: 1000, visuals_provider=self.provider)
        self.headers = {'X-Agent-Gateway': 'synthetic-gateway-key', 'X-Agent-Actor': 'actor-1',
                        'X-Agent-Session': 'visuals-tab'}

    def get(self, query='context_version=7', body='', headers=None):
        return self.service.handle('GET', '/api/flight-control/visuals?' + query, body,
                                   self.headers if headers is None else headers)

    def test_media_stays_out_of_state_model_checkpoint_and_logs(self):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            response = self.get()
            self.assertEqual(response.status_code, 200, response.payload)
            self.assertEqual(set(response.payload), PUBLIC_FIELDS)
            self.assertEqual(response.payload['schema_version'], 'flight-visuals/v1')
            self.assertEqual(response.payload['streams'][2]['sources'][0]['url'], SIGNED_URL)
            context = self.service.handle('GET', '/api/flight-control/context', '', self.headers)
            reply = self.service.handle('POST', '/api/flight-control/agent/messages', json.dumps({
                'text': '向前飞10米', 'request_id': 'visuals-prompt-isolation', 'context_version': 7}), self.headers)
        self.assertEqual(reply.payload['status'], 'rejected')
        self.assertEqual(reply.payload['error_code'], 'PLATFORM_DISABLED')
        checkpoint = self.store.load('visuals-tab').to_dict()
        for value in (context.payload, reply.payload, self.model_inputs, checkpoint, out.getvalue(), err.getvalue()):
            self.assertNotIn('SYNTHETIC_MEDIA_SECRET', json.dumps(value))
            self.assertNotIn('media.example', json.dumps(value))
        self.assertEqual(len(context.payload['tools']), 65)
        self.assertEqual(len(self.boundary.calls), 1)
        self.assertFalse(any(method == 'POST' for method, *_ in self.boundary.snapshot.calls))

    def test_gateway_actor_session_context_and_exact_query_are_checked_before_java(self):
        for headers, code in (({}, 'UNAUTHENTICATED'),
            ({**self.headers, 'X-Agent-Actor': 'other'}, 'FORBIDDEN'),
            ({**self.headers, 'X-Agent-Session': 'bad/session'}, 'INVALID_SESSION')):
            with self.subTest(code=code): self.assertEqual(self.get(headers=headers).payload['error_code'], code)
        for query in ('', 'context_version=7&context_version=7', 'context_version=true',
                      'context_version=7.0', 'context_version=7&drone_id=other',
                      'context_version=9007199254740992',
                      'context_version=7&url=https%3A%2F%2Fmedia.example', 'context_version=7#secret'):
            with self.subTest(query=query): self.assertEqual(self.get(query).payload['error_code'], 'INVALID_REQUEST')
        self.assertEqual(self.get('context_version=8').payload['error_code'], 'STALE_CONTEXT')
        for body in ('{}', ' ', b'{}'):
            with self.subTest(body=body): self.assertEqual(self.get(body=body).payload['error_code'], 'INVALID_REQUEST')
        self.assertEqual(self.boundary.calls, [])

    def test_unavailable_provider_and_simulator_do_not_invent_visuals(self):
        for executor in (self.executor, MemoryFlightControlSimulator(self.binding)):
            self.service = CockpitService(self.binding, executor, self.factory,
                gateway_token='synthetic-gateway-key', clock=lambda: 1000)
            response = self.get()
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.payload['error_code'], 'VISUALS_UNAVAILABLE')
            self.assertNotIn('streams', response.payload)
        self.assertEqual(self.boundary.calls, [])

    def test_wrong_provider_resource_binding_is_rejected_before_http(self):
        from dataclasses import replace
        from lingxi_flight_control.ktd_visuals import KtdVisualsProvider
        mismatched = KtdVisualsProvider(replace(self.binding, hangar_id='other-hangar'),
            site_id='site-1', task_id=None, transport=self.transport, clock=lambda: 1000)
        self.service = CockpitService(self.binding, self.executor, self.factory,
            gateway_token='synthetic-gateway-key', clock=lambda: 1000, visuals_provider=mismatched)
        self.assertEqual(self.get().payload['error_code'], 'VISUALS_BINDING_MISMATCH')
        self.assertEqual(self.boundary.calls, [])

    def test_failure_does_not_return_or_persist_the_previous_url(self):
        self.assertEqual(self.get().status_code, 200)
        self.boundary.failure = TimeoutError('SYNTHETIC_MEDIA_SECRET')
        response = self.get()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.payload['error_code'], 'VISUALS_TRANSPORT_ERROR')
        self.assertNotIn('streams', response.payload)
        self.assertNotIn('SYNTHETIC_MEDIA_SECRET', json.dumps(response.payload))
        self.assertEqual(self.store.list_sessions(), ())
        self.boundary.failure = None
        self.boundary.packet = {'code': 403, 'success': False, 'message': 'SYNTHETIC_MEDIA_SECRET'}
        response = self.get()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.payload['error_code'], 'VISUALS_FORBIDDEN')
        self.assertNotIn('SYNTHETIC_MEDIA_SECRET', json.dumps(response.payload))

    def test_reset_during_read_discards_late_media_description_without_waiting(self):
        self.boundary.entered, self.boundary.release = threading.Event(), threading.Event()
        results = []
        worker = threading.Thread(target=lambda: results.append(self.get()))
        worker.start()
        self.assertTrue(self.boundary.entered.wait(1))
        try:
            reset = self.service.handle('POST', '/api/flight-control/agent/reset',
                '{"context_version":7}', self.headers)
            self.assertEqual(reset.payload['status'], 'cancelled')
            self.assertTrue(worker.is_alive())
        finally:
            self.boundary.release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0].status_code, 409)
        self.assertEqual(results[0].payload['error_code'], 'STALE_CONTEXT')
        self.assertNotIn('SYNTHETIC_MEDIA_SECRET', json.dumps(results[0].payload))

    def test_other_session_reset_cannot_cancel_this_sessions_display_read(self):
        self.boundary.entered, self.boundary.release = threading.Event(), threading.Event()
        results = []
        worker = threading.Thread(target=lambda: results.append(self.get()))
        worker.start()
        self.assertTrue(self.boundary.entered.wait(1))
        try:
            reset = self.service.handle('POST', '/api/flight-control/agent/reset',
                '{"context_version":7}', {**self.headers, 'X-Agent-Session': 'other-tab'})
            self.assertEqual(reset.payload['status'], 'cancelled')
        finally:
            self.boundary.release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0].status_code, 200)
        self.assertEqual(set(results[0].payload), PUBLIC_FIELDS)

    def test_http_visuals_success_and_failures_are_no_store_without_changing_context_route(self):
        server = build_standalone_server(self.service, '127.0.0.1', 0)
        class SocketBoundary:
            def __init__(self, request):
                self.request, self.output = request, bytearray()
            def makefile(self, *args): return BytesIO(self.request)
            def sendall(self, data): self.output.extend(data)

        def exchange(path, headers):
            raw = ('GET /api/flight-control' + path + ' HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n'
                + ''.join(f'{key}: {value}\r\n' for key, value in headers.items()) + '\r\n').encode()
            connection = SocketBoundary(raw)
            # Exercise the actual HTTP parser/dispatcher/serializer while the
            # repository's offline guard forbids all real socket connections.
            server.RequestHandlerClass(connection, ('127.0.0.1', 0), server)
            head, body = bytes(connection.output).split(b'\r\n\r\n', 1)
            lines = head.decode().split('\r\n')
            response_headers = dict(line.split(': ', 1) for line in lines[1:])
            return int(lines[0].split()[1]), response_headers, json.loads(body)

        try:
            for path, cache in (('/visuals?context_version=7', 'no-store'), ('/context', None)):
                code, headers, payload = exchange(path, self.headers)
                self.assertEqual(code, 200)
                self.assertEqual(headers.get('Cache-Control'), cache)
                self.assertTrue(payload['success'])
            code, headers, payload = exchange('/visuals?context_version=7', {**self.headers, 'X-Agent-Actor': 'other'})
            self.assertEqual(code, 403)
            self.assertEqual(headers.get('Cache-Control'), 'no-store')
            self.assertEqual(payload['error_code'], 'FORBIDDEN')
        finally:
            server.server_close()


class VisualsRuntimeTests(unittest.TestCase):
    def test_runtime_wires_independent_visuals_policy_without_enabling_control_or_model(self):
        from dataclasses import replace
        from lingxi_flight_control.platform_runtime import PlatformRuntimeConfig, build_platform_cockpit
        with tempfile.TemporaryDirectory() as directory:
            binding = RuntimeBinding('actor-1', 'project-1', 'hangar-1', 'drone-1', 'batch-1', 7)
            config = PlatformRuntimeConfig(base_url='https://platform.invalid/api', binding=binding,
                site_id='site-1', task_id=None, journal_path=Path(directory) / 'commands.sqlite3',
                server_token='synthetic-platform-key', gateway_token='synthetic-gateway-key')
            boundary = CombinedBoundary()
            def model_factory(): raise AssertionError('visuals must not invoke the model')
            headers = {'X-Agent-Gateway': config.gateway_token, 'X-Agent-Actor': binding.actor_id,
                       'X-Agent-Session': 'runtime-tab'}
            service = build_platform_cockpit(config, model_factory, opener=boundary, clock=lambda: 1000)
            result = service.handle('GET', '/api/flight-control/visuals?context_version=7', '', headers)
            self.assertEqual(result.status_code, 200, result.payload)
            self.assertFalse(result.payload['streams'][2]['available'])
            self.assertNotIn('SYNTHETIC_MEDIA_SECRET', json.dumps(result.payload))
            allowed = build_platform_cockpit(replace(config, media_allowed_origins=('https://media.example',)),
                model_factory, opener=boundary, clock=lambda: 1000)
            result = allowed.handle('GET', '/api/flight-control/visuals?context_version=7', '', headers)
            self.assertTrue(result.payload['streams'][2]['available'])
            self.assertEqual(boundary.snapshot.calls, [])
            self.assertTrue(all(request.get_method() == 'GET' for request, _ in boundary.calls))


if __name__ == '__main__':
    unittest.main()
