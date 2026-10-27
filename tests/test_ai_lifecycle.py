"""V1.4 2.1.2 / 3.3：静态标注 AI 生命周期的 typed 契约、去重与冲突追问。

全部使用模拟器 seam 与注入的假网关；没有任何真实算法、模型推理或平台调用。
"""
import json
import unittest

from lingxi_flight_control import FlightControlActionRequest, MemoryFlightControlSimulator, RuntimeBinding
from lingxi_flight_control.ai_lifecycle import (
    ALGORITHM_LIFECYCLE_UNAVAILABLE, ALGORITHM_NOT_CONFIGURED, ALGORITHM_REPLACE_UNSUPPORTED,
    BODY_ANNOTATION_AI_UNSUPPORTED,
    CONFLICT_MESSAGE, CONFLICT_QUESTION, AlgorithmError, AlgorithmLifecycleReceipt,
    AlgorithmPlan, AlgorithmStage, AnnotationAlgorithmConfig, AnnotationGeometry,
    SimulatorAlgorithmLifecycleGateway, UnavailableAlgorithmLifecycleGateway,
    UnavailableAnnotationAlgorithmGateway, algorithms_to_stop, annotation_active_set,
    apply_plan, conflict_clarification, normalize_algorithms, plan_selection,
)
from lingxi_flight_control.cockpit import CockpitService
from lingxi_flight_control.flight_policy import PathCheck
from fence_support import StaticFenceProvider
from lingxi_flight_control.task_events import TaskEventError


def binding():
    return RuntimeBinding('actor', 'project', 'hangar', 'drone', 'batch', 1)


def simulator(*, algorithm_gateway=None, mapping_gateway=None, **state):
    sim = MemoryFlightControlSimulator(binding(), clock=lambda: 1000.0,
                                       path_checker=StaticFenceProvider(PathCheck('clear')),
                                       algorithm_gateway=algorithm_gateway,
                                       mapping_gateway=mapping_gateway)
    sim.state.control_owner = 'actor'
    sim.state.configured_algorithms = ['smoke', 'car', 'person']
    sim.state.route_id = 'route-main'
    sim.state.route_status = 'running'
    sim.state.breakpoint_index = 3
    sim.state.task_status = 'route_executing'
    for key, value in state.items():
        setattr(sim.state, key, value)
    return sim


class ContractTests(unittest.TestCase):
    def test_annotation_geometry_gate(self):
        # 点/线/面允许配置，配置可以为空。
        for geometry in (AnnotationGeometry.POINT, AnnotationGeometry.LINE, AnnotationGeometry.FACE):
            for algorithms in ((), ('smoke',)):
                config = AnnotationAlgorithmConfig('ann-1', geometry, algorithms, 1)
                config.validate(configured=['smoke'])
        # 体标注不允许绑定 AI，且不能靠「配置为空」绕过。
        for algorithms in ((), ('smoke',)):
            with self.subTest(algorithms=algorithms):
                with self.assertRaises(AlgorithmError) as caught:
                    AnnotationAlgorithmConfig('ann-1', AnnotationGeometry.BODY, algorithms, 1).validate(
                        configured=['smoke'])
                self.assertEqual(caught.exception.code, BODY_ANNOTATION_AI_UNSUPPORTED)

    def test_unknown_algorithm_is_rejected_not_replaced(self):
        for value in ('smoke2', 'smoke ', ''):
            with self.subTest(value=value):
                with self.assertRaises(AlgorithmError) as caught:
                    normalize_algorithms([value], configured=['smoke'])
                self.assertIn(caught.exception.code,
                              {ALGORITHM_NOT_CONFIGURED, 'INVALID_ALGORITHM_SELECTION'})

    def test_normalization_dedupes_and_preserves_order(self):
        self.assertEqual(normalize_algorithms(['b', 'a', 'b']), ('b', 'a'))
        with self.assertRaises(AlgorithmError):
            normalize_algorithms([], allow_empty=False)

    def test_default_gateways_are_typed_unavailable(self):
        gateway = UnavailableAlgorithmLifecycleGateway()
        receipt = gateway.start(binding(), 'ann-1', ['smoke'], request_id='r1')
        self.assertFalse(receipt.confirmed)
        self.assertEqual(receipt.error_code, ALGORITHM_LIFECYCLE_UNAVAILABLE)
        self.assertEqual(receipt.execution_mode, 'unavailable')
        self.assertIs(receipt.stage, AlgorithmStage.START_REQUESTED)
        with self.assertRaises(AlgorithmError):
            UnavailableAnnotationAlgorithmGateway().expected(binding(), 'ann-1')

    def test_simulator_receipts_are_marked_as_simulator(self):
        gateway = SimulatorAlgorithmLifecycleGateway()
        started = gateway.start(binding(), 'ann-1', ['smoke'], request_id='r1')
        stopped = gateway.stop(binding(), 'ann-1', ['smoke'], request_id='r2')
        self.assertTrue(started.confirmed and stopped.confirmed)
        self.assertEqual(started.execution_mode, 'simulator')
        self.assertEqual(stopped.execution_mode, 'simulator')
        self.assertIs(started.stage, AlgorithmStage.STARTED)
        self.assertIs(stopped.stage, AlgorithmStage.STOPPED)
        self.assertEqual(gateway.running('ann-1'), ())


