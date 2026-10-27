"""V1.4 4.1.2 / 4.1.3：测绘中断原子契约、临时任务替换与低电量续飞。

模拟器与假传输验证；真实平台的原子接口与测绘数据库事务**未验证**。

``MappingReservationStateMachineTests`` 与 ``CrossProcessReservationTests`` 覆盖
「同一 command_id/idempotency_key/context/binding 在跨进程并发下最多调用一次测绘
网关」：前者用同进程两个执行器，后者用两个独立 Python 进程共享同一个 SQLite
journal（见 ``mapping_race_worker.py``）。
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace

from lingxi_flight_control import FlightControlActionRequest, FlightControlStatus, MemoryFlightControlSimulator, RuntimeBinding
from lingxi_flight_control.flight_policy import MAPPING_MESSAGE, PathCheck
from fence_support import HardBoundedFenceProvider, StaticFenceProvider
from lingxi_flight_control.ktd_platform import (
    KtdHttpResponse, KtdPlatformExecutor, KtdSnapshot, SqliteCommandJournal,
)
from lingxi_flight_control.mapping_interruption import (
    INTERRUPTION_STEPS, MAPPING_INTERRUPTION_FAILED, MAPPING_INTERRUPTION_NOT_CONFIRMED,
    MAPPING_INTERRUPTION_PARTIAL, MAPPING_INTERRUPTION_UNKNOWN,
    MAPPING_INTERRUPTION_UNAVAILABLE,
    NON_INTERRUPTING_PAYLOAD_ACTIONS, MappingInterruptionReceipt, MappingInterruptionRequest,
    RESERVATION_FAILED, RESERVATION_PENDING, RESERVATION_SUCCEEDED, RESERVATION_UNKNOWN,
    SimulatorMappingInterruptionGateway, UnavailableMappingInterruptionGateway,
    audit_summary, interruption_required,
)
from lingxi_flight_control.task_events import (
    TEMPORARY_DECISION_REQUIRED, TEMPORARY_RESUME_UNAVAILABLE, TaskEventError,
    post_temporary_decision, validate_post_temporary_choice,
)


def binding():
    return RuntimeBinding('actor', 'project', 'hangar', 'drone', 'batch', 1)


def mapping_simulator(*, mapping_gateway=None):
    sim = MemoryFlightControlSimulator(binding(), clock=lambda: 1000.0,
                                       path_checker=StaticFenceProvider(PathCheck('clear')),
                                       mapping_gateway=mapping_gateway)
    sim.state.control_owner = 'actor'
    sim.state.route_id = 'route-main'
    sim.state.route_status = 'running'
    sim.state.breakpoint_index = 7
    sim.state.task_status = 'route_executing'
    sim.state.task_type = 'mapping'
    sim.state.auto_stitching = True
    sim.state.rebuild_type = 'oblique'
    sim.state.break_info = {'index': 4, 'kind': 'photo'}
    return sim


class Harness(unittest.TestCase):
    def setUp(self):
        self.counter = 0
        self.sim = mapping_simulator()

    def preview(self, action, **parameters):
        self.counter += 1
        key = str(self.counter)
        return self.sim.preview(FlightControlActionRequest(key, key, action, parameters, 1))

    def confirm(self, card):
        return self.sim.confirm(card.confirmation_id, command_id=card.command_id,
                                idempotency_key=card.idempotency_key,
                                confirmation_binding=card.confirmation_binding,
                                context_version=1)


class PreviewWarningTests(Harness):
    def test_first_preview_warns_and_changes_nothing(self):
        before = self.sim.state.to_dict()
        card = self.preview('move_forward', distance_m=10)
        self.assertIs(card.status, FlightControlStatus.PREVIEW_REQUIRED)
        self.assertIn(MAPPING_MESSAGE, card.warnings)
        # 未确认前不得执行任何动作，测绘状态也不变。
        self.assertEqual(self.sim.state.to_dict(), before)
        self.assertTrue(self.sim.state.auto_stitching)

    def test_payload_actions_are_not_mapping_interruptions(self):
        for action in ('camera_photo', 'camera_start_recording', 'speaker_speak',
                       'searchlight_brightness', 'gimbal_down'):
            with self.subTest(action=action):
                self.assertFalse(interruption_required(action, self.sim.state))
                self.assertIn(action, NON_INTERRUPTING_PAYLOAD_ACTIONS)
        card = self.preview('camera_photo')
        self.assertNotIn(MAPPING_MESSAGE, card.warnings)
        self.assertFalse(self.sim.state.mapping_invalidated)

    def test_payload_actions_do_not_invalidate_mapping(self):
        card = self.preview('camera_photo')
        self.confirm(card)
        self.assertTrue(self.sim.state.auto_stitching)
        self.assertFalse(self.sim.state.mapping_invalidated)
        self.assertEqual(self.sim.state.breakpoint_index, 7)


class AtomicInterruptionTests(Harness):
    def test_confirmed_interruption_runs_all_six_steps_in_order(self):
        card = self.preview('move_forward', distance_m=10)
        receipt = self.confirm(card)
        self.assertIs(receipt.status, FlightControlStatus.ACCEPTED)
        record = receipt.to_dict()['mapping_interruption']
        self.assertTrue(record['atomic'])
        self.assertEqual(record['execution_mode'], 'simulator')
        self.assertEqual(tuple(record['completed_steps']), INTERRUPTION_STEPS)
        # 六步的落地效果。
        self.assertFalse(self.sim.state.auto_stitching)
        self.assertIsNone(self.sim.state.rebuild_type)
        self.assertIsNone(self.sim.state.break_info)
        self.assertIsNone(self.sim.state.breakpoint_index)
        self.assertTrue(self.sim.state.route_resume_disabled)
        self.assertTrue(self.sim.state.mapping_invalidated)
        self.assertEqual(self.sim.state.route_status, 'interrupted')

    def test_interruption_happens_before_the_flight_action(self):
        order = []
        sim = self.sim
        inner = SimulatorMappingInterruptionGateway(lambda: sim.state)

        class Recording(SimulatorMappingInterruptionGateway):
            def interrupt(self, request):
                order.append(('interrupt', sim.state.mapping_invalidated))
                return inner.interrupt(request)

        self.sim._mapping_gateway = Recording(lambda: sim.state)
        card = self.preview('move_forward', distance_m=10)
        pose = (self.sim.state.longitude, self.sim.state.latitude)
        self.confirm(card)
        self.assertEqual(order, [('interrupt', False)])
        self.assertNotEqual((self.sim.state.longitude, self.sim.state.latitude), pose)

    def test_unconfirmed_interruption_does_nothing(self):
        gateway = SimulatorMappingInterruptionGateway(lambda: self.sim.state)
        before = self.sim.state.to_dict()
        receipt = gateway.interrupt(MappingInterruptionRequest(
            command_id='cmd-1', action='move_forward', task_batch_id='batch', confirmed=False))
        self.assertFalse(receipt.atomic)
        self.assertEqual(receipt.error_code, 'MAPPING_INTERRUPTION_NOT_CONFIRMED')
        self.assertEqual(self.sim.state.to_dict(), before)

    def test_unavailable_gateway_blocks_the_flight_action(self):
        sim = mapping_simulator(mapping_gateway=UnavailableMappingInterruptionGateway())
        longitude, latitude = sim.state.longitude, sim.state.latitude
        card = sim.preview(FlightControlActionRequest('c1', 'k1', 'move_forward', {'distance_m': 10}, 1))
        self.assertIs(card.status, FlightControlStatus.PREVIEW_REQUIRED)
        self.assertIn(MAPPING_MESSAGE, card.warnings)
        receipt = sim.confirm(card.confirmation_id, command_id='c1', idempotency_key='k1',
                              confirmation_binding=card.confirmation_binding, context_version=1)
        self.assertIs(receipt.status, FlightControlStatus.REJECTED)
        self.assertEqual(receipt.error_code, MAPPING_INTERRUPTION_UNAVAILABLE)
        # 动作没有执行：位姿与测绘状态都保持原样。
        self.assertEqual((sim.state.longitude, sim.state.latitude), (longitude, latitude))
        self.assertTrue(sim.state.auto_stitching)
        self.assertFalse(sim.state.mapping_invalidated)
        self.assertEqual(sim.mapping_interruption_audit()['execution_mode'], 'unavailable')

    def test_partial_failure_is_reported_not_silently_ignored(self):
        class BrokenState:
            auto_stitching = True
            rebuild_type = 'oblique'
            break_info = None
            breakpoint_index = 7
            route_resume_disabled = False
            mapping_invalidated = False
            route_status = 'running'

            def __setattr__(self, name, value):
                if name == 'breakpoint_index':
                    raise RuntimeError('database unavailable')
                object.__setattr__(self, name, value)

        gateway = SimulatorMappingInterruptionGateway(lambda: BrokenState())
        receipt = gateway.interrupt(MappingInterruptionRequest(
            command_id='cmd-1', action='move_forward', task_batch_id='batch', confirmed=True))
        self.assertFalse(receipt.atomic)
        self.assertEqual(receipt.error_code, MAPPING_INTERRUPTION_PARTIAL)
        self.assertEqual(receipt.failed_step, 'invalidate_task_breakpoint')
        self.assertEqual(receipt.completed_steps,
                         ('stop_auto_stitching', 'void_rebuild_type', 'void_break_info'))

    def test_invalidated_mapping_can_never_be_resumed(self):
        self.confirm(self.preview('move_forward', distance_m=10))
        for action in ('route_resume', 'continue_from_breakpoint'):
            with self.subTest(action=action):
                card = self.preview(action)
                self.assertEqual(card.error_code, 'MAPPING_NOT_RESUMABLE')

    def test_old_card_dies_after_the_interruption(self):
        stale = self.preview('turn_left', angle_deg=30)
        card = self.preview('move_forward', distance_m=10)
        self.confirm(card)
        # 中断前生成的另一张确认卡必须失效，不能在中断之后再执行动作。
        again = self.confirm(stale)
        self.assertIs(again.status, FlightControlStatus.REJECTED)
        self.assertIn(again.error_code,
                      {'POLICY_STATE_CHANGED', 'ROUTE_STATE_CHANGED',
                       'MAPPING_INTERRUPTION_UNAVAILABLE', 'MAPPING_NOT_RESUMABLE'})
        self.assertTrue(self.sim.state.mapping_invalidated)

    def test_low_battery_and_recharge_never_resume_invalidated_mapping(self):
        self.confirm(self.preview('move_forward', distance_m=10))
        self.sim.apply_task_event('low_battery_return', task_batch_id='batch')
        self.sim.apply_task_event('recharged_online', task_batch_id='batch')
        self.assertEqual(self.sim.state.route_status, 'ended')
        self.assertIsNone(self.sim.state.breakpoint_index)


class TemporaryTaskTests(Harness):
    def setUp(self):
        super().setUp()
        self.sim.state.task_type = 'inspection'
        self.sim.state.auto_stitching = False

    def test_new_temporary_task_replaces_old_and_keeps_main_breakpoint(self):
        self.confirm(self.preview('point_flight', longitude=113.0, latitude=23.0, altitude_m=50))
        first = self.sim.state.temporary_task_id
        self.confirm(self.preview('point_orbit', longitude=113.1, latitude=23.1, radius_m=30))
        self.assertNotEqual(self.sim.state.temporary_task_id, first)
        self.assertEqual(self.sim.state.breakpoint_index, 7)

    def test_old_temporary_completion_is_rejected(self):
        self.confirm(self.preview('point_flight', longitude=113.0, latitude=23.0, altitude_m=50))
        first = self.sim.state.temporary_task_id
        self.confirm(self.preview('point_orbit', longitude=113.1, latitude=23.1, radius_m=30))
        with self.assertRaises(TaskEventError) as caught:
            self.sim.apply_task_event('temporary_completed', task_batch_id='batch',
                                      temporary_task_id=first)
        self.assertEqual(caught.exception.code, 'STALE_TEMPORARY_TASK_EVENT')

    def test_completion_requires_an_explicit_choice(self):
        self.confirm(self.preview('point_flight', longitude=113.0, latitude=23.0, altitude_m=50))
        current = self.sim.state.temporary_task_id
        self.sim.apply_task_event('temporary_completed', task_batch_id='batch',
                                  temporary_task_id=current)
        decision = post_temporary_decision(self.sim.state)
        self.assertTrue(decision['required'])
        self.assertEqual(decision['options'], ['resume_main', 'end_all'])
        self.assertIsNone(decision['default'])
        # 不猜：未选择时不能恢复也不能结束。
        with self.assertRaises(TaskEventError) as caught:
            validate_post_temporary_choice(self.sim.state, None)
        self.assertEqual(caught.exception.code, TEMPORARY_DECISION_REQUIRED)
        result = self.sim.resolve_temporary_outcome('resume_main')
        self.assertTrue(result['resumable'])
        self.assertEqual(self.sim.state.route_status, 'running')
        self.assertEqual(self.sim.state.breakpoint_index, 7)

    def test_end_all_choice_clears_the_breakpoint(self):
        self.confirm(self.preview('point_flight', longitude=113.0, latitude=23.0, altitude_m=50))
        current = self.sim.state.temporary_task_id
        self.sim.apply_task_event('temporary_completed', task_batch_id='batch',
                                  temporary_task_id=current)
        self.sim.resolve_temporary_outcome('end_all')
        self.assertIsNone(self.sim.state.breakpoint_index)
        self.assertEqual(self.sim.state.route_status, 'ended')
        with self.assertRaises(TaskEventError):
            self.sim.apply_task_event('temporary_completed', task_batch_id='batch',
                                      temporary_task_id=current)

    def test_resume_is_refused_after_mapping_invalidation(self):
        self.sim.state.task_type = 'mapping'
        self.confirm(self.preview('move_forward', distance_m=10))
        # 测绘被作废后临时任务也结束，route_status 为 interrupted/ended，无断点。
        self.assertIsNone(self.sim.state.breakpoint_index)
        decision = post_temporary_decision(self.sim.state)
        self.assertFalse(decision['required'])
        with self.assertRaises(TaskEventError) as caught:
            validate_post_temporary_choice(self.sim.state, 'resume_main')
        self.assertEqual(caught.exception.code, TEMPORARY_DECISION_REQUIRED)

    def test_low_battery_return_has_highest_priority(self):
        self.confirm(self.preview('point_flight', longitude=113.0, latitude=23.0, altitude_m=50))
        self.sim.apply_task_event('low_battery_return', task_batch_id='batch')
        self.assertTrue(self.sim.state.low_battery_return)
        self.assertIsNone(self.sim.state.temporary_task_id)
        self.assertEqual(self.sim.state.task_status, 'returning_home')
        self.assertEqual(self.sim.state.breakpoint_index, 7)
        # 返航期间拒绝新的临时任务与旧的完成事件。
        self.assertEqual(self.preview('point_orbit', longitude=113.1, latitude=23.1,
                                      radius_m=30).error_code, 'LOW_BATTERY_RETURN_PRIORITY')
        with self.assertRaises(TaskEventError) as caught:
            self.sim.apply_task_event('temporary_completed', task_batch_id='batch',
                                      temporary_task_id='stale')
        self.assertEqual(caught.exception.code, 'LOW_BATTERY_RETURN_PRIORITY')

    def test_recharge_restores_only_the_valid_inspection_mainline(self):
        self.sim.apply_task_event('low_battery_return', task_batch_id='batch')
        self.sim.apply_task_event('recharged_online', task_batch_id='batch')
        self.assertEqual(self.sim.state.route_status, 'running')
        self.assertEqual(self.sim.state.breakpoint_index, 7)
        self.assertFalse(self.sim.state.low_battery_return)


class _PlatformGateway:
    """假测绘中断网关：只记录调用顺序与次数，不执行任何真实作废。"""

    execution_mode = 'fake-atomic'
    supports_idempotency_key = True

    def __init__(self, order):
        self.order = order
        self.calls = []
        self.receipt = None

    def interrupt(self, request):
        self.calls.append(request)
        self.order.append('mapping')
        if self.receipt is not None:
            return self.receipt
        return MappingInterruptionReceipt(
            atomic=True, execution_mode=self.execution_mode, confirmed=True,
            completed_steps=INTERRUPTION_STEPS, audit_id='fake-1')


class _PlatformTransport:
    """假传输：记录提交次数；注入异常即模拟「结果未知」。"""

    def __init__(self, order):
        self.order = order
        self.calls = 0
        self.failure = None
        self.response = KtdHttpResponse(200, {'code': 200})

    def submit(self, command, context):
        self.calls += 1
        self.order.append('submit')
        if self.failure is not None:
            raise self.failure
        return self.response


class PlatformConfirmOrderTests(unittest.TestCase):
    """V1.4 P1：confirm 阶段所有确定性预检查必须早于不可逆的测绘原子作废。

    平台执行器 + 假传输 + 假网关；不连接真实平台，也不验证真实测绘事务。
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 1000.0
        self.binding = RuntimeBinding('actor-1', 'project-1', 'hangar-1', 'drone-1', 'batch-1', 1)
        self.order = []
        self.transport = _PlatformTransport(self.order)
        self.gateway = _PlatformGateway(self.order)
        self.journal_path = self.directory.name + '/commands.sqlite3'
        self.snapshot = KtdSnapshot(
            actor_id='actor-1', project_id='project-1', hangar_id='hangar-1', drone_id='drone-1',
            site_id='site-1', task_id='java-task-1', context_version=1, version=7,
            observed_at=self.now, control_owner='actor-1', profile='DJI_HANGAR',
            control_lease_expires_at=self.now + 60,
            permissions=frozenset({'move_forward', 'read_state'}),
            capabilities={'move_forward': True, 'UAV_MOVE_DISTANCE': True},
            state={'online': True, 'airborne': True, 'longitude': 113.0, 'latitude': 23.0,
                   'altitude_m': 100.0, 'height_m': 50.0, 'yaw_deg': 90.0,
                   'task_type': 'mapping', 'route_status': 'running', 'route_id': 'route-main',
                   'auto_stitching': True, 'breakpoint_index': 7},
        )

    def build(self, gateway=None):
        return KtdPlatformExecutor(
            self.binding, site_id='site-1', task_id='java-task-1',
            transport=self.transport, snapshot_provider=lambda: self.snapshot,
            journal=SqliteCommandJournal(self.journal_path), enabled=True,
            clock=lambda: self.now,
            path_checker=HardBoundedFenceProvider(PathCheck('clear')),
            mapping_gateway=self.gateway if gateway is None else gateway)

    def request(self, action='move_forward', parameters=None, key='key-1'):
        return FlightControlActionRequest('command-1', key, action,
                                          {'distance_m': 10} if parameters is None else parameters, 1)

    def confirm(self, executor, card):
        return executor.confirm(card.confirmation_id, command_id=card.command_id,
                                idempotency_key=card.idempotency_key,
                                confirmation_binding=card.confirmation_binding, context_version=1)

    def test_gateway_unavailable_never_submits_and_never_claims_voided(self):
        self.gateway.receipt = MappingInterruptionReceipt(
            atomic=False, execution_mode='unavailable', completed_steps=(),
            error_code=MAPPING_INTERRUPTION_UNAVAILABLE)
        executor = self.build()
        card = executor.preview(self.request())
        self.assertEqual(card.to_dict()['status'], 'preview_required')
        self.assertEqual(self.gateway.calls, [])
        receipt = self.confirm(executor, card)
        self.assertEqual(receipt.error_code, MAPPING_INTERRUPTION_UNAVAILABLE)
        self.assertEqual(len(self.gateway.calls), 1)
        self.assertEqual(self.transport.calls, 0)
        self.assertEqual(self.order, ['mapping'])
        record = receipt.to_dict()['mapping_interruption']
        self.assertFalse(record['atomic'])
        self.assertEqual(record['completed_steps'], [])
        # 不伪造「已作废」：回执只说没下发，不说测绘成果已失效。
        self.assertNotIn('已作废', receipt.message)

    def test_gateway_partial_is_reported_as_incomplete_and_never_submits(self):
        self.gateway.receipt = MappingInterruptionReceipt(
            atomic=False, execution_mode='fake', completed_steps=('stop_auto_stitching',),
            failed_step='void_rebuild_type', audit_id='fake-2',
            error_code=MAPPING_INTERRUPTION_PARTIAL, message='中断在 void_rebuild_type 失败。')
        executor = self.build()
        card = executor.preview(self.request())
        receipt = self.confirm(executor, card)
        self.assertEqual(receipt.error_code, MAPPING_INTERRUPTION_PARTIAL)
        self.assertEqual(self.transport.calls, 0)
        self.assertEqual(self.order, ['mapping'])
        self.assertIn('测绘中断未完成', receipt.message)
        record = receipt.to_dict()['mapping_interruption']
        self.assertFalse(record['atomic'])
        self.assertEqual(record['failed_step'], 'void_rebuild_type')
        self.assertEqual(record['completed_steps'], ['stop_auto_stitching'])

    def test_gateway_not_confirmed_is_reported_and_never_submits(self):
        self.gateway.receipt = MappingInterruptionReceipt(
            atomic=False, execution_mode='fake', completed_steps=(),
            error_code=MAPPING_INTERRUPTION_NOT_CONFIRMED)
        executor = self.build()
        card = executor.preview(self.request())
        receipt = self.confirm(executor, card)
        self.assertEqual(receipt.error_code, MAPPING_INTERRUPTION_NOT_CONFIRMED)
        self.assertEqual(self.transport.calls, 0)
        self.assertIn('测绘中断未完成', receipt.message)

    def test_success_voids_mapping_once_then_submits(self):
        executor = self.build()
        card = executor.preview(self.request())
        receipt = self.confirm(executor, card)
        self.assertEqual(receipt.to_dict()['status'], 'submitted')
        self.assertEqual(len(self.gateway.calls), 1)
        self.assertEqual(self.transport.calls, 1)
        # 顺序固定：先不可逆作废，再平台提交。
        self.assertEqual(self.order, ['mapping', 'submit'])
        record = receipt.to_dict()['mapping_interruption']
        self.assertTrue(record['atomic'])
        self.assertEqual(tuple(record['completed_steps']), INTERRUPTION_STEPS)

    def test_unknown_submit_is_kept_and_never_retried(self):
        self.transport.failure = TimeoutError('upstream timeout')
        executor = self.build()
        card = executor.preview(self.request())
        first = self.confirm(executor, card)
        self.assertEqual(first.to_dict()['status'], 'unknown')
        self.assertEqual(len(self.gateway.calls), 1)
        self.assertEqual(self.transport.calls, 1)
        # 重启后同一幂等键重放：返回既有 unknown，不再次作废、不再次下发。
        restarted = self.build()
        second = self.confirm(restarted, card)
        self.assertEqual(second.to_dict()['status'], 'unknown')
        self.assertEqual(len(self.gateway.calls), 1)
        self.assertEqual(self.transport.calls, 1)
        self.assertEqual(self.order, ['mapping', 'submit'])

    def test_non_mapping_task_never_calls_the_gateway(self):
        self.snapshot = KtdSnapshot(**{**self.snapshot.__dict__, 'state': {
            **self.snapshot.state, 'task_type': 'inspection', 'route_status': 'idle'}})
        executor = self.build()
        card = executor.preview(self.request())
        self.assertEqual(card.to_dict()['status'], 'preview_required')
        receipt = self.confirm(executor, card)
        self.assertEqual(receipt.to_dict()['status'], 'submitted')
        self.assertEqual(self.gateway.calls, [])
        self.assertEqual(self.order, ['submit'])
        self.assertNotIn('mapping_interruption', receipt.to_dict())


