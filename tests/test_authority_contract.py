"""V1.4 权威快照字段契约：接受、拒绝与确认卡失效。

全部使用合成夹具与注入快照，不读取网络、不连接平台。
"""
import json
from pathlib import Path
import tempfile
import unittest

from lingxi_flight_control.authority_contract import (
    AuthorityContractError, audit_snapshot, require_authority, requirements_for,
)
from lingxi_flight_control.full_control import (
    FlightControlActionRequest, FlightControlStatus, RuntimeBinding,
)
from lingxi_flight_control.ktd_platform import (
    KtdHttpResponse, KtdPlatformError, KtdPlatformExecutor, KtdSnapshot, SqliteCommandJournal,
)

FIXTURE = Path(__file__).resolve().parent.parent / 'fixtures' / 'v14-authority-contract-v1.json'


def load_fixture():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


BASE_STATE = {
    'online': True, 'airborne': True, 'longitude': 113.2, 'latitude': 23.1,
    'altitude_m': 50.0, 'height_m': 30.0, 'yaw_deg': 90.0, 'battery_percent': 90.0,
    'flight_status': 'FLYING', 'route_status': 'running',
}


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.response = KtdHttpResponse(200, {'code': 200})

    def submit(self, command, context):
        self.calls.append((command, context))
        return self.response


def build_snapshot(*, now=1000.0, state=None, with_extensions=True, **overrides):
    fixture_state = load_fixture()['state'] if with_extensions else {}
    values = dict(
        actor_id='actor-1', project_id='project-1', hangar_id='hangar-1', drone_id='drone-1',
        site_id='site-1', task_id='java-task-1', context_version=1, version='a' * 64,
        observed_at=now, control_owner='actor-1', profile='DJI_HANGAR',
        control_lease_expires_at=now + 270,
        permissions=frozenset({'read_state', 'move_forward', 'route_pause', 'hover', 'camera_zoom'}),
        capabilities={'move_forward': True, 'route_pause': True, 'hover': True,
                      'camera_zoom': True, 'UAV_MOVE_DISTANCE': True},
        state={**BASE_STATE, **fixture_state, **(state or {})},
        task_batch_id='batch-1',
        action_availability={
            'read_state': {'available': True, 'reasons': []},
            'move_forward': {'available': True, 'reasons': []},
            'route_pause': {'available': True, 'reasons': []},
            'hover': {'available': True, 'reasons': []},
            'camera_zoom': {'available': True, 'reasons': []},
        },
    )
    values.update(overrides)
    return KtdSnapshot(**values)