class SetMathTests(unittest.TestCase):
    def test_global_and_annotation_algorithms_dedupe(self):
        active = annotation_active_set(['smoke'], ['A'], {'A': ['smoke', 'car']}, ['smoke', 'car'])
        self.assertEqual(active, ('smoke', 'car'))

    def test_shared_algorithm_between_two_annotations_is_not_stopped(self):
        mapping = {'A': ['smoke'], 'B': ['smoke']}
        self.assertEqual(algorithms_to_stop('A', annotation_algorithms=mapping,
                                            global_algorithms=[],
                                            active_annotations=['A', 'B']), ())
        self.assertEqual(algorithms_to_stop('B', annotation_algorithms=mapping,
                                            global_algorithms=[],
                                            active_annotations=['B']), ('smoke',))

    def test_globally_selected_algorithm_is_never_auto_closed(self):
        self.assertEqual(algorithms_to_stop('A', annotation_algorithms={'A': ['smoke']},
                                            global_algorithms=['smoke'],
                                            active_annotations=['A']), ())

    def test_unconfigured_algorithm_never_enters_active_set(self):
        self.assertEqual(annotation_active_set(['ghost'], ['A'], {'A': ['smoke']}, ['smoke']),
                         ('smoke',))


class SelectionTests(unittest.TestCase):
    def test_conflict_requires_clarification_before_any_card(self):
        plan = plan_selection(['smoke'], configured=['smoke', 'car'],
                              global_algorithms=[], active_annotations=['A'],
                              annotation_algorithms={'A': ['smoke']})
        self.assertTrue(plan.requires_clarification)
        self.assertEqual(plan.question, CONFLICT_QUESTION)
        self.assertEqual(plan.conflicts, ('smoke',))

    def test_add_mode_keeps_both(self):
        plan = plan_selection(['car'], configured=['smoke', 'car'],
                              global_algorithms=[], active_annotations=['A'],
                              annotation_algorithms={'A': ['smoke']}, mode='add')
        self.assertEqual(plan.kind, 'apply')
        self.assertEqual(plan.suppressed, ())
        self.assertEqual(set(plan.resulting_active), {'smoke', 'car'})

    def test_replace_mode_suppresses_only_the_conflicting_annotation_algorithm(self):
        plan = plan_selection(['smoke'], configured=['smoke', 'car'],
                              global_algorithms=[], active_annotations=['A'],
                              annotation_algorithms={'A': ['smoke', 'car']}, mode='replace')
        self.assertEqual(plan.suppressed, ('smoke',))
        self.assertEqual(plan.resulting_active, ('car',))

    def test_multi_algorithm_selection_is_atomic(self):
        # 任一算法未配置：整批拒绝，不产生部分绑定。
        with self.assertRaises(AlgorithmError) as caught:
            plan_selection(['smoke', 'ghost'], configured=['smoke', 'car'])
        self.assertEqual(caught.exception.code, ALGORITHM_NOT_CONFIGURED)

    def test_clarification_plan_cannot_be_applied(self):
        plan = plan_selection(['smoke'], configured=['smoke'], active_annotations=['A'],
                              annotation_algorithms={'A': ['smoke']})
        sim = simulator()
        with self.assertRaises(AlgorithmError):
            apply_plan(plan, sim.state)

    def test_apply_plan_writes_whole_selection(self):
        sim = simulator()
        plan = plan_selection(['smoke', 'car'], configured=['smoke', 'car', 'person'])
        globals_now, suppressed = apply_plan(plan, sim.state)
        self.assertEqual(globals_now, ['car', 'smoke'])
        self.assertEqual(suppressed, [])


