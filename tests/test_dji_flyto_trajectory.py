"""大疆 fly_to_point 回放：只承认能对上已检查折线的轨迹，不放宽 20 米门槛。"""

import math
import unittest

from lingxi_flight_control.geometry import horizontal_distance_m
from lingxi_flight_control.ktd_platform import (
    KtdPlatformError, _java_calc_lat_lon, _java_move_bearing, dji_commanded_flyto_point,
    dji_flyto_replay, guard_dji_flyto,
)
from lingxi_flight_control.path_geometry import micro_move_path, point_flight_path


def pose(**overrides):
    state = {
        "profile": "DJI_HANGAR",
        "longitude": 113.0,
        "latitude": 23.0,
        "altitude_m": 100.0,
        "height_m": 50.0,
        "yaw_deg": 90.0,
    }
    state.update(overrides)
    return state


class FlytoReplayTests(unittest.TestCase):
    def test_horizontal_endpoint_matches_the_checked_segment_in_metres(self):
        for action, yaw in (
            ("move_forward", 0.0),
            ("move_backward", 10.0),
            ("move_left", -100.0),
            ("move_right", 170.0),
            ("move_forward", -179.9),
        ):
            with self.subTest(action=action, yaw=yaw):
                state = pose(yaw_deg=yaw)
                parameters = {"distance_m": 10}
                commanded = dji_commanded_flyto_point(action, parameters, state)
                geometry = micro_move_path(action, parameters, state)
                end = geometry.points[1]
                gap = horizontal_distance_m(commanded[0], commanded[1], end["longitude"], end["latitude"])
                self.assertLessEqual(gap, 0.001)
                self.assertEqual(commanded[2], end["altitude"])
                self.assertEqual(geometry.points[0]["altitude"], geometry.points[1]["altitude"])
                self.assertIsNone(dji_flyto_replay(action, parameters, state))
                # 注释里的「千米」如果拿来用，10 会按 10 公里计算，不能再算对上。
                bearing = _java_move_bearing(yaw, action)
                far_lon, far_lat = _java_calc_lat_lon(state["longitude"], state["latitude"], bearing, 10 * 1000)
                far = horizontal_distance_m(far_lon, far_lat, end["longitude"], end["latitude"])
                self.assertGreater(far, 1000)

    def test_straight_polyline_does_not_waive_the_climb_below_20_metres(self):
        state = pose(height_m=15)
        parameters = {"distance_m": 10}
        geometry = micro_move_path("move_forward", parameters, state)
        self.assertEqual(geometry.segments, 1)
        self.assertEqual(geometry.points[0]["altitude"], geometry.points[1]["altitude"])
        climb_altitude = state["altitude_m"] + (20 - state["height_m"])
        self.assertNotEqual(geometry.points[0]["altitude"], climb_altitude)
        self.assertNotEqual(geometry.points[1]["altitude"], climb_altitude)
        self.assertEqual(dji_flyto_replay("move_forward", parameters, state), "DJI_FLYTO_IMPLICIT_CLIMB")
        with self.assertRaises(KtdPlatformError) as caught:
            guard_dji_flyto("move_forward", parameters, state)
        self.assertEqual(str(caught.exception), "DJI_FLYTO_IMPLICIT_CLIMB")

    def test_same_altitude_point_flight_matches_and_a_diagonal_does_not(self):
        level = {"longitude": 113.01, "latitude": 23.01, "altitude_m": 100.0}
        self.assertIsNone(dji_flyto_replay("point_flight", level, pose()))
        diagonal = {"longitude": 113.01, "latitude": 23.01, "altitude_m": 120.0}
        geometry = point_flight_path(diagonal, pose())
        self.assertEqual(geometry.segments, 1)
        self.assertNotEqual(geometry.points[0]["altitude"], geometry.points[1]["altitude"])
        self.assertEqual(dji_flyto_replay("point_flight", diagonal, pose()), "FLYTO_TRAJECTORY_UNVERIFIED")
        with self.assertRaises(KtdPlatformError) as caught:
            guard_dji_flyto("point_flight", diagonal, pose())
        self.assertEqual(str(caught.exception), "FLYTO_TRAJECTORY_UNVERIFIED")

    def test_vertical_point_flight_is_the_commanded_segment(self):
        parameters = {"longitude": 113.0, "latitude": 23.0, "altitude_m": 120.0}
        self.assertIsNone(dji_flyto_replay("point_flight", parameters, pose()))
        lowered = {"longitude": 113.0, "latitude": 23.0, "altitude_m": 60.0}
        with self.assertRaises(KtdPlatformError) as caught:
            guard_dji_flyto("point_flight", lowered, pose())
        self.assertEqual(str(caught.exception), "DJI_LOW_TARGET_UNVERIFIED")

    def test_replay_uses_metres_not_a_loose_straight_line_exception(self):
        # 低高度的直线终点即使和指令点一致，也不能把回放结果改成放行。
        state = pose(height_m=19.99)
        parameters = {"distance_m": 10}
        commanded = dji_commanded_flyto_point("move_forward", parameters, state)
        geometry = micro_move_path("move_forward", parameters, state)
        end = geometry.points[1]
        gap = horizontal_distance_m(commanded[0], commanded[1], end["longitude"], end["latitude"])
        self.assertLess(gap, 0.001)
        self.assertTrue(math.isclose(commanded[2], end["altitude"]))
        self.assertEqual(dji_flyto_replay("move_forward", parameters, state), "DJI_FLYTO_IMPLICIT_CLIMB")


if __name__ == "__main__":
    unittest.main()
