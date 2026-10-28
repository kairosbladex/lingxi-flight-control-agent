#!/usr/bin/env python3
"""原始 Java 回执契约 + 明确合成的回环 HTTP 续接；不连接外部平台或设备。"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

from lingxi_flight_control import OllamaDecider, RuntimeBinding
from lingxi_flight_control.cockpit import COCKPIT_SYSTEM_PROMPT
from lingxi_flight_control.command_receipts import decode_command_receipt
from lingxi_flight_control.full_control import FlightControlActionRequest
from lingxi_flight_control.ktd_platform import KtdHttpResponse
from lingxi_flight_control.platform_runtime import PlatformRuntimeConfig, build_platform_cockpit
from lingxi_flight_control.standalone_preview import build_standalone_server


ROOT = Path(__file__).resolve().parents[2]
RECEIPT_FIELDS = frozenset({'success', 'schema_version', 'execution_mode', 'command_id',
    'context_version', 'reported_status', 'report_source', 'receipt_version', 'reported_at',
    'received_at', 'device_effect', 'continuation_allowed', 'trace_id'})
CONFIRM_FIELDS = ('confirmation_id', 'confirmation_binding', 'command_id',
                  'idempotency_key', 'context_version')


def check(condition, message):
    if not condition:
        raise AssertionError(message)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def local_path(value):
    if '://' in value:
        raise argparse.ArgumentTypeError('只接受本地文件路径，不接受远端 URL')
    return Path(value).expanduser().resolve()


def run(fixture: Path, output: Path | None = None):
    raw = fixture.read_bytes()
    original = json.loads(raw)
    source = original['data']
    # This first check uses the Java converter's unmodified packet and identities.
    original_binding = RuntimeBinding(source['actor_employee_id'], source['project_id'],
        source['hangar_id'], source['drone_id'], 'raw-fixture-validation', 1)
    original_request = FlightControlActionRequest(source['command_id'], source['idempotency_key'],
        source['action'], source['parameters'], 1)
    decoded = decode_command_receipt(KtdHttpResponse(200, original), binding=original_binding,
        site_id=source['site_id'], task_id=source['task_id'], request=original_request,
        trace_id=source['trace_id'], now=source['updated_at'] + 1)
    check(decoded['status'] == 'completed', '原始 Java converter 夹具必须报告 completed')

    # All permissions and runtime bindings below are synthetic test conditions.
    # They do not change the fixture or establish that Java permits real flight.
    started = source['submitted_at']
    now = [started]
    binding = RuntimeBinding('smoke-actor', 'smoke-project', 'smoke-hangar', 'smoke-drone', 'smoke-batch', 1)
    site_id, task_id = 'smoke-site', 'smoke-task'
    server_token, gateway = 'synthetic-java-credential', 'synthetic-gateway-credential'
    resources = {'siteId': [site_id], 'hangarId': [binding.hangar_id],
                 'droneId': [binding.drone_id], 'taskId': [task_id]}
    java_calls, agent_calls, model_calls, reports = [], [], [], {}
    permissions = ['read_state', 'move_forward', 'move_right']
    snapshot = {'schema_version': 'lingxi-platform-snapshot/v1', 'snapshot_version': 'a' * 64,
        'generated_at': started, 'expires_at': started + 5,
        'actor_employee_id': binding.actor_id, 'project_id': binding.project_id,
        'site_id': site_id, 'hangar_id': binding.hangar_id, 'drone_id': binding.drone_id, 'task_id': task_id,
        'control_owner_employee_id': binding.actor_id, 'control_owner_updated_at': started,
        'control_lease_expires_at': started + 300, 'control_owner_version': 'b' * 64,
        'profile': 'DJI_HANGAR', 'telemetry': {'sampled_at': started, 'received_at': started,
            'source': 'java-udp-cache', 'sequence': None, 'version': 'c' * 64},
        'state': {'online': True, 'airborne': True, 'longitude': 113, 'latitude': 23,
            'altitude_m': 100, 'height_m': 50, 'yaw_deg': 90, 'battery_percent': None,
            'flight_status': None, 'route_status': None},
        'permissions': permissions,
        'capabilities': {**dict.fromkeys(permissions, True), 'UAV_MOVE_DISTANCE': True},
        'action_availability': {name: {'available': True, 'reasons': []} for name in permissions}}

    class JavaFixture(BaseHTTPRequestHandler):
        def authorized(self):
            return (self.headers.get('Authorization') == 'Bearer ' + server_token
                and self.headers.get('projectId') == binding.project_id
                and self.headers.get('X-Agent-Gateway') is None)

        def reply(self, payload, status=200):
            encoded = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            target = urlsplit(self.path)
            query = parse_qs(target.query, keep_blank_values=True)
            if target.path == '/api/lingxi/agent/platform/snapshot':
                valid = self.authorized() and query == resources
                payload = {'code': 200, 'success': True, 'data': snapshot}
            elif target.path == '/api/lingxi/agent/commands/receipt':
                command = query.get('commandId', [None])[0]
                valid = (self.authorized() and command in reports
                    and query == {**resources, 'commandId': [command]})
                payload = reports.get(command, {'code': 404, 'success': False})
            else:
                valid, payload = False, {'code': 404, 'success': False}
            java_calls.append(('GET', target.path, valid))
            self.reply(payload if valid else {'code': 403, 'success': False}, 200 if valid else 403)

        def do_POST(self):
            target = urlsplit(self.path)
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
            tracking = body.get('tracking', {})
            command = tracking.get('commandId')
            expected = {'terminalId': binding.drone_id, 'siteId': site_id, 'taskId': task_id,
                'type': 202, 'mode': 2, 'param': 10, 'tracking': tracking}
            valid = (self.authorized() and target.path == '/api/screen/uav/operation/control'
                and not target.query and body == expected and isinstance(command, str)
                and set(tracking) == {'schemaVersion', 'commandId', 'idempotencyKey', 'traceId',
                                      'snapshotVersion', 'expiresAt', 'hangarId'}
                and tracking['schemaVersion'] == 'lingxi-command-request/v1'
                and tracking['idempotencyKey'] == command and tracking['hangarId'] == binding.hangar_id
                and tracking['snapshotVersion'] == snapshot['snapshot_version']
                and tracking['expiresAt'] == started + 5
                and self.headers.get('X-Trace-Id') == tracking['traceId']
                and self.headers.get('X-Idempotency-Key') == command)
            java_calls.append(('POST', target.path, valid))
            if not valid:
                self.reply({'code': 403, 'success': False}, 403)
                return
            check(command not in reports, '合成 Java 夹具收到重复控制 POST')
            packet = deepcopy(original)
            packet['data'].update(command_id=command, idempotency_key=command,
                trace_id=tracking['traceId'], actor_employee_id=binding.actor_id,
                project_id=binding.project_id, site_id=site_id, hangar_id=binding.hangar_id,
                drone_id=binding.drone_id, task_id=task_id, action='move_forward',
                parameters={'distance_m': body['param']}, status='submitted', version=1,
                submitted_at=started, updated_at=started, expires_at=started + 120,
                reported_at=None, received_at=None, error_code=None,
                dispatch_ids={key: str(uuid4()) for key in ('bid', 'tid', 'fly_to_id')})
            reports[command] = packet
            self.reply(packet)

        def log_message(self, *args):
            pass

    def model_transport(url, payload, timeout):
        model_calls.append(True)
        check(len(model_calls) <= 2, '回执查询或隔离会话意外触发模型')
        action, distance = ('move_forward', 10) if len(model_calls) == 1 else ('move_right', 5)
        return {'done_reason': 'stop', 'message': {'tool_calls': [
            {'function': {'name': action, 'arguments': {'distance_m': distance}}}]}}

    java = ThreadingHTTPServer(('127.0.0.1', 0), JavaFixture)
    java_worker = threading.Thread(target=java.serve_forever, daemon=True)
    java_worker.start()
    agent, agent_worker = None, None
    try:
        with tempfile.TemporaryDirectory() as directory:
            config = PlatformRuntimeConfig(base_url=f'http://127.0.0.1:{java.server_port}/api',
                binding=binding, site_id=site_id, task_id=task_id,
                journal_path=Path(directory) / 'commands.sqlite3', server_token=server_token,
                gateway_token=gateway, allow_insecure_http=True)
            service = build_platform_cockpit(config, lambda: OllamaDecider(model='synthetic-model',
                base_url='http://model.invalid', system_prompt=COCKPIT_SYSTEM_PROMPT,
                transport=model_transport, max_attempts=1), enable_control=True, clock=lambda: now[0])
            agent = build_standalone_server(service, '127.0.0.1', 0)
            agent_worker = threading.Thread(target=agent.serve_forever, daemon=True)
            agent_worker.start()
            client = build_opener(ProxyHandler({}), NoRedirect())

            def request(path, body=None, *, session='smoke-tab', actor=None, authenticated=True):
                headers = {'Content-Type': 'application/json', 'X-Agent-Session': session}
                if authenticated:
                    headers.update({'X-Agent-Gateway': gateway, 'X-Agent-Actor': actor or binding.actor_id})
                req = Request(f'http://127.0.0.1:{agent.server_port}/api/flight-control' + path,
                    data=None if body is None else json.dumps(body).encode(), headers=headers,
                    method='GET' if body is None else 'POST')
                try:
                    response = client.open(req, timeout=5)
                except HTTPError as error:
                    response = error
                with response:
                    code, value = response.code, json.load(response)
                agent_calls.append((req.get_method(), urlsplit(path).path, code))
                return code, value

            def message(text, request_id, **options):
                return request('/agent/messages', {'text': text, 'request_id': request_id,
                                                    'context_version': 1}, **options)

            def report(path, expected_status, continuation, **options):
                code, value = request(path, **options)
                check(code == 200 and set(value) == RECEIPT_FIELDS, '设备报告不是严格 13 字段 DTO')
                check(value['success'] is True and value['execution_mode'] == 'platform'
                    and value['schema_version'] == 'flight-command-receipt/v1'
                    and value['command_id'] == card['command_id'] and value['context_version'] == 1
                    and value['trace_id'] == submission['trace_id']
                    and value['report_source'] == 'ktd-command-journal'
                    and value['device_effect'] == 'not_verified'
                    and value['reported_status'] == expected_status
                    and value['continuation_allowed'] is continuation, '设备报告状态或续接权限错误')
                expected_version = 1 if expected_status == 'submitted' else 2
                expected_time = None if expected_status == 'submitted' else started + 1
                check(type(value['receipt_version']) is int and value['receipt_version'] == expected_version
                    and value['reported_at'] == expected_time and value['received_at'] == expected_time,
                    '设备报告版本或时间与合成 Java 报告不一致')
                return value

            code, denied = request('/context', authenticated=False)
            check(code == 401 and denied['error_code'] == 'UNAUTHENTICATED', '网关未拒绝缺失身份')
            check(not java_calls and not model_calls, '认证前触发了外部边界')
            code, context = request('/context')
            check(code == 200 and context['state']['available'], '合成平台状态不可读')
            check({row['action'] for row in context['tools'] if row['available']} == set(permissions),
                  '合成许可没有经过真实能力校验')
            code, proposal = message('先向前飞10米再向右飞5米', 'initial-intent')
            check(code == 200 and proposal['status'] == 'preview_required', '未生成第一张确认卡')
            card = proposal['card']
            check(card['receipt_tracking'] is True, '确认卡未声明回执跟踪')
            confirmation = {key: card[key] for key in CONFIRM_FIELDS}
            check(not any(method == 'POST' for method, _, _ in java_calls), '确认前下发了指令')
            code, wrong_session = request('/agent/confirm', confirmation, session='other-tab')
            check(code == 200 and wrong_session['status'] == 'rejected', '其他会话可确认本会话动作')
            code, submission = request('/agent/confirm', confirmation)
            check(code == 200 and submission['status'] == 'submitted', '平台提交没有可靠回执')
            check('device_report' not in submission and '_dispatch_fingerprint' not in json.dumps(submission),
                  '公开提交响应泄露内部设备报告')
            check(request('/agent/confirm', confirmation)[1] == submission, '确认重放改变提交回执')
            receipt_path = '/agent/receipt?' + urlencode({'command_id': card['command_id'], 'context_version': 1})
            report(receipt_path, 'submitted', False)
            check(message('继续', 'before-completion')[1]['status'] == 'clarification', '提交即允许继续')
            check(len(model_calls) == 1, '完成报告之前再次触发模型')
            before = len(java_calls)
            for path, options, expected in (
                (receipt_path, {'authenticated': False}, 'UNAUTHENTICATED'),
                (receipt_path, {'actor': 'other-actor'}, 'FORBIDDEN'),
                (receipt_path, {'session': 'invalid/session'}, 'INVALID_SESSION'),
                (receipt_path.replace('context_version=1', 'context_version=2'), {}, 'STALE_CONTEXT'),
                (receipt_path + '&context_version=1', {}, 'INVALID_REQUEST')):
                code, rejected = request(path, **options)
                check(code >= 400 and rejected['error_code'] == expected, '查询身份/上下文/参数校验失败')
            check(len(java_calls) == before, '被拒绝的查询仍访问 Java')
            reports[card['command_id']]['data'].update(status='completed', version=2,
                updated_at=started + 1, reported_at=started + 1, received_at=started + 1)
            now[0] = started + 2
            report(receipt_path, 'completed', False, session='other-tab')
            check(message('继续', 'other-continue', session='other-tab')[1]['status'] == 'clarification',
                  '跨会话恢复了原意图')
            completed = report(receipt_path, 'completed', True)
            check(report(receipt_path, 'completed', True) == completed, '重复查询改变同版本设备报告')
            code, next_proposal = message('继续', 'next-step')
            check(code == 200 and next_proposal['status'] == 'preview_required'
                and next_proposal['card']['action'] == 'move_right'
                and next_proposal['card']['command_id'] != card['command_id'], '显式继续未产生独立的新卡')
            report(receipt_path, 'completed', False)
            check(request('/agent/reset', {'context_version': 1})[1]['status'] == 'cancelled', '重置失败')
            report(receipt_path, 'completed', False)
            check(message('继续', 'after-reset')[1]['status'] == 'clarification', '重置后复活旧意图')
            control_posts = sum(method == 'POST' for method, _, _ in java_calls)
            check(control_posts == 1 and len(model_calls) == 2, '查询或继续自动重发了控制指令')
            check(all(valid for _, _, valid in java_calls), 'Java HTTP 路由、query、身份或控制参数错误')
            check(fixture.read_bytes() == raw, '原始 Java 夹具被改写')
            public = {'provenance': {'fixture_sha256': hashlib.sha256(raw).hexdigest(),
                'raw_converter_fixture_validated_without_changes': True,
                'flow_source': 'synthetic-permissions-and-rebound-java-receipt',
                'synthetic_fields': ['resource_identity', 'command_id', 'trace_id', 'idempotency_key',
                    'status', 'version', 'timestamps', 'dispatch_ids', 'snapshot_permissions'],
                'real_platform_or_device_calls': 0},
                'context': context, 'card': card, 'submission': submission,
                'device_receipt': completed, 'next_card': next_proposal['card']}
            check(all(secret not in json.dumps(public) for secret in (server_token, gateway)),
                  '公开 DTO 包含网关或平台凭据')
            if output is not None:
                with output.open('x', encoding='utf8') as handle:
                    json.dump(public, handle, ensure_ascii=False, indent=2)
                    handle.write('\n')
            return {'result': 'PASS', 'source': public['provenance']['flow_source'],
                'fixture_sha256': public['provenance']['fixture_sha256'],
                'raw_converter_fixture_status': decoded['status'], 'receipt_fields': len(RECEIPT_FIELDS),
                'agent_http_requests': len(agent_calls),
                'java_snapshot_http_requests': sum(path.endswith('/platform/snapshot') for _, path, _ in java_calls),
                'java_receipt_http_requests': sum(path.endswith('/commands/receipt') for _, path, _ in java_calls),
                'java_control_posts': control_posts, 'model_calls': len(model_calls),
                'confirmation_cards': 2, 'external_platform_requests': 0, 'physical_device_requests': 0,
                'public_dto_output': str(output) if output is not None else None}
    finally:
        if agent is not None:
            agent.shutdown()
            agent.server_close()
            agent_worker.join(timeout=2)
        java.shutdown()
        java.server_close()
        java_worker.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', type=local_path, default=ROOT / 'fixtures/ktd-command-receipt-v1.json',
                        help='本地 Java converter 原始 JSON；不接受远端 URL')
    parser.add_argument('--output', type=local_path, help='可选公开 DTO JSON 输出；已有文件不覆盖')
    args = parser.parse_args()
    print(json.dumps(run(args.fixture, args.output), ensure_ascii=False))


if __name__ == '__main__':
    main()
