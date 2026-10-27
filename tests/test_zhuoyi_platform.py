"""Agent client for the Java flight-control API; only the HTTP boundary is scripted."""
import json
import unittest
from urllib.parse import parse_qs, urlsplit

from lingxi_flight_control.full_control import (
    FlightControlAction, FlightControlActionRequest, FlightControlStatus, RuntimeBinding,
)
from lingxi_flight_control.zhuoyi_platform import (
    STATE_SCHEMA, ZhuoyiPlatformExecutor,
)


def state(owner="actor-1", available=True, reasons=None):
    return {
        "schema_version": STATE_SCHEMA,
        "snapshot_version": "f" * 64,
        "project_id": "project-1",
        "site_id": "site-1",
        "hangar_id": "hangar-1",
        "drone_id": "drone-1",
        "task_id": None,
        "profile": "DJI_HANGAR",
        "online": True,
        "airborne": True,
        "longitude": 113.0,
        "latitude": 23.0,
        "altitude_m": 50.0,
        "height_m": 30.0,
        "yaw_deg": 90.0,
        "control_owner_employee_id": owner,
        "control_lease_expires_at": 1792400300,
        "observed_at": 1792400000.0,
        "permissions": ["camera_photo", "move_forward"],
        "action_availability": {
            "camera_photo": {"available": available, "reasons": reasons or []},
            "move_forward": {"available": True, "reasons": []},
        },
    }


class FakePlatform:
    def __init__(self):
        self.calls = []
        self.state = state()
        self.commands = {}
        self.receipts = {}
        self.command_error = None
        self.hold_accepted = False

    def __call__(self, method, url, body, headers, timeout):
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        self.calls.append((method, parts.path, query, body, dict(headers)))
        if parts.path == "/api/v1/flight-control/state":
            return 200, self.state
        if parts.path == "/api/v1/flight-control/commands":
            if self.command_error is not None:
                return 400, {"code": self.command_error, "message": "rejected"}
            payload = json.loads(body)
            command_id = payload["command_id"]
            receipt = {
                "status": "submitted", "command_id": command_id,
                "error_code": None, "continuation_allowed": False,
            }
            self.commands[command_id] = payload
            self.receipts[command_id] = receipt
            return 200, receipt
        if parts.path == "/api/v1/flight-control/receipt":
            command_id = query["commandId"][0]
            receipt = dict(self.receipts[command_id])
            action = self.commands[command_id]["action"]
            if action == "move_forward" and not self.hold_accepted:
                receipt.update(status="completed", continuation_allowed=True)
            else:
                receipt.update(status="accepted", continuation_allowed=True)
            self.receipts[command_id] = receipt
            return 200, receipt
        return 404, {"code": "NOT_FOUND"}


