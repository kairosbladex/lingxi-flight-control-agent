"""鉴权网关的纯函数回归（离线，不开 socket）。

socket 级联调由部署验证脚本负责：仓库的离线守护 ``tests/test_offline_guard.py``
会把整套单元测试里的 ``socket.connect`` 换成抛错，所以这里只测纯函数。
"""

import json
import os
import tempfile
import unittest

from tools.agent_gateway import (
    GatewayConfigError,
    is_public_path,
    forward_headers,
    is_origin_allowed,
    load_gateway_token,
    load_operators,
    resolve_actor,
    upstream_target,
)


class OperatorListTests(unittest.TestCase):
    def write(self, payload) -> str:
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(payload, handle)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_flat_and_wrapped_forms_both_work(self):
        flat = load_operators(self.write({"tok-a": "actor-1"}))
        self.assertEqual(flat, {"tok-a": "actor-1"})
        wrapped = load_operators(self.write({"tokens": {"tok-b": {"actor_id": "actor-2"}}}))
        self.assertEqual(wrapped, {"tok-b": "actor-2"})

    def test_bad_lists_never_start_wide_open(self):
        for payload in ({}, {"tokens": {}}, {"tok": ""}, {"tok": {"actor_id": "  "}},
                        {"tok": {"actor_id": "bad\nactor"}}, ["not-a-map"]):
            with self.assertRaises(GatewayConfigError):
                load_operators(self.write(payload))
        with self.assertRaises(GatewayConfigError):
            load_operators("/nonexistent/operators.json")


class GatewayTokenTests(unittest.TestCase):
    def test_short_or_missing_secret_is_refused(self):
        os.environ["LINGXI_TEST_TOKEN"] = "short"
        self.addCleanup(os.environ.pop, "LINGXI_TEST_TOKEN", None)
        with self.assertRaises(GatewayConfigError):
            load_gateway_token("LINGXI_TEST_TOKEN")
        with self.assertRaises(GatewayConfigError):
            load_gateway_token("LINGXI_MISSING_TOKEN")
        with self.assertRaises(GatewayConfigError):
            load_gateway_token(None)

    def test_long_secret_is_accepted(self):
        os.environ["LINGXI_TEST_TOKEN"] = "x" * 32
        self.addCleanup(os.environ.pop, "LINGXI_TEST_TOKEN", None)
        self.assertEqual(load_gateway_token("LINGXI_TEST_TOKEN"), "x" * 32)


class ResolveActorTests(unittest.TestCase):
    operators = {"tok-a": "actor-1", "tok-b": "actor-2"}

    def test_token_header_and_bearer_both_resolve(self):
        self.assertEqual(resolve_actor({"X-Operator-Token": "tok-a"}, self.operators), "actor-1")
        self.assertEqual(resolve_actor({"Authorization": "Bearer tok-b"}, self.operators), "actor-2")

    def test_unknown_or_missing_token_resolves_to_nothing(self):
        for headers in ({}, {"X-Operator-Token": "nope"}, {"X-Operator-Token": "   "},
                        {"Authorization": "Basic dXNlcjpwdw=="}):
            self.assertIsNone(resolve_actor(headers, self.operators))


class OriginTests(unittest.TestCase):
    def test_only_allowlisted_origins_pass(self):
        allowed = ("http://192.168.110.17", "http://127.0.0.1:5173")
        self.assertTrue(is_origin_allowed("http://192.168.110.17", allowed))
        self.assertTrue(is_origin_allowed("http://192.168.110.17/", allowed))
        self.assertFalse(is_origin_allowed("http://evil.example.com", allowed))
        self.assertFalse(is_origin_allowed("http://192.168.110.17.evil.com", allowed))
        self.assertFalse(is_origin_allowed(None, allowed))


class ForwardHeaderTests(unittest.TestCase):
    def test_client_identity_is_stripped_and_gateway_identity_injected(self):
        forwarded = forward_headers({
            "Content-Type": "application/json",
            "Host": "192.168.130.238:8794",
            "Content-Length": "42",
            "X-Agent-Actor": "forged-operator",
            "x-agent-gateway": "forged-secret",
            "X-Operator-Token": "tok-a",
            "Authorization": "Bearer tok-a",
            "X-Trace-Id": "trace-123",
        }, "actor-1", "real-secret")
        # 客户端自带的（含大小写变体）身份头一律丢弃，只留网关注入的那份
        self.assertEqual([key for key in forwarded if key.lower() == "x-agent-actor"],
                         ["X-Agent-Actor"])
        self.assertEqual([key for key in forwarded if key.lower() == "x-agent-gateway"],
                         ["X-Agent-Gateway"])
        self.assertEqual(forwarded["X-Agent-Actor"], "actor-1")
        self.assertEqual(forwarded["X-Agent-Gateway"], "real-secret")
        self.assertEqual(forwarded["Content-Type"], "application/json")
        self.assertEqual(forwarded["X-Trace-Id"], "trace-123")
        self.assertNotIn("X-Operator-Token", forwarded)
        self.assertNotIn("Authorization", forwarded)
        self.assertNotIn("Host", forwarded)
        self.assertNotIn("Content-Length", forwarded)

    def test_illegal_trace_id_is_replaced_and_missing_origin_never_reaches_upstream(self):
        forwarded = forward_headers({"X-Trace-Id": "bad\nvalue"}, "actor-1", "secret")
        self.assertTrue(forwarded["X-Trace-Id"].startswith("gateway-"))
        self.assertNotIn("X-Agent-Actor-forged", forwarded)


class PublicPathTests(unittest.TestCase):
    def test_only_healthz_is_public(self):
        for path in ('/healthz', '/healthz/', '/healthz?x=1', '/'):
            self.assertTrue(is_public_path(path), path)
        for path in ('/api/flight-preview/preview', '/api/flight-preview/pose', '/healthz/secret'):
            self.assertFalse(is_public_path(path), path)


class UpstreamTests(unittest.TestCase):
    def test_upstream_path_prefix_is_kept(self):
        self.assertEqual(upstream_target("http://127.0.0.1:8793", "/api/flight-preview/pose?x=1"),
                         ("127.0.0.1", "/api/flight-preview/pose?x=1", 8793))
        self.assertEqual(upstream_target("http://127.0.0.1:8793/api/flight-preview", "/pose"),
                         ("127.0.0.1", "/api/flight-preview/pose", 8793))


if __name__ == "__main__":
    unittest.main()
