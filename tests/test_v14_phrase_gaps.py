"""V1.4 4.4 表6 / 4.3.2 / 4.3.4 / 4.1.3 / 4.2：用户可见话术原文与确定性触发回归。

本文件只做两件事，不扩大解析范围：

1. **逐字**断言话术常量与设计规范原文一致（含规范内部不一致处的取舍记录）；
2. 断言每条话术确实挂在一条确定性触发路径上（参数取极值、载荷未挂载、
   不支持的目标/航线类型、复合远目标追问、临时任务超时兜底、低电量返航提示）。

规范未定义处（"距离较远"阈值、超时时长、已挂载能力清单字段）都在被断言处显式
标注为**假设**，不得被读成规范数值。

**入口边界（不要误读成生产可达）**：4.1.3 超时用例经由
``MemoryFlightControlSimulator.resolve_temporary_outcome`` 触发，它是**模拟器/测试
入口**；HTTP / agent 生产路径尚未接线，因此这些用例只证明状态机与模拟器入口行为，
不代表真实平台链路已经会自动超时返航。
"""
from __future__ import annotations

import json
import unittest

from lingxi_flight_control import (
    DecisionKind, FlightControlActionRequest, MemoryFlightControlSimulator, ModelDecision,
    RuntimeBinding, ToolCall,
)
from lingxi_flight_control import flight_policy as policy
from lingxi_flight_control import task_events as events
from lingxi_flight_control.ai_lifecycle import (
    BODY_ANNOTATION_AI_UNSUPPORTED, AlgorithmError, AnnotationAlgorithmConfig,
    AnnotationGeometry,
)
from lingxi_flight_control.cockpit import CockpitService, _in_flight_unsupported
from lingxi_flight_control.geometry import horizontal_distance_m

# ---- 设计规范 V1.4 原文（从 docx 逐字复制，测试断言的唯一期望值来源） ----
DOC_2_2_1_CONTROL = "您尚未获取无人机控制权，请先申请后再下发指令。"            # 2.2.1 / 表6 场景1
DOC_2_2_3_ROUTE = "当前正在执行自动航线，请先说‘暂停航线’或‘悬停’再操作。"       # 2.2.3
DOC_TABLE6_ROUTE = "当前正在执行自动航线，请先说‘暂停航线’或‘悬停’。"            # 4.4 表6 场景2（少「再操作」）
DOC_2_2_5_ZOOM = "当前设备最大变焦倍数为30倍，已为您按最大变焦设置执行。"        # 2.2.5 示例
DOC_4_3_2_ZOOM = "当前设备最大变焦为30倍，已为您按最大变焦设置。"                # 4.3.2 表（第三种写法）
DOC_TABLE6_ZOOM = "指令已拦截，当前设备最大变焦倍数为30倍，已为您按最大变焦设置。"  # 表6 场景3
DOC_4_3_2_LENS = "当前设备未挂载该镜头。"                                      # 4.3.2 表
DOC_4_3_2_SEARCHLIGHT = "当前设备未挂载探照灯。"                                # 4.3.2 表
DOC_4_3_2_VOLUME = "音量最大支持100%，已为您按最大音量设置。"                    # 4.3.2 表 / 表6
DOC_4_3_2_BRIGHTNESS = "亮度最大支持100%，已按最大值设置。"                      # 4.3.2 表 / 表6
DOC_4_3_2_ALGORITHM = "当前项目未配置该AI算法。"                                # 4.3.2 表
DOC_TABLE6_COMPLEX = "当前版本暂不支持自动规划复杂路径或网格航线，请手动接管。"      # 表6 场景「系统不支持」
DOC_TABLE6_MIXED = "单次任务暂不支持混合目标巡检，请分次下发。"                     # 3.2.2 / 表6
DOC_TABLE6_BODY = "当前版本不支持立体目标规划，请将其标注为点或面。"                # 表6 场景「体目标拦截」
DOC_3_2_2_BODY = "当前版本不支持立体目标，请标注为点或面。"                        # 3.2.2 表（变体）
DOC_TABLE6_BODY_AI = "体标注不支持AI配置"                                       # 表6 场景「体标注配置AI拦截」引号内原文
DOC_4_2_RECHARGE = "临时任务未完成，充电重启后将恢复原始任务。"                     # 4.2
DOC_TABLE6_OBSTACLE = "指令已拦截，平台检测到前方存在障碍物，为了安全未执行移动。"   # 表6 R4
DOC_TABLE6_MAPPING = "当前正在执行测绘任务，中断将导致测绘成果失效，是否确认？"        # 表6 R5
DOC_TABLE6_GIMBAL = "当前云台俯仰角度上限为90度，已为您调整。"                       # 表6 R1（4.3.2）
DOC_4_3_4_QUESTION = ("A和B距离较远，单次任务电量可能不足，"
                      "您是先去A再评估电量，还是放弃B点？")                        # 4.3.4 歧义追问


def action(name, **arguments):
    return ModelDecision(DecisionKind.TOOL_CALL, tool_call=ToolCall(name, arguments))


def final(message="计划完成。"):
    return ModelDecision(DecisionKind.FINAL, message=message)


class _Brain:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.inputs = []

    def decide(self, text, checkpoint, tools):
        self.inputs.append(text)
        return self.decisions.pop(0)


def target(longitude, latitude, name=None):
    point = {"longitude": longitude, "latitude": latitude}
    if name is not None:
        point["name"] = name
    return point


