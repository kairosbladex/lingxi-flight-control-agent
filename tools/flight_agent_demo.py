#!/usr/bin/env python3
"""真实联调 238 上的模型：跑通「思考 -> 调工具 -> 看结果 -> 再思考」。

用法：
    # 最小执行器（relative_move / hover 两个工具）
    PYTHONPATH=src python3 tools/flight_agent_demo.py "先向前飞10米，然后原地悬停30秒"
    # 完整驾驶舱（59 个动作，带预览/确认）
    PYTHONPATH=src python3 tools/flight_agent_demo.py --cockpit "接管控制权，然后向前飞10米，再拍张照片"
不带参数时跑一组内置用例。
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from lingxi_flight_control import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    BoundedAgentLoop,
    ExecutionContext,
    LoopStatus,
    MemoryFlightControlSimulator,
    MemoryFlightSimulator,
    OllamaDecider,
    RuntimeBinding,
    SafetyFastPathDecider,
    build_full_control_tool_registry,
)


class TracingDecider:
    """包一层用于打印每一步的思考与决策。"""

    def __init__(self, inner, verbose: bool = True):
        self._inner = inner
        self._verbose = verbose
        self.step = 0

    def decide(self, user_message, checkpoint, tools):
        self.step += 1
        started = time.time()
        decision = self._inner.decide(user_message, checkpoint, tools)
        elapsed = time.time() - started
        if self._verbose:
            label = decision.kind.value if hasattr(decision.kind, "value") else decision.kind
            if decision.tool_call is not None:
                arguments = json.dumps(dict(decision.tool_call.arguments), ensure_ascii=False)
                detail = f"{decision.tool_call.name} {arguments}"
            else:
                detail = decision.message
            print(f"  [第{self.step}轮思考 {elapsed:.1f}s] {label}: {detail}")
        return decision


def _print_observations(checkpoint) -> None:
    for index, observation in enumerate(checkpoint.observations, start=1):
        payload = observation.get("result", observation.get("error"))
        summary = payload
        if isinstance(payload, dict) and "state" in payload:
            # 完整遥测太长，只留判断所需的关键字段。
            summary = {k: payload[k] for k in ("status", "action", "error_code", "message") if k in payload}
        print(f"  [观察{index}] {observation.get('tool')}: "
              f"{json.dumps(summary, ensure_ascii=False, default=str)}")


def _print_result(result, checkpoint, extra: str = "") -> None:
    print(f"{'-' * 78}")
    print(f"最终状态: {result.status.value}")
    if result.message:
        print(f"答复: {result.message}")
    executed = [c["name"] for c in (checkpoint.executed_tool_calls or [])]
    print(f"步骤数: {checkpoint.step_count}/{checkpoint.max_steps}  已执行: {executed}")
    if extra:
        print(extra)


def run_simple(decider, command: str, *, max_steps: int) -> None:
    """最小执行器路径：工具无副作用，不需要确认。"""
    simulator = MemoryFlightSimulator(ExecutionContext("exec-demo", "drone-01", 0, True))
    print(f"\n{'=' * 78}\n[最小执行器] 用户: {command}\n{'-' * 78}")
    result = BoundedAgentLoop(decider, simulator.tool_registry(), max_steps=max_steps).run(command)
    if result.status is not LoopStatus.FINAL:
        print(f"  [终止] status={result.status.value} error={result.error}")
    _print_observations(result.checkpoint)
    _print_result(result, result.checkpoint,
                  f"模拟器位置: x={simulator.position.x_m} y={simulator.position.y_m} z={simulator.position.z_m}")


def run_cockpit(decider, command: str, *, max_steps: int, auto_confirm: bool) -> None:
    """完整驾驶舱路径：每个动作都要经过预览/确认两阶段。"""
    binding = RuntimeBinding("actor-demo", "project-demo", "hangar-01", "drone-01", "batch-01", 1)
    simulator = MemoryFlightControlSimulator(binding)
    print(f"\n{'=' * 78}\n[驾驶舱] 用户: {command}\n{'-' * 78}")
    loop = BoundedAgentLoop(decider, build_full_control_tool_registry(simulator), max_steps=max_steps)

    checkpoint = None
    result = None
    for _ in range(max_steps * 4):
        result = loop.run(command, checkpoint=checkpoint, confirmed=checkpoint is not None)
        checkpoint = result.checkpoint
        if result.status is not LoopStatus.CLARIFICATION:
            break
        if auto_confirm:
            print(f"  [确认放行] {result.message}")
        else:
            print(f"  [等待人工确认] {result.message}")
            break

    if result is not None and result.status is not LoopStatus.FINAL:
        print(f"  [终止] status={result.status.value} error={result.error}")
    if checkpoint is not None:
        _print_observations(checkpoint)
        state = simulator.state
        _print_result(result, checkpoint,
                      f"控制权={state.control_owner} 高度={state.altitude_m} "
                      f"纬度={state.latitude:.6f} 经度={state.longitude:.6f} "
                      f"相机={state.camera['mode']} 照片={state.camera['photo_count']} 版本={state.revision}")


def main() -> int:
    parser = argparse.ArgumentParser(description="238 模型 + 飞控 Agent 真实联调")
    parser.add_argument("commands", nargs="*", help="自然语言指令")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--think", action="store_true", help="开启模型思考模式（更慢）")
    parser.add_argument("--cockpit", action="store_true", help="使用完整 59 动作驾驶舱")
    parser.add_argument("--no-auto-confirm", action="store_true", help="不自动放行确认")
    parser.add_argument("--max-steps", type=int, default=24)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    if args.commands:
        commands = args.commands
    elif args.cockpit:
        commands = ["接管控制权，然后向前飞10米，再拍张照片",
                    "切到红外镜头并设置测温范围-10到120度",
                    "读取当前状态",
                    "急停"]
    else:
        commands = ["向前飞10米", "先向前飞10米，然后原地悬停30秒",
                    "向前飞500米", "上升3米然后后退2米"]

    # think=None 表示不发送该字段。显式 think=False 会让 qwen3.5 的工具调用完全失效。
    decider = SafetyFastPathDecider(
        OllamaDecider(model=args.model, base_url=args.base_url,
                      think=True if args.think else None, keep_alive="30m"))
    traced = TracingDecider(decider, verbose=not args.quiet)
    print(f"模型: {args.model}   端点: {args.base_url}   模式: {'驾驶舱' if args.cockpit else '最小执行器'}")

    failures = 0
    for command in commands:
        traced.step = 0
        try:
            if args.cockpit:
                run_cockpit(traced, command, max_steps=args.max_steps,
                            auto_confirm=not args.no_auto_confirm)
            else:
                run_simple(traced, command, max_steps=args.max_steps)
        except Exception as exc:  # 联调脚本：单条失败不应中断其余用例
            failures += 1
            print(f"  [异常] {type(exc).__name__}: {exc}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
