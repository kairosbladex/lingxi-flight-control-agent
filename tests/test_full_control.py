import unittest
from concurrent.futures import ThreadPoolExecutor

from lingxi_flight_control import (
    FlightControlAction,
    FlightControlActionRequest,
    FlightControlStatus,
    MemoryFlightControlSimulator,
    RuntimeBinding,
    action_tool_names,
    action_tool_definitions,
    action_manifest,
    parse_flight_control_text,
)


class FullControlSimulatorTests(unittest.TestCase):
    def setUp(self):
        self.binding = RuntimeBinding("actor-1", "project-1", "hangar-1", "drone-1", "batch-1", 1)
        self.simulator = MemoryFlightControlSimulator(self.binding, clock=lambda: 100.0)
        self.sequence = 0
        acquire = FlightControlActionRequest("acquire", "acquire", FlightControlAction.CONTROL_ACQUIRE, {}, 1)
        card = self.simulator.preview(acquire)
        self.simulator.confirm(card.confirmation_id, command_id="acquire", idempotency_key="acquire",
                               confirmation_binding=card.confirmation_binding, context_version=1)

    def execute(self, action, parameters=None):
        self.sequence += 1
        request = FlightControlActionRequest(
            f"command-{self.sequence}", f"key-{self.sequence}", action, parameters or {}, 1
        )
        card = self.simulator.preview(request)
        self.assertEqual(card.status, FlightControlStatus.PREVIEW_REQUIRED)
        return self.simulator.confirm(
            card.confirmation_id,
            command_id=request.command_id,
            idempotency_key=request.idempotency_key,
            confirmation_binding=card.confirmation_binding,
            context_version=1,
        )

    def test_runtime_binding_and_serializable_simulator_receipt(self):
        result = self.execute(FlightControlAction.CONTROL_ACQUIRE)
        self.assertEqual(result.status, FlightControlStatus.ACCEPTED)
        self.assertEqual(result.to_dict()["execution_mode"], "simulator")
        self.assertEqual(result.state["control_owner"], "actor-1")
        self.assertEqual(result.state["context_version"], 1)

    def test_every_flight_action_updates_or_reads_memory_state(self):
        actions = [
            (FlightControlAction.MOVE_FORWARD, {"distance_m": 10}),
            (FlightControlAction.MOVE_BACKWARD, {"distance_m": 2}),
            (FlightControlAction.MOVE_LEFT, {"distance_m": 3}),
            (FlightControlAction.MOVE_RIGHT, {"distance_m": 3}),
            (FlightControlAction.MOVE_UP, {"distance_m": 5}),
            (FlightControlAction.MOVE_DOWN, {"distance_m": 1}),
            (FlightControlAction.TURN_LEFT, {"angle_deg": 15}),
            (FlightControlAction.TURN_RIGHT, {"angle_deg": 30}),
            (FlightControlAction.EMERGENCY_STOP, {}),
            (FlightControlAction.TAKEOFF, {"altitude_m": 30}),
            (FlightControlAction.LAND, {}),
            (FlightControlAction.LAND_ALTERNATE, {}),
            (FlightControlAction.RETURN_HOME, {}),
            (FlightControlAction.CANCEL_RETURN_HOME, {}),
        ]
        for action, parameters in actions:
            with self.subTest(action=action):
                result = self.execute(action, parameters)
                self.assertEqual(result.status, FlightControlStatus.ACCEPTED)
                self.assertEqual(result.state["last_action"], action.value)

    def test_route_camera_infrared_payload_and_parameters(self):
        cases = [
            (FlightControlAction.UPLOAD_ROUTE, {"route_id": "r1", "waypoints": [
                {"longitude": 1, "latitude": 2}, {"longitude": 1.1, "latitude": 2.1},
                {"longitude": 1.2, "latitude": 2.2}]}),
            (FlightControlAction.EXECUTE_ROUTE, {}),
            (FlightControlAction.ROUTE_PAUSE, {}),
            (FlightControlAction.ROUTE_RESUME, {}),
            (FlightControlAction.ROUTE_PAUSE, {}),
            (FlightControlAction.CONTINUE_FROM_BREAKPOINT, {"index": 2}),
            # P1：非测绘自动航线中，坐标动作必须从暂停态开始。
            (FlightControlAction.ROUTE_PAUSE, {}),
            (FlightControlAction.POINT_FLIGHT, {"longitude": 113.4, "latitude": 34.8, "altitude_m": 50}),
            (FlightControlAction.POINT_ORBIT, {"longitude": 113.4, "latitude": 34.8, "radius_m": 20}),
            (FlightControlAction.SET_MOVE_SPEED, {"value": 8}),
            (FlightControlAction.SET_ROLL_SPEED, {"value": 20}),
            (FlightControlAction.SET_PITCH_SPEED, {"value": 20}),
            (FlightControlAction.SET_VERTICAL_SPEED, {"value": 4}),
            (FlightControlAction.SET_YAW_SPEED, {"value": 60}),
            (FlightControlAction.RESET_PARAMETERS, {}),
            (FlightControlAction.CAMERA_ELECTRO_OPTICAL, {}),
            (FlightControlAction.CAMERA_ZOOM, {"value": 4}),
            (FlightControlAction.CAMERA_INFRARED, {}),
            (FlightControlAction.CAMERA_WIDE, {}),
            (FlightControlAction.GIMBAL_UP, {}),
            (FlightControlAction.GIMBAL_DOWN, {}),
            (FlightControlAction.GIMBAL_LEFT, {}),
            (FlightControlAction.GIMBAL_RIGHT, {}),
            (FlightControlAction.GIMBAL_DIGITAL_ZOOM, {"value": 3}),
            (FlightControlAction.GIMBAL_RESET, {}),
            (FlightControlAction.GIMBAL_45, {}),
            (FlightControlAction.GIMBAL_90, {}),
            (FlightControlAction.CAMERA_LOCK_FOCUS, {}),
            (FlightControlAction.CAMERA_PANORAMA, {}),
            (FlightControlAction.CAMERA_PHOTO, {}),
            (FlightControlAction.CAMERA_START_RECORDING, {}),
            (FlightControlAction.CAMERA_STOP_RECORDING, {}),
            (FlightControlAction.INFRARED_SET_LIMITS, {"min_c": -10, "max_c": 80}),
            (FlightControlAction.PAYLOAD_MEGAPHONE, {"enabled": True}),
            (FlightControlAction.PAYLOAD_SEARCHLIGHT, {"enabled": True}),
            (FlightControlAction.PAYLOAD_METHANE, {"enabled": True}),
            (FlightControlAction.PAYLOAD_DROPPER, {"payload": "sample"}),
            (FlightControlAction.PAYLOAD_DYNAMIC, {"payload": "laser", "enabled": True}),
        ]
        for action, parameters in cases:
            with self.subTest(action=action):
                result = self.execute(action, parameters)
                self.assertEqual(result.status, FlightControlStatus.ACCEPTED)
                self.assertEqual(result.state["last_action"], action.value)

    def test_permission_and_context_actions(self):
        self.execute(FlightControlAction.REMOTE_REQUEST)
        self.execute(FlightControlAction.REMOTE_APPROVE)
        self.execute(FlightControlAction.SET_KEYBOARD_MODE)
        self.execute(FlightControlAction.SET_JOYSTICK_MODE)
        self.execute(FlightControlAction.SET_VIRTUAL_BUTTON, {"button": "takeoff", "action": "takeoff"})
        self.execute(FlightControlAction.CONTROL_RELEASE)
        self.assertEqual(self.simulator.snapshot()["state"]["control_owner"], None)

    def test_cancel_expire_duplicate_switch_and_old_confirmation_fail_closed(self):
        request = FlightControlActionRequest("c1", "k1", FlightControlAction.MOVE_FORWARD, {"distance_m": 5}, 1)
        card = self.simulator.preview(request)
        cancelled = self.simulator.cancel(card.confirmation_id, command_id="c1", idempotency_key="k1",
                                           confirmation_binding=card.confirmation_binding, context_version=1)
        self.assertEqual(cancelled.status, FlightControlStatus.CANCELLED)
        duplicate = self.simulator.preview(request)
        self.assertEqual(duplicate.status, FlightControlStatus.PREVIEW_REQUIRED)
        switched = RuntimeBinding("actor-1", "project-1", "hangar-1", "drone-2", "batch-2", 2)
        self.simulator.bind(switched)
        stale = self.simulator.confirm(card.confirmation_id, command_id="c1", idempotency_key="k1",
                                       confirmation_binding=card.confirmation_binding, context_version=1)
        self.assertEqual(stale.status, FlightControlStatus.STALE_CONTEXT)
        self.assertEqual(self.simulator.snapshot()["binding"]["drone_id"], "drone-2")

    def test_parallel_confirmations_keep_state_updates_atomic(self):
        requests = [
            FlightControlActionRequest("parallel-1", "parallel-1", FlightControlAction.MOVE_FORWARD,
                                       {"distance_m": 1}, 1),
            FlightControlActionRequest("parallel-2", "parallel-2", FlightControlAction.SPEAKER_SPEAK,
                                       {"text": "注意安全"}, 1),
        ]
        cards = [self.simulator.preview(request) for request in requests]

        def confirm(card, request):
            return self.simulator.confirm(
                card.confirmation_id,
                command_id=request.command_id,
                idempotency_key=request.idempotency_key,
                confirmation_binding=card.confirmation_binding,
                context_version=1,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(confirm, cards, requests))
        self.assertEqual([result.status for result in results],
                         [FlightControlStatus.ACCEPTED, FlightControlStatus.ACCEPTED])
        self.assertEqual(self.simulator.state.revision, 3)
        self.assertGreater(self.simulator.state.latitude, 0.0)
        self.assertEqual(self.simulator.state.payloads["speaker_text"], "注意安全")

    def test_natural_language_to_structured_action_and_tool_coverage(self):
        move = parse_flight_control_text("向前 12 米", command_id="c", idempotency_key="k", context_version=1)
        self.assertEqual(move.action, FlightControlAction.MOVE_FORWARD)
        self.assertEqual(move.parameters["distance_m"], 12)
        self.assertEqual(parse_flight_control_text("拍照", command_id="c2", idempotency_key="k2").action,
                         FlightControlAction.CAMERA_PHOTO)
        names = set(action_tool_names())
        self.assertEqual(names, {action.value for action in FlightControlAction})
        definitions = action_tool_definitions()
        self.assertEqual({item["name"] for item in definitions}, names)
        self.assertTrue(all(item["execution_mode"] == "simulator" for item in definitions))
        manifest = action_manifest()
        self.assertEqual({item["action"] for item in manifest["tools"]}, names)
        move = next(item for item in manifest["tools"] if item["action"] == "move_forward")
        self.assertEqual(move["parameter_names"], ["distance_m"])


if __name__ == "__main__":
    unittest.main()