class AuthorityAuditTests(unittest.TestCase):
    def test_every_v14_field_is_present_with_declared_status(self):
        expected = load_fixture()['expected_status']
        audit = audit_snapshot(build_snapshot())
        for name, status in expected.items():
            with self.subTest(field=name):
                self.assertEqual(audit.status(name), status, audit.as_dict())

    def test_batch_echo_absent_is_reported_as_unbound_not_as_platform_fact(self):
        audit = audit_snapshot(build_snapshot(task_batch_id=None))
        self.assertEqual(audit.status('task_batch_id'), 'unbound')
        self.assertIn('task_batch_id', audit.as_dict()['incomplete'])

    def test_unknown_values_are_reported_and_block_their_actions(self):
        snapshot = build_snapshot(state={'route_status': None, 'flight_status': None})
        audit = audit_snapshot(snapshot)
        self.assertEqual(audit.status('route_status'), 'unknown')
        for action in ('move_forward', 'route_pause', 'hover'):
            with self.subTest(action=action):
                with self.assertRaises(AuthorityContractError) as caught:
                    require_authority(snapshot, action)
                self.assertEqual(caught.exception.code, 'AUTHORITATIVE_STATE_INCOMPLETE')
                self.assertTrue(caught.exception.fields)

    def test_unknown_route_status_alone_blocks_micro_and_temporary_actions(self):
        # flight_status 明确，只有 route_status 为 null：仍不能证明“不在自动航线”，
        # 因此 V1.4 2.2.3 门禁无法判定，必须 fail-closed 而不是放行。
        snapshot = build_snapshot(state={'route_status': None})
        for action in ('move_forward', 'turn_left', 'point_flight', 'point_orbit'):
            with self.subTest(action=action):
                with self.assertRaises(AuthorityContractError) as caught:
                    require_authority(snapshot, action)
                self.assertEqual(caught.exception.code, 'AUTHORITATIVE_STATE_INCOMPLETE')
                self.assertEqual(caught.exception.fields, ('route_status',))

    def test_absent_extension_fields_do_not_masquerade_as_verified(self):
        snapshot = build_snapshot(state=dict(BASE_STATE), with_extensions=False)
        audit = audit_snapshot(snapshot)
        self.assertEqual(audit.status('route_id'), 'absent')
        self.assertEqual(audit.status('device_limits'), 'absent')
        # 旧快照没有 route_id 时仍可执行，但审计必须显示缺失，不能显示 present。
        require_authority(snapshot, 'move_forward')

    def test_requirements_are_typed_per_action(self):
        # 只读展示不要求任何字段，但也不授予写权限。
        self.assertEqual(requirements_for('read_state').core, ())
        self.assertEqual(requirements_for('control_acquire').core, ('online',))
        self.assertIn('airborne', requirements_for('move_forward').core)
        self.assertIn('route_status', requirements_for('route_pause').core)
        self.assertIn('route_id', requirements_for('route_pause').extension)
        self.assertIn('device_limits', requirements_for('camera_zoom').extension)
        with self.assertRaises(AuthorityContractError):
            requirements_for('')

    def test_legacy_quality_does_not_claim_route_or_flight_status(self):
        snapshot = build_snapshot(state={'route_status': None, 'flight_status': None},
                                  authorization_quality='legacy-read-api')
        # 旧只读接口不提供这些事实：不升级成已验证，也不冒充权威拒绝原因。
        require_authority(snapshot, 'route_pause')


class SnapshotProviderFieldTests(unittest.TestCase):
    """新增字段必须由 provider 真正校验，而不是被静默忽略。"""

    def setUp(self):
        from lingxi_flight_control.ktd_snapshot import KtdSnapshotProvider
        self.provider_class = KtdSnapshotProvider
        self.now = 1000.0
        self.binding = RuntimeBinding('actor-1', 'project-1', 'hangar-1', 'drone-1', 'batch-1', 7)

    def payload(self):
        fixture = load_fixture()
        return {'code': 200, 'success': True, 'data': {
            'schema_version': 'lingxi-platform-snapshot/v1', 'snapshot_version': 'a' * 64,
            'generated_at': self.now, 'expires_at': self.now + 4,
            'actor_employee_id': 'actor-1', 'project_id': 'project-1', 'site_id': 'site-1',
            'hangar_id': 'hangar-1', 'drone_id': 'drone-1', 'task_id': 'java-task-1',
            'control_owner_employee_id': 'actor-1', 'control_owner_updated_at': self.now - 30,
            'control_lease_expires_at': self.now + 270, 'control_owner_version': 'b' * 64,
            'profile': 'DJI_HANGAR',
            'telemetry': {'sampled_at': self.now - 1, 'received_at': self.now - .5,
                          'source': 'java-udp-cache', 'sequence': None, 'version': 'c' * 64},
            'state': {**BASE_STATE, **fixture['state']},
            'capabilities': {'read_state': True, 'move_forward': True, 'UAV_MOVE_DISTANCE': True},
            'permissions': ['read_state', 'move_forward'],
            'action_availability': {
                'read_state': {'available': True, 'reasons': []},
                'move_forward': {'available': True, 'reasons': []},
            },
        }}

    def transport(self, payload):
        class _Transport:
            actor_id = 'actor-1'

            def read_snapshot(_self, binding, *, site_id, task_id):
                return KtdHttpResponse(200, payload)
        return _Transport()

    def provider(self, payload):
        return self.provider_class(self.binding, site_id='site-1', task_id='java-task-1',
                                   transport=self.transport(payload), clock=lambda: self.now)

    def test_new_fields_are_accepted_and_carried_into_the_snapshot(self):
        snapshot = self.provider(self.payload())()
        self.assertEqual(snapshot.state['route_id'], 'route-main-1')
        self.assertEqual(snapshot.state['breakpoint_index'], 7)
        self.assertEqual(snapshot.state['temporary_task_id'], 'temp-task-1')
        self.assertEqual(snapshot.state['task_type'], 'inspection')
        self.assertEqual(snapshot.state['device_limits']['camera_zoom'], {'min': 1, 'max': 30})
        self.assertEqual(snapshot.task_batch_id, 'batch-1')

    def test_negative_field_cases_are_rejected_with_stable_codes(self):
        for case in load_fixture()['negative']:
            with self.subTest(case=case['name']):
                payload = self.payload()
                payload['data']['state'].update(case['state'])
                if case['state'].get('route_id', 'missing') is None:
                    payload['data']['state']['route_id'] = None
                with self.assertRaises(KtdPlatformError) as caught:
                    self.provider(payload)()
                self.assertEqual(str(caught.exception), case['error'])

    def test_missing_base_state_key_is_incomplete_not_silently_defaulted(self):
        case = load_fixture()['missing_state_key']
        payload = self.payload()
        payload['data']['state'].pop(case['state_without'])
        with self.assertRaises(KtdPlatformError) as caught:
            self.provider(payload)()
        self.assertEqual(str(caught.exception), case['error'])

    def test_version_and_binding_changes_are_rejected(self):
        for mutate, code in (
            (lambda data: data.update(snapshot_version='not-a-version'), 'INVALID_SNAPSHOT_RESPONSE'),
            (lambda data: data.update(drone_id='drone-2'), 'RESOURCE_BINDING_MISMATCH'),
            (lambda data: data.update(task_id='other-task'), 'RESOURCE_BINDING_MISMATCH'),
            (lambda data: data.update(actor_employee_id='actor-2'), 'RESOURCE_BINDING_MISMATCH'),
            (lambda data: data['state'].update(task_batch_id='batch-2'), 'RESOURCE_BINDING_MISMATCH'),
        ):
            with self.subTest(code=code):
                payload = self.payload()
                mutate(payload['data'])
                with self.assertRaises(KtdPlatformError) as caught:
                    self.provider(payload)()
                self.assertEqual(str(caught.exception), code)


