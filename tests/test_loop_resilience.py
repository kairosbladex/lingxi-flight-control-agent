"""循环韧性与步数计费：确认轮次不占步数、参数校验失败可自纠。

这两条都是实测出来的高频问题：
- 默认 max_steps=8 时，4 个动作的复合指令执行完就 max_steps_exceeded，最终答复发不出来；
- 参数写错（越界 / 缺必填 / 多余字段）会让整个 run 直接 tool_error，模型没机会修正。
"""

from __future__ import annotations

import unittest

from lingxi_flight_control import (
    AgentCheckpoint,
    BoundedAgentLoop,
    DecisionKind,
    LoopStatus,
    ModelDecision,
    ToolCall,
    ToolDefinition,
    ToolParameter,
    ToolRegistry,
    ToolSchema,
)


def definition(name: str = "read_value", handler=None, *, side_effect: bool = False) -> ToolDefinition:
    return ToolDefinition(
        name, "测试工具",
        ToolSchema({"value": ToolParameter("integer", minimum=0, maximum=10)}, ("value",)),
        handler or (lambda args: {"value": args["value"]}),
        side_effect=side_effect,
    )


def registry(handler=None, *, side_effect: bool = False) -> ToolRegistry:
    return ToolRegistry({"read_value": definition(handler=handler, side_effect=side_effect)})


def tool(value: int, *, name: str = "read_value") -> ModelDecision:
    return ModelDecision(DecisionKind.TOOL_CALL, tool_call=ToolCall(name, {"value": value}))


def final(message: str = "完成") -> ModelDecision:
    return ModelDecision(DecisionKind.FINAL, message=message)


class ScriptedDecider:
    """按脚本返回决定；待确认的调用在未执行前会被重发。

    确认被要求后循环会重新调用 `decide()`，模型必须重发**同一个**工具调用，
    `confirmation_binding` 才匹配得上、动作才会执行。真实模型遵循同一契约。

    判定「还没执行」只能看观察数：确认轮次会让 `tool_calls` 重复增长，靠列表长度无法判断。
    """

    def __init__(self, decisions):
        self._decisions = list(decisions)
        self.seen = []
        self._pending = None
        self._pending_at = None

    def decide(self, _message, checkpoint, _tools):
        self.seen.append([dict(o) for o in checkpoint.observations])
        observed = len(checkpoint.observations)
        if self._pending is not None and observed == self._pending_at:
            return self._pending
        self._pending = None
        if not self._decisions:
            return final()
        decision = self._decisions.pop(0)
        if decision.tool_call is not None:
            self._pending = decision
            self._pending_at = observed
        return decision


def drive(loop, command: str = "测试", *, rounds: int = 40, confirm: bool = True):
    """反复以 confirmed=True 续跑，直到不再是澄清。"""
    checkpoint = None
    result = None
    for _ in range(rounds):
        result = loop.run(command, checkpoint=checkpoint, confirmed=confirm and checkpoint is not None)
        checkpoint = result.checkpoint
        if result.status is not LoopStatus.CLARIFICATION:
            return result
    raise AssertionError("循环没有收敛：可能是确认轮次没有上限")


