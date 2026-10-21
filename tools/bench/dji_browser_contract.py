#!/usr/bin/env python3
"""真实模型+合成Java契约；平台请求全部被OldJava拦截，绝不触达设备。"""
import argparse
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from urllib.request import Request, urlopen
from lingxi_flight_control.standalone_preview import build_standalone_server

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tests'))
from test_browser_gateway import BrowserGatewayTests
from lingxi_flight_control import OllamaDecider
from lingxi_flight_control.browser_gateway import BrowserGateway
from lingxi_flight_control.cockpit import COCKPIT_SYSTEM_PROMPT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url')
    parser.add_argument('--offline-scripted', action='store_true', help='固定模型建议，仅本地HTTP与合成Java')
    parser.add_argument('--model', default='qwen3.5:9b')
    parser.add_argument('--precision-only', action='store_true',
                        help='仅验证精确距离/云台角度理解；云台旧速度接口必须拒绝精确角度')
    args = parser.parse_args()
    if not args.offline_scripted and not args.base_url:
        parser.error('真实模型验证需要base-url')
    cases = [('往前飞10米', 'move_forward'), ('开始录像', 'camera_start_recording'),
             ('停止录像', 'camera_stop_recording'), ('光学变焦设置为5倍', 'camera_zoom'),
             ('切换到广角镜头', 'camera_wide'), ('暂停当前航线', 'route_pause'),
             ('帮我拍张照', 'camera_photo')]
    if args.precision_only:
        cases = [('往前飞10米', 'move_forward'), ('云台向下调10度', 'gimbal_down')]
    rows = []
    for phrase, expected in cases:
        fixture = BrowserGatewayTests()
        fixture.setUp()
        fixture.action=expected
        fixture.parameters=({'value':5} if expected=='camera_zoom' else
                            {'angle_deg':10} if expected=='gimbal_down' else None)
        server = None
        try:
            if not args.offline_scripted:
                fixture.gateway = BrowserGateway(fixture.gateway.config,
                lambda: OllamaDecider(model=args.model, base_url=args.base_url,
                    system_prompt=COCKPIT_SYSTEM_PROMPT, timeout=45, max_attempts=1,
                    max_output_tokens=2048, keep_alive='30m'),
                enable_control=True, opener=fixture.java, clock=lambda: 1000,
                allowed_drone_sns=['uav-sn-1'])
            server = build_standalone_server(fixture.gateway, '127.0.0.1', 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            def http_call(path, body):
                request = Request(f'http://127.0.0.1:{server.server_port}/api/flight-control'+path,
                    data=json.dumps(body).encode(), headers={**fixture.headers,'Content-Type':'application/json'})
                with urlopen(request, timeout=60) as response:
                    return SimpleNamespace(payload=json.load(response), status_code=response.status)
            fixture.call = http_call
            proposal = fixture.call('/agent/messages',
                {'text': phrase, 'request_id': 'bench-1', 'context_version': 1})
            card = proposal.payload.get('card') or {}
            action = card.get('action') or proposal.payload.get('action')
            row = {'expected': expected, 'action': action, 'status': proposal.payload.get('status'),
                   'before_confirm_posts': len(fixture.java.posts), 'device_requests': 0}
            if row['status'] == 'preview_required' and action == expected:
                confirmed = fixture.call('/agent/confirm', fixture.confirmation(card))
                row.update(result=confirmed.payload.get('status'),
                           device_effect=confirmed.payload.get('device_effect'),
                           fixture_posts=len(fixture.java.posts))
            row['passed'] = row.get('result') == 'submitted' and row['before_confirm_posts'] == 0
            if args.precision_only and expected == 'gimbal_down':
                row.update(error_code=proposal.payload.get('error_code'),
                           parameters=proposal.payload.get('parameters'))
                row['passed'] = (action == expected and row['status'] == 'rejected'
                    and row['error_code'] == 'GIMBAL_EXACT_ANGLE_UNAVAILABLE'
                    and row['parameters'] == {'angle_deg':10} and len(fixture.java.posts) == 0)
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
        finally:
            if server is not None:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
            fixture.doCleanups()
    print(json.dumps({'passed': sum(row['passed'] for row in rows), 'total': len(rows),
        'evidence': ('scripted-model' if args.offline_scripted else 'real-model')+'/synthetic-java/no-device'}))
    return 0 if all(row['passed'] for row in rows) else 1


if __name__ == '__main__':
    raise SystemExit(main())
