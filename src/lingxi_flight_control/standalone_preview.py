"""独立飞控 Agent 预览服务。

这个服务只依赖本仓库的模型 Agent 和内存状态，不依赖 Java、旧前端、MQTT
或真实飞控。位姿由外部遥测适配器通过 ``/pose`` 写入，模型只生成预览卡，
确认和取消仍然是不可下发的内存状态变更。
"""

from __future__ import annotations

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import math
import re
import threading
import time
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from .agent import ModelDecider
from .preview import FlightPreviewAgent, ReferencePose, StaleReferencePose
from .full_control import RuntimeBinding


MAX_BODY_BYTES = 512 * 1024
MAX_ID_CHARS = 128
DEFAULT_POSE_TTL_SEC = 15.0
DEFAULT_CARD_TTL_SEC = 120.0
_ID = re.compile(r"^[^\x00-\x1f\x7f]{1,128}$")


class StandalonePreviewError(ValueError):
    """带稳定 HTTP 错误码的独立服务错误。"""

    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class StandaloneResponse:
    status_code: int
    payload: dict[str, Any]


@dataclass(frozen=True)
class _Context:
    actor_id: str
    project_id: str
    hangar_id: str
    drone_id: str
    task_id: str
    context_version: int

    @property
    def key(self) -> tuple[str, str, str, str, str]:
        return (self.actor_id, self.project_id, self.hangar_id,
                self.drone_id, self.task_id)

    def binding(self) -> RuntimeBinding:
        return RuntimeBinding(
            self.actor_id, self.project_id, self.hangar_id, self.drone_id,
            self.task_id, self.context_version,
        )


def _json_object(body: bytes | bytearray | str | None) -> dict[str, Any]:
    if body is None:
        return {}
    raw = bytes(body).decode("utf-8") if isinstance(body, (bytes, bytearray)) else body
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise StandalonePreviewError("INVALID_REQUEST", "请求体必须是 JSON 对象") from exc
    if not isinstance(value, Mapping):
        raise StandalonePreviewError("INVALID_REQUEST", "请求体必须是 JSON 对象")
    return dict(value)