class ExecutorAuthorityTests(unittest.TestCase):
    """执行器侧：unknown 阻断、确认哈希包含新字段、状态变化使旧卡失效。"""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.binding = RuntimeBinding('actor-1', 'project-1', 'hangar-1', 'drone-1', 'batch-1', 1)
        self.now = 1000.0
        self.transport = FakeTransport()
        self.snapshot = build_snapshot(now=self.now)

    def executor(self, snapshot=None):
        self.snapshot = snapshot or self.snapshot
        return KtdPlatformExecutor(
            self.binding, site_id='site-1', task_id='java-task-1', transport=self.transport,
            snapshot_provider=lambda: self.snapshot,
            journal=SqliteCommandJournal(self.directory.name + '/commands.sqlite3'),
            enabled=True, clock=lambda: self.now)

    def request(self, action='move_forward', parameters=None, key='key-1'):
        parameters = {'distance_m': 10} if parameters is None else parameters
        return FlightControlActionRequest('cmd-1', key, action, parameters, 1)

    def test_unknown_flight_facts_block_before_any_dispatch(self):
        snapshot = build_snapshot(now=self.now, state={'flight_status': None})
        receipt = self.executor(snapshot).preview(self.request())
        self.assertEqual(receipt.status, FlightControlStatus.REJECTED)
        self.assertEqual(receipt.error_code, 'AUTHORITATIVE_STATE_INCOMPLETE')
        self.assertEqual(self.transport.calls, [])

    def test_route_id_participates_in_the_confirmation_binding(self):
        executor = self.executor()
        card = executor.preview(self.request('route_pause', {}))
        self.assertEqual(card.status, FlightControlStatus.PREVIEW_REQUIRED, card.to_dict())
        # 航线绑定变化（同一 route_status、不同 route_id）必须让旧卡失效。
        self.snapshot = build_snapshot(now=self.now, state={'route_id': 'route-main-2'})
        receipt = executor.confirm(card.confirmation_id, command_id='cmd-1', idempotency_key='key-1',
                                   confirmation_binding=card.confirmation_binding, context_version=1)
        self.assertEqual(receipt.status, FlightControlStatus.REJECTED)
        self.assertEqual(receipt.error_code, 'AUTHORITATIVE_STATE_CHANGED')
        self.assertEqual(self.transport.calls, [])

    def test_breakpoint_change_invalidates_the_card(self):
        executor = self.executor()
        card = executor.preview(self.request('route_pause', {}))
        self.snapshot = build_snapshot(now=self.now, state={'breakpoint_index': 8})
        receipt = executor.confirm(card.confirmation_id, command_id='cmd-1', idempotency_key='key-1',
                                   confirmation_binding=card.confirmation_binding, context_version=1)
        self.assertEqual(receipt.status, FlightControlStatus.REJECTED)
        self.assertEqual(receipt.error_code, 'AUTHORITATIVE_STATE_CHANGED')

    def test_low_battery_and_mapping_flags_invalidate_the_card(self):
        for change in ({'low_battery_return': True}, {'mapping_invalidated': True},
                       {'auto_stitching': False}, {'temporary_task_id': 'temp-9'}):
            with self.subTest(change=change):
                self.directory.cleanup()
                self.directory = tempfile.TemporaryDirectory()
                self.addCleanup(self.directory.cleanup)
                executor = self.executor(build_snapshot(now=self.now))
                card = executor.preview(self.request('route_pause', {}))
                self.assertEqual(card.status, FlightControlStatus.PREVIEW_REQUIRED)
                self.snapshot = build_snapshot(now=self.now, state=change)
                receipt = executor.confirm(card.confirmation_id, command_id='cmd-1',
                                           idempotency_key='key-1',
                                           confirmation_binding=card.confirmation_binding,
                                           context_version=1)
                self.assertEqual(receipt.status, FlightControlStatus.REJECTED, change)
                self.assertEqual(receipt.error_code, 'AUTHORITATIVE_STATE_CHANGED', change)

    def test_task_type_change_to_mapping_is_blocked_before_dispatch(self):
        executor = self.executor(build_snapshot(now=self.now))
        card = executor.preview(self.request('route_pause', {}))
        self.snapshot = build_snapshot(now=self.now, state={'task_type': 'mapping'})
        receipt = executor.confirm(card.confirmation_id, command_id='cmd-1', idempotency_key='key-1',
                                   confirmation_binding=card.confirmation_binding, context_version=1)
        self.assertEqual(receipt.status, FlightControlStatus.REJECTED)
        # V1.4 P1：preview/confirm 之间任务类型变成测绘，快照哈希先失配，确认卡作废。
        # 顺序上不会走到测绘作废网关（不可逆副作用），也不会下发。
        self.assertEqual(receipt.error_code, 'AUTHORITATIVE_STATE_CHANGED')
        self.assertEqual(self.transport.calls, [])

    def test_mapping_task_without_atomic_contract_is_blocked_before_dispatch(self):
        snapshot = build_snapshot(now=self.now, state={'task_type': 'mapping'})
        executor = self.executor(snapshot)
        card = executor.preview(self.request('route_pause', {}))
        self.assertEqual(card.status, FlightControlStatus.REJECTED, card.to_dict())
        # 测绘任务中断在平台缺少原子契约时预览即拒，也不会下发。
        self.assertEqual(card.error_code, 'MAPPING_INTERRUPTION_UNAVAILABLE')
        self.assertEqual(self.transport.calls, [])

    def test_authority_contract_report_is_read_only_and_explains_blocks(self):
        executor = self.executor(build_snapshot(now=self.now, state={'route_status': None}))
        report = executor.authority_contract('route_pause')
        self.assertTrue(report['available'])
        self.assertTrue(report['blocked'])
        self.assertEqual(report['error_code'], 'AUTHORITATIVE_STATE_INCOMPLETE')
        self.assertIn('route_status', report['unknown_fields'])
        self.assertEqual(self.transport.calls, [])

    def test_batch_echo_mismatch_is_rejected_before_compile(self):
        snapshot = build_snapshot(now=self.now, task_batch_id='batch-other')
        receipt = self.executor(snapshot).preview(self.request())
        self.assertEqual(receipt.status, FlightControlStatus.REJECTED)
        self.assertEqual(receipt.error_code, 'RESOURCE_BINDING_MISMATCH')


if __name__ == '__main__':
    unittest.main()
