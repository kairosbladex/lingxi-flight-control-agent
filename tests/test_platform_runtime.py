"""平台启动装配：只使用合成配置、临时日志和网络边界夹具。"""

from pathlib import Path
from io import BytesIO
import json
import tempfile
import unittest
from urllib.parse import urlsplit

from lingxi_flight_control.platform_runtime import PlatformRuntimeConfig, build_platform_cockpit
from lingxi_flight_control.ollama_decider import OllamaDecider
from test_command_receipts import command_receipt_payload


class PlatformRuntimeConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.environment = {
            'KTD_BASE_URL': 'https://platform.invalid/api',
            'KTD_SERVER_TOKEN': 'synthetic-server-credential',
            'KTD_ACTOR_EMPLOYEE_ID': 'actor-1',
            'KTD_PROJECT_ID': 'project-1',
            'KTD_SITE_ID': 'site-1',
            'KTD_HANGAR_ID': 'hangar-1',
            'KTD_DRONE_ID': 'drone-1',
            'FLIGHT_AGENT_TASK_BATCH_ID': 'batch-1',
            'FLIGHT_AGENT_GATEWAY_TOKEN': 'synthetic-gateway-credential',
            'FLIGHT_AGENT_JOURNAL': str(Path(self.directory.name) / 'commands.sqlite3'),
        }

    def test_server_configuration_binds_identity_without_creating_a_journal(self):
        config = PlatformRuntimeConfig.from_environment(self.environment)
        self.assertEqual(config.binding.actor_id, 'actor-1')
        self.assertEqual(config.binding.project_id, 'project-1')
        self.assertEqual(config.binding.task_batch_id, 'batch-1')
        self.assertEqual(config.binding.context_version, 1)
        self.assertEqual(config.site_id, 'site-1')
        self.assertIsNone(config.task_id)
        self.assertFalse(config.allow_insecure_http)
        self.assertFalse(config.journal_path.exists())
        self.assertNotIn(self.environment['KTD_SERVER_TOKEN'], repr(config))
        self.assertNotIn(self.environment['FLIGHT_AGENT_GATEWAY_TOKEN'], repr(config))

    def test_missing_server_configuration_fails_without_leaking_other_values(self):
        for key in self.environment:
            with self.subTest(missing=key):
                data = dict(self.environment)
                del data[key]
                with self.assertRaises(ValueError) as raised:
                    PlatformRuntimeConfig.from_environment(data)
                self.assertIn(key, str(raised.exception))
                self.assertNotIn('synthetic-server-credential', str(raised.exception))
                self.assertFalse(Path(self.environment['FLIGHT_AGENT_JOURNAL']).exists())

    def test_optional_task_and_integer_context_are_preserved(self):
        config = PlatformRuntimeConfig.from_environment({**self.environment,
            'KTD_TASK_ID': 'java-task-1', 'FLIGHT_AGENT_CONTEXT_VERSION': '7'})
        self.assertEqual(config.task_id, 'java-task-1')
        self.assertEqual(config.binding.context_version, 7)

    def test_invalid_configuration_is_rejected_before_any_io(self):
        for name, value in (
            ('KTD_BASE_URL', 'https://user:synthetic-secret@platform.invalid'),
            ('KTD_BASE_URL', 'https://platform.invalid/other/path'),
            ('KTD_BASE_URL', 'http://platform.invalid/api'),
            ('KTD_SERVER_TOKEN', 'unsafe\r\ncredential'),
            ('KTD_PROJECT_ID', 'project\r\nother'),
            ('FLIGHT_AGENT_GATEWAY_TOKEN', 'unsafe\ncredential'),
            ('FLIGHT_AGENT_CONTEXT_VERSION', 'true'),
            ('FLIGHT_AGENT_CONTEXT_VERSION', '-1'),
            ('FLIGHT_AGENT_CONTEXT_VERSION', '1.5'),
            ('KTD_ALLOW_INSECURE_HTTP', 'yes'),
            ('FLIGHT_AGENT_JOURNAL', 'relative.sqlite3'),
        ):
            with self.subTest(field=name):
                with self.assertRaises(ValueError) as raised:
                    PlatformRuntimeConfig.from_environment({**self.environment, name: value})
                self.assertNotIn('synthetic-secret', str(raised.exception))
                self.assertFalse(Path(self.environment['FLIGHT_AGENT_JOURNAL']).exists())

    def test_insecure_http_needs_its_own_explicit_server_setting(self):
        config = PlatformRuntimeConfig.from_environment({**self.environment,
            'KTD_BASE_URL': 'http://platform.invalid/api', 'KTD_ALLOW_INSECURE_HTTP': 'true'})
        self.assertTrue(config.allow_insecure_http)