def _id(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise StandalonePreviewError("INVALID_REQUEST", f"{name} 必须是字符串")
    value = value.strip()
    if not _ID.fullmatch(value):
        raise StandalonePreviewError("INVALID_REQUEST", f"{name} 不能为空或超过{MAX_ID_CHARS}个字符")
    return value


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StandalonePreviewError("INVALID_POSE", f"{name} 必须是数字", 422)
    value = float(value)
    if not math.isfinite(value):
        raise StandalonePreviewError("INVALID_POSE", f"{name} 必须是有限数字", 422)
    return value


def _version(value: Any) -> int:
    if value is None:
        return 1
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StandalonePreviewError("INVALID_REQUEST", "context_version 必须是非负整数")
    return value


def _allowed(body: Mapping[str, Any], keys: set[str]) -> None:
    unknown = sorted(set(body) - keys)
    if unknown:
        raise StandalonePreviewError("INVALID_REQUEST", f"请求包含不支持的字段: {unknown[0]}")


class StandalonePreviewService:
    """独立预览 API 的纯服务层，测试时无需启动 socket。"""

    def __init__(self, decider_factory: Callable[[], ModelDecider], *,
                 clock: Callable[[], float] = time.time,
                 pose_ttl_sec: float = DEFAULT_POSE_TTL_SEC,
                 card_ttl_sec: float = DEFAULT_CARD_TTL_SEC,
                 gateway_token: str | None = None):
        if not callable(decider_factory):
            raise ValueError("decider_factory must be callable")
        if pose_ttl_sec <= 0 or card_ttl_sec <= 0:
            raise ValueError("TTLs must be positive")
        if gateway_token is not None and not str(gateway_token).strip():
            raise ValueError("gateway_token 不能是空字符串")
        # 操作者身份只能由持有该凭证的网关注入；不配置时保持本地开发行为。
        self._gateway_token = str(gateway_token) if gateway_token is not None else None
        self._decider_factory = decider_factory
        self._clock = clock
        self._pose_ttl_sec = float(pose_ttl_sec)
        self._card_ttl_sec = float(card_ttl_sec)
        self._poses: dict[tuple[str, str, str, str, str], ReferencePose] = {}
        self._agents: dict[tuple[str, str, str, str, str], FlightPreviewAgent] = {}
        self._versions: dict[tuple[str, str, str, str, str], int] = {}
        self._lock = threading.RLock()

    @property
    def execution_mode(self) -> str:
        return "preview"

    def handle(self, method: str, path: str, body: bytes | bytearray | str | None = b"",
               headers: Mapping[str, str] | None = None) -> StandaloneResponse:
        headers = headers or {}
        trace_id = self._trace_id(headers)
        try:
            route, query = self._route(path)
            payload = _json_object(body)
            if self._gateway_token is not None and route != "healthz":
                self._require_gateway(headers)
            if route == "healthz" and method.upper() == "GET":
                return self._ok({"status": "ok", "execution_mode": self.execution_mode,
                                 "pose_ttl_sec": self._pose_ttl_sec,
                                 "card_ttl_sec": self._card_ttl_sec}, trace_id)
            if route == "pose" and method.upper() == "GET":
                return self._get_pose(query, headers, trace_id)
            if route == "pose" and method.upper() == "POST":
                return self._put_pose(payload, headers, trace_id)
            if route == "preview" and method.upper() == "POST":
                return self._preview(payload, headers, trace_id)
            if route in {"confirm", "cancel"} and method.upper() == "POST":
                return self._finish(route, payload, headers, trace_id)
            if route is None:
                return self._error(404, "NOT_FOUND", "没有这个独立 Agent 接口", trace_id)
            return self._error(405, "METHOD_NOT_ALLOWED", "请求方法不支持", trace_id)
        except StandalonePreviewError as exc:
            return self._error(exc.status_code, exc.code, str(exc), trace_id)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._error(400, "INVALID_REQUEST", "请求体必须是 UTF-8 JSON", trace_id)
        except Exception as exc:  # pragma: no cover - 最后一道 fail-closed 保护
            return self._error(500, "INTERNAL_ERROR", f"独立预览服务异常: {type(exc).__name__}", trace_id)

    def _route(self, path: str) -> tuple[str | None, dict[str, list[str]]]:
        parsed = urlsplit(path or "")
        route = parsed.path.rstrip("/")
        if route in {"/healthz", "/api/flight-preview/healthz"}:
            return "healthz", parse_qs(parsed.query)
        prefix = "/api/flight-preview/"
        if not route.startswith(prefix):
            return None, parse_qs(parsed.query)
        name = route[len(prefix):]
        if name in {"pose", "preview", "confirm", "cancel"}:
            return name, parse_qs(parsed.query)
        return None, parse_qs(parsed.query)

    def _require_gateway(self, headers: Mapping[str, str]) -> None:
        """身份由网关注入：没有正确凭证的请求一律拒绝，客户端不能自报操作者。"""
        supplied = headers.get("X-Agent-Gateway") or headers.get("x-agent-gateway") or ""
        if not hmac.compare_digest(str(supplied), self._gateway_token or ""):
            raise StandalonePreviewError(
                "UNAUTHENTICATED", "缺少或错误的服务网关凭证，操作者身份必须由网关注入", 401)

    def _trace_id(self, headers: Mapping[str, str]) -> str:
        value = headers.get("X-Trace-Id") or headers.get("x-trace-id")
        if isinstance(value, str) and _ID.fullmatch(value.strip()):
            return value.strip()
        return f"standalone-{uuid4().hex}"

    def _actor(self, headers: Mapping[str, str]) -> str:
        value = headers.get("X-Agent-Actor") or headers.get("x-agent-actor")
        return _id(value or "standalone-ui", "actor_id")

    def _context(self, body: Mapping[str, Any], headers: Mapping[str, str], *,
                 require_site: bool) -> tuple[_Context, str | None]:
        keys = {"project_id", "hangar_id", "drone_id", "task_id", "site_id", "context_version",
                "text", "command_id", "idempotency_key", "pose", "confirmation_id",
                "confirmation_binding"}
        _allowed(body, keys)
        context = _Context(
            self._actor(headers), _id(body.get("project_id"), "project_id"),
            _id(body.get("hangar_id"), "hangar_id"), _id(body.get("drone_id"), "drone_id"),
            _id(body.get("task_id"), "task_id"), _version(body.get("context_version")),
        )
        site_id = body.get("site_id")
        if require_site:
            site_id = _id(site_id, "site_id")
        elif site_id is not None:
            site_id = _id(site_id, "site_id")
        return context, site_id

    def _pose(self, value: Any) -> ReferencePose:
        if not isinstance(value, Mapping):
            raise StandalonePreviewError("INVALID_POSE", "必须提供当前无人机位姿", 422)
        _allowed(value, {"longitude", "latitude", "altitude_m", "heading_deg", "observed_at"})
        pose = ReferencePose(
            _number(value.get("longitude"), "longitude"),
            _number(value.get("latitude"), "latitude"),
            _number(value.get("altitude_m"), "altitude_m"),
            _number(value.get("heading_deg"), "heading_deg"),
            _number(value.get("observed_at"), "observed_at"),
        )
        try:
            pose.validate(self._clock())
        except StaleReferencePose as exc:
            raise StandalonePreviewError("STALE_POSE", str(exc), 409) from exc
        except ValueError as exc:
            # 位置/航向越界、字段非法是请求问题，不是「过期」。
            raise StandalonePreviewError("INVALID_POSE", str(exc), 422) from exc
        return pose

    def _agent(self, context: _Context) -> FlightPreviewAgent:
        key = context.key
        with self._lock:
            previous = self._versions.get(key)
            if previous is not None and previous != context.context_version:
                raise StandalonePreviewError("STALE_CONTEXT", "上下文版本已变化，请重新读取位姿", 409)
            self._versions[key] = context.context_version
            agent = self._agents.get(key)
            if agent is None:
                agent = FlightPreviewAgent(
                    context.binding(), self._decider_factory(), clock=self._clock,
                    ttl_sec=self._card_ttl_sec,
                )
                self._agents[key] = agent
            return agent

    def _put_pose(self, body: Mapping[str, Any], headers: Mapping[str, str], trace_id: str) -> StandaloneResponse:
        context, site_id = self._context(body, headers, require_site=False)
        pose = self._pose(body.get("pose"))
        with self._lock:
            self._poses[context.key] = pose
        return self._ok({"pose": pose.__dict__, "pose_source": "standalone-telemetry-ingest",
                         "project_id": context.project_id, "hangar_id": context.hangar_id,
                         "drone_id": context.drone_id, "task_id": context.task_id,
                         **({"site_id": site_id} if site_id else {})}, trace_id)

    def _get_pose(self, query: Mapping[str, list[str]], headers: Mapping[str, str], trace_id: str) -> StandaloneResponse:
        def q(name: str) -> Any:
            values = query.get(name, [])
            return values[-1] if values else None
        raw_version = q("context_version")
        if raw_version is None:
            context_version = 1
        else:
            try:
                context_version = int(raw_version)
            except (TypeError, ValueError) as exc:
                raise StandalonePreviewError("INVALID_REQUEST", "context_version 必须是非负整数") from exc
        body = {"project_id": q("project_id"), "hangar_id": q("hangar_id"),
                "drone_id": q("drone_id"), "task_id": q("task_id"),
                "context_version": context_version}
        context, _ = self._context(body, headers, require_site=False)
        with self._lock:
            pose = self._poses.get(context.key)
        if pose is None:
            raise StandalonePreviewError("POSE_NOT_FOUND", "尚未收到该无人机的实时位姿", 404)
        try:
            pose.validate(self._clock())
        except StaleReferencePose as exc:
            raise StandalonePreviewError("STALE_POSE", str(exc), 409) from exc
        except ValueError as exc:
            raise StandalonePreviewError("INVALID_POSE", str(exc), 422) from exc
        return self._ok({"pose": pose.__dict__, "pose_source": "standalone-telemetry",
                         "project_id": context.project_id, "hangar_id": context.hangar_id,
                         "drone_id": context.drone_id, "task_id": context.task_id}, trace_id)

    def _preview(self, body: Mapping[str, Any], headers: Mapping[str, str], trace_id: str) -> StandaloneResponse:
        context, site_id = self._context(body, headers, require_site=True)
        if not isinstance(body.get("text"), str) or not body["text"].strip():
            raise StandalonePreviewError("INVALID_REQUEST", "text 不能为空")
        command_id = _id(body.get("command_id"), "command_id")
        idempotency_key = _id(body.get("idempotency_key"), "idempotency_key")
        pose = self._pose(body.get("pose")) if body.get("pose") is not None else None
        if pose is None:
            with self._lock:
                pose = self._poses.get(context.key)
            if pose is None:
                raise StandalonePreviewError("POSE_NOT_FOUND", "尚未收到该无人机的实时位姿", 404)
            try:
                pose.validate(self._clock())
            except StaleReferencePose as exc:
                raise StandalonePreviewError("STALE_POSE", str(exc), 409) from exc
            except ValueError as exc:
                raise StandalonePreviewError("INVALID_POSE", str(exc), 422) from exc
        with self._lock:
            self._poses[context.key] = pose
        try:
            result = self._agent(context).preview(
                body["text"], command_id=command_id, idempotency_key=idempotency_key,
                site_id=site_id or "", task_id=context.task_id, pose=pose,
            )
        except StaleReferencePose as exc:
            # 模型推理期间位姿过期：已知且可重试，按契约回 409（原先落到 500 兜底）。
            raise StandalonePreviewError("STALE_POSE", str(exc), 409) from exc
        except ValueError as exc:
            # 与 AgentService 同一约定：循环抛出的 ValueError 是请求不合法 → 400。
            raise StandalonePreviewError("INVALID_REQUEST", str(exc), 400) from exc
        return self._ok(result, trace_id, run_id=command_id)

    def _finish(self, route: str, body: Mapping[str, Any], headers: Mapping[str, str], trace_id: str) -> StandaloneResponse:
        context, _ = self._context(body, headers, require_site=False)
        confirmation_id = _id(body.get("confirmation_id"), "confirmation_id")
        command_id = _id(body.get("command_id"), "command_id")
        idempotency_key = _id(body.get("idempotency_key"), "idempotency_key")
        binding = body.get("confirmation_binding")
        if not isinstance(binding, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", binding):
            raise StandalonePreviewError("INVALID_CONFIRMATION", "confirmation_binding 必须是64位摘要")
        with self._lock:
            agent = self._agents.get(context.key)
        if agent is None:
            raise StandalonePreviewError("NOT_FOUND", "预览卡不存在或已被替代", 404)
        try:
            result = agent.confirm(
                confirmation_id, command_id=command_id, idempotency_key=idempotency_key,
                confirmation_binding=binding.lower(), context_version=context.context_version,
                cancel=route == "cancel",
            )
        except ValueError as exc:
            # 卡不存在/已过期/与预览不一致都是请求问题，按同一约定回 400，而不是 500。
            raise StandalonePreviewError("INVALID_REQUEST", str(exc), 400) from exc
        return self._ok(result, trace_id, run_id=confirmation_id)

    @staticmethod
    def _ok(payload: Mapping[str, Any], trace_id: str, *, run_id: str | None = None) -> StandaloneResponse:
        value = dict(payload)
        value.setdefault("execution_mode", "preview")
        value["trace_id"] = trace_id
        value["run_id"] = run_id or value.get("command_id") or trace_id
        return StandaloneResponse(200, {"success": True, **value, "data": value})

    @staticmethod
    def _error(status_code: int, code: str, message: str, trace_id: str) -> StandaloneResponse:
        value = {"success": False, "status": "error", "error_code": code,
                 "message": message, "trace_id": trace_id,
                 "execution_mode": "preview", "dispatch_allowed": False,
                 "dispatched": False}
        return StandaloneResponse(status_code, value)


def build_standalone_server(service: StandalonePreviewService, host: str, port: int,
                            *, allowed_origin: str = "*") -> ThreadingHTTPServer:
    """把纯服务层包装成 HTTP；不含任何设备或平台传输。"""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "lingxi-standalone-preview"
        sys_version = ""

        def _dispatch(self) -> None:
            if self.command == "OPTIONS":
                self.send_response(204)
                self._cors()
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = -1
            if length < 0 or length > MAX_BODY_BYTES:
                response = StandaloneResponse(413, {"success": False, "status": "error",
                                                    "error_code": "BODY_TOO_LARGE",
                                                    "message": "请求体过大"})
            else:
                body = self.rfile.read(length) if length else b""
                response = service.handle(self.command, self.path, body,
                                          {key: value for key, value in self.headers.items()})
            encoded = json.dumps(response.payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            self.send_response(response.status_code)
            self._cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            if self.command == 'GET' and self.path.split('?', 1)[0].rstrip('/') == '/api/flight-control/visuals':
                self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(encoded)

        def _cors(self) -> None:
            self.send_header("Access-Control-Allow-Origin", allowed_origin)
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Agent-Actor, X-Trace-Id")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Vary", "Origin")

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._dispatch()

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def version_string(self) -> str:
            return self.server_version

    return ThreadingHTTPServer((host, port), Handler)
