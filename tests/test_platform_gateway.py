import json
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from lingxi_flight_control.full_control import RuntimeBinding
from lingxi_flight_control.platform_runtime import PlatformRuntimeConfig
from lingxi_flight_control.platform_gateway import PlatformGateway
from lingxi_flight_control.standalone_preview import StandaloneResponse


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = PlatformRuntimeConfig('http://127.0.0.1:18090',
            RuntimeBinding('a', 'p', 'h', 'd', 't', 1), 's', 't',
            Path(self.temp.name) / 'commands.sqlite', 'synthetic', 'synthetic-gateway-token', True)
        self.built = []
        self.now = 0
        def factory(config, decider, **kwargs):
            self.built.append(config)
            class Service:
                def handle(self, method, path, body, headers):
                    return StandaloneResponse(200, {'actor': config.binding.actor_id,
                        'drone': config.binding.drone_id, 'headers': list(headers)})
            return Service()
        self.gateway = PlatformGateway(self.config, lambda: None, factory=factory,
            clock=lambda: self.now, max_contexts=2, ttl=60)
        self.headers = {'X-Agent-Gateway': self.config.gateway_token, 'X-Agent-Actor': 'a',
            'X-Agent-Platform-Token': 'synthetic-login', 'X-Agent-Session': 'session-1',
            'X-Agent-Context': json.dumps(dict(actor_id='a', project_id='p', site_id='s',
                                             hangar_id='h', drone_id='d', task_id='t'))}

    def call(self, headers=None):
        return self.gateway.handle('GET', '/api/flight-control/context', headers=headers or self.headers)

    def test_gateway_authentication_precedes_runtime_creation(self):
        self.assertEqual(self.call({'X-Agent-Gateway': 'wrong'}).status_code, 401)
        self.assertEqual(self.built, [])

    def test_scope_and_login_are_bound_and_secrets_not_forwarded_to_cockpit(self):
        self.assertEqual(self.call().status_code, 200)
        self.call()
        self.assertEqual(len(self.built), 1)
        self.assertEqual(self.built[0].server_token, 'synthetic-login')
        self.assertEqual(self.built[0].task_id, 't')
        self.assertNotIn('synthetic-login', str(self.built[0].journal_path))
        self.assertNotIn('x-agent-platform-token', self.call().payload['headers'])
        self.headers['X-Agent-Platform-Token'] = 'different-login'
        self.call()
        self.assertEqual(len(self.built), 2)

    def test_foreign_actor_and_malformed_scope_never_build(self):
        self.headers['X-Agent-Actor'] = 'other'
        self.assertEqual(self.call().status_code, 403)
        self.headers['X-Agent-Context'] = '{}'
        self.assertEqual(self.call().status_code, 400)
        self.assertEqual(self.built, [])

    def test_bounded_contexts_expire_without_replaying_commands(self):
        self.call()
        self.headers['X-Agent-Platform-Token'] = 'second'
        self.call()
        self.headers['X-Agent-Platform-Token'] = 'third'
        self.assertEqual(self.call().status_code, 503)
        self.now = 61
        self.assertEqual(self.call().status_code, 200)
        self.assertEqual(len(self.gateway.contexts), 1)

    def test_legacy_downgrade_is_rejected(self):
        with self.assertRaises(ValueError):
            PlatformGateway(replace(self.config, legacy_read_api=True), lambda: None)

    def test_real_runtime_uses_snapshot_and_refuses_write_when_disabled(self):
        from test_platform_runtime import FakeJavaOpener
        from lingxi_flight_control.platform_runtime import build_platform_cockpit
        from lingxi_flight_control import OllamaDecider
        opener = FakeJavaOpener()
        opener.data['task_id'] = 'task-1'
        scope = dict(actor_id='actor-1', project_id='project-1', site_id='site-1',
                     hangar_id='hangar-1', drone_id='drone-1', task_id='task-1')
        headers = {**self.headers, 'X-Agent-Context': json.dumps(scope), 'X-Agent-Actor': 'actor-1'}
        def model_transport(*args):
            return {'done_reason': 'stop', 'message': {'tool_calls': [
                {'function': {'name': 'move_forward', 'arguments': {'distance_m': 10}}}]}}
        gateway = PlatformGateway(self.config,
            lambda: OllamaDecider(model='fixture', base_url='http://model.invalid', transport=model_transport, max_attempts=1),
            factory=lambda config, decider, **kw: build_platform_cockpit(config, decider,
                opener=opener, clock=lambda: 1000, **kw))
        context = gateway.handle('GET', '/api/flight-control/context', headers=headers)
        self.assertEqual(context.payload['context']['task_id'], 'task-1')
        self.assertEqual(context.payload['execution_mode'], 'platform')
        result = gateway.handle('POST', '/api/flight-control/agent/messages', json.dumps({
            'text': '向前飞10米', 'request_id': 'message', 'context_version': 1}), headers)
        self.assertEqual(result.payload['error_code'], 'PLATFORM_DISABLED')
        self.assertTrue(opener.calls)
        self.assertTrue(all(call[0] == 'GET' for call in opener.calls))