class PhraseRegistryTests(unittest.TestCase):
    """第 1 层：话术常量必须与规范原文逐字一致。"""

    def test_control_phrase_is_verbatim(self):
        self.assertEqual(policy.CONTROL_MESSAGE, DOC_2_2_1_CONTROL)

    def test_route_phrase_keeps_2_2_3_wording_and_records_table6_difference(self):
        # 规范内部不一致：2.2.3 有「再操作」，4.4 表6 没有。既有实现符合 2.2.3，
        # 本任务不改写既有话术，只把差异记录进测试，避免被误当成漏改。
        self.assertEqual(policy.ROUTE_MESSAGE, DOC_2_2_3_ROUTE)
        self.assertNotEqual(policy.ROUTE_MESSAGE, DOC_TABLE6_ROUTE)

    def test_camera_zoom_phrase_is_2_2_5_verbatim(self):
        self.assertEqual(policy.CAMERA_ZOOM_MAX_MESSAGE, DOC_2_2_5_ZOOM)
        self.assertEqual(policy.CAMERA_ZOOM_MAX_TEMPLATE.format(maximum=30), DOC_2_2_5_ZOOM)

    def test_camera_zoom_spec_variants_are_recorded(self):
        # 规范同一句有三种写法（2.2.5 / 4.3.2 / 表6），全部记录，避免对账时丢证据。
        self.assertEqual(policy.CAMERA_ZOOM_MAX_MESSAGE, DOC_2_2_5_ZOOM)
        self.assertNotEqual(policy.CAMERA_ZOOM_MAX_MESSAGE, DOC_4_3_2_ZOOM)
        self.assertNotEqual(policy.CAMERA_ZOOM_MAX_MESSAGE, DOC_TABLE6_ZOOM)
        self.assertEqual(policy.CAMERA_ZOOM_MAX_INTERCEPT_MESSAGE, DOC_TABLE6_ZOOM)

    def test_lens_not_mounted_phrase_is_verbatim(self):
        self.assertEqual(policy.LENS_NOT_MOUNTED_MESSAGE, DOC_4_3_2_LENS)

    def test_searchlight_not_mounted_phrase_is_verbatim(self):
        self.assertEqual(policy.SEARCHLIGHT_NOT_MOUNTED_MESSAGE, DOC_4_3_2_SEARCHLIGHT)

    def test_speaker_volume_max_phrase_is_verbatim(self):
        self.assertEqual(policy.SPEAKER_VOLUME_MAX_MESSAGE, DOC_4_3_2_VOLUME)
        self.assertEqual(policy.SPEAKER_VOLUME_MAX_TEMPLATE.format(maximum=100), DOC_4_3_2_VOLUME)

    def test_searchlight_brightness_max_phrase_is_verbatim(self):
        self.assertEqual(policy.SEARCHLIGHT_BRIGHTNESS_MAX_MESSAGE, DOC_4_3_2_BRIGHTNESS)
        self.assertEqual(policy.SEARCHLIGHT_BRIGHTNESS_MAX_TEMPLATE.format(maximum=100),
                         DOC_4_3_2_BRIGHTNESS)

    def test_algorithm_not_configured_phrase_is_verbatim(self):
        self.assertEqual(policy.AI_ALGORITHM_NOT_CONFIGURED_MESSAGE, DOC_4_3_2_ALGORITHM)

    def test_complex_route_phrase_is_verbatim(self):
        self.assertEqual(policy.COMPLEX_ROUTE_MANUAL_MESSAGE, DOC_TABLE6_COMPLEX)

    def test_mixed_target_phrase_is_verbatim(self):
        self.assertEqual(policy.MIXED_TARGET_MESSAGE, DOC_TABLE6_MIXED)

    def test_body_target_phrase_is_verbatim_and_3_2_2_variant_recorded(self):
        self.assertEqual(policy.BODY_TARGET_MESSAGE, DOC_TABLE6_BODY)
        self.assertEqual(policy.BODY_TARGET_STANDARD_MESSAGE, DOC_3_2_2_BODY)
        self.assertNotEqual(policy.BODY_TARGET_MESSAGE, policy.BODY_TARGET_STANDARD_MESSAGE)

    def test_body_annotation_ai_phrase_is_verbatim(self):
        self.assertEqual(policy.BODY_ANNOTATION_AI_PHRASE, DOC_TABLE6_BODY_AI)
        self.assertEqual(policy.BODY_ANNOTATION_AI_MESSAGE, DOC_TABLE6_BODY_AI + "。")

    def test_table6_r4_obstacle_phrase_recorded_as_difference(self):
        # 表6 R4 原文与当前对外实现（4.3.1 合并版）不同：只记录差异，不把「不同」当通过。
        self.assertEqual(policy.TABLE6_OBSTACLE_MESSAGE, DOC_TABLE6_OBSTACLE)
        self.assertEqual(policy.BLOCKED_MESSAGE,
                         "指令已拦截，平台检测到路径存在障碍物、禁飞区或限高，为了安全未执行移动。")
        self.assertNotEqual(policy.BLOCKED_MESSAGE, policy.TABLE6_OBSTACLE_MESSAGE)

    def test_table6_r5_mapping_interrupt_phrase_recorded_as_difference(self):
        # 表6 R5 原文与当前对外实现（4.1.2 合并版）不同：差异待产品裁决，见模块 docstring。
        self.assertEqual(policy.TABLE6_MAPPING_INTERRUPT_MESSAGE, DOC_TABLE6_MAPPING)
        self.assertNotEqual(policy.MAPPING_MESSAGE, policy.TABLE6_MAPPING_INTERRUPT_MESSAGE)
        self.assertTrue(policy.MAPPING_MESSAGE.startswith("中断将导致测绘成果失效，是否确认继续？"))

    def test_gimbal_pitch_max_phrase_is_verbatim(self):
        self.assertEqual(policy.GIMBAL_PITCH_MAX_TEMPLATE.format(maximum=90), DOC_TABLE6_GIMBAL)

    def test_recharge_notice_is_verbatim(self):
        self.assertEqual(events.TEMPORARY_TASK_UNFINISHED_RECHARGE_MESSAGE, DOC_4_2_RECHARGE)

    def test_compound_question_is_verbatim(self):
        self.assertEqual(policy.COMPOUND_FAR_TARGET_QUESTION, DOC_4_3_4_QUESTION)
        self.assertEqual(
            policy.COMPOUND_FAR_TARGET_QUESTION_TEMPLATE.format(first="A", second="B"),
            DOC_4_3_4_QUESTION)


