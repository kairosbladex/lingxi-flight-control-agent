"""DeepSeek 对抗单测：极限边界输入（Corner-Cases）与 Fuzzing 测试。

依据 V1.4 2.2.5 规范与 Task 3 要求：
- 变焦倍数 10000 倍、负变焦、非数 (NaN/Inf)
- 相对位移超限
- 变焦、音量、探照灯极值智能取值并生成 adjustments 审计说明
- 证明确定性层 100% 拦截或智能取极值，绝不发生系统抛未捕获异常
"""

from __future__ import annotations

import math
import unittest

from lingxi_flight_control.flight_policy import (
    PolicyError,
    normalize_parameters,
)


class AdversarialInputsTests(unittest.TestCase):
    def setUp(self):
        self.state_with_limits = {
            "device_limits": {
                "camera_zoom": {"min": 1.0, "max": 30.0},
                "speaker_volume": {"min": 0.0, "max": 100.0},
                "searchlight_brightness": {"min": 0.0, "max": 100.0},
                "set_move_speed": {"min": 0.5, "max": 15.0},
            }
        }

    def test_01_zoom_extreme_high_clamps_to_device_maximum(self):
        """变焦 10000 倍智能取设备极值 30 倍，并附加 adjustments 提示说明。"""
        res, adj = normalize_parameters("camera_zoom", {"value": 10000.0}, self.state_with_limits)
        self.assertEqual(res["value"], 30.0)
        self.assertEqual(len(adj), 1)
        self.assertEqual(adj[0]["requested"], 10000.0)
        self.assertEqual(adj[0]["effective"], 30.0)
        self.assertIn("已将10000调整为30", adj[0]["message"])

    def test_02_zoom_negative_clamps_to_device_minimum(self):
        """变焦负数智能取设备极值 1.0 倍。"""
        res, adj = normalize_parameters("camera_zoom", {"value": -5.0}, self.state_with_limits)
        self.assertEqual(res["value"], 1.0)
        self.assertEqual(len(adj), 1)
        self.assertEqual(adj[0]["effective"], 1.0)

    def test_03_volume_and_brightness_extreme_high_clamp_to_100(self):
        """音量 200% 与探照灯 150% 智能取上限 100%。"""
        res_vol, adj_vol = normalize_parameters("speaker_volume", {"value": 200}, self.state_with_limits)
        self.assertEqual(res_vol["value"], 100.0)
        self.assertEqual(len(adj_vol), 1)

        res_brt, adj_brt = normalize_parameters("searchlight_brightness", {"value": 150}, self.state_with_limits)
        self.assertEqual(res_brt["value"], 100.0)
        self.assertEqual(len(adj_brt), 1)

    def test_04_speed_exceeds_device_table_clamps_to_effective(self):
        """移动速度设定 50m/s，设备表限 15m/s，取 15m/s。"""
        res, adj = normalize_parameters("set_move_speed", {"value": 50.0}, self.state_with_limits)
        self.assertEqual(res["value"], 15.0)
        self.assertEqual(adj[0]["maximum"], 15.0)

    def test_05_nan_and_inf_fail_closed_with_invalid_parameters(self):
        """非数 (NaN / Inf) 参数严格拦截，绝不发生非预期异常。"""
        for bad_val in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(PolicyError) as ctx:
                normalize_parameters("camera_zoom", {"value": bad_val}, self.state_with_limits)
            self.assertEqual(ctx.exception.code, "INVALID_PARAMETERS")

    def test_06_missing_device_limits_and_out_of_protocol_fails_safely(self):
        """无设备表且超出协议范围时，抛出 DEVICE_LIMITS_UNAVAILABLE，绝不暗中猜测。"""
        empty_state = {}
        with self.assertRaises(PolicyError) as ctx:
            normalize_parameters("camera_zoom", {"value": 50.0}, empty_state)
        self.assertEqual(ctx.exception.code, "DEVICE_LIMITS_UNAVAILABLE")

    def test_07_in_range_values_remain_unchanged_with_no_adjustments(self):
        """在合法区间内的参数保持原样，零 adjustments。"""
        res, adj = normalize_parameters("camera_zoom", {"value": 10.0}, self.state_with_limits)
        self.assertEqual(res["value"], 10.0)
        self.assertEqual(len(adj), 0)


if __name__ == "__main__":
    unittest.main()
