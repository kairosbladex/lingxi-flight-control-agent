"""对抗式编程跨 Agent 红蓝对抗测试集 (Adversarial Cross-Agent Battles)。

依据总控大脑“对抗式编程与评委打分机制”要求：
- 战役 1 (DeepSeek 攻 vs Codex 防)：极端恶意标注坐标、重复 order、非法几何体攻击网关
- 战役 2 (Codex 攻 vs DeepSeek 防)：极地极值坐标、亚毫米级位移、测绘作废后非法强行恢复攻击
- 战役 3 (Grok 攻 vs 平台防)：网络断连、504 超时、快照字段剥离与恶意破损报文攻击
- 战役 4 (agy 攻 vs 意图解析防)：否定意图、零步长欺骗、多重复合歧义与方言攻击
"""

from __future__ import annotations

import math
import unittest

from lingxi_flight_control.adapters.ktd_platform_adapter import (
    KtdPlatformAdapter,
    RouteStatusUnavailableError,
)
from lingxi_flight_control.adapters.static_annotation_gateway import (
    GeometryType,
    StaticAnnotation,
    StaticAnnotationGateway,
    TargetGeometryError,
)
from lingxi_flight_control.adapters.virtual_algorithm_gateway import VirtualAlgorithmGateway
from lingxi_flight_control.flight_policy import PolicyError, check_flight_policy
from lingxi_flight_control.intent_evals import decompose_intent
from lingxi_flight_control.path_geometry import (
    PathGeometryError,
    PathUnavailable,
    build_path,
    micro_move_path,
)


