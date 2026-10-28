#!/usr/bin/env python3
"""Owned-loopback browser fixture: real Agent/Java adapter, scripted boundaries.

No environment configuration, live model, production platform, or device is used.
Only this process's fake Java listener can receive adapter requests. This is an
explicit integration fixture, never a product API or model-accuracy evaluation.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, build_opener
from uuid import uuid4

from lingxi_flight_control import OllamaDecider, RuntimeBinding
from lingxi_flight_control.cockpit import COCKPIT_SYSTEM_PROMPT
from lingxi_flight_control.command_receipts import decode_command_receipt
from lingxi_flight_control.full_control import FlightControlActionRequest
from lingxi_flight_control.ktd_platform import KtdHttpResponse
from lingxi_flight_control.platform_runtime import PlatformRuntimeConfig, build_platform_cockpit
from lingxi_flight_control.standalone_preview import StandaloneResponse, build_standalone_server
from vendor_control_smoke import NoRedirect, check, sha, strict


ROOT = Path(__file__).resolve().parents[2]
MARKER = 'lingxi-chat-bubble-platform-fixture/v1'
RAW_FIXTURE_SHA256 = '19f13c591e93466f65414a0294851022bfe159b53e8412380d93ab13c7365eaa'
CONFIRM = ('confirmation_id', 'confirmation_binding', 'command_id', 'idempotency_key', 'context_version')
ALLOWED = {
    ('GET', '/api/flight-control/context'), ('GET', '/api/flight-control/state'),
    ('GET', '/api/flight-control/agent/receipt'),
    *(('POST', '/api/flight-control/agent/' + route) for route in ('messages', 'confirm', 'cancel', 'reset')),
}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf8')


def sources():
    paths = sorted((ROOT / 'src').rglob('*.py')) + [Path(__file__).resolve(),
        ROOT / 'tools/bench/vendor_control_smoke.py', ROOT / 'fixtures/ktd-command-receipt-v1.json']
    return {str(path.relative_to(ROOT)): sha(path.read_bytes()) for path in paths}


def serve(duration):
    before = sources()
    raw = (ROOT / 'fixtures/ktd-command-receipt-v1.json').read_bytes()
    check(sha(raw) == RAW_FIXTURE_SHA256, '原样Java converter fixture哈希不一致')
    original = strict(raw)
    source = original['data']
    original_binding = RuntimeBinding(source['actor_employee_id'], source['project_id'],
        source['hangar_id'], source['drone_id'], 'raw-fixture-check', 1)
    decoded = decode_command_receipt(KtdHttpResponse(200, original), binding=original_binding,
        site_id=source['site_id'], task_id=source['task_id'],
        request=FlightControlActionRequest(source['command_id'], source['idempotency_key'],
            source['action'], source['parameters'], 1), trace_id=source['trace_id'], now=source['updated_at'] + 1)
    check(decoded['status'] == 'completed', '原始Java converter fixture未通过')

    started = time.time()
    binding = RuntimeBinding('bubble-actor', 'bubble-project', 'bubble-hangar', 'bubble-drone', 'bubble-batch', 1)
    site_id, task_id = 'bubble-site', 'bubble-task'
    # Deliberately synthetic constants, local to this fixture. Never read env.
    server_token, gateway = 'synthetic-bubble-java-credential', 'synthetic-bubble-gateway-credential'
    resources = {'siteId': [site_id], 'hangarId': [binding.hangar_id],
                 'droneId': [binding.drone_id], 'taskId': [task_id]}
    java_calls, agent_calls, model_calls, previews = [], [], [], []
    reports, agent_location = {}, {}
    lock = threading.RLock()
    stop = threading.Event()
    allowed_ports = set()
    network = {'connects': 0, 'blocked': 0}
    limits = {'agent_http_requests': 500, 'java_http_requests': 1000, 'model_calls': 40,
              'java_control_posts': 8, 'model_attempts': 1, 'max_model_steps': 3,
              'wall_seconds': duration, 'http_timeout_seconds': 5}

    def audit(event, arguments):
        if event == 'socket.connect':
            address = arguments[1]
            allowed = isinstance(address, tuple) and address[0] == '127.0.0.1' and address[1] in allowed_ports
            network['connects' if allowed else 'blocked'] += 1
            check(allowed, 'fixture只允许连接自己的回环服务')
        elif event == 'socket.bind':
            check(arguments[1] == ('127.0.0.1', 0), 'fixture只能监听127.0.0.1临时端口')
        elif event == 'socket.getaddrinfo':
            check(arguments[0] == '127.0.0.1', 'fixture不得查询外部地址')
        elif event in {'subprocess.Popen', 'os.system'}:
            raise AssertionError('fixture不得启动外部程序')
    sys.addaudithook(audit)

    permissions = ['read_state', 'move_forward']

    def snapshot():
        observed = time.time() - 0.001
        return {'schema_version': 'lingxi-platform-snapshot/v1', 'snapshot_version': 'a' * 64,
            'generated_at': observed, 'expires_at': observed + 5,
            'actor_employee_id': binding.actor_id, 'project_id': binding.project_id,
            'site_id': site_id, 'hangar_id': binding.hangar_id, 'drone_id': binding.drone_id, 'task_id': task_id,
            'control_owner_employee_id': binding.actor_id, 'control_owner_updated_at': started,
            'control_lease_expires_at': started + 300, 'control_owner_version': 'b' * 64,
            'profile': 'DJI_HANGAR', 'telemetry': {'sampled_at': observed, 'received_at': observed,
                'source': 'java-udp-cache', 'sequence': None, 'version': 'c' * 64},
            'state': {'online': True, 'airborne': True, 'longitude': 113, 'latitude': 23,
                'altitude_m': 100, 'height_m': 50, 'yaw_deg': 90, 'battery_percent': None,
                'flight_status': None, 'route_status': None},
            'permissions': permissions,
            'capabilities': {**dict.fromkeys(permissions, True), 'UAV_MOVE_DISTANCE': True},
            'action_availability': {name: {'available': True, 'reasons': []} for name in permissions}}

    class JavaFixture(BaseHTTPRequestHandler):
        timeout = 5

        def authorized(self):
            return (self.headers.get('Authorization') == 'Bearer ' + server_token
                and self.headers.get('projectId') == binding.project_id
                and self.headers.get('X-Agent-Gateway') is None)

        def reply(self, payload, status=200):
            body = encoded(payload)
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            target = urlsplit(self.path)
            query = parse_qs(target.query, keep_blank_values=True)
            with lock:
                if target.path == '/api/lingxi/agent/platform/snapshot':
                    valid = self.authorized() and query == resources
                    payload = {'code': 200, 'success': True, 'data': snapshot()}
                elif target.path == '/api/lingxi/agent/commands/receipt':
                    command = query.get('commandId', [None])[0]
                    valid = self.authorized() and command in reports and query == {**resources, 'commandId': [command]}
                    payload = reports.get(command, {'code': 404, 'success': False})
                    if valid and payload['data']['status'] == 'submitted':
                        # A scripted Java boundary report, only exposed when the
                        # user explicitly requests the product receipt endpoint.
                        reported = time.time()
                        payload['data'].update(status='completed', version=2, updated_at=reported,
                                               reported_at=reported, received_at=reported)
                else:
                    valid, payload = False, {'code': 404, 'success': False}
                java_calls.append({'method': 'GET', 'path': target.path, 'valid': bool(valid)})
                within_budget = len(java_calls) <= limits['java_http_requests']
                self.reply(payload if valid and within_budget else {'code': 403, 'success': False},
                           200 if valid and within_budget else 403)

        def do_POST(self):
            target = urlsplit(self.path)
            try:
                body = strict(self.rfile.read(min(int(self.headers.get('Content-Length', '0')), 65537)))
            except (ValueError, AssertionError):
                body = {}
            tracking = body.get('tracking', {}) if isinstance(body, dict) else {}
            command = tracking.get('commandId') if isinstance(tracking, dict) else None
            with lock:
                expected = {'terminalId': binding.drone_id, 'siteId': site_id, 'taskId': task_id,
                    'type': 202, 'mode': 2, 'param': 10, 'tracking': tracking}
                now = time.time()
                valid = (self.authorized() and target.path == '/api/screen/uav/operation/control'
                    and not target.query and body == expected and isinstance(command, str)
                    and isinstance(tracking, dict) and set(tracking) == {'schemaVersion', 'commandId',
                        'idempotencyKey', 'traceId', 'snapshotVersion', 'expiresAt', 'hangarId'}
                    and tracking['schemaVersion'] == 'lingxi-command-request/v1'
                    and tracking['idempotencyKey'] == command and tracking['hangarId'] == binding.hangar_id
                    and tracking['snapshotVersion'] == 'a' * 64
                    and type(tracking['expiresAt']) in (int, float) and now < tracking['expiresAt'] <= now + 5
                    and self.headers.get('X-Trace-Id') == tracking['traceId']
                    and self.headers.get('X-Idempotency-Key') == command and command not in reports
                    and len(reports) < limits['java_control_posts'])
                event = {'method': 'POST', 'path': target.path, 'valid': bool(valid),
                         'body_sha256': sha(encoded(body))}
                if valid:
                    event.update(body=deepcopy(body), command_id=command, trace_id=tracking['traceId'])
                java_calls.append(event)
                if not valid:
                    self.reply({'code': 403, 'success': False}, 403)
                    return
                packet = deepcopy(original)
                packet['data'].update(command_id=command, idempotency_key=command,
                    trace_id=tracking['traceId'], actor_employee_id=binding.actor_id,
                    project_id=binding.project_id, site_id=site_id, hangar_id=binding.hangar_id,
                    drone_id=binding.drone_id, task_id=task_id, action='move_forward',
                    parameters={'distance_m': 10}, status='submitted', version=1,
                    submitted_at=now, updated_at=now, expires_at=now + 120,
                    reported_at=None, received_at=None, error_code=None,
                    dispatch_ids={key: str(uuid4()) for key in ('bid', 'tid', 'fly_to_id')})
                reports[command] = packet
                self.reply(packet)

        def log_message(self, *args):
            pass

    def model_transport(url, payload, timeout):
        content = next(message['content'] for message in payload['messages'] if message['role'] == 'user')
        text = strict(content).get('current_request')
        tool = text in {'10米', '十米', '向前飞10米'}
        response = {'done_reason': 'stop', 'message': ({'tool_calls': [{'function': {
            'name': 'move_forward', 'arguments': {'distance_m': 10}}}]} if tool else
            {'content': '请补充飞行距离，例如10米。'})}
        with lock:
            check(len(model_calls) < limits['model_calls'], 'fixture模型调用预算耗尽')
            model_calls.append({'input_sha256': sha(str(text).encode('utf8')),
                'fixed_phrase': text if text in {'向前飞', '10米', '十米', '向前飞10米'} else 'unsupported',
                'decision': 'move_forward_10' if tool else 'clarification',
                'tool_schema_sha256': sha(encoded(payload.get('tools', []))),
                'scripted_boundary': True})
        return response

    class FixtureApplication:
        def __init__(self, service):
            self.service = service

        def evidence(self):
            with lock:
                after = sources()
                posts = [deepcopy(call) for call in java_calls if call['method'] == 'POST']
                result = {'fixture_marker': MARKER, **agent_location,
                    'recorded_at': datetime.now(timezone.utc).isoformat(),
                    'limits': limits, 'counts': {'agent_http_requests': len(agent_calls),
                        'model_calls': len(model_calls), 'java_control_posts': len(posts),
                        'java_snapshot_gets': sum(call['path'].endswith('/platform/snapshot') for call in java_calls),
                        'java_receipt_gets': sum(call['path'].endswith('/commands/receipt') for call in java_calls),
                        'live_model_requests': 0, 'external_platform_requests': 0, 'physical_device_requests': 0},
                    'java_control_posts': posts, 'java_http_calls': deepcopy(java_calls),
                    'agent_http_calls': deepcopy(agent_calls), 'model_calls': deepcopy(model_calls),
                    'previews': deepcopy(previews), 'network': dict(network),
                    'source_freeze': {'before': before, 'after': after, 'stable': before == after},
                    'provenance': {'raw_converter_fixture_sha256': sha(raw), 'raw_converter_validated': True,
                        'runtime': ['CockpitService', 'BoundedAgentLoop', 'OllamaDecider', 'KtdPlatformExecutor'],
                        'boundaries': ['scripted-model-transport', 'owned-loopback-fake-java'],
                        'authorization_snapshot': 'synthetic-test-permissions-only',
                        'configuration_source': 'fixed-in-script-no-environment',
                        'device_effect': 'not_verified', 'model_accuracy': 'not_evaluable'}}
                check(all(secret not in json.dumps(result) for secret in (server_token, gateway)),
                      '证据不得包含合成凭据')
                return result

        def handle(self, method, path, body=b'', headers=None):
            if method == 'GET' and path == '/__fixture/evidence':
                return StandaloneResponse(200, self.evidence())
            route = urlsplit(path).path
            with lock:
                if (method, route) not in ALLOWED or len(agent_calls) >= limits['agent_http_requests']:
                    return StandaloneResponse(404, {'success': False, 'error_code': 'FIXTURE_ROUTE_NOT_ALLOWED'})
                previous_posts = sum(call['method'] == 'POST' for call in java_calls)
                safe_headers = {key: value for key, value in (headers or {}).items()
                    if key.lower() not in {'x-agent-gateway', 'x-agent-actor'}}
                safe_headers.update({'X-Agent-Gateway': gateway, 'X-Agent-Actor': binding.actor_id})
                session = next((value for key, value in safe_headers.items() if key.lower() == 'x-agent-session'), '')
                try:
                    payload = strict(body) if body else {}
                except (ValueError, AssertionError):
                    payload = {}
                if not isinstance(payload, dict):
                    payload = {}
                expected = next((card['confirmation'] for card in reversed(previews)
                    if card['session_sha256'] == sha(session.encode())
                    and card['confirmation']['confirmation_id'] == payload.get('confirmation_id')), None)
            # Do not hold the evidence lock while the real service calls Java.
            response = self.service.handle(method, path, body, safe_headers)
            with lock:
                record = {'method': method, 'path': route, 'status': response.status_code,
                    'response_status': response.payload.get('status'),
                    'request_body_sha256': sha(bytes(body)), 'java_posts_before': previous_posts,
                    'java_posts_after': sum(call['method'] == 'POST' for call in java_calls)}
                if route.endswith(('/confirm', '/cancel')):
                    record.update(confirmation_fields=sorted(payload),
                        confirmation_matches_preview=expected is not None and payload == expected,
                        confirmation_sha256=sha(encoded(payload)))
                card = response.payload.get('card')
                if isinstance(card, dict):
                    confirmation = {key: card[key] for key in CONFIRM}
                    previews.append({'session_sha256': sha(session.encode()), 'action': card['action'],
                        'parameters': card['parameters'], 'confirmation': confirmation,
                        'confirmation_sha256': sha(encoded(confirmation)), 'trace_id': response.payload.get('trace_id')})
                agent_calls.append(record)
            return response

    java = ThreadingHTTPServer(('127.0.0.1', 0), JavaFixture)
    java.daemon_threads = False
    allowed_ports.add(java.server_port)
    java_worker = threading.Thread(target=java.serve_forever, daemon=True)
    java_worker.start()
    agent, agent_worker, directory = None, None, None
    try:
        directory = tempfile.TemporaryDirectory(prefix='bubble-platform-fixture-')
        config = PlatformRuntimeConfig(base_url=f'http://127.0.0.1:{java.server_port}/api',
            binding=binding, site_id=site_id, task_id=task_id, journal_path=Path(directory.name) / 'commands.sqlite3',
            server_token=server_token, gateway_token=gateway, allow_insecure_http=True)
        service = build_platform_cockpit(config, lambda: OllamaDecider(model='scripted-bubble-fixture',
            base_url='http://model.invalid', system_prompt=COCKPIT_SYSTEM_PROMPT,
            transport=model_transport, max_attempts=1), enable_control=True,
            opener=build_opener(ProxyHandler({}), NoRedirect()))
        application = FixtureApplication(service)
        agent = build_standalone_server(application, '127.0.0.1', 0)
        # Close each fixture HTTP connection after its response. server_close
        # can then join every accepted request before the SQLite folder leaves.
        agent.RequestHandlerClass.protocol_version = 'HTTP/1.0'
        agent.RequestHandlerClass.timeout = 5
        agent.daemon_threads = False
        allowed_ports.add(agent.server_port)
        agent_location.update(agent_url=f'http://127.0.0.1:{agent.server_port}/api/flight-control',
                              evidence_url=f'http://127.0.0.1:{agent.server_port}/__fixture/evidence')
        agent_worker = threading.Thread(target=agent.serve_forever, daemon=True)
        agent_worker.start()
        for name in (signal.SIGINT, signal.SIGTERM):
            signal.signal(name, lambda *_: stop.set())
        print(json.dumps({'fixture_marker': MARKER, **agent_location,
                          'duration_seconds': duration}, ensure_ascii=False), flush=True)
        stop.wait(duration)
    finally:
        if agent is not None:
            agent.shutdown(); agent.server_close(); agent_worker.join(timeout=2)
        java.shutdown(); java.server_close(); java_worker.join(timeout=2)
        if directory is not None:
            directory.cleanup()


def bounded_duration(value):
    try:
        result = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('duration must be an integer') from None
    if not 1 <= result <= 300:
        raise argparse.ArgumentTypeError('duration must be between 1 and 300 seconds')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--duration', type=bounded_duration, default=180,
                        help='Maximum lifetime (1..300 seconds); SIGINT/SIGTERM also stop owned listeners')
    args = parser.parse_args()
    serve(args.duration)


if __name__ == '__main__':
    main()
