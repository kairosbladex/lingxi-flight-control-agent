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
    OllamaDecider,
    build_tools_payload,
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
        self.assertIn("model not found", str(ctx.exception))

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

    def test_http_error_reports_status_and_body(self):
        import urllib.error
        error = urllib.error.HTTPError(
            "http://host", 404, "Not Found", {}, io.BytesIO(b'{"error":"model not found"}'))
        with self._patch(side_effect=error):
            with self.assertRaises(ValueError) as ctx:
                http_transport("http://host/api/chat", {}, 5.0)
        message = str(ctx.exception)
        self.assertIn("HTTP 404", message)
        self.assertIn("model not found", message)

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
