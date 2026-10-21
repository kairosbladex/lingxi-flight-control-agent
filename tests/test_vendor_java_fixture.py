"""Replay frozen Java/WebConfig response bytes through the public Python lifecycle.

Only HTTP, clock and UUID randomness are substituted. Java's invalid-request
samples are injected as upstream rejections; Python never emits duplicate JSON
keys or accepts a browser-supplied method as authorization.
"""
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from lingxi_flight_control.full_control import RuntimeBinding
from lingxi_flight_control.ktd_platform import (
    KtdHttpTransport, KtdPlatformError, KtdPlatformExecutor, SqliteCommandJournal,
)
from lingxi_flight_control.ktd_snapshot import KtdSnapshotProvider
from lingxi_flight_control.vendor_control import VendorControlRequest, VENDOR_METHODS


FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'ktd-vendor-http-v1.json'
FIXTURE_SHA = 'c73c7be750b3f3661718c3730f267ec96a84c64afaa622aaeb734159453ca5b2'


class RawJavaBoundary:
    def __init__(self, snapshot, case):
        self.snapshot, self.case, self.requests = snapshot, case, []

    def open(self, request, *, timeout):
        self.requests.append(request)
        path = urlsplit(request.full_url).path
        if path.endswith('/snapshot'):
            record = self.snapshot['http']['response']
            expected_query = {key: [value] for key, value in self.snapshot['http']['request']['query'].items()}
            assert request.get_method() == 'GET'
            assert parse_qs(urlsplit(request.full_url).query) == expected_query
        elif path.endswith('/vendor-request'):
            assert request.get_method() == 'POST'
            actual = json.loads(request.data)
            expected = json.loads(self.case['submission']['request']['body_utf8'])
            # Even duplicate-key rejection fixtures never make Python send duplicate keys.
            if self.case['case_id'] == 'duplicate-key':
                expected['method'] = self.case['method']
            assert actual == expected, (actual, expected)
            record = self.case['submission']['response']
        else:
            assert path.endswith('/vendor-receipt') and request.get_method() == 'GET'
            # Bind the method to the persisted attempt, including Java's wrong-method sample.
            expected_query = {'commandId': [self.case['expected']['command_id']],
                'method': [self.case['method']], 'siteId': [self.snapshot['expected_binding']['site_id']],
                'hangarId': [self.snapshot['expected_binding']['hangar_id']],
                'droneId': [self.snapshot['expected_binding']['drone_id']]}
            task = self.snapshot['expected_binding']['task_id']
            if task is not None:
                expected_query['taskId'] = [task]
            assert parse_qs(urlsplit(request.full_url).query) == expected_query
            record = self.case['response']
        raw = record['body_utf8'].encode('utf-8')
        assert len(raw) == record['body_bytes']
        assert sha256(raw).hexdigest() == record['body_sha256']
        result = BytesIO(raw)
        result.code = record['http_status']
        return result


class JavaVendorFixtureTests(unittest.TestCase):
    def test_all_frozen_http_samples_cross_snapshot_confirmation_and_durable_journal(self):
        raw = FIXTURE.read_bytes()
        self.assertEqual(len(raw), 90137)
        self.assertEqual(sha256(raw).hexdigest(), FIXTURE_SHA)
        fixture = json.loads(raw)
        self.assertEqual(fixture['production_java_sha'], 'af034e1b16512a67d96d57ef2f12c7518c98a898')
        self.assertEqual(len(fixture['cases']), 13)
        snapshots = {snapshot['id']: snapshot for snapshot in fixture['snapshots']}
        for case in fixture['cases']:
            with self.subTest(case=case['case_id']), tempfile.TemporaryDirectory() as directory:
                snapshot = snapshots[case['snapshot_ref']]
                bound = snapshot['expected_binding']
                now = [snapshot['clock_seconds']]
                binding = RuntimeBinding(bound['actor_id'], bound['project_id'], bound['hangar_id'],
                                         bound['drone_id'], 'synthetic-batch', bound['context_version'])
                http = RawJavaBoundary(snapshot, case)
                transport = KtdHttpTransport('https://platform.invalid/api', token='synthetic-token',
                                             actor_id=binding.actor_id, opener=http)
                provider = KtdSnapshotProvider(binding, site_id=bound['site_id'], task_id=bound['task_id'],
                                                transport=transport, clock=lambda: now[0])
                journal = SqliteCommandJournal(Path(directory) / 'commands.sqlite3')
                executor = KtdPlatformExecutor(binding, site_id=bound['site_id'], task_id=bound['task_id'],
                    transport=transport, snapshot_provider=provider, journal=journal, clock=lambda: now[0],
                    enable_vendor_requests=True, allowed_vendor_methods=VENDOR_METHODS)
                expected = case['expected']
                request = VendorControlRequest(expected['command_id'], expected['idempotency_key'],
                                                case['method'], bound['context_version'])
                with patch('lingxi_flight_control.ktd_platform.uuid4', side_effect=[
                    SimpleNamespace(hex='synthetic-confirmation'), SimpleNamespace(hex=expected['trace_id'])]):
                    card = executor.preview_vendor_request(request)
                self.assertEqual(card.status, 'preview_required', card.to_dict())
                args = {key: card.to_dict()[key] for key in ('confirmation_id', 'command_id',
                    'idempotency_key', 'confirmation_binding', 'context_version')}
                submitted = executor.confirm(**args)
                code = json.loads(case['submission']['response']['body_utf8'])['code']
                local_status = 'rejected' if 400 <= code < 500 else ('unknown' if code != 200
                    or expected['status'] == 'unknown' else 'submitted')
                self.assertEqual(submitted.status, local_status)
                if case['request']['http_method'] == 'GET':
                    now[0] = case['clock_seconds']
                    if expected['code'] == 200:
                        report = executor.query_vendor_receipt(request.command_id,
                            idempotency_key=request.idempotency_key, context_version=request.context_version)
                        actual = json.loads(case['response']['body_utf8'])['data']
                        self.assertEqual(report, {**actual, 'context_version': request.context_version})
                        self.assertEqual(report['status'], expected['status'])
                        self.assertEqual(report['result_code'], expected['result_code'])
                        self.assertEqual(report['vendor_authority'], 'unknown')
                    else:
                        with self.assertRaisesRegex(KtdPlatformError, '^VENDOR_RECEIPT_UPSTREAM_REJECTED$'):
                            executor.query_vendor_receipt(request.command_id,
                                idempotency_key=request.idempotency_key, context_version=request.context_version)
                self.assertEqual(executor.confirm(**args).status, local_status)
                self.assertEqual(sum(request.get_method() == 'POST' for request in http.requests), 1)
                self.assertFalse(executor.confirm(**args).device_effect_verified)


if __name__ == '__main__':
    unittest.main()
