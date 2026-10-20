#!/usr/bin/env python3
"""同源聚合代理：把任务下发 Agent 与飞控 Agent 挂到同一个页面入口下。

**只负责静态路由与反向代理**，不解析业务请求、不改请求体、不持有任何模型或平台
凭证。两个助手仍然是两个独立服务、两套独立会话；本代理只是让浏览器把它们看成
同一个源，从而共用一个页面。

路由表（前缀匹配，全部固定配置、不来自请求）::

    /agent-hub/            本地静态宿主页（web/agent-hub）
    /flight-agent-ui/      飞控前端（本地目录或 Vite dev 上游）
    /task-dispatch/        任务下发前端（上游，挂载到上游根路径）
    /api/flight-control/   飞控 API → 飞控服务
    /api/chat/ ...         任务下发 API → 任务下发服务

默认只监听回环地址。内网共享部署请走 nginx 或明确加 ``--allow-remote``，
并自行确认两个上游的鉴权与来源策略。

用法::

    python3 tools/agent_hub_server.py --port 8800 \\
      --flight-ui-dir ../lingxi-flight-agent-ui/dist \\
      --flight-api http://127.0.0.1:8794 \\
      --task-ui http://127.0.0.1:5055

本工具只用标准库；不含任何设备或平台传输。
"""

from __future__ import annotations

import argparse
import mimetypes
import sys
import threading
import time
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

MAX_BODY_BYTES = 8 * 1024 * 1024
UPSTREAM_TIMEOUT_SEC = 180
HOP_BY_HOP = (
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "host",
    # 由本地 HTTP 服务器重新生成，避免与上游重复。
    "server", "date",
)
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

HUB_MOUNT = "/agent-hub"
FLIGHT_UI_MOUNT = "/flight-agent-ui"
TASK_UI_MOUNT = "/task-dispatch"
FLIGHT_API_MOUNT = "/api/flight-control"
# 任务下发服务（自带页面和 API）使用的前缀；顺序无关，最长前缀先命中。
TASK_API_PREFIXES = (
    "/api/chat", "/api/health", "/api/status", "/api/settings", "/api/speech",
    "/api/algorithm", "/api/poi", "/api/region", "/api/control",
)


class HubConfigError(ValueError):
    """配置错误：宁可不启动，也不要暴露到非预期地址。"""


@dataclass(frozen=True)
class Route:
    kind: str
    path: str


def strip_mount(path: str, mount: str) -> str | None:
    """``/task-dispatch/a`` → ``/a``；``/task-dispatch`` → ``/``；不匹配返回 None。"""
    if path == mount:
        return "/"
    if path.startswith(mount + "/"):
        return path[len(mount):] or "/"
    return None


def classify(path: str) -> Route | None:
    """把请求路径映射到固定路由；未知路径一律不代理。"""
    target = strip_mount(path, HUB_MOUNT)
    if target is not None:
        return Route("hub", target)
    if strip_mount(path, FLIGHT_UI_MOUNT) is not None:
        return Route("flight-ui", path)
    target = strip_mount(path, TASK_UI_MOUNT)
    if target is not None:
        return Route("task-ui", target)
    if strip_mount(path, FLIGHT_API_MOUNT) is not None:
        return Route("flight-api", path)
    for prefix in TASK_API_PREFIXES:
        if strip_mount(path, prefix) is not None:
            return Route("task-api", path)
    return None


def safe_static_file(root: Path, relative: str) -> Path | None:
    """解析静态文件并阻止路径穿越；目录回落到 index.html。"""
    candidate = (root / relative.lstrip("/")).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    if candidate.is_dir():
        candidate = candidate / "index.html"
    return candidate if candidate.is_file() else None


def validate_binding(host: str, allow_remote: bool) -> None:
    if host in LOOPBACK_HOSTS:
        return
    if not allow_remote:
        raise HubConfigError(
            f"拒绝监听 {host}：默认只允许回环地址，内网部署请显式加 --allow-remote 或使用 nginx"
        )


