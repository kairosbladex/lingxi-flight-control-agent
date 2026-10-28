"""测试外部平台适配器与快照 route_status 健壮容错。"""

from __future__ import annotations

import unittest

from lingxi_flight_control.adapters.ktd_platform_adapter import (
    KtdPlatformAdapter,
    RouteStatusUnavailableError,
    ROUTE_STATE_SOURCE_UNAVAILABLE,
)


class KtdPlatformAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = KtdPlatformAdapter()

    def test_null_route_status_blocks_move_forward(self):
        snapshot = {"drone_id": "drone-1", "route_status": None, "task_status": "flying"}
        with self.assertRaises(RouteStatusUnavailableError) as ctx:
            self.adapter.adapt_snapshot(snapshot, requested_action="move_forward")
        self.assertEqual(ctx.exception.code, ROUTE_STATE_SOURCE_UNAVAILABLE)
        self.assertTrue(ctx.exception.diagnostic["fail_closed"])

    def test_null_route_status_blocks_hover_and_turn(self):
        snapshot = {"drone_id": "drone-1", "route_status": None}
        for act in ("hover", "turn_left", "turn_right", "point_flight", "point_orbit"):
            with self.assertRaises(RouteStatusUnavailableError) as ctx:
                self.adapter.adapt_snapshot(snapshot, requested_action=act)
            self.assertEqual(ctx.exception.code, ROUTE_STATE_SOURCE_UNAVAILABLE)

    def test_null_route_status_allows_read_only_queries(self):
        snapshot = {"drone_id": "drone-1", "route_status": None, "battery": 85}
        for act in ("read_state", "camera_zoom_query", "battery_query"):
            res = self.adapter.adapt_snapshot(snapshot, requested_action=act)
            self.assertTrue(res.get("_degraded_route_status"))
            self.assertEqual(res.get("_route_status_unavailable_reason"), "PLATFORM_SNAPSHOT_NULL")
            self.assertEqual(res["battery"], 85)

    def test_valid_route_status_passes_normally(self):
        snapshot = {"drone_id": "drone-1", "route_status": "idle", "altitude_m": 50}
        res = self.adapter.adapt_snapshot(snapshot, requested_action="move_forward")
        self.assertNotIn("_degraded_route_status", res)
        self.assertEqual(res["route_status"], "idle")

    def test_payload_formatters(self):
        route_payload = self.adapter.format_route_control_payload(120)
        self.assertEqual(route_payload, {"type": 120})
        micro_payload = self.adapter.format_micro_operation_payload(201, mode=2, param=10.0)
        self.assertEqual(micro_payload, {"type": 201, "mode": 2, "param": 10.0})

    def test_permissive_route_status_fallback_is_rejected(self):
        with self.assertRaises(ValueError):
            KtdPlatformAdapter(fallback_policy="allow")


if __name__ == "__main__":
    unittest.main()
