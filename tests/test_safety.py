"""安全快路径测试：确定性识别、否定句拒绝、模型不可用时的可用性。"""

from __future__ import annotations

import time
import unittest

from lingxi_flight_control import (
    BoundedAgentLoop,
    FlightControlStatus,
    LoopStatus,
    MemoryFlightControlSimulator,
    RuntimeBinding,
    SafetyFastPath,
    SafetyFastPathDecider,
    build_full_control_tool_registry,
)
from lingxi_flight_control.ollama_decider import OllamaDecider


class ExplodingDecider:
    """任何调用都失败，用来证明安全链路不依赖模型。"""

    def __init__(self):
        self.calls = 0

    def decide(self, *_):
        self.calls += 1
        raise ValueError("model server is unreachable")


class StaticDecider:
    def __init__(self, decision):
        self._decision = decision
        self.calls = 0

    def decide(self, *_):
        self.calls += 1
        return self._decision


def binding() -> RuntimeBinding:
    return RuntimeBinding("actor-1", "project-1", "hangar-1", "drone-1", "batch-1", 1)


def cockpit_tools():
    return build_full_control_tool_registry(
        MemoryFlightControlSimulator(binding())).definitions()


def emergency_stop_until_settled(loop, command: str, *, rounds: int = 6):
    """驱动确认：第一轮产生确认绑定，之后以 confirmed=True 放行。

    循环要求先建立确认绑定再执行（刻意的两阶段设计），所以需要两次本地调用；
    但两次都完全不碰模型，总耗时在毫秒级。
    """
    checkpoint = None
    result = None
    for _ in range(rounds):
        result = loop.run(command, checkpoint=checkpoint, confirmed=checkpoint is not None)
        checkpoint = result.checkpoint
        if result.status is not LoopStatus.CLARIFICATION:
            return result
    return result


class RecognitionTests(unittest.TestCase):
    def setUp(self):
        self.lane = SafetyFastPath()

    def test_unambiguous_safety_commands_are_recognized(self):
        cases = {
            "急停": "emergency_stop", "紧急停止": "emergency_stop", "马上停止": "emergency_stop",
            "停止所有动作": "emergency_stop", "刹停": "emergency_stop",
            "返航": "return_home", "一键返航": "return_home",
            "取消返航": "cancel_return_home", "停止返航": "cancel_return_home",
            "降落": "land", "原地降落": "land",
        }
        for text, expected in cases.items():
            call = self.lane.recognize(text)
            self.assertIsNotNone(call, text)
            self.assertEqual(call.name, expected, text)

    def test_takeoff_is_not_in_the_fast_path(self):
        # 快路径只能发空参数，而起飞需要高度；替用户猜高度是危险的。
        for text in ("起飞", "一键起飞", "立即起飞"):
            self.assertIsNone(self.lane.recognize(text), text)

    def test_punctuation_and_whitespace_are_normalized(self):
        for text in ("急停！", " 急停 ", "急停。", "急  停", "急停~"):
            self.assertEqual(self.lane.recognize(text).name, "emergency_stop", repr(text))

    def test_negated_or_hedged_phrases_are_never_recognized(self):
        """子串匹配会在这里出人命：「不要返航」包含「返航」。"""
        for text in ("不要返航", "别返航", "先别返航", "暂时不要返航", "我不返航",
                     "不要降落", "别降落", "能不能返航", "是否要返航", "等我说了再返航",
                     "先不要起飞", "暂停返回"):
            self.assertIsNone(self.lane.recognize(text), text)

    def test_unrelated_and_ambiguous_text_is_left_to_the_model(self):
        for text in ("向前飞10米", "拍照", "停", "停止", "继续", "", "   ", None, "帮我看看状态"):
            self.assertIsNone(self.lane.recognize(text), repr(text))

    def test_conflicting_phrase_table_is_rejected(self):
        with self.assertRaises(ValueError):
            SafetyFastPath({"a": ("急停",), "b": ("急停",)})
        with self.assertRaises(ValueError):
            SafetyFastPath({"a": ()})
        with self.assertRaises(ValueError):
            SafetyFastPath({"a": (" ",)})

    def test_recognized_actions_have_no_arguments(self):
        call = self.lane.recognize("急停")
        self.assertEqual(dict(call.arguments), {})


