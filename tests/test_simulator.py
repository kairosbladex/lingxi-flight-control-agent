import math
import unittest

from lingxi_flight_control import (
    AgentCheckpoint,
    BoundedAgentLoop,
    CommandStatus,
    DecisionKind,
    ExecutionContext,
    HoverAction,
    LoopStatus,
    ModelDecision,
    RelativeMove,
    ToolCall,
)
from lingxi_flight_control.simulator import MemoryFlightSimulator


class MemoryFlightSimulatorTests(unittest.TestCase):
    def setUp(self):
        self.context = ExecutionContext("execution-1", "drone-1", 3, True)
        self.simulator = MemoryFlightSimulator(self.context)

    def test_duplicate_command_is_not_applied_twice(self):
        move = RelativeMove("right", 4, "command-1")
        self.simulator.relative_move(self.context, move)
        result = self.simulator.relative_move(self.context, move)
        self.assertEqual(result.status, CommandStatus.DUPLICATE)
        self.assertEqual(result.position.x_m, 4)

    def test_stale_context_is_rejected(self):
        stale = ExecutionContext("execution-1", "drone-1", 2, True)
        result = self.simulator.relative_move(stale, RelativeMove("up", 2, "command-2"))
        self.assertEqual(result.status, CommandStatus.STALE_CONTEXT)
        self.assertEqual(result.position, self.simulator.position)

    def test_unconfirmed_context_is_rejected_before_execution(self):
        with self.assertRaises(ValueError):
            MemoryFlightSimulator(ExecutionContext("execution-1", "drone-1", 1, False))
        for invalid in (
            ExecutionContext("execution-1", "drone-1", True, True),
            ExecutionContext("execution-1", "drone-1", 1.0, True),
            ExecutionContext("execution-1", "drone-1", -1, True),
        ):
            with self.assertRaises(ValueError):
                invalid.validate()

    def test_hover_is_idempotent_and_rejects_stale_context(self):
        action = HoverAction(5, "hover-1")
        accepted = self.simulator.hover(self.context, action)
        duplicate = self.simulator.hover(self.context, action)
        stale = self.simulator.hover(ExecutionContext("execution-1", "drone-1", 2, True), HoverAction(5, "hover-2"))
        self.assertEqual(accepted.status, CommandStatus.ACCEPTED)
        self.assertEqual(duplicate.status, CommandStatus.DUPLICATE)
        self.assertEqual(stale.status, CommandStatus.STALE_CONTEXT)
        self.assertEqual(self.simulator.position.x_m, 0)
        self.assertEqual(self.simulator.position.y_m, 0)
        self.assertEqual(self.simulator.position.z_m, 0)

    def test_exact_relative_and_hover_upper_bounds_are_accepted(self):
        move = self.simulator.relative_move(self.context, RelativeMove("forward", 100, "max-move"))
        hover = self.simulator.hover(self.context, HoverAction(300, "max-hover"))
        self.assertEqual(move.status, CommandStatus.ACCEPTED)
        self.assertEqual(hover.status, CommandStatus.ACCEPTED)
        self.assertEqual(self.simulator.position.y_m, 100)

    def test_memory_tool_registry_exposes_relative_move_and_hover(self):
        registry = self.simulator.tool_registry()
        self.assertEqual({tool.name for tool in registry.definitions()}, {"relative_move", "hover"})
        definitions = {tool.name: tool for tool in registry.definitions()}
        self.assertIsNone(definitions["relative_move"].handler)
        self.assertEqual(definitions["relative_move"].schema.properties["direction"].enum,
                         ("forward", "backward", "left", "right", "up", "down"))
        self.assertEqual(definitions["relative_move"].schema.properties["distance_m"].maximum, 100)
        self.assertTrue(definitions["relative_move"].schema.properties["distance_m"].exclusive_minimum)
        self.assertEqual(definitions["hover"].schema.properties["duration_s"].maximum, 300)

    def test_registry_binds_live_simulator_runtime_context(self):
        registry = self.simulator.tool_registry()
        self.assertEqual(registry.runtime_context(), self.context)
        self.simulator.context = ExecutionContext("execution-1", "drone-1", 4, True)
        self.assertEqual(registry.runtime_context(), self.simulator.context)
        result = registry.get("relative_move").handler({
            "direction": "forward", "distance_m": 2, "idempotency_key": "live-context"
        })
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(self.simulator.position.y_m, 2)

    def test_checkpoint_runtime_context_round_trip_and_fresh_resume(self):
        class Resume:
            def __init__(self):
                self.calls = 0

            def decide(self, _, checkpoint, __):
                self.calls += 1
                if self.calls == 1:
                    return ModelDecision(DecisionKind.CLARIFY, message="继续执行")
                if not checkpoint.observations:
                    return ModelDecision(DecisionKind.TOOL_CALL, tool_call=ToolCall(
                        "relative_move", {
                            "direction": "forward", "distance_m": 3, "idempotency_key": "resume-1"
                        }
                    ))
                return ModelDecision(DecisionKind.FINAL, message="完成")

        decider = Resume()
        pending = BoundedAgentLoop(decider, self.simulator.tool_registry()).run("继续")
        restored = AgentCheckpoint.from_dict(pending.checkpoint.to_dict())
        self.assertEqual(restored.runtime_context, self.context.to_dict())
        done = BoundedAgentLoop(decider, self.simulator.tool_registry()).run(
            "继续", checkpoint=restored
        )
        self.assertEqual(done.status, LoopStatus.FINAL)
        self.assertEqual(done.checkpoint.observations[0]["result"]["status"], "accepted")
        self.assertEqual(self.simulator.position.y_m, 3)

    def test_restored_checkpoint_rejects_new_runtime_before_decider(self):
        class Resume:
            def __init__(self):
                self.calls = 0

            def decide(self, *_):
                self.calls += 1
                raise AssertionError("stale checkpoint must stop before the decider")

        class Pending:
            def decide(self, *_):
                return ModelDecision(DecisionKind.CLARIFY, message="暂停")

        pending = BoundedAgentLoop(Pending(), self.simulator.tool_registry()).run("继续")
        restored = AgentCheckpoint.from_dict(pending.checkpoint.to_dict())
        self.simulator.context = ExecutionContext("execution-1", "drone-1", 4, True)
        decider = Resume()
        result = BoundedAgentLoop(decider, self.simulator.tool_registry()).run(
            "继续", checkpoint=restored
        )
        self.assertEqual(result.status, LoopStatus.TOOL_ERROR)
        self.assertEqual(result.error["code"], "stale_context")
        self.assertEqual(decider.calls, 0)
        self.assertEqual(result.checkpoint.tool_calls, pending.checkpoint.tool_calls)
        self.assertEqual(result.checkpoint.observations, pending.checkpoint.observations)
        self.assertEqual(self.simulator.position.y_m, 0)

    def test_restored_checkpoint_rejects_runtime_identity_change(self):
        class Pending:
            def decide(self, *_):
                return ModelDecision(DecisionKind.CLARIFY, message="暂停")

        pending = BoundedAgentLoop(Pending(), self.simulator.tool_registry()).run("继续")
        for stale in (
            ExecutionContext("execution-2", "drone-1", 3, True),
            ExecutionContext("execution-1", "drone-2", 3, True),
        ):
            with self.subTest(stale=stale):
                self.simulator.context = stale
                restored = AgentCheckpoint.from_dict(pending.checkpoint.to_dict())
                result = BoundedAgentLoop(Pending(), self.simulator.tool_registry()).run(
                    "继续", checkpoint=restored
                )
                self.assertEqual(result.status, LoopStatus.TOOL_ERROR)
                self.assertEqual(result.error["code"], "stale_context")
                self.assertEqual(result.checkpoint.observations, pending.checkpoint.observations)
                self.assertEqual(self.simulator.position, self.simulator.position.__class__())

    def test_runtime_change_during_decision_is_rejected_before_handler(self):
        class SwitchRuntime:
            def __init__(self):
                self.calls = 0

            def decide(self, *_):
                self.calls += 1
                self.simulator.context = ExecutionContext("execution-1", "drone-1", 4, True)
                return ModelDecision(DecisionKind.TOOL_CALL, tool_call=ToolCall(
                    "relative_move", {
                        "direction": "forward", "distance_m": 2, "idempotency_key": "race-1"
                    }
                ))

        decider = SwitchRuntime()
        decider.simulator = self.simulator
        result = BoundedAgentLoop(decider, self.simulator.tool_registry()).run("继续")
        self.assertEqual(result.status, LoopStatus.TOOL_ERROR)
        self.assertEqual(result.error["code"], "stale_context")
        self.assertEqual(decider.calls, 1)
        self.assertEqual(result.checkpoint.tool_calls, [])
        self.assertEqual(self.simulator.position, self.simulator.position.__class__())

    def test_relative_move_and_hover_tools_execute_through_bounded_loop(self):
        class MoveThenHover:
            def decide(self, _, checkpoint, __):
                if not checkpoint.observations:
                    return ModelDecision(DecisionKind.TOOL_CALL, tool_call=ToolCall(
                        "relative_move", {"direction": "forward", "distance_m": 10, "idempotency_key": "move-1"}
                    ))
                if len(checkpoint.observations) == 1:
                    return ModelDecision(DecisionKind.TOOL_CALL, tool_call=ToolCall(
                        "hover", {"duration_s": 5, "idempotency_key": "hover-1"}
                    ))
                return ModelDecision(DecisionKind.FINAL, message="模拟动作完成")
        result = BoundedAgentLoop(MoveThenHover(), self.simulator.tool_registry()).run("向前飞十米并悬停")
        self.assertEqual(result.status, LoopStatus.FINAL)
        self.assertEqual(result.checkpoint.observations[0]["result"]["status"], "accepted")
        self.assertEqual(result.checkpoint.observations[1]["result"]["status"], "accepted")
        self.assertEqual(result.checkpoint.observations[1]["result"]["position"]["y_m"], 10)
        self.assertEqual(self.simulator.position.y_m, 10)

    def test_registered_tool_replay_returns_duplicate_without_moving_again(self):
        class Replay:
            def decide(self, _, checkpoint, __):
                if len(checkpoint.observations) < 2:
                    return ModelDecision(DecisionKind.TOOL_CALL, tool_call=ToolCall(
                        "relative_move", {"direction": "forward", "distance_m": 10, "idempotency_key": "same"}
                    ))
                return ModelDecision(DecisionKind.FINAL, message="完成")
        result = BoundedAgentLoop(Replay(), self.simulator.tool_registry()).run("重放测试")
        self.assertEqual(result.status, LoopStatus.FINAL)
        self.assertEqual([item["result"]["status"] for item in result.checkpoint.observations],
                         ["accepted", "duplicate"])
        self.assertEqual(self.simulator.position.y_m, 10)

    def test_memory_tool_schemas_reject_bounds_missing_and_extra_fields(self):
        class Invalid:
            def __init__(self, call):
                self.call = call
            def decide(self, *_):
                return ModelDecision(DecisionKind.TOOL_CALL, tool_call=self.call)
        calls = (
            ToolCall("relative_move", {"direction": "forward", "distance_m": 0, "idempotency_key": "x"}),
            ToolCall("relative_move", {"direction": "forward", "distance_m": 101, "idempotency_key": "x"}),
            ToolCall("relative_move", {"direction": "diagonal", "distance_m": 1, "idempotency_key": "x"}),
            ToolCall("relative_move", {"direction": "forward", "distance_m": 1}),
            ToolCall("relative_move", {"direction": "forward", "distance_m": True, "idempotency_key": "x"}),
            ToolCall("relative_move", {"direction": "forward", "distance_m": math.nan, "idempotency_key": "x"}),
            ToolCall("relative_move", {"direction": "forward", "distance_m": math.inf, "idempotency_key": "x"}),
            ToolCall("relative_move", {"direction": "forward", "distance_m": 1, "idempotency_key": ""}),
            ToolCall("hover", {"duration_s": 301, "idempotency_key": "x"}),
            ToolCall("hover", {"duration_s": 0, "idempotency_key": "x"}),
            ToolCall("hover", {"duration_s": True, "idempotency_key": "x"}),
            ToolCall("hover", {"duration_s": 1, "idempotency_key": "x", "extra": True}),
        )
        for call in calls:
            result = BoundedAgentLoop(Invalid(call), self.simulator.tool_registry()).run("测试")
            # 参数写错不再让 run 直接死掉：模型会拿到校验错误并有机会改正，
            # 一直改不对则由 max_steps 截停并把最后一个错误带出来。
            # NaN/Inf 走另一条硬失败路径——它们根本无法序列化、也就无法回放给模型。
            self.assertIn(result.status, (LoopStatus.MAX_STEPS_EXCEEDED, LoopStatus.TOOL_ERROR), call)
            self.assertIsNotNone(result.error, call)
            self.assertEqual(result.checkpoint.executed_tool_calls, [], call)
        self.assertEqual(self.simulator.position, self.simulator.position.__class__())


if __name__ == "__main__":
    unittest.main()
