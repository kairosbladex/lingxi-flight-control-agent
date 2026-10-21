"""Ollama 决策适配器的离线测试：注入假传输层，不访问网络。"""

from __future__ import annotations

import io
import json
import unittest

from lingxi_flight_control import (
    BoundedAgentLoop,
    DecisionKind,
    ExecutionContext,
    MemoryFlightSimulator,
    ToolDefinition,
    ToolParameter,
    ToolSchema,
)
from lingxi_flight_control.models import AgentCheckpoint, parameter_json_schema
from lingxi_flight_control.ollama_decider import (
    DEFAULT_KEEP_RECENT_OBSERVATIONS,
    DEFAULT_NUM_CTX,
    DEFAULT_OUTPUT_RESERVE_TOKENS,
    DEFAULT_SYSTEM_PROMPT,
    OllamaDecider,
    PromptBudgetExceeded,
    build_tools_payload,
    generation_telemetry,
    http_transport,
    tool_payload,
)


class FakeTransport:
    """按脚本回放响应的传输层，并记录每次请求体。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, url, payload, timeout):
        self.calls.append({"url": url, "payload": json.loads(json.dumps(payload)), "timeout": timeout})
        if not self._responses:
            raise AssertionError("unexpected extra model call")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def tool_call_response(name, arguments):
    return {"message": {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": name, "arguments": arguments}}]}}


def message_response(content):
    return {"message": {"role": "assistant", "content": content}}


def checkpoint(**overrides):
    payload = dict(user_input="向前飞10米", context={}, messages=[], tool_calls=[], observations=[])
    payload.update(overrides)
    return AgentCheckpoint(**payload)


def budget_tools(count=2):
    """小工具集：预算测试只关心消息长度，不该让工具契约本身吃掉整个预算。"""
    return tuple(
        ToolDefinition(
            name=f"action_{index}",
            description="紧凑的测试动作",
            schema=ToolSchema(
                properties={"value": ToolParameter("number", minimum=0, maximum=10)},
                required=("value",)),
            handler=None)
        for index in range(count)
    )


def bulk_observations(count, *, blob_chars=0):
    """造 count 条互不重名、体积可控的观察结果，便于断言「哪几条被省略」。"""
    return [
        {
            "tool": f"action_{index:02d}",
            "arguments": {"value": index},
            "result": {"status": "accepted", "action": f"action_{index:02d}",
                       "blob": "x" * blob_chars},
        }
        for index in range(count)
    ]


class SchemaConversionTests(unittest.TestCase):
    def test_parameter_schema_maps_bounds_and_enums(self):
        schema = parameter_json_schema(ToolParameter(
            "number", minimum=0, maximum=100, exclusive_minimum=True))
        self.assertEqual(schema, {"type": "number", "minimum": 0, "maximum": 100, "exclusiveMinimum": True})
        enum = parameter_json_schema(ToolParameter("string", enum=("forward", "back"), min_length=1))
        self.assertEqual(enum, {"type": "string", "enum": ["forward", "back"], "minLength": 1})

    def test_tool_payload_matches_function_calling_shape(self):
        definition = ToolDefinition(
            name="relative_move", description="移动",
            schema=ToolSchema(
                properties={
                    "direction": ToolParameter("string", enum=("forward",)),
                    "distance_m": ToolParameter("number", minimum=0, maximum=100),
                },
                required=("direction", "distance_m")),
            handler=lambda _: None)
        payload = tool_payload(definition)
        self.assertEqual(payload["type"], "function")
        function = payload["function"]
        self.assertEqual(function["name"], "relative_move")
        self.assertEqual(function["parameters"]["required"], ["direction", "distance_m"])
        self.assertFalse(function["parameters"]["additionalProperties"])
        self.assertEqual(function["parameters"]["properties"]["distance_m"]["maximum"], 100)

    def test_handlers_never_cross_the_model_boundary(self):
        definition = ToolDefinition(
            name="t", description="d", schema=ToolSchema(properties={}), handler=lambda _: 1)
        serialized = json.dumps(build_tools_payload([definition]))
        self.assertNotIn("handler", serialized)

    def test_non_definition_is_rejected(self):
        with self.assertRaises(ValueError):
            build_tools_payload([{"name": "x"}])


class DecideTests(unittest.TestCase):
    def make(self, response, **kwargs):
        transport = FakeTransport([response])
        decider = OllamaDecider(model="test-model", transport=transport, **kwargs)
        return decider, transport

    def test_native_tool_call_becomes_tool_decision(self):
        decider, _ = self.make(tool_call_response(
            "relative_move", {"direction": "forward", "distance_m": 10, "idempotency_key": "k"}))
        decision = decider.decide("向前飞10米", checkpoint(), ())
        self.assertEqual(decision.kind, DecisionKind.TOOL_CALL)
        self.assertEqual(decision.tool_call.name, "relative_move")
        self.assertEqual(decision.tool_call.arguments["distance_m"], 10)
        self.assertEqual(decision.message, "")
        decision.validate()

    def test_json_string_arguments_are_accepted(self):
        decider, _ = self.make(tool_call_response(
            "relative_move", json.dumps({"direction": "up", "distance_m": 3, "idempotency_key": "k"})))
        decision = decider.decide("上升3米", checkpoint(), ())
        self.assertEqual(decision.tool_call.arguments["direction"], "up")

    def test_plain_message_becomes_final(self):
        decider, _ = self.make(message_response("已收到，准备执行。"))
        decision = decider.decide("你好", checkpoint(), ())
        self.assertEqual(decision.kind, DecisionKind.FINAL)
        self.assertIsNone(decision.tool_call)

    def test_question_becomes_clarification(self):
        decider, _ = self.make(message_response("请问您要做正射影像还是倾斜摄影的测绘？"))
        decision = decider.decide("做测绘任务", checkpoint(), ())
        self.assertEqual(decision.kind, DecisionKind.CLARIFY)

    def test_clarify_detector_is_overridable(self):
        decider, _ = self.make(message_response("随便一句话"), clarify_detector=lambda _: True)
        self.assertEqual(decider.decide("x", checkpoint(), ()).kind, DecisionKind.CLARIFY)

    def test_tool_call_wins_over_prose_in_same_turn(self):
        response = {"message": {"role": "assistant", "content": "我这就执行",
                                "tool_calls": [{"function": {"name": "hover",
                                                             "arguments": {"duration_s": 5,
                                                                           "idempotency_key": "k"}}}]}}
        decider, _ = self.make(response)
        decision = decider.decide("悬停5秒", checkpoint(), ())
        self.assertEqual(decision.kind, DecisionKind.TOOL_CALL)
        self.assertEqual(decision.message, "")

    def test_empty_response_fails_closed_after_bounded_retries(self):
        empty = {"message": {"role": "assistant", "content": "   "}}
        decider, transport = self.make(empty)
        transport._responses.append(empty)          # 重试一次仍然为空
        with self.assertRaises(ValueError) as ctx:
            decider.decide("x", checkpoint(), ())
        self.assertIn("after 2 attempts", str(ctx.exception))
        self.assertEqual(decider.empty_responses, 2)
        self.assertEqual(len(transport.calls), 2)

    def test_empty_response_is_retried_and_recovers(self):
        empty = {"message": {"role": "assistant", "content": ""}}
        good = message_response("恢复成功")
        decider, transport = self.make(empty)
        transport._responses.append(good)
        decision = decider.decide("x", checkpoint(), ())
        self.assertEqual(decision.message, "恢复成功")
        self.assertEqual(decider.empty_responses, 1)
        self.assertEqual(len(transport.calls), 2)

    def test_retry_never_duplicates_an_executed_tool_call(self):
        # 空响应时没有任何工具被执行，所以重试的只是决策，不会重复副作用。
        empty = {"message": {"role": "assistant", "content": ""}}
        decider, transport = self.make(empty)
        transport._responses.append(tool_call_response("hover", {"duration_s": 5, "idempotency_key": "k"}))
        decision = decider.decide("悬停5秒", checkpoint(), ())
        self.assertEqual(decision.kind, DecisionKind.TOOL_CALL)
        self.assertEqual(len(transport.calls), 2)

    def test_missing_message_object_is_rejected(self):
        decider, _ = self.make({"done": True})
        with self.assertRaises(ValueError):
            decider.decide("x", checkpoint(), ())

    def test_server_error_field_is_rejected(self):
        decider, _ = self.make({"error": "model not found"})
        with self.assertRaises(ValueError) as ctx:
            decider.decide("x", checkpoint(), ())
        self.assertEqual(str(ctx.exception), "model server returned an error response")
        self.assertIsNone(decider.last_raw_response)

    def test_bad_tool_call_shapes_are_rejected(self):
        for response in (
            {"message": {"tool_calls": [{"function": {"arguments": {}}}]}},
            {"message": {"tool_calls": [{"function": {"name": "x", "arguments": "not json"}}]}},
            {"message": {"tool_calls": ["nope"]}},
            {"message": {"tool_calls": "nope"}},
        ):
            decider, _ = self.make(response)
            with self.assertRaises(ValueError):
                decider.decide("x", checkpoint(), ())


class MessageRebuildTests(unittest.TestCase):
    def test_unexecuted_calls_are_not_replayed(self):
        # 等待确认时 tool_calls 已经记录了调用，但没有任何 observation；
        # 这类前缀调用必须丢弃，否则会构造出 assistant tool_calls 后缺少 tool 结果的非法序列。
        cp = checkpoint(
            tool_calls=[{"name": "hover", "arguments": {"duration_s": 5, "idempotency_key": "k"}}],
            observations=[])
        messages = OllamaDecider(transport=FakeTransport([])).messages("向前飞10米", cp)
        self.assertEqual([m["role"] for m in messages], ["system", "user"])

    def test_executed_turn_round_trips_as_native_messages(self):
        cp = checkpoint(
            executed_tool_calls=[{"name": "relative_move",
                                  "arguments": {"direction": "forward", "distance_m": 10,
                                                "idempotency_key": "k"}}],
            observations=[{"tool": "relative_move",
                           "result": {"status": "accepted", "position": {"x_m": 0, "y_m": 10, "z_m": 0}}}])
        messages = OllamaDecider(transport=FakeTransport([])).messages("向前飞10米", cp)
        self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant", "tool"])
        self.assertEqual(messages[2]["tool_calls"][0]["function"]["name"], "relative_move")
        self.assertEqual(json.loads(messages[3]["content"])["status"], "accepted")

    def test_replay_pairs_each_execution_with_its_own_observation(self):
        """确认过的动作会在 tool_calls 里重复出现，回放只能依据已执行记录，否则名与结果会错位。"""
        cp = checkpoint(
            tool_calls=[{"name": "control_acquire", "arguments": {}},
                        {"name": "control_acquire", "arguments": {}},
                        {"name": "move_forward", "arguments": {"distance_m": 10}},
                        {"name": "move_forward", "arguments": {"distance_m": 10}}],
            executed_tool_calls=[{"name": "control_acquire", "arguments": {}},
                                 {"name": "move_forward", "arguments": {"distance_m": 10}}],
            observations=[{"tool": "control_acquire", "result": {"action": "control_acquire"}},
                          {"tool": "move_forward", "result": {"action": "move_forward"}}])
        messages = OllamaDecider(transport=FakeTransport([])).messages("接管并前进", cp)
        pairs = [(messages[i]["tool_calls"][0]["function"]["name"],
                  json.loads(messages[i + 1]["content"])["action"])
                 for i in range(2, len(messages), 2)]
        self.assertEqual(pairs, [("control_acquire", "control_acquire"),
                                 ("move_forward", "move_forward")])

    def test_tool_errors_are_replayed_as_observations(self):
        cp = checkpoint(
            executed_tool_calls=[{"name": "hover",
                                  "arguments": {"duration_s": 5, "idempotency_key": "k"}}],
            observations=[{"tool": "hover", "error": {"code": "tool_exception", "message": "boom"}}])
        messages = OllamaDecider(transport=FakeTransport([])).messages("悬停5秒", cp)
        self.assertIn("boom", messages[3]["content"])

    def test_payload_carries_options(self):
        decider = OllamaDecider(model="m", transport=FakeTransport([]), temperature=0.2,
                                num_ctx=4096, think=True, keep_alive="10m")
        body = decider.payload("向前飞10米", checkpoint(), ())
        self.assertEqual(body["options"]["temperature"], 0.2)
        self.assertEqual(body["options"]["num_ctx"], 4096)
        self.assertTrue(body["think"])
        self.assertEqual(body["keep_alive"], "10m")
        self.assertFalse(body["stream"])

    def test_think_field_is_omitted_by_default(self):
        # 显式 think=False 会让 qwen3.5 的工具调用完全失效，因此默认不发这个字段。
        decider = OllamaDecider(model="m", transport=FakeTransport([]))
        self.assertNotIn("think", decider.payload("x", checkpoint(), ()))
        self.assertIn("think", OllamaDecider(model="m", transport=FakeTransport([]),
                                             think=True).payload("x", checkpoint(), ()))


class PromptBudgetTests(unittest.TestCase):
    """上下文预算：绝不让服务端静默截断。

    Ollama 在 prompt 超过 ``num_ctx`` 时丢的是**最前面**的内容，也就是 system prompt 里的
    硬性约束，而且不报错。所以 ``OllamaDecider`` 必须在发送前自己算预算、按确定策略压缩。
    """

    #: docs/model-selection.md 实测（238 上的 qwen3.5:9b + Ollama 0.21.0）：
    #: 59 工具 + system prompt = 3525 prompt tokens；
    #: 再叠 20 次 read_state 观察（每次回执 1386 字符）后完整 prompt = 14913 tokens
    #: （带口令标记的复跑为 14987，这里取较小值当下界）。
    MEASURED_HEAD_TOKENS = 3525
    MEASURED_TWENTY_OBSERVATION_TOKENS = 14913
    #: qwen3.5:9b 原生声明 256K；默认窗口不该直接开满去白占 KV cache。
    NATIVE_CONTEXT_TOKENS = 256 * 1024

    def decider(self, **kwargs):
        kwargs.setdefault("num_ctx", 8192)
        return OllamaDecider(model="test-model", transport=FakeTransport([]), **kwargs)

    # ---- 默认值取舍 --------------------------------------------------------

    def test_default_num_ctx_holds_the_measured_loop_without_wasting_kv_cache(self):
        # 留余量：实测的 20 次观察完整对话必须装得下（还要留出生成预算）。
        self.assertGreaterEqual(DEFAULT_NUM_CTX - DEFAULT_OUTPUT_RESERVE_TOKENS,
                                self.MEASURED_TWENTY_OBSERVATION_TOKENS)
        self.assertGreater(DEFAULT_NUM_CTX, 8192)
        # 固定头部（59 工具 + system prompt）只占默认窗口的一小部分，其余留给对话与生成。
        self.assertLess(self.MEASURED_HEAD_TOKENS, DEFAULT_NUM_CTX // 8)
        # 不浪费：远小于原生上限，KV cache 不按 256K 白占显存。
        self.assertLessEqual(DEFAULT_NUM_CTX, self.NATIVE_CONTEXT_TOKENS // 4)
        self.assertEqual(OllamaDecider(model="m", transport=FakeTransport([]))._num_ctx,
                         DEFAULT_NUM_CTX)

    def test_num_ctx_must_be_positive_and_leave_room_for_generation(self):
        for kwargs in ({"num_ctx": 0}, {"num_ctx": -1}, {"num_ctx": None},
                       {"num_ctx": DEFAULT_OUTPUT_RESERVE_TOKENS}):
            with self.assertRaises(ValueError):
                OllamaDecider(model="m", **kwargs)

    # ---- 压缩策略硬性要求 --------------------------------------------------

    def test_over_budget_prompt_keeps_system_prompt_and_current_user_message(self):
        definitions = budget_tools()
        decider = self.decider(num_ctx=4096)                      # 预算 = 4096 - 2048
        cp = checkpoint(observations=bulk_observations(12, blob_chars=4000))
        messages = decider.messages("向前飞10米", cp, definitions)

        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[0]["content"], DEFAULT_SYSTEM_PROMPT)
        self.assertEqual(messages[1], {"role": "user", "content": "向前飞10米"})
        self.assertLessEqual(decider.estimate_prompt_tokens(messages, definitions),
                             decider.prompt_budget_tokens)

    def test_only_the_most_recent_observations_are_replayed(self):
        definitions = budget_tools()
        decider = self.decider(num_ctx=DEFAULT_NUM_CTX)
        observations = [
            {"tool": f"action_{index:02d}", "arguments": {"value": index},
             "result": {"status": "accepted", "blob": f"OMITTED-{index:02d}"}}
            for index in range(DEFAULT_KEEP_RECENT_OBSERVATIONS + 6)
        ]
        messages = decider.messages("向前飞10米", checkpoint(observations=observations), definitions)

        replayed = [m["tool_calls"][0]["function"]["name"] for m in messages if m.get("tool_calls")]
        self.assertEqual(replayed,
                         [o["tool"] for o in observations[-DEFAULT_KEEP_RECENT_OBSERVATIONS:]])
        self.assertEqual(messages[2]["role"], "assistant")
        self.assertEqual(
            messages[2]["content"],
            "（更早的 6 次工具调用已省略："
            + " ".join(f"action_{index:02d}✓" for index in range(6)) + "）")
        # 省略的观察只留摘要；它们的回执内容绝不回放。
        serialized = json.dumps(messages, ensure_ascii=False)
        for index in range(6):
            self.assertNotIn(f"OMITTED-{index:02d}", serialized)

    def test_oversized_single_observation_is_omitted_rather_than_truncated(self):
        definitions = budget_tools()
        decider = self.decider(num_ctx=4096)
        observations = [{"tool": "read_state", "arguments": {},
                         "result": {"blob": "x" * 30000}}]
        messages = decider.messages("向前飞10米", checkpoint(observations=observations), definitions)

        self.assertIn("更早的 1 次工具调用已省略", messages[2]["content"])
        # 超大回执连一个字符都不进 prompt；省略说明里只留工具名与状态。
        self.assertNotIn("x" * 100, json.dumps(messages, ensure_ascii=False))
        self.assertEqual(messages[2]["content"], "（更早的 1 次工具调用已省略：read_state✓）")
        self.assertLessEqual(decider.estimate_prompt_tokens(messages, definitions),
                             decider.prompt_budget_tokens)

    def test_built_prompt_never_exceeds_the_budget(self):
        definitions = budget_tools()
        for num_ctx in (DEFAULT_OUTPUT_RESERVE_TOKENS + 1024, 4096, 8192, DEFAULT_NUM_CTX):
            for count, blob_chars in ((0, 0), (1, 0), (5, 800), (40, 2000), (60, 6000)):
                decider = self.decider(num_ctx=num_ctx)
                cp = checkpoint(observations=bulk_observations(count, blob_chars=blob_chars))
                messages = decider.messages("向前飞10米", cp, definitions)
                self.assertLessEqual(
                    decider.estimate_prompt_tokens(messages, definitions),
                    decider.prompt_budget_tokens,
                    msg=f"num_ctx={num_ctx} observations={count} blob={blob_chars}")

    def test_payload_sends_the_compressed_messages(self):
        definitions = budget_tools()
        decider = self.decider(num_ctx=4096)
        body = decider.payload("向前飞10米", checkpoint(observations=bulk_observations(30)), definitions)
        self.assertIn("已省略", json.dumps(body["messages"], ensure_ascii=False))
        self.assertLessEqual(decider.estimate_prompt_tokens(body["messages"], definitions),
                             decider.prompt_budget_tokens)

    def test_compression_is_deterministic(self):
        definitions = budget_tools()
        cp = checkpoint(observations=bulk_observations(20, blob_chars=200))
        first = self.decider(num_ctx=4096).messages("向前飞10米", cp, definitions)
        second = self.decider(num_ctx=4096).messages("向前飞10米", cp, definitions)
        self.assertEqual(first, second)

    def test_budget_uses_an_injectable_counter(self):
        definitions = budget_tools()
        # 每字符 1 token 的注入计数，比默认字符估算保守得多；压缩必须在它上面同样成立。
        decider = self.decider(num_ctx=4096, token_estimator=lambda text: len(text))
        messages = decider.messages("向前飞10米",
                                    checkpoint(observations=bulk_observations(8)), definitions)
        self.assertLessEqual(decider.estimate_prompt_tokens(messages, definitions),
                             decider.prompt_budget_tokens)

    def test_prompt_that_cannot_fit_even_after_compression_fails_closed(self):
        decider = self.decider(num_ctx=4096, system_prompt="约束" * 4000)
        with self.assertRaises(PromptBudgetExceeded) as ctx:
            decider.messages("向前飞10米", checkpoint(), budget_tools())
        self.assertIn("system prompt", str(ctx.exception))

    def test_small_conversation_is_replayed_without_a_placeholder(self):
        definitions = budget_tools()
        decider = self.decider()
        messages = decider.messages("向前飞10米",
                                    checkpoint(observations=bulk_observations(3)), definitions)
        self.assertEqual([m["role"] for m in messages],
                         ["system", "user"] + ["assistant", "tool"] * 3)
        self.assertNotIn("已省略", json.dumps(messages, ensure_ascii=False))

    def test_default_config_holds_a_real_twenty_observation_cockpit_loop(self):
        """真实 59 工具驾驶舱 + 20 次 read_state：默认配置必须装得下并保住 system prompt。"""
        from lingxi_flight_control import (
            MemoryFlightControlSimulator,
            RuntimeBinding,
            build_full_control_tool_registry,
        )

        simulator = MemoryFlightControlSimulator(RuntimeBinding("a", "p", "h", "d", "b", 1))
        registry = build_full_control_tool_registry(simulator)
        definitions = registry.definitions()
        receipt = registry.get("read_state").handler({})
        observations = [{"tool": "read_state", "arguments": {}, "result": receipt}
                        for _ in range(20)]
        decider = OllamaDecider(model="test-model", transport=FakeTransport([]))
        messages = decider.messages("读取当前状态", checkpoint(observations=observations),
                                    definitions)

        self.assertEqual(messages[0]["content"], DEFAULT_SYSTEM_PROMPT)
        self.assertEqual(messages[1], {"role": "user", "content": "读取当前状态"})
        omitted = 20 - DEFAULT_KEEP_RECENT_OBSERVATIONS
        self.assertEqual(messages[2]["content"],
                         f"（更早的 {omitted} 次工具调用已省略：{' '.join(['read_state✓'] * omitted)}）")
        self.assertLessEqual(decider.estimate_prompt_tokens(messages, definitions),
                             decider.prompt_budget_tokens)
        # 默认窗口必须装得下实测的完整对话；压缩只是策略上的进一步收窄。
        self.assertGreaterEqual(decider.prompt_budget_tokens,
                                self.MEASURED_TWENTY_OBSERVATION_TOKENS)


class OmissionSummaryTests(unittest.TestCase):
    """被压掉的早期观察换成一条紧凑摘要（工具名 + 状态），而不是一句裸占位。

    裸占位只说「有更早的调用」，模型不知道做过什么，容易重复执行；摘要把「做过什么、
    成没成」留在上下文里。摘要本身也必须受同一套预算约束，且**优先于观察被压**。
    """

    def decider(self, **kwargs):
        kwargs.setdefault("num_ctx", DEFAULT_NUM_CTX)
        return OllamaDecider(model="test-model", transport=FakeTransport([]), **kwargs)

    def test_summary_is_deterministic_and_names_each_omitted_call(self):
        definitions = budget_tools()
        observations = bulk_observations(DEFAULT_KEEP_RECENT_OBSERVATIONS + 3)
        messages = self.decider().messages("向前飞10米",
                                           checkpoint(observations=observations), definitions)
        expected = ("（更早的 3 次工具调用已省略："
                    "action_00✓ action_01✓ action_02✓）")
        self.assertEqual(messages[2]["content"], expected)
        # 确定性：同一检查点重复构建必须逐字节相同。
        again = self.decider().messages("向前飞10米",
                                        checkpoint(observations=observations), definitions)
        self.assertEqual(messages, again)

    def test_failed_calls_are_marked_as_failed_without_replaying_their_body(self):
        definitions = budget_tools()
        observations = [
            {"tool": "hover", "arguments": {},
             "error": {"code": "tool_error", "message": "距离超限的详细原因"}},
            {"tool": "action_00", "arguments": {"value": 0}, "result": {"status": "accepted"}},
            {"tool": "action_01", "arguments": {"value": 1}, "result": {"status": "accepted"}},
        ]
        messages = self.decider(keep_recent_observations=1).messages(
            "向前飞10米", checkpoint(observations=observations), definitions)
        self.assertEqual(messages[2]["content"],
                         "（更早的 2 次工具调用已省略：hover✗ action_00✓）")
        self.assertNotIn("距离超限的详细原因", json.dumps(messages, ensure_ascii=False))

    def test_summary_is_compressed_before_any_observation_is_dropped(self):
        """完整摘要装不下时，先退化成紧凑占位，而不是把观察一条条丢掉。"""
        definitions = budget_tools()
        # 省掉的观察带一个超长工具名：完整摘要 ≈3000 tokens，装不进 num_ctx=4096 的 2048 预算。
        observations = [{"tool": "y" * 6000, "arguments": {},
                         "result": {"status": "accepted"}}] + [
            {"tool": f"action_{index:02d}", "arguments": {"value": index},
             "result": {"status": "accepted"}}
            for index in range(3)
        ]
        decider = self.decider(num_ctx=4096, keep_recent_observations=1)
        messages = decider.messages("向前飞10米", checkpoint(observations=observations), definitions)

        self.assertEqual(messages[2]["content"], "（更早的 3 次工具调用已省略）")
        replayed = [m["tool_calls"][0]["function"]["name"] for m in messages if m.get("tool_calls")]
        self.assertEqual(replayed, ["action_02"])          # 观察一条都没被丢
        self.assertLessEqual(decider.estimate_prompt_tokens(messages, definitions),
                             decider.prompt_budget_tokens)

    def test_summary_can_be_turned_off_for_an_ab_comparison(self):
        # A/B 用：关掉摘要就是修复前那句裸占位，其余策略一字不改。
        definitions = budget_tools()
        decider = self.decider(keep_recent_observations=2, omission_summary=False)
        messages = decider.messages("向前飞10米",
                                    checkpoint(observations=bulk_observations(4)), definitions)
        self.assertEqual(messages[2]["content"], "（更早的 2 次工具调用已省略）")

    def test_summary_is_never_sent_over_budget(self):
        definitions = budget_tools()
        for num_ctx in (4096, 8192, DEFAULT_NUM_CTX):
            for count in (13, 20, 60):
                decider = self.decider(num_ctx=num_ctx)
                messages = decider.messages("向前飞10米",
                                            checkpoint(observations=bulk_observations(count)),
                                            definitions)
                self.assertLessEqual(
                    decider.estimate_prompt_tokens(messages, definitions),
                    decider.prompt_budget_tokens,
                    msg=f"num_ctx={num_ctx} observations={count}")


class BudgetTelemetryTests(unittest.TestCase):
    """压缩不能是静默的：每次构建待发送载荷都要能读出估算值与省略条数。

    没有这组计数器，线上无法区分「预算闸门在工作」和「根本没触发压缩」，
    也就无法在上下文被压到影响决策之前发现问题。
    """

    def make(self, **kwargs):
        kwargs.setdefault("num_ctx", 4096)
        return OllamaDecider(model="m", transport=FakeTransport([]), **kwargs)

    def test_snapshot_is_empty_before_anything_is_built(self):
        decider = self.make()
        self.assertIsNone(decider.last_prompt_estimate)
        self.assertEqual(decider.last_omitted_observations, 0)
        self.assertEqual(decider.compressed_payloads, 0)
        self.assertEqual(decider.omitted_observation_turns, 0)

    def test_payload_records_the_estimate_and_the_omissions(self):
        definitions = budget_tools()
        decider = self.make()
        body = decider.payload("向前飞10米",
                               checkpoint(observations=bulk_observations(30)), definitions)

        self.assertEqual(decider.last_omitted_observations,
                         30 - DEFAULT_KEEP_RECENT_OBSERVATIONS)
        self.assertEqual(decider.last_prompt_estimate,
                         decider.estimate_prompt_tokens(body["messages"], definitions))
        self.assertLessEqual(decider.last_prompt_estimate, decider.prompt_budget_tokens)
        self.assertEqual(decider.compressed_payloads, 1)
        self.assertEqual(decider.omitted_observation_turns, 30 - DEFAULT_KEEP_RECENT_OBSERVATIONS)

    def test_small_conversation_reports_no_compression(self):
        decider = self.make(num_ctx=DEFAULT_NUM_CTX)
        decider.payload("向前飞10米", checkpoint(observations=bulk_observations(3)), budget_tools())
        self.assertEqual(decider.last_omitted_observations, 0)
        self.assertEqual(decider.compressed_payloads, 0)
        self.assertIsNotNone(decider.last_prompt_estimate)

    def test_counters_accumulate_across_requests(self):
        definitions = budget_tools()
        decider = self.make()
        for _ in range(3):
            decider.payload("向前飞10米", checkpoint(observations=bulk_observations(20)), definitions)
        self.assertEqual(decider.compressed_payloads, 3)
        self.assertEqual(decider.omitted_observation_turns,
                          3 * (20 - DEFAULT_KEEP_RECENT_OBSERVATIONS))

    def test_decide_surfaces_the_budget_of_the_request_it_sent(self):
        transport = FakeTransport([message_response("好")])
        decider = OllamaDecider(model="m", transport=transport, num_ctx=4096)
        decider.decide("向前飞10米", checkpoint(observations=bulk_observations(30)), budget_tools())
        self.assertEqual(decider.last_omitted_observations, 30 - DEFAULT_KEEP_RECENT_OBSERVATIONS)
        self.assertLessEqual(decider.last_prompt_estimate, decider.prompt_budget_tokens)


class ResultProjectorTests(unittest.TestCase):
    """回执投影挂在解码器上；它只作用于 result，永远不碰 error 回执。"""

    def test_projector_trims_replayed_results(self):
        seen = []

        def projector(name, result):
            seen.append(name)
            return {"status": result["status"]}

        decider = OllamaDecider(model="m", transport=FakeTransport([]), result_projector=projector)
        cp = checkpoint(observations=[{"tool": "read_state", "arguments": {},
                                       "result": {"status": "accepted", "command_id": "c"}}])
        messages = decider.messages("读取当前状态", cp)
        self.assertEqual(json.loads(messages[3]["content"]), {"status": "accepted"})
        self.assertEqual(seen, ["read_state"])

    def test_projector_never_touches_error_observations(self):
        # 错误回执是模型自纠的唯一线索，必须原样回放。
        decider = OllamaDecider(model="m", transport=FakeTransport([]),
                                result_projector=lambda _name, _result: {"dropped": True})
        cp = checkpoint(observations=[{"tool": "hover",
                                       "error": {"code": "tool_exception", "message": "boom"}}])
        self.assertIn("boom", decider.messages("悬停5秒", cp)[3]["content"])

    def test_without_projector_results_are_replayed_verbatim(self):
        decider = OllamaDecider(model="m", transport=FakeTransport([]))
        cp = checkpoint(observations=[{"tool": "read_state", "arguments": {},
                                       "result": {"status": "accepted", "command_id": "c"}}])
        self.assertEqual(json.loads(decider.messages("读取当前状态", cp)[3]["content"]),
                         {"status": "accepted", "command_id": "c"})

    def test_non_callable_projector_is_rejected(self):
        with self.assertRaises(ValueError):
            OllamaDecider(model="m", result_projector="nope")


class GenerationTelemetryTests(unittest.TestCase):
    """生成预留（``output_reserve``）的取值只能靠实测，所以生成读数必须是一等公民。

    Ollama 的响应里 ``done_reason="length"`` 表示生成被 `num_ctx` 截断——那正是
    output_reserve 太小才会出现的故障；``eval_count`` 是它的量化版本。
    """

    def test_generation_readouts_are_exposed(self):
        telemetry = generation_telemetry(
            {"done_reason": "stop", "eval_count": 319, "prompt_eval_count": 9156})
        self.assertEqual(telemetry, {"done_reason": "stop", "eval_count": 319,
                                     "prompt_eval_count": 9156})

    def test_truncated_generation_is_visible(self):
        # done_reason=length 说明这次生成撞上了 num_ctx 的尾巴，必须能被读出来。
        self.assertEqual(generation_telemetry({"done_reason": "length"})["done_reason"], "length")

    def test_missing_response_yields_stable_none_fields(self):
        self.assertEqual(generation_telemetry(None),
                         {"done_reason": None, "eval_count": None, "prompt_eval_count": None})
        self.assertEqual(generation_telemetry("nope"),
                         {"done_reason": None, "eval_count": None, "prompt_eval_count": None})

    def test_decider_surfaces_the_generation_readouts_of_the_last_response(self):
        decider = OllamaDecider(model="m", transport=FakeTransport([tool_call_response("hover", {})]))
        self.assertEqual(decider.last_generation["done_reason"], None)
        decider.decide("悬停5秒", checkpoint(), ())
        # 注入的假传输层没有生成读数：字段必须稳定存在且为 None，而不是 KeyError。
        self.assertIsNone(decider.last_generation["done_reason"])
        decider.last_raw_response = {"done_reason": "stop", "eval_count": 812}
        self.assertEqual(decider.last_generation["eval_count"], 812)


class ConstructorValidationTests(unittest.TestCase):
    def test_invalid_construction_is_rejected(self):
        for kwargs in ({"model": ""}, {"model": "m", "base_url": ""}, {"model": "m", "timeout": 0},
                       {"model": "m", "num_ctx": 0}, {"model": "m", "max_attempts": 0}):
            with self.assertRaises(ValueError):
                OllamaDecider(**kwargs)

    def test_endpoint_is_built_from_base_url(self):
        decider = OllamaDecider(model="m", base_url="http://host:11434/",
                                transport=FakeTransport([]))
        self.assertEqual(decider._endpoint, "http://host:11434/api/chat")


class EndToEndLoopTests(unittest.TestCase):
    """适配器 + 循环 + 模拟器全栈跑通：模型决定 -> 白名单 -> 执行 -> 观察 -> 再决定。"""

    def test_multi_step_tool_chain_executes_through_the_real_loop(self):
        simulator = MemoryFlightSimulator(ExecutionContext("e", "d", 0, True))
        registry = simulator.tool_registry()
        transport = FakeTransport([
            tool_call_response("relative_move",
                               {"direction": "forward", "distance_m": 10, "idempotency_key": "k1"}),
            tool_call_response("hover", {"duration_s": 30, "idempotency_key": "k2"}),
            message_response("已前进10米并悬停30秒。"),
        ])
        decider = OllamaDecider(model="test-model", transport=transport)
        result = BoundedAgentLoop(decider, registry, max_steps=5).run("先向前飞10米，然后原地悬停30秒")

        self.assertEqual(result.status.value, "final")
        self.assertEqual([c["name"] for c in result.checkpoint.tool_calls], ["relative_move", "hover"])
        self.assertEqual(simulator.position.y_m, 10.0)
        self.assertEqual(len(transport.calls), 3)
        # 第二次请求必须已经带上第一次的观察结果，否则模型无法续做第二步。
        second = transport.calls[1]["payload"]["messages"]
        self.assertIn("tool", [m["role"] for m in second])
        # 下发给模型的只有契约，没有 handler。
        self.assertNotIn("handler", json.dumps(transport.calls[0]["payload"]["tools"]))

    def test_out_of_range_argument_is_fed_back_and_never_reaches_the_handler(self):
        simulator = MemoryFlightSimulator(ExecutionContext("e", "d", 0, True))
        registry = simulator.tool_registry()
        # 模型无视 100 米上限给出 500 米；看到校验错误后改为向用户澄清。
        transport = FakeTransport([
            tool_call_response("relative_move",
                               {"direction": "forward", "distance_m": 500, "idempotency_key": "k"}),
            message_response("距离上限是 100 米，请确认您要飞多远。"),
        ])
        result = BoundedAgentLoop(OllamaDecider(model="test-model", transport=transport),
                                  registry).run("向前飞500米")
        self.assertEqual(result.status.value, "clarification")   # 模型改为向用户澄清
        self.assertEqual(simulator.position.y_m, 0.0)     # 越界参数从未触达执行器
        self.assertEqual(result.checkpoint.executed_tool_calls, [])
        self.assertEqual(len(transport.calls), 2)         # 错误被回喂给模型
        replayed = json.dumps(transport.calls[1]["payload"]["messages"], ensure_ascii=False)
        self.assertIn("maximum", replayed)

    def test_model_failure_fails_closed_without_executing(self):
        simulator = MemoryFlightSimulator(ExecutionContext("e", "d", 0, True))
        transport = FakeTransport([{"message": {"role": "assistant", "content": ""}}])
        result = BoundedAgentLoop(OllamaDecider(model="test-model", transport=transport),
                                  simulator.tool_registry()).run("向前飞10米")
        self.assertEqual(result.status.value, "tool_error")
        self.assertEqual(simulator.position.y_m, 0.0)


class HttpTransportTests(unittest.TestCase):
    """默认传输层的错误路径：报错必须能区分服务不可达与冷启动加载慢。"""

    def _patch(self, **kwargs):
        from unittest import mock
        return mock.patch("urllib.request.urlopen", **kwargs)

    def _response(self, body: bytes):
        from unittest import mock
        response = mock.MagicMock()
        response.read.return_value = body
        response.__enter__ = lambda s: s
        response.__exit__ = lambda s, *a: False
        return response

    def test_success_returns_parsed_object(self):
        with self._patch(return_value=self._response(json.dumps({"message": {"content": "ok"}}).encode())):
            data = http_transport("http://host/api/chat", {"model": "m"}, 5.0)
        self.assertEqual(data["message"]["content"], "ok")

    def test_http_error_reports_status_without_body(self):
        import urllib.error
        error = urllib.error.HTTPError(
            "http://host", 404, "Not Found", {}, io.BytesIO(b'{"error":"model not found"}'))
        with self._patch(side_effect=error):
            with self.assertRaises(ValueError) as ctx:
                http_transport("http://host/api/chat", {}, 5.0)
        message = str(ctx.exception)
        self.assertIn("HTTP 404", message)
        self.assertNotIn("model not found", message)

    def test_timeout_is_distinguishable_from_unreachable(self):
        import urllib.error
        with self._patch(side_effect=urllib.error.URLError(TimeoutError("timed out"))):
            with self.assertRaises(ValueError) as ctx:
                http_transport("http://host/api/chat", {}, 30.0)
        self.assertIn("did not respond within 30s", str(ctx.exception))
        self.assertIn("keep_alive", str(ctx.exception))

        with self._patch(side_effect=urllib.error.URLError(ConnectionRefusedError("refused"))):
            with self.assertRaises(ValueError) as ctx:
                http_transport("http://host/api/chat", {}, 5.0)
        self.assertIn("unreachable", str(ctx.exception))

    def test_invalid_json_and_non_object_are_rejected(self):
        for body in (b"<html>oops</html>", b"[1,2,3]"):
            with self._patch(return_value=self._response(body)):
                with self.assertRaises(ValueError):
                    http_transport("http://host/api/chat", {}, 5.0)


if __name__ == "__main__":
    unittest.main()