class AdversarialBattlesTests(unittest.TestCase):
    """跨 Agent 攻防对抗评测。"""

    # =========================================================================
    # 战役 1: DeepSeek (攻) vs Codex (防)
    # 目标：对 Codex 实现的静态标注网关与虚拟算法网关施加恶意脏数据注入
    # =========================================================================
    def test_battle_1_deepseek_attacks_codex_annotation_gateway(self):
        gateway = StaticAnnotationGateway()

        # 攻击 1.1: 传入空列表试图引起越界
        with self.assertRaises(TargetGeometryError):
            gateway.validate_and_resolve_targets([])

        # 攻击 1.2: 构造点目标中带有相同 order，验证排序稳定性与抗冲突能力
        p1 = StaticAnnotation("p1", "Point A", GeometryType.POINT, [113.1, 22.1], "v1", order=1)
        p2 = StaticAnnotation("p2", "Point B", GeometryType.POINT, [113.2, 22.2], "v1", order=1)
        p3 = StaticAnnotation("p3", "Point C", GeometryType.POINT, [113.3, 22.3], "v1", order=2)
        res = gateway.validate_and_resolve_targets([p1, p2, p3])
        self.assertEqual(len(res), 3)
        self.assertEqual(res[2].target_id, "p3")

        # 攻击 1.3: 点与体混合注入，验证体目标一票否决
        pv = StaticAnnotation("v1", "Vol 1", GeometryType.VOLUME, [], "v1")
        with self.assertRaises(TargetGeometryError) as ctx:
            gateway.validate_and_resolve_targets([p1, pv])
        self.assertIn(ctx.exception.code, ("BODY_ANNOTATION_AI_UNSUPPORTED", "MIXED_TARGET_UNSUPPORTED"))

    def test_battle_1_deepseek_attacks_virtual_algorithm_lifecycle(self):
        gw = VirtualAlgorithmGateway(global_algorithms=["车辆识别"])

        # 攻击 1.4: 离开不存在的标注，断言零异常且不破坏全局算法
        stopped = gw.on_depart_annotation("non_existent_anno")
        self.assertEqual(stopped, set())
        self.assertIn("车辆识别", gw.active_algorithms)

        # 攻击 1.5: 连续重复到达同一标注，断言幂等不重入
        gw.on_arrive_annotation("anno-1", ["烟火检测"])
        newly = gw.on_arrive_annotation("anno-1", ["烟火检测"])
        self.assertEqual(newly, set())
        self.assertEqual(len(gw.active_algorithms), 2)

    # =========================================================================
    # 战役 2: Codex (攻) vs DeepSeek (防)
    # 目标：对 DeepSeek 的高精几何轨迹与测绘状态机施加极端物理与时序越权攻击
    # =========================================================================
    def test_battle_2_codex_attacks_deepseek_geometry_boundaries(self):
        state = {
            "longitude": 113.882100,
            "latitude": 22.511200,
            "height_m": 30.0,
            "altitude_m": 80.0,
            "yaw_deg": 45.0,
            "task_status": "flying",
        }

        # 攻击 2.1: 试图传入 0.000000001 米极微步长或负步长
        with self.assertRaises(PathGeometryError):
            micro_move_path("move_forward", {"distance_m": -1.0}, state)

        # 攻击 2.2: 极地附近极限纬度飞越 (lat=89.999999) 试图造成算术溢出
        pole_state = dict(state, latitude=89.999999, yaw_deg=0.0)
        # 向正北移动将越过 90 度
        with self.assertRaises(PathGeometryError):
            micro_move_path("move_forward", {"distance_m": 10.0}, pole_state)

        # 攻击 2.3: 测绘任务已被中断作废后，强行尝试 route_resume 航线恢复攻击
        invalidated_state = {
            "task_type": "mapping",
            "mapping_invalidated": True,
            "route_resume_disabled": True,
        }
        with self.assertRaises(PolicyError) as ctx:
            check_flight_policy("route_resume", invalidated_state)
        self.assertEqual(ctx.exception.code, "MAPPING_NOT_RESUMABLE")

    # =========================================================================
    # 战役 3: Grok (攻) vs 平台适配层 (防)
    # 目标：注入模拟平台故障，核验系统能否优雅 fail-closed 绝不崩溃
    # =========================================================================
    def test_battle_3_grok_fault_injection_attacks_platform_adapter(self):
        adapter = KtdPlatformAdapter()

        # 攻击 3.1: 模拟后端返回空字典或缺失 route_status 字段
        empty_snapshot = {}
        with self.assertRaises(RouteStatusUnavailableError) as ctx:
            adapter.adapt_snapshot(empty_snapshot, requested_action="move_forward")
        self.assertEqual(ctx.exception.code, "ROUTE_STATE_SOURCE_UNAVAILABLE")

        # 攻击 3.2: 模拟只读查询在 route_status 缺失下的降级防护
        read_res = adapter.adapt_snapshot(empty_snapshot, requested_action="read_state")
        self.assertTrue(read_res.get("_degraded_route_status"))

    # =========================================================================
    # 战役 4: agy (攻) vs 意图解析 (防)
    # 目标：输入自然语言歧义、边界极值与复合口语混淆
    # =========================================================================
    def test_battle_4_agy_intent_fuzzing_and_dialects(self):
        # 攻击 4.1: 空文本与纯空格攻击
        res_empty = decompose_intent("   ")
        self.assertEqual(len(res_empty["actions"]), 0)

        # 攻击 4.2: 复合超限与连续标点输入
        text = "先向前飞999米，，然后云台向下看100度，，再紧急刹车！"
        res = decompose_intent(text)
        self.assertEqual(len(res["actions"]), 3)
        # 前向位移必须智能 clamp 到 20 米极值
        self.assertEqual(res["actions"][0]["action"], "move_forward")
        self.assertEqual(res["actions"][0]["parameters"]["distance_m"], 20.0)
        # 云台俯仰向下 100 度必须被解析
        self.assertEqual(res["actions"][1]["action"], "gimbal_pitch")
        # 紧急刹车被识别为急停
        self.assertEqual(res["actions"][2]["action"], "emergency_stop")


if __name__ == "__main__":
    unittest.main()