class StepAccountingTests(unittest.TestCase):
    """确认是用户授权握手，不该算作 Agent 的一步。"""

    def test_confirmation_round_trips_do_not_consume_steps(self):
        calls = []
        actions = 4
        script = [tool(i, name="read_value") for i in range(1, actions + 1)] + [final()]
        loop = BoundedAgentLoop(
            ScriptedDecider(script),
            registry(lambda args: calls.append(args) or {"ok": True}, side_effect=True),
            max_steps=actions + 1)          # 只够「每个动作一步」+ 收尾
        result = drive(loop)
        self.assertIs(result.status, LoopStatus.FINAL)
        self.assertEqual(len(calls), actions)
        self.assertLessEqual(result.checkpoint.step_count, actions + 1)

    def test_max_steps_still_caps_executed_actions(self):
        calls = []
        script = [tool(i) for i in range(1, 7)] + [final()]
        loop = BoundedAgentLoop(
            ScriptedDecider(script),
            registry(lambda args: calls.append(args) or {"ok": True}, side_effect=True),
            max_steps=2)
        result = drive(loop)
        self.assertIs(result.status, LoopStatus.MAX_STEPS_EXCEEDED)
        self.assertEqual(len(calls), 2)     # 上限依然有效

    def test_max_steps_still_caps_a_decider_that_never_stops(self):
        class NeverStops:
            def decide(self, *_):
                return tool(1)

        result = BoundedAgentLoop(NeverStops(), registry(), max_steps=3).run("测试")
        self.assertIs(result.status, LoopStatus.MAX_STEPS_EXCEEDED)
        self.assertEqual(result.checkpoint.step_count, 3)

    def test_runaway_confirmation_ping_pong_is_bounded(self):
        """模型每次换来换去、永远不执行同一个调用时，必须被上限截停而不是挂死。"""
        class PingPong:
            def __init__(self):
                self.n = 0

            def decide(self, *_):
                self.n += 1
                return tool(1 if self.n % 2 else 2)

        calls = []
        loop = BoundedAgentLoop(
            PingPong(),
            registry(lambda args: calls.append(args) or {"ok": True}, side_effect=True),
            max_steps=3)
        result = drive(loop)
        self.assertIs(result.status, LoopStatus.MAX_STEPS_EXCEEDED)
        self.assertEqual(calls, [])          # 从未获得过匹配的确认


class ValidationRecoveryTests(unittest.TestCase):
    """参数校验失败应回喂模型，而不是让整个 run 死掉。"""

    def test_validation_error_is_fed_back_and_the_model_corrects(self):
        calls = []
        script = [tool(99), tool(5), final("改成5了")]
        decider = ScriptedDecider(script)
        loop = BoundedAgentLoop(decider, registry(lambda args: calls.append(args) or {"ok": True}))
        result = drive(loop)
        self.assertIs(result.status, LoopStatus.FINAL)
        self.assertEqual(calls, [{"value": 5}])
        # 模型在下一次决策时看得到错误
        self.assertIn("error", decider.seen[1][0])
        self.assertIn("maximum", decider.seen[1][0]["error"]["message"])

    def test_validation_error_is_not_recorded_as_executed(self):
        script = [tool(99), tool(5), final()]
        loop = BoundedAgentLoop(ScriptedDecider(script), registry())
        result = drive(loop)
        self.assertIs(result.status, LoopStatus.FINAL)
        executed = [c["arguments"] for c in result.checkpoint.executed_tool_calls]
        self.assertEqual(executed, [{"value": 5}])
        self.assertNotIn({"value": 99}, executed)

    def test_repeated_invalid_calls_terminate_with_the_error_surfaced(self):
        class AlwaysBad:
            def decide(self, *_):
                return tool(99)

        result = BoundedAgentLoop(AlwaysBad(), registry(), max_steps=3).run("测试")
        self.assertIs(result.status, LoopStatus.MAX_STEPS_EXCEEDED)
        self.assertIsNotNone(result.error)
        self.assertIn("maximum", result.error["message"])

    def test_validation_failure_disarms_a_pending_confirmation(self):
        """一次失败打断授权链，旧的确认不能继续生效。"""
        calls = []

        class ChangesMind:
            """第一次给合法参数，用户放行后改口给越界参数，然后又改回合法参数。"""

            def __init__(self):
                self.decisions = [tool(1), tool(99), tool(1), final()]

            def decide(self, *_):
                return self.decisions.pop(0) if self.decisions else final()

        loop = BoundedAgentLoop(
            ChangesMind(),
            registry(lambda args: calls.append(args) or {"ok": True}, side_effect=True),
            max_steps=6)

        first = loop.run("测试")
        self.assertIs(first.status, LoopStatus.CLARIFICATION)      # 待确认 value=1
        self.assertEqual(calls, [])

        # 用户放行后模型改口给越界参数 → 校验失败并打断授权；
        # 随后模型又说回 value=1，此时必须重新要确认而不是直接执行。
        bad = loop.run("测试", checkpoint=first.checkpoint, confirmed=True)
        self.assertIs(bad.status, LoopStatus.CLARIFICATION)
        self.assertFalse(bad.checkpoint.confirmed)
        self.assertEqual(calls, [])
        self.assertEqual(bad.checkpoint.executed_tool_calls, [])

    def test_unknown_tool_still_fails_closed(self):
        class Unknown:
            def decide(self, *_):
                return ModelDecision(DecisionKind.TOOL_CALL, tool_call=ToolCall("missing", {}))

        result = BoundedAgentLoop(Unknown(), ToolRegistry()).run("测试")
        self.assertIs(result.status, LoopStatus.TOOL_ERROR)
        self.assertIn("unknown tool", result.error["message"])

    def test_handler_exception_still_fails_closed_by_default(self):
        class Explodes:
            def decide(self, *_):
                return tool(1)

        result = BoundedAgentLoop(Explodes(), registry(lambda _: 1 / 0)).run("测试")
        self.assertIs(result.status, LoopStatus.TOOL_ERROR)
        self.assertEqual(result.error["code"], "tool_exception")

    def test_unsafe_model_output_still_fails_closed(self):
        class Unsafe:
            def decide(self, *_):
                return ModelDecision(DecisionKind.FINAL, message="MQTT payload 执行成功")

        result = BoundedAgentLoop(Unsafe(), registry()).run("测试")
        self.assertIs(result.status, LoopStatus.TOOL_ERROR)

    def test_unsafe_tool_output_still_fails_closed(self):
        result = BoundedAgentLoop(ScriptedDecider([tool(1)]),
                                  registry(lambda _: {"mqtt_payload": "x"})).run("测试")
        self.assertIs(result.status, LoopStatus.TOOL_ERROR)


