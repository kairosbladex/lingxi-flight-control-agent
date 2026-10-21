#!/usr/bin/env python3
"""Java序列化合成样本→真实HTTP→Agent会话；仅连接脚本自己的两个回环服务。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from lingxi_flight_control import OllamaDecider, RuntimeBinding
from lingxi_flight_control.platform_runtime import PlatformRuntimeConfig, build_platform_cockpit
from lingxi_flight_control.standalone_preview import build_standalone_server


def main():
    fixture = Path(__file__).resolve().parents[2] / 'fixtures/ktd-platform-snapshot-v1.json'
    packet = json.loads(fixture.read_text(encoding='utf8'))
    data = packet['data']
    java_calls, model_calls = [], []
    expected_query = {key: [data[field]] for key, field in (
        ('siteId', 'site_id'), ('hangarId', 'hangar_id'), ('droneId', 'drone_id'), ('taskId', 'task_id'))
        if data[field] is not None}

    class JavaFixture(BaseHTTPRequestHandler):
        def do_GET(self):
            target = urlsplit(self.path)
            valid = (target.path == '/api/lingxi/agent/platform/snapshot'
                and parse_qs(target.query) == expected_query
                and self.headers.get('Authorization') == 'Bearer synthetic-http-credential'
                and self.headers.get('projectId') == data['project_id'])
            java_calls.append(('GET', target.path, valid))
            body = packet if valid else {'code': 403}
            self.send_response(200 if valid else 403)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def do_POST(self):
            java_calls.append(('POST', urlsplit(self.path).path, False))
            self.send_response(405)
            self.end_headers()

        def log_message(self, *args):
            pass

    def model_transport(url, payload, timeout):
        model_calls.append(True)
        return {'done_reason': 'stop', 'message': {'tool_calls': [
            {'function': {'name': 'move_forward', 'arguments': {'distance_m': 10}}}]}}

    java_server = ThreadingHTTPServer(('127.0.0.1', 0), JavaFixture)
    java_worker = threading.Thread(target=java_server.serve_forever, daemon=True)
    java_worker.start()
    agent_server, agent_worker = None, None
    try:
        with tempfile.TemporaryDirectory() as directory:
            config = PlatformRuntimeConfig(
                base_url=f'http://127.0.0.1:{java_server.server_port}/api',
                binding=RuntimeBinding(data['actor_employee_id'], data['project_id'], data['hangar_id'],
                                       data['drone_id'], 'http-smoke-batch', 1),
                site_id=data['site_id'], task_id=data['task_id'], journal_path=Path(directory) / 'commands.sqlite3',
                server_token='synthetic-http-credential', gateway_token='synthetic-gateway-credential',
                allow_insecure_http=True)
            service = build_platform_cockpit(config, lambda: OllamaDecider(model='fixture',
                base_url='http://model.invalid', transport=model_transport, max_attempts=1),
                enable_control=True, clock=lambda: data['generated_at'])
            agent_server = build_standalone_server(service, '127.0.0.1', 0)
            agent_worker = threading.Thread(target=agent_server.serve_forever, daemon=True)
            agent_worker.start()

            def request(path, body=None, authenticated=True):
                headers = {'Content-Type': 'application/json', 'X-Agent-Session': 'http-smoke-tab'}
                if authenticated:
                    headers.update({'X-Agent-Gateway': config.gateway_token,
                                    'X-Agent-Actor': config.binding.actor_id})
                req = Request(f'http://127.0.0.1:{agent_server.server_port}/api/flight-control' + path,
                    data=json.dumps(body).encode() if body is not None else None, headers=headers,
                    method='POST' if body is not None else 'GET')
                try:
                    response = urlopen(req, timeout=5)
                except HTTPError as error:
                    response = error
                with response:
                    return response.code, json.load(response)

            status, denied = request('/context', authenticated=False)
            assert status == 401 and denied['error_code'] == 'UNAUTHENTICATED'
            assert java_calls == [] and model_calls == []
            status, context = request('/context')
            assert status == 200 and context['execution_mode'] == 'platform'
            assert context['state']['available'] and context['state']['altitude_m'] == data['state']['altitude_m']
            assert {tool['action'] for tool in context['tools'] if tool['available']} == {'read_state'}
            status, rejected = request('/agent/messages', {
                'text': '向前飞10米', 'request_id': 'http-smoke-message', 'context_version': 1})
            assert status == 200 and rejected['status'] == 'rejected'
            assert rejected['error_code'] == 'ACTION_NOT_PERMITTED' and not rejected['dispatched']
            assert len(model_calls) == 1
            assert java_calls and all(method == 'GET' and valid for method, _, valid in java_calls)
            print(json.dumps({'result': 'PASS', 'source': 'java-serialized-synthetic-fixture',
                'agent_http_requests': 3, 'java_snapshot_http_requests': len(java_calls),
                'model_calls': len(model_calls), 'confirmations': 0, 'java_control_posts': 0,
                'external_platform_requests': 0, 'physical_device_requests': 0}, ensure_ascii=False))
    finally:
        if agent_server:
            agent_server.shutdown()
            agent_server.server_close()
            agent_worker.join(timeout=2)
        java_server.shutdown()
        java_server.server_close()
        java_worker.join(timeout=2)


if __name__ == '__main__':
    main()
