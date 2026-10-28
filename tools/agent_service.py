#!/usr/bin/env python3
"""把驾驶舱 Agent 作为本地 HTTP 服务跑起来（工作项 2：服务化 + 检查点持久化）。

用法：
    PYTHONPATH=src python3 tools/agent_service.py
    PYTHONPATH=src python3 tools/agent_service.py --executor loopback --port 8792

它把「一次任务跨请求续跑」所需的三块拼起来：

    OllamaDecider(238 模型) -> SafetyFastPathDecider -> BoundedAgentLoop
                                                     |-> SessionRunner
                                                         |-> FileCheckpointStore(.agent-sessions)
                                                             |-> AgentService -> ThreadingHTTPServer

HTTP 契约与幂等保证见 docs/service.md；真实 HTTP 端到端脚本见
tools/bench/service_e2e.py（它刻意不叫 test_*.py，不会进 unittest discover）。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
import sys
from typing import Any

from lingxi_flight_control import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    BoundedAgentLoop,
    LoopbackExecutor,
    MemoryFlightControlSimulator,
    OllamaDecider,
    RuntimeBinding,
    SafetyFastPathDecider,
    RuleBasedToolSelector,
    build_full_control_tool_registry,
    coerce_executor,
    project_observation_result,
)
from lingxi_flight_control.service import AgentService, build_http_server
from lingxi_flight_control.session import DEFAULT_TTL_SEC, FileCheckpointStore, SessionRunner

#: 服务默认绑定：与 tools/flight_agent_demo.py 的驾驶舱演示同一形状（actor/项目/机库/无人机/批次/版本）。
DEFAULT_BINDING = RuntimeBinding("actor-service", "project-demo", "hangar-01",
                                 "drone-01", "batch-01", 1)

DEFAULT_STORE_DIR = ".agent-sessions"
DEFAULT_PORT = 8791
DEFAULT_MAX_STEPS = 24
DEFAULT_MAX_ROUNDS = 12


@dataclass(frozen=True)
class ServiceStack:
    """一次装配的全部部件；e2e 脚本用同一个工厂，保证与服务入口同构。"""

    executor: Any
    store: FileCheckpointStore
    runner: SessionRunner
    service: AgentService


def build_executor(name: str, binding: RuntimeBinding = DEFAULT_BINDING):
    """按名字装配执行后端；返回归一化后的 `RuntimeExecutor`。"""
    if name == "simulator":
        backend = MemoryFlightControlSimulator(binding)
    elif name == "loopback":
        backend = LoopbackExecutor(binding)
    else:  # pragma: no cover - argparse 已经限定取值
        raise ValueError(f"unknown executor: {name}")
    return coerce_executor(backend)


def build_decider(args: argparse.Namespace) -> SafetyFastPathDecider:
    """真实模型决策器：安全快路径短路 + 238 端点。

    ``keep_alive="30m"`` 是共享端点（5 人共用）上的礼貌设置：模型保持加载，
    避免每个请求都重新预填充 59 个工具 schema。
    """
    brain = OllamaDecider(model=args.model, base_url=args.base_url, keep_alive="30m",
                          result_projector=project_observation_result,
                          tool_selector=RuleBasedToolSelector())
    return SafetyFastPathDecider(brain)


def build_stack(*, decider: Any, executor_name: str = "simulator",
                store_dir: str = DEFAULT_STORE_DIR, ttl_sec: float = DEFAULT_TTL_SEC,
                max_steps: int = DEFAULT_MAX_STEPS,
                max_rounds: int = DEFAULT_MAX_ROUNDS,
                binding: RuntimeBinding = DEFAULT_BINDING) -> ServiceStack:
    """装配执行器 / 存储 / 运行器 / 服务；调用方只负责起 server。"""
    os.makedirs(store_dir, exist_ok=True)
    executor = build_executor(executor_name, binding)
    store = FileCheckpointStore(store_dir, ttl_sec=ttl_sec)
    loop = BoundedAgentLoop(decider, build_full_control_tool_registry(executor),
                            max_steps=max_steps)
    runner = SessionRunner(loop, store, max_rounds=max_rounds)
    service = AgentService(runner, execution_mode=executor.execution_mode, store=store)
    return ServiceStack(executor=executor, store=store, runner=runner, service=service)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="灵犀飞控 Agent 常驻服务（HTTP）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="监听端口")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Ollama 模型名")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Ollama 端点")
    parser.add_argument("--executor", choices=("simulator", "loopback"), default="simulator",
                        help="驾驶舱执行后端（默认 simulator）")
    parser.add_argument("--store-dir", default=DEFAULT_STORE_DIR,
                        help="会话检查点目录（默认 .agent-sessions）")
    parser.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS,
                        help="单个循环的最大步数")
    parser.add_argument("--ttl-sec", type=float, default=DEFAULT_TTL_SEC,
                        help="会话 TTL 秒数（默认 1800）")
    parser.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS,
                        help="单次请求内最多驱动的轮数")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    stack = build_stack(decider=build_decider(args), executor_name=args.executor,
                        store_dir=args.store_dir, ttl_sec=args.ttl_sec,
                        max_steps=args.max_steps, max_rounds=args.max_rounds)
    server = build_http_server(stack.service, args.host, args.port)
    host, port = server.server_address[0], server.server_address[1]
    print(f"服务已启动: http://{host}:{port}"
          f"  执行模式={stack.executor.execution_mode}"
          f"  模型={args.model}  端点={args.base_url}"
          f"  会话目录={stack.store.directory}  TTL={args.ttl_sec:.0f}s"
          f"  max_steps={args.max_steps}  max_rounds={args.max_rounds}", flush=True)
    print(f"健康检查: curl -s http://{host}:{port}/healthz", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n收到中断，正在停止服务。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
