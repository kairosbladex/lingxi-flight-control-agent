#!/usr/bin/env python3
"""`keep_recent_observations` 的实测取舍：同一道复合驾驶舱指令，不同回放上限跑真实循环。

要回答的问题只有一个：把更早的观察压掉，会不会让模型**重复执行已经做过的动作**、
或者反复确认？这件事只能靠真实循环观察，合成对话证明不了。

做法：固定一条 8 动作的复合驾驶舱指令，同一台模拟器状态、同一套 59 工具，只改
`keep_recent_observations`，跑完整 `BoundedAgentLoop`（含预览/确认两阶段），记录：

- 是否 `final`、实际动作序列、是否出现重复的 (工具, 参数)；
- 墙钟、真实模型调用次数、每次决策的 prompt 估算峰值；
- 收尾检查点在每个回放上限下的**离线**估算 tokens（确定性，不发请求）。

实测结论（详见 `docs/model-selection.md`「回放上限」）：12 / 20 / 无上限在 8 动作指令上
行为一致；`回放上限=6` + 裸占位会 fail-closed 崩在收尾（3/3 复现），换成摘要后能过。

用法：
    PYTHONPATH=src python3 tools/bench/keep_recent_experiment.py [model] [caps] [max_steps] [on|off|both]
    # caps 用逗号分隔（inf/1000000 表示不设上限）；最后一个参数是省略说明：摘要/裸占位/两者都跑
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter

from lingxi_flight_control import (
    DEFAULT_KEEP_RECENT_OBSERVATIONS,
    DEFAULT_NUM_CTX,
    BoundedAgentLoop,
    LoopStatus,
    MemoryFlightControlSimulator,
    OllamaDecider,
    RuntimeBinding,
    build_full_control_tool_registry,
    estimate_prompt_tokens,
    project_observation_result,
)

BASE_URL = "http://127.0.0.1:11434"

#: 固定指令：8 个动作，覆盖 control / flight / camera / infrared / state 五个域。
#: 观察条数必须多于最小的回放上限（6），否则压缩根本不触发，实验也就测不到东西。
COMMAND = ("接管控制权，向前飞10米，向右飞5米，上升3米，拍一张照片，"
           "切到红外镜头，把测温范围设为 -10 到 120 度，最后读取当前状态")

#: 「不设上限」的写法：比任何真实观察条数都大。
UNCAPPED = 10 ** 6


class MeteredDecider:
    """统计真实模型调用次数与 prompt 估算峰值；其余原样转发。"""

    def __init__(self, inner):
        self._inner = inner
        self.calls = 0
        self.peak_estimate: int | None = None
        self.worst_eval: int | None = None
        self.done_reasons: Counter[str] = Counter()

    def decide(self, user_message, checkpoint, tools):
        self.calls += 1
        decision = self._inner.decide(user_message, checkpoint, tools)
        estimate = self._inner.last_prompt_estimate
        if estimate is not None:
            self.peak_estimate = max(self.peak_estimate or 0, estimate)
        generation = self._inner.last_generation
        self.done_reasons[str(generation["done_reason"])] += 1
        eval_count = generation["eval_count"]
        if isinstance(eval_count, int):
            self.worst_eval = max(self.worst_eval or 0, eval_count)
        return decision


def duplicates(executed) -> list[str]:
    """重复执行：同一 (工具, 参数) 出现多次。确认轮的重复提议不算——它不执行。"""
    keys = [f"{entry['name']}{json.dumps(entry['arguments'], ensure_ascii=False, sort_keys=True)}"
            for entry in executed]
    return [key for key, count in Counter(keys).items() if count > 1]


def offline_estimates(checkpoint, definitions, model, caps, *, summary: bool = True) -> list[dict]:
    """收尾检查点在每个回放上限下的确定性估算（不发请求）。"""
    rows = []
    for cap in caps:
        decider = OllamaDecider(model=model, num_ctx=DEFAULT_NUM_CTX, output_reserve=1,
                                keep_recent_observations=cap, omission_summary=summary,
                                transport=lambda *_: {})
        messages = decider.messages(COMMAND, checkpoint, definitions)
        replayed = sum(1 for message in messages if message.get("tool_calls"))
        rows.append({
            "回放上限": "无上限" if cap >= UNCAPPED else cap,
            "回放观察": f"{replayed}/{len(checkpoint.observations)}",
            "估算 tokens": estimate_prompt_tokens(messages, definitions),
        })
    return rows


def run_arm(model: str, cap: int, *, max_steps: int, summary: bool = True) -> dict:
    simulator = MemoryFlightControlSimulator(
        RuntimeBinding("actor-bench", "project-bench", "hangar-01", "drone-01", "batch-01", 1))
    registry = build_full_control_tool_registry(simulator)
    definitions = registry.definitions()
    brain = OllamaDecider(
        model=model, base_url=BASE_URL, keep_alive="30m", think=None,
        keep_recent_observations=cap, omission_summary=summary,
        result_projector=project_observation_result)
    meter = MeteredDecider(brain)
    loop = BoundedAgentLoop(meter, registry, max_steps=max_steps)

    started = time.time()
    checkpoint = None
    result = None
    for _ in range(max_steps * 4):
        result = loop.run(COMMAND, checkpoint=checkpoint, confirmed=checkpoint is not None)
        checkpoint = result.checkpoint
        if result.status is not LoopStatus.CLARIFICATION:
            break
    wall = time.time() - started

    executed = list(checkpoint.executed_tool_calls)
    last_generation = generation_summary(brain)
    arm = {
        "回放上限": "无上限" if cap >= UNCAPPED else cap,
        "省略摘要": summary,
        "结果": result.status.value,
        "错误": result.error,
        "空响应重试": brain.empty_responses,
        "步骤数": f"{checkpoint.step_count}/{checkpoint.max_steps}",
        "确认轮次": checkpoint.confirmation_rounds,
        "动作序列": [entry["name"] for entry in executed],
        "重复执行": duplicates(executed),
        "模型调用": meter.calls,
        "墙钟s": round(wall, 1),
        "估算峰值": f"{meter.peak_estimate}/{brain.prompt_budget_tokens}",
        "收尾省略观察": brain.last_omitted_observations,
        "累计压缩载荷": brain.compressed_payloads,
        "最坏生成": last_generation,
        "答复": (result.message or "").strip().replace("\n", " ")[:60],
        "离线估算": offline_estimates(checkpoint, definitions, model,
                                      [6, 12, 20, UNCAPPED], summary=summary),
    }
    return arm


def generation_summary(brain: OllamaDecider) -> dict:
    generation = brain.last_generation
    return {"eval_count": generation["eval_count"], "done_reason": generation["done_reason"],
            "prompt_eval_count": generation["prompt_eval_count"]}


def parse_summary_modes(raw: str) -> list[bool]:
    """``on`` / ``off`` / ``both``：A/B 省略说明是摘要还是裸占位。"""
    modes = {"on": [True], "off": [False], "both": [False, True]}
    if raw not in modes:
        raise SystemExit("summary 只能是 on / off / both")
    return modes[raw]


def parse_caps(raw: str) -> list[int]:
    caps: list[int] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        caps.append(UNCAPPED if item.lower() in {"inf", "infinity", "none"} else int(item))
    if not caps:
        raise SystemExit("至少给一个回放上限")
    return caps


def main() -> int:
    model = sys.argv[1] if len(sys.argv) > 1 else "qwen3.5:9b"
    caps = parse_caps(sys.argv[2]) if len(sys.argv) > 2 else [
        6, DEFAULT_KEEP_RECENT_OBSERVATIONS, 20, UNCAPPED]
    max_steps = int(sys.argv[3]) if len(sys.argv) > 3 else 24
    summaries = parse_summary_modes(sys.argv[4]) if len(sys.argv) > 4 else [True]
    print(f"模型={model}  指令={COMMAND}  max_steps={max_steps}  "
          f"回放上限={caps}  省略摘要={summaries}")
    for summary in summaries:
        for cap in caps:
            try:
                arm = run_arm(model, cap, max_steps=max_steps, summary=summary)
            except Exception as exc:  # 联调：单档失败不该掩盖其余数据
                arm = {"回放上限": "无上限" if cap >= UNCAPPED else cap,
                       "省略摘要": summary,
                       "异常": f"{type(exc).__name__}: {exc}"}
            print(json.dumps(arm, ensure_ascii=False))
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
