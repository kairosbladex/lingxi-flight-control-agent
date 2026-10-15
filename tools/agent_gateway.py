#!/usr/bin/env python3
"""独立飞控预览服务的鉴权网关（内网测试用）。

**解决的问题**：预览服务原先直接读客户端自带的 ``X-Agent-Actor``，谁都能冒充
别的操作者。网关把这一步收回基础设施：

1. 预览服务只监听回环地址，外部无法直连；
2. 浏览器只跟网关说话，网关**丢弃**客户端自带的 ``X-Agent-Actor`` / ``X-Agent-Gateway``；
3. 网关按 ``X-Operator-Token``（或 ``Authorization: Bearer``）在操作者清单里查到
   ``actor_id``，再注入 ``X-Agent-Actor``，并带上与预览服务共享的网关凭证；
4. 预览服务只在凭证正确时接受该身份（见 ``standalone_preview._require_gateway``）。

CORS 也从 ``*`` 收窄为显式来源白名单。每一条请求都写一行审计日志。

用法::

    export LINGXI_GATEWAY_TOKEN=$(openssl rand -hex 32)
    python3 tools/agent_gateway.py \\
      --host 0.0.0.0 --port 8794 --upstream http://127.0.0.1:8793 \\
      --operators-file /home/user/flight-agent-preview/operators.json \\
      --allowed-origin http://192.168.110.17 \\
      --gateway-token-env LINGXI_GATEWAY_TOKEN \\
      --log-file /home/user/flight-agent-preview/logs/gateway.log

本工具只用标准库，不含任何设备或平台传输。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib.parse import urlsplit
from uuid import uuid4

MAX_BODY_BYTES = 512 * 1024
_ID = re.compile(r"^[^\x00-\x1f\x7f]{1,128}$")
# 这些头由网关注入，客户端送来的一律丢弃。
IDENTITY_HEADERS = ("x-agent-actor", "x-agent-gateway", "x-operator-token", "authorization")
# 逐跳头不转发。
HOP_BY_HOP = ("connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
              "te", "trailers", "transfer-encoding", "upgrade", "content-length", "host")
UNAUTHENTICATED = {
    "success": False, "status": "error", "error_code": "UNAUTHENTICATED",
    "message": "缺少或无效的操作者凭证，请用带 operator_token 的链接打开",
    "execution_mode": "preview", "dispatch_allowed": False, "dispatched": False,
}


class GatewayConfigError(ValueError):
    """配置错误：宁可不启动，也不要以宽松模式运行。"""


def load_operators(path: str) -> dict[str, str]:
    """读取操作者清单：``{"<token>": "<actor_id>"}`` 或 ``{"tokens": {...}}``。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as exc:
        raise GatewayConfigError(f"操作者清单不可读：{path}（{exc}）") from exc
    if not isinstance(raw, Mapping):
        raise GatewayConfigError("操作者清单必须是 JSON 对象")
    tokens = raw.get("tokens", raw)
    if not isinstance(tokens, Mapping) or not tokens:
        raise GatewayConfigError("操作者清单里没有任何 token")
    operators: dict[str, str] = {}
    for token, value in tokens.items():
        actor = value.get("actor_id") if isinstance(value, Mapping) else value
        if not isinstance(actor, str) or not _ID.fullmatch(actor.strip()):
            raise GatewayConfigError(f"token 对应的 actor_id 不合法：{actor!r}")
        if not isinstance(token, str) or not token.strip():
            raise GatewayConfigError("token 不能为空")
        operators[token] = actor.strip()
    return operators


def load_gateway_token(env_name: str | None) -> str:
    if not env_name:
        raise GatewayConfigError("必须用 --gateway-token-env 指定共享凭证来源")
    value = os.environ.get(env_name)
    if not value or len(value) < 16:
        raise GatewayConfigError(f"环境变量 {env_name} 未设置或过短（至少 16 字符）")
    return value


def resolve_actor(headers: Mapping[str, str], operators: Mapping[str, str]) -> str | None:
    """从请求头解析操作者；查不到就返回 None（调用方回 401）。"""
    token = headers.get("X-Operator-Token") or headers.get("x-operator-token") or ""
    if not token:
        authorization = headers.get("Authorization") or headers.get("authorization") or ""
        if authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
    return operators.get(token.strip()) if token else None


def is_public_path(path: str) -> bool:
    """`/healthz` 免鉴权：供守护进程/监控探活，内容只有状态与 TTL。"""
    return urlsplit(path or "").path.rstrip("/") in {"", "/healthz"}


def is_origin_allowed(origin: str | None, allowed: tuple[str, ...]) -> bool:
    if not origin:
        return False
    return origin.rstrip("/") in {item.rstrip("/") for item in allowed}


def forward_headers(headers: Mapping[str, str], actor: str, gateway_token: str) -> dict[str, str]:
    """构造转发头：丢掉客户端身份与逐跳头，注入网关注解的身份。"""
    forwarded = {
        key: value for key, value in headers.items()
        if key.lower() not in HOP_BY_HOP and key.lower() not in IDENTITY_HEADERS
    }
    trace_id = headers.get("X-Trace-Id") or headers.get("x-trace-id")
    if not trace_id or not _ID.fullmatch(str(trace_id).strip()):
        trace_id = f"gateway-{uuid4().hex}"
    forwarded["X-Trace-Id"] = str(trace_id).strip()
    forwarded["X-Agent-Actor"] = actor
    forwarded["X-Agent-Gateway"] = gateway_token
    return forwarded