def validate_upstream(value: str, label: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HubConfigError(f"{label} 必须是 http(s) 地址：{value!r}")
    return value.rstrip("/")


def upstream_target(base: str, path: str) -> tuple[str, str, int, bool]:
    parsed = urlsplit(base)
    target = (parsed.path.rstrip("/") + path) or "/"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.hostname or "127.0.0.1", target, port, parsed.scheme == "https"


def response_headers(headers: Mapping[str, str] | list[tuple[str, str]],
                     mount: str | None = None) -> list[tuple[str, str]]:
    """过滤逐跳头；可把上游重定向的挂载前缀补回去。接受 Mapping 或成对列表。"""
    items = headers.items() if isinstance(headers, Mapping) else headers
    forwarded: list[tuple[str, str]] = []
    for key, value in items:
        lowered = key.lower()
        if lowered in HOP_BY_HOP:
            continue
        if mount and lowered == "location" and value.startswith("/") and not value.startswith(mount):
            value = mount + value
        forwarded.append((key, value))
    return forwarded


def build_hub(root: Path, *, host: str = "127.0.0.1", port: int = 8800,
              flight_ui_dir: Path | None = None,
              flight_ui_upstream: str = "http://127.0.0.1:5173",
              flight_api: str = "http://127.0.0.1:8794",
              task_ui_upstream: str | None = "http://127.0.0.1:5055",
              task_ui_dir: Path | None = None,
              task_api: str | None = None,
              allow_remote: bool = False,
              log_file: str | None = None) -> ThreadingHTTPServer:
    """组装代理；不发起任何上游请求，只在请求到来时转发。"""
    validate_binding(host, allow_remote)
    if task_ui_upstream is None and task_ui_dir is None:
        raise HubConfigError("任务下发前端必须配置 --task-ui 或 --task-ui-dir 之一")
    if flight_ui_dir is None:
        flight_ui_upstream = validate_upstream(flight_ui_upstream, "飞控前端上游")
    if task_ui_upstream is not None:
        task_ui_upstream = validate_upstream(task_ui_upstream, "任务下发前端上游")
    task_api_base = validate_upstream(
        task_api or task_ui_upstream or "http://127.0.0.1:5055", "任务下发 API 上游")
    flight_api_base = validate_upstream(flight_api, "飞控 API 上游")

    flight_ui_root = flight_ui_dir.resolve() if flight_ui_dir else None
    task_ui_root = task_ui_dir.resolve() if task_ui_dir else None

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
        server_version = "lingxi-agent-hub"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _send_bytes(self, status: int, payload: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

        def _error(self, status: int, message: str) -> None:
            payload = (message + "\n").encode("utf-8")
            self._send_bytes(status, payload, "text/plain; charset=utf-8")

        def _serve_static(self, root: Path, url_path: str) -> bool:
            file_path = safe_static_file(root, url_path)
            if file_path is None:
                return False
            content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
                content_type += "; charset=utf-8"
            self._send_bytes(200, file_path.read_bytes(), content_type)
            return True

        def _proxy(self, base: str, target_path: str, mount: str | None) -> None:
            host_name, target, port_number, secure = upstream_target(base, target_path)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = -1
            if length < 0 or length > MAX_BODY_BYTES:
                return self._error(413, "请求体过大")
            body = self.rfile.read(length) if length else b""
            headers = {
                key: value for key, value in self.headers.items()
                if key.lower() not in HOP_BY_HOP
            }
            connection_class = HTTPSConnection if secure else HTTPConnection
            connection = connection_class(host_name, port_number, timeout=UPSTREAM_TIMEOUT_SEC)
            started = time.time()
            try:
                connection.request(self.command, target, body=body, headers=headers)
                response = connection.getresponse()
                payload = response.read()
            except OSError as exc:
                audit(
                    f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {self.command} {self.path} "
                    f"502 UPSTREAM_ERROR {type(exc).__name__} → {base}{target}"
                )
                return self._error(502, "上游服务不可达")
            finally:
                connection.close()
            self.send_response(response.status)
            for key, value in response_headers(response.getheaders(), mount):
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
            audit(
                f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {self.command} {self.path} "
                f"{response.status} {int((time.time() - started) * 1000)}ms → {base}{target}"
            )

        def _handle(self) -> None:
            path = urlsplit(self.path or "/").path
            route = classify(path)
            if route is None:
                return self._error(404, "未知路径：聚合入口只代理已配置的助手")
            if self.command not in {"GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"}:
                return self._error(405, "不支持的方法")
            if route.kind == "hub":
                if self._serve_static(root, route.path):
                    return
                return self._error(404, "宿主页不存在")
            if route.kind == "flight-ui":
                # 构建产物挂在 /flight-agent-ui/ 下；Vite dev 上游同样按该 base 提供。
                relative = strip_mount(path, FLIGHT_UI_MOUNT) or "/"
                if flight_ui_root is not None and self._serve_static(flight_ui_root, relative):
                    return
                if flight_ui_root is not None:
                    return self._error(404, "飞控前端资源不存在")
                return self._proxy(flight_ui_upstream, path, None)
            if route.kind == "task-ui":
                if task_ui_root is not None and self._serve_static(task_ui_root, route.path):
                    return
                if task_ui_root is not None:
                    return self._error(404, "任务下发前端资源不存在")
                return self._proxy(task_ui_upstream or "", route.path, TASK_UI_MOUNT)
            if route.kind == "flight-api":
                return self._proxy(flight_api_base, path, None)
            return self._proxy(task_api_base, path, None)

        do_GET = _handle
        do_HEAD = _handle
        do_POST = _handle
        do_PUT = _handle
        do_DELETE = _handle
        do_PATCH = _handle
        do_OPTIONS = _handle

    return ThreadingHTTPServer((host, port), Handler)


def default_hub_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "web" / "agent-hub"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="任务下发 + 飞控 Agent 同源聚合代理")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8800)
    parser.add_argument("--hub-dir", default=str(default_hub_dir()))
    parser.add_argument("--flight-ui-dir", default=None,
                        help="飞控前端构建目录；不配置则代理到 --flight-ui-upstream（Vite dev）")
    parser.add_argument("--flight-ui-upstream", default="http://127.0.0.1:5173")
    parser.add_argument("--flight-api", default="http://127.0.0.1:8794")
    parser.add_argument("--task-ui", default="http://127.0.0.1:5055",
                        help="任务下发前端上游；与 --task-ui-dir 二选一")
    parser.add_argument("--task-ui-dir", default=None,
                        help="任务下发前端构建目录；配置后优先于 --task-ui")
    parser.add_argument("--task-api", default="http://127.0.0.1:5055",
                        help="任务下发 API 上游；默认与任务下发服务同址")
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument("--log-file", default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    hub_dir = Path(args.hub_dir)
    if not (hub_dir / "index.html").is_file():
        print(f"聚合入口启动失败：找不到宿主页 {hub_dir / 'index.html'}", file=sys.stderr)
        return 2
    try:
        server = build_hub(
            hub_dir,
            host=args.host,
            port=args.port,
            flight_ui_dir=Path(args.flight_ui_dir) if args.flight_ui_dir else None,
            flight_ui_upstream=args.flight_ui_upstream,
            flight_api=args.flight_api,
            task_ui_upstream=None if args.task_ui_dir else args.task_ui,
            task_ui_dir=Path(args.task_ui_dir) if args.task_ui_dir else None,
            task_api=args.task_api,
            allow_remote=args.allow_remote,
            log_file=args.log_file,
        )
    except HubConfigError as exc:
        print(f"聚合入口启动失败：{exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"聚合入口启动失败：无法绑定 {args.host}:{args.port}（{exc}）", file=sys.stderr)
        return 2
    host, port = server.server_address[:2]
    print(f"灵犀智能助手聚合入口: http://{host}:{port}/agent-hub/", flush=True)
    print(f"  任务下发 API → {args.task_api or args.task_ui}", flush=True)
    print(f"  飞控 API     → {args.flight_api}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n收到中断，正在停止聚合入口。", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
