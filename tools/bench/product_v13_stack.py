#!/usr/bin/env python3
"""Owned Java fixture + real Agent HTTP + built Vue UI, never MQTT or live models."""
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import threading
import time
from urllib.request import Request, build_opener, ProxyHandler
from uuid import uuid4

from lingxi_flight_control import RuntimeBinding
from lingxi_flight_control.cockpit import CockpitService
from lingxi_flight_control.standalone_preview import build_standalone_server
from lingxi_flight_control.zhuoyi_platform import ZhuoyiPlatformExecutor


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--jar', type=Path, required=True)
    parser.add_argument('--ui', type=Path, required=True)
    parser.add_argument('--java', required=True)
    parser.add_argument('--serve-seconds', type=int, default=0)
    args = parser.parse_args()
    if not 0 <= args.serve_seconds <= 300:
        parser.error('serve-seconds must be 0..300')
    token = uuid4().hex
    opener = build_opener(ProxyHandler({}))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        java_port = sock.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith(('ZHUOYI_', 'SPRING_', 'SERVER_'))}
    env['ZHUOYI_GATEWAY_BEARER_TOKEN'] = token
    with tempfile.TemporaryFile() as java_log:
        process = subprocess.Popen([args.java, '-Xmx384m', '-XX:ActiveProcessorCount=2', '-jar', str(args.jar),
            '--spring.profiles.active=fixture', '--server.address=127.0.0.1', f'--server.port={java_port}',
            '--zhuoyi.flight-control.enabled=true', '--zhuoyi.flight-control.dji-transport.enabled=true',
            '--zhuoyi.flight-control.mqtt.enabled=false', '--zhuoyi.flight-control.dji-transport.mqtt.enabled=false'],
            env=env, stdout=java_log, stderr=java_log)
        timer = threading.Timer(args.serve_seconds + 90, process.kill)
        timer.start()
        servers = []
        try:
            binding = RuntimeBinding('actor-1', 'project-1', 'hangar-1', 'drone-1', 'isolated-v13', 1)
            executor = ZhuoyiPlatformExecutor(binding, base_url=f'http://127.0.0.1:{java_port}',
                gateway_token=token, site_id='site-1', receipt_timeout=3)
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError('isolated Java exited before startup')
                if executor.read_state().get('available'):
                    break
                time.sleep(.2)
            else:
                raise RuntimeError('isolated Java startup timed out')
            service = CockpitService(binding, executor, lambda: None, gateway_token=token,
                enable_plan_execution=True, plan_decider_factory=lambda: None)
            agent = build_standalone_server(service, '127.0.0.1', 0)
            servers.append(agent)
            threading.Thread(target=agent.serve_forever, daemon=True).start()
            agent_port = agent.server_port
            counts = {'messages': 0, 'confirmations': 0}

            class Bridge(SimpleHTTPRequestHandler):
                def log_message(self, *_):
                    pass

                def proxy(self):
                    if self.path.split('?')[0] not in {'/api/flight-control/' + p for p in (
                        'context', 'state', 'agent/messages', 'agent/confirm', 'agent/cancel', 'agent/reset')}:
                        self.send_error(404)
                        return
                    body = self.rfile.read(int(self.headers.get('Content-Length', 0))) if self.command == 'POST' else None
                    if self.path.endswith('/messages'):
                        counts['messages'] += 1
                    if self.path.endswith('/confirm'):
                        counts['confirmations'] += 1
                    request = Request(f'http://127.0.0.1:{agent_port}' + self.path, data=body,
                        method=self.command, headers={'Content-Type': 'application/json',
                            'X-Agent-Gateway': token, 'X-Agent-Actor': binding.actor_id,
                            'X-Agent-Session': self.headers.get('X-Agent-Session', 'stack-smoke')})
                    with opener.open(request, timeout=15) as response:
                        data = response.read()
                        self.send_response(response.status)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Content-Length', str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)

                def do_GET(self):
                    if self.path.startswith('/api/'):
                        return self.proxy()
                    if self.path.startswith('/flight-agent-ui/'):
                        self.path = self.path.removeprefix('/flight-agent-ui')
                    return super().do_GET()

                def do_POST(self):
                    self.proxy()

            ui = ThreadingHTTPServer(('127.0.0.1', 0), partial(Bridge, directory=str(args.ui)))
            servers.append(ui)
            threading.Thread(target=ui.serve_forever, daemon=True).start()
            origin = f'http://127.0.0.1:{ui.server_port}'
            sequence = 0

            def send(path, data):
                request = Request(origin + '/api/flight-control/agent/' + path,
                    data=json.dumps(data).encode(), headers={'Content-Type': 'application/json'})
                with opener.open(request, timeout=15) as response:
                    return json.load(response)

            results = []
            for text, expected in [('喊话音量调到150%', 'speaker_volume'),
                                   ('开始喊话：交通安全', 'speaker_speak'),
                                   ('边向前飞10米边喊话：交通安全', 'move_forward')]:
                sequence += 1
                proposal = send('messages', {'text': text, 'request_id': str(sequence), 'context_version': 1})
                assert proposal['status'] == 'preview_required', proposal
                card = proposal['card']
                assert card['action'] == expected
                payload = {key: card[key] for key in ('confirmation_id', 'confirmation_binding',
                    'command_id', 'idempotency_key', 'context_version')}
                result = send('confirm', payload)
                assert result['status'] == 'accepted', result
                assert result['device_effect'] == 'not_verified'
                assert send('confirm', payload) == result
                results.append({'action': expected, 'status': result['status'],
                    'order': card.get('execution_order', 'single'), 'effective': card['parameters'],
                    'step_statuses': [s['status'] for s in result.get('steps', [])]})
            print(json.dumps({'url': origin, 'java_port': java_port, 'agent_port': agent_port,
                'passed': len(results), 'results': results, 'mqtt': False,
                'model': 'exact-commands-no-model', 'device': 'Java-fixture-only'}, ensure_ascii=False), flush=True)
            if args.serve_seconds:
                time.sleep(args.serve_seconds)
        finally:
            ports = [s.server_port for s in servers] + [java_port]
            for server in reversed(servers):
                server.shutdown()
                server.server_close()
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            timer.cancel()
            for port in ports:
                with socket.socket() as probe:
                    probe.settimeout(1)
                    assert probe.connect_ex(('127.0.0.1', port)) != 0
            print(json.dumps({'cleanup': 'all listeners closed', 'ports': ports}), flush=True)


if __name__ == '__main__':
    main()
