"""聚合宿主页与同源代理的离线契约测试。

不启动任何 socket：只验证路由分类、静态路径穿越防护、上游地址校验、
响应头处理和宿主页的隔离约定。真实转发在隔离实例中手工验收。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from tools.agent_hub_server import (
    FLIGHT_API_MOUNT,
    FLIGHT_UI_MOUNT,
    HUB_MOUNT,
    TASK_UI_MOUNT,
    HubConfigError,
    classify,
    response_headers,
    safe_static_file,
    strip_mount,
    upstream_target,
    validate_binding,
    validate_upstream,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
HUB_HTML = REPO_ROOT / "web" / "agent-hub" / "index.html"


class StripMountTests(unittest.TestCase):
    def test_exact_mount_becomes_root(self) -> None:
        self.assertEqual(strip_mount("/task-dispatch", TASK_UI_MOUNT), "/")

    def test_prefixed_path_is_stripped(self) -> None:
        self.assertEqual(strip_mount("/task-dispatch/api/x", TASK_UI_MOUNT), "/api/x")

    def test_similar_prefix_is_not_matched(self) -> None:
        self.assertIsNone(strip_mount("/task-dispatchx", TASK_UI_MOUNT))
        self.assertIsNone(strip_mount("/agent-hub-extra/a", HUB_MOUNT))


class ClassifyTests(unittest.TestCase):
    def test_hub_assets_are_local(self) -> None:
        self.assertEqual(classify("/agent-hub/").kind, "hub")
        self.assertEqual(classify("/agent-hub/index.html").path, "/index.html")

    def test_flight_ui_keeps_full_path(self) -> None:
        route = classify("/flight-agent-ui/assets/app.js")
        self.assertEqual(route.kind, "flight-ui")
        self.assertEqual(route.path, "/flight-agent-ui/assets/app.js")

    def test_task_ui_is_stripped(self) -> None:
        self.assertEqual(classify("/task-dispatch/").path, "/")
        self.assertEqual(classify("/task-dispatch/favicon.svg").path, "/favicon.svg")

    def test_flight_api_is_not_captured_by_task_api(self) -> None:
        route = classify("/api/flight-control/agent/messages")
        self.assertEqual(route.kind, "flight-api")

    def test_task_api_prefixes(self) -> None:
        for path in ("/api/chat/ground", "/api/health", "/api/settings/mode",
                     "/api/speech/stt", "/api/status"):
            self.assertEqual(classify(path).kind, "task-api", path)

    def test_unknown_and_lookalike_paths_are_not_proxied(self) -> None:
        for path in ("/", "/index.html", "/api/flight-controlx", "/api/chatx",
                     "/static/app.js", "/admin"):
            self.assertIsNone(classify(path), path)

    def test_mount_constants_stay_stable(self) -> None:
        self.assertEqual(HUB_MOUNT, "/agent-hub")
        self.assertEqual(FLIGHT_UI_MOUNT, "/flight-agent-ui")
        self.assertEqual(TASK_UI_MOUNT, "/task-dispatch")
        self.assertEqual(FLIGHT_API_MOUNT, "/api/flight-control")


class StaticFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = REPO_ROOT / "web" / "agent-hub"

    def test_index_is_served_for_directory(self) -> None:
        self.assertEqual(safe_static_file(self.root, "/"), self.root.resolve() / "index.html")

    def test_index_is_served_for_empty_path(self) -> None:
        self.assertEqual(safe_static_file(self.root, ""), self.root.resolve() / "index.html")

    def test_traversal_is_rejected(self) -> None:
        for relative in ("/../tools/agent_hub_server.py", "/%2e%2e/README.md",
                         "/../../README.md", "/a/../../README.md"):
            self.assertIsNone(safe_static_file(self.root, relative), relative)

    def test_missing_file_returns_none(self) -> None:
        self.assertIsNone(safe_static_file(self.root, "/missing.js"))


class UpstreamTests(unittest.TestCase):
    def test_only_loopback_is_default(self) -> None:
        for host in ("127.0.0.1", "localhost", "::1"):
            validate_binding(host, allow_remote=False)
        with self.assertRaises(HubConfigError):
            validate_binding("0.0.0.0", allow_remote=False)
        validate_binding("0.0.0.0", allow_remote=True)

    def test_upstream_requires_http(self) -> None:
        self.assertEqual(validate_upstream("http://127.0.0.1:5055/", "x"),
                         "http://127.0.0.1:5055")
        for value in ("file:///etc/passwd", "ftp://host/x", "127.0.0.1:5055", ""):
            with self.assertRaises(HubConfigError, msg=value):
                validate_upstream(value, "x")

    def test_target_keeps_base_path_and_port(self) -> None:
        self.assertEqual(
            upstream_target("http://127.0.0.1:5055", "/api/chat/ground"),
            ("127.0.0.1", "/api/chat/ground", 5055, False))
        self.assertEqual(
            upstream_target("https://agent.local/base", "/api/x"),
            ("agent.local", "/base/api/x", 443, True))

    def test_response_headers_drop_hop_by_hop(self) -> None:
        headers = [
            ("Content-Type", "application/json"),
            ("Connection", "keep-alive"),
            ("Transfer-Encoding", "chunked"),
            ("Content-Length", "10"),
            ("Set-Cookie", "a=b"),
        ]
        self.assertEqual(
            response_headers(headers),
            [("Content-Type", "application/json"), ("Set-Cookie", "a=b")])

    def test_redirect_location_gets_mount_prefix(self) -> None:
        self.assertEqual(
            response_headers([("Location", "/login")], TASK_UI_MOUNT),
            [("Location", "/task-dispatch/login")])
        self.assertEqual(
            response_headers([("Location", "/task-dispatch/login")], TASK_UI_MOUNT),
            [("Location", "/task-dispatch/login")])


class HostPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = HUB_HTML.read_text(encoding="utf-8")

    def test_two_iframes_are_present(self) -> None:
        self.assertIn('id="view-task"', self.html)
        self.assertIn('id="view-flight"', self.html)
        self.assertIn("任务下发助手", self.html)
        self.assertIn("飞行控制助手", self.html)

    def test_sources_default_to_same_origin_mounts(self) -> None:
        self.assertIn("task: '/task-dispatch/'", self.html)
        self.assertIn("flight: '/flight-agent-ui/'", self.html)

    def test_iframes_are_lazy_and_stateful(self) -> None:
        self.assertIn("dataset.loaded", self.html)
        self.assertIn("frame.src = sources[view]", self.html)

    def test_no_sandbox_or_cross_frame_messaging(self) -> None:
        self.assertNotIn("<iframe sandbox", self.html.lower())
        self.assertNotIn("postMessage", self.html)
        self.assertNotIn("contentWindow", self.html)

    def test_source_scheme_is_restricted(self) -> None:
        self.assertIn("url.protocol !== 'http:'", self.html)
        self.assertIn("url.protocol !== 'https:'", self.html)

    def test_microphone_is_delegated_only_to_task_assistant(self) -> None:
        task_tag = self.html.split('id="view-task"', 1)[1].split(">", 1)[0]
        flight_tag = self.html.split('id="view-flight"', 1)[1].split(">", 1)[0]
        self.assertIn("microphone", task_tag)
        self.assertNotIn("microphone", flight_tag)


if __name__ == "__main__":
    unittest.main()
