"""V1.4 任务事件：typed 契约、幂等账本、低电量优先级与旧事件拒绝。

事件源是合成夹具；本仓没有真实平台事件桥。
"""
import json
from pathlib import Path
import unittest
from types import SimpleNamespace

from lingxi_flight_control.full_control import ACTION_SPECS, MemoryFlightControlSimulator, RuntimeBinding
from lingxi_flight_control.task_events import (
    TaskEvent, TaskEventError, TaskEventKind, TaskEventLedger, UnavailableTaskEventBridge,
    HttpTaskEventBridge,
    apply_task_event, coerce_kind,
)

FIXTURE = Path(__file__).resolve().parent.parent / 'fixtures' / 'v14-task-events-v1.json'


def load_fixture():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def binding():
    return RuntimeBinding('actor-1', 'project-1', 'hangar-1', 'drone-1', 'batch-1', 1)


def simulator(**state):
    sim = MemoryFlightControlSimulator(binding(), clock=lambda: 1000.0)
    sim.state.control_owner = 'actor-1'
    sim.state.route_id = 'route-main-1'
    sim.state.route_status = 'running'
    sim.state.breakpoint_index = 4
    sim.state.task_status = 'route_executing'
    sim.state.task_type = 'inspection'
    for key, value in state.items():
        setattr(sim.state, key, value)
    return sim


def typed_event(item, *, event_batch=None):
    return TaskEvent(
        event_id=item['event_id'], kind=TaskEventKind(item['kind']),
        project_id='project-1', hangar_id='hangar-1', drone_id='drone-1',
        task_batch_id=event_batch or item.get('task_batch_id', 'batch-1'),
        sequence=item['sequence'], occurred_at=1000.0,
        temporary_task_id=item.get('temporary_task_id'),
        annotation_id=item.get('annotation_id'),
        state_version=item.get('state_version'),
    )


class EventContractTests(unittest.TestCase):
    def test_http_bridge_decodes_typed_event_batch(self):
        class Transport:
            def read_task_events(self, binding, **kwargs):
                return SimpleNamespace(status_code=200, payload={'code': 200, 'data': [{
                    'eventId': 'event-1', 'kind': 'low_battery_return',
                    'projectId': 'project-1', 'hangarId': 'hangar-1',
                    'droneId': 'drone-1', 'taskBatchId': 'batch-1',
                    'sequence': 2, 'occurredAt': 1001.0}]})
        events = HttpTaskEventBridge(Transport(), site_id='site').read_events(
            binding(), after_sequence=1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, TaskEventKind.LOW_BATTERY_RETURN)
        self.assertEqual(events[0].sequence, 2)

    def test_http_bridge_does_not_turn_upstream_failure_into_empty_events(self):
        class Transport:
            def read_task_events(self, binding, **kwargs):
                raise TimeoutError('bounded timeout')
        with self.assertRaises(TaskEventError) as caught:
            HttpTaskEventBridge(Transport(), site_id='site').read_events(binding())
        self.assertEqual(caught.exception.code, 'TASK_EVENT_SOURCE_UNAVAILABLE')

    def test_event_requires_identity_sequence_and_binding(self):
        for change, code in (
            ({'event_id': ''}, 'INVALID_TASK_EVENT'),
            ({'sequence': -1}, 'INVALID_TASK_EVENT'),
            ({'sequence': '1'}, 'INVALID_TASK_EVENT'),
            ({'occurred_at': float('nan')}, 'INVALID_TASK_EVENT'),
            ({'kind': TaskEventKind.TEMPORARY_TASK_COMPLETED}, 'INVALID_TASK_EVENT'),
            ({'kind': TaskEventKind.ANNOTATION_ENTERED, 'annotation_id': None}, 'INVALID_TASK_EVENT'),
            ({'task_batch_id': ''}, 'TASK_EVENT_BINDING_MISMATCH'),
        ):
            with self.subTest(change=change):
                values = dict(event_id='ev-1', kind=TaskEventKind.ALL_TASKS_ENDED,
                              project_id='project-1', hangar_id='hangar-1', drone_id='drone-1',
                              task_batch_id='batch-1', sequence=0, occurred_at=1000.0,
                              temporary_task_id='temp-a', annotation_id='ann-1')
                values.update(change)
                event = TaskEvent(**values)
                with self.assertRaises(TaskEventError) as caught:
                    event.validate()
                self.assertEqual(caught.exception.code, code)

    def test_legacy_names_are_upgraded_not_reinterpreted(self):
        self.assertIs(coerce_kind('temporary_completed'), TaskEventKind.TEMPORARY_TASK_COMPLETED)
        self.assertIs(coerce_kind('all_tasks_ended'), TaskEventKind.ALL_TASKS_ENDED)
        with self.assertRaises(TaskEventError):
            coerce_kind('unknown_event')

    def test_ledger_is_idempotent_and_rejects_old_sequence(self):
        ledger = TaskEventLedger()
        event = typed_event({'event_id': 'ev-1', 'kind': 'all_tasks_ended', 'sequence': 3})
        self.assertEqual(ledger.accept(event, binding()), 'applied')
        self.assertEqual(ledger.accept(event, binding()), 'DUPLICATE_TASK_EVENT')
        with self.assertRaises(TaskEventError) as caught:
            ledger.accept(typed_event({'event_id': 'ev-2', 'kind': 'all_tasks_ended', 'sequence': 2}),
                          binding())
        self.assertEqual(caught.exception.code, 'STALE_TASK_EVENT')
        summary = ledger.snapshot()
        self.assertEqual((summary['applied'], summary['duplicates'], summary['last_sequence']), (1, 1, 3))

    def test_ledger_rejects_foreign_binding(self):
        ledger = TaskEventLedger()
        with self.assertRaises(TaskEventError) as caught:
            ledger.accept(typed_event({'event_id': 'ev-1', 'kind': 'all_tasks_ended',
                                       'sequence': 0}, event_batch='batch-2'), binding())
        self.assertEqual(caught.exception.code, 'TASK_EVENT_BINDING_MISMATCH')