class DeciderTests(unittest.TestCase):
    def test_safety_command_short_circuits_the_model(self):
        inner = ExplodingDecider()
        decider = SafetyFastPathDecider(inner)
        from lingxi_flight_control.models import AgentCheckpoint
        checkpoint = AgentCheckpoint(user_input="急停", context={}, messages=[],
                                     tool_calls=[], observations=[])
        from lingxi_flight_control import DecisionKind
        decision = decider.decide("急停", checkpoint, cockpit_tools())
        self.assertEqual(decision.kind, DecisionKind.TOOL_CALL)
        self.assertEqual(decision.tool_call.name, "emergency_stop")
        self.assertEqual(inner.calls, 0)          # 模型完全没被调用
        self.assertEqual(decider.safety_hits, 1)

    def test_non_safety_command_delegates(self):
        from lingxi_flight_control import DecisionKind, ModelDecision
        from lingxi_flight_control.models import AgentCheckpoint
        inner = StaticDecider(ModelDecision(DecisionKind.FINAL, message="模型答复"))
        decider = SafetyFastPathDecider(inner)
        checkpoint = AgentCheckpoint(user_input="向前飞10米", context={}, messages=[],
                                     tool_calls=[], observations=[])
        decision = decider.decide("向前飞10米", checkpoint, cockpit_tools())
        self.assertEqual(decision.message, "模型答复")
        self.assertEqual(inner.calls, 1)
        self.assertEqual(decider.fallbacks, 1)

    def test_action_outside_the_tool_surface_falls_back_to_the_model(self):
        from lingxi_flight_control import DecisionKind, ModelDecision
        from lingxi_flight_control.models import AgentCheckpoint
        inner = StaticDecider(ModelDecision(DecisionKind.FINAL, message="模型兜底"))
        decider = SafetyFastPathDecider(inner)
        checkpoint = AgentCheckpoint(user_input="急停", context={}, messages=[],
                                     tool_calls=[], observations=[])
        # 传空工具集：急停不在工具面内，不能凭空返回一个未注册的调用。
        decision = decider.decide("急停", checkpoint, ())
        self.assertEqual(decision.message, "模型兜底")
        self.assertEqual(inner.calls, 1)

    def test_already_executed_action_closes_without_recalling_the_model(self):
        from lingxi_flight_control import DecisionKind
        from lingxi_flight_control.models import AgentCheckpoint
        inner = ExplodingDecider()
        decider = SafetyFastPathDecider(inner)
        checkpoint = AgentCheckpoint(
            user_input="急停", context={}, messages=[], tool_calls=[],
            observations=[{"tool": "emergency_stop", "result": {}}],
            executed_tool_calls=[{"name": "emergency_stop", "arguments": {}, "binding": "b"}])
        decision = decider.decide("急停", checkpoint, cockpit_tools())
        self.assertEqual(decision.kind, DecisionKind.FINAL)
        self.assertEqual(inner.calls, 0)

    def test_closing_message_must_be_a_template(self):
        with self.assertRaises(ValueError):
            SafetyFastPathDecider(ExplodingDecider(), closing_message="没有占位符")


