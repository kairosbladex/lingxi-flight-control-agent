import json
import unittest

from lingxi_flight_control import (
    DecisionKind,
    ModelDecision,
    StandalonePreviewService,
    ToolCall,
)


class FakeDecider:
    def __init__(self):
        self.calls = 0

    def decide(self, text, checkpoint, tools):
        self.calls += 1
        return ModelDecision(DecisionKind.TOOL_CALL,
                             tool_call=ToolCall("move_forward", {"distance_m": 10}))


class StandalonePreviewTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.decider = FakeDecider()
        self.service = StandalonePreviewService(lambda: self.decider, clock=lambda: self.now)
        self.headers = {"X-Agent-Actor": "operator-1", "X-Trace-Id": "trace-1"}
        self.context = {
            "project_id": "project-1", "hangar_id": "hangar-1", "drone_id": "drone-1",
            "task_id": "task-1", "context_version": 1, "site_id": "site-1",
        }
        self.pose = {"longitude": 113.0, "latitude": 23.0, "altitude_m": 50.0,
                     "heading_deg": 90.0, "observed_at": 100.0}

    def call(self, method, path, payload=None):
        body = None if payload is None else json.dumps(payload)
        response = self.service.handle(method, path, body, self.headers)
        return response.status_code, response.payload

    def test_pose_ingest_preview_and_confirm_stay_standalone(self):
        status, pose = self.call("POST", "/api/flight-preview/pose",
                                 {**self.context, "pose": self.pose})
        self.assertEqual(status, 200)
        self.assertEqual(pose["pose_source"], "standalone-telemetry-ingest")

        status, card = self.call("POST", "/api/flight-preview/preview", {
            **self.context, "text": "往前飞10米", "command_id": "cmd-1",
            "idempotency_key": "idem-1",
        })
        self.assertEqual(status, 200)
        self.assertEqual(card["execution_mode"], "preview")
        self.assertEqual(card["action"], "move_forward")
        self.assertEqual(card["parameters"], {"distance_m": 10})
        self.assertGreater(card["target"]["longitude"], 113.0)
        self.assertFalse(card["dispatch_allowed"])
        self.assertFalse(card["dispatched"])
        self.assertEqual(card["trace_id"], "trace-1")

        status, result = self.call("POST", "/api/flight-preview/confirm", {
            **{key: self.context[key] for key in ("project_id", "hangar_id", "drone_id", "task_id", "context_version")},
            "confirmation_id": card["confirmation_id"],
            "command_id": card["command_id"],
            "idempotency_key": card["idempotency_key"],
            "confirmation_binding": card["confirmation_binding"],
        })
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "confirmed_not_dispatched")
        self.assertFalse(result["dispatched"])
        self.assertEqual(self.decider.calls, 1)

    def test_pose_staleness_and_context_tamper_are_rejected(self):
        status, payload = self.call("POST", "/api/flight-preview/pose",
                                    {**self.context, "pose": {**self.pose, "observed_at": 80.0}})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error_code"], "STALE_POSE")

        status, payload = self.call("POST", "/api/flight-preview/preview", {
            **self.context, "text": "往前飞10米", "command_id": "cmd-1",
            "idempotency_key": "idem-1", "pose": self.pose, "context_version": 2,
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload["execution_mode"], "preview")

        bad_headers = {**self.headers, "X-Agent-Actor": "operator-2"}
        response = self.service.handle("POST", "/api/flight-preview/confirm", json.dumps({
            "project_id": "project-1", "hangar_id": "hangar-1", "drone_id": "drone-1",
            "task_id": "task-1", "context_version": 2, "confirmation_id": "missing",
            "command_id": "cmd-1", "idempotency_key": "idem-1",
            "confirmation_binding": "0" * 64,
        }), bad_headers)
        self.assertEqual(response.status_code, 404)

    def test_unknown_fields_and_missing_pose_never_call_model(self):
        status, payload = self.call("POST", "/api/flight-preview/preview", {
            **self.context, "text": "往前飞10米", "command_id": "cmd-1",
            "idempotency_key": "idem-1", "pose": self.pose, "actor_id": "forged",
        })
        self.assertEqual(status, 400)
        self.assertEqual(payload["error_code"], "INVALID_REQUEST")
        self.assertEqual(self.decider.calls, 0)

        status, payload = self.call("POST", "/api/flight-preview/preview", {
            **self.context, "text": "往前飞10米", "command_id": "cmd-1",
            "idempotency_key": "idem-1",
        })
        self.assertEqual(status, 404)
        self.assertEqual(payload["error_code"], "POSE_NOT_FOUND")
        self.assertEqual(self.decider.calls, 0)


if __name__ == "__main__":
    unittest.main()