class ScenarioTests(unittest.TestCase):
    """按夹具场景驱动模拟器；每个场景独立一份状态。"""

    def run_scenario(self, scenario):
        sim = simulator()
        for item in scenario['events']:
            event = typed_event(item)
            apply_task_event(sim.state, sim._task_events, event, sim.binding)
        return sim

    def test_fixture_scenarios(self):
        for scenario in load_fixture()['scenarios']:
            with self.subTest(scenario=scenario['name']):
                if 'expect_error' in scenario:
                    sim = simulator()
                    with self.assertRaises(TaskEventError) as caught:
                        for item in scenario['events']:
                            apply_task_event(sim.state, sim._task_events, typed_event(item), sim.binding)
                    self.assertEqual(caught.exception.code, scenario['expect_error'])
                    continue
                sim = self.run_scenario(scenario)
                for key, value in scenario['expect'].items():
                    self.assertEqual(getattr(sim.state, key), value, key)
                if 'expect_duplicates' in scenario:
                    self.assertEqual(sim.task_event_ledger()['duplicates'], scenario['expect_duplicates'])

    def test_low_battery_clears_temporary_task_and_keeps_main_breakpoint(self):
        sim = simulator(temporary_task_id='temp-a', breakpoint_index=None, route_status='point_flight')
        sim.apply_task_event('low_battery_return', task_batch_id='batch-1')
        self.assertTrue(sim.state.low_battery_return)
        self.assertIsNone(sim.state.temporary_task_id)
        self.assertEqual(sim.state.breakpoint_index, 0)
        self.assertEqual(sim.state.task_status, 'returning_home')

    def test_temporary_completion_after_low_battery_is_rejected(self):
        sim = simulator(temporary_task_id='temp-a')
        sim.apply_task_event('low_battery_return', task_batch_id='batch-1')
        with self.assertRaises(TaskEventError) as caught:
            sim.apply_task_event('temporary_completed', task_batch_id='batch-1',
                                 temporary_task_id='temp-a')
        self.assertEqual(caught.exception.code, 'LOW_BATTERY_RETURN_PRIORITY')

    def test_recharged_online_never_resumes_after_mapping_invalidated(self):
        sim = simulator(task_type='mapping')
        sim.apply_task_event('low_battery_return', task_batch_id='batch-1')
        sim.apply_task_event('mapping_invalidated', task_batch_id='batch-1')
        sim.apply_task_event('recharged_online', task_batch_id='batch-1')
        self.assertEqual(sim.state.route_status, 'ended')
        self.assertIsNone(sim.state.breakpoint_index)
        self.assertFalse(sim.state.auto_stitching)

    def test_recharged_without_low_battery_is_rejected(self):
        sim = simulator()
        with self.assertRaises(TaskEventError) as caught:
            sim.apply_task_event('recharged_online', task_batch_id='batch-1')
        self.assertEqual(caught.exception.code, 'UNEXPECTED_RECHARGE_EVENT')

    def test_annotation_lifecycle_is_bound_to_configured_algorithms(self):
        sim = simulator()
        sim.state.configured_algorithms = ['smoke']
        sim.state.annotation_algorithms = {'ann-a': ['smoke'], 'ann-b': ['smoke']}
        sim.apply_task_event('annotation_entered', task_batch_id='batch-1', annotation_id='ann-a')
        self.assertEqual(sim.state.active_annotations, ['ann-a'])
        self.assertEqual(sim.state.active_algorithms, ['smoke'])
        sim.apply_task_event('annotation_entered', task_batch_id='batch-1', annotation_id='ann-b')
        sim.apply_task_event('annotation_left', task_batch_id='batch-1', annotation_id='ann-a')
        # 另一个标注仍引用同一算法：不能误关。
        self.assertEqual(sim.state.active_algorithms, ['smoke'])
        sim.apply_task_event('annotation_left', task_batch_id='batch-1', annotation_id='ann-b')
        self.assertEqual(sim.state.active_algorithms, [])
        with self.assertRaises(TaskEventError) as caught:
            sim.apply_task_event('annotation_entered', task_batch_id='batch-1', annotation_id='ann-x')
        self.assertEqual(caught.exception.code, 'UNKNOWN_ANNOTATION')

    def test_end_all_clears_breakpoint_and_cannot_be_undone_by_old_event(self):
        sim = simulator(temporary_task_id='temp-a')
        sim.apply_task_event('all_tasks_ended', task_batch_id='batch-1')
        self.assertIsNone(sim.state.breakpoint_index)
        self.assertIsNone(sim.state.temporary_task_id)
        self.assertEqual(sim.state.route_status, 'ended')
        with self.assertRaises(TaskEventError):
            sim.apply_task_event('temporary_completed', task_batch_id='batch-1',
                                 temporary_task_id='temp-a')

    def test_wrong_batch_id_is_rejected_by_the_public_seam(self):
        sim = simulator()
        with self.assertRaises(ValueError):
            sim.apply_task_event('all_tasks_ended', task_batch_id='batch-2')

    def test_duplicate_delivery_does_not_double_advance_revision(self):
        sim = simulator()
        sim.apply_task_event('all_tasks_ended', task_batch_id='batch-1', event_id='ev-x', sequence=0)
        revision = sim.state.revision
        sim.apply_task_event('all_tasks_ended', task_batch_id='batch-1', event_id='ev-x', sequence=0)
        self.assertEqual(sim.state.revision, revision)
        self.assertEqual(sim.task_event_ledger()['duplicates'], 1)

    def test_task_events_are_not_model_tools(self):
        sim = simulator()
        registry = sim.tool_registry()
        tool_names = {definition.name for definition in registry.definitions()}
        for kind in TaskEventKind:
            self.assertNotIn(kind.value, tool_names)
            self.assertFalse(hasattr(sim, kind.value))
        self.assertFalse(any('task_event' in name for name in tool_names))
        for action in ACTION_SPECS:
            self.assertNotIn(action.value, {kind.value for kind in TaskEventKind})


class BridgeBoundaryTests(unittest.TestCase):
    def test_default_bridge_is_typed_unavailable_not_empty(self):
        bridge = UnavailableTaskEventBridge()
        with self.assertRaises(TaskEventError) as caught:
            bridge.read_events(binding(), after_sequence=-1)
        self.assertEqual(caught.exception.code, 'TASK_EVENT_SOURCE_UNAVAILABLE')


if __name__ == '__main__':
    unittest.main()