class ContractTests(unittest.TestCase):
    def test_interrupting_action_set_matches_the_policy_module(self):
        from lingxi_flight_control.flight_policy import INTERRUPTING_ACTIONS
        from lingxi_flight_control.mapping_interruption import MAPPING_INTERRUPTING_ACTIONS
        self.assertEqual(MAPPING_INTERRUPTING_ACTIONS, frozenset(INTERRUPTING_ACTIONS))

    def test_steps_are_fixed_and_ordered(self):
        self.assertEqual(INTERRUPTION_STEPS, (
            'stop_auto_stitching', 'void_rebuild_type', 'void_break_info',
            'invalidate_task_breakpoint', 'disable_route_resume', 'block_task_resume'))

    def test_audit_summary_has_no_business_payload(self):
        receipts = [MappingInterruptionReceipt(atomic=True, execution_mode='simulator'),
                    MappingInterruptionReceipt(atomic=False, execution_mode='unavailable',
                                               error_code=MAPPING_INTERRUPTION_UNAVAILABLE)]
        summary = audit_summary(receipts)
        self.assertEqual((summary['operations'], summary['atomic'], summary['blocked']), (2, 1, 1))
        self.assertNotIn('route_id', summary)

    def test_request_validation(self):
        for change, code in (
            ({'command_id': ''}, 'INVALID_MAPPING_INTERRUPTION'),
            ({'task_batch_id': ''}, 'INVALID_MAPPING_INTERRUPTION'),
            ({'confirmed': 'yes'}, 'INVALID_MAPPING_INTERRUPTION'),
            ({'route_id': 'route-main '}, 'INVALID_MAPPING_INTERRUPTION'),
        ):
            values = dict(command_id='cmd-1', action='move_forward', task_batch_id='batch',
                          confirmed=True)
            values.update(change)
            with self.subTest(change=change):
                with self.assertRaises(Exception) as caught:
                    MappingInterruptionRequest(**values).validate()
                self.assertEqual(getattr(caught.exception, 'code', None), code)

    def test_platform_executor_uses_the_atomic_gateway_before_dispatch(self):
        from test_ktd_platform import PlatformExecutorTests
        fixture = PlatformExecutorTests()
        fixture.setUp()
        try:
            fixture.snapshot = replace(fixture.snapshot, state={**fixture.snapshot.state,
                'route_status': 'running', 'task_type': 'mapping'})
            calls = []

            class AtomicGateway(UnavailableMappingInterruptionGateway):
                execution_mode = 'fake-atomic'
                supports_idempotency_key = True

                def interrupt(self, request):
                    calls.append(request.action)
                    return MappingInterruptionReceipt(
                        atomic=True, execution_mode=self.execution_mode, confirmed=True,
                        completed_steps=INTERRUPTION_STEPS, audit_id='fake-1')

            from lingxi_flight_control.ktd_platform import KtdPlatformExecutor, SqliteCommandJournal
            executor = KtdPlatformExecutor(
                fixture.binding, site_id='site-1', task_id='java-task-1',
                transport=fixture.transport, snapshot_provider=lambda: fixture.snapshot,
                journal=SqliteCommandJournal(fixture.journal_path), enabled=True,
                clock=lambda: fixture.now, mapping_gateway=AtomicGateway())
            card = executor.preview(fixture.request())
            self.assertIn(MAPPING_MESSAGE, card.warnings)
            self.assertEqual(fixture.transport.calls, [])
            receipt = fixture.confirm(executor, card)
            self.assertEqual(receipt.error_code, None, receipt.to_dict())
            self.assertEqual(calls, ['move_forward'])
            self.assertEqual(len(fixture.transport.calls), 1)
            self.assertEqual(executor.mapping_interruption_audit()['execution_mode'], 'fake-atomic')
        finally:
            fixture.doCleanups()


