import unittest

from lingxi_flight_control import (
    AgentCheckpoint,
    BoundedAgentLoop,
    CommandStatus,
    DecisionKind,
    DispatchStatus,
    ExecutionContext,
    FlightExecutionAgent,
    LoopStatus,
    ModelDecision,
    TaskDispatchAgent,
    ToolCall,
    ToolDefinition,
    ToolParameter,
    ToolRegistry,
    ToolSchema,
    parse_relative_move,
)
from lingxi_flight_control.simulator import MemoryFlightSimulator


class TaskDispatchAgentTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def interpreter(text):
            return ToolCall("create_task", {"description": text})

        def create_task(arguments):
            self.calls.append(arguments)
            return {"task_id": "draft-1"}

        self.agent = TaskDispatchAgent(interpreter, {"create_task": create_task})

    def test_requires_confirmation_before_tool_call(self):
        result = self.agent.handle("去园区巡检")
        self.assertEqual(result.status, DispatchStatus.CONFIRMATION_REQUIRED)
        self.assertEqual(self.calls, [])

    def test_confirmed_intent_calls_only_registered_tool(self):
        result = self.agent.handle("去园区巡检", confirmed=True)
        self.assertEqual(result.status, DispatchStatus.DISPATCHED)
        self.assertEqual(result.output, {"task_id": "draft-1"})
        self.assertEqual(self.calls, [{"description": "去园区巡检"}])

    def test_unknown_tool_is_rejected(self):
        agent = TaskDispatchAgent(lambda _: ToolCall("delete_everything", {}), {})
        result = agent.handle("清理")
        self.assertEqual(result.status, DispatchStatus.REJECTED)
        self.assertEqual(self.calls, [])

    def test_flight_text_parse_and_execute(self):
        context = ExecutionContext("execution-1", "drone-1", 1, True)
        simulator = MemoryFlightSimulator(context)
        agent = FlightExecutionAgent(context, simulator)

        result = agent.handle("向前 10 米", idempotency_key="command-1")

        self.assertEqual(result.status, CommandStatus.ACCEPTED)
        self.assertEqual(result.position.y_m, 10)

    def test_parser_rejects_multiple_or_out_of_range_commands(self):
        with self.assertRaises(ValueError):
            parse_relative_move("向前10米再向右2米", idempotency_key="command-1")
        with self.assertRaises(ValueError):
            parse_relative_move("向前101米", idempotency_key="command-1")


class BoundedAgentLoopTests(unittest.TestCase):
    def definition(self, name="read_value", handler=None, *, side_effect=False):
        return ToolDefinition(
            name, "test tool", ToolSchema({"value": ToolParameter("integer", minimum=0, maximum=10)}, ("value",)),
            handler or (lambda args: {"value": args["value"]}), side_effect=side_effect,
        )

    def test_final_and_clarification_end_loop(self):
        for decision, status in ((ModelDecision(DecisionKind.FINAL, message="完成"), LoopStatus.FINAL),
                                 (ModelDecision(DecisionKind.CLARIFY, message="请补充"), LoopStatus.CLARIFICATION)):
            class Decider:
                def decide(self, *_): return decision
            result = BoundedAgentLoop(Decider(), ToolRegistry()).run("测试")
            self.assertEqual(result.status, status)

    def test_tool_call_observation_then_final(self):
        calls = []
        class Decider:
            def decide(self, _, checkpoint, __):
                if not checkpoint.observations:
                    return ModelDecision(DecisionKind.TOOL_CALL, tool_call=ToolCall("read_value", {"value": 3}))
                return ModelDecision(DecisionKind.FINAL, message="完成")
        registry = ToolRegistry({"read_value": self.definition(handler=lambda args: calls.append(args) or {"ok": True})})
        result = BoundedAgentLoop(Decider(), registry).run("查询")
        self.assertEqual(result.status, LoopStatus.FINAL)
        self.assertEqual(calls, [{"value": 3}])

    def test_unknown_bad_arguments_and_exception_are_tool_errors(self):
        class Unknown:
            def decide(self, *_): return ModelDecision(DecisionKind.TOOL_CALL, tool_call=ToolCall("missing", {}))
        result = BoundedAgentLoop(Unknown(), ToolRegistry()).run("查询")
        self.assertEqual(result.status, LoopStatus.TOOL_ERROR)
        class Bad:
            def decide(self, *_): return ModelDecision(DecisionKind.TOOL_CALL, tool_call=ToolCall("read_value", {"value": 99}))
        result = BoundedAgentLoop(Bad(), ToolRegistry({"read_value": self.definition()})).run("查询")
        self.assertEqual(result.status, LoopStatus.TOOL_ERROR)
        class Explodes:
            def decide(self, *_): return ModelDecision(DecisionKind.TOOL_CALL, tool_call=ToolCall("read_value", {"value": 1}))
        result = BoundedAgentLoop(Explodes(), ToolRegistry({"read_value": self.definition(handler=lambda _: 1 / 0)})).run("查询")
        self.assertEqual(result.error["code"], "tool_exception")

    def test_max_steps_and_side_effect_confirmation(self):
        class Loop:
            def decide(self, *_): return ModelDecision(DecisionKind.TOOL_CALL, tool_call=ToolCall("read_value", {"value": 1}))
        result = BoundedAgentLoop(Loop(), ToolRegistry({"read_value": self.definition()}), max_steps=2).run("查询")
        self.assertEqual(result.status, LoopStatus.MAX_STEPS_EXCEEDED)
        calls = []
        class Create:
            def decide(self, _, checkpoint, __):
                if checkpoint.observations:
                    return ModelDecision(DecisionKind.FINAL, message="已生成草稿")
                return ModelDecision(DecisionKind.TOOL_CALL, tool_call=ToolCall("create_task", {"value": 1}))
        registry = ToolRegistry({"create_task": self.definition("create_task", lambda args: calls.append(args) or "created", side_effect=True)})
        pending = BoundedAgentLoop(Create(), registry).run("创建")
        self.assertEqual(pending.status, LoopStatus.CLARIFICATION)
        self.assertEqual(calls, [])
        done = BoundedAgentLoop(Create(), registry).run("创建", checkpoint=pending.checkpoint, confirmed=True)
        self.assertEqual(done.status, LoopStatus.FINAL)
        self.assertEqual(calls, [{"value": 1}])

    def test_survey_and_unsafe_model_output_are_blocked(self):
        class Never:
            def decide(self, *_): raise AssertionError("must clarify first")
        result = BoundedAgentLoop(Never(), ToolRegistry()).run("做测绘任务")
        self.assertEqual(result.status, LoopStatus.CLARIFICATION)
        class Unsafe:
            def decide(self, *_): return ModelDecision(DecisionKind.FINAL, message="MQTT payload 执行成功")
        result = BoundedAgentLoop(Unsafe(), ToolRegistry()).run("测试")
        self.assertEqual(result.status, LoopStatus.TOOL_ERROR)


if __name__ == "__main__":
    unittest.main()
