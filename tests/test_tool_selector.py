"""工具检索层测试：确定性规则、未命中回退全量、绝不静默减少能力。

检索层的失败方向是不对称的：多给一个工具只是多花 token，少给一个工具会让模型
**结构上无法完成任务**。所以这里的断言集中在「不会少给」：安全动作永远在候选里、
前置动作（控制权）永远在候选里、已经用过的工具永远在候选里、任何不确定都回退全量。
"""

from __future__ import annotations

import importlib.util
import json
import os
import unittest

from lingxi_flight_control import (
    BoundedAgentLoop,
    DecisionKind,
    LoopStatus,
    MemoryFlightControlSimulator,
    ModelDecision,
    OllamaDecider,
    RuntimeBinding,
    SafetyFastPathDecider,
    ToolCall,
    ToolRegistry,
    action_tool_definitions,
    build_full_control_tool_registry,
)
from lingxi_flight_control.models import AgentCheckpoint
from lingxi_flight_control.tool_selector import (
    ALWAYS_INCLUDED,
    DOMAIN_KEYWORDS,
    SELECTION_MODES,
    RuleBasedToolSelector,
    SelectionResult,
    default_domain_of,
)

BENCH_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "tools", "bench", "tool_selection.py")


def binding() -> RuntimeBinding:
    return RuntimeBinding("actor-1", "project-1", "hangar-1", "drone-1", "batch-1", 1)


def cockpit_definitions():
    return build_full_control_tool_registry(MemoryFlightControlSimulator(binding())).definitions()


def checkpoint(user_input="测试", **overrides) -> AgentCheckpoint:
    payload = dict(user_input=user_input, context={}, messages=[], tool_calls=[], observations=[])
    payload.update(overrides)
    return AgentCheckpoint(**payload)


def checkpoint_with(*, executed=(), attempted=(), observed=()):
    return checkpoint(
        tool_calls=[{"name": name, "arguments": {}} for name in attempted],
        executed_tool_calls=[{"name": name, "arguments": {}} for name in executed],
        observations=[{"tool": name, "arguments": {}, "result": {"status": "accepted"}}
                      for name in observed],
    )