def _platform_snapshot(now: float, **state) -> KtdSnapshot:
    values = {'online': True, 'airborne': True, 'longitude': 113.0, 'latitude': 23.0,
              'altitude_m': 100.0, 'height_m': 50.0, 'yaw_deg': 90.0,
              'task_type': 'mapping', 'route_status': 'running', 'route_id': 'route-main',
              'auto_stitching': True, 'breakpoint_index': 7}
    values.update(state)
    return KtdSnapshot(
        actor_id='actor-1', project_id='project-1', hangar_id='hangar-1', drone_id='drone-1',
        site_id='site-1', task_id='java-task-1', context_version=1, version=7,
        observed_at=now, control_owner='actor-1', profile='DJI_HANGAR',
        control_lease_expires_at=now + 100000,
        permissions=frozenset({'move_forward', 'read_state'}),
        capabilities={'move_forward': True, 'UAV_MOVE_DISTANCE': True},
        state=values)


class MappingReservationStateMachineTests(unittest.TestCase):
    """持久化 reservation：先落库再调网关、unknown 不重试、failed 对同一幂等键终态。

    用同一个 SQLite journal 上的两个执行器模拟「两个进程」；真正的跨进程用例见
    ``CrossProcessReservationTests``。
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = [1000.0]
        self.binding = RuntimeBinding('actor-1', 'project-1', 'hangar-1', 'drone-1', 'batch-1', 1)
        self.journal_path = self.directory.name + '/commands.sqlite3'
        self.order = []
        self.transport = _PlatformTransport(self.order)
        self.gateway = _PlatformGateway(self.order)
        self.snapshot = _platform_snapshot(self.now[0])

    def clock(self):
        return self.now[0]

    def build(self, gateway=None, journal=None, snapshot=None):
        return KtdPlatformExecutor(
            self.binding, site_id='site-1', task_id='java-task-1',
            transport=self.transport, snapshot_provider=lambda: snapshot or self.snapshot,
            journal=journal or SqliteCommandJournal(self.journal_path), enabled=True,
            clock=self.clock, path_checker=HardBoundedFenceProvider(PathCheck('clear')),
            mapping_gateway=self.gateway if gateway is None else gateway)

    def request(self, key='key-1', command='command-1', distance=10):
        return FlightControlActionRequest(command, key, 'move_forward', {'distance_m': distance}, 1)

    def confirm(self, executor, card):
        return executor.confirm(card.confirmation_id, command_id=card.command_id,
                                idempotency_key=card.idempotency_key,
                                confirmation_binding=card.confirmation_binding, context_version=1)

    def reservation(self, executor, key='key-1'):
        entry = SqliteCommandJournal(self.journal_path).read_mapping(executor._scope(key))
        return None if entry is None else entry[1]

    def test_reservation_is_persisted_before_the_gateway_is_called(self):
        seen = {}
        journal = SqliteCommandJournal(self.journal_path)
        outer = self

        class Probe(_PlatformGateway):
            def interrupt(self, request):
                entry = journal.read_mapping(outer.executor_scope)
                seen['state'] = entry[1]['state'] if entry else None
                seen['record'] = entry[1] if entry else None
                seen['request'] = request
                return super().interrupt(request)

        gateway = Probe(self.order)
        executor = self.build(gateway=gateway)
        outer.executor_scope = executor._scope('key-1')
        card = executor.preview(self.request())
        receipt = self.confirm(executor, card)
        self.assertEqual(receipt.to_dict()['status'], 'submitted')
        # 调用网关的瞬间，reservation 已经是 unknown（pending 已经先落库并 CAS 过）。
        self.assertEqual(seen['state'], RESERVATION_UNKNOWN)
        record = seen['record']
        for field in ('command_id', 'idempotency_key', 'trace_id', 'context_version',
                      'snapshot_version', 'task_batch_id', 'fingerprint'):
            self.assertIn(field, record)
        self.assertEqual(record['command_id'], 'command-1')
        self.assertEqual(record['idempotency_key'], 'key-1')
        self.assertEqual(record['trace_id'], card.trace_id)
        self.assertEqual(record['context_version'], 1)
        self.assertEqual(record['snapshot_version'], 7)
        self.assertEqual(record['task_batch_id'], 'batch-1')
        self.assertEqual(len(record['fingerprint']), 64)
        # 网关拿到的是同一个稳定幂等键与同一组标识。
        self.assertEqual(seen['request'].idempotency_key, 'key-1')
        self.assertEqual(seen['request'].fingerprint, record['fingerprint'])
        self.assertEqual(self.reservation(executor)['state'], RESERVATION_SUCCEEDED)

    def test_gateway_idempotency_support_is_never_assumed(self):
        # 默认（未声明）按不支持处理：本地 reservation 才是唯一保证。
        plain = type('PlainGateway', (_PlatformGateway,), {'supports_idempotency_key': False})(
            self.order)
        executor = self.build(gateway=plain)
        card = executor.preview(self.request())
        receipt = self.confirm(executor, card)
        audit = executor.mapping_interruption_audit()
        self.assertFalse(audit['idempotency_supported'])
        self.assertFalse(receipt.to_dict()['mapping_reservation']['gateway_idempotency_supported'])
        # 无论是否支持，幂等键都会随请求传出去。
        self.assertEqual(plain.calls[0].idempotency_key, 'key-1')

    def test_unknown_gateway_outcome_is_never_retried(self):
        class Broken(_PlatformGateway):
            def interrupt(self, request):
                self.calls.append(request)
                self.order.append('mapping')
                raise TimeoutError('gateway timeout')

        gateway = Broken(self.order)
        executor = self.build(gateway=gateway)
        card = executor.preview(self.request())
        first = self.confirm(executor, card)
        self.assertEqual(first.to_dict()['status'], 'unknown')
        self.assertEqual(first.error_code, MAPPING_INTERRUPTION_UNKNOWN)
        self.assertEqual(len(gateway.calls), 1)
        self.assertEqual(self.transport.calls, 0)
        self.assertEqual(self.reservation(executor)['state'], RESERVATION_UNKNOWN)
        # 重启后同一幂等键重放：不再次调用网关，也不 submit。
        restarted = self.build(gateway=gateway)
        second = self.confirm(restarted, restarted.preview(self.request()))
        self.assertEqual(second.to_dict()['status'], 'unknown')
        self.assertEqual(second.error_code, MAPPING_INTERRUPTION_UNKNOWN)
        self.assertEqual(len(gateway.calls), 1)
        self.assertEqual(self.transport.calls, 0)
        self.assertEqual(self.reservation(executor)['state'], RESERVATION_UNKNOWN)
        # unknown 绝不能被改写成 unavailable 或 success。
        self.assertNotEqual(second.error_code, MAPPING_INTERRUPTION_UNAVAILABLE)
        self.assertNotIn('已作废', second.message)

    def test_failed_gateway_is_terminal_for_the_same_idempotency_key(self):
        self.gateway.receipt = MappingInterruptionReceipt(
            atomic=False, execution_mode='unavailable', error_code=MAPPING_INTERRUPTION_UNAVAILABLE)
        executor = self.build()
        card = executor.preview(self.request())
        first = self.confirm(executor, card)
        self.assertEqual(first.error_code, MAPPING_INTERRUPTION_UNAVAILABLE)
        self.assertEqual(self.reservation(executor)['state'], RESERVATION_FAILED)
        restarted = self.build()
        second = self.confirm(restarted, restarted.preview(self.request()))
        self.assertEqual(second.error_code, MAPPING_INTERRUPTION_UNAVAILABLE)
        self.assertEqual(len(self.gateway.calls), 1)
        self.assertEqual(self.transport.calls, 0)

    def test_atomic_without_confirmation_is_not_success(self):
        self.gateway.receipt = MappingInterruptionReceipt(
            atomic=True, execution_mode='fake', completed_steps=INTERRUPTION_STEPS,
            confirmed=False)
        executor = self.build()
        receipt = self.confirm(executor, executor.preview(self.request()))
        self.assertEqual(receipt.error_code, MAPPING_INTERRUPTION_NOT_CONFIRMED)
        self.assertEqual(self.transport.calls, 0)
        self.assertEqual(self.reservation(executor)['state'], RESERVATION_FAILED)

    def test_same_key_with_a_different_fingerprint_is_a_conflict(self):
        self.gateway.receipt = MappingInterruptionReceipt(
            atomic=False, execution_mode='fake', error_code=MAPPING_INTERRUPTION_PARTIAL)
        executor = self.build()
        self.confirm(executor, executor.preview(self.request()))
        # 同一幂等键、不同参数 = 不同 fingerprint，必须拒绝，而不是复用作废结果。
        other = executor.preview(self.request(distance=20))
        receipt = self.confirm(executor, other)
        self.assertEqual(receipt.error_code, 'IDEMPOTENCY_CONFLICT')
        self.assertEqual(len(self.gateway.calls), 1)
        self.assertEqual(self.transport.calls, 0)

    def test_pending_reservation_with_live_lease_is_not_taken_over(self):
        executor = self.build()
        request = self.request()
        scope, fingerprint = executor._scope('key-1'), executor._fingerprint(request)
        record = executor._reservation_record(
            request, self.snapshot, scope=scope, fingerprint=fingerprint,
            trace_id='trace-x', action_value='move_forward')
        self.assertEqual(SqliteCommandJournal(self.journal_path)
                         .reserve_mapping(scope, fingerprint, record)[0], 'acquired')
        receipt = self.confirm(executor, executor.preview(request))
        self.assertEqual(receipt.error_code, MAPPING_INTERRUPTION_UNKNOWN)
        self.assertEqual(self.gateway.calls, [])
        self.assertEqual(self.transport.calls, 0)

    def test_crashed_pending_reservation_is_recovered_exactly_once(self):
        executor = self.build()
        request = self.request()
        scope, fingerprint = executor._scope('key-1'), executor._fingerprint(request)
        record = executor._reservation_record(
            request, self.snapshot, scope=scope, fingerprint=fingerprint,
            trace_id='trace-crash', action_value='move_forward')
        # 模拟「reservation 落库后、网关调用前进程消失」：pending + 租约已过期。
        record['lease_expires_at'] = self.now[0] - 1
        journal = SqliteCommandJournal(self.journal_path)
        self.assertEqual(journal.reserve_mapping(scope, fingerprint, record)[0], 'acquired')
        recovery = self.build()
        receipt = self.confirm(recovery, recovery.preview(request))
        self.assertEqual(receipt.to_dict()['status'], 'submitted')
        self.assertEqual(len(self.gateway.calls), 1)
        self.assertEqual(self.transport.calls, 1)
        self.assertEqual(self.reservation(recovery)['attempts'], 1)

    def test_gateway_success_then_journal_crash_never_calls_the_gateway_again(self):
        broken = SqliteCommandJournal(self.journal_path)

        def crash(*_args, **_kwargs):
            raise RuntimeError('process died before the command journal write')

        broken.claim = crash
        executor = self.build(journal=broken)
        card = executor.preview(self.request())
        crashed = self.confirm(executor, card)
        self.assertEqual(crashed.error_code, 'JOURNAL_OR_STATE_UNAVAILABLE')
        self.assertEqual(len(self.gateway.calls), 1)
        self.assertEqual(self.transport.calls, 0)
        self.assertEqual(self.reservation(executor)['state'], RESERVATION_SUCCEEDED)
        # 恢复：作废已经成功，只允许补写账本 + submit 一次，绝不再调网关。
        recovery = self.build()
        receipt = self.confirm(recovery, recovery.preview(self.request()))
        self.assertEqual(receipt.to_dict()['status'], 'submitted')
        self.assertEqual(len(self.gateway.calls), 1)
        self.assertEqual(self.transport.calls, 1)

    def test_manual_resolution_is_the_only_way_out_of_unknown(self):
        class Broken(_PlatformGateway):
            def interrupt(self, request):
                self.calls.append(request)
                raise TimeoutError('gateway timeout')

        gateway = Broken(self.order)
        executor = self.build(gateway=gateway)
        card = executor.preview(self.request())
        self.assertEqual(self.confirm(executor, card).error_code, MAPPING_INTERRUPTION_UNKNOWN)
        # 没有权威回执就不能自己往前走。
        with self.assertRaises(Exception) as caught:
            executor.resolve_mapping_interruption(
                command_id='command-1', idempotency_key='key-1', context_version=1,
                resolution='voided', authority_ref='')
        self.assertEqual(str(caught.exception), 'INVALID_MAPPING_RESOLUTION')
        # 人工核对确认已作废：reservation → succeeded，但恢复本身不调网关、不 submit。
        resolved = executor.resolve_mapping_interruption(
            command_id='command-1', idempotency_key='key-1', context_version=1,
            resolution='voided', authority_ref='ticket-1')
        self.assertEqual(resolved['state'], RESERVATION_SUCCEEDED)
        self.assertEqual(len(gateway.calls), 1)
        self.assertEqual(self.transport.calls, 0)
        # 之后重新 preview → confirm：只补账本与 submit 一次。
        recovery = self.build(gateway=gateway)
        receipt = self.confirm(recovery, recovery.preview(self.request()))
        self.assertEqual(receipt.to_dict()['status'], 'submitted')
        self.assertEqual(len(gateway.calls), 1)
        self.assertEqual(self.transport.calls, 1)

    def test_manual_resolution_can_declare_the_void_never_happened(self):
        class Broken(_PlatformGateway):
            def interrupt(self, request):
                self.calls.append(request)
                raise TimeoutError('gateway timeout')

        gateway = Broken(self.order)
        executor = self.build(gateway=gateway)
        self.confirm(executor, executor.preview(self.request()))
        resolved = executor.resolve_mapping_interruption(
            command_id='command-1', idempotency_key='key-1', context_version=1,
            resolution='not_voided', authority_ref='ticket-2')
        self.assertEqual(resolved['state'], RESERVATION_FAILED)
        recovery = self.build(gateway=gateway)
        receipt = self.confirm(recovery, recovery.preview(self.request()))
        self.assertEqual(receipt.error_code, MAPPING_INTERRUPTION_FAILED)
        self.assertEqual(len(gateway.calls), 1)
        self.assertEqual(self.transport.calls, 0)

    def test_non_mapping_task_creates_no_reservation(self):
        self.snapshot = _platform_snapshot(self.now[0], task_type='inspection', route_status='idle')
        executor = self.build()
        card = executor.preview(self.request())
        self.assertEqual(card.to_dict()['status'], 'preview_required')
        self.assertEqual(self.confirm(executor, card).to_dict()['status'], 'submitted')
        self.assertIsNone(self.reservation(executor))
        self.assertEqual(self.gateway.calls, [])
        self.assertEqual(self.transport.calls, 1)


class CrossProcessReservationTests(unittest.TestCase):
    """两个**独立 Python 进程**同时 confirm 同一张确认卡。"""

    WORKER = pathlib.Path(__file__).resolve().parent / 'mapping_race_worker.py'
    ROOT = pathlib.Path(__file__).resolve().parents[1]

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.journal_path = self.directory.name + '/commands.sqlite3'
        self.calls_path = self.directory.name + '/calls.txt'
        self._spawned = 0

    def calls(self):
        path = pathlib.Path(self.calls_path)
        return path.read_text().splitlines() if path.exists() else []

    def spawn(self, mode='normal', *, key='key-1', command='command-1', distance=10.0,
              start_at=0.0):
        self._spawned += 1
        result = self.directory.name + f'/result-{self._spawned}.json'
        environment = {**os.environ, 'PYTHONPATH': str(self.ROOT / 'src'),
                       'PYTHONDONTWRITEBYTECODE': '1'}
        process = subprocess.Popen(
            [sys.executable, str(self.WORKER), '--journal', self.journal_path,
             '--calls', self.calls_path, '--mode', mode, '--key', key,
             '--command', command, '--distance', str(distance),
             '--start-at', str(start_at), '--result', result],
            env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return process, result

    def collect(self, spawned, timeout=60):
        process, result = spawned
        _stdout, stderr = process.communicate(timeout=timeout)
        path = pathlib.Path(result)
        data = json.loads(path.read_text()) if path.exists() else None
        return process.returncode, data, stderr

    def executor(self):
        """只用于人工核对/读取 reservation 的执行器；不会调用网关或传输。"""
        return KtdPlatformExecutor(
            RuntimeBinding('actor-1', 'project-1', 'hangar-1', 'drone-1', 'batch-1', 1),
            site_id='site-1', task_id='java-task-1', transport=_PlatformTransport([]),
            snapshot_provider=lambda: _platform_snapshot(1000.0),
            journal=SqliteCommandJournal(self.journal_path), enabled=True,
            clock=lambda: 1000.0, path_checker=HardBoundedFenceProvider(PathCheck('clear')),
            mapping_gateway=UnavailableMappingInterruptionGateway())

    def test_two_processes_confirm_one_card_and_call_the_gateway_once(self):
        start_at = time.time() + 0.4
        first = self.spawn(start_at=start_at)
        second = self.spawn(start_at=start_at)
        outcomes = [self.collect(first), self.collect(second)]
        for returncode, data, stderr in outcomes:
            self.assertEqual(returncode, 0, stderr)
            self.assertIsNotNone(data, stderr)
        # 同一张确认卡：两个进程的 confirmation_id 必须一致。
        self.assertEqual(outcomes[0][1]['confirmation_id'], outcomes[1][1]['confirmation_id'])
        # 至多一次网关、至多一次 submit。
        self.assertEqual(self.calls().count('gateway'), 1, self.calls())
        self.assertEqual(self.calls().count('submit'), 1, self.calls())
        statuses = sorted(item[1]['status'] for item in outcomes)
        self.assertIn('submitted', statuses)
        self.assertTrue(set(statuses) <= {'submitted', 'unknown'}, statuses)
        # 输家要么拿到既有回执，要么在网关调用窗口里明确 unknown；都不得自己再调网关。
        # 它读到的是某一瞬间的状态，所以允许 pending/unknown/succeeded 三种快照。
        self.assertTrue(any(item[1]['reservation_state'] == RESERVATION_SUCCEEDED
                            for item in outcomes), outcomes)
        self.assertTrue(all(item[1]['reservation_state'] in
                            {RESERVATION_PENDING, RESERVATION_UNKNOWN, RESERVATION_SUCCEEDED}
                            for item in outcomes), outcomes)

    def test_conflicting_fingerprint_under_the_same_key_is_rejected(self):
        start_at = time.time() + 0.4
        first = self.spawn(start_at=start_at, distance=10.0)
        second = self.spawn(start_at=start_at, distance=20.0)
        outcomes = [self.collect(first), self.collect(second)]
        codes = [item[1].get('error_code') for item in outcomes if item[1]]
        self.assertEqual(codes.count('IDEMPOTENCY_CONFLICT'), 1, outcomes)
        self.assertEqual(self.calls().count('gateway'), 1, self.calls())
        self.assertEqual(self.calls().count('submit'), 1, self.calls())

    def test_gateway_success_then_crash_does_not_call_the_gateway_again(self):
        crashed = self.collect(self.spawn(mode='crash_after_success'))
        self.assertEqual(crashed[0], 9, crashed[2])
        self.assertEqual(self.calls().count('gateway'), 1, self.calls())
        self.assertEqual(self.calls().count('submit'), 0, self.calls())
        recovered = self.collect(self.spawn())
        self.assertEqual(recovered[0], 0, recovered[2])
        self.assertEqual(recovered[1]['status'], 'submitted')
        self.assertEqual(self.calls().count('gateway'), 1, self.calls())
        self.assertEqual(self.calls().count('submit'), 1, self.calls())

    def test_unknown_gateway_is_never_retried_across_processes(self):
        crashed = self.collect(self.spawn(mode='crash_in_gateway'))
        self.assertEqual(crashed[0], 9, crashed[2])
        self.assertEqual(self.calls().count('gateway'), 1, self.calls())
        self.assertEqual(self.calls().count('submit'), 0, self.calls())
        for _ in range(2):
            outcome = self.collect(self.spawn())
            self.assertEqual(outcome[0], 0, outcome[2])
            self.assertEqual(outcome[1]['status'], 'unknown')
            self.assertEqual(outcome[1]['error_code'], MAPPING_INTERRUPTION_UNKNOWN)
            self.assertEqual(outcome[1]['reservation_state'], RESERVATION_UNKNOWN)
            self.assertEqual(self.calls().count('gateway'), 1, self.calls())
            self.assertEqual(self.calls().count('submit'), 0, self.calls())
        # 只有显式人工核对才能继续；恢复接口本身不调网关、不 submit。
        resolved = self.executor().resolve_mapping_interruption(
            command_id='command-1', idempotency_key='key-1', context_version=1,
            resolution='voided', authority_ref='ticket-cross-process')
        self.assertEqual(resolved['state'], RESERVATION_SUCCEEDED)
        self.assertEqual(self.calls().count('gateway'), 1, self.calls())
        self.assertEqual(self.calls().count('submit'), 0, self.calls())
        final = self.collect(self.spawn())
        self.assertEqual(final[0], 0, final[2])
        self.assertEqual(final[1]['status'], 'submitted')
        self.assertEqual(self.calls().count('gateway'), 1, self.calls())
        self.assertEqual(self.calls().count('submit'), 1, self.calls())

    def test_different_command_ids_each_call_the_gateway(self):
        self.assertEqual(self.collect(self.spawn(key='key-1', command='command-1'))[0], 0)
        self.assertEqual(self.collect(self.spawn(key='key-2', command='command-2'))[0], 0)
        self.assertEqual(self.calls().count('gateway'), 2, self.calls())
        self.assertEqual(self.calls().count('submit'), 2, self.calls())


if __name__ == '__main__':
    unittest.main()
