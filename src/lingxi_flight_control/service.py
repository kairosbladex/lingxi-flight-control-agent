"""Agent 服务层：把会话运行器接到 HTTP 契约上。

关键约束：**路由与判定完全不碰 socket。** `AgentService.handle(method, path, body)`
是纯函数式的入口，单测直接调它即可覆盖全部状态码；`build_http_server` 只是一个
薄壳，把 `http.server` 的请求翻译成 `service.handle(...)` 的调用。

错误语义（不发明成功）：
- 请求本身不合法 -> 400 `INVALID_REQUEST`；
- 会话过期 -> 200 `stale_context` / `SESSION_EXPIRED`（客户端应当重新开始）；
- 会话被篡改 -> 400 `SESSION_TAMPERED`；
- 循环返回 tool_error / stale_context -> **原样** 200 返回，HTTP 层不改判。
"""

from __future__ import annotations

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
from typing import Any, Mapping
from urllib.parse import unquote

from .session import (
    CheckpointStore,
    SessionError,
    SessionExpired,
    SessionNotFound,
    SessionTampered,
)

_HEALTHZ_PATH = "/healthz"
_MESSAGES_PATTERN = re.compile(r"^/sessions/(?P<session_id>[^/]+)/messages$")
_SESSION_PATTERN = re.compile(r"^/sessions/(?P<session_id>[^/]+)$")


@dataclass(frozen=True)
class ServiceResponse:
    """一次服务调用的结果：HTTP 状态码 + JSON 载荷。"""

    status_code: int
    payload: dict[str, Any]


def _error_payload(error_code: str, message: str) -> dict[str, Any]:
    return {"status": "error", "error_code": error_code, "message": message}


def _route(path: str) -> tuple[str, str] | None:
    """把请求路径映射成 (路由名, session_id)；未知路径返回 None。"""
    cleaned = path.split("?", 1)[0].split("#", 1)[0]
    if cleaned in (_HEALTHZ_PATH, _HEALTHZ_PATH + "/"):
        return ("healthz", "")
    candidate = cleaned[:-1] if cleaned.endswith("/") and cleaned != "/" else cleaned
    match = _MESSAGES_PATTERN.fullmatch(candidate)
    if match is not None:
        return ("messages", unquote(match.group("session_id")))
    match = _SESSION_PATTERN.fullmatch(candidate)
    if match is not None:
        return ("session", unquote(match.group("session_id")))
    return None


def decode_json_body(body: bytes | bytearray | str | None) -> dict[str, Any]:
    """解析请求体为 JSON 对象；空体视为 `{}`，其余不合法一律 `ValueError`。"""
    if body is None:
        return {}
    if isinstance(body, (bytes, bytearray)):
        if not bytes(body).strip():
            return {}
        try:
            raw = bytes(body).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("body must be UTF-8 encoded JSON") from exc
    elif isinstance(body, str):
        raw = body
    else:
        raise ValueError("body must be bytes or str")
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"body must be a JSON object: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("body must be a JSON object")
    return dict(payload)