class SimulatorLifecycleTests(unittest.TestCase):
    def test_six_stages_are_recorded_separately(self):
        sim = simulator(annotation_algorithms={'A': ['smoke']})
        sim.apply_task_event('annotation_entered', task_batch_id='batch', annotation_id='A')
        record = sim.algorithm_lifecycle()['annotations']['A']
        self.assertEqual(record['expected'], ['smoke'])
        self.assertEqual(record['dispatched'], ['smoke'])
        self.assertEqual(record['start_requested'], ['smoke'])
        self.assertEqual(record['started'], ['smoke'])
        self.assertEqual(record['execution_mode'], 'simulator')
        self.assertEqual(sim.algorithm_lifecycle()['execution_mode'], 'simulator')

    def test_start_request_alone_never_claims_running(self):
        """网关只受理请求、不确认启动时，active_algorithms 不能显示运行中。"""
        class UnconfirmedGateway(UnavailableAlgorithmLifecycleGateway):
            execution_mode = 'unconfirmed'

            def start(self, binding, annotation_id, algorithms, *, request_id=''):
                return AlgorithmLifecycleReceipt(
                    stage=AlgorithmStage.START_REQUESTED, annotation_id=annotation_id,
                    algorithms=tuple(algorithms), execution_mode=self.execution_mode,
                    confirmed=False, request_id=request_id,
                    error_code=ALGORITHM_LIFECYCLE_UNAVAILABLE, message='accepted only')

        sim = simulator(annotation_algorithms={'A': ['smoke']},
                        algorithm_gateway=UnconfirmedGateway())
        sim.apply_task_event('annotation_entered', task_batch_id='batch', annotation_id='A')
        self.assertEqual(sim.state.active_algorithms, [])
        record = sim.algorithm_lifecycle()['annotations']['A']
        self.assertEqual(record['start_requested'], ['smoke'])
        self.assertEqual(record['started'], [])
        self.assertEqual(record['error_code'], ALGORITHM_LIFECYCLE_UNAVAILABLE)

    def test_two_annotations_sharing_algorithm_keep_it_after_one_leaves(self):
        sim = simulator(annotation_algorithms={'A': ['smoke'], 'B': ['smoke']})
        for annotation in ('A', 'B'):
            sim.apply_task_event('annotation_entered', task_batch_id='batch', annotation_id=annotation)
        self.assertEqual(sim.state.active_algorithms, ['smoke'])
        sim.apply_task_event('annotation_left', task_batch_id='batch', annotation_id='A')
        self.assertEqual(sim.state.active_algorithms, ['smoke'])
        # 离开 A 时不应发出关闭请求：B 仍引用同一算法。
        self.assertEqual(sim.algorithm_lifecycle()['annotations']['A']['stop_requested'], [])
        sim.apply_task_event('annotation_left', task_batch_id='batch', annotation_id='B')
        self.assertEqual(sim.state.active_algorithms, [])

    def test_global_algorithm_survives_leaving_the_annotation(self):
        sim = simulator(annotation_algorithms={'A': ['smoke']})
        sim.state.global_algorithms = ['smoke']
        sim.apply_task_event('annotation_entered', task_batch_id='batch', annotation_id='A')
        sim.apply_task_event('annotation_left', task_batch_id='batch', annotation_id='A')
        self.assertEqual(sim.state.active_algorithms, ['smoke'])
        self.assertEqual(sim.algorithm_lifecycle()['annotations']['A']['stop_requested'], [])

    def test_duplicate_and_out_of_order_events_are_idempotent_or_rejected(self):
        sim = simulator(annotation_algorithms={'A': ['smoke']})
        sim.apply_task_event('annotation_entered', task_batch_id='batch', annotation_id='A',
                             event_id='ev-1', sequence=5, annotation_version=2)
        revision = sim.state.revision
        # 同一 event_id 重复投递：幂等，不推进版本。
        sim.apply_task_event('annotation_entered', task_batch_id='batch', annotation_id='A',
                             event_id='ev-1', sequence=5, annotation_version=2)
        self.assertEqual(sim.state.revision, revision)
        self.assertEqual(sim.state.active_annotations, ['A'])
        # 不同 event_id 的重复到达：合法但状态不变，同样不推进版本。
        sim.apply_task_event('annotation_entered', task_batch_id='batch', annotation_id='A',
                             event_id='ev-2', sequence=6, annotation_version=2)
        self.assertEqual(sim.state.revision, revision)
        # 旧序号拒绝。
        with self.assertRaises(TaskEventError) as caught:
            sim.apply_task_event('annotation_left', task_batch_id='batch', annotation_id='A',
                                 event_id='ev-3', sequence=4, annotation_version=2)
        self.assertEqual(caught.exception.code, 'STALE_TASK_EVENT')
        # 旧标注版本拒绝。
        with self.assertRaises(TaskEventError) as caught:
            sim.apply_task_event('annotation_left', task_batch_id='batch', annotation_id='A',
                                 event_id='ev-4', sequence=7, annotation_version=1)
        self.assertEqual(caught.exception.code, 'STALE_ANNOTATION_VERSION')
        # 错误 task_batch 拒绝。
        with self.assertRaises(ValueError):
            sim.apply_task_event('annotation_left', task_batch_id='other', annotation_id='A')

    def test_unknown_annotation_is_rejected(self):
        sim = simulator(annotation_algorithms={'A': ['smoke']})
        with self.assertRaises(TaskEventError) as caught:
            sim.apply_task_event('annotation_entered', task_batch_id='batch', annotation_id='X')
        self.assertEqual(caught.exception.code, 'UNKNOWN_ANNOTATION')

    def test_unconfigured_algorithm_never_runs(self):
        sim = simulator(annotation_algorithms={'A': ['ghost']})
        sim.apply_task_event('annotation_entered', task_batch_id='batch', annotation_id='A')
        self.assertEqual(sim.state.active_algorithms, [])

    def test_lifecycle_is_not_exposed_as_model_tool(self):
        sim = simulator()
        names = {definition.name for definition in sim.tool_registry().definitions()}
        for name in ('select_algorithms', 'algorithm_lifecycle', 'resolve_temporary_outcome'):
            self.assertNotIn(name, names)


