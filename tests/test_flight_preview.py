import math
import socket
import unittest
from unittest.mock import patch

from lingxi_flight_control import ModelDecision, DecisionKind, ToolCall, RuntimeBinding
from lingxi_flight_control.geometry import EARTH_RADIUS_M, relative_target
from lingxi_flight_control.preview import FlightPreviewAgent, ReferencePose, platform_request


class Decider:
    def __init__(self, call=None):
        self.call = call or ToolCall("move_forward", {"distance_m": 10})
        self.calls = 0

    def decide(self, text, checkpoint, tools):
        self.calls += 1
        assert all(tool.handler is None for tool in tools)
        return ModelDecision(DecisionKind.TOOL_CALL, tool_call=self.call)


class FlightPreviewTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.binding = RuntimeBinding("actor", "project", "hangar", "drone", "batch", 1)
        self.decider = Decider()
        self.agent = FlightPreviewAgent(self.binding, self.decider, clock=lambda: self.now)
        self.pose = ReferencePose(113, 23, 50, 90, 100)
        self.blocker = patch.object(socket.socket, "connect", side_effect=AssertionError("no platform network"))
        self.blocker.start()
        self.addCleanup(self.blocker.stop)

    def preview(self, text="往前飞10米", **changes):
        args = dict(command_id="c1", idempotency_key="k1", pose=self.pose, site_id="site", task_id="task")
        args.update(changes)
        return self.agent.preview(text, **args)

    def confirm(self, card, **changes):
        args = {key: card[key] for key in ("command_id", "idempotency_key", "confirmation_binding", "context_version")}
        args.update(changes)
        return self.agent.confirm(card["confirmation_id"], **args)

    def test_model_preview_and_confirm_never_execute_and_preserve_reference(self):
        card = self.preview()
        self.assertEqual(card["status"], "preview_required")
        self.assertEqual(card["parameters"], {"distance_m": 10})
        self.assertGreater(card["target"]["longitude"], 113)
        self.assertAlmostEqual(card["target"]["latitude"], 23, places=8)
        self.assertEqual(card["target"]["altitude_m"], 50)
        self.assertEqual(card["platform_request"], {"method": "POST", "path": "/screen/uav/operation/control",
                         "body": {"siteId": "site", "terminalId": "drone", "taskId": "task", "type": 202, "mode": 2, "param": 10}})
        result = self.confirm(card)
        self.assertEqual(result["status"], "confirmed_not_dispatched")
        self.assertFalse(result["dispatch_allowed"])
        self.assertFalse(result["dispatched"])
        self.assertEqual(result, self.confirm(card))
        self.assertEqual(result["origin"], self.pose.__dict__)
        self.assertEqual(self.decider.calls, 1)  # 确认锁定已展示动作，不再次问模型并替换参数。

    def test_tamper_expiry_cancel_and_context(self):
        card = self.preview()
        for changes in ({"command_id": "c2"}, {"confirmation_binding": "f" * 64}, {"context_version": 2}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.confirm(card, **changes)
        self.assertEqual(self.confirm(card, cancel=True)["status"], "cancelled")
        self.assertEqual(self.confirm(card)["status"], "cancelled")
        self.now = 221
        with self.assertRaisesRegex(ValueError, "过期"):
            self.confirm(card)

    def test_replay_and_new_intent_invalidate_previous_card(self):
        card = self.preview()
        self.assertEqual(card, self.preview())
        with self.assertRaises(ValueError):
            self.preview(text="往后10米")
        self.decider.call = ToolCall("move_backward", {"distance_m": 10})
        self.preview(text="往后10米", command_id="c2", idempotency_key="k2")
        with self.assertRaises(ValueError):
            self.confirm(card)

    def test_model_fault_unknown_tool_and_invalid_distance_produce_no_card(self):
        for name, value in (("move_forward", 21), ("move_forward", 0), ("move_forward", True), ("takeoff", 10)):
            with self.subTest(name=name, value=value):
                self.decider.call = ToolCall(name, {"distance_m": value})
                self.assertEqual(self.preview()["status"], "clarification")
        with patch.object(self.decider, "decide", side_effect=TimeoutError("test")):
            self.assertEqual(self.preview()["status"], "clarification")
        with patch.object(self.decider, "decide", return_value=ModelDecision(DecisionKind.FINAL, message="已经飞完")):
            self.assertNotIn("已经飞完", self.preview()["message"])

    def test_missing_stale_and_nonfinite_pose_rejected_before_model(self):
        for pose in (ReferencePose(113, 23, 50, math.nan, 100), ReferencePose(113, 23, 50, 90, 80)):
            with self.subTest(pose=pose), self.assertRaises(ValueError):
                self.preview(pose=pose)
        self.assertEqual(self.decider.calls, 0)

    def test_platform_distance_contract_and_id_mapping(self):
        for value in (0, 20.01, True, math.inf):
            with self.subTest(value=value), self.assertRaises(ValueError):
                platform_request(self.binding, "site", None, "move_forward", value)
        request = platform_request(self.binding, "site", None, "move_right", 20)
        self.assertNotIn("taskId", request["body"])
        self.assertEqual(request["body"]["siteId"], "site")
        self.assertEqual(request["body"]["type"], 205)


class HeadingGeometryTests(unittest.TestCase):
    def test_cardinal_headings_and_relative_directions(self):
        for heading, axis, sign in ((0, 1, 1), (90, 0, 1), (180, 1, -1), (270, 0, -1)):
            with self.subTest(heading=heading):
                target = relative_target(113, 23, heading, "forward", 10)
                self.assertGreater((target[axis] - (113, 23)[axis]) * sign, 0)
                lat1, lat2 = math.radians(23), math.radians(target[1])
                dlon = math.radians(target[0] - 113)
                arc = 2 * math.asin(math.sqrt(math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2))
                self.assertAlmostEqual(arc * EARTH_RADIUS_M, 10, places=5)
        self.assertGreater(relative_target(113, 23, 90, "left", 10)[1], 23)
        self.assertLess(relative_target(113, 23, 90, "right", 10)[1], 23)
        self.assertLess(relative_target(113, 23, 90, "backward", 10)[0], 113)

    def test_dateline_and_simulator_turn_then_move(self):
        self.assertLess(relative_target(179.99999, 0, 90, "forward", 10)[0], 0)
        from lingxi_flight_control import MemoryFlightControlSimulator, FlightControlActionRequest
        sim = MemoryFlightControlSimulator(RuntimeBinding("a", "p", "h", "d", "b", 1))
        sim.state.longitude, sim.state.latitude = 113, 23
        for action, params in (("control_acquire", {}), ("turn_right", {"angle_deg": 90}), ("move_forward", {"distance_m": 10})):
            req = FlightControlActionRequest(action, action, action, params, 1)
            card = sim.preview(req)
            sim.confirm(card.confirmation_id, command_id=action, idempotency_key=action,
                        confirmation_binding=card.confirmation_binding, context_version=1)
        self.assertGreater(sim.state.longitude, 113)
        self.assertAlmostEqual(sim.state.latitude, 23, places=8)