class AgentService:
    """会话服务的路由与判定层；不持有连接、不开 socket。"""

    def __init__(self, runner: Any, *, execution_mode: str, store: CheckpointStore | None = None):
        if not isinstance(execution_mode, str) or not execution_mode.strip():
            raise ValueError("execution_mode must be a non-empty string")
        if store is None:
            store = getattr(runner, "store", None)
        if not isinstance(store, CheckpointStore):
            raise ValueError("AgentService requires a CheckpointStore (pass store=...)")
        self._runner = runner
        self._execution_mode = execution_mode
        self._store = store

    @property
    def execution_mode(self) -> str:
        return self._execution_mode

    @property
    def store(self) -> CheckpointStore:
        return self._store

    def handle(self, method: str, path: str, body: bytes | bytearray | str | None = b"") -> ServiceResponse:
        """纯路由入口：全部 HTTP 语义都收敛在这里，便于离线测试。"""
        if not isinstance(method, str) or not isinstance(path, str):
            return ServiceResponse(400, _error_payload("INVALID_REQUEST",
                                                       "method and path must be strings"))
        verb = method.strip().upper()
        routed = _route(path)
        if routed is None:
            return ServiceResponse(404, _error_payload("NOT_FOUND", f"unknown path: {path}"))
        kind, session_id = routed
        if kind == "healthz":
            if verb != "GET":
                return self._method_not_allowed(verb, path)
            return self._handle_healthz()
        if kind == "messages":
            if verb != "POST":
                return self._method_not_allowed(verb, path)
            return self._handle_message(session_id, body)
        if verb == "GET":
            return self._handle_get(session_id)
        if verb == "DELETE":
            return self._handle_delete(session_id)
        return self._method_not_allowed(verb, path)

    # -- routes -------------------------------------------------------------

    def _handle_healthz(self) -> ServiceResponse:
        try:
            sessions = len(self._store.list_sessions())
        except SessionError as exc:  # pragma: no cover - 存储异常时宁可暴露
            return ServiceResponse(500, _error_payload("INTERNAL_ERROR", str(exc)))
        return ServiceResponse(200, {
            "status": "ok",
            "execution_mode": self._execution_mode,
            "sessions": sessions,
        })

    def _handle_message(self, session_id: str, body: Any) -> ServiceResponse:
        try:
            payload = decode_json_body(body)
        except ValueError as exc:
            return ServiceResponse(400, _error_payload("INVALID_REQUEST", str(exc)))
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return ServiceResponse(400, _error_payload(
                "INVALID_REQUEST", "text must be a non-empty string"))
        confirmed = payload.get("confirmed", False)
        if not isinstance(confirmed, bool):
            return ServiceResponse(400, _error_payload(
                "INVALID_REQUEST", "confirmed must be a boolean"))
        request_id = payload.get("request_id")
        if request_id is not None:
            if not isinstance(request_id, str):
                return ServiceResponse(400, _error_payload(
                    "INVALID_REQUEST", "request_id must be a string or null"))
            request_id = request_id.strip() or None
        context = payload.get("context")
        if context is not None and not isinstance(context, Mapping):
            return ServiceResponse(400, _error_payload(
                "INVALID_REQUEST", "context must be an object or null"))
        try:
            response = self._runner.handle(session_id, text=text, confirmed=confirmed,
                                           request_id=request_id,
                                           context=None if context is None else dict(context))
        except SessionExpired as exc:
            return ServiceResponse(200, {
                "status": "stale_context",
                "error_code": "SESSION_EXPIRED",
                "session_id": session_id,
                "message": str(exc),
            })
        except SessionTampered as exc:
            return ServiceResponse(400, _error_payload("SESSION_TAMPERED", str(exc)))
        except SessionError as exc:
            return ServiceResponse(400, _error_payload("SESSION_ERROR", str(exc)))
        except ValueError as exc:
            # 循环对越界/不可序列化输入抛 ValueError：这是请求的问题，不是服务故障。
            return ServiceResponse(400, _error_payload("INVALID_REQUEST", str(exc)))
        except Exception as exc:  # pragma: no cover - 兜底，绝不把异常说成成功
            return ServiceResponse(500, _error_payload(
                "INTERNAL_ERROR", f"{type(exc).__name__}: {exc}"))
        return ServiceResponse(200, dict(response))

    def _handle_get(self, session_id: str) -> ServiceResponse:
        try:
            record = self._store.load(session_id)
        except SessionNotFound:
            return ServiceResponse(404, _error_payload(
                "NOT_FOUND", f"session not found: {session_id}"))
        except SessionExpired as exc:
            return ServiceResponse(200, {
                "status": "stale_context",
                "error_code": "SESSION_EXPIRED",
                "session_id": session_id,
                "message": str(exc),
            })
        except SessionTampered as exc:
            return ServiceResponse(400, _error_payload("SESSION_TAMPERED", str(exc)))
        except SessionError as exc:
            return ServiceResponse(400, _error_payload("SESSION_ERROR", str(exc)))
        checkpoint = record.checkpoint
        return ServiceResponse(200, {
            "session_id": record.session_id,
            "checkpoint_revision": record.revision,
            "status": checkpoint.status.value,
            "step_count": checkpoint.step_count,
            "user_input": checkpoint.user_input,
            "expires_at": record.expires_at,
        })

    def _handle_delete(self, session_id: str) -> ServiceResponse:
        try:
            self._store.load(session_id)
        except SessionNotFound:
            return ServiceResponse(404, {"deleted": False, **_error_payload(
                "NOT_FOUND", f"session not found: {session_id}")})
        except (SessionExpired, SessionTampered):
            # 过期或损坏的会话仍然必须可删：删除是恢复路径，不是执行路径。
            pass
        except SessionError as exc:
            return ServiceResponse(400, _error_payload("SESSION_ERROR", str(exc)))
        try:
            self._store.delete(session_id)
        except SessionError as exc:
            return ServiceResponse(400, _error_payload("SESSION_ERROR", str(exc)))
        return ServiceResponse(200, {"deleted": True})

    @staticmethod
    def _method_not_allowed(verb: str, path: str) -> ServiceResponse:
        return ServiceResponse(405, _error_payload(
            "METHOD_NOT_ALLOWED", f"method {verb or '?'} is not allowed on {path}"))


def build_http_server(service: AgentService, host: str, port: int) -> ThreadingHTTPServer:
    """把 `AgentService` 包成 HTTP/1.1 服务；只做协议翻译，不含业务判定。

    - 始终发送 `Content-Length` 与 `Content-Type: application/json; charset=utf-8`；
    - JSON 序列化统一 `ensure_ascii=False, sort_keys=True`；
    - 除了一行访问日志，不向 stdout 打印任何东西（默认的 stderr 日志被关掉）。
    """

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "lingxi-flight-control-agent"
        sys_version = ""

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的命名约定
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def do_DELETE(self) -> None:  # noqa: N802
            self._dispatch()

        def do_PUT(self) -> None:  # noqa: N802
            self._dispatch()

        def do_PATCH(self) -> None:  # noqa: N802
            self._dispatch()

        def do_HEAD(self) -> None:  # noqa: N802
            self._dispatch()

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = 0
            body = self.rfile.read(length) if length > 0 else b""
            try:
                response = service.handle(self.command, self.path, body)
            except Exception as exc:  # pragma: no cover - service.handle 自己已兜底
                response = ServiceResponse(500, _error_payload(
                    "INTERNAL_ERROR", f"{type(exc).__name__}: {exc}"))
            encoded = json.dumps(response.payload, ensure_ascii=False,
                                 sort_keys=True).encode("utf-8")
            self.send_response(response.status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(encoded)

        def version_string(self) -> str:
            """不带 Python 版本，避免把解释器细节回给对方。"""
            return self.server_version

        def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
            print(f"[agent-service] {self.command} {self.path} -> {code}")

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            """关掉默认的 stderr 输出；访问日志由 log_request 打一行。"""
            return

        def log_error(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    return ThreadingHTTPServer((host, port), _Handler)
