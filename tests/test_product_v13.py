"""V1.3 rules through existing simulator, cockpit and fake platform boundaries."""
from dataclasses import replace
import json
import unittest

from lingxi_flight_control import (MemoryFlightControlSimulator, RuntimeBinding,
    FlightControlActionRequest, FlightControlStatus, ModelDecision, DecisionKind, ToolCall)
from lingxi_flight_control.cockpit import CockpitService
from lingxi_flight_control.geometry import horizontal_distance_m
from lingxi_flight_control.flight_policy import PathCheck, MAPPING_MESSAGE
from fence_support import HardBoundedFenceProvider, StaticFenceProvider


class ProductV13Tests(unittest.TestCase):
    def setUp(self):
        self.binding = RuntimeBinding('actor', 'project', 'hangar', 'drone', 'batch', 1)
        self.path = PathCheck('clear')
        self.sim = MemoryFlightControlSimulator(self.binding,
            path_checker=StaticFenceProvider(lambda *_: self.path))
        self.sim.state.control_owner = 'actor'
        self.sim.state.altitude_m = 50
        self.counter = 0

    def preview(self, action, **parameters):
        self.counter += 1
        key = str(self.counter)
        return self.sim.preview(FlightControlActionRequest(key, key, action, parameters, 1))

    def confirm(self, card):
        return self.sim.confirm(card.confirmation_id, command_id=card.command_id,
            idempotency_key=card.idempotency_key, confirmation_binding=card.confirmation_binding,
            context_version=1)

    def execute(self, action, **parameters):
        card = self.preview(action, **parameters)
        self.assertEqual(card.status.value, 'preview_required', card.to_dict())
        receipt = self.confirm(card)
        self.assertEqual(receipt.status.value, 'accepted', receipt.to_dict())
        return receipt

    def route(self, task_type='inspection'):
        self.sim.state.route_id = 'original'
        self.sim.state.route_status = 'running'
        self.sim.state.task_type = task_type
        self.sim.state.breakpoint_index = 7
        self.sim.state.auto_stitching = task_type == 'mapping'

    def event(self, event, **kwargs):
        return self.sim.apply_task_event(event, task_batch_id='batch', **kwargs)

    def test_all_flight_and_payload_actions_require_control(self):
        self.sim.state.control_owner = None
        for action, parameters in [('emergency_stop', {}), ('return_home', {}),
            ('camera_photo', {}), ('move_forward', {'distance_m': 10})]:
            card = self.preview(action, **parameters)
            self.assertEqual(card.error_code, 'CONTROL_NOT_OWNED')
            self.assertIn('您尚未获取无人机控制权', card.message)

    def test_control_revoked_between_preview_and_confirm(self):
        for action in ('emergency_stop', 'camera_photo', 'return_home'):
            self.sim.state.control_owner = 'actor'
            card = self.preview(action)
            self.sim.state.control_owner = 'other'
            before = self.sim.state.to_dict()
            self.assertEqual(self.confirm(card).error_code, 'CONTROL_NOT_OWNED')
            self.assertEqual(before, self.sim.state.to_dict())

    def test_running_route_blocks_micro_but_allows_payload(self):
        self.route()
        self.assertEqual(self.preview('move_forward', distance_m=10).error_code, 'ROUTE_PAUSE_REQUIRED')
        self.assertEqual(self.preview('turn_left', angle_deg=90).error_code, 'ROUTE_PAUSE_REQUIRED')
        self.execute('camera_photo')
        self.execute('route_pause')
        self.execute('move_forward', distance_m=10)
        self.assertEqual(self.sim.state.breakpoint_index, 7)
        self.execute('route_resume')

    def test_route_starts_after_motion_preview(self):
        card = self.preview('move_forward', distance_m=10)
        self.route()
        self.assertEqual(self.confirm(card).error_code, 'ROUTE_STATE_CHANGED')

    def test_clamp_uses_current_device_table_and_confirmed_effective_value(self):
        self.sim.state.device_limits['camera_zoom']['max'] = 12
        card = self.preview('camera_zoom', value=100)
        self.assertEqual(card.parameters, {'value': 12})
        self.assertEqual(card.adjustments[0]['requested'], 100)
        self.assertEqual(self.sim.state.camera['zoom'], 1)
        receipt = self.confirm(card)
        self.assertEqual(receipt.state['camera']['zoom'], 12)
        self.assertEqual(receipt.adjustments, card.adjustments)

    def test_limit_changes_invalidate_card_not_silently_reclamp(self):
        card = self.preview('camera_zoom', value=100)
        self.sim.state.device_limits['camera_zoom']['max'] = 10
        self.assertEqual(self.confirm(card).error_code, 'POLICY_STATE_CHANGED')
        self.assertEqual(self.sim.state.camera['zoom'], 1)

    def test_missing_limits_and_nonfinite_values_never_guess(self):
        self.sim.state.device_limits = {}
        self.assertEqual(self.preview('camera_zoom', value=100).error_code, 'DEVICE_LIMITS_UNAVAILABLE')
        for value in (True, float('nan'), float('inf')):
            self.assertEqual(self.preview('camera_zoom', value=value).error_code, 'INVALID_PARAMETERS')

    def test_path_block_at_preview_and_recheck_at_confirmation(self):
        self.path = PathCheck('blocked')
        self.assertEqual(self.preview('move_forward', distance_m=10).error_code, 'PATH_BLOCKED')
        self.path = PathCheck('clear')
        card = self.preview('move_forward', distance_m=10)
        self.path = PathCheck('blocked')
        self.assertEqual(self.confirm(card).error_code, 'PATH_BLOCKED')

    def test_fence_outage_warns_without_bypassing_control(self):
        self.path = None
        card = self.preview('move_forward', distance_m=10)
        self.assertIn('电子围栏接口不可用', card.warnings[0])
        self.assertEqual(self.confirm(card).status.value, 'accepted')
        self.sim.state.control_owner = None
        self.assertEqual(self.preview('move_forward', distance_m=10).error_code, 'CONTROL_NOT_OWNED')

    def test_mapping_interruption_only_on_confirmation_and_never_resumes(self):
        self.route('mapping')
        card = self.preview('move_forward', distance_m=10)
        self.assertIn(MAPPING_MESSAGE, card.warnings)
        self.assertTrue(self.sim.state.auto_stitching)
        self.assertFalse(self.sim.state.mapping_invalidated)
        self.confirm(card)
        self.assertTrue(self.sim.state.mapping_invalidated)
        self.assertFalse(self.sim.state.auto_stitching)
        self.assertIsNone(self.sim.state.breakpoint_index)
        for action in ('route_resume', 'continue_from_breakpoint'):
            self.assertEqual(self.preview(action).error_code, 'MAPPING_NOT_RESUMABLE')
        self.event('low_battery_return')
        self.assertEqual(self.event('recharged_online')['route_status'], 'ended')

    def test_mapping_type_change_invalidates_unwarned_stop_card(self):
        self.route()
        card = self.preview('emergency_stop')
        self.sim.state.task_type = 'mapping'
        self.assertEqual(self.confirm(card).error_code, 'POLICY_STATE_CHANGED')

    def test_point_orbit_stays_on_the_radius_and_never_moves_to_the_target(self):
        self.sim.state.longitude = 113.0
        self.sim.state.latitude = 23.0
        receipt = self.execute('point_orbit', longitude=114.0, latitude=24.0, radius_m=30)
        distance = horizontal_distance_m(
            receipt.state['longitude'], receipt.state['latitude'], 114.0, 24.0)
        self.assertAlmostEqual(distance, 30.0, delta=0.5)
        self.assertNotEqual((round(receipt.state['longitude'], 5), round(receipt.state['latitude'], 5)),
                            (114.0, 24.0))
        self.assertEqual(receipt.state['route_status'], 'point_orbit')

    def test_latest_temporary_replaces_previous_and_preserves_main_breakpoint(self):
        self.route()
        first = self.execute('point_flight', longitude=113, latitude=23, altitude_m=50)
        old_id = first.state['temporary_task_id']
        latest = self.execute('point_orbit', longitude=114, latitude=24, radius_m=30)
        self.assertNotEqual(old_id, latest.state['temporary_task_id'])
        self.assertEqual(latest.state['route_id'], 'original')
        self.assertEqual(latest.state['breakpoint_index'], 7)
        with self.assertRaises(ValueError):
            self.event('temporary_completed', temporary_task_id=old_id)
        self.event('temporary_completed', temporary_task_id=latest.state['temporary_task_id'])
        self.execute('continue_from_breakpoint')
        self.assertEqual(self.sim.state.breakpoint_index, 7)

    def test_low_battery_priority_and_recharge_restore_only_original(self):
        self.route()
        self.execute('point_orbit', longitude=114, latitude=24, radius_m=30)
        self.event('low_battery_return')
        self.assertIsNone(self.sim.state.temporary_task_id)
        self.assertEqual(self.preview('cancel_return_home').error_code, 'LOW_BATTERY_RETURN_PRIORITY')
        self.event('recharged_online')
        self.assertEqual(self.sim.state.route_status, 'running')
        self.assertEqual(self.sim.state.route_id, 'original')
        self.assertEqual(self.sim.state.breakpoint_index, 7)

    def test_end_all_clears_breakpoint_and_events_reject_wrong_binding(self):
        self.route()
        self.event('all_tasks_ended')
        self.event('low_battery_return')
        self.assertEqual(self.event('recharged_online')['route_status'], 'ended')
        with self.assertRaises(ValueError):
            self.sim.apply_task_event('low_battery_return', task_batch_id='other')

    def service(self, action=None, parameters=None, **kwargs):
        class Model:
            def decide(self, *_):
                if action is None:
                    raise AssertionError('exact intent should not call model')
                return ModelDecision(DecisionKind.TOOL_CALL, tool_call=ToolCall(action, parameters or {}))
        return CockpitService(self.binding, self.sim, Model, **kwargs)

    def message(self, service, text):
        self.counter += 1
        return service.handle('POST', '/api/flight-control/agent/messages', json.dumps({
            'text': text, 'request_id': str(self.counter), 'context_version': 1}),
            {'X-Agent-Session': 'v13'}).payload

    def test_cockpit_hover_is_its_own_action_and_never_emergency_stop(self):
        # V1.3 2.4：悬停是独立动作，由执行器折算成暂停航线或停止运动；
        # 手动模式下也不得降级成紧急停止（两者语义不同）。
        self.route()
        result = self.message(self.service(), '悬停')
        self.assertEqual(result['card']['action'], 'hover')
        self.sim.state.route_status = 'idle'
        result = self.message(self.service(), '悬停')
        self.assertEqual(result['card']['action'], 'hover')

    def test_system_prompt_never_teaches_hover_as_emergency_stop(self):
        # 模型路径（非精确“悬停”）同样不能把悬停教成急停：V1.4 2.2.4 与 4.3.1
        # 把两者定义为不同动作，急停会改变任务状态。
        from lingxi_flight_control.cockpit import COCKPIT_SYSTEM_PROMPT
        self.assertNotIn('悬停使用emergency_stop', COCKPIT_SYSTEM_PROMPT)
        self.assertIn('手动飞行悬停使用hover', COCKPIT_SYSTEM_PROMPT)

    def test_cockpit_raw_zoom_is_grounded_before_adjustment(self):
        result = self.message(self.service('camera_zoom', {'value': 100}), '变焦100倍')
        self.assertEqual(result['status'], 'preview_required', result)
        self.assertEqual(result['card']['parameters'], {'value': 30})
        self.assertEqual(result['card']['requested_parameters'], {'value': 100})
        wrong = self.message(self.service('camera_zoom', {'value': 30}), '变焦100倍')
        self.assertEqual(wrong['status'], 'clarification')

    def test_unsupported_and_concurrent_requests_never_become_other_actions(self):
        for text in ('绕楼螺旋飞', '用网格航线飞', '开始三维建模', '边向前飞边喊话',
                     '打开AI识别烟火'):
            result = self.message(self.service(), text)
            self.assertIn(result['status'], ('rejected', 'clarification'))
            self.assertNotIn('card', result)

    def test_plan_adjustment_is_shown_and_then_executes_effective_value(self):
        decisions = [ModelDecision(DecisionKind.TOOL_CALL, tool_call=ToolCall('camera_zoom', {'value': 100})),
                     ModelDecision(DecisionKind.FINAL, message='计划完成。')]
        class Planner:
            def decide(self, *_):
                return decisions.pop(0)
        service = self.service(enable_plan_execution=True, plan_decider_factory=Planner)
        result = self.message(service, '变焦100倍')
        self.assertEqual(result['card']['steps'][0]['parameters'], {'value': 30})
        self.assertIn('100调整为30', result['message'])
        card = result['card']
        body = {key: card[key] for key in ('confirmation_id', 'confirmation_binding',
                'command_id', 'idempotency_key', 'context_version')}
        receipt = service.handle('POST', '/api/flight-control/agent/confirm', json.dumps(body),
                                 {'X-Agent-Session': 'v13'}).payload
        self.assertEqual(receipt['plan_status'], 'completed', receipt)
        self.assertEqual(self.sim.state.camera['zoom'], 30)
        self.assertEqual(receipt['steps'][0]['adjustments'][0]['requested'], 100)

    def test_plan_cannot_silently_use_changed_limit(self):
        decisions = [ModelDecision(DecisionKind.TOOL_CALL, tool_call=ToolCall('camera_zoom', {'value': 100})),
                     ModelDecision(DecisionKind.FINAL, message='计划完成。')]
        class Planner:
            def decide(self, *_):
                return decisions.pop(0)
        service = self.service(enable_plan_execution=True, plan_decider_factory=Planner)
        card = self.message(service, '变焦100倍')['card']
        self.sim.state.device_limits['camera_zoom']['max'] = 12
        body = {key: card[key] for key in ('confirmation_id', 'confirmation_binding',
                'command_id', 'idempotency_key', 'context_version')}
        receipt = service.handle('POST', '/api/flight-control/agent/confirm', json.dumps(body),
                                 {'X-Agent-Session': 'v13'}).payload
        self.assertEqual(receipt['error_code'], 'POLICY_STATE_CHANGED')
        self.assertEqual(self.sim.state.camera['zoom'], 1)

    def test_gimbal_clamp_is_grounded_on_original_and_rechecked(self):
        self.sim.state.gimbal_pitch_deg = -85
        service = self.service('gimbal_down', {'angle_deg': 30})
        result = self.message(service, '云台向下看30度')
        self.assertEqual(result['card']['parameters'], {'angle_deg': 5})
        self.assertEqual(result['card']['requested_parameters'], {'angle_deg': 30})

    def test_no_breakpoint_can_be_invented_for_resume(self):
        self.assertEqual(self.preview('continue_from_breakpoint').error_code, 'BREAKPOINT_UNAVAILABLE')

    def test_mapping_camera_does_not_invalidate_result(self):
        self.route('mapping')
        self.execute('camera_photo')
        self.assertTrue(self.sim.state.auto_stitching)
        self.assertFalse(self.sim.state.mapping_invalidated)

    def test_hover_negation_question_and_extra_clause_do_not_use_fastpath(self):
        for text in ('不要悬停', '悬停了吗？', '悬停然后向前飞'):
            self.assertNotIn('card', self.message(self.service(), text))

    def test_legacy_loop_cannot_auto_confirm_new_mapping_risk(self):
        from lingxi_flight_control.full_control import build_full_control_tool_registry
        self.route('mapping')
        registry = build_full_control_tool_registry(self.sim)
        tool = registry.get('emergency_stop')
        receipt = tool.handler({})
        self.assertEqual(receipt['error_code'], 'POLICY_CONFIRMATION_REQUIRED')
        self.assertTrue(self.sim.state.auto_stitching)
        self.assertFalse(self.sim.state.mapping_invalidated)


