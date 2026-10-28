"""AgentService 的离线契约测试：路由、状态码、错误映射。

全部断言直接调用 `AgentService.handle`，**不开 socket**。`http.server` 适配层
由 ``tools/bench/service_e2e.py``（不在 discover 范围内）用真实 HTTP 覆盖。
"""

from __future__ import annotations

import json
import unittest

from lingxi_flight_control import (
    BoundedAgentLoop,
    DecisionKind,
    MemoryFlightControlSimulator,
    ModelDecision,
    RuntimeBinding,
    ToolCall,
    ToolDefinition,
    ToolRegistry,
    ToolSchema,
    build_full_control_tool_registry,
)
from lingxi_flight_control.service import AgentService, ServiceResponse, decode_json_body
from lingxi_flight_control.session import (
    MemoryCheckpointStore,
    SessionError,
    SessionExpired,
    SessionRunner,
    SessionTampered,
)

ACQUIRE = "control_acquire"
MOVE_FORWARD = "move_forward"
CAMERA_PHOTO = "camera_photo"
COMMAND = "接管控制权，然后向前飞10米，再拍张照片"


def binding(version: int = 1, drone: str = "drone-1") -> RuntimeBinding:
    return RuntimeBinding("actor-1", "project-1", "hangar-1", drone, "batch-1", version)


def call(name: str, **arguments) -> ModelDecision:
    return ModelDecision(DecisionKind.TOOL_CALL, tool_call=ToolCall(name, arguments))


def final(message: str = "完成") -> ModelDecision:
    return ModelDecision(DecisionKind.FINAL, message=message)


class ScriptedDecider:
    def __init__(self, decisions):
        self._decisions = list(decisions)
        self._pending = None
        self._pending_at = None

    def decide(self, _user_message, checkpoint, _tools):
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


def cockpit_script():
    return [call(ACQUIRE), call(MOVE_FORWARD, distance_m=10), call(CAMERA_PHOTO), final("已完成")]


def build_service(*, decisions=None, store=None, max_rounds: int = 12, registry=None):
    simulator = MemoryFlightControlSimulator(binding())
    tool_registry = registry if registry is not None else build_full_control_tool_registry(simulator)
    loop = BoundedAgentLoop(
        ScriptedDecider(cockpit_script() if decisions is None else decisions),
        tool_registry, max_steps=8)
    store = store if store is not None else MemoryCheckpointStore()
    runner = SessionRunner(loop, store, max_rounds=max_rounds)
    service = AgentService(runner, execution_mode="simulator", store=store)
    return service, simulator, store


