#!/usr/bin/env python3
"""生成预留（``output_reserve``）的实测依据：真实模型一次决策最坏要生成多少 token。

`prompt + 生成` 必须一起装进 `num_ctx`，所以预算要给生成留出 `output_reserve`。
这个数只能实测：留小了会出现 `done_reason="length"`（生成被截断，响应不可用），
留大了白白压低 prompt 预算。

做法：用**生产同一条路径**（`OllamaDecider.payload()`：同一 system prompt、同一套 59 工具、
同一 `num_ctx`、同一预算闸门与投影）发几条代表性请求，记录每条的真实生成读数。

用法：
    PYTHONPATH=src python3 tools/bench/generation_reserve.py [model] [base_url]
"""

from __future__ import annotations

import json
import sys

from lingxi_flight_control import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DEFAULT_NUM_CTX,
    DEFAULT_OUTPUT_RESERVE_TOKENS,
    AgentCheckpoint,
    MemoryFlightControlSimulator,
    OllamaDecider,
    RuntimeBinding,
    build_full_control_tool_registry,
    generation_telemetry,
    http_transport,
    project_observation_result,
)

#: 代表性请求。覆盖：单动作、8 动作复合规划、状态全量汇报、歧义澄清、长规划思考。
#: 最后两条专门用来把「思考」的尾巴顶出来——eval_count 含 thinking。
PROMPTS = {
    "单动作": "接管控制权",
    "8动作复合": ("接管控制权，向前飞10米，向右飞5米，上升3米，拍一张照片，"
                  "切到红外镜头，把测温范围设为 -10 到 120 度，最后读取当前状态"),
    "状态汇报": "读取当前状态",
    "歧义澄清": "做测绘任务",
    "长规划思考": "我要巡检一条 3 公里长的输电线路，请规划完整飞行流程："
                  "高度、速度、相机模式、拍摄间隔分别怎么设，并说明每一步的判断依据。",
}


def main() -> int:
    model = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MODEL
    base_url = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_BASE_URL

    simulator = MemoryFlightControlSimulator(
        RuntimeBinding("actor-bench", "project-bench", "hangar-01", "drone-01", "batch-01", 1))
    definitions = build_full_control_tool_registry(simulator).definitions()
    brain = OllamaDecider(model=model, base_url=base_url, keep_alive="30m", think=None,
                          num_ctx=DEFAULT_NUM_CTX, result_projector=project_observation_result)
    endpoint = base_url.rstrip("/") + "/api/chat"
    checkpoint = AgentCheckpoint(user_input="", context={}, messages=[], tool_calls=[],
                                 observations=[])

    print(f"模型={model}  num_ctx={DEFAULT_NUM_CTX}  "
          f"output_reserve={DEFAULT_OUTPUT_RESERVE_TOKENS}  "
          f"prompt预算={brain.prompt_budget_tokens}  工具数={len(definitions)}")
    worst = {"eval_count": 0, "done_reason": None, "case": None}
    truncated: list[str] = []
    for label, prompt in PROMPTS.items():
        body = brain.payload(prompt, checkpoint, definitions)
        response = http_transport(endpoint, body, 600.0)
        telemetry = generation_telemetry(response)
        message = response.get("message") or {}
        row = {
            "场景": label,
            "prompt估算": brain.last_prompt_estimate,
            "实测 prompt_eval_count": telemetry["prompt_eval_count"],
            "实测 eval_count": telemetry["eval_count"],
            "实测 done_reason": telemetry["done_reason"],
            "答复字数": len((message.get("content") or "").strip()),
            "工具调用": [call.get("function", {}).get("name")
                      for call in (message.get("tool_calls") or [])],
        }
        print(json.dumps(row, ensure_ascii=False))
        sys.stdout.flush()
        eval_count = telemetry["eval_count"]
        if telemetry["done_reason"] == "length":
            truncated.append(label)
        if isinstance(eval_count, int) and eval_count > worst["eval_count"]:
            worst = {"eval_count": eval_count, "done_reason": telemetry["done_reason"],
                     "case": label}

    utilization = (100.0 * worst["eval_count"] / DEFAULT_OUTPUT_RESERVE_TOKENS
                   if DEFAULT_OUTPUT_RESERVE_TOKENS else None)
    print(json.dumps({
        "最坏生成": worst,
        "预留": DEFAULT_OUTPUT_RESERVE_TOKENS,
        "预留占用率%": round(utilization, 1) if utilization is not None else None,
        "余量倍数": round(DEFAULT_OUTPUT_RESERVE_TOKENS / worst["eval_count"], 1)
        if worst["eval_count"] else None,
        "done_reason=length 的场景": truncated,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