class ProductV13PlatformTests(unittest.TestCase):
    def test_platform_zoom_requires_device_table_and_returns_adjustment(self):
        from test_ktd_platform import PlatformExecutorTests
        fixture = PlatformExecutorTests()
        fixture.setUp()
        try:
            fixture.snapshot = replace(fixture.snapshot,
                permissions=fixture.snapshot.permissions | {'camera_zoom'},
                capabilities={**fixture.snapshot.capabilities, 'camera_zoom': True})
            executor = fixture.executor()
            request = fixture.request('camera_zoom', {'value': 100})
            self.assertEqual(executor.preview(request).error_code, 'DEVICE_LIMITS_UNAVAILABLE')
            fixture.snapshot = replace(fixture.snapshot, state={**fixture.snapshot.state,
                'device_limits': {'camera_zoom': {'min': 1, 'max': 12}}})
            card = executor.preview(request)
            self.assertEqual(card.parameters, {'value': 12})
            receipt = fixture.confirm(executor, card)
            self.assertEqual(receipt.to_dict()['status'], 'submitted')
            self.assertEqual(receipt.adjustments[0]['requested'], 100)
            self.assertEqual(fixture.transport.calls[0][0].body['param'], 12)
        finally:
            fixture.doCleanups()

    def test_zhuoyi_confirm_rechecks_owner_and_path(self):
        from test_zhuoyi_platform import ZhuoyiPlatformExecutorTests
        fixture = ZhuoyiPlatformExecutorTests()
        fixture.setUp()
        path = [PathCheck('clear')]
        fixture.executor._path_checker = HardBoundedFenceProvider(lambda *_: path[0])
        card = fixture.executor.preview(fixture.request('move_forward', distance_m=10))
        self.assertEqual(card.status.value, 'preview_required')
        path[0] = PathCheck('blocked')
        result = fixture.executor.confirm(card.confirmation_id, command_id=card.command_id,
            idempotency_key=card.idempotency_key, confirmation_binding=card.confirmation_binding,
            context_version=1)
        self.assertEqual(result.error_code, 'PATH_BLOCKED')
        self.assertEqual(fixture.platform.commands, {})

    def test_platform_rechecks_path_and_auto_route_before_sending(self):
        from test_ktd_platform import PlatformExecutorTests
        fixture = PlatformExecutorTests()
        fixture.setUp()
        try:
            executor = fixture.executor()
            fixture.snapshot = replace(fixture.snapshot, state={**fixture.snapshot.state, 'route_status': 'running'})
            self.assertEqual(executor.preview(fixture.request()).error_code, 'ROUTE_PAUSE_REQUIRED')
            fixture.snapshot = replace(fixture.snapshot, state={**fixture.snapshot.state, 'route_status': 'paused'})
            path = [PathCheck('clear')]
            executor._path_checker = HardBoundedFenceProvider(lambda *_: path[0])
            card = executor.preview(fixture.request())
            path[0] = PathCheck('blocked')
            self.assertEqual(fixture.confirm(executor, card).error_code, 'PATH_BLOCKED')
            self.assertEqual(fixture.transport.calls, [])
        finally:
            fixture.doCleanups()

    def test_platform_mapping_interruption_requires_real_atomic_contract(self):
        from test_ktd_platform import PlatformExecutorTests
        fixture = PlatformExecutorTests()
        fixture.setUp()
        try:
            fixture.snapshot = replace(fixture.snapshot, state={**fixture.snapshot.state,
                'route_status': 'running', 'task_type': 'mapping'})
            executor = fixture.executor()
            # V1.4 4.1.1：首次 preview 必须展示「成果失效」确认，而不是静默拒绝，
            # 也不是直接执行；此时不能有任何平台写入。
            card = executor.preview(fixture.request())
            self.assertIs(card.status, FlightControlStatus.PREVIEW_REQUIRED)
            self.assertIn(MAPPING_MESSAGE, card.warnings)
            self.assertEqual(fixture.transport.calls, [])
            # 确认后仍必须在缺少原子契约时拒绝，且不发送任何飞控动作。
            receipt = fixture.confirm(executor, card)
            self.assertEqual(receipt.error_code, 'MAPPING_INTERRUPTION_UNAVAILABLE')
            self.assertEqual(fixture.transport.calls, [])
            self.assertEqual(executor.mapping_interruption_audit()['execution_mode'], 'unavailable')
        finally:
            fixture.doCleanups()