def body(payload) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class ServiceRouteTests(unittest.TestCase):
    def test_healthz_reports_mode_and_session_count(self):
        service, _, _ = build_service()
        response = service.handle("GET", "/healthz")
        self.assertIsInstance(response, ServiceResponse)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.payload,
                         {"status": "ok", "execution_mode": "simulator", "sessions": 0})
        service.handle("POST", "/sessions/s-1/messages",
                       body({"text": COMMAND, "request_id": "r1"}))
        self.assertEqual(service.handle("GET", "/healthz").payload["sessions"], 1)

    def test_healthz_query_string_and_trailing_slash_are_tolerated(self):
        service, _, _ = build_service()
        for path in ("/healthz?verbose=1", "/healthz/"):
            with self.subTest(path=path):
                self.assertEqual(service.handle("GET", path).status_code, 200)

    def test_post_message_then_get_summary_then_delete(self):
        service, _, store = build_service()
        posted = service.handle("POST", "/sessions/s-1/messages",
                                body({"text": COMMAND, "confirmed": False,
                                      "request_id": "req-1"}))
        self.assertEqual(posted.status_code, 200)
        self.assertEqual(posted.payload["status"], "clarification")
        self.assertEqual(posted.payload["executed"], [])
        self.assertEqual(posted.payload["tool_call"], {"name": ACQUIRE, "arguments": {}})
        self.assertEqual(posted.payload["checkpoint_revision"], 1)

        summary = service.handle("GET", "/sessions/s-1")
        self.assertEqual(summary.status_code, 200)
        self.assertEqual(sorted(summary.payload),
                         ["checkpoint_revision", "expires_at", "session_id", "status",
                          "step_count", "user_input"])
        self.assertEqual(summary.payload["session_id"], "s-1")
        self.assertEqual(summary.payload["status"], "clarification")
        self.assertEqual(summary.payload["step_count"], 0)
        self.assertEqual(summary.payload["user_input"], COMMAND)
        self.assertEqual(summary.payload["checkpoint_revision"], 1)
        self.assertEqual(summary.payload["expires_at"], store.load("s-1").expires_at)

        deleted = service.handle("DELETE", "/sessions/s-1")
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.payload, {"deleted": True})
        self.assertEqual(service.handle("GET", "/sessions/s-1").status_code, 404)
        self.assertEqual(service.handle("DELETE", "/sessions/s-1").status_code, 404)
        self.assertEqual(service.handle("GET", "/healthz").payload["sessions"], 0)

    def test_unknown_paths_return_404(self):
        service, _, _ = build_service()
        cases = [
            ("GET", "/"),
            ("GET", "/nope"),
            ("GET", "/sessions"),
            ("GET", "/sessions/s-1/nope"),
            ("GET", "/sessions/s-1/messages/extra"),
        ]
        for method, path in cases:
            with self.subTest(method=method, path=path):
                response = service.handle(method, path)
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.payload["status"], "error")
                self.assertEqual(response.payload["error_code"], "NOT_FOUND")

    def test_unknown_session_returns_404(self):
        service, _, _ = build_service()
        for method in ("GET", "DELETE"):
            with self.subTest(method=method):
                response = service.handle(method, "/sessions/missing")
                self.assertEqual(response.status_code, 404)
        self.assertEqual(service.handle("DELETE", "/sessions/missing").payload["deleted"],
                         False)

    def test_unsupported_method_on_known_path_returns_405(self):
        service, _, _ = build_service()
        service.handle("POST", "/sessions/s-1/messages", body({"text": COMMAND}))
        cases = [
            ("PUT", "/healthz"),
            ("POST", "/healthz"),
            ("DELETE", "/healthz"),
            ("GET", "/sessions/s-1/messages"),
            ("PUT", "/sessions/s-1"),
            ("POST", "/sessions/s-1"),
        ]
        for method, path in cases:
            with self.subTest(method=method, path=path):
                response = service.handle(method, path)
                self.assertEqual(response.status_code, 405)
                self.assertEqual(response.payload["error_code"], "METHOD_NOT_ALLOWED")

    def test_malformed_requests_return_400(self):
        service, _, _ = build_service()
        cases = {
            "not-json": b"{not json",
            "array-body": b"[1, 2]",
            "string-body": b"\"text\"",
            "empty-body": b"",
            "not-utf8": b"\xff\xfe\x00",
            "missing-text": body({"confirmed": True}),
            "blank-text": body({"text": "   "}),
            "text-not-string": body({"text": 12}),
            "confirmed-not-bool": body({"text": COMMAND, "confirmed": "yes"}),
            "request-id-not-string": body({"text": COMMAND, "request_id": 12}),
            "context-not-object": body({"text": COMMAND, "context": [1]}),
        }
        for name, payload in cases.items():
            with self.subTest(case=name):
                response = service.handle("POST", "/sessions/s-1/messages", payload)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.payload["status"], "error")
                self.assertEqual(response.payload["error_code"], "INVALID_REQUEST")
                self.assertTrue(response.payload["message"])
        # 非法请求不该留下任何会话。
        self.assertEqual(service.handle("GET", "/healthz").payload["sessions"], 0)

    def test_malformed_session_id_returns_400_session_error(self):
        service, _, _ = build_service()
        response = service.handle("POST", "/sessions/a%2Fb/messages",
                                  body({"text": COMMAND}))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.payload["error_code"], "SESSION_ERROR")

    def test_request_body_helpers(self):
        self.assertEqual(decode_json_body(b""), {})
        self.assertEqual(decode_json_body(None), {})
        self.assertEqual(decode_json_body(b'{"a": 1}'), {"a": 1})
        self.assertEqual(decode_json_body('{"a": 1}'), {"a": 1})
        for bad in (b"[1]", b"{", b"\xff", 5):
            with self.assertRaises(ValueError):
                decode_json_body(bad)

    def test_service_requires_runner_and_store(self):
        with self.assertRaises(ValueError):
            AgentService(object(), execution_mode="simulator")
        _, _, store = build_service()
        with self.assertRaises(ValueError):
            AgentService(object(), execution_mode="", store=store)
        with self.assertRaises(ValueError):
            AgentService(object(), execution_mode="simulator", store=object())


