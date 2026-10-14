import math
import unittest

from lingxi_flight_control import (
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
            self.assertEqual(result.status, LoopStatus.TOOL_ERROR, call)
        self.assertEqual(self.simulator.position, self.simulator.position.__class__())


if __name__ == "__main__":
    unittest.main()