class CockpitConflictTests(unittest.TestCase):
    def setUp(self):
        self.binding = binding()
        self.sim = simulator(annotation_algorithms={'A': ['烟火']},
                             active_annotations=['A'])
        self.sim.state.configured_algorithms = ['烟火', '车辆', '人员']
        self.sim.state.global_algorithms = []
        self.service = CockpitService(self.binding, self.sim, lambda: None,
            enable_plan_execution=True, plan_decider_factory=lambda: None)
        self.count = 0

    def send(self, text):
        self.count += 1
        return self.service.handle('POST', '/api/flight-control/agent/messages', json.dumps({
            'text': text, 'request_id': str(self.count), 'context_version': 1}),
            {'X-Agent-Session': 'ai-conflict'}).payload

    def test_conflicting_ai_requires_clarification_without_a_card(self):
        reply = self.send('打开AI识别烟火')
        self.assertEqual(reply['status'], 'clarification', reply)
        self.assertEqual(reply['error_code'], 'ALGORITHM_CONFLICT_CLARIFICATION_REQUIRED')
        self.assertIn(CONFLICT_QUESTION, reply['message'])
        self.assertFalse(reply.get('card'))
        self.assertEqual(self.sim.state.global_algorithms, [])

    def test_non_conflicting_ai_still_produces_a_card(self):
        reply = self.send('打开AI识别车辆')
        self.assertEqual(reply['status'], 'preview_required', reply)
        self.assertEqual(reply['card']['action'], 'ai_algorithm_set')

    def test_unknown_ai_is_not_silently_ignored(self):
        reply = self.send('打开AI识别幽灵算法')
        self.assertEqual(reply['status'], 'rejected', reply)
        self.assertEqual(reply['error_code'], ALGORITHM_NOT_CONFIGURED)
        self.assertEqual(self.sim.state.global_algorithms, [])

    def test_conflict_helper_returns_none_without_annotation_state(self):
        self.assertIsNone(conflict_clarification(['smoke'], {'configured_algorithms': ['smoke']}))

    def test_add_answer_continues_with_a_card(self):
        first = self.send('打开AI识别烟火')
        self.assertEqual(first['error_code'], 'ALGORITHM_CONFLICT_CLARIFICATION_REQUIRED')
        second = self.send('同时开启')
        self.assertEqual(second['status'], 'preview_required', second)
        self.assertEqual(second['card']['action'], 'ai_algorithm_set')
        self.assertEqual(second['card']['parameters'], {'algorithm': '烟火', 'enabled': True})

    def test_replace_answer_produces_no_card(self):
        self.send('打开AI识别烟火')
        reply = self.send('仅替换')
        self.assertEqual(reply['status'], 'clarification', reply)
        self.assertEqual(reply['error_code'], ALGORITHM_REPLACE_UNSUPPORTED)
        self.assertFalse(reply.get('card'))
        self.assertEqual(self.sim.state.global_algorithms, [])