class ClampPhraseTests(unittest.TestCase):
    """第 2 层：参数取极值时返回规范原文（4.3.2 / 表6 场景3）。"""

    def setUp(self):
        self.state = {"device_limits": {
            "camera_zoom": {"min": 1.0, "max": 30.0},
            "speaker_volume": {"min": 0.0, "max": 100.0},
            "searchlight_brightness": {"min": 0.0, "max": 100.0},
        }}

    def test_camera_zoom_above_max_reports_spec_phrase(self):
        result, adjustments = policy.normalize_parameters(
            "camera_zoom", {"value": 100.0}, self.state)
        self.assertEqual(result["value"], 30.0)
        self.assertEqual(len(adjustments), 1)
        self.assertEqual(adjustments[0]["policy_message"], DOC_2_2_5_ZOOM)

    def test_camera_zoom_below_min_has_no_spec_phrase(self):
        # 规范只给了「取上限」话术，下限未定义 → 不得编造规范原文。
        result, adjustments = policy.normalize_parameters(
            "camera_zoom", {"value": -5.0}, self.state)
        self.assertEqual(result["value"], 1.0)
        self.assertNotIn("policy_message", adjustments[0])

    def test_volume_above_max_reports_spec_phrase(self):
        result, adjustments = policy.normalize_parameters(
            "speaker_volume", {"value": 150.0}, self.state)
        self.assertEqual(result["value"], 100.0)
        self.assertEqual(adjustments[0]["policy_message"], DOC_4_3_2_VOLUME)

    def test_brightness_above_max_reports_spec_phrase(self):
        result, adjustments = policy.normalize_parameters(
            "searchlight_brightness", {"value": 200.0}, self.state)
        self.assertEqual(result["value"], 100.0)
        self.assertEqual(adjustments[0]["policy_message"], DOC_4_3_2_BRIGHTNESS)

    def test_in_range_value_has_no_spec_phrase(self):
        _, adjustments = policy.normalize_parameters("camera_zoom", {"value": 10.0}, self.state)
        self.assertEqual(adjustments, [])

    def test_phrase_uses_actual_device_maximum_not_hard_coded_30(self):
        state = {"device_limits": {"camera_zoom": {"min": 1.0, "max": 12.0}}}
        result, adjustments = policy.normalize_parameters("camera_zoom", {"value": 100.0}, state)
        self.assertEqual(result["value"], 12.0)
        self.assertEqual(adjustments[0]["policy_message"],
                         "当前设备最大变焦倍数为12倍，已为您按最大变焦设置执行。")

    def test_gimbal_up_clamp_reports_pitch_limit_phrase(self):
        state = {"device_limits": {"gimbal_up": {"min": 0.1, "max": 90.0},
                                   "gimbal_pitch_deg": {"min": -90.0, "max": 90.0}},
                 "gimbal_pitch_deg": 0.0}
        result, adjustments = policy.normalize_parameters("gimbal_up", {"angle_deg": 120.0}, state)
        self.assertEqual(result["angle_deg"], 90.0)
        self.assertEqual(adjustments[0]["policy_message"], DOC_TABLE6_GIMBAL)

    def test_gimbal_down_clamp_reports_absolute_pitch_limit_phrase(self):
        # 规范只给「上限」措辞；向下取极值命中下限，本仓以俯仰方向绝对限值填入 X（假设）。
        state = {"device_limits": {"gimbal_down": {"min": 0.1, "max": 90.0},
                                   "gimbal_pitch_deg": {"min": -90.0, "max": 90.0}},
                 "gimbal_pitch_deg": 0.0}
        result, adjustments = policy.normalize_parameters("gimbal_down", {"angle_deg": 120.0}, state)
        self.assertEqual(result["angle_deg"], 90.0)
        self.assertEqual(adjustments[0]["policy_message"], DOC_TABLE6_GIMBAL)


class PayloadMountTests(unittest.TestCase):
    """4.3.2：镜头/探照灯是否挂载只看设备清单，缺失字段不冒充「未挂载」。"""

    def test_unknown_mount_state_returns_nothing(self):
        self.assertIsNone(policy.payload_mount_message("camera_infrared", {}))
        self.assertIsNone(policy.payload_mount_message("payload_searchlight", {}))

    def test_unmounted_lens_returns_spec_phrase(self):
        state = {policy.MOUNTED_LENSES_KEY: ["electro_optical", "wide"]}
        self.assertEqual(policy.payload_mount_message("camera_infrared", state),
                         DOC_4_3_2_LENS)
        self.assertIsNone(policy.payload_mount_message("camera_wide", state))

    def test_unmounted_searchlight_returns_spec_phrase(self):
        state = {policy.MOUNTED_PAYLOADS_KEY: ["megaphone"]}
        self.assertEqual(policy.payload_mount_message("payload_searchlight", state),
                         DOC_4_3_2_SEARCHLIGHT)
        self.assertEqual(policy.payload_mount_message("searchlight_brightness", state),
                         DOC_4_3_2_SEARCHLIGHT)

    def test_empty_mount_list_means_nothing_mounted(self):
        state = {policy.MOUNTED_PAYLOADS_KEY: []}
        self.assertEqual(policy.payload_mount_message("payload_searchlight", state),
                         DOC_4_3_2_SEARCHLIGHT)

    def test_simulator_snapshot_projects_mount_capabilities(self):
        binding = RuntimeBinding('actor', 'project', 'hangar', 'drone', 'batch', 1)
        simulator = MemoryFlightControlSimulator(binding)
        # 未核验时是 None（不是「已挂载」，也不是空清单）。
        self.assertIsNone(simulator.snapshot()['state'][policy.MOUNTED_LENSES_KEY])
        self.assertIsNone(simulator.snapshot()['state'][policy.MOUNTED_PAYLOADS_KEY])
        simulator.state.mounted_lenses = ['electro_optical']
        simulator.state.mounted_payloads = ['megaphone']
        snapshot = simulator.snapshot()['state']
        self.assertEqual(snapshot[policy.MOUNTED_LENSES_KEY], ['electro_optical'])
        self.assertEqual(snapshot[policy.MOUNTED_PAYLOADS_KEY], ['megaphone'])
        # 快照投影后，纯策略函数直接可用（不经 patch）。
        self.assertEqual(
            policy.payload_mount_message('camera_infrared',
                                         {policy.MOUNTED_LENSES_KEY: snapshot[policy.MOUNTED_LENSES_KEY]}),
            DOC_4_3_2_LENS)


