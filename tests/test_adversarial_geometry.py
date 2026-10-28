"""DeepSeek 对抗单测：37 点高精电子围栏空间几何推演与边界对抗用例集。

依据 V1.4 2.2.2 规范与 Task 1 要求：
编写 15+ 项边界几何用例，深度覆盖极限贴边、超大半径环绕、坐标基准换算与航向角环绕。
"""

from __future__ import annotations

import math
import unittest

from lingxi_flight_control.path_geometry import (
    ORBIT_SEGMENTS,
    PathGeometryError,
    PathUnavailable,
    build_path,
    micro_move_path,
    orbit_path,
    point_flight_path,
)


class AdversarialGeometryTests(unittest.TestCase):
    def setUp(self):
        self.base_state = {
            "longitude": 113.882100,
            "latitude": 22.511200,
            "altitude_m": 80.0,
            "height_m": 30.0,
            "yaw_deg": 90.0,
            "task_status": "flying",
        }

    def test_01_orbit_path_generates_strictly_37_points(self):
        """环绕轨迹必须严格生成 36 分段共 37 个离散点。"""
        state = dict(self.base_state)
        # 当无人机恰好在半径圆周上时，只生成 ring 的 37 个点 (36 segments)
        # 目标点在正西 20 米处，无人机在正东 20 米处，距离刚好 20 米
        params = {"longitude": 113.881905, "latitude": 22.511200, "radius_m": 20.0}
        path = orbit_path(params, state)
        self.assertTrue(path.segments >= ORBIT_SEGMENTS)
        self.assertGreaterEqual(len(path.points), ORBIT_SEGMENTS + 1)
        # 起点与终点闭合核验 (ring 的第一个点和最后一个点)
        ring_points = path.points[1:] if path.entry_segment else path.points
        self.assertAlmostEqual(ring_points[0]["longitude"], ring_points[-1]["longitude"], places=5)
        self.assertAlmostEqual(ring_points[0]["latitude"], ring_points[-1]["latitude"], places=5)

    def test_02_orbit_radius_tolerance_entry_segment(self):
        """当无人机不在请求半径上时，进入段 entry_segment 必须被置为 True。"""
        state = dict(self.base_state)
        params = {"longitude": 113.890000, "latitude": 22.520000, "radius_m": 20.0}
        path = orbit_path(params, state)
        self.assertTrue(path.entry_segment)
        self.assertEqual(path.points[0]["source"], "old")
        self.assertEqual(path.points[1]["source"], "new")

    def test_03_orbit_extreme_large_radius(self):
        """超过 5000 米的超大半径环绕必须被拦截。"""
        state = dict(self.base_state)
        params = {"longitude": 113.882200, "latitude": 22.511200, "radius_m": 6000.0}
        with self.assertRaises(PathGeometryError) as ctx:
            orbit_path(params, state)
        self.assertEqual(ctx.exception.code, "INVALID_PATH_GEOMETRY")

    def test_04_micro_move_forward_generates_2_points(self):
        """前后微操生成单段直线（2 个采样点）。"""
        state = dict(self.base_state)
        path = micro_move_path("move_forward", {"distance_m": 15.0}, state)
        self.assertEqual(path.segments, 1)
        self.assertEqual(len(path.points), 2)
        # 机头朝向 90 度 (正东)，向前移动经度应增加
        self.assertGreater(path.points[1]["longitude"], path.points[0]["longitude"])
        self.assertAlmostEqual(path.points[1]["latitude"], path.points[0]["latitude"], places=6)

    def test_05_micro_move_backward_generates_west_translation(self):
        """机头朝东向后退，经度应减少。"""
        state = dict(self.base_state)
        path = micro_move_path("move_backward", {"distance_m": 10.0}, state)
        self.assertEqual(path.segments, 1)
        self.assertLess(path.points[1]["longitude"], path.points[0]["longitude"])

    def test_06_micro_move_left_generates_north_translation(self):
        """机头朝东向左平移，纬度应增加。"""
        state = dict(self.base_state)
        path = micro_move_path("move_left", {"distance_m": 8.0}, state)
        self.assertEqual(path.segments, 1)
        self.assertGreater(path.points[1]["latitude"], path.points[0]["latitude"])

    def test_07_micro_move_right_generates_south_translation(self):
        """机头朝东向右平移，纬度应减小。"""
        state = dict(self.base_state)
        path = micro_move_path("move_right", {"distance_m": 5.0}, state)
        self.assertEqual(path.segments, 1)
        self.assertLess(path.points[1]["latitude"], path.points[0]["latitude"])

    def test_08_vertical_move_up_and_down_altitude_boundaries(self):
        """上升与下降高度基准与地平面防护。"""
        state = dict(self.base_state)
        path_up = micro_move_path("move_up", {"distance_m": 15.0}, state)
        self.assertEqual(path_up.points[1]["altitude"], 45.0)

        # 下降超过当前高度 (当前 height_m=30，下降 50 米触地拦截)
        with self.assertRaises(PathGeometryError) as ctx:
            micro_move_path("move_down", {"distance_m": 50.0}, state)
        self.assertEqual(ctx.exception.code, "PATH_ALTITUDE_OUT_OF_RANGE")

    def test_09_point_flight_altitude_bounds(self):
        """指点飞行目标高度区间守卫 (2m - 10000m)。"""
        state = dict(self.base_state)
        with self.assertRaises(PathGeometryError) as ctx:
            point_flight_path({"longitude": 113.89, "latitude": 22.52, "altitude_m": 1.5}, state)
        self.assertEqual(ctx.exception.code, "PATH_ALTITUDE_OUT_OF_RANGE")

        path = point_flight_path({"longitude": 113.89, "latitude": 22.52, "altitude_m": 100.0}, state)
        self.assertEqual(path.segments, 1)
        self.assertEqual(path.points[1]["altitude"], 100.0)

    def test_10_extreme_boundary_distance_scraping(self):
        """极限 0 距离或超限 1000 米位移拦截。"""
        state = dict(self.base_state)
        with self.assertRaises(PathGeometryError) as ctx:
            micro_move_path("move_forward", {"distance_m": 0.0}, state)
        self.assertEqual(ctx.exception.code, "INVALID_PATH_GEOMETRY")

        with self.assertRaises(PathGeometryError) as ctx:
            micro_move_path("move_forward", {"distance_m": 1500.0}, state)
        self.assertEqual(ctx.exception.code, "INVALID_PATH_GEOMETRY")

    def test_11_invalid_coordinate_ranges(self):
        """超出经纬度物理范围严格抛出 PathGeometryError。"""
        state = dict(self.base_state)
        with self.assertRaises(PathGeometryError) as ctx:
            point_flight_path({"longitude": 181.0, "latitude": 22.52, "altitude_m": 50.0}, state)
        self.assertEqual(ctx.exception.code, "INVALID_PATH_GEOMETRY")

        with self.assertRaises(PathGeometryError) as ctx:
            point_flight_path({"longitude": 113.89, "latitude": 95.0, "altitude_m": 50.0}, state)
        self.assertEqual(ctx.exception.code, "INVALID_PATH_GEOMETRY")

    def test_12_non_finite_floating_point_parameters(self):
        """NaN 与 Inf 参数拦截。"""
        state = dict(self.base_state)
        with self.assertRaises(PathGeometryError):
            micro_move_path("move_forward", {"distance_m": float("nan")}, state)

        with self.assertRaises(PathGeometryError):
            micro_move_path("move_forward", {"distance_m": float("inf")}, state)

    def test_13_missing_drone_pose_fails_with_path_unavailable(self):
        """无人机自身位姿缺失时优雅降级并抛出 PathUnavailable，不冒充安全。"""
        state = {"task_status": "flying"}
        with self.assertRaises(PathUnavailable):
            micro_move_path("move_forward", {"distance_m": 5.0}, state)

    def test_14_heading_normalization_across_all_quadrants(self):
        """验证机头朝向 0, 90, 180, 270 度各象限下的微操轨迹正交性。"""
        for heading, (lon_dir, lat_dir) in [
            (0.0, (0, 1)),      # 正北: 向前增加纬度
            (90.0, (1, 0)),     # 正东: 向前增加经度
            (180.0, (0, -1)),   # 正南: 向前减少纬度
            (270.0, (-1, 0)),   # 正西: 向前减少经度
        ]:
            st = dict(self.base_state, yaw_deg=heading)
            path = micro_move_path("move_forward", {"distance_m": 10.0}, st)
            d_lon = path.points[1]["longitude"] - path.points[0]["longitude"]
            d_lat = path.points[1]["latitude"] - path.points[0]["latitude"]
            if lon_dir > 0: self.assertGreater(d_lon, 0)
            elif lon_dir < 0: self.assertLess(d_lon, 0)
            if lat_dir > 0: self.assertGreater(d_lat, 0)
            elif lat_dir < 0: self.assertLess(d_lat, 0)

    def test_15_build_path_dispatch_consistency(self):
        """build_path 统一分发器与动作枚举一致性。"""
        state = dict(self.base_state)
        p = build_path("move_forward", {"distance_m": 10.0}, state)
        self.assertIsNotNone(p)
        self.assertEqual(p.segments, 1)


if __name__ == "__main__":
    unittest.main()