class FakeJavaOpener:
    """明确的HTTP边界夹具；许可为合成条件，不能证明Java已开放距离模式。"""
    def __init__(self):
        self.calls = []
        self.data = {
            'schema_version': 'lingxi-platform-snapshot/v1', 'snapshot_version': 'a' * 64,
            'generated_at': 1000, 'expires_at': 1005,
            'actor_employee_id': 'actor-1', 'project_id': 'project-1', 'site_id': 'site-1',
            'hangar_id': 'hangar-1', 'drone_id': 'drone-1', 'task_id': None,
            'control_owner_employee_id': 'actor-1', 'control_owner_updated_at': 1000,
            'control_lease_expires_at': 1300, 'control_owner_version': 'b' * 64,
            'profile': 'DJI_HANGAR',
            'telemetry': {'sampled_at': 1000, 'received_at': 1000,
                          'source': 'java-udp-cache', 'sequence': None, 'version': 'c' * 64},
            'state': {'online': True, 'airborne': True, 'longitude': 113, 'latitude': 23,
                      'altitude_m': 100, 'height_m': 50, 'yaw_deg': 90,
                      'battery_percent': None, 'flight_status': None, 'route_status': None},
            'permissions': ['read_state', 'move_forward'],
            'capabilities': {'read_state': True, 'move_forward': True, 'UAV_MOVE_DISTANCE': True},
            'action_availability': {'read_state': {'available': True, 'reasons': []},
                                    'move_forward': {'available': True, 'reasons': []}},
        }

    def open(self, request, timeout):
        method = request.get_method()
        path = urlsplit(request.full_url).path
        self.calls.append((method, path, json.loads(request.data) if request.data else None))
        if method == 'GET':
            assert path == '/api/lingxi/agent/platform/snapshot'
            payload = {'code': 200, 'data': self.data}
        else:
            assert method == 'POST' and path == '/api/screen/uav/operation/control'
            body = json.loads(request.data)
            tracking = body['tracking']
            payload = command_receipt_payload(command_id=tracking['commandId'],
                idempotency_key=tracking['idempotencyKey'], trace_id=tracking['traceId'],
                submitted_at=1000, updated_at=1000, parameters={'distance_m': body['param']})
        response = BytesIO(json.dumps(payload).encode())
        response.code = 200
        return response