class CompoundAmbiguityTests(unittest.TestCase):
    """4.3.4：只有「两个不同目标 + 距离可测且超过阈值」才追问；阈值是显式假设。"""

    def setUp(self):
        self.shanghai = target(113.0, 23.0)

    def test_threshold_is_an_explicit_assumption(self):
        self.assertEqual(policy.COMPOUND_FAR_TARGET_DISTANCE_ASSUMPTION_M, 2000.0)
        # 规范只说「距离较远」，没有给数值；这里显式标注为假设，可被调用方覆盖。
        self.assertEqual(policy.compound_distance_question(
            [self.shanghai, target(114.0, 23.0)]), DOC_4_3_4_QUESTION)

    def test_near_targets_do_not_trigger(self):
        self.assertIsNone(policy.compound_distance_question(
            [self.shanghai, target(113.001, 23.0)]))

    def test_threshold_boundary_is_strictly_greater(self):
        nearer = target(113.01, 23.0)
        distance = horizontal_distance_m(113.0, 23.0, 113.01, 23.0)
        self.assertIsNone(policy.compound_distance_question(
            [self.shanghai, nearer], limit_m=distance))
        self.assertEqual(policy.compound_distance_question(
            [self.shanghai, nearer], limit_m=distance - 1.0), DOC_4_3_4_QUESTION)

    def test_default_threshold_is_the_2000m_assumption(self):
        near = target(113.01, 23.0)   # 约 1.0 km，低于 2000 m 假设阈值
        far = target(113.02, 23.0)    # 约 2.0 km，超过 2000 m 假设阈值
        self.assertIsNone(policy.compound_distance_question([self.shanghai, near]))
        self.assertEqual(policy.compound_distance_question([self.shanghai, far]),
                         DOC_4_3_4_QUESTION)

    def test_identical_targets_do_not_trigger(self):
        same = target(113.0, 23.0)
        self.assertIsNone(policy.compound_distance_question([self.shanghai, same]))

    def test_one_target_does_not_trigger(self):
        self.assertIsNone(policy.compound_distance_question([self.shanghai]))

    def test_three_targets_do_not_use_two_target_sentence(self):
        # 规范只给了 A/B 两目标句式；三个及以上目标不套用，交回模型澄清。
        self.assertIsNone(policy.compound_distance_question(
            [self.shanghai, target(114.0, 23.0), target(115.0, 23.0)]))

    def test_missing_or_invalid_coordinates_do_not_trigger(self):
        self.assertIsNone(policy.compound_distance_question(
            [self.shanghai, {"longitude": None, "latitude": 23.0}]))
        self.assertIsNone(policy.compound_distance_question(
            [self.shanghai, {"longitude": float("nan"), "latitude": 23.0}]))
        self.assertIsNone(policy.compound_distance_question(
            [self.shanghai, {"longitude": 999.0, "latitude": 23.0}]))

    def test_named_targets_replace_a_and_b_placeholders(self):
        question = policy.compound_distance_question(
            [target(113.0, 23.0, name="一号塔"), target(114.0, 23.0, name="二号塔")])
        self.assertEqual(question, "一号塔和二号塔距离较远，单次任务电量可能不足，"
                                   "您是先去一号塔再评估电量，还是放弃二号塔点？")

    def test_negative_limit_is_rejected_instead_of_guessed(self):
        with self.assertRaises(ValueError):
            policy.compound_distance_question([self.shanghai, target(114.0, 23.0)], limit_m=-1)


