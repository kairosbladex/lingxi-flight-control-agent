import unittest

from lingxi_flight_control import DispatchStatus, TaskDispatchAgent, ToolCall


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


if __name__ == "__main__":
    unittest.main()