class PlatformRuntimeFlowTests(unittest.TestCase):
    def setUp(self):
        PlatformRuntimeConfigTests.setUp(self)
        self.config = PlatformRuntimeConfig.from_environment(self.environment)
        self.opener = FakeJavaOpener()
        self.model_calls = []
        def model_transport(url, payload, timeout):
            self.model_calls.append(True)
            return {'done_reason': 'stop', 'message': {'tool_calls': [
                {'function': {'name': 'move_forward', 'arguments': {'distance_m': 10}}}]}}
        self.factory = lambda: OllamaDecider(model='fixture', base_url='http://model.invalid',
                                            transport=model_transport, max_attempts=1)
        self.headers = {'X-Agent-Session': 'platform-runtime-tab', 'X-Agent-Actor': 'actor-1',
                        'X-Agent-Gateway': self.environment['FLIGHT_AGENT_GATEWAY_TOKEN']}

    def service(self, **options):
        return build_platform_cockpit(self.config, self.factory, opener=self.opener,
                                      clock=lambda: 1000, **options)

    def propose(self, service):
        return service.handle('POST', '/api/flight-control/agent/messages', json.dumps({
            'text': '向前飞10米', 'request_id': 'message-1', 'context_version': 1}), self.headers)

    def test_default_runtime_reads_authoritative_state_but_never_submits_controls(self):
        service = self.service()
        self.assertEqual(self.opener.calls, [])
        context = service.handle('GET', '/api/flight-control/context', '', self.headers)
        self.assertEqual(context.status_code, 200)
        self.assertEqual(context.payload['execution_mode'], 'platform')
        self.assertTrue(context.payload['state']['available'])
        self.assertEqual(context.payload['state']['altitude_m'], 100)
        self.assertNotIn('control_owner', context.payload['state'])
        self.assertTrue(context.payload['state']['control_owned'])
        enabled = {tool['action'] for tool in context.payload['tools'] if tool['available']}
        self.assertEqual(enabled, {'read_state'})
        result = self.propose(service)
        self.assertEqual(result.payload['status'], 'rejected')
        self.assertEqual(result.payload['error_code'], 'PLATFORM_DISABLED')
        self.assertFalse(any(method == 'POST' for method, _, _ in self.opener.calls))
        self.assertNotIn(self.config.server_token, json.dumps(context.payload))
        self.assertNotIn(self.config.gateway_token, json.dumps(context.payload))

    def test_gateway_and_actor_are_enforced_before_reading_java_or_calling_model(self):
        service = self.service()
        for headers, expected in (({'X-Agent-Session': 'tab'}, 401),
                                  ({**self.headers, 'X-Agent-Actor': 'other'}, 403)):
            with self.subTest(expected=expected):
                result = service.handle('GET', '/api/flight-control/context', '', headers)
                self.assertEqual(result.status_code, expected)
        self.assertEqual(self.opener.calls, [])
        self.assertEqual(self.model_calls, [])

    def test_explicit_control_enable_still_requires_card_confirmation_and_returns_submission(self):
        service = self.service(enable_control=True)
        result = self.propose(service)
        self.assertEqual(result.payload['status'], 'preview_required', result.payload)
        self.assertFalse(any(method == 'POST' for method, _, _ in self.opener.calls))
        card = result.payload['card']
        self.assertTrue(card['receipt_tracking'])
        body = json.dumps({key: card[key] for key in ('confirmation_id', 'confirmation_binding',
                           'command_id', 'idempotency_key', 'context_version')})
        confirmed = service.handle('POST', '/api/flight-control/agent/confirm', body, self.headers)
        self.assertEqual(confirmed.payload['status'], 'submitted', confirmed.payload)
        self.assertEqual(confirmed.payload['device_effect'], 'not_verified')
        self.assertTrue(confirmed.payload['receipt_tracking'])
        writes = [body for method, _, body in self.opener.calls if method == 'POST']
        self.assertEqual(writes, [{'terminalId': 'drone-1', 'siteId': 'site-1',
            'type': 202, 'mode': 2, 'param': 10, 'tracking': {
                'schemaVersion': 'lingxi-command-request/v1',
                'commandId': card['command_id'], 'idempotencyKey': card['idempotency_key'],
                'traceId': confirmed.payload['trace_id'], 'snapshotVersion': 'a' * 64,
                'expiresAt': 1005, 'hangarId': 'hangar-1'}}])
        self.assertEqual(len(self.model_calls), 1)
        self.assertEqual(service.handle('POST', '/api/flight-control/agent/confirm', body,
                                       self.headers).payload, confirmed.payload)
        self.assertEqual(len([call for call in self.opener.calls if call[0] == 'POST']), 1)

    def test_write_flag_and_private_journal_are_validated_before_creating_runtime(self):
        with self.assertRaises(ValueError):
            self.service(enable_control='true')
        self.assertFalse(self.config.journal_path.exists())
        self.config.journal_path.parent.chmod(0o755)
        with self.assertRaises(ValueError):
            self.service()
        self.assertFalse(self.config.journal_path.exists())
        self.assertEqual(self.opener.calls, [])


if __name__ == '__main__':
    unittest.main()
