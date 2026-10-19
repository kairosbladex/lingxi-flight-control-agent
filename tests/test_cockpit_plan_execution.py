"""复合指令闭环：模型规划整份计划，用户一次确认，按顺序执行。"""
import json
import unittest

from lingxi_flight_control import (
    DecisionKind, MemoryFlightControlSimulator, ModelDecision, RuntimeBinding, ToolCall,
)
from lingxi_flight_control.cockpit import CockpitService


def action(name, **arguments):
    return ModelDecision(DecisionKind.TOOL_CALL, tool_call=ToolCall(name, arguments))


def final(message='计划完成。'):
    return ModelDecision(DecisionKind.FINAL, message=message)


class Brain:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.inputs = []

    def decide(self, text, checkpoint, tools):
        self.inputs.append(text)
        return self.decisions.pop(0)


class CockpitPlanExecutionTests(unittest.TestCase):
    def setUp(self):
        self.clock = [1000.0]
        self.binding = RuntimeBinding('actor-1', 'project-1', 'hangar-1', 'drone-1', 'batch-1', 1)
        self.simulator = MemoryFlightControlSimulator(self.binding, clock=lambda: self.clock[0])
        self.simulator.state.control_owner = 'actor-1'
        self.simulator.state.longitude = 113
        self.simulator.state.latitude = 23
        self.simulator.state.altitude_m = 50
        self.simulator.state.heading_deg = 90
        self.planner = Brain([])
        self.fallback = Brain([])
        self.service = CockpitService(
            self.binding, self.simulator, lambda: self.fallback,
            clock=lambda: self.clock[0], enable_plan_execution=True,
            plan_decider_factory=lambda: self.planner, max_plan_steps=5)
        self.counter = 0

    def request(self, route, body=None, session='tab-plan'):
        response = self.service.handle(
            'POST', '/api/flight-control/' + route, json.dumps(body or {}),
            {'X-Agent-Session': session})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.payload['dispatched'])
        return response.payload

    def message(self, text):
        self.counter += 1
        return self.request('agent/messages', {'text': text,
            'request_id': 'plan-' + str(self.counter), 'context_version': 1})

    def confirm(self, card, route='confirm'):
        return self.request('agent/' + route, {
            key: card[key] for key in ('confirmation_id', 'confirmation_binding',
                                       'command_id', 'idempotency_key', 'context_version')})

    def plan(self, *decisions):
        self.planner = Brain(list(decisions))

    def test_compound_instruction_confirms_once_and_executes_both_steps(self):
        self.plan(action('move_forward', distance_m=10), action('camera_photo'), final())
        proposal = self.message('往前飞10米，并帮我拍张照')
        self.assertEqual(proposal['status'], 'preview_required')
        card = proposal['card']
        self.assertEqual(card['kind'], 'flight_plan')
        self.assertEqual(card['execution_mode'], 'simulator')
        self.assertEqual([step['action'] for step in card['steps']],
                         ['move_forward', 'camera_photo'])
        self.assertEqual(card['steps'][0]['parameters'], {'distance_m': 10})
        # 计划卡只是预览：确认前没有任何状态变化。
        self.assertEqual(self.simulator.state.longitude, 113)
        self.assertEqual(self.simulator.state.camera['photo_count'], 0)

        receipt = self.confirm(card)
        self.assertEqual(receipt['status'], 'accepted')
        self.assertEqual(receipt['plan_status'], 'completed')
        self.assertEqual([step['status'] for step in receipt['steps']],
                         ['accepted', 'accepted'])
        self.assertGreater(self.simulator.state.longitude, 113)
        self.assertEqual(self.simulator.state.camera['photo_count'], 1)
        self.assertEqual(self.fallback.inputs, [])

        after = self.simulator.state.to_dict()
        self.assertEqual(self.confirm(card), receipt)
        self.assertEqual(self.simulator.state.to_dict(), after)
        self.assertEqual(self.simulator.state.camera['photo_count'], 1)

    def test_plan_stops_at_first_failed_step_and_does_not_run_the_rest(self):
        self.plan(action('move_forward', distance_m=10), action('route_pause'), final())
        card = self.message('往前飞10米，然后暂停航线')['card']
        receipt = self.confirm(card)
        self.assertEqual(receipt['status'], 'rejected')
        self.assertEqual(receipt['plan_status'], 'stopped')
        self.assertEqual(receipt['failed_step'], 2)
        self.assertEqual(receipt['error_code'], 'ROUTE_NOT_LOADED')
        self.assertEqual([step['status'] for step in receipt['steps']],
                         ['accepted', 'rejected'])
        self.assertGreater(self.simulator.state.longitude, 113)
        self.assertEqual(self.simulator.state.route_status, 'idle')
        self.assertIn('第2步', receipt['message'])

    def test_control_acquisition_inside_the_plan_unlocks_later_steps(self):
        self.simulator.state.control_owner = None
        self.plan(action('control_acquire'), action('move_forward', distance_m=10), final())
        card = self.message('先获取控制权再向前飞10米')['card']
        self.assertEqual([step['action'] for step in card['steps']],
                         ['control_acquire', 'move_forward'])
        receipt = self.confirm(card)
        self.assertEqual(receipt['plan_status'], 'completed')
        self.assertEqual(self.simulator.state.control_owner, 'actor-1')
        self.assertGreater(self.simulator.state.longitude, 113)

    def test_plan_without_control_is_rejected_before_showing_a_card(self):
        self.simulator.state.control_owner = None
        self.plan(action('move_forward', distance_m=10), action('camera_photo'), final())
        proposal = self.message('往前飞10米，并帮我拍张照')
        self.assertEqual(proposal['status'], 'rejected')
        self.assertEqual(proposal['error_code'], 'CONTROL_NOT_OWNED')
        self.assertNotIn('card', proposal)
        self.assertEqual(self.simulator.state.longitude, 113)

    def test_state_change_between_card_and_confirmation_blocks_execution(self):
        self.plan(action('move_forward', distance_m=10), final())
        card = self.message('往前飞10米')['card']
        self.simulator.state.revision += 1
        receipt = self.confirm(card)
        self.assertEqual(receipt['status'], 'rejected')
        self.assertEqual(receipt['error_code'], 'CONFIRMATION_MISMATCH')
        self.assertEqual(self.simulator.state.longitude, 113)

    def test_cancel_executes_nothing_and_old_card_cannot_be_confirmed(self):
        self.plan(action('move_forward', distance_m=10), action('camera_photo'), final())
        card = self.message('往前飞10米，并帮我拍张照')['card']
        cancelled = self.confirm(card, 'cancel')
        self.assertEqual(cancelled['status'], 'cancelled')
        self.assertEqual(cancelled['plan_status'], 'cancelled')
        self.assertEqual(self.simulator.state.longitude, 113)
        self.assertEqual(self.simulator.state.camera['photo_count'], 0)
        self.assertEqual(self.confirm(card)['status'], 'rejected')

    def test_parameter_not_grounded_in_the_instruction_is_clarified(self):
        self.plan(action('move_forward', distance_m=5), final())
        proposal = self.message('往前飞10米')
        self.assertEqual(proposal['status'], 'clarification')
        self.assertNotIn('card', proposal)
        self.assertEqual(self.simulator.state.longitude, 113)

    def test_plan_longer_than_the_limit_is_clarified(self):
        self.plan(*([action('camera_photo')] * 6), final())
        proposal = self.message('连续拍六张照片')
        self.assertEqual(proposal['status'], 'clarification')
        self.assertIn('超过5步', proposal['message'])
        self.assertNotIn('card', proposal)
        self.assertEqual(self.simulator.state.camera['photo_count'], 0)

    def test_single_step_plan_uses_the_same_confirmation_flow(self):
        self.plan(action('camera_photo'), final())
        card = self.message('拍一张照片')['card']
        self.assertEqual(card['kind'], 'flight_plan')
        self.assertEqual(len(card['steps']), 1)
        self.assertEqual(self.confirm(card)['plan_status'], 'completed')
        self.assertEqual(self.simulator.state.camera['photo_count'], 1)

    def test_zero_step_result_is_not_turned_into_a_card(self):
        self.plan(final('当前没有需要执行的动作。'))
        proposal = self.message('现在无人机高度是多少？')
        self.assertEqual(proposal['status'], 'final')
        self.assertNotIn('card', proposal)
        self.assertEqual(self.simulator.state.revision, 0)

    def test_plan_completion_wording_is_not_mistaken_for_clarification(self):
        # 通用澄清启发式会把“请确认”判成提问；规划面不能因此丢掉已记录的步骤。
        self.plan(action('move_forward', distance_m=10),
                  ModelDecision(DecisionKind.CLARIFY,
                                message='计划已生成，共1步。请确认执行。'))
        proposal = self.message('往前飞10米')
        self.assertEqual(proposal['status'], 'preview_required')
        self.assertEqual(proposal['card']['kind'], 'flight_plan')

    def test_genuine_missing_parameter_after_a_step_still_clarifies(self):
        self.plan(action('move_forward', distance_m=10),
                  ModelDecision(DecisionKind.CLARIFY, message='请提供下一步的距离。'))
        proposal = self.message('往前飞10米')
        self.assertEqual(proposal['status'], 'clarification')
        self.assertNotIn('card', proposal)
        self.assertEqual(self.simulator.state.longitude, 113)

    def test_plan_execution_requires_verified_completion_receipts(self):
        class FakePlatformExecutor:
            execution_mode = 'platform'

            def runtime_context(self):
                return None

            def cancel(self, **_kwargs):
                return None

            def preview(self, _request):
                return None

            def confirm(self, *_args, **_kwargs):
                return None

            def read_state(self):
                return {}

        with self.assertRaises(ValueError) as raised:
            CockpitService(
                self.binding, FakePlatformExecutor(), lambda: self.fallback,
                gateway_token='x' * 32, enable_plan_execution=True,
                plan_decider_factory=lambda: self.planner)
        self.assertIn('verified completion receipts', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
