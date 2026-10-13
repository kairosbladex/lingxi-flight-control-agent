import unittest

from lingxi_flight_control import CommandStatus, ExecutionContext, RelativeMove
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


if __name__ == "__main__":
    unittest.main()
