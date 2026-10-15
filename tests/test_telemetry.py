"""结构化遥测测试：事件字段确定、序列确定、sink 异常不影响决策。"""

from __future__ import annotations

import io
import json
import unittest

from lingxi_flight_control import (
    BoundedAgentLoop,
    DecisionKind,
    ExecutionContext,
    LoopStatus,
    MemoryFlightSimulator,
    ModelDecision,
    OllamaDecider,
    ToolCall,
    ToolDefinition,
    ToolParameter,
    ToolRegistry,
    ToolSchema,
)
from lingxi_flight_control.models import AgentCheckpoint
from lingxi_flight_control.telemetry import (
    EVENT_COMPRESSED,
    EVENT_DECIDE,
    EVENT_FAIL_CLOSED,
    EVENT_FIELDS,
    EVENT_STALE_CONTEXT,
    EVENT_UNKNOWN_TOOL,
    TELEMETRY_SCHEMA_VERSION,
    MemorySink,
    StdoutSink,
    Telemetry,
    TelemetryEvent,
)


class FakeTransport:
    """按脚本回放响应，并记录请求体（不访问网络）。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __call__(self, url, payload, timeout):
        self.calls.append({"url": url, "payload": json.loads(json.dumps(payload))})
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def tool_call_response(name, arguments):
    return {"message": {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": name, "arguments": arguments}}]},
        "done_reason": "stop", "eval_count": 42, "prompt_eval_count": 7000}


def checkpoint_with_observations(count: int) -> AgentCheckpoint:
    observations = [{"tool": "read_state", "arguments": {}, "result": {"status": "accepted"}}
                    for _ in range(count)]
    return AgentCheckpoint(user_input="向前飞10米", context={}, messages=[], tool_calls=[],
                           observations=observations)


class RecordingDecider:
    def __init__(self, decision):
        self._decision = decision
        self.calls = 0

    def decide(self, *_):
        self.calls += 1
        return self._decision


class ExplodingDecider:
    def decide(self, *_):
        raise AssertionError("decider must not be called")


class SinkPlumbingTests(unittest.TestCase):
    def test_stdout_sink_writes_one_json_object_per_line(self):
        stream = io.StringIO()
        telemetry = Telemetry(StdoutSink(stream))
        telemetry.fail_closed(reason="unknown_tool", message="不认识的工具", tool="nope", step=2)
        telemetry.stale_context(reason="stale_context", message="上下文已变", step=3)

        lines = [line for line in stream.getvalue().splitlines() if line]
        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0])
        self.assertEqual(first["event"], EVENT_FAIL_CLOSED)
        self.assertEqual(first["schema_version"], TELEMETRY_SCHEMA_VERSION)
        self.assertEqual(first["reason"], "unknown_tool")
        self.assertEqual(telemetry.emitted, 2)
        self.assertEqual(telemetry.sink_errors, 0)

    def test_default_sink_is_stdout_json_line(self):
        telemetry = Telemetry()
        self.assertTrue(telemetry.enabled)
        self.assertIsInstance(telemetry.sink, StdoutSink)

    def test_sink_exception_is_swallowed_and_counted(self):
        def exploding_sink(event):
            raise RuntimeError("log collector down")

        telemetry = Telemetry(exploding_sink)
        telemetry.fail_closed(reason="model_error", message="模型不可用")
        self.assertEqual(telemetry.submitted, 1)
        self.assertEqual(telemetry.emitted, 0, "sink 没成功就不算 emitted")
        self.assertEqual(telemetry.sink_errors, 1)

    def test_fields_named_like_emit_parameters_cannot_break_the_run(self):
        """字段名撞形参会把 TypeError 抛进决策路径——位置-only 参数堵死这条路。"""
        sink = MemorySink()
        telemetry = Telemetry(sink)
        telemetry.emit("custom", name="抢名字", event_name="也抢")
        self.assertEqual(sink.events[0].to_dict()["name"], "抢名字")
        self.assertEqual(telemetry.internal_errors, 0)
        self.assertEqual(telemetry.sink_errors, 0)

    def test_unserializable_fields_do_not_escape_the_default_sink(self):
        stream = io.StringIO()
        telemetry = Telemetry(StdoutSink(stream))
        telemetry.emit("custom", weird=object())
        payload = json.loads(stream.getvalue())
        self.assertIn("weird", payload)
        self.assertEqual(telemetry.sink_errors, 0)

    def test_disabled_telemetry_emits_nothing(self):
        sink = MemorySink()
        telemetry = Telemetry(sink, enabled=False)
        telemetry.fail_closed(reason="model_error", message="x")
        self.assertEqual(sink.events, [])
        self.assertEqual(telemetry.emitted, 0)
        self.assertFalse(telemetry.enabled)

    def test_schema_violations_are_counted_and_never_raised(self):
        sink = MemorySink()
        telemetry = Telemetry(sink)
        telemetry.emit(EVENT_DECIDE, attempt=1)                    # 缺必填字段
        telemetry.emit(EVENT_DECIDE, nope=1)                       # 未知字段
        telemetry.fail_closed(reason="invented_reason", message="x")  # 词表外的 reason
        self.assertEqual(sink.events, [])
        self.assertEqual(telemetry.schema_errors, 3)
        self.assertEqual(telemetry.submitted, 3, "计数发生在校验之前，便于发现配置错误")
        self.assertEqual(telemetry.emitted, 0)

    def test_event_payload_is_plain_json_types(self):
        event = TelemetryEvent(EVENT_STALE_CONTEXT, {"reason": "stale_context"})
        self.assertEqual(event.name, EVENT_STALE_CONTEXT)
        self.assertEqual(json.loads(json.dumps(event.to_dict()))["event"], EVENT_STALE_CONTEXT)


class EventContractTests(unittest.TestCase):
    def setUp(self):
        self.sink = MemorySink()
        self.telemetry = Telemetry(self.sink)

    def _assert_field_set(self, name):
        matching = [event for event in self.sink.events if event.name == name]
        self.assertTrue(matching, f"{name} 事件没有产生")
        for event in matching:
            with self.subTest(event=name):
                self.assertEqual(set(event.to_dict()), set(EVENT_FIELDS[name]),
                                 f"{name} 的字段名必须与文档一致")

    def test_decide_event_fields_and_generation_readings(self):
        decider = OllamaDecider(model="m", telemetry=self.telemetry,
                                transport=FakeTransport([tool_call_response("read_state", {})]))
        decider.decide("读取当前状态", checkpoint_with_observations(0), ())
        self._assert_field_set(EVENT_DECIDE)
        payload = self.sink.events[0].to_dict()
        self.assertEqual(payload["kind"], DecisionKind.TOOL_CALL.value)
        self.assertEqual(payload["tool"], "read_state")
        self.assertEqual(payload["prompt_budget"], decider.prompt_budget_tokens)
        self.assertEqual(payload["prompt_estimate"], decider.last_prompt_estimate)
        self.assertEqual(payload["done_reason"], "stop")
        self.assertEqual(payload["eval_count"], 42)
        self.assertFalse(payload["generation_truncated"])
        self.assertFalse(payload["near_budget"])
        self.assertEqual(payload["omitted_observations"], 0)

    def test_decide_event_flags_truncated_generation(self):
        response = tool_call_response("read_state", {})
        response["done_reason"] = "length"
        decider = OllamaDecider(model="m", telemetry=self.telemetry,
                                transport=FakeTransport([response]))
        decider.decide("读取当前状态", checkpoint_with_observations(0), ())
        payload = self.sink.events[0].to_dict()
        self.assertTrue(payload["generation_truncated"])

    def test_compressed_event_fires_only_when_observations_are_omitted(self):
        decider = OllamaDecider(model="m", keep_recent_observations=1, telemetry=self.telemetry,
                                transport=FakeTransport([]))
        decider.payload("向前飞10米", checkpoint_with_observations(3), ())
        self.assertEqual([event.name for event in self.sink.events], [EVENT_COMPRESSED])
        payload = self.sink.events[0].to_dict()
        self._assert_field_set(EVENT_COMPRESSED)
        self.assertEqual(payload["omitted_observations"], 2)
        self.assertEqual(payload["kept_observations"], 1)
        self.assertEqual(payload["omission_mode"], "summary")
        self.assertEqual(payload["compressed_payloads"], 1)
        self.assertEqual(payload["omitted_observation_turns"], 2)

        self.sink.events.clear()
        decider.payload("向前飞10米", checkpoint_with_observations(1), ())
        self.assertEqual(self.sink.events, [], "没有省略就不该有压缩事件")

    def test_fail_closed_and_unknown_tool_events(self):
        loop = BoundedAgentLoop(
            RecordingDecider(ModelDecision(DecisionKind.TOOL_CALL,
                                           tool_call=ToolCall("missing", {}))),
            ToolRegistry(), telemetry=self.telemetry)
        result = loop.run("查询")
        self.assertIs(result.status, LoopStatus.TOOL_ERROR)
        self.assertEqual([event.name for event in self.sink.events],
                         [EVENT_FAIL_CLOSED, EVENT_UNKNOWN_TOOL])
        self._assert_field_set(EVENT_FAIL_CLOSED)
        self._assert_field_set(EVENT_UNKNOWN_TOOL)
        self.assertEqual(self.sink.events[0].to_dict()["reason"], "unknown_tool")
        self.assertEqual(self.sink.events[1].to_dict()["name"], "missing")

    def test_tool_exception_event_carries_the_platform_failure(self):
        definition = ToolDefinition("read_value", "test", ToolSchema({}, ()),
                                    handler=lambda _: 1 / 0)
        loop = BoundedAgentLoop(
            RecordingDecider(ModelDecision(DecisionKind.TOOL_CALL,
                                           tool_call=ToolCall("read_value", {}))),
            ToolRegistry({"read_value": definition}), telemetry=self.telemetry)
        result = loop.run("查询")
        self.assertEqual(result.error["code"], "tool_exception")
        self.assertEqual([event.name for event in self.sink.events], [EVENT_FAIL_CLOSED])
        payload = self.sink.events[0].to_dict()
        self.assertEqual(payload["reason"], "tool_exception")
        self.assertEqual(payload["tool"], "read_value")
        self.assertIn("division", payload["message"])

    def test_stale_context_events(self):
        registry = ToolRegistry(
            runtime_context_provider=lambda: ExecutionContext("exec-1", "drone-1", 0, True))
        checkpoint = AgentCheckpoint(
            user_input="继续", context={}, messages=[], tool_calls=[], observations=[],
            runtime_context=ExecutionContext("exec-1", "drone-2", 0, True).to_dict())
        result = BoundedAgentLoop(ExplodingDecider(), registry,
                                  telemetry=self.telemetry).run("继续", checkpoint=checkpoint)
        self.assertEqual(result.error["code"], "stale_context")
        self.assertEqual([event.name for event in self.sink.events],
                         [EVENT_FAIL_CLOSED, EVENT_STALE_CONTEXT])
        self._assert_field_set(EVENT_STALE_CONTEXT)
        self.assertEqual(self.sink.events[0].to_dict()["reason"], "stale_context")

    def test_event_sequence_is_deterministic_across_two_identical_runs(self):
        def run_once():
            sink = MemorySink()
            telemetry = Telemetry(sink)
            decider = OllamaDecider(model="m", keep_recent_observations=1, telemetry=telemetry,
                                    transport=FakeTransport([tool_call_response("read_state", {})]))
            decider.decide("读取当前状态", checkpoint_with_observations(3), ())
            loop = BoundedAgentLoop(
                RecordingDecider(ModelDecision(DecisionKind.TOOL_CALL,
                                               tool_call=ToolCall("missing", {}))),
                ToolRegistry(), telemetry=telemetry)
            loop.run("查询")
            return [event.to_dict() for event in sink.events]

        first, second = run_once(), run_once()
        self.assertEqual(first, second)
        self.assertEqual([item["event"] for item in first],
                         [EVENT_COMPRESSED, EVENT_DECIDE, EVENT_FAIL_CLOSED, EVENT_UNKNOWN_TOOL])

    def test_sink_exception_does_not_change_the_run_result(self):
        class ExplodingSink:
            def __call__(self, event):
                raise RuntimeError("log collector down")

        class Scripted:
            """先执行一个正常工具，再调用一个白名单外的工具（硬失败，产生事件）。"""

            def __init__(self):
                self.index = 0

            def decide(self, _, checkpoint, __):
                self.index += 1
                if self.index == 1:
                    return ModelDecision(DecisionKind.TOOL_CALL,
                                         tool_call=ToolCall("read_value", {"value": 1}))
                return ModelDecision(DecisionKind.TOOL_CALL, tool_call=ToolCall("missing", {}))

        def run(telemetry):
            calls: list[int] = []
            definition = ToolDefinition(
                "read_value", "test",
                ToolSchema({"value": ToolParameter("integer", minimum=0, maximum=10)}, ("value",)),
                handler=lambda args: calls.append(args["value"]) or {"ok": True})
            result = BoundedAgentLoop(Scripted(), ToolRegistry({"read_value": definition}),
                                      telemetry=telemetry).run("查询")
            return (result.status, dict(result.error), list(result.checkpoint.observations),
                    list(result.checkpoint.executed_tool_calls), list(calls))

        baseline = run(Telemetry.disabled())
        exploding = Telemetry(ExplodingSink())
        observed = run(exploding)
        self.assertEqual(baseline, observed)
        self.assertIs(baseline[0], LoopStatus.TOOL_ERROR)
        self.assertGreater(exploding.sink_errors, 0)
        self.assertEqual(exploding.emitted, 0)


if __name__ == "__main__":
    unittest.main()
