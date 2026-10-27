"""V1.3 悬停与指点/环绕动作：模拟器语义 + 平台折算。

对应设计规范 2.4（悬停等价暂停、手动模式原地悬停）与 4.5（指点飞行、环绕飞行）。
平台没有统一“悬停”操作码，因此这里的重点是：**悬停绝不降级成紧急停止**，
并且按当前运动方式选择真正的取消指令。
"""

import tempfile
import unittest

from lingxi_flight_control.geometry import offset_target
from lingxi_flight_control.full_control import (
    ACTION_SPECS, FlightControlAction, FlightControlActionRequest, FlightControlStatus,
    MemoryFlightControlSimulator, RuntimeBinding,
)
from lingxi_flight_control.ktd_platform import (
    KtdHttpResponse, KtdPlatformExecutor, KtdSnapshot, SqliteCommandJournal,
)


class _Transport:
    def __init__(self):
        self.calls = []

    def submit(self, command, context):
        self.calls.append(command)
        return KtdHttpResponse(200, {"code": 200})


class SimulatorHoverTests(unittest.TestCase):
    def setUp(self):
        self.binding = RuntimeBinding("actor-1", "project-1", "hangar-1", "drone-01", "batch-1", 1)
        self.sim = MemoryFlightControlSimulator(self.binding)
        self.sim.state.control_owner = "actor-1"
        self.sim.state.task_status = "flying"
        self.sim.state.altitude_m = 50.0

    def run_hover(self, **parameters):
        card = self.sim.preview(FlightControlActionRequest(
            "cmd-1", "key-1", FlightControlAction.HOVER, parameters, 1))
        self.assertIs(card.status, FlightControlStatus.PREVIEW_REQUIRED, card.to_dict())
        return self.sim.confirm(
            card.confirmation_id, command_id="cmd-1", idempotency_key="key-1",
            confirmation_binding=card.confirmation_binding, context_version=1)

    def test_hover_clears_speed_without_touching_task_state(self):
        self.sim.state.ground_speed_mps = 3.0
        self.sim.state.vertical_speed_mps = 1.0
        receipt = self.run_hover()
        self.assertIs(receipt.status, FlightControlStatus.ACCEPTED, receipt.to_dict())
        self.assertEqual(self.sim.state.ground_speed_mps, 0.0)
        self.assertEqual(self.sim.state.vertical_speed_mps, 0.0)
        self.assertEqual(self.sim.state.task_status, "flying")
        self.assertNotEqual(self.sim.state.task_status, "emergency_stopped")
        self.assertEqual(self.sim.state.last_action, "hover")

    def test_hover_during_route_pauses_and_keeps_breakpoint(self):
        self.sim.state.route_id = "route-1"
        self.sim.state.route_status = "running"
        receipt = self.run_hover()
        self.assertIs(receipt.status, FlightControlStatus.ACCEPTED, receipt.to_dict())
        self.assertEqual(self.sim.state.route_status, "paused")
        self.assertEqual(self.sim.state.breakpoint_index, 0)
        self.assertFalse(self.sim.state.mapping_invalidated)

    def test_hover_rejects_parameters_and_needs_control(self):
        with self.assertRaises(ValueError):
            FlightControlActionRequest("cmd-2", "key-2", "hover", {"distance_m": 5}, 1).validate()
        self.sim.state.control_owner = None
        card = self.sim.preview(FlightControlActionRequest("cmd-3", "key-3", "hover", {}, 1))
        self.assertIs(card.status, FlightControlStatus.REJECTED)
        self.assertEqual(card.error_code, "CONTROL_NOT_OWNED")

    def test_hover_is_a_model_visible_tool(self):
        spec = ACTION_SPECS[FlightControlAction.HOVER]
        self.assertEqual(spec.domain, "flight")
        self.assertEqual(dict(spec.parameters), {})
        self.assertIn("hover", self.sim.tool_registry().get("hover").name)


class PlatformHoverTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.binding = RuntimeBinding("actor-1", "project-1", "hangar-1", "drone-1", "batch-1", 1)
        self.now = 1000.0
        self.transport = _Transport()
        self.state = {"online": True, "airborne": True, "longitude": 113.0, "latitude": 23.0,
                      "altitude_m": 100.0, "height_m": 50.0, "yaw_deg": 90.0,
                      "route_status": "hover"}
        self.capabilities = {"hover": True, "point_flight": True, "point_orbit": True,
                             "move_forward": True, "UAV_MOVE_DISTANCE": True}
        self.snapshot = self.build_snapshot()

    def build_snapshot(self, **overrides):
        fields = dict(
            actor_id="actor-1", project_id="project-1", hangar_id="hangar-1",
            drone_id="drone-1", site_id="site-1", task_id="java-task-1",
            context_version=1, version=7, observed_at=self.now,
            control_owner="actor-1", profile="DJI_HANGAR",
            control_lease_expires_at=self.now + 60,
            permissions=frozenset({"hover", "point_flight", "point_orbit", "move_forward",
                                   "read_state"}),
            capabilities=dict(self.capabilities),
            state=dict(self.state),
        )
        fields.update(overrides)
        return KtdSnapshot(**fields)

    def executor(self):
        return KtdPlatformExecutor(
            self.binding, site_id="site-1", task_id="java-task-1",
            transport=self.transport, snapshot_provider=lambda: self.snapshot,
            journal=SqliteCommandJournal(self.directory.name + "/commands.sqlite3"),
            enabled=True, clock=lambda: self.now,
        )

    def place_on_orbit(self, longitude, latitude, radius_m, bearing_deg=0.0):
        aircraft_longitude, aircraft_latitude = offset_target(longitude, latitude, bearing_deg, radius_m)
        self.state = {**self.state, "longitude": aircraft_longitude, "latitude": aircraft_latitude}
        self.snapshot = self.build_snapshot()

    def dispatch(self, action, parameters, executor=None):
        executor = executor or self.executor()
        self.calls = getattr(self, "calls", 0) + 1
        command_id, key = f"cmd-{self.calls}", f"key-{self.calls}"
        card = executor.preview(FlightControlActionRequest(command_id, key, action, parameters, 1))
        self.assertIs(card.status, FlightControlStatus.PREVIEW_REQUIRED, card.to_dict())
        receipt = executor.confirm(
            card.confirmation_id, command_id=command_id, idempotency_key=key,
            confirmation_binding=card.confirmation_binding, context_version=1)
        return executor, receipt, card

    def test_hover_without_prior_motion_sends_move_stop(self):
        _, receipt, _ = self.dispatch("hover", {})
        self.assertEqual(receipt.status, "submitted")
        command = self.transport.calls[-1]
        self.assertEqual(command.path, "/screen/uav/operation/control")
        self.assertEqual(command.body["type"], 207)
        self.assertEqual(command.body["mode"], 3)
        self.assertEqual(command.body["param"], 0)
        self.assertIn("未经回执验证", " ".join(receipt.warnings))

    def test_hover_during_route_pauses_instead_of_stopping(self):
        self.snapshot = self.build_snapshot(state={**self.state, "route_status": "running"})
        _, receipt, _ = self.dispatch("hover", {})
        self.assertEqual(receipt.status, "submitted")
        command = self.transport.calls[-1]
        self.assertEqual(command.path, "/screen/uav/route/control")
        self.assertEqual(command.body["type"], 110)

    def test_hover_after_displacement_cancels_fly_to_point(self):
        executor, _, _ = self.dispatch("move_forward", {"distance_m": 10})
        _, receipt, _ = self.dispatch("hover", {}, executor)
        self.assertEqual(receipt.status, "submitted")
        command = self.transport.calls[-1]
        self.assertEqual(command.path, "/screen/uav/fly/control")
        self.assertEqual(command.body["type"], 120)

    def test_hover_after_orbit_exits_poi_mode(self):
        self.place_on_orbit(113.0, 23.0, 30)
        executor, _, _ = self.dispatch(
            "point_orbit", {"longitude": 113.0, "latitude": 23.0, "radius_m": 30})
        _, receipt, _ = self.dispatch("hover", {}, executor)
        self.assertEqual(receipt.status, "submitted")
        command = self.transport.calls[-1]
        self.assertEqual(command.path, "/screen/uav/fly/control")
        self.assertEqual(command.body["type"], 122)

    def test_hover_requires_airborne_and_capability(self):
        self.snapshot = self.build_snapshot(state={**self.state, "airborne": False})
        card = self.executor().preview(
            FlightControlActionRequest("cmd-1", "key-1", "hover", {}, 1))
        self.assertEqual(card.error_code, "FLIGHT_STATE_UNAVAILABLE")
        self.snapshot = self.build_snapshot(capabilities={"move_forward": True})
        card = self.executor().preview(
            FlightControlActionRequest("cmd-1", "key-1", "hover", {}, 1))
        self.assertEqual(card.error_code, "CAPABILITY_UNAVAILABLE")
        self.assertEqual(self.transport.calls, [])

    def test_hover_refuses_when_route_state_is_unknown(self):
        # 不知道航线是否在执行，就无法在“暂停航线”和“停止运动”之间选；不能猜。
        state = {key: value for key, value in self.state.items() if key != "route_status"}
        self.snapshot = self.build_snapshot(state=state)
        card = self.executor().preview(
            FlightControlActionRequest("cmd-1", "key-1", "hover", {}, 1))
        self.assertEqual(card.error_code, "ROUTE_STATE_UNAVAILABLE")
        self.assertEqual(self.transport.calls, [])

    def test_point_flight_uses_fly_control_with_fixed_speed(self):
        # 当前绝对高 100。同高指点的单点终点和已检查水平直线一致，可以下发。
        _, receipt, _ = self.dispatch(
            "point_flight", {"longitude": 113.1, "latitude": 23.1, "altitude_m": 100})
        self.assertEqual(receipt.status, "submitted")
        command = self.transport.calls[-1]
        self.assertEqual(command.path, "/screen/uav/fly/control")
        self.assertEqual(command.body["type"], 119)
        self.assertEqual(command.body["height"], 100.0)
        self.assertEqual(command.body["speed"], 5)
        self.assertEqual(command.body["longitude"], 113.1)

    def test_point_flight_refuses_a_diagonal_the_replay_cannot_prove(self):
        # 目标绝对高 120、当前 100，同时改了水平位置。不能把围栏直线当成实际轨迹。
        card = self.executor().preview(FlightControlActionRequest(
            "cmd-1", "key-1", "point_flight",
            {"longitude": 113.1, "latitude": 23.1, "altitude_m": 120}, 1))
        self.assertEqual(card.error_code, "FLYTO_TRAJECTORY_UNVERIFIED")
        self.assertIn("未下发", card.message)
        self.assertEqual(self.transport.calls, [])

    def test_vertical_point_flight_keeps_the_single_altitude_change(self):
        _, receipt, _ = self.dispatch(
            "point_flight", {"longitude": 113.0, "latitude": 23.0, "altitude_m": 120})
        self.assertEqual(receipt.status, "submitted")
        self.assertEqual(self.transport.calls[-1].body["height"], 120.0)
        self.assertEqual(self.transport.calls[-1].body["longitude"], 113.0)

    def test_point_flight_refuses_the_unchecked_climb_below_20_metres(self):
        self.snapshot = self.build_snapshot(state={**self.state, "height_m": 15})
        card = self.executor().preview(FlightControlActionRequest(
            "cmd-1", "key-1", "point_flight",
            {"longitude": 113.1, "latitude": 23.1, "altitude_m": 120}, 1))
        self.assertEqual(card.error_code, "DJI_FLYTO_IMPLICIT_CLIMB")
        self.assertIn("未下发", card.message)
        self.assertEqual(self.transport.calls, [])

    def test_point_flight_requires_relative_height_before_fly_to(self):
        state = {key: value for key, value in self.state.items() if key != "height_m"}
        self.snapshot = self.build_snapshot(state=state)
        card = self.executor().preview(FlightControlActionRequest(
            "cmd-1", "key-1", "point_flight",
            {"longitude": 113.1, "latitude": 23.1, "altitude_m": 120}, 1))
        self.assertEqual(card.error_code, "RELATIVE_HEIGHT_UNAVAILABLE")
        self.assertEqual(self.transport.calls, [])

    def test_point_flight_refuses_a_target_below_the_unverified_relative_floor(self):
        card = self.executor().preview(FlightControlActionRequest(
            "cmd-1", "key-1", "point_flight",
            {"longitude": 113.1, "latitude": 23.1, "altitude_m": 60}, 1))
        self.assertEqual(card.error_code, "DJI_LOW_TARGET_UNVERIFIED")
        self.assertEqual(self.transport.calls, [])

    def test_point_flight_rejects_altitude_outside_platform_range(self):
        card = self.executor().preview(FlightControlActionRequest(
            "cmd-1", "key-1", "point_flight",
            {"longitude": 113.1, "latitude": 23.1, "altitude_m": 1}, 1))
        self.assertEqual(card.error_code, "TARGET_ALTITUDE_OUT_OF_RANGE")

    def test_point_orbit_enters_poi_only_when_already_at_the_radius(self):
        self.place_on_orbit(113.0, 23.0, 30)
        _, receipt, card = self.dispatch(
            "point_orbit", {"longitude": 113.0, "latitude": 23.0, "radius_m": 30})
        self.assertEqual(receipt.status, "submitted")
        command = self.transport.calls[-1]
        self.assertEqual(command.path, "/screen/uav/fly/control")
        self.assertEqual(command.body["type"], 121)
        self.assertEqual(command.body["height"], 100.0)
        self.assertEqual(command.body["speed"], -5)
        self.assertTrue(any("一致" in warning for warning in card.warnings))
        self.assertNotIn("radius", command.body)

    def test_point_orbit_rejects_a_radius_the_platform_cannot_command(self):
        card = self.executor().preview(FlightControlActionRequest(
            "cmd-1", "key-1", "point_orbit",
            {"longitude": 113.0, "latitude": 23.0, "radius_m": 30}, 1))
        self.assertEqual(card.error_code, "ORBIT_RADIUS_NOT_COMMANDED")
        self.assertIn("未下发", card.message)
        self.assertEqual(self.transport.calls, [])


if __name__ == "__main__":
    unittest.main()