class ServiceLoopContractTests(unittest.TestCase):
    def test_cockpit_baseline_completes_in_two_http_requests(self):
        service, simulator, store = build_service()
        first = service.handle("POST", "/sessions/s-base/messages", body({
            "text": COMMAND, "confirmed": False, "request_id": "req-1"}))
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.payload["status"], "clarification")
        self.assertEqual(first.payload["executed"], [])
        self.assertEqual(simulator.state.revision, 0)
        self.assertIsNotNone(store.load("s-base").checkpoint.confirmation_binding)

        second = service.handle("POST", "/sessions/s-base/messages", body({
            "text": COMMAND, "confirmed": True, "request_id": "req-2"}))
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.payload["status"], "final")
        self.assertEqual(second.payload["executed"],
                         [ACQUIRE, MOVE_FORWARD, CAMERA_PHOTO])
        self.assertEqual(simulator.state.control_owner, "actor-1")
        self.assertAlmostEqual(simulator.state.latitude, 0.0000899266130030023)
        self.assertEqual(simulator.state.camera["photo_count"], 1)

    def test_duplicate_request_id_is_answered_from_the_session_log(self):
        service, simulator, _ = build_service()
        payload = {"text": COMMAND, "confirmed": True, "request_id": "req-dup"}
        first = service.handle("POST", "/sessions/s-dup/messages", body(payload))
        self.assertEqual(first.payload["status"], "final")
        revision = simulator.state.revision
        photos = simulator.state.camera["photo_count"]
        second = service.handle("POST", "/sessions/s-dup/messages", body(payload))
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.payload, first.payload)
        self.assertEqual(simulator.state.revision, revision)
        self.assertEqual(simulator.state.camera["photo_count"], photos)

    def test_unknown_tool_is_reported_verbatim_as_tool_error(self):
        service, simulator, _ = build_service(decisions=[call("no_such_tool")])
        response = service.handle("POST", "/sessions/s-tool/messages",
                                  body({"text": "随便说点什么", "request_id": "req-1"}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.payload["status"], "tool_error")
        self.assertEqual(response.payload["error"]["code"], "tool_error")
        self.assertIn("no_such_tool", response.payload["error"]["message"])
        self.assertEqual(response.payload["executed"], [])
        self.assertEqual(simulator.state.revision, 0)

    def test_handler_exception_is_reported_verbatim_as_tool_error(self):
        def explode(_arguments):
            raise RuntimeError("执行后端炸了")

        registry = ToolRegistry()
        registry.register(ToolDefinition(
            "boom", "总是抛异常的工具", ToolSchema({}), handler=explode,
            side_effect=True, requires_confirmation=True))
        service, _, _ = build_service(decisions=[call("boom")], registry=registry)
        response = service.handle("POST", "/sessions/s-boom/messages",
                                  body({"text": "触发一个异常", "confirmed": True,
                                        "request_id": "req-1"}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.payload["status"], "tool_error")
        self.assertEqual(response.payload["error"]["code"], "tool_exception")
        self.assertIn("执行后端炸了", response.payload["error"]["message"])
        self.assertEqual(response.payload["executed"], [])

    def test_runtime_context_is_checked_by_the_loop_not_the_http_layer(self):
        service, simulator, _ = build_service()
        good = simulator.runtime_context().to_dict()
        accepted = service.handle("POST", "/sessions/s-ctx/messages", body({
            "text": COMMAND, "context": good, "request_id": "req-1"}))
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.payload["status"], "clarification")

        stale = dict(good, drone_id="drone-2")
        rejected = service.handle("POST", "/sessions/s-ctx2/messages", body({
            "text": COMMAND, "context": stale, "request_id": "req-1"}))
        self.assertEqual(rejected.status_code, 200)          # HTTP 层不发明成功，也不改判
        self.assertEqual(rejected.payload["status"], "tool_error")
        self.assertEqual(rejected.payload["error"]["code"], "stale_context")
        self.assertEqual(rejected.payload["executed"], [])

    def test_tool_error_exit_path_is_persisted(self):
        service, _, store = build_service(decisions=[call("no_such_tool")])
        service.handle("POST", "/sessions/s-persist/messages",
                       body({"text": "随便说点什么", "request_id": "req-1"}))
        record = store.load("s-persist")
        self.assertEqual(record.revision, 1)
        self.assertEqual(record.checkpoint.status.value, "tool_error")
        summary = service.handle("GET", "/sessions/s-persist")
        self.assertEqual(summary.payload["status"], "tool_error")


class _StubRunner:
    """只用来验证错误映射的假运行器：不跑循环，只按脚本抛异常。"""

    def __init__(self, exc: Exception):
        self._exc = exc

    def handle(self, *_args, **_kwargs):
        raise self._exc


class ServiceErrorMappingTests(unittest.TestCase):
    def _service(self, exc: Exception):
        store = MemoryCheckpointStore()
        return AgentService(_StubRunner(exc), execution_mode="simulator", store=store)

    def test_session_expired_maps_to_stale_context(self):
        service = self._service(SessionExpired("会话过期"))
        response = service.handle("POST", "/sessions/s-1/messages",
                                  body({"text": COMMAND, "request_id": "r1"}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.payload["status"], "stale_context")
        self.assertEqual(response.payload["error_code"], "SESSION_EXPIRED")
        self.assertEqual(response.payload["session_id"], "s-1")

    def test_session_tampered_maps_to_400(self):
        service = self._service(SessionTampered("文件被改写"))
        response = service.handle("POST", "/sessions/s-1/messages", body({"text": COMMAND}))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.payload["error_code"], "SESSION_TAMPERED")

    def test_session_error_maps_to_400(self):
        service = self._service(SessionError("session_id 不合法"))
        response = service.handle("POST", "/sessions/s-1/messages", body({"text": COMMAND}))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.payload["error_code"], "SESSION_ERROR")

    def test_value_error_maps_to_invalid_request(self):
        service = self._service(ValueError("user input contains forbidden content"))
        response = service.handle("POST", "/sessions/s-1/messages", body({"text": COMMAND}))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.payload["error_code"], "INVALID_REQUEST")

    def test_unexpected_error_maps_to_500(self):
        service = self._service(RuntimeError("boom"))
        response = service.handle("POST", "/sessions/s-1/messages", body({"text": COMMAND}))
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.payload["error_code"], "INTERNAL_ERROR")


if __name__ == "__main__":
    unittest.main()