class CockpitPhraseTriggerTests(unittest.TestCase):
    """第 3 层：规范话术经驾驶舱真实入口可见（模拟器，不联网）。"""

    def setUp(self):
        self.clock = [1000.0]
        self.binding = RuntimeBinding('actor-1', 'project-1', 'hangar-1', 'drone-1', 'batch-1', 1)
        self.simulator = MemoryFlightControlSimulator(self.binding, clock=lambda: self.clock[0])
        self.simulator.state.control_owner = 'actor-1'
        self.simulator.state.longitude = 113.0
        self.simulator.state.latitude = 23.0
        self.simulator.state.altitude_m = 50
        self.counter = 0

    def service(self, *decisions, plan=False, planner=None):
        brain = _Brain(list(decisions))
        kwargs = {}
        if plan:
            kwargs.update(enable_plan_execution=True, plan_decider_factory=lambda: planner)
        return CockpitService(self.binding, self.simulator, lambda: brain,
                              clock=lambda: self.clock[0], **kwargs)

    def message(self, service, text, session='phrase-tab'):
        self.counter += 1
        response = service.handle(
            'POST', '/api/flight-control/agent/messages',
            json.dumps({'text': text, 'request_id': 'phrase-' + str(self.counter),
                        'context_version': 1}),
            {'X-Agent-Session': session})
        self.assertEqual(response.status_code, 200)
        return response.payload

    # ---- 4.3.2 参数取极值 ----

    def test_volume_clamp_message_contains_spec_phrase(self):
        result = self.message(self.service(), '音量调到150%')
        self.assertEqual(result['status'], 'preview_required', result)
        self.assertIn(DOC_4_3_2_VOLUME, result['message'])
        self.assertEqual(result['card']['adjustments'][0]['policy_message'], DOC_4_3_2_VOLUME)
        self.assertEqual(result['card']['parameters'], {'value': 100.0})

    def test_brightness_clamp_message_contains_spec_phrase(self):
        result = self.message(self.service(), '亮度调到200%')
        self.assertEqual(result['status'], 'preview_required', result)
        self.assertIn(DOC_4_3_2_BRIGHTNESS, result['message'])
        self.assertEqual(result['card']['parameters'], {'value': 100.0})

    def test_camera_zoom_clamp_message_contains_spec_phrase(self):
        service = self.service(action('camera_zoom', value=100))
        result = self.message(service, '变焦100倍')
        self.assertEqual(result['status'], 'preview_required', result)
        self.assertIn(DOC_2_2_5_ZOOM, result['message'])
        self.assertEqual(result['card']['parameters'], {'value': 30.0})

    # ---- 4.3.2 未挂载镜头 / 探照灯 ----

    def test_unmounted_lens_is_rejected_with_spec_phrase(self):
        # 真实状态链路：sim.state → snapshot/read_state → cockpit；不使用 patch。
        self.simulator.state.mounted_lenses = ['electro_optical', 'wide']
        service = self.service(action('camera_infrared'))
        result = self.message(service, '切到红外')
        self.assertEqual(result['status'], 'rejected', result)
        self.assertEqual(result['error_code'], 'PAYLOAD_NOT_MOUNTED')
        self.assertEqual(result['message'], DOC_4_3_2_LENS)
        self.assertNotIn('card', result)

    def test_mounted_lens_reaches_preview(self):
        self.simulator.state.mounted_lenses = ['electro_optical', 'infrared']
        service = self.service(action('camera_infrared'))
        result = self.message(service, '切到红外')
        self.assertEqual(result['status'], 'preview_required', result)
        self.assertEqual(result['card']['action'], 'camera_infrared')

    def test_unmounted_searchlight_is_rejected_with_spec_phrase(self):
        self.simulator.state.mounted_payloads = ['megaphone']
        service = self.service(action('payload_searchlight', enabled=True))
        result = self.message(service, '打开探照灯')
        self.assertEqual(result['status'], 'rejected', result)
        self.assertEqual(result['error_code'], 'PAYLOAD_NOT_MOUNTED')
        self.assertEqual(result['message'], DOC_4_3_2_SEARCHLIGHT)
        self.assertNotIn('card', result)

    def test_unknown_mount_state_still_reaches_preview(self):
        # 清单字段缺失 = 未核验，不能宣称「未挂载」；设备事实由执行器/平台判定。
        service = self.service(action('payload_searchlight', enabled=True))
        result = self.message(service, '打开探照灯')
        self.assertEqual(result['status'], 'preview_required', result)

    # ---- 4.3.2 未配置 AI 算法（既有确定性路径，逐字回归） ----

    def test_algorithm_not_configured_is_reported_verbatim(self):
        result = self.message(self.service(), '打开AI识别烟火')
        self.assertEqual(result['status'], 'rejected', result)
        self.assertEqual(result['error_code'], 'ALGORITHM_NOT_CONFIGURED')
        self.assertEqual(result['message'], DOC_4_3_2_ALGORITHM)

    # ---- 4.4 表6 系统不支持 / 混合目标 / 体目标 ----

    def test_grid_route_is_rejected_with_spec_phrase(self):
        for text in ('绕楼螺旋飞', '用网格航线飞', '用网格飞'):
            result = self.message(self.service(), text)
            self.assertEqual(result['status'], 'rejected', result)
            self.assertEqual(result['error_code'], 'UNSUPPORTED_IN_FLIGHT_TASK')
            self.assertEqual(result['message'], DOC_TABLE6_COMPLEX)
            self.assertNotIn('card', result)

    def test_mixed_target_is_rejected_with_spec_phrase(self):
        result = self.message(self.service(), '去A塔，再沿着B河道巡视')
        self.assertEqual(result['status'], 'rejected', result)
        self.assertEqual(result['error_code'], 'MIXED_TARGET_UNSUPPORTED')
        self.assertEqual(result['message'], DOC_TABLE6_MIXED)
        self.assertNotIn('card', result)

    def test_body_target_is_rejected_with_spec_phrase(self):
        result = self.message(self.service(), '去巡检那栋立体大楼的外立面')
        self.assertEqual(result['status'], 'rejected', result)
        self.assertEqual(result['error_code'], 'UNSUPPORTED_TARGET_GEOMETRY')
        self.assertEqual(result['message'], DOC_TABLE6_BODY)
        self.assertNotIn('card', result)

    def test_mapping_request_keeps_its_existing_in_flight_wording(self):
        # 测绘类在飞行中的既有拦截语不属于本次补齐的 表6 十场景话术，保持原文不动。
        result = self.message(self.service(), '开始三维建模')
        self.assertEqual(result['status'], 'rejected', result)
        self.assertNotIn('card', result)
        self.assertIn('请结束当前控制后使用任务规划入口', result['message'])

    def test_plan_step_with_unmounted_lens_is_rejected_with_spec_phrase(self):
        self.simulator.state.mounted_lenses = ['electro_optical']
        planner = _Brain([action('camera_infrared'), final()])
        service = self.service(plan=True, planner=planner)
        result = self.message(service, '切到红外')
        self.assertEqual(result['status'], 'clarification', result)
        self.assertEqual(result['message'], DOC_4_3_2_LENS)

    # ---- 4.3.4 顺序复合远目标追问 ----

    def test_plan_with_two_far_targets_asks_spec_question_before_card(self):
        planner = _Brain([
            action('point_flight', longitude=113.0, latitude=23.0, altitude_m=50),
            action('point_flight', longitude=114.0, latitude=23.0, altitude_m=50),
            final(),
        ])
        service = self.service(plan=True, planner=planner)
        result = self.message(service, '去东经113.0北纬23.0高50米，再去东经114.0北纬23.0高50米')
        self.assertEqual(result['status'], 'clarification', result)
        self.assertEqual(result['error_code'], policy.COMPOUND_DISTANCE_CLARIFICATION_REQUIRED)
        self.assertEqual(result['message'], DOC_4_3_4_QUESTION)
        self.assertNotIn('card', result)

    def test_plan_with_two_near_targets_shows_plan_card(self):
        planner = _Brain([
            action('point_flight', longitude=113.0, latitude=23.0, altitude_m=50),
            action('point_flight', longitude=113.0005, latitude=23.0, altitude_m=50),
            final(),
        ])
        service = self.service(plan=True, planner=planner)
        result = self.message(service, '去东经113.0北纬23.0高50米，再去东经113.0005北纬23.0高50米')
        self.assertEqual(result['status'], 'preview_required', result)
        self.assertEqual([step['action'] for step in result['card']['steps']],
                         ['point_flight', 'point_flight'])

    def test_single_target_plan_does_not_ask_ambiguity_question(self):
        planner = _Brain([
            action('point_flight', longitude=113.0, latitude=23.0, altitude_m=50),
            final(),
        ])
        service = self.service(plan=True, planner=planner)
        result = self.message(service, '去东经113.0北纬23.0高50米')
        self.assertEqual(result['status'], 'preview_required', result)


