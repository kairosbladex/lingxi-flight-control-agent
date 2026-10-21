"""Bounded loopback HTTP check with synthetic Java and a forbidden model."""
import json
from pathlib import Path
import sys
import threading
from time import monotonic
from unittest.mock import patch
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tests'))
from test_browser_gateway import BrowserGatewayTests
from lingxi_flight_control.standalone_preview import build_standalone_server


def main():
    fixture = BrowserGatewayTests()
    fixture.setUp()
    server = build_standalone_server(fixture.gateway, '127.0.0.1', 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_port
    rows = []
    try:
        with patch('lingxi_flight_control.ollama_decider.OllamaDecider.decide',
                   side_effect=AssertionError('model forbidden')) as model:
            for index, text in enumerate(('当前无人机状态是什么？', '刷新状态', '现在电量多少？')):
                started = monotonic()
                request = Request(f'http://127.0.0.1:{port}/api/flight-control/agent/messages',
                    data=json.dumps(dict(text=text, request_id=f'query-{index}', context_version=1)).encode(),
                    headers={**fixture.headers, 'Content-Type': 'application/json'})
                with urlopen(request, timeout=5) as response:
                    result = json.load(response)
                assert result['kind'] == 'state_query' and result['status'] == 'final', result
                assert result['dispatched'] is False and 'card' not in result
                assert fixture.java.posts == []
                rows.append({'case': index, 'status': result['status'],
                             'elapsed_ms': round((monotonic() - started) * 1000)})
            model.assert_not_called()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        fixture.doCleanups()
    assert not thread.is_alive()
    print(json.dumps({'cases': rows, 'passed': len(rows), 'model_calls': 0,
        'java_control_posts': 0, 'device_requests': 0, 'port': port, 'server_stopped': True}))


if __name__ == '__main__':
    main()