def load_bench():
    spec = importlib.util.spec_from_file_location("tool_selection_bench", BENCH_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SelectorFallbackTests(unittest.TestCase):
    def setUp(self):
        self.selector = RuleBasedToolSelector()
        self.definitions = cockpit_definitions()
        self.all_names = tuple(definition.name for definition in self.definitions)

    def test_no_keyword_match_falls_back_to_the_full_set(self):
        for message in ("帮我看看", "继续", "好的", "确认", "", "   "):
            with self.subTest(message=message):
                result = self.selector.select(message, self.definitions)
                self.assertEqual(result.mode, "full")
                self.assertEqual(result.names, self.all_names)
                self.assertEqual(len(result.names), len(self.definitions))

    def test_unknown_tool_shapes_do_not_break_the_fallback(self):
        """认不出的定义形状 → 回退全量（原样带走），而不是抛错或静默丢工具。"""
        definitions = [{"name": "read_state", "description": "读取当前飞行状态"},
                       {"name": "mystery_tool", "description": ""}]
        result = self.selector.select("帮我看看", definitions)
        self.assertEqual(result.mode, "full")
        self.assertEqual(result.names, ("read_state", "mystery_tool"))

    def test_result_names_are_always_a_subset_of_the_input(self):
        for message in ("拍张照片", "帮我看看", "向前飞10米", "降落"):
            with self.subTest(message=message):
                result = self.selector.select(message, self.definitions)
                self.assertTrue(set(result.names) <= set(self.all_names))
                self.assertEqual(len(result.names), len(set(result.names)), "不能重复")

    def test_selection_is_deterministic(self):
        first = self.selector.select("接管控制权并向前飞10米再拍张照片", self.definitions)
        second = self.selector.select("接管控制权并向前飞10米再拍张照片", self.definitions)
        self.assertEqual(first.names, second.names)
        self.assertEqual((first.mode, first.domains, first.reason),
                         (second.mode, second.domains, second.reason))


class SelectorSubsetTests(unittest.TestCase):
    def setUp(self):
        self.selector = RuleBasedToolSelector()
        self.definitions = cockpit_definitions()

    def test_state_query_uses_a_small_read_only_candidate_set(self):
        result = self.selector.select("读取当前状态", self.definitions)
        self.assertEqual(result.mode, "retrieved")
        self.assertEqual(result.names, ("emergency_stop", "land", "return_home", "read_state"))
        self.assertIn("read_state", result.names)

    def test_min_candidates_must_be_positive(self):
        for bad in (0, -1, True, 1.5):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    RuleBasedToolSelector(min_candidates=bad)

    def test_selection_is_stable_across_the_steps_of_one_task(self):
        """同一任务内工具面必须稳定。

        工具段属于 prompt 前缀：只要它在任务中途变化，KV cache 就失效，每一步都要重付
        一次预填充——这正是 `docs/model-selection.md`「不要做工具分组渐进下发」那条结论的
        代价。检索只依赖 user 消息（以及已经下发给模型的工具），因此任务内稳定。
        """
        message = "接管控制权，然后向前飞10米，再拍张照片"
        first = self.selector.select(message, self.definitions)
        state = checkpoint_with(executed=("control_acquire", "move_forward", "camera_photo"))
        later = self.selector.select(message, self.definitions, checkpoint=state)
        self.assertEqual(first.names, later.names)
        self.assertEqual(first.mode, later.mode)

    def test_matched_domain_returns_a_strict_subset(self):
        result = self.selector.select("拍张照片", self.definitions)
        self.assertEqual(result.mode, "retrieved")
        self.assertIn("camera_photo", result.names)
        self.assertLess(len(result.names), len(self.definitions))
        self.assertIn("camera", result.domains)

    def test_safety_tools_are_never_retrieved_away(self):
        """急停/返航/降落与状态读取是检索层的安全底线，任何消息都必须保留。"""
        for message in ("拍张照片", "云台向上", "打开喊话器", "向前飞10米"):
            with self.subTest(message=message):
                result = self.selector.select(message, self.definitions)
                for name in ALWAYS_INCLUDED:
                    self.assertIn(name, result.names, f"{message} 丢了安全工具 {name}")

    def test_prerequisite_control_acquire_is_added_for_mutating_domains(self):
        """驾驶舱要求先持有控制权才能改变状态；检索层不能把前置动作检索掉。"""
        for message, tool in (("向前飞10米", "move_forward"),
                              ("拍张照片", "camera_photo"),
                              ("上传航线", "upload_route"),
                              ("打开喊话器", "payload_megaphone")):
            with self.subTest(message=message):
                result = self.selector.select(message, self.definitions)
                self.assertIn(tool, result.names)
                self.assertIn("control_acquire", result.names)

    def test_already_executed_tools_survive_a_narrow_selection(self):
        state = checkpoint_with(executed=("payload_dropper",))
        result = self.selector.select("拍张照片", self.definitions, checkpoint=state)
        self.assertIn("camera_photo", result.names)
        self.assertIn("payload_dropper", result.names, "已执行动作不能被检索掉")

    def test_attempted_and_observed_tools_survive_a_narrow_selection(self):
        """确认握手中途续跑时，待确认的工具与已回放的观察工具都必须还在候选里。"""
        state = checkpoint_with(attempted=("payload_megaphone",), observed=("set_move_speed",))
        result = self.selector.select("拍张照片", self.definitions, checkpoint=state)
        self.assertIn("payload_megaphone", result.names)
        self.assertIn("set_move_speed", result.names)

    def test_multiple_domains_in_one_command_are_all_kept(self):
        result = self.selector.select("接管控制权，然后向前飞10米，再拍张照片", self.definitions)
        for name in ("control_acquire", "move_forward", "camera_photo"):
            self.assertIn(name, result.names)
        self.assertEqual(result.mode, "retrieved")

    def test_read_state_is_reachable_even_for_a_narrow_command(self):
        result = self.selector.select("向前飞10米", self.definitions)
        self.assertIn("read_state", result.names)

    def test_selection_modes_and_domain_table_are_documented_constants(self):
        self.assertEqual(SELECTION_MODES, ("full", "retrieved"))
        self.assertTrue(set(ALWAYS_INCLUDED) <= {definition.name for definition in self.definitions})
        for domain, words in DOMAIN_KEYWORDS.items():
            with self.subTest(domain=domain):
                self.assertTrue(words, f"{domain} 的词表不能为空")

    def test_domains_come_from_action_specs_not_the_tool_name(self):
        self.assertEqual(default_domain_of("camera_photo"), "camera")
        self.assertEqual(default_domain_of("move_forward"), "flight")
        self.assertEqual(default_domain_of("read_state"), "state")
        self.assertEqual(default_domain_of("not_a_tool"), "")

    def test_works_on_plain_tool_dicts_too(self):
        definitions = action_tool_definitions()
        result = self.selector.select("拍张照片", definitions)
        self.assertIn("camera_photo", result.names)
        self.assertLess(len(result.names), len(definitions))


class DeciderIntegrationTests(unittest.TestCase):
    class RecordingSelector:
        def __init__(self):
            self.calls = []

        def select(self, user_message, definitions, *, checkpoint=None):
            self.calls.append(user_message)
            return RuleBasedToolSelector().select(user_message, definitions, checkpoint=checkpoint)

    class ExplodingSelector:
        def __init__(self):
            self.calls = 0

        def select(self, user_message, definitions, *, checkpoint=None):
            self.calls += 1
            raise RuntimeError("selector bug")

    def test_decider_sends_only_the_selected_tools(self):
        selector = self.RecordingSelector()
        decider = OllamaDecider(model="m", tool_selector=selector, transport=lambda *_: {})
        definitions = cockpit_definitions()
        body = decider.payload("拍张照片", checkpoint(), definitions)

        names = [tool["function"]["name"] for tool in body["tools"]]
        self.assertLess(len(names), len(definitions))
        self.assertIn("camera_photo", names)
        self.assertEqual(selector.calls, ["拍张照片"])
        self.assertEqual(decider.last_tool_selection, "retrieved")
        self.assertEqual(decider.last_selected_tools, len(names))
        # 预算读数必须按压缩后的工具面算，否则「省了多少」这个数字是假的。
        self.assertEqual(decider.last_prompt_estimate,
                         decider.estimate_prompt_tokens(body["messages"],
                                                        tuple(d for d in definitions
                                                              if d.name in set(names))))
        self.assertLess(decider.last_prompt_estimate,
                        OllamaDecider(model="m", transport=lambda *_: {}).estimate_prompt_tokens(
                            body["messages"], definitions))

    def test_full_fallback_reports_the_full_set(self):
        decider = OllamaDecider(model="m", tool_selector=RuleBasedToolSelector(),
                                transport=lambda *_: {})
        definitions = cockpit_definitions()
        body = decider.payload("帮我看看", checkpoint(), definitions)
        self.assertEqual(len(body["tools"]), len(definitions))
        self.assertEqual(decider.last_tool_selection, "full")
        self.assertEqual(decider.last_selected_tools, len(definitions))

    def test_selector_failure_falls_back_to_the_full_set_and_never_fails_the_run(self):
        """检索层有 bug 时只能退化成「全量下发」，绝不能变成「工具变少」或直接失败。"""
        selector = self.ExplodingSelector()
        decider = OllamaDecider(model="m", tool_selector=selector, transport=lambda *_: {})
        definitions = cockpit_definitions()
        body = decider.payload("拍张照片", checkpoint(), definitions)
        self.assertEqual(len(body["tools"]), len(definitions))
        self.assertEqual(selector.calls, 1)
        self.assertEqual(decider.selector_errors, 1)
        self.assertEqual(decider.last_tool_selection, "full")

    def test_decider_without_a_selector_behaves_exactly_as_before(self):
        decider = OllamaDecider(model="m", transport=lambda *_: {})
        definitions = cockpit_definitions()
        body = decider.payload("拍张照片", checkpoint(), definitions)
        self.assertEqual(len(body["tools"]), len(definitions))
        self.assertEqual(decider.last_tool_selection, "full")
        self.assertEqual(decider.last_selected_tools, len(definitions))
        self.assertEqual(decider.selector_errors, 0)

    def test_safety_fast_path_never_reaches_the_selector(self):
        selector = self.ExplodingSelector()
        decider = SafetyFastPathDecider(
            OllamaDecider(model="m", tool_selector=selector, transport=lambda *_: {}))
        decision = decider.decide("急停", checkpoint(user_input="急停"), cockpit_definitions())
        self.assertIs(decision.kind, DecisionKind.TOOL_CALL)
        self.assertEqual(decision.tool_call.name, "emergency_stop")
        self.assertEqual(selector.calls, 0, "安全快路径不经过检索层")
        self.assertEqual(decider.safety_hits, 1)

    def test_unknown_tool_still_fails_closed_with_a_selector(self):
        class Unknown:
            def decide(self, *_):
                return ModelDecision(DecisionKind.TOOL_CALL, tool_call=ToolCall("missing", {}))

        loop = BoundedAgentLoop(Unknown(), ToolRegistry(),
                                telemetry=OllamaDecider(model="m", transport=lambda *_: {}).telemetry)
        result = loop.run("查询")
        self.assertIs(result.status, LoopStatus.TOOL_ERROR)
        self.assertEqual(result.error["code"], "tool_error")

    def test_selection_does_not_change_registry_validation(self):
        """检索只影响下发给模型的候选面；schema 校验与 fail-closed 一个字没动。"""
        definitions = cockpit_definitions()
        registry = build_full_control_tool_registry(MemoryFlightControlSimulator(binding()))
        self.assertEqual(len(definitions), len(registry.definitions()))
        errors = registry.validate(ToolCall("move_forward", {"distance_m": 5000}))
        self.assertEqual(errors, [])


class BenchCoverageTests(unittest.TestCase):
    """确定性护栏：46 条 benchmark 用例的期望工具必须都在检索候选里。

    这条不需要模型：只要检索层漏掉了某条用例的期望工具，这里立刻红，
    不至于等到花钱跑完 46 次模型调用才发现准确率掉了。
    """

    def test_every_benchmark_case_keeps_its_expected_tool(self):
        bench = load_bench()
        selector = RuleBasedToolSelector()
        descriptors = [(name, description) for name, (description, _, _) in bench.SPEC.items()]
        missing = []
        results = []
        for text, expected in bench.CASES:
            result = selector.select(text, descriptors)
            results.append(result)
            if expected not in result.names:
                missing.append((text, expected))
        self.assertEqual(missing, [], f"检索层漏掉了这些用例的期望工具: {missing}")
        retrieved = [len(result.names) for result in results if result.mode == "retrieved"]
        self.assertTrue(retrieved, "46 条用例里至少要有一条真的缩小了工具面")
        self.assertLess(max(retrieved), len(bench.SPEC))
        self.assertEqual(len(bench.CASES), 46, "基准用例数变了，准确率对比就不可比了")

    def test_benchmark_subset_is_materially_smaller(self):
        bench = load_bench()
        selector = RuleBasedToolSelector()
        descriptors = [(name, description) for name, (description, _, _) in bench.SPEC.items()]
        results = [selector.select(text, descriptors) for text, _ in bench.CASES]
        sizes = [len(result.names) for result in results]
        retrieved = [result for result in results if result.mode == "retrieved"]
        self.assertGreaterEqual(len(retrieved), len(bench.CASES) - 3,
                                "回退全量的用例不应超过 3 条（回退是兜底，不是常态）")
        self.assertLess(sum(sizes) / len(sizes), len(bench.SPEC) * 0.8,
                        f"平均候选面 {sum(sizes) / len(sizes):.1f} 相对全量 {len(bench.SPEC)} 不够小")


class ProductionFactoryWiringTests(unittest.TestCase):
    """生产入口默认接入检索；计划专用模型仍由独立注册表负责。"""

    def test_cockpit_entrypoints_construct_deciders_with_selector(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for relative in ('tools/cockpit_server.py', 'tools/platform_cockpit_server.py',
                         'tools/zhuoyi_cockpit_server.py'):
            with self.subTest(relative=relative):
                with open(os.path.join(root, relative), encoding='utf-8') as handle:
                    source = handle.read()
                self.assertIn('RuleBasedToolSelector', source)
                self.assertIn('tool_selector=', source)


if __name__ == "__main__":
    unittest.main()