class TemporaryDecisionTimeoutTests(unittest.TestCase):
    """4.1.3 超时兜底：60 秒是显式假设；超时后按 4.2 低电量规则处理。

    入口边界：本节经 ``MemoryFlightControlSimulator.resolve_temporary_outcome``
    （模拟器/测试入口）触发；HTTP / agent 生产路径尚未接线，用例不代表生产已可达。
    """

    def setUp(self):
        self.clock = [1000.0]
        self.binding = RuntimeBinding('actor', 'project', 'hangar', 'drone', 'batch', 1)
        self.simulator = MemoryFlightControlSimulator(self.binding, clock=lambda: self.clock[0])
        self.simulator.state.control_owner = 'actor'
        self.simulator.state.route_id = 'route-main'
        self.simulator.state.route_status = 'running'
        self.simulator.state.task_status = 'route_executing'
        self.simulator.state.breakpoint_index = 7
        self.counter = 0

    def preview(self, name, **parameters):
        self.counter += 1
        key = 'cmd-' + str(self.counter)
        return self.simulator.preview(
            FlightControlActionRequest(key, key, name, parameters, 1))

    def confirm(self, card):
        return self.simulator.confirm(
            card.confirmation_id, command_id=card.command_id,
            idempotency_key=card.idempotency_key,
            confirmation_binding=card.confirmation_binding, context_version=1)

    def complete_temporary_task(self):
        self.confirm(self.preview('point_flight', longitude=113.5, latitude=23.5,
                                  altitude_m=50))
        temporary = self.simulator.state.temporary_task_id
        self.assertIsNotNone(temporary)
        self.simulator.apply_task_event('temporary_completed', task_batch_id='batch',
                                        temporary_task_id=temporary)
        self.assertEqual(self.simulator.state.route_status, 'temporary_completed')

    def test_timeout_length_is_an_explicit_assumption(self):
        self.assertEqual(events.POST_TEMPORARY_DECISION_TIMEOUT_SEC, 60.0)
        self.assertEqual(events.TEMPORARY_DECISION_TIMEOUT_ACTION, 'low_battery_return')

    def test_deadline_is_anchored_on_platform_event_time(self):
        self.complete_temporary_task()
        self.assertEqual(self.simulator.state.temporary_completed_at, 1000.0)
        self.assertEqual(self.simulator.state.temporary_decision_deadline, 1060.0)
        self.assertEqual(events.temporary_decision_deadline(self.simulator.state), 1060.0)

    def test_before_timeout_still_requires_explicit_choice(self):
        self.complete_temporary_task()
        decision = events.post_temporary_decision(self.simulator.state, now=1059.9)
        self.assertTrue(decision['required'])
        self.assertFalse(decision['timed_out'])
        self.assertAlmostEqual(decision['remaining_sec'], 0.1)
        self.assertIsNone(decision['timeout_error_code'])
        # 未超时前仍不许系统替用户猜。
        self.assertEqual(events.validate_post_temporary_choice(
            self.simulator.state, 'resume_main', now=1059.9), 'resume_main')

    def test_timeout_boundary_is_inclusive(self):
        self.complete_temporary_task()
        self.assertFalse(events.temporary_decision_timed_out(self.simulator.state, now=1059.999))
        self.assertTrue(events.temporary_decision_timed_out(self.simulator.state, now=1060.0))
        decision = events.post_temporary_decision(self.simulator.state, now=1060.0)
        self.assertTrue(decision['timed_out'])
        self.assertEqual(decision['timeout_error_code'], events.TEMPORARY_DECISION_TIMED_OUT)

    def test_choice_after_timeout_is_refused_then_low_battery_rule_applies(self):
        self.complete_temporary_task()
        with self.assertRaises(events.TaskEventError) as caught:
            events.validate_post_temporary_choice(self.simulator.state, 'resume_main', now=1060.0)
        self.assertEqual(caught.exception.code, events.TEMPORARY_DECISION_TIMED_OUT)

    def test_apply_before_timeout_changes_nothing(self):
        self.complete_temporary_task()
        before = self.simulator.state.to_dict()
        result = events.apply_temporary_decision_timeout(self.simulator.state, now=1059.0)
        self.assertFalse(result['applied'])
        self.assertEqual(result['reason'], 'not_timed_out')
        self.assertEqual(self.simulator.state.to_dict(), before)

    def test_timeout_applies_low_battery_rule_and_keeps_breakpoint(self):
        self.complete_temporary_task()
        revision = self.simulator.state.revision
        result = events.apply_temporary_decision_timeout(self.simulator.state, now=1060.0)
        self.assertTrue(result['applied'])
        self.assertEqual(result['action'], 'low_battery_return')
        self.assertTrue(self.simulator.state.low_battery_return)
        self.assertEqual(self.simulator.state.task_status, 'returning_home')
        # 与 4.2 同一套断点语义：超时不得清空主线断点。
        self.assertEqual(self.simulator.state.breakpoint_index, 7)
        self.assertIsNone(self.simulator.state.temporary_task_id)
        self.assertEqual(self.simulator.state.revision, revision + 1)

    def test_timeout_application_is_idempotent(self):
        self.complete_temporary_task()
        first = events.apply_temporary_decision_timeout(self.simulator.state, now=1060.0)
        self.assertTrue(first['applied'])
        snapshot = self.simulator.state.to_dict()
        second = events.apply_temporary_decision_timeout(self.simulator.state, now=1070.0)
        self.assertFalse(second['applied'])
        self.assertEqual(second['reason'], 'low_battery_return')
        self.assertEqual(self.simulator.state.to_dict(), snapshot)

    def test_recharge_after_timeout_resumes_original_mainline(self):
        self.complete_temporary_task()
        events.apply_temporary_decision_timeout(self.simulator.state, now=1060.0)
        self.simulator.apply_task_event('recharged_online', task_batch_id='batch')
        self.assertFalse(self.simulator.state.low_battery_return)
        self.assertEqual(self.simulator.state.route_status, 'running')
        self.assertEqual(self.simulator.state.task_status, 'route_executing')
        self.assertEqual(self.simulator.state.breakpoint_index, 7)

    # ---- 4.1.3 生产入口：resolve_temporary_outcome（注入确定性时钟） ----

    def test_production_entry_before_deadline_still_requires_choice(self):
        self.complete_temporary_task()
        self.clock[0] = 1059.0
        decision = self.simulator.resolve_temporary_outcome()
        self.assertTrue(decision['required'])
        self.assertFalse(decision['timed_out'])
        self.assertEqual(decision['options'], list(events.POST_TEMPORARY_OPTIONS))

    def test_production_entry_before_deadline_accepts_explicit_choice(self):
        self.complete_temporary_task()
        self.clock[0] = 1059.0
        result = self.simulator.resolve_temporary_outcome('resume_main')
        self.assertFalse(result['required'])
        self.assertEqual(self.simulator.state.route_status, 'running')
        self.assertEqual(self.simulator.state.task_status, 'route_executing')
        self.assertFalse(self.simulator.state.low_battery_return)

    def test_production_entry_after_deadline_applies_low_battery_rule(self):
        self.complete_temporary_task()
        self.clock[0] = 1060.0
        result = self.simulator.resolve_temporary_outcome()
        self.assertTrue(result['timed_out'])
        self.assertTrue(result['applied'])
        self.assertEqual(result['action'], 'low_battery_return')
        self.assertTrue(self.simulator.state.low_battery_return)
        self.assertEqual(self.simulator.state.task_status, 'returning_home')
        # 4.2 断点语义：超时兜底保留主线断点，等待充电重启后续飞。
        self.assertEqual(self.simulator.state.breakpoint_index, 7)

    def test_production_entry_rejects_choice_after_deadline(self):
        self.complete_temporary_task()
        self.clock[0] = 1060.0
        with self.assertRaises(events.TaskEventError) as caught:
            self.simulator.resolve_temporary_outcome('resume_main')
        self.assertEqual(caught.exception.code, events.TEMPORARY_DECISION_TIMED_OUT)
        # 选择未被采纳：不恢复主线，已按低电量规则进入返航。
        self.assertNotEqual(self.simulator.state.route_status, 'running')
        self.assertTrue(self.simulator.state.low_battery_return)
        self.assertEqual(self.simulator.state.task_status, 'returning_home')

    def test_explicit_choice_before_deadline_disarms_timer(self):
        # 用户已选「恢复主线」后，哪怕时钟越过原 deadline，也不得再判超时返航。
        self.complete_temporary_task()
        self.clock[0] = 1059.0
        self.simulator.resolve_temporary_outcome('resume_main')
        self.clock[0] = 5000.0
        decision = self.simulator.resolve_temporary_outcome()
        self.assertFalse(decision['timed_out'])
        self.assertFalse(self.simulator.state.low_battery_return)
        self.assertEqual(self.simulator.state.route_status, 'running')
        self.assertIsNone(self.simulator.state.temporary_decision_deadline)

    def test_production_entry_timeout_then_recharge_resumes_mainline(self):
        self.complete_temporary_task()
        self.clock[0] = 1200.0
        self.simulator.resolve_temporary_outcome()
        self.simulator.apply_task_event('recharged_online', task_batch_id='batch')
        self.assertFalse(self.simulator.state.low_battery_return)
        self.assertEqual(self.simulator.state.route_status, 'running')
        self.assertEqual(self.simulator.state.task_status, 'route_executing')
        self.assertEqual(self.simulator.state.breakpoint_index, 7)


