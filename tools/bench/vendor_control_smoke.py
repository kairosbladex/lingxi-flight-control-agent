#!/usr/bin/env python3
"""原样Java厂商fixture校验与自有回环HTTP确认链；零在线模型、平台或设备。"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

from lingxi_flight_control import OllamaDecider, RuntimeBinding
from lingxi_flight_control.cockpit import COCKPIT_SYSTEM_PROMPT
from lingxi_flight_control.ktd_platform import KtdHttpResponse, KtdPlatformError
from lingxi_flight_control.ktd_snapshot import KtdSnapshotProvider
from lingxi_flight_control.platform_runtime import PlatformRuntimeConfig, build_platform_cockpit
from lingxi_flight_control.standalone_preview import build_standalone_server
from lingxi_flight_control.vendor_control import VendorControlRequest, decode_vendor_receipt


ROOT = Path(__file__).resolve().parents[2]
FIXTURE_HASH = 'c73c7be750b3f3661718c3730f267ec96a84c64afaa622aaeb734159453ca5b2'
METHODS = ('flight_authority_grab', 'drc_mode_enter', 'drc_mode_exit')
TEXTS = ('申请DJI厂商飞控航权', '进入DRC模式', '退出DRC模式')
CONFIRM = ('confirmation_id', 'confirmation_binding', 'command_id', 'idempotency_key', 'context_version')
PUBLIC = frozenset({'success', 'schema_version', 'execution_mode', 'kind', 'command_id',
    'context_version', 'method', 'reported_status', 'report_source', 'receipt_version',
    'reported_at', 'received_at', 'result_code', 'vendor_authority', 'drc_link',
    'device_effect', 'continuation_allowed', 'trace_id'})


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def unique(pairs):
    result = {}
    for key, value in pairs:
        check(key not in result, '重复JSON字段: ' + key)
        result[key] = value
    return result


def strict(raw):
    def invalid(value):
        raise ValueError('非有限JSON数字: ' + value)
    def finite(value):
        number = float(value)
        check(math.isfinite(number), '非有限JSON数字')
        return number
    text = raw.decode('utf8', errors='strict') if isinstance(raw, bytes) else raw
    return json.loads(text, object_pairs_hook=unique, parse_constant=invalid, parse_float=finite)


def sources():
    paths = sorted((ROOT / 'src').rglob('*.py')) + [Path(__file__).resolve(),
        ROOT / 'fixtures/ktd-vendor-http-v1.json', ROOT / 'fixtures/ktd-vendor-http-v1.provenance.json']
    return {str(path.relative_to(ROOT)): sha(path.read_bytes()) for path in paths}


def original_validation(fixture):
    raw = fixture.read_bytes()
    check(sha(raw) == FIXTURE_HASH, 'Java冻结fixture哈希不一致')
    manifest = strict(raw)
    provenance = strict((fixture.with_suffix('.provenance.json')).read_bytes())
    check(manifest['schema_version'] == 'lingxi-vendor-http-fixtures/v1', '错误fixture版本')
    check(provenance['fixture_sha256'] == sha(raw) and provenance['preserved_original_bytes'] is True
          and provenance['production_java_commit'] == manifest['production_java_sha'], 'fixture来源清单不符')
    cases = {case['case_id']: case for case in manifest['cases']}
    snapshots = {item['id']: item for item in manifest['snapshots']}
    count = 0

    def walk(value):
        nonlocal count
        if isinstance(value, dict):
            if 'body_utf8' in value:
                body = value['body_utf8'].encode('utf8')
                check(len(body) == value['body_bytes'] and sha(body) == value['body_sha256'], '原HTTP正文哈希/长度不符')
                count += 1
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(manifest)
    decoded, rejected = [], []
    for item in snapshots.values():
        expected = item['expected_binding']
        binding = RuntimeBinding(expected['actor_id'], expected['project_id'], expected['hangar_id'],
            expected['drone_id'], 'raw-java-fixture', expected['context_version'])
        packet = strict(item['http']['response']['body_utf8'])

        class SnapshotTransport:
            actor_id = binding.actor_id

            def read_snapshot(self, *args, **kwargs):
                return KtdHttpResponse(200, packet)

        observed = KtdSnapshotProvider(binding, site_id=expected['site_id'], task_id=expected['task_id'],
            transport=SnapshotTransport(), clock=lambda: item['clock_seconds'])()
        check(observed.version == packet['data']['snapshot_version'], '原快照版本未保留')
    for case in cases.values():
        expected = case['expected']
        resource = snapshots[case['snapshot_ref']]['expected_binding']
        binding = RuntimeBinding(resource['actor_id'], resource['project_id'], resource['hangar_id'],
            resource['drone_id'], 'raw-java-fixture', resource['context_version'])
        request = VendorControlRequest(expected['command_id'], expected['idempotency_key'],
            expected['method'], binding.context_version)
        packet = strict(case['response']['body_utf8'])
        check(packet['code'] == expected['code'] and case['response']['http_status'] == 200
              and case['response']['cache_control'] == 'no-store', '原HTTP信封不符')
        if expected['code'] != 200:
            check(packet['data'] == '' and packet['success'] is False
                  and packet['extra']['error_code'] == expected['error_code'], '原失败信封不符')
        try:
            result = decode_vendor_receipt(KtdHttpResponse(case['response']['http_status'], packet),
                binding=binding, site_id=resource['site_id'], task_id=resource['task_id'], request=request,
                trace_id=expected['trace_id'], now=case['clock_seconds'])
        except KtdPlatformError as error:
            check(expected['code'] != 200 and str(error) == 'VENDOR_RECEIPT_UPSTREAM_REJECTED', '原包拒绝原因不符')
            rejected.append(case['case_id'])
        else:
            check(expected['code'] == 200 and result['status'] == expected['status']
                  and result['result_code'] == expected['result_code'], '原包状态或结果码不符')
            decoded.append(case['case_id'])
    try:
        strict(cases['duplicate-key']['request']['body_utf8'])
    except AssertionError:
        pass
    else:
        raise AssertionError('原始重复method请求未保留')
    check(len(cases) == 13 and len(snapshots) == 2 and count == 56 and len(decoded) == 10 and len(rejected) == 3,
          '原样验证分母不一致')
    return manifest, {'fixture_sha256': sha(raw), 'fixture_bytes': len(raw), 'snapshots': 2,
        'body_records': count, 'decoded_cases': decoded, 'rejected_envelopes': rejected,
        'raw_packets_modified': False}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def run(fixture: Path, output: Path):
    check(not output.exists(), '输出已存在，禁止覆盖')
    started = time.monotonic()
    before = sources()
    manifest, original = original_validation(fixture)
    cases = {case['case_id']: case for case in manifest['cases']}
    snapshot = next(item for item in manifest['snapshots'] if item['id'] == 'with-task')
    packet = snapshot['http']['response']['body_utf8'].encode('utf8')
    now = [snapshot['clock_seconds']]
    bound = snapshot['expected_binding']
    binding = RuntimeBinding(bound['actor_id'], bound['project_id'], bound['hangar_id'], bound['drone_id'], 'vendor-smoke', 1)
    site, task = bound['site_id'], bound['task_id']
    resources = {'siteId': [site], 'hangarId': [binding.hangar_id], 'droneId': [binding.drone_id], 'taskId': [task]}
    token, gateway = 'synthetic-vendor-server-token', 'synthetic-vendor-gateway-token'
    calls, agent_calls, model_calls, rebindings, public_reports = [], [], [], [], []
    records, current = {}, [METHODS[0]]
    allowed_ports, network = set(), {'connects': 0, 'blocked': 0}
    limits = {'agent_http_requests': 100, 'java_http_requests': 160, 'scripted_model_calls': 8,
              'max_model_steps': 3, 'model_max_attempts': 1, 'http_timeout_seconds': 5, 'wall_seconds': 90}

    def audit(event, arguments):
        if event == 'socket.connect':
            address = arguments[1]
            if not isinstance(address, tuple) or address[0] != '127.0.0.1' or address[1] not in allowed_ports:
                network['blocked'] += 1
                raise AssertionError('只允许当前smoke自有回环端口')
            network['connects'] += 1
        elif event == 'socket.bind':
            address = arguments[1]
            check(isinstance(address, tuple) and address == ('127.0.0.1', 0), '只允许127.0.0.1临时监听')
    sys.addaudithook(audit)

    def rebound(case_id, body, ids=None):
        source = strict(cases[case_id]['response']['body_utf8'])
        result = deepcopy(source)
        changes = {'command_id': body['commandId'], 'idempotency_key': body['idempotencyKey'],
                   'trace_id': body['traceId'], 'method': body['method']}
        if ids is not None:
            changes['dispatch_ids'] = ids
        differences = {key: {'original': source['data'][key], 'synthetic': value}
                       for key, value in changes.items() if source['data'][key] != value}
        result['data'].update(changes)
        rebindings.append({'source_case': case_id, 'changes': differences,
            'source_body_sha256': cases[case_id]['response']['body_sha256']})
        return json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf8')

    class JavaFixture(BaseHTTPRequestHandler):
        def reply(self, raw, status=200):
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def authorized(self):
            return (self.headers.get('Authorization') == 'Bearer ' + token
                    and self.headers.get('projectId') == binding.project_id
                    and self.headers.get('X-Agent-Gateway') is None)

        def do_GET(self):
            target = urlsplit(self.path)
            query = parse_qs(target.query, keep_blank_values=True)
            valid = self.authorized()
            if target.path == '/api/lingxi/agent/platform/snapshot':
                valid = valid and query == resources
                raw = packet
            elif target.path == '/api/lingxi/agent/commands/vendor-receipt':
                command = query.get('commandId', [None])[0]
                record = records.get(command)
                valid = valid and record is not None and query == {**resources, 'commandId': [command], 'method': [record['body']['method']]}
                raw = record['raw'] if record else b'{}'
            else:
                valid, raw = False, b'{}'
            calls.append({'method': 'GET', 'path': target.path, 'valid': bool(valid)})
            self.reply(raw if valid else b'{"code":403,"success":false}', 200 if valid else 403)

        def do_POST(self):
            target = urlsplit(self.path)
            body = strict(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
            required = {'schemaVersion', 'method', 'commandId', 'idempotencyKey', 'traceId', 'siteId', 'hangarId', 'droneId', 'taskId', 'snapshotVersion', 'expiresAt'}
            valid = (self.authorized() and target.path == '/api/screen/uav/operation/control/vendor-request'
                and not target.query and set(body) == required and body['method'] in METHODS
                and body['schemaVersion'] == 'lingxi-vendor-control-request/v1'
                and {key: [body[key]] for key in resources} == resources
                and body['snapshotVersion'] == strict(packet)['data']['snapshot_version']
                and type(body['expiresAt']) in (float, int) and now[0] < body['expiresAt'] <= snapshot['clock_seconds'] + 4
                and self.headers.get('X-Trace-Id') == body['traceId']
                and self.headers.get('X-Idempotency-Key') == body['idempotencyKey']
                and body['idempotencyKey'] == body['commandId'] and body['commandId'] not in records)
            calls.append({'method': 'POST', 'path': target.path, 'valid': bool(valid)})
            if not valid:
                self.reply(b'{"code":403,"success":false}', 403)
                return
            source = 'unknown-after-commit' if body['method'] == METHODS[2] else 'submitted-' + body['method']
            raw = rebound(source, body)
            records[body['commandId']] = {'body': body, 'raw': raw}
            self.reply(raw)

        def log_message(self, *args):
            pass

    def model_transport(url, payload, timeout):
        check(len(model_calls) < limits['scripted_model_calls'], '超过合成模型预算')
        model_calls.append({'method': current[0], 'tools': len(payload.get('tools', []))})
        movement = current[0] == 'move_forward'
        return {'done_reason': 'stop', 'message': {'tool_calls': [{'function': {
            'name': 'move_forward' if movement else 'request_vendor_control',
            'arguments': {'distance_m': 10} if movement else {'method': current[0]}}}]}}

    java = ThreadingHTTPServer(('127.0.0.1', 0), JavaFixture)
    allowed_ports.add(java.server_port)
    java_thread = threading.Thread(target=java.serve_forever, daemon=True)
    java_thread.start()
    agent, agent_thread = None, None
    server_instances = 1
    client = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with tempfile.TemporaryDirectory(prefix='vendor-http-smoke-') as directory:
            config = PlatformRuntimeConfig(base_url=f'http://127.0.0.1:{java.server_port}/api',
                binding=binding, site_id=site, task_id=task, journal_path=Path(directory) / 'commands.sqlite3',
                server_token=token, gateway_token=gateway, allow_insecure_http=True, allowed_vendor_methods=METHODS)

            def start_agent(enabled):
                nonlocal agent, agent_thread, server_instances
                if agent is not None:
                    agent.shutdown(); agent.server_close(); agent_thread.join(timeout=2)
                service = build_platform_cockpit(config, lambda: OllamaDecider(model='scripted-offline',
                    base_url='http://model.invalid', system_prompt=COCKPIT_SYSTEM_PROMPT,
                    transport=model_transport, max_attempts=1), enable_control=False,
                    enable_vendor_requests=enabled, clock=lambda: now[0])
                agent = build_standalone_server(service, '127.0.0.1', 0)
                allowed_ports.add(agent.server_port)
                agent_thread = threading.Thread(target=agent.serve_forever, daemon=True)
                agent_thread.start(); server_instances += 1

            def request(path, body=None, *, session='vendor-smoke', actor=None, authenticated=True):
                check(len(agent_calls) < limits['agent_http_requests'] and len(calls) < limits['java_http_requests'], '超过HTTP预算')
                check(time.monotonic() - started < limits['wall_seconds'], '超过总时限')
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
                    code, value = response.code, strict(response.read())
                agent_calls.append({'method': req.get_method(), 'path': urlsplit(path).path, 'status': code})
                return code, value

            def message(text, *, session='vendor-smoke'):
                return request('/agent/messages', {'text': text, 'request_id': uuid4().hex, 'context_version': 1}, session=session)

            def public(path, method, status):
                code, value = request(path)
                check(code == 200 and set(value) == PUBLIC, '公开回执不是严格18字段')
                check(value['success'] is True and value['schema_version'] == 'vendor-control-receipt/v1'
                    and value['execution_mode'] == 'platform' and value['kind'] == 'vendor_service_result'
                    and value['method'] == method and value['reported_status'] == status
                    and value['vendor_authority'] == value['drc_link'] == 'unknown'
                    and value['device_effect'] == 'not_verified' and value['continuation_allowed'] is False,
                    '公开回执状态、持权或续接边界错误')
                public_reports.append(value)
                return value

            start_agent(False)
            check(request('/context', authenticated=False)[0] == 401 and not calls, '认证前访问Java')
            closed = request('/context')[1]
            check(len(closed['tools']) == 59 and all(row['action'] != 'request_vendor_control' for row in closed['tools']), '默认开关未关闭')
            check(not model_calls and not records, '默认关闭触发写入或模型')
            start_agent(True)
            context = request('/context')[1]
            check({row['action'] for row in context['tools'] if row['available']} == {'read_state', 'request_vendor_control'}, '移动能力未保持关闭')
            check(len(context['tools']) == 60, 'vendor配置不应改默认59工具基线')
            saved = []
            for method, text in zip(METHODS, TEXTS):
                current[0] = method
                before_posts = len(records)
                code, proposal = message(text)
                check(code == 200 and proposal['status'] == 'preview_required', '未生成厂商确认卡: ' + str(proposal))
                card = proposal['card']
                check(card.get('kind') == 'vendor_control_request' and card['action'] == 'request_vendor_control'
                    and card['parameters'] == {'method': method} and card['receipt_tracking'] is True, '确认卡契约错误')
                confirmation = {key: card[key] for key in CONFIRM}
                check(len(records) == before_posts, '预览自动提交')
                check(request('/agent/confirm', confirmation, session='other-tab')[1]['status'] == 'rejected', '跨会话确认成功')
                check(request('/agent/confirm', confirmation, actor='other-actor')[0] == 403, '跨员工确认成功')
                submission = request('/agent/confirm', confirmation)[1]
                expected = 'unknown' if method == METHODS[2] else 'submitted'
                check(submission['status'] == expected and submission.get('kind') == 'vendor_control_request', '确认结果错误')
                check(request('/agent/confirm', confirmation)[1] == submission and len(records) == before_posts + 1, '确认重放重新POST')
                path = '/agent/vendor-receipt?' + urlencode({'command_id': card['command_id'], 'context_version': 1})
                report = public(path, method, expected)
                check(report['command_id'] == card['command_id'] and report['trace_id'] == submission['trace_id'], '回执未绑定原确认')
                if method != METHODS[2]:
                    record = records[card['command_id']]
                    terminal = 'request-succeeded' if method == METHODS[0] else 'failed-int-min'
                    ids = strict(record['raw'])['data']['dispatch_ids']
                    record['raw'] = rebound(terminal, record['body'], ids)
                    now[0] = max(now[0], strict(record['raw'])['data']['updated_at'])
                    report = public(path, method, 'request_succeeded' if method == METHODS[0] else 'failed')
                    check(report['result_code'] == (0 if method == METHODS[0] else -(2**31)), '终态结果码错误')
                count = len(model_calls)
                check(message('继续')[1]['status'] == 'clarification' and len(model_calls) == count, '厂商结果意外续接模型')
                saved.append((card, path, report))

            before_calls, before_posts = len(calls), len(records)
            path = saved[0][1]
            for invalid, options, expected in (
                (path, {'authenticated': False}, 'UNAUTHENTICATED'),
                (path, {'actor': 'other-actor'}, 'FORBIDDEN'),
                (path, {'session': 'bad/session'}, 'INVALID_SESSION'),
                (path + '&context_version=1', {}, 'INVALID_REQUEST'),
                (path + '&method=drc_mode_exit', {}, 'INVALID_REQUEST'),
                (path.replace('context_version=1', 'context_version=2'), {}, 'STALE_CONTEXT'),
                ('/agent/vendor-receipt?command_id=only', {}, 'INVALID_REQUEST')):
                code, rejected = request(invalid, **options)
                check(code >= 400 and rejected['error_code'] == expected, '查询隔离错误: ' + expected)
            check(len(calls) == before_calls and len(records) == before_posts, '拒绝查询仍访问Java')
            for cancel in (True, False):
                current[0] = METHODS[1]
                card = message(TEXTS[1])[1]['card']
                confirmation = {key: card[key] for key in CONFIRM}
                route, payload = ('/agent/cancel', confirmation) if cancel else ('/agent/reset', {'context_version': 1})
                check(request(route, payload)[1]['status'] == 'cancelled', '取消/重置失败')
                if not cancel:
                    check(request('/agent/confirm', confirmation)[1]['status'] == 'rejected', '重置后旧卡可提交')
            check(len(records) == 3, '取消/重置产生POST')
            current[0] = 'move_forward'
            denied = message('向前飞10米', session='movement-denied')[1]
            check(denied['status'] == 'rejected' and 'card' not in denied and len(records) == 3,
                  '显式移动请求未被平台写开关阻断')
            start_agent(False)
            check(len(request('/context')[1]['tools']) == 59, '重启未关闭vendor工具')
            for card, path, report in saved:
                restored = public(path, card['parameters']['method'], report['reported_status'])
                check(restored == report, '关闭写开关重启后GET未恢复既有journal回执')
                check(request('/agent/confirm', {key: card[key] for key in CONFIRM})[1]['status'] == 'rejected', '重启后无卡仍可POST')
            check(len(records) == 3 and all(call['valid'] for call in calls), 'Java路由/身份/POST次数错误')
            check(sha(fixture.read_bytes()) == FIXTURE_HASH, '原fixture被改写')
            after = sources()
            changed = [path for path in sorted(set(before) | set(after)) if before.get(path) != after.get(path)]
            result = {'schema_version': 'lingxi-vendor-control-smoke/v1', 'result': 'PASS',
                'recorded_at': datetime.now(timezone.utc).isoformat(), 'original_fixture': original,
                'source_freeze': {'before': before, 'after': after, 'changed': changed, 'stable': not changed},
                'provenance': {'flow': 'owned-loopback-http-with-explicit-synthetic-rebinding',
                    'production_java_commit': manifest['production_java_sha'],
                    'raw_java_packets_unchanged_phase': True, 'synthetic_rebindings': rebindings,
                    'snapshot_http_bytes_unchanged': True, 'real_platform_requests': 0, 'physical_device_requests': 0,
                    'live_model_requests': 0, 'movement_enabled': False, 'offline_unittest_discover': False},
                'limits': limits, 'counts': {'agent_http_requests': len(agent_calls), 'java_http_requests': len(calls),
                    'vendor_posts': len(records), 'scripted_model_calls': len(model_calls),
                    'public_receipts': len(public_reports), 'public_receipt_fields': len(PUBLIC),
                    'raw_snapshots': 2, 'raw_decoded_cases': 10, 'raw_rejected_envelopes': 3,
                    'server_instances': server_instances, 'max_concurrent_servers': 2, **network},
                'java_http_calls': calls, 'agent_http_calls': agent_calls, 'model_calls': model_calls,
                'public_reports': public_reports, 'elapsed_seconds': time.monotonic() - started,
                'limitations': ['回环fixture服务不是实际Java在线联调', '合成模型只验证已冻结工具协议，不代表自然语言准确率',
                    'request_succeeded不证明持续航权/DRC链路或设备效果', '厂商请求不解除旧动作拒绝或允许续接',
                    '源文件在运行期间变化时stable=false，不能宣称冻结验收']}
            check(all(secret not in json.dumps(result) for secret in (token, gateway)), '报告泄露合成凭据')
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open('x', encoding='utf8') as handle:
                json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.write('\n')
            return {'result': 'PASS', 'output': str(output), 'report_sha256': sha(output.read_bytes()),
                    'source_stable': not changed, **result['counts']}
    finally:
        if agent is not None:
            agent.shutdown(); agent.server_close(); agent_thread.join(timeout=2)
        java.shutdown(); java.server_close(); java_thread.join(timeout=2)


def local_path(value):
    if '://' in value:
        raise argparse.ArgumentTypeError('只接受本地文件，不接受远端URL')
    return Path(value).expanduser().resolve()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', type=local_path, default=ROOT / 'fixtures/ktd-vendor-http-v1.json')
    parser.add_argument('--output', type=local_path, help='新报告路径；已有文件禁止覆盖')
    args = parser.parse_args()
    output = args.output or ROOT / 'artifacts' / ('vendor-control-smoke-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:8]) / 'report.json'
    print(json.dumps(run(args.fixture, output), ensure_ascii=False))


if __name__ == '__main__':
    main()