class PlatformBoundaryTests(unittest.TestCase):
    def test_platform_has_no_algorithm_level_lifecycle(self):
        """平台只有任务级总开关；必须返回 typed unavailable，不伪造运行成功。"""
        from test_ktd_platform import PlatformExecutorTests
        from lingxi_flight_control.full_control import FlightControlActionRequest

        fixture = PlatformExecutorTests()
        fixture.setUp()
        try:
            executor = fixture.executor()
            receipt = executor.preview(FlightControlActionRequest(
                'cmd-1', 'key-1', 'ai_algorithm_set',
                {'algorithm': 'smoke', 'enabled': True}, 1))
            self.assertEqual(receipt.error_code, ALGORITHM_LIFECYCLE_UNAVAILABLE)
            self.assertIn('算法粒度', receipt.message)
            self.assertEqual(fixture.transport.calls, [])
        finally:
            fixture.doCleanups()


class PlanContractTests(unittest.TestCase):
    def test_plan_is_serializable_and_read_only(self):
        plan = plan_selection(['smoke'], configured=['smoke'], active_annotations=['A'],
                              annotation_algorithms={'A': ['smoke']})
        self.assertIsInstance(plan, AlgorithmPlan)
        payload = plan.to_dict()
        json.dumps(payload)
        self.assertEqual(payload['question'], CONFLICT_QUESTION)
        self.assertIn(CONFLICT_QUESTION, CONFLICT_MESSAGE)


if __name__ == '__main__':
    unittest.main()
