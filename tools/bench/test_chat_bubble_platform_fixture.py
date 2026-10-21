#!/usr/bin/env python3
"""Explicit loopback integration test; deliberately outside offline discover.

Run with PYTHONPATH=src python3 tools/bench/test_chat_bubble_platform_fixture.py.
Only the fixture's child process and its two owned loopback listeners are used.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import unittest
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[2]
MARKER = 'lingxi-chat-bubble-platform-fixture/v1'
CONFIRM = ('confirmation_id', 'confirmation_binding', 'command_id', 'idempotency_key', 'context_version')


class ChatBubblePlatformFixtureTest(unittest.TestCase):
    def test_clarification_confirmation_java_receipt_and_no_replay(self):
        environment = dict(os.environ)
        # These intentionally unusable values must never configure the fixture.
        environment.update(KTD_BASE_URL='https://production.invalid/forbidden',
                           KTD_SERVER_TOKEN='DO-NOT-USE-ENVIRONMENT-CREDENTIAL',
                           FLIGHT_AGENT_GATEWAY_TOKEN='DO-NOT-USE-ENVIRONMENT-GATEWAY')
        process = subprocess.Popen(
            [sys.executable, str(ROOT / 'tools/bench/chat_bubble_platform_fixture.py'), '--duration', '60'],
            cwd=ROOT, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        client = build_opener(ProxyHandler({}))
        try:
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(timeout=10), 'fixture did not announce readiness')
            line = process.stdout.readline()
            if not line:
                self.fail('fixture failed to start: ' + process.stderr.read()[-2000:])
            startup = json.loads(line)
            self.assertEqual(startup['fixture_marker'], MARKER)
            self.assertNotIn('DO-NOT-USE', line)
            base = startup['agent_url']
            self.assertTrue(base.startswith('http://127.0.0.1:'))
            self.assertTrue(base.endswith('/api/flight-control'))

            def request(path, body=None, *, session='bubble-contract'):
                req = Request(base + path,
                    data=None if body is None else json.dumps(body).encode(),
                    headers={'Content-Type': 'application/json', 'X-Agent-Session': session},
                    method='GET' if body is None else 'POST')
                try:
                    response = client.open(req, timeout=5)
                except HTTPError as error:
                    response = error
                with response:
                    return response.code, json.load(response)

            def evidence():
                with client.open(startup['evidence_url'], timeout=5) as response:
                    return json.load(response)

            def message(text):
                code, value = request('/agent/messages',
                    {'text': text, 'request_id': uuid4().hex, 'context_version': 1})
                self.assertEqual(code, 200)
                return value

            self.assertEqual(evidence()['counts']['java_control_posts'], 0)
            code, context = request('/context')
            self.assertEqual(code, 200)
            self.assertEqual(context['execution_mode'], 'platform')
            self.assertEqual(context['context']['drone_id'], 'bubble-drone')
            self.assertEqual(request('/agent/messages', ['invalid-non-object'])[0], 400)
            self.assertEqual(message('向前飞')['status'], 'clarification')
            proposal = message('10米')
            self.assertEqual(proposal['status'], 'preview_required')
            card = proposal['card']
            self.assertEqual(card['action'], 'move_forward')
            self.assertEqual(card['parameters'], {'distance_m': 10})
            self.assertEqual(evidence()['counts']['java_control_posts'], 0)
            self.assertEqual(request('/agent/messages', ['invalid-with-pending-card'])[0], 400)
            confirmation = {key: card[key] for key in CONFIRM}
            for field in CONFIRM:
                modified = {**confirmation, field: 2 if field == 'context_version' else 'wrong-binding'}
                code, rejected = request('/agent/confirm', modified)
                self.assertIn(code, (200, 409))
                self.assertIn(rejected['status'], ('rejected', 'stale_context'))
            self.assertEqual(request('/agent/confirm', {**confirmation, 'extra': True})[0], 400)
            self.assertEqual(request('/agent/confirm', confirmation, session='other-tab')[1]['status'], 'rejected')
            self.assertEqual(evidence()['counts']['java_control_posts'], 0)
            models_before = evidence()['counts']['model_calls']
            code, submitted = request('/agent/confirm', confirmation)
            self.assertEqual(code, 200)
            self.assertEqual(submitted['status'], 'submitted')
            self.assertEqual(submitted['device_effect'], 'not_verified')
            self.assertEqual(evidence()['counts']['java_control_posts'], 1)
            self.assertEqual(request('/agent/confirm', confirmation)[1], submitted)
            receipt_path = '/agent/receipt?' + urlencode({'command_id': card['command_id'], 'context_version': 1})
            code, receipt = request(receipt_path)
            self.assertEqual(code, 200)
            self.assertEqual(receipt['reported_status'], 'completed')
            self.assertEqual(receipt['device_effect'], 'not_verified')
            self.assertEqual(len(receipt), 13)
            self.assertEqual(request(receipt_path)[1], receipt)
            self.assertEqual(evidence()['counts']['java_control_posts'], 1)
            self.assertEqual(evidence()['counts']['model_calls'], models_before)

            cancelled_card = message('向前飞10米')['card']
            cancelled = {key: cancelled_card[key] for key in CONFIRM}
            self.assertEqual(request('/agent/cancel', cancelled)[1]['status'], 'cancelled')
            self.assertEqual(request('/agent/confirm', cancelled)[1]['status'], 'rejected')
            reset_card = message('向前飞10米')['card']
            self.assertEqual(request('/agent/reset', {'context_version': 1})[1]['status'], 'cancelled')
            self.assertEqual(request('/agent/confirm', {key: reset_card[key] for key in CONFIRM})[1]['status'], 'rejected')
            self.assertEqual(request('/state')[0], 200)
            final = evidence()
            self.assertEqual(final['counts']['java_control_posts'], 1)
            self.assertEqual(final['counts']['live_model_requests'], 0)
            self.assertEqual(final['counts']['external_platform_requests'], 0)
            self.assertEqual(final['counts']['physical_device_requests'], 0)
            self.assertEqual(final['network']['blocked'], 0)
            self.assertTrue(final['source_freeze']['stable'])
            java_post = final['java_control_posts'][0]
            self.assertTrue(java_post['valid'])
            self.assertEqual(java_post['path'], '/api/screen/uav/operation/control')
            self.assertEqual(java_post['body']['param'], 10)
            self.assertEqual(java_post['body']['tracking']['commandId'], card['command_id'])
            self.assertEqual(java_post['body']['tracking']['traceId'], submitted['trace_id'])
            successful = [call for call in final['agent_http_calls']
                if call['path'].endswith('/confirm') and call['response_status'] == 'submitted']
            self.assertEqual(len(successful), 2)
            self.assertTrue(all(call['confirmation_matches_preview'] for call in successful))
            self.assertTrue(all(call['confirmation_fields'] == sorted(CONFIRM) for call in successful))
            self.assertNotIn('DO-NOT-USE', json.dumps(final))
        finally:
            process.terminate()
            process.wait(timeout=10)
            process.stdout.close()
            process.stderr.close()
            client.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
