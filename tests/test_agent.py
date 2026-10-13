import unittest

from lingxi_flight_control import (
    CommandStatus,
    DispatchStatus,
    ExecutionContext,
    FlightExecutionAgent,
    TaskDispatchAgent,
    ToolCall,
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


if __name__ == "__main__":
    unittest.main()
