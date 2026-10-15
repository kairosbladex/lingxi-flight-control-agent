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
    FixtureExecutor,
    LoopStatus,
    LoopbackExecutor,
    MemoryFlightControlSimulator,
    MemoryFlightSimulator,
    OllamaDecider,
    RuntimeBinding,
    SafetyFastPathDecider,
    Telemetry,
    build_full_control_tool_registry,
    coerce_executor,
    load_fixture,
    project_observation_result,
)

#: 夹具执行器的默认夹具（source=simulator-export，不是真机录制）。
DEFAULT_FIXTURE = "fixtures/cockpit_basic.json"


class TracingDecider:
    """包一层用于打印每一步的思考与决策。"""

    def __init__(self, inner, verbose: bool = True, meter=None):
        self._inner = inner
        self._verbose = verbose
        # 原始 OllamaDecider：用它读 prompt 预算遥测（装饰器只转发，不暴露这些读数）。
        self._meter = meter
        self.step = 0
        #: 本次命令里模型真实生成的读数；安全快路径短路时不产生新响应，所以按对象身份判断。
        self.generations = 0
        self.worst_generation: dict | None = None

    def decide(self, user_message, checkpoint, tools):
        self.step += 1
        if self._meter is not None:
            # 安全快路径不发模型调用，此时读数是上一轮的，先清掉免得打印陈旧预算。
            self._meter.last_prompt_estimate = None
        previous_response = self._meter.last_raw_response if self._meter is not None else None
        started = time.time()
        decision = self._inner.decide(user_message, checkpoint, tools)
        elapsed = time.time() - started
        generation = None
        if self._meter is not None and self._meter.last_raw_response is not previous_response:
            generation = self._meter.last_generation
            self._track_generation(generation)
        if self._verbose:
            label = decision.kind.value if hasattr(decision.kind, "value") else decision.kind
            if decision.tool_call is not None:
                arguments = json.dumps(dict(decision.tool_call.arguments), ensure_ascii=False)
                detail = f"{decision.tool_call.name} {arguments}"
            else:
                detail = decision.message
            print(f"  [第{self.step}轮思考 {elapsed:.1f}s] {label}: {detail}")
            if self._meter is not None and self._meter.last_prompt_estimate is not None:
                print(f"      [prompt预算] 估算 {self._meter.last_prompt_estimate}"
                      f"/{self._meter.prompt_budget_tokens} tokens"
                      f"  回放省略观察 {self._meter.last_omitted_observations} 条")
            if generation is not None:
                print(f"      [生成] done_reason={generation['done_reason']}"
                      f"  eval_count={generation['eval_count']}")
        return decision

    def _track_generation(self, generation) -> None:
        """记录最坏一次生成：``output_reserve`` 只由它来定。"""
        if generation.get("eval_count") is None and generation.get("done_reason") is None:
            return
        self.generations += 1
        current = self.worst_generation
        if current is None or (generation.get("eval_count") or 0) > (current.get("eval_count") or 0):
            self.worst_generation = dict(generation)

    def report_generation(self) -> str:
        if self.worst_generation is None:
            return "  [生成] 本命令无模型生成（安全快路径短路）"
        return (f"  [生成最坏] eval_count={self.worst_generation['eval_count']}"
                f"  done_reason={self.worst_generation['done_reason']}"
                f"  （{self.generations} 次真实生成）")

    def report_budget(self) -> str:
        if self._meter is None:
            return ""
        return (f"  [prompt预算累计] 压缩载荷 {self._meter.compressed_payloads} 次, "
                f"省略观察 {self._meter.omitted_observation_turns} 条")


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


def run_simple(decider, command: str, *, max_steps: int, telemetry=None) -> None:
    """最小执行器路径：工具无副作用，不需要确认。"""
    simulator = MemoryFlightSimulator(ExecutionContext("exec-demo", "drone-01", 0, True))
    print(f"\n{'=' * 78}\n[最小执行器] 用户: {command}\n{'-' * 78}")
    result = BoundedAgentLoop(decider, simulator.tool_registry(), max_steps=max_steps,
                              telemetry=telemetry).run(command)
    if result.status is not LoopStatus.FINAL:
        print(f"  [终止] status={result.status.value} error={result.error}")
    _print_observations(result.checkpoint)
    _print_result(result, result.checkpoint,
                  f"模拟器位置: x={simulator.position.x_m} y={simulator.position.y_m} z={simulator.position.z_m}")