class ZhuoyiPlatformExecutorTests(unittest.TestCase):
    def setUp(self):
        self.clock = [1792400000.0]
        self.binding = RuntimeBinding("actor-1", "project-1", "hangar-1", "drone-1",
                                      "batch-1", 1)
        self.platform = FakePlatform()
        self.executor = ZhuoyiPlatformExecutor(
            self.binding, base_url="http://127.0.0.1:18091", gateway_token="gateway",
            site_id="site-1", transport=self.platform, clock=lambda: self.clock[0],
            receipt_timeout=1.0, poll_interval=0.01)

    def request(self, action, command_id="cmd-1", **parameters):
        return FlightControlActionRequest(command_id, command_id, action, parameters, 1)

    def test_preview_and_confirm_submit_once_and_return_accepted(self):
        request = self.request(FlightControlAction.CAMERA_PHOTO)
        preview = self.executor.preview(request)
        self.assertEqual(preview.status, FlightControlStatus.PREVIEW_REQUIRED)
        self.assertEqual(preview.confirmation_id is not None, True)
        self.assertEqual(preview.state["snapshot_version"], "f" * 64)

        receipt = self.executor.confirm(
            preview.confirmation_id, command_id=request.command_id,
            idempotency_key=request.idempotency_key,
            confirmation_binding=preview.confirmation_binding, context_version=1)
        self.assertEqual(receipt.status, FlightControlStatus.ACCEPTED)
        command = self.platform.commands["cmd-1"]
        self.assertEqual(command["action"], "camera_photo")
        self.assertEqual(command["snapshot_version"], "f" * 64)
        self.assertGreater(command["expires_at"], 1792400000)
        headers = next(call[4] for call in self.platform.calls
                       if call[1] == "/api/v1/flight-control/commands")
        self.assertEqual(headers["Authorization"], "Bearer gateway")
        self.assertEqual(headers["X-Project-Id"], "project-1")
        self.assertEqual(headers["X-Actor-Id"], "actor-1")

    def test_movement_is_accepted_only_after_completed(self):
        request = self.request(FlightControlAction.MOVE_FORWARD, distance_m=10)
        preview = self.executor.preview(request)
        receipt = self.executor.confirm(
            preview.confirmation_id, command_id=request.command_id,
            idempotency_key=request.idempotency_key,
            confirmation_binding=preview.confirmation_binding, context_version=1)
        self.assertEqual(receipt.status, FlightControlStatus.ACCEPTED)
        # completed 只是接口终态，不是另一次位置回读。
        self.assertFalse(receipt.arrival_verified)
        self.assertEqual(self.platform.receipts["cmd-1"]["status"], "completed")
        self.assertEqual(self.executor.supports_plan_execution, True)

    def test_dji_move_below_20_metres_is_not_submitted(self):
        self.platform.state["height_m"] = 10
        request = self.request(FlightControlAction.MOVE_FORWARD, distance_m=10)
        preview = self.executor.preview(request)
        self.assertEqual(preview.status, FlightControlStatus.REJECTED)
        self.assertEqual(preview.error_code, "DJI_FLYTO_IMPLICIT_CLIMB")
        self.assertIn("未下发", preview.message)
        self.assertEqual(self.platform.commands, {})

    def test_dji_diagonal_point_flight_is_not_submitted(self):
        self.platform.state["permissions"] = ["point_flight"]
        self.platform.state["action_availability"]["point_flight"] = {"available": True, "reasons": []}
        request = self.request(FlightControlAction.POINT_FLIGHT,
                               longitude=113.01, latitude=23.01, altitude_m=80)
        preview = self.executor.preview(request)
        self.assertEqual(preview.status, FlightControlStatus.REJECTED)
        self.assertEqual(preview.error_code, "FLYTO_TRAJECTORY_UNVERIFIED")
        self.assertIn("未下发", preview.message)
        self.assertEqual(self.platform.commands, {})

    def test_dji_descent_below_20_metres_is_not_submitted(self):
        self.platform.state["height_m"] = 25
        self.platform.state["permissions"] = ["move_down"]
        self.platform.state["action_availability"]["move_down"] = {"available": True, "reasons": []}
        request = self.request(FlightControlAction.MOVE_DOWN, distance_m=10)
        preview = self.executor.preview(request)
        self.assertEqual(preview.error_code, "DJI_LOW_TARGET_UNVERIFIED")
        self.assertEqual(self.platform.commands, {})

    def test_move_acknowledgement_is_not_arrival(self):
        self.platform.hold_accepted = True
        original = self.platform

        def transport(method, url, body, headers, timeout):
            if "/receipt" in url:
                self.clock[0] += 2
            return original(method, url, body, headers, timeout)

        self.executor._transport = transport
        request = self.request(FlightControlAction.MOVE_FORWARD, distance_m=10)
        preview = self.executor.preview(request)
        receipt = self.executor.confirm(
            preview.confirmation_id, command_id=request.command_id,
            idempotency_key=request.idempotency_key,
            confirmation_binding=preview.confirmation_binding, context_version=1)
        self.assertEqual(receipt.status, FlightControlStatus.UNKNOWN)
        self.assertEqual(receipt.error_code, "RECEIPT_PENDING")
        self.assertFalse(receipt.arrival_verified)
        self.assertEqual(self.platform.receipts["cmd-1"]["status"], "accepted")

    def test_control_and_availability_are_checked_before_a_card(self):
        self.platform.state = state(owner="actor-2")
        preview = self.executor.preview(self.request(FlightControlAction.CAMERA_PHOTO))
        self.assertEqual(preview.status, FlightControlStatus.REJECTED)
        self.assertEqual(preview.error_code, "CONTROL_NOT_OWNED")

        self.platform.state = state(available=False, reasons=["VENDOR_AUTHORITY_UNAVAILABLE"])
        preview = self.executor.preview(self.request(FlightControlAction.CAMERA_PHOTO))
        self.assertEqual(preview.status, FlightControlStatus.REJECTED)
        self.assertEqual(preview.error_code, "ACTION_NOT_AVAILABLE")

    def test_platform_rejection_is_returned_without_a_card(self):
        self.platform.command_error = "SNAPSHOT_STALE"
        request = self.request(FlightControlAction.CAMERA_PHOTO)
        preview = self.executor.preview(request)
        receipt = self.executor.confirm(
            preview.confirmation_id, command_id=request.command_id,
            idempotency_key=request.idempotency_key,
            confirmation_binding=preview.confirmation_binding, context_version=1)
        self.assertEqual(receipt.status, FlightControlStatus.REJECTED)
        self.assertEqual(receipt.error_code, "SNAPSHOT_STALE")

    def test_state_exposes_control_owner_and_revision(self):
        state = self.executor.read_state()
        self.assertTrue(state["control_owned"])
        self.assertEqual(state["control_owner"], "actor-1")
        self.assertEqual(state["revision"], "f" * 64)
        self.assertEqual(self.executor.runtime_context().drone_id, "drone-1")


if __name__ == "__main__":
    unittest.main()