class LatencyAndAvailabilityTests(unittest.TestCase):
    def test_emergency_stop_executes_with_the_model_unreachable(self):
        """模型服务挂掉时急停仍必须可用——这是快路径存在的第二个理由。"""
        simulator = MemoryFlightControlSimulator(binding())
        registry = build_full_control_tool_registry(simulator)
        decider = SafetyFastPathDecider(ExplodingDecider())
        loop = BoundedAgentLoop(decider, registry, max_steps=6)

        started = time.time()
        result = emergency_stop_until_settled(loop, "急停")
        wall = time.time() - started

        self.assertIs(result.status, LoopStatus.FINAL)
        self.assertEqual(simulator.state.task_status, "emergency_stopped")
        self.assertLess(wall, 0.5, f"安全快路径耗时 {wall:.3f}s，说明它没走模型")
        receipt = result.checkpoint.observations[0]["result"]
        self.assertEqual(receipt["status"], FlightControlStatus.ACCEPTED.value)

    def test_emergency_stop_does_not_require_control_ownership(self):
        """急停恰恰是「控制权不在自己手上」时最需要，不能要求先握手。"""
        simulator = MemoryFlightControlSimulator(binding())
        registry = build_full_control_tool_registry(simulator)
        self.assertIsNone(simulator.state.control_owner)
        loop = BoundedAgentLoop(SafetyFastPathDecider(ExplodingDecider()), registry, max_steps=6)
        result = emergency_stop_until_settled(loop, "急停")
        self.assertIs(result.status, LoopStatus.FINAL)
        self.assertEqual(simulator.state.task_status, "emergency_stopped")
        self.assertEqual(result.checkpoint.observations[0]["result"]["status"],
                         FlightControlStatus.ACCEPTED.value)

    def test_return_home_still_requires_control_ownership(self):
        # 返航会驱动自主飞行，与他人操控可能冲突，保持要求控制权。
        simulator = MemoryFlightControlSimulator(binding())
        registry = build_full_control_tool_registry(simulator)
        loop = BoundedAgentLoop(SafetyFastPathDecider(ExplodingDecider()), registry, max_steps=6)
        result = emergency_stop_until_settled(loop, "返航")
        receipt = result.checkpoint.observations[0]["result"]
        self.assertEqual(receipt["status"], FlightControlStatus.REJECTED.value)
        self.assertEqual(receipt["error_code"], "CONTROL_NOT_OWNED")

    def test_oversized_or_bad_safety_arguments_never_reach_the_simulator(self):
        # 安全短语不带参数，schema 里 additionalProperties=False；
        # 即使有人伪造参数也进不去。这里确认正常路径下参数为空。
        simulator = MemoryFlightControlSimulator(binding())
        registry = build_full_control_tool_registry(simulator)
        loop = BoundedAgentLoop(SafetyFastPathDecider(ExplodingDecider()), registry, max_steps=6)
        result = emergency_stop_until_settled(loop, "返航")
        self.assertIs(result.status, LoopStatus.FINAL)
        self.assertEqual(result.checkpoint.executed_tool_calls[0]["arguments"], {})

    def test_ollama_decider_still_used_for_normal_commands(self):
        # 快路径不改变普通命令的路径：仍然走 OllamaDecider。
        # 传输层必须注入：默认传输层会真的去连模型服务器。这条测试原先依赖
        # 「本机连不上」才通过——服务器可达时它会拿到真实决策并失败，
        # 也就是一条会随环境变绿的假测试。注入后既完全离线，又直接断言
        # 「普通命令确实进了模型传输层」，比只看异常更强。
        calls: list[str] = []

        def offline_transport(url, payload, timeout):
            calls.append(url)
            raise ValueError("model server is unreachable")

        decider = SafetyFastPathDecider(OllamaDecider(model="m", timeout=1.0,
                                                      transport=offline_transport))
        from lingxi_flight_control.models import AgentCheckpoint
        checkpoint = AgentCheckpoint(user_input="向前飞10米", context={}, messages=[],
                                     tool_calls=[], observations=[])
        with self.assertRaises(ValueError):       # 传输层失败 → 交由循环 fail-closed
            decider.decide("向前飞10米", checkpoint, cockpit_tools())
        # 传输层异常不在重试范围内（重试只覆盖空响应），所以恰好一次调用。
        self.assertEqual(len(calls), 1, "普通命令必须走到模型传输层")
        self.assertTrue(calls[0].endswith("/api/chat"))


if __name__ == "__main__":
    unittest.main()