def build_cockpit_executor(name: str, binding: RuntimeBinding, fixture_path: str):
    """按名字装配执行后端；返回归一化后的 RuntimeExecutor。

    ``simulator`` 是零行为变化的默认路径；``loopback`` 用同一套握手换一个模式名，
    用来证明执行器挂点真的通了；``fixture`` 只回放夹具里录到的动作，其余一律拒绝。
    """
    if name == "simulator":
        backend = MemoryFlightControlSimulator(binding)
    elif name == "loopback":
        backend = LoopbackExecutor(binding)
    elif name == "fixture":
        backend = FixtureExecutor(load_fixture(fixture_path))
    else:  # pragma: no cover - argparse 已经限定取值
        raise ValueError(f"unknown executor: {name}")
    return coerce_executor(backend)


def run_cockpit(decider, command: str, *, max_steps: int, auto_confirm: bool,
                executor_name: str = "simulator", fixture_path: str = DEFAULT_FIXTURE,
                telemetry=None) -> None:
    """完整驾驶舱路径：每个动作都要经过预览/确认两阶段。"""
    binding = RuntimeBinding("actor-demo", "project-demo", "hangar-01", "drone-01", "batch-01", 1)
    executor = build_cockpit_executor(executor_name, binding, fixture_path)
    print(f"\n{'=' * 78}\n[驾驶舱/{executor.execution_mode}] 用户: {command}\n{'-' * 78}")
    if executor_name == "fixture":
        print(f"  [夹具] {fixture_path} source={executor.fixture_source}："
              f"只回放录到的动作，其余动作会被 FIXTURE_NOT_FOUND 拒绝")
    loop = BoundedAgentLoop(decider, build_full_control_tool_registry(executor),
                            max_steps=max_steps, telemetry=telemetry)

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
        state = executor.read_state()
        camera = state.get("camera") or {}
        _print_result(result, checkpoint,
                      f"模式={executor.execution_mode} 控制权={state.get('control_owner')} "
                      f"高度={state.get('altitude_m')} "
                      f"纬度={state.get('latitude', 0.0):.6f} 经度={state.get('longitude', 0.0):.6f} "
                      f"相机={camera.get('mode')} 照片={camera.get('photo_count')} "
                      f"版本={state.get('revision')}")


def main() -> int:
    parser = argparse.ArgumentParser(description="238 模型 + 飞控 Agent 真实联调")
    parser.add_argument("commands", nargs="*", help="自然语言指令")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--think", action="store_true", help="开启模型思考模式（更慢）")
    parser.add_argument("--cockpit", action="store_true", help="使用完整 59 动作驾驶舱")
    parser.add_argument("--executor", choices=("simulator", "loopback", "fixture"),
                        default="simulator", help="驾驶舱执行后端（默认 simulator）")
    parser.add_argument("--fixture", default=DEFAULT_FIXTURE, help="--executor fixture 使用的夹具路径")
    parser.add_argument("--no-auto-confirm", action="store_true", help="不自动放行确认")
    parser.add_argument("--max-steps", type=int, default=24)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--telemetry", action="store_true",
                        help="打开结构化遥测（stdout JSON line，一行一个事件）")
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
    # 回执按域投影：非驾驶舱工具不认识，会原样回放，所以最小执行器路径同样安全。
    telemetry = Telemetry() if args.telemetry else None
    brain = OllamaDecider(model=args.model, base_url=args.base_url,
                          think=True if args.think else None, keep_alive="30m",
                          result_projector=project_observation_result,
                          telemetry=telemetry)
    decider = SafetyFastPathDecider(brain)
    traced = TracingDecider(decider, verbose=not args.quiet, meter=brain)
    print(f"模型: {args.model}   端点: {args.base_url}   模式: {'驾驶舱' if args.cockpit else '最小执行器'}")

    failures = 0
    for command in commands:
        traced.step = 0
        traced.generations = 0
        traced.worst_generation = None
        try:
            if args.cockpit:
                run_cockpit(traced, command, max_steps=args.max_steps,
                            auto_confirm=not args.no_auto_confirm,
                            executor_name=args.executor, fixture_path=args.fixture,
                            telemetry=telemetry)
            else:
                run_simple(traced, command, max_steps=args.max_steps, telemetry=telemetry)
        except Exception as exc:  # 联调脚本：单条失败不应中断其余用例
            failures += 1
            print(f"  [异常] {type(exc).__name__}: {exc}")
        if not args.quiet:
            print(traced.report_generation())
            budget = traced.report_budget()
            if budget:
                print(budget)
            if telemetry is not None:
                counters = telemetry.counters()
                print(f"  [遥测] 事件 {counters['emitted']}/{counters['submitted']} 条"
                      f"  sink 异常 {counters['sink_errors']}"
                      f"  字段违约 {counters['schema_errors']}"
                      f"  内部异常 {counters['internal_errors']}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
