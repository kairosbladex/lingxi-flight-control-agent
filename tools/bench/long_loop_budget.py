#!/usr/bin/env python3
"""真实循环里的上下文预算验证：跑够 >12 次观察，确认压缩发生且 system prompt 仍在。

`tools/bench/context_budget.py` 用合成对话验证「预算闸门」；这里验证的是另一半：
**真实 `BoundedAgentLoop` 跑出真实观察之后**，压缩仍然成立、模型仍能给出正确结论。

做法：前 16 步由脚本决定 read_state（不花模型调用，但每步都产生一条真实回执），
第 17 步起交给真实 `OllamaDecider`。此时 16 条观察已超过 keep_recent_observations=12，
压缩必然触发；用户问的是 system prompt 里的口令标记，答对就证明硬性约束没被丢掉。

用法：
    PYTHONPATH=src python3 tools/bench/long_loop_budget.py [model] [scripted_steps]
"""

import json
import sys

from lingxi_flight_control import (
    DEFAULT_KEEP_RECENT_OBSERVATIONS,
    DEFAULT_NUM_CTX,
    BoundedAgentLoop,
    DecisionKind,
    MemoryFlightControlSimulator,
    ModelDecision,
    OllamaDecider,
    RuntimeBinding,
    ToolCall,
    build_full_control_tool_registry,
    estimate_prompt_tokens,
    project_observation_result,
)
from lingxi_flight_control.ollama_decider import DEFAULT_SYSTEM_PROMPT

BASE_URL = "http://192.168.130.238:11434"
SCRIPTED_STEPS = 16

MARKER = "ZEBRA-7391"
SYSTEM_PROMPT = DEFAULT_SYSTEM_PROMPT + (
    f"\n9. 口令标记是 {MARKER}。用户询问口令标记时，只回答这个标记本身。"
)
USER_MESSAGE = "口令标记是什么？只回答标记本身。"


class ScriptedThenModel:
    """先按脚本发 read_state 攒观察，攒够之后交给真实模型收尾。"""

    def __init__(self, scripted: int, model):
        self._scripted = scripted
        self._model = model
        self.model_calls = 0

    def decide(self, user_message, checkpoint, tools):
        if len(checkpoint.observations) < self._scripted:
            return ModelDecision(DecisionKind.TOOL_CALL, tool_call=ToolCall("read_state", {}))
        self.model_calls += 1
        return self._model.decide(user_message, checkpoint, tools)


def main() -> int:
    model = sys.argv[1] if len(sys.argv) > 1 else "qwen3.5:9b"
    scripted = int(sys.argv[2]) if len(sys.argv) > 2 else SCRIPTED_STEPS

    simulator = MemoryFlightControlSimulator(
        RuntimeBinding("actor-bench", "project-bench", "hangar-01", "drone-01", "batch-01", 1))
    registry = build_full_control_tool_registry(simulator)
    definitions = registry.definitions()

    brain = OllamaDecider(model=model, system_prompt=SYSTEM_PROMPT, base_url=BASE_URL,
                          keep_alive="30m", think=None,
                          result_projector=project_observation_result)
    decider = ScriptedThenModel(scripted, brain)
    loop = BoundedAgentLoop(decider, registry, max_steps=scripted + 4)
    result = loop.run(USER_MESSAGE, confirmed=True)
    checkpoint = result.checkpoint

    # 三种口径，用同一套构建代码：完全不压 / 只压缩 / 压缩 + 回执投影。
    raw_decider = OllamaDecider(
        model=model, system_prompt=SYSTEM_PROMPT, num_ctx=10 ** 6, output_reserve=1,
        keep_recent_observations=10 ** 6, transport=lambda *_: {})
    compress_only = OllamaDecider(model=model, system_prompt=SYSTEM_PROMPT, transport=lambda *_: {})
    raw_messages = raw_decider.messages(USER_MESSAGE, checkpoint, definitions)
    compressed_messages = compress_only.messages(USER_MESSAGE, checkpoint, definitions)

    response = brain.last_raw_response or {}
    print(f"模型={model}  脚本步数={scripted}  默认num_ctx={DEFAULT_NUM_CTX}  "
          f"回放上限={DEFAULT_KEEP_RECENT_OBSERVATIONS}  回执投影=on")
    print(json.dumps({
        "循环结果": result.status.value,
        "观察条数": len(checkpoint.observations),
        "模型调用次数": decider.model_calls,
        "答复": result.message.strip()[:40],
        "口令正确": MARKER in result.message,
    }, ensure_ascii=False))
    for label, decider_, messages in (
        ("未压缩·未投影（全部观察）", raw_decider, raw_messages),
        ("只压缩（不投影）", compress_only, compressed_messages),
        (f"压缩+投影（实际发送）", brain, None),
    ):
        estimate = (decider_.last_prompt_estimate if messages is None
                    else estimate_prompt_tokens(messages, definitions))
        print(json.dumps({
            "场景": label,
            "消息数": (len(brain.messages(USER_MESSAGE, checkpoint, definitions))
                      if messages is None else len(messages)),
            "估算 tokens": estimate,
        }, ensure_ascii=False))
    print(json.dumps({
        "场景": f"实际发送（num_ctx={DEFAULT_NUM_CTX}）",
        "省略观察": brain.last_omitted_observations,
        "预算": brain.prompt_budget_tokens,
        "实测 prompt_eval_count": response.get("prompt_eval_count"),
        "实测 eval_count": response.get("eval_count"),
        "在预算内": (brain.last_prompt_estimate or 0) <= brain.prompt_budget_tokens,
    }, ensure_ascii=False))
    print(json.dumps({
        "预算累计": {"压缩载荷次数": brain.compressed_payloads,
                     "省略观察条数": brain.omitted_observation_turns},
    }, ensure_ascii=False))

    # 更小的窗口下，同样的检查点必须继续往前丢观察才能装进预算（离线，不发请求）。
    for num_ctx in (16384, 12288):
        tighter = OllamaDecider(model=model, system_prompt=SYSTEM_PROMPT, num_ctx=num_ctx,
                                transport=lambda *_: {})
        messages = tighter.messages(USER_MESSAGE, checkpoint, definitions)
        replayed = sum(1 for m in messages if m.get("tool_calls"))
        print(json.dumps({
            "场景": f"同一检查点 num_ctx={num_ctx}（离线）",
            "回放观察": f"{replayed}/{len(checkpoint.observations)}",
            "估算 tokens": tighter.estimate_prompt_tokens(messages, definitions),
            "预算": tighter.prompt_budget_tokens,
        }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