def upstream_target(upstream: str, path: str) -> tuple[str, str, int]:
    parsed = urlsplit(upstream)
    host, port = parsed.hostname or "127.0.0.1", parsed.port or 80
    return host, (parsed.path.rstrip("/") + path) or "/", port


def build_gateway(host: str, port: int, *, upstream: str, operators: Mapping[str, str],
                  gateway_token: str, allowed_origins: tuple[str, ...],
                  log_file: str | None = None, max_body_bytes: int = MAX_BODY_BYTES) -> ThreadingHTTPServer:
    """把纯函数包成 HTTP 网关；不含任何设备或平台传输。"""
    if not allowed_origins:
        raise GatewayConfigError("必须至少配置一个 --allowed-origin")
    log_lock = threading.Lock()
    log_handle = open(log_file, "a", encoding="utf-8") if log_file else None

    def audit(line: str) -> None:
        with log_lock:
            if log_handle is not None:
                log_handle.write(line + "\n")
                log_handle.flush()
            else:
                print(line, flush=True)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "lingxi-flight-gateway"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _origin(self) -> str | None:
            return self.headers.get("Origin") or self.headers.get("origin")

        def _cors(self) -> None:
            origin = self._origin()
            if is_origin_allowed(origin, allowed_origins):
                self.send_header("Access-Control-Allow-Origin", str(origin).rstrip("/"))
                self.send_header("Vary", "Origin")
                # X-Agent-Actor 只是为兼容老客户端放行，转发前一律丢弃。
                self.send_header("Access-Control-Allow-Headers",
                                 "Content-Type, X-Operator-Token, X-Trace-Id, X-Agent-Actor")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

        def _json(self, status: int, payload: Mapping[str, Any]) -> None:
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self._cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(encoded)

        def _handle(self) -> None:
            started = time.time()
            origin = self._origin()
            path = self.path or "/"

            if self.command == "OPTIONS":
                if not is_origin_allowed(origin, allowed_origins):
                    return self._json(403, {"success": False, "error_code": "ORIGIN_NOT_ALLOWED",
                                            "message": "来源不在白名单内"})
                self.send_response(204)
                self._cors()
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            if origin and not is_origin_allowed(origin, allowed_origins):
                audit(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} origin={origin} {self.command} {path} 403 ORIGIN_NOT_ALLOWED")
                return self._json(403, {"success": False, "error_code": "ORIGIN_NOT_ALLOWED",
                                        "message": "来源不在白名单内"})

            actor = resolve_actor(self.headers, operators)
            if actor is None and not is_public_path(path):
                audit(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} actor=- {self.command} {path} 401 UNAUTHENTICATED")
                return self._json(401, UNAUTHENTICATED)
            if actor is None:
                actor = "anonymous-probe"

            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = -1
            if length < 0 or length > max_body_bytes:
                return self._json(413, {"success": False, "error_code": "BODY_TOO_LARGE",
                                        "message": "请求体过大"})
            body = self.rfile.read(length) if length else b""

            host_name, target, port_number = upstream_target(upstream, path)
            headers = forward_headers(self.headers, actor, gateway_token)
            connection = HTTPConnection(host_name, port_number, timeout=180)
            try:
                connection.request(self.command, target, body=body, headers=headers)
                response = connection.getresponse()
                payload = response.read()
            except OSError as exc:
                audit(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} actor={actor} {self.command} {path} 502 UPSTREAM_ERROR")
                return self._json(502, {"success": False, "error_code": "UPSTREAM_ERROR",
                                        "message": f"预览服务不可达：{type(exc).__name__}"})
            finally:
                connection.close()

            self.send_response(response.status)
            self._cors()
            self.send_header("Content-Type", response.getheader("Content-Type")
                             or "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
            audit(
                f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} actor={actor} {self.command} {path} "
                f"{response.status} {int((time.time() - started) * 1000)}ms"
            )

        do_GET = _handle
        do_POST = _handle
        do_HEAD = _handle
        do_OPTIONS = _handle

    return ThreadingHTTPServer((host, port), Handler)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="独立飞控预览服务鉴权网关")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8794)
    parser.add_argument("--upstream", default="http://127.0.0.1:8793")
    parser.add_argument("--operators-file", required=True)
    parser.add_argument("--allowed-origin", action="append", default=[])
    parser.add_argument("--gateway-token-env", required=True)
    parser.add_argument("--log-file", default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        operators = load_operators(args.operators_file)
        gateway_token = load_gateway_token(args.gateway_token_env)
        server = build_gateway(
            args.host, args.port, upstream=args.upstream, operators=operators,
            gateway_token=gateway_token, allowed_origins=tuple(args.allowed_origin),
            log_file=args.log_file,
        )
    except GatewayConfigError as exc:
        print(f"网关启动失败：{exc}", file=sys.stderr)
        return 2
    host, port = server.server_address
    print(f"飞控预览鉴权网关已启动: http://{host}:{port} → {args.upstream}", flush=True)
    print(f"操作者 {len(operators)} 个；来源白名单 {list(args.allowed_origin)}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n收到中断，正在停止网关。", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
