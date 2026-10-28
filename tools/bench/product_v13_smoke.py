#!/usr/bin/env python3
"""Bounded loopback HTTP acceptance, scripted intent, no external connections."""
import json
import resource
import signal
import socket
import threading
from urllib.request import ProxyHandler, Request, build_opener

from lingxi_flight_control import (MemoryFlightControlSimulator, RuntimeBinding,
    ModelDecision, DecisionKind, ToolCall)
from lingxi_flight_control.cockpit import CockpitService
from lingxi_flight_control.standalone_preview import build_standalone_server


def run():
    signal.alarm(30)
    resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    binding = RuntimeBinding('smoke-actor', 'smoke-project', 'smoke-hangar', 'smoke-drone', 'smoke-batch', 1)
    simulator = MemoryFlightControlSimulator(binding)
    simulator.state.control_owner = binding.actor_id
    simulator.state.altitude_m = 50

    class ScriptedIntent:
        def decide(self, message, *_):
            text = json.loads(message)['current_request']
            calls = {'变焦100倍': ToolCall('camera_zoom', {'value': 100}),
                     '向前飞10米': ToolCall('move_forward', {'distance_m': 10})}
            return ModelDecision(DecisionKind.TOOL_CALL, tool_call=calls[text])

    service = CockpitService(binding, simulator, ScriptedIntent)
    server = build_standalone_server(service, '127.0.0.1', 0)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    opener = build_opener(ProxyHandler({}))
    sequence = 0
    checks = []

    def request(route, body):
        data = json.dumps(body).encode()
        req = Request(f'http://127.0.0.1:{port}/api/flight-control/agent/{route}',
            data=data, headers={'Content-Type': 'application/json', 'X-Agent-Session': 'smoke-v13'})
        with opener.open(req, timeout=3) as response:
            return json.load(response)

    def message(text):
        nonlocal sequence
        sequence += 1
        return request('messages', {'text': text, 'request_id': str(sequence), 'context_version': 1})

    def confirm(card):
        return request('confirm', {key: card[key] for key in ('confirmation_id', 'confirmation_binding',
            'command_id', 'idempotency_key', 'context_version')})

    try:
        card = message('变焦100倍')['card']
        assert card['parameters'] == {'value': 30}
        assert card['requested_parameters'] == {'value': 100}
        result = confirm(card)
        assert result['state']['camera']['zoom'] == 30
        assert not result['dispatched'] and result['device_effect'] == 'not_verified'
        assert confirm(card) == result
        checks.append('zoom-adjustment-confirm-replay')
        simulator.state.control_owner = None
        assert message('急停')['error_code'] == 'CONTROL_NOT_OWNED'
        checks.append('emergency-stop-ownership')
        simulator.state.control_owner = binding.actor_id
        simulator.state.route_id, simulator.state.route_status = 'main-route', 'running'
        assert message('向前飞10米')['error_code'] == 'ROUTE_PAUSE_REQUIRED'
        card = message('悬停')['card']
        assert card['action'] == 'route_pause'
        assert confirm(card)['state']['route_status'] == 'paused'
        checks.append('running-route-hover-pause')
        simulator.state.route_status, simulator.state.task_type = 'running', 'mapping'
        simulator.state.auto_stitching = True
        card = message('向前飞10米')['card']
        assert any('成果失效' in warning for warning in card['warnings'])
        result = confirm(card)
        assert result['state']['mapping_invalidated']
        assert not result['state']['auto_stitching']
        checks.append('mapping-interruption-explicit-confirmation')
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        signal.alarm(0)
    with socket.socket() as probe:
        probe.settimeout(1)
        assert probe.connect_ex(('127.0.0.1', port)) != 0
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes, Linux reports KiB.
    import sys
    peak_mb = peak / (1024 * 1024 if sys.platform == 'darwin' else 1024)
    assert peak_mb < 256
    return {'passed': len(checks), 'checks': checks, 'port': port, 'listener_closed': True,
            'peak_rss_mb': round(peak_mb, 2), 'model': 'scripted-intent-not-live-model',
            'execution_mode': 'simulator', 'external_requests': 0, 'device_effect': 'not_verified'}


if __name__ == '__main__':
    print(json.dumps(run(), ensure_ascii=False, indent=2))
