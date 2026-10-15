#!/usr/bin/env python3
"""真实 HTTP 端到端：两个请求跑通驾驶舱基线任务，并证明重复提交不会重复执行。

**这不是 unittest 模块**（文件名不是 test_*.py，目录也不在 discover 范围内），
所以它可以真的起 `http.server`、真的走 socket。

覆盖的正是工作项 2 的核心承诺：

1. 第一个请求 `confirmed=false` 只做确认握手，状态停在 clarification；
2. 第二个请求 `confirmed=true` 续跑到 final，动作序列恰为
   ``["control_acquire", "move_forward", "camera_photo"]``；
3. 把第二个请求**原样重发一次**（同 request_id），响应必须逐字段相同，
   且模拟器状态（revision / photo_count）不得变化——没有第二次副作用。

可选 `--restart-between-requests` 会在两个请求之间把服务整段重启（新的执行器、
新的运行器、新的 HTTP server），只有会话目录是共享的，用来实测「跨进程续跑」。

用法：
    # 离线自检（不需要模型，确定性脚本决策器）
    PYTHONPATH=src python3 tools/bench/service_e2e.py --offline-scripted
    # 跨进程续跑实测（离线）
    PYTHONPATH=src python3 tools/bench/service_e2e.py --offline-scripted --restart-between-requests
    # 真实模型（共享 238 端点，串行、900s 超时、keep_alive=30m）
    PYTHONPATH=src python3 tools/bench/service_e2e.py --model <name> --base-url <url>

退出码：final 状态或动作序列不符即非 0。
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import tempfile
import threading
from typing import Any

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from agent_service import (  # noqa: E402 - 需要先把 tools/ 放进 sys.path
    DEFAULT_BINDING,
    DEFAULT_MAX_ROUNDS,
    DEFAULT_MAX_STEPS,
    DEFAULT_STORE_DIR,
    build_decider,
    build_stack,
)
from lingxi_flight_control import DecisionKind, ModelDecision, ToolCall  # noqa: E402
from lingxi_flight_control.service import build_http_server  # noqa: E402

COMMAND = "接管控制权，然后向前飞10米，再拍张照片"
EXPECTED_EXECUTED = ["control_acquire", "move_forward", "camera_photo"]
SESSION_ID = "bench-cockpit-baseline"
#: 共享模型端点：宁可等久一点，也不要因为超时把一次成功的长循环判成失败。
DEFAULT_TIMEOUT_SEC = 900.0


class ScriptedDecider:
    """确定性脚本决策器（--offline-scripted 用），语义与离线单测里的同构。

    待确认的调用在「观察数没变」时会重发，这样 `confirmation_binding` 才匹配得上、
    动作才会真正执行——真实模型遵循同一契约。
    """

    def __init__(self, decisions):
        self._decisions = list(decisions)
        self._pending: ModelDecision | None = None
        self._pending_at: int | None = None
        self.calls = 0

    def decide(self, _user_message, checkpoint, _tools) -> ModelDecision:
        self.calls += 1
        observed = len(checkpoint.observations)
        if self._pending is not None and observed == self._pending_at:
            return self._pending
        self._pending = None
        if not self._decisions:
            return ModelDecision(DecisionKind.FINAL, message="已完成。")
        decision = self._decisions.pop(0)
        if decision.tool_call is not None:
            self._pending = decision
            self._pending_at = observed
        return decision


def scripted_cockpit_decider() -> ScriptedDecider:
    def call(name: str, **arguments) -> ModelDecision:
        return ModelDecision(DecisionKind.TOOL_CALL, tool_call=ToolCall(name, arguments))

    return ScriptedDecider([
        call("control_acquire"),
        call("move_forward", distance_m=10),
        call("camera_photo"),
        ModelDecision(DecisionKind.FINAL, message="已接管控制权、前进10米并拍照。"),
    ])


class ServiceProcess:
    """一次「进程」：自己的执行器、运行器与会话目录；只有文件是跨进程共享的。"""

    def __init__(self, decider: Any, args: argparse.Namespace, store_dir: str):
        self.stack = build_stack(decider=decider, executor_name=args.executor,
                                 store_dir=store_dir, max_steps=args.max_steps,
                                 max_rounds=args.max_rounds, binding=DEFAULT_BINDING)
        self.server = build_http_server(self.stack.service, args.host, args.port)
        self.host, self.port = self.server.server_address[0], self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever,
                                        name="agent-service-e2e", daemon=True)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5.0)

    def connect(self, timeout: float) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def read_state(self) -> dict[str, Any]:
        return dict(self.stack.executor.read_state())


def request_json(connection: http.client.HTTPConnection, method: str, path: str,
                 payload: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {} if body is None else {"Content-Type": "application/json"}
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    raw = response.read().decode("utf-8")
    try:
        decoded = json.loads(raw)
    except ValueError:
        decoded = {"raw": raw}
    return response.status, decoded


def post(connection: http.client.HTTPConnection, session_id: str,
         payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return request_json(connection, "POST", f"/sessions/{session_id}/messages", payload)


def _state_digest(state: dict[str, Any]) -> dict[str, Any]:
    camera = state.get("camera") or {}
    return {
        "revision": state.get("revision"),
        "photo_count": camera.get("photo_count"),
        "control_owner": state.get("control_owner"),
        "latitude": state.get("latitude"),
        "longitude": state.get("longitude"),
        "last_action": state.get("last_action"),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Agent 服务真实 HTTP 端到端（两个请求 + 幂等重放）")
    parser.add_argument("--model", default=None, help="Ollama 模型名（离线模式忽略）")
    parser.add_argument("--base-url", default=None, help="Ollama 端点（离线模式忽略）")
    parser.add_argument("--executor", choices=("simulator", "loopback"), default="simulator")
    parser.add_argument("--offline-scripted", action="store_true",
                        help="用确定性脚本决策器替代真实模型（不需要模型即可自检）")
    parser.add_argument("--restart-between-requests", action="store_true",
                        help="两个请求之间整段重启服务，实测跨进程续跑")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="0 表示自动挑一个空闲端口")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SEC)
    parser.add_argument("--store-dir", default=DEFAULT_STORE_DIR)
    parser.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    parser.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS)
    parser.add_argument("--keep-store", action="store_true",
                        help="保留会话目录（默认用临时目录并在结束时删除）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    temporary: Any = None
    store_dir = args.store_dir
    if not args.keep_store and store_dir == DEFAULT_STORE_DIR:
        temporary = tempfile.TemporaryDirectory(prefix="agent-service-e2e-")
        store_dir = temporary.name

    if args.offline_scripted:
        make_decider = scripted_cockpit_decider
        mode = "离线脚本决策器"
    else:
        if not args.model or not args.base_url:
            from lingxi_flight_control import DEFAULT_BASE_URL, DEFAULT_MODEL
            args.model = args.model or DEFAULT_MODEL
            args.base_url = args.base_url or DEFAULT_BASE_URL

        def make_decider():
            return build_decider(args)

        mode = f"真实模型 {args.model} @ {args.base_url}"

    failures: list[str] = []
    process = ServiceProcess(make_decider(), args, store_dir)
    process.start()
    print(f"端到端模式: {mode}")
    print(f"执行后端: {process.stack.executor.execution_mode}   会话目录: {process.stack.store.directory}")
    print(f"监听: {process.url}   超时: {args.timeout:.0f}s")
    print(f"命令: {COMMAND}")
    print(f"预期动作序列: {EXPECTED_EXECUTED}")
    print("-" * 78)

    connection = process.connect(args.timeout)
    try:
        first_payload = {"text": COMMAND, "confirmed": False, "request_id": "req-1"}
        status_1, response_1 = post(connection, SESSION_ID, first_payload)
        print(f"[请求1] POST /sessions/{SESSION_ID}/messages  HTTP {status_1}")
        print(f"        请求体: {json.dumps(first_payload, ensure_ascii=False)}")
        print(f"        响应: {json.dumps(response_1, ensure_ascii=False, sort_keys=True)}")
        if response_1.get("status") != "clarification":
            failures.append(f"请求1 期望 clarification，实际 {response_1.get('status')!r}")

        if args.restart_between_requests:
            connection.close()
            process.stop()
            print("[重启] 关闭服务：新的执行器 + 新的运行器 + 新的 HTTP server，"
                  "只有会话目录是共享的")
            process = ServiceProcess(make_decider(), args, store_dir)
            process.start()
            connection = process.connect(args.timeout)
            health = request_json(connection, "GET", "/healthz")
            summary = request_json(connection, "GET", f"/sessions/{SESSION_ID}")
            print(f"[重启后] 监听: {process.url}")
            print(f"[重启后] GET /healthz -> HTTP {health[0]}: "
                  f"{json.dumps(health[1], ensure_ascii=False, sort_keys=True)}")
            print(f"[重启后] GET /sessions/{SESSION_ID} -> HTTP {summary[0]}: "
                  f"{json.dumps(summary[1], ensure_ascii=False, sort_keys=True)}")

        second_payload = {"text": COMMAND, "confirmed": True, "request_id": "req-2"}
        status_2, response_2 = post(connection, SESSION_ID, second_payload)
        print(f"[请求2] POST /sessions/{SESSION_ID}/messages  HTTP {status_2}")
        print(f"        请求体: {json.dumps(second_payload, ensure_ascii=False)}")
        print(f"        响应: {json.dumps(response_2, ensure_ascii=False, sort_keys=True)}")
        if response_2.get("status") != "final":
            failures.append(f"请求2 期望 final，实际 {response_2.get('status')!r}")

        executed = response_2.get("executed")
        print(f"已执行动作序列: {executed}")
        if executed != EXPECTED_EXECUTED:
            failures.append(f"动作序列期望 {EXPECTED_EXECUTED}，实际 {executed!r}")

        state_before = process.read_state()
        status_3, response_3 = post(connection, SESSION_ID, second_payload)
        state_after = process.read_state()
        print(f"[请求3] 原样重发请求2（同 request_id=req-2）  HTTP {status_3}")
        print(f"        响应: {json.dumps(response_3, ensure_ascii=False, sort_keys=True)}")
        identical = response_3 == response_2
        print(f"响应是否逐字段相同: {identical}")
        if not identical:
            failures.append("重发同一 request_id 的响应与首次不同（幂等回放失效）")
        digest_before = _state_digest(state_before)
        digest_after = _state_digest(state_after)
        print(f"重发前状态: {json.dumps(digest_before, ensure_ascii=False, sort_keys=True)}")
        print(f"重发后状态: {json.dumps(digest_after, ensure_ascii=False, sort_keys=True)}")
        if digest_before != digest_after:
            failures.append("重发同一 request_id 改变了模拟器状态（出现了第二次副作用）")

        print("-" * 78)
        print(f"模拟器状态: {json.dumps(state_after, ensure_ascii=False, sort_keys=True)}")
    finally:
        connection.close()
        process.stop()
        if temporary is not None:
            temporary.cleanup()

    if failures:
        print("端到端失败:")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("端到端通过: two-request resume + idempotent replay OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
