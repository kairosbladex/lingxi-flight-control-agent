"""Durable tracked requests through the real transport and an offline HTTP boundary."""
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

from lingxi_flight_control.full_control import FlightControlActionRequest, RuntimeBinding
from lingxi_flight_control.ktd_platform import KtdHttpTransport, KtdPlatformError, KtdPlatformExecutor, KtdSnapshot, SqliteCommandJournal
from test_command_receipts import command_receipt_payload


class TrackedJavaBoundary:
    def __init__(self):
        self.calls = []
        self.record = None
        self.timeout_after_post = False
        self.query_error = None
        self.raw_query = None

    def open(self, request, *, timeout):
        target = urlsplit(request.full_url)
        body = json.loads(request.data) if request.data else None
        self.calls.append((request.get_method(), target.path, parse_qs(target.query), body))
        if request.get_method() == 'POST':
            assert target.path == '/api/screen/uav/operation/control'
            tracking = body['tracking']
            self.record = command_receipt_payload(command_id=tracking['commandId'],
                idempotency_key=tracking['idempotencyKey'], trace_id=tracking['traceId'],
                task_id='java-task-1', submitted_at=1000, updated_at=1000,
                parameters={'distance_m': body['param']})
            if self.timeout_after_post:
                raise TimeoutError('private-upstream-detail')
        else:
            assert target.path == '/api/lingxi/agent/commands/receipt'
            if self.query_error:
                raise self.query_error
        response = BytesIO(self.raw_query if request.get_method() == 'GET' and self.raw_query is not None
                           else json.dumps(self.record).encode())
        response.code = 200
        return response


class TrackedCommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'commands.db'
        self.binding = RuntimeBinding('actor-1', 'project-1', 'hangar-1', 'drone-1', 'batch-1', 7)
        self.now = 1000.0
        self.boundary = TrackedJavaBoundary()
        self.transport = KtdHttpTransport('https://platform.test/api', token='synthetic-token',
            actor_id='actor-1', opener=self.boundary)
        self.snapshot = KtdSnapshot('actor-1', 'project-1', 'hangar-1', 'drone-1', 'site-1',
            'java-task-1', 7, 'a' * 64, 1000, 'actor-1', 'DJI_HANGAR',
            frozenset({'move_forward', 'read_state'}), {'move_forward': True, 'UAV_MOVE_DISTANCE': True},
            {'online': True, 'airborne': True, 'longitude': 113, 'latitude': 23,
             'altitude_m': 60, 'height_m': 50, 'yaw_deg': 90}, 1060)
        self.request = FlightControlActionRequest('command-1', 'key-1', 'move_forward', {'distance_m': 10}, 7)

    def executor(self, **changes):
        return KtdPlatformExecutor(self.binding, site_id='site-1', task_id='java-task-1',
            transport=self.transport, snapshot_provider=lambda: self.snapshot,
            journal=SqliteCommandJournal(self.path), enabled=True, track_commands=True,
            clock=lambda: self.now, **changes)

    def submit(self, executor):
        card = executor.preview(self.request)
        self.assertEqual(card.to_dict()['status'], 'preview_required')
        self.assertTrue(card.receipt_tracking)
        result = executor.confirm(card.confirmation_id, command_id=card.command_id,
            idempotency_key=card.idempotency_key, confirmation_binding=card.confirmation_binding,
            context_version=7)
        return card, result

    def query(self, executor):
        return executor.query_receipt('command-1', idempotency_key='key-1', context_version=7)

    def test_tracking_metadata_comes_from_the_confirmed_binding_and_survives_restart(self):
        executor = self.executor()
        card, result = self.submit(executor)
        self.assertEqual(result.to_dict()['status'], 'submitted')
        body = self.boundary.calls[0][3]
        self.assertEqual({key: body[key] for key in ('terminalId', 'siteId', 'taskId', 'mode', 'type', 'param')},
                         {'terminalId': 'drone-1', 'siteId': 'site-1', 'taskId': 'java-task-1', 'mode': 2, 'type': 202, 'param': 10})
        self.assertEqual(body['tracking'], {'schemaVersion': 'lingxi-command-request/v1',
            'commandId': 'command-1', 'idempotencyKey': 'key-1', 'traceId': card.trace_id,
            'snapshotVersion': 'a' * 64, 'expiresAt': 1005, 'hangarId': 'hangar-1'})
        report = self.query(self.executor())
        self.assertEqual(report['status'], 'submitted')
        self.assertEqual(report['command_id'], 'command-1')
        self.assertEqual(report['context_version'], 7)
        self.assertNotIn('dispatch_ids', report)
        self.assertEqual(self.boundary.calls[-1][2], {'commandId': ['command-1'], 'siteId': ['site-1'],
            'hangarId': ['hangar-1'], 'droneId': ['drone-1'], 'taskId': ['java-task-1']})
        replay = self.executor().confirm(card.confirmation_id, command_id=card.command_id,
            idempotency_key=card.idempotency_key, confirmation_binding=card.confirmation_binding, context_version=7)
        self.assertEqual(replay.to_dict()['status'], 'submitted')
        self.assertEqual(sum(method == 'POST' for method, *_ in self.boundary.calls), 1)

    def test_unknown_post_outcome_is_queried_without_any_retransmission(self):
        self.boundary.timeout_after_post = True
        executor = self.executor()
        _, result = self.submit(executor)
        self.assertEqual(result.to_dict()['status'], 'unknown')
        self.now = 1002
        self.boundary.record['data'].update(status='completed', version=3, reported_at=1001,
                                            received_at=1001, updated_at=1001)
        report = self.query(self.executor())
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['device_effect'], 'not_verified')
        self.assertEqual(sum(method == 'POST' for method, *_ in self.boundary.calls), 1)

    def test_queries_require_a_persisted_send_and_matching_context_and_request(self):
        executor = self.executor()
        with self.assertRaises(KtdPlatformError):
            self.query(executor)
        self.assertEqual(self.boundary.calls, [])
        self.submit(executor)
        before = len(self.boundary.calls)
        for command, key, version in (('other', 'key-1', 7), ('command-1', 'other', 7),
                                       ('command-1', 'key-1', 8), ('command-1', 'key-1', True)):
            with self.subTest(command=command, key=key, version=version), self.assertRaises(KtdPlatformError):
                executor.query_receipt(command, idempotency_key=key, context_version=version)
        self.binding = replace(self.binding, task_batch_id='other-batch')
        with self.assertRaises(KtdPlatformError):
            self.query(self.executor())
        self.assertEqual(len(self.boundary.calls), before)

    def test_unverifiable_submission_metadata_preserves_unknown_in_the_journal(self):
        class WrongIdentity(TrackedJavaBoundary):
            def open(inner, request, *, timeout):
                super().open(request, timeout=timeout).close()
                inner.record['data']['command_id'] = 'other'
                response = BytesIO(json.dumps(inner.record).encode())
                response.code = 200
                return response
        self.boundary = WrongIdentity()
        self.transport = KtdHttpTransport('https://platform.test/api', token='synthetic-token',
                                         actor_id='actor-1', opener=self.boundary)
        _, result = self.submit(self.executor())
        self.assertEqual(result.to_dict()['status'], 'unknown')
        with self.assertRaises(KtdPlatformError):
            self.query(self.executor())
        self.assertEqual(sum(method == 'POST' for method, *_ in self.boundary.calls), 1)

    def test_query_errors_redact_details_and_never_change_submission_or_resend(self):
        executor = self.executor()
        card, _ = self.submit(executor)
        for failure in (TimeoutError('private-upstream-detail'), RuntimeError('private-upstream-detail')):
            self.boundary.query_error = failure
            with self.assertRaises(KtdPlatformError) as error:
                self.query(executor)
            self.assertNotIn('private-upstream-detail', str(error.exception))
        self.boundary.query_error = None
        self.boundary.raw_query = b'{"code":200,"code":403}'
        with self.assertRaises(KtdPlatformError):
            self.query(executor)
        replay = executor.confirm(card.confirmation_id, command_id=card.command_id,
            idempotency_key=card.idempotency_key, confirmation_binding=card.confirmation_binding, context_version=7)
        self.assertEqual(replay.to_dict()['status'], 'submitted')
        self.assertEqual(sum(method == 'POST' for method, *_ in self.boundary.calls), 1)

    def test_old_or_contradictory_reports_cannot_replace_a_known_device_report(self):
        executor = self.executor()
        self.submit(executor)
        self.now = 1002
        data = self.boundary.record['data']
        data.update(status='completed', version=3, reported_at=1001, received_at=1001, updated_at=1001)
        self.assertEqual(self.query(executor)['status'], 'completed')
        data.update(status='in_progress', version=2)
        with self.assertRaises(KtdPlatformError):
            self.query(executor)
        data.update(status='failed', version=3)
        with self.assertRaises(KtdPlatformError):
            self.query(executor)
        data.update(status='conflict', version=4, error_code='TERMINAL_CONFLICT')
        self.assertEqual(self.query(executor)['status'], 'conflict')

    def test_tracked_mode_cannot_silently_use_an_untracked_profile_or_action(self):
        for profile, action, parameters in (('PZ_HANGAR', 'move_forward', {'distance_m': 10}),
                                             ('DJI_HANGAR', 'camera_photo', {})):
            self.snapshot = replace(self.snapshot, profile=profile, permissions=frozenset({action}),
                capabilities={action: True, 'UAV_MOVE_DISTANCE': True})
            request = FlightControlActionRequest('command', 'key', action, parameters, 7)
            with self.subTest(profile=profile, action=action):
                self.assertEqual(self.executor().preview(request).to_dict()['status'], 'rejected')
        self.assertEqual(self.boundary.calls, [])

    def test_submission_evidence_survives_restart_and_rejects_older_query(self):
        self.submit(self.executor())
        self.boundary.record['data']['version'] = 0
        with self.assertRaisesRegex(KtdPlatformError, 'STALE_COMMAND_RECEIPT'):
            self.query(self.executor())
        self.assertEqual(sum(method == 'POST' for method, *_ in self.boundary.calls), 1)

    def test_restart_cannot_replay_confirmed_receipt_into_a_different_resource_binding(self):
        card, _ = self.submit(self.executor())
        self.binding = replace(self.binding, task_batch_id='other-batch', hangar_id='other-hangar')
        result = self.executor().confirm(card.confirmation_id, command_id=card.command_id,
            idempotency_key=card.idempotency_key, confirmation_binding=card.confirmation_binding,
            context_version=7)
        self.assertEqual(result.to_dict()['status'], 'rejected')
        self.assertEqual(result.error_code, 'CONFIRMATION_MISMATCH')
        self.assertFalse(result.state['available'])
        self.assertEqual(sum(method == 'POST' for method, *_ in self.boundary.calls), 1)

    def test_new_version_cannot_change_command_deadline_or_actual_dispatch(self):
        original = command_receipt_payload()
        changes = ({'expires_at': 1140}, {'submitted_at': 999}, {'dispatch_ids': {
            **original['data']['dispatch_ids'], 'bid': '40000000-0000-4000-8000-000000000001'}})
        for index, change in enumerate(changes):
            with self.subTest(change=change):
                self.path = Path(self.directory.name) / f'immutable-{index}.db'
                self.submit(self.executor())
                self.query(self.executor())
                self.boundary.record['data'].update(version=2, **change)
                with self.assertRaisesRegex(KtdPlatformError, 'COMMAND_RECEIPT_CONFLICT'):
                    self.query(self.executor())

    def test_completed_query_before_post_returns_survives_late_submission_finish(self):
        entered, release = threading.Event(), threading.Event()
        class SlowPost(TrackedJavaBoundary):
            def open(inner, request, *, timeout):
                response = super().open(request, timeout=timeout)
                if request.get_method() == 'POST':
                    entered.set()
                    if not release.wait(3):
                        raise TimeoutError('test barrier timed out')
                return response
        self.boundary = SlowPost()
        self.transport = KtdHttpTransport('https://platform.test/api', token='synthetic-token',
            actor_id='actor-1', opener=self.boundary)
        sending, querying = self.executor(), self.executor()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.submit, sending)
            try:
                self.assertTrue(entered.wait(2))
                self.now = 1002
                self.boundary.record['data'].update(status='completed', version=3,
                    reported_at=1001, received_at=1001, updated_at=1001)
                self.assertEqual(self.query(querying)['status'], 'completed')
            finally:
                release.set()
            card, result = future.result(timeout=2)
        self.assertEqual(result.to_dict()['status'], 'submitted')
        restarted = self.executor()
        replay = restarted.confirm(card.confirmation_id, command_id=card.command_id,
            idempotency_key=card.idempotency_key, confirmation_binding=card.confirmation_binding,
            context_version=7)
        self.assertEqual(replay.to_dict()['status'], 'submitted')
        self.assertEqual(replay.device_report['status'], 'completed')
        self.assertEqual(sum(method == 'POST' for method, *_ in self.boundary.calls), 1)

    def test_parallel_older_query_cannot_overwrite_another_executors_completion(self):
        entered, release = threading.Event(), threading.Event()
        class SlowFirstQuery(TrackedJavaBoundary):
            blocked = False
            def open(inner, request, *, timeout):
                response = super().open(request, timeout=timeout)
                if request.get_method() == 'GET' and not inner.blocked:
                    inner.blocked = True
                    entered.set()
                    if not release.wait(3):
                        raise TimeoutError('test barrier timed out')
                return response
        self.boundary = SlowFirstQuery()
        self.transport = KtdHttpTransport('https://platform.test/api', token='synthetic-token',
            actor_id='actor-1', opener=self.boundary)
        self.submit(self.executor())
        older, newer = self.executor(), self.executor()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.query, older)
            try:
                self.assertTrue(entered.wait(2))
                self.now = 1002
                self.boundary.record['data'].update(status='completed', version=3,
                    reported_at=1001, received_at=1001, updated_at=1001)
                self.assertEqual(self.query(newer)['status'], 'completed')
            finally:
                release.set()
            with self.assertRaisesRegex(KtdPlatformError, 'STALE_COMMAND_RECEIPT'):
                future.result(timeout=2)
        self.assertEqual(self.query(self.executor())['status'], 'completed')
        self.assertEqual(sum(method == 'POST' for method, *_ in self.boundary.calls), 1)


if __name__ == '__main__':
    unittest.main()