class InFlightUnsupportedBoundaryTests(unittest.TestCase):
    """L6：不支持话术的确定性判定不能把单目标/载荷动作或否定句误判成拒绝。"""

    def test_single_point_with_payload_action_is_not_mixed_target(self):
        # 「到目标点后全域拍照」是单点目标 + 载荷动作，不是点+面混合目标。
        self.assertIsNone(_in_flight_unsupported('到目标点后全域拍照'))

    def test_negated_or_substituted_route_words_are_not_rejected(self):
        for text in ('不要用网格航线，改用正射影像', '如果我用网格航线飞会怎样',
                     '能不能用网格航线飞'):
            self.assertIsNone(_in_flight_unsupported(text))

    def test_table6_mixed_sample_is_still_rejected_verbatim(self):
        self.assertEqual(_in_flight_unsupported('去A塔，再沿着B河道巡视'),
                         ('MIXED_TARGET_UNSUPPORTED', DOC_TABLE6_MIXED))

    def test_geometry_route_is_rejected_with_same_spec_phrase(self):
        # 规范 1.1/3.2.1 注记：网格航线与**几何体航线**都直接拒绝。
        for text in ('用几何体航线飞', '几何体航测', '走几何体航线'):
            self.assertEqual(_in_flight_unsupported(text),
                             ('UNSUPPORTED_IN_FLIGHT_TASK', DOC_TABLE6_COMPLEX), text)

    def test_plain_geometry_word_is_not_rejected(self):
        # 裸词「几何体」不是航线请求（例如检查建筑几何体是否变形），不得误拒。
        self.assertIsNone(_in_flight_unsupported('检查建筑几何体是否变形'))
        self.assertIsNone(_in_flight_unsupported('这个几何体标得很准'))

    def test_negated_geometry_route_is_not_rejected(self):
        self.assertIsNone(_in_flight_unsupported('不要用几何体航线'))

    def test_cockpit_rejects_geometry_route_with_spec_phrase(self):
        clock = [1000.0]
        binding = RuntimeBinding('actor-9', 'project-1', 'hangar-1', 'drone-1', 'batch-1', 1)
        simulator = MemoryFlightControlSimulator(binding, clock=lambda: clock[0])
        simulator.state.control_owner = 'actor-9'
        service = CockpitService(binding, simulator, lambda: None, clock=lambda: clock[0])
        response = service.handle(
            'POST', '/api/flight-control/agent/messages',
            json.dumps({'text': '用几何体航线飞', 'request_id': 'geometry-route-1',
                        'context_version': 1}),
            {'X-Agent-Session': 'geometry-tab'})
        result = response.payload
        self.assertEqual(result['status'], 'rejected', result)
        self.assertEqual(result['error_code'], 'UNSUPPORTED_IN_FLIGHT_TASK')
        self.assertEqual(result['message'], DOC_TABLE6_COMPLEX)
        self.assertNotIn('card', result)

    def test_cockpit_does_not_reject_plain_geometry_word(self):
        clock = [1000.0]
        binding = RuntimeBinding('actor-9', 'project-1', 'hangar-1', 'drone-1', 'batch-1', 1)
        simulator = MemoryFlightControlSimulator(binding, clock=lambda: clock[0])
        simulator.state.control_owner = 'actor-9'
        service = CockpitService(binding, simulator, lambda: None, clock=lambda: clock[0])
        response = service.handle(
            'POST', '/api/flight-control/agent/messages',
            json.dumps({'text': '检查建筑几何体是否变形', 'request_id': 'geometry-word-1',
                        'context_version': 1}),
            {'X-Agent-Session': 'geometry-tab'})
        result = response.payload
        self.assertNotEqual(result.get('error_code'), 'UNSUPPORTED_IN_FLIGHT_TASK')
        self.assertNotEqual(result.get('message'), DOC_TABLE6_COMPLEX)
        self.assertNotIn('card', result)

    def test_cockpit_does_not_treat_negated_geometry_route_as_request(self):
        clock = [1000.0]
        binding = RuntimeBinding('actor-9', 'project-1', 'hangar-1', 'drone-1', 'batch-1', 1)
        simulator = MemoryFlightControlSimulator(binding, clock=lambda: clock[0])
        simulator.state.control_owner = 'actor-9'
        service = CockpitService(binding, simulator, lambda: None, clock=lambda: clock[0])
        response = service.handle(
            'POST', '/api/flight-control/agent/messages',
            json.dumps({'text': '不要用几何体航线', 'request_id': 'geometry-neg-1',
                        'context_version': 1}),
            {'X-Agent-Session': 'geometry-tab'})
        result = response.payload
        self.assertEqual(result['status'], 'clarification', result)
        self.assertNotEqual(result.get('message'), DOC_TABLE6_COMPLEX)

    def test_cockpit_does_not_reject_point_with_payload_action_as_mixed(self):
        clock = [1000.0]
        binding = RuntimeBinding('actor-9', 'project-1', 'hangar-1', 'drone-1', 'batch-1', 1)
        simulator = MemoryFlightControlSimulator(binding, clock=lambda: clock[0])
        simulator.state.control_owner = 'actor-9'
        service = CockpitService(binding, simulator, lambda: None, clock=lambda: clock[0])
        response = service.handle(
            'POST', '/api/flight-control/agent/messages',
            json.dumps({'text': '到目标点后全域拍照', 'request_id': 'mixed-neg-1',
                        'context_version': 1}),
            {'X-Agent-Session': 'mixed-neg-tab'})
        result = response.payload
        self.assertNotEqual(result.get('message'), DOC_TABLE6_MIXED)
        self.assertNotEqual(result.get('error_code'), 'MIXED_TARGET_UNSUPPORTED')

    def test_cockpit_lets_negated_route_words_fall_through_to_no_action_gate(self):
        clock = [1000.0]
        binding = RuntimeBinding('actor-9', 'project-1', 'hangar-1', 'drone-1', 'batch-1', 1)
        simulator = MemoryFlightControlSimulator(binding, clock=lambda: clock[0])
        simulator.state.control_owner = 'actor-9'
        service = CockpitService(binding, simulator, lambda: None, clock=lambda: clock[0])
        response = service.handle(
            'POST', '/api/flight-control/agent/messages',
            json.dumps({'text': '不要用网格航线，改用正射影像', 'request_id': 'mixed-neg-2',
                        'context_version': 1}),
            {'X-Agent-Session': 'mixed-neg-tab'})
        result = response.payload
        self.assertEqual(result['status'], 'clarification', result)
        self.assertNotEqual(result.get('message'), DOC_TABLE6_COMPLEX)
        self.assertNotIn('card', result)