class ReplayTests(unittest.TestCase):
    """失败尝试也必须能被正确回放，模型才知道自己哪里错了。"""

    def test_every_observation_carries_its_own_tool_name_and_arguments(self):
        script = [tool(99), tool(5), final()]
        loop = BoundedAgentLoop(ScriptedDecider(script), registry())
        result = drive(loop)
        observations = result.checkpoint.observations
        self.assertEqual([o["tool"] for o in observations], ["read_value", "read_value"])
        self.assertEqual([o["arguments"] for o in observations], [{"value": 99}, {"value": 5}])
        self.assertIn("error", observations[0])
        self.assertIn("result", observations[1])

    def test_ollama_replay_pairs_each_observation_with_its_own_call(self):
        from lingxi_flight_control.ollama_decider import OllamaDecider

        script = [tool(99), tool(5), final()]
        loop = BoundedAgentLoop(ScriptedDecider(script), registry())
        result = drive(loop)
        messages = OllamaDecider(transport=lambda *_: {}).messages(
            "测试", result.checkpoint)
        pairs = [(messages[i]["tool_calls"][0]["function"]["arguments"],
                  "error" if "error" in messages[i + 1]["content"] else "result")
                 for i in range(2, len(messages), 2)]
        self.assertEqual(pairs, [({"value": 99}, "error"), ({"value": 5}, "result")])

    def test_checkpoint_round_trip_keeps_the_new_counters(self):
        loop = BoundedAgentLoop(ScriptedDecider([tool(1), final()]),
                                registry(side_effect=True))
        result = drive(loop)
        restored = AgentCheckpoint.from_dict(result.checkpoint.to_dict())
        self.assertEqual(restored.to_dict(), result.checkpoint.to_dict())


if __name__ == "__main__":
    unittest.main()
