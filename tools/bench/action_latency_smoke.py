"""Preview-only real-model probe; Java is synthetic and every write is forbidden."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import threading
import time
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tests'))
from test_browser_gateway import BrowserGatewayTests
from lingxi_flight_control import OllamaDecider
from lingxi_flight_control.browser_gateway import BrowserGateway
from lingxi_flight_control.cockpit import COCKPIT_SYSTEM_PROMPT
from lingxi_flight_control.ollama_decider import http_transport
from lingxi_flight_control.standalone_preview import build_standalone_server


CASES = [('拍一张照片', 'camera_photo', {}),
         ('向前飞2米', 'move_forward', {'distance_m': 2}),
         ('向后飞2米', 'move_backward', {'distance_m': 2}),
         ('拍一张照片', 'camera_photo', {}),
         ('向右平移2米', 'move_right', {'distance_m': 2}),
         ('向左平移2米', 'move_left', {'distance_m': 2})]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--model', default='qwen3.5:9b')
    parser.add_argument('--offline-scripted', action='store_true')
    args = parser.parse_args()
    base = args.base_url.rstrip('/')
    fixture = BrowserGatewayTests()
    fixture.setUp()
    calls, rows, violations = [], [], []
    deadline = time.monotonic() + 360
    current = [None]

    def transport(url, payload, timeout):
        assert url == base + '/api/chat', 'unexpected model endpoint'
        if len(calls) >= 12 or time.monotonic() >= deadline:
            raise TimeoutError('probe budget exhausted')
        row = {'case': current[0], 'timeout_sec': timeout,
               'tool_count': len(payload.get('tools', [])),
               'tools_sha256': hashlib.sha256(json.dumps(payload.get('tools'), sort_keys=True).encode()).hexdigest()}
        calls.append(row)
        started = time.monotonic()
        try:
            if args.offline_scripted:
                _, name, arguments = CASES[current[0]]
                response = {'done_reason': 'stop', 'message': {'tool_calls': [
                    {'function': {'name': name, 'arguments': arguments}}]}}
            else:
                response = http_transport(url, payload, min(timeout, max(.01, deadline - time.monotonic())))
            row['generation'] = {key: response.get(key) for key in (
                'done_reason', 'prompt_eval_count', 'eval_count', 'load_duration',
                'prompt_eval_duration', 'eval_duration', 'total_duration')}
            row['tools'] = [item.get('function', {}).get('name')
                            for item in response.get('message', {}).get('tool_calls', [])]
            return response
        except Exception as exc:
            row['error_type'] = type(exc).__name__
            raise
        finally:
            row['elapsed_ms'] = round((time.monotonic() - started) * 1000)

    class ReadOnlyJava:
        def open(self, request, timeout):
            if request.get_method() != 'GET':
                violations.append('java_write_attempt')
                raise AssertionError('Java writes forbidden')
            return fixture.java.open(request, timeout)

    fixture.gateway = BrowserGateway(fixture.gateway.config,
        lambda: OllamaDecider(model=args.model, base_url=base,
            system_prompt=COCKPIT_SYSTEM_PROMPT, timeout=45, max_attempts=1,
            max_output_tokens=2048, keep_alive='30m', transport=transport),
        enable_control=True, opener=ReadOnlyJava(), clock=lambda: 1000,
        allowed_drone_sns=['uav-sn-1'])
    server = build_standalone_server(fixture.gateway, '127.0.0.1', 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_port
    print(json.dumps({'event': 'probe_start', 'time': datetime.now(timezone.utc).isoformat(),
        'model': args.model, 'offline_scripted': args.offline_scripted,
        'cases_sha256': hashlib.sha256(json.dumps(CASES, ensure_ascii=False).encode()).hexdigest(),
        'prompt_sha256': hashlib.sha256(COCKPIT_SYSTEM_PROMPT.encode()).hexdigest(),
        'budgets': {'seconds': 360, 'calls': 12, 'request_timeout': 45, 'run_timeout': 65,
                    'steps': 3, 'num_ctx': 32768, 'max_output_tokens': 2048},
        'session': 'one synthetic session; previews only; no confirmations', 'port': port}), flush=True)
    try:
        for index, (text, expected, parameters) in enumerate(CASES):
            current[0] = index
            row = {'case': index, 'input': text, 'expected_action': expected}
            if time.monotonic() >= deadline or len(calls) >= 12:
                row.update(status='missing', reason='probe_budget_exhausted')
                rows.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
                continue
            started, before = time.monotonic(), len(calls)
            def send():
                request = Request(f'http://127.0.0.1:{port}/api/flight-control/agent/messages',
                    data=json.dumps(dict(text=text, request_id=f'latency-{index}', context_version=1)).encode(),
                    headers={**fixture.headers, 'Content-Type': 'application/json'})
                with urlopen(request, timeout=150) as response:
                    return json.load(response)
            try:
                result = send()
                card = result.get('card') or {}
                row.update(status=result.get('status'), error_code=result.get('error_code'),
                    action=card.get('action'), parameters=card.get('parameters'),
                    elapsed_ms=round((time.monotonic()-started)*1000), model_calls=len(calls)-before,
                    passed=(result.get('status') == 'preview_required' and card.get('action') == expected
                            and card.get('parameters') == parameters), dispatched=result.get('dispatched'))
                assert result.get('dispatched') is False
                assert not fixture.java.posts and not violations
                replay_calls = len(calls)
                replay = send()
                assert replay == result and len(calls) == replay_calls
                row['idempotent_replay'] = True
            except Exception as exc:
                row.update(status='probe_error', passed=False, error_type=type(exc).__name__)
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        fixture.doCleanups()
    summary = {'event': 'probe_complete', 'passed': sum(r.get('passed', False) for r in rows),
        'total': len(CASES), 'rows': rows, 'model_calls': calls,
        'java_control_posts': len(fixture.java.posts), 'write_attempts': len(violations),
        'confirmations': 0, 'device_requests': 0, 'server_stopped': not thread.is_alive()}
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if summary['passed'] == len(CASES) and not violations else 1


if __name__ == '__main__':
    raise SystemExit(main())