class TemporaryRechargeNoticeTests(unittest.TestCase):
    """4.2：返航时正在执行临时任务 → 前端提示充电重启后恢复原始任务。"""

    def setUp(self):
        self.clock = [1000.0]
        self.binding = RuntimeBinding('actor', 'project', 'hangar', 'drone', 'batch', 1)
        self.simulator = MemoryFlightControlSimulator(self.binding, clock=lambda: self.clock[0])
        self.simulator.state.control_owner = 'actor'
        self.simulator.state.route_id = 'route-main'
        self.simulator.state.route_status = 'running'
        self.simulator.state.breakpoint_index = 7

    def start_temporary_task(self):
        key = 'notice-cmd'
        card = self.simulator.preview(FlightControlActionRequest(
            key, key, 'point_flight', {'longitude': 113.5, 'latitude': 23.5,
                                       'altitude_m': 50}, 1))
        self.simulator.confirm(card.confirmation_id, command_id=card.command_id,
                               idempotency_key=card.idempotency_key,
                               confirmation_binding=card.confirmation_binding,
                               context_version=1)
        self.assertIsNotNone(self.simulator.state.temporary_task_id)

    def test_low_battery_return_with_unfinished_temporary_task_emits_notice(self):
        self.start_temporary_task()
        self.simulator.apply_task_event('low_battery_return', task_batch_id='batch')
        self.assertTrue(self.simulator.state.low_battery_return)
        self.assertIsNone(self.simulator.state.temporary_task_id)
        self.assertEqual(events.temporary_task_return_notice(self.simulator.state),
                         DOC_4_2_RECHARGE)

    def test_notice_disappears_after_recharging_online(self):
        self.start_temporary_task()
        self.simulator.apply_task_event('low_battery_return', task_batch_id='batch')
        self.simulator.apply_task_event('recharged_online', task_batch_id='batch')
        self.assertIsNone(events.temporary_task_return_notice(self.simulator.state))

    def test_plain_flight_has_no_recharge_notice(self):
        self.assertIsNone(events.temporary_task_return_notice(self.simulator.state))

    def test_simulator_snapshot_projects_return_flags(self):
        self.start_temporary_task()
        self.simulator.apply_task_event('low_battery_return', task_batch_id='batch')
        snapshot = self.simulator.snapshot()['state']
        self.assertTrue(snapshot['low_battery_return'])
        self.assertTrue(snapshot['temporary_task_abandoned'])
        # 充电重启后两个事实都复位，提示不再有依据。
        self.simulator.apply_task_event('recharged_online', task_batch_id='batch')
        snapshot = self.simulator.snapshot()['state']
        self.assertFalse(snapshot['low_battery_return'])
        self.assertFalse(snapshot['temporary_task_abandoned'])

    def test_status_query_surfaces_notice_through_real_snapshot(self):
        # 不 patch：simulator 真实事件 → to_dict/snapshot → CockpitService 状态查询。
        self.start_temporary_task()
        self.simulator.apply_task_event('low_battery_return', task_batch_id='batch')
        service = CockpitService(self.binding, self.simulator, lambda: None,
                                 clock=lambda: self.clock[0])
        response = service.handle(
            'POST', '/api/flight-control/agent/messages',
            json.dumps({'text': '刷新状态', 'request_id': 'notice-query',
                        'context_version': 1}),
            {'X-Agent-Session': 'notice-tab'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.payload['kind'], 'state_query')
        self.assertIn(DOC_4_2_RECHARGE, response.payload['message'])


class BodyAnnotationBackendPhraseTests(unittest.TestCase):
    """4.4 表6「体标注配置AI拦截」：既有后端拒绝路径必须返回同一句原文。"""

    def test_existing_backend_returns_verbatim_body_ai_phrase(self):
        config = AnnotationAlgorithmConfig(
            annotation_id='annotation-1', geometry=AnnotationGeometry.BODY,
            algorithms=('fire',), version=1)
        with self.assertRaises(AlgorithmError) as caught:
            config.validate(configured=('fire',))
        self.assertEqual(caught.exception.code, BODY_ANNOTATION_AI_UNSUPPORTED)
        self.assertEqual(str(caught.exception), policy.BODY_ANNOTATION_AI_MESSAGE)

    def test_quote_punctuation_difference_is_recorded_not_normalized_away(self):
        # 表6 引号内原文无句号；既有后端消息是「原文 + 。」。不擅自改后端文案，
        # 只把「仅差一个结尾句号」固定为显式差异。
        self.assertEqual(policy.BODY_ANNOTATION_AI_PHRASE, DOC_TABLE6_BODY_AI)
        self.assertFalse(policy.BODY_ANNOTATION_AI_PHRASE.endswith("。"))
        self.assertEqual(policy.BODY_ANNOTATION_AI_MESSAGE,
                         policy.BODY_ANNOTATION_AI_PHRASE + "。")
        config = AnnotationAlgorithmConfig(
            annotation_id='annotation-1', geometry=AnnotationGeometry.BODY,
            algorithms=('fire',), version=1)
        with self.assertRaises(AlgorithmError) as caught:
            config.validate(configured=('fire',))
        message = str(caught.exception)
        self.assertNotEqual(message, DOC_TABLE6_BODY_AI)
        self.assertEqual(message.rstrip("。"), DOC_TABLE6_BODY_AI)
        self.assertEqual(message, policy.BODY_ANNOTATION_AI_MESSAGE)


if __name__ == '__main__':
    unittest.main()
