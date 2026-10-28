#!/usr/bin/env python3
"""上下文预算实测：证明 num_ctx=8192 会静默丢掉 system prompt，而预算压缩能保住它。

三个可以独立复现的事实：
1. 不做预算管理时，20 次 read_state 观察的对话在 num_ctx=8192 下被压在 8192 上限，
   真实 prompt_eval_count 远低于完整内容，且 system prompt 里的口令标记丢失；
2. `OllamaDecider` 的预算压缩在同一条对话上构造出的 prompt 装进预算，
   口令标记仍在；
3. 保守字符估算与真实 prompt_eval_count 的偏差（估算必须偏大才算安全）。

用法：
    PYTHONPATH=src python3 tools/bench/context_budget.py [model] [observations]
"""

import json
import sys

from lingxi_flight_control import (
    DEFAULT_NUM_CTX,
    DEFAULT_OUTPUT_RESERVE_TOKENS,
    AgentCheckpoint,
    MemoryFlightControlSimulator,
    OllamaDecider,
    PromptBudgetExceeded,
    RuntimeBinding,
    build_full_control_tool_registry,
    estimate_prompt_tokens,
)
from lingxi_flight_control.ollama_decider import (
    DEFAULT_SYSTEM_PROMPT,
    build_tools_payload,
    http_transport,
)

BASE_URL = "http://127.0.0.1:11434"

#: 塞进 system prompt 的口令标记。Ollama 超限时丢的是最前面，所以它是最先消失的东西。
MARKER = "ZEBRA-7391"
SYSTEM_PROMPT = DEFAULT_SYSTEM_PROMPT + (
    f"\n9. 口令标记是 {MARKER}。用户询问口令标记时，只回答这个标记本身。"
)
USER_MESSAGE = "口令标记是什么？只回答标记本身。"


def build_receipt():
    simulator = MemoryFlightControlSimulator(
        RuntimeBinding("actor-bench", "project-bench", "hangar-01", "drone-01", "batch-01", 1))
    registry = build_full_control_tool_registry(simulator)
    receipt = registry.get("read_state").handler({})
    return registry, receipt


def raw_messages(receipt, count, *, user_message=USER_MESSAGE, system_prompt=SYSTEM_PROMPT):
    """修复前的行为：不做任何预算管理，把全部观察直接拼进对话。"""
    messages = [{"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}]
    for _ in range(count):
        messages.append({"role": "assistant", "content": "",
                         "tool_calls": [{"function": {"name": "read_state", "arguments": {}}}]})
        messages.append({"role": "tool",
                         "content": json.dumps(receipt, ensure_ascii=False, sort_keys=True, default=str)})
    return messages


def probe(model, messages, tools, num_ctx):
    body = {"model": model, "messages": messages, "stream": False, "tools": tools,
            "options": {"temperature": 0.0, "num_ctx": num_ctx}, "keep_alive": "30m"}
    response = http_transport(BASE_URL + "/api/chat", body, 600.0)
    message = response.get("message") or {}
    content = message.get("content") or ""
    return {
        "num_ctx": num_ctx,
        "prompt_tokens": response.get("prompt_eval_count"),
        "eval_tokens": response.get("eval_count"),
        "marker_recalled": MARKER in json.dumps(response, ensure_ascii=False),
        "answer": content.strip()[:40],
    }


def main() -> int:
    model = sys.argv[1] if len(sys.argv) > 1 else "qwen3.5:9b"
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    registry, receipt = build_receipt()
    definitions = registry.definitions()
    tools = build_tools_payload(definitions)
    receipt_chars = len(json.dumps(receipt, ensure_ascii=False, sort_keys=True, default=str))

    raw = raw_messages(receipt, count, system_prompt=SYSTEM_PROMPT)
    checkpoint = AgentCheckpoint(
        user_input=USER_MESSAGE, context={}, messages=[], tool_calls=[],
        observations=[{"tool": "read_state", "arguments": {}, "result": receipt}
                      for _ in range(count)],
    )
    head = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": USER_MESSAGE}]
    # 压缩必须用「真正要发送的那个 num_ctx」来算预算，否则估算与窗口对不上。
    window_sizes = (8192, 16384, DEFAULT_NUM_CTX)
    deciders = {
        nctx: OllamaDecider(model=model, system_prompt=SYSTEM_PROMPT, base_url=BASE_URL,
                            keep_alive="30m", think=None, num_ctx=nctx)
        for nctx in window_sizes
    }
    compressed: dict[int, list] = {}
    rejections: dict[int, str] = {}
    for nctx, decider in deciders.items():
        try:
            compressed[nctx] = decider.messages(USER_MESSAGE, checkpoint, definitions)
        except PromptBudgetExceeded as exc:
            # 59 个工具的契约本身就超出这个小窗口的预算：硬失败，而不是发出去被静默截断。
            rejections[nctx] = str(exc)

    print(f"模型={model}  观察数={count}  单条回执={receipt_chars}字符  "
          f"默认num_ctx={DEFAULT_NUM_CTX}  生成预留={DEFAULT_OUTPUT_RESERVE_TOKENS}")
    print(f"完整(未压缩)消息数={len(raw)}  估算={estimate_prompt_tokens(raw, definitions)} tokens")
    for nctx, decider in deciders.items():
        if nctx in rejections:
            print(f"压缩后 num_ctx={nctx}: 拒绝发送 —— {rejections[nctx]}")
            continue
        messages = compressed[nctx]
        replayed = sum(1 for m in messages if m.get("tool_calls"))
        estimate = decider.estimate_prompt_tokens(messages, definitions)
        print(f"压缩后 num_ctx={nctx}: 消息数={len(messages)} 回放观察={replayed}/{count} "
              f"估算={estimate} ≤ 预算 {decider.prompt_budget_tokens}: "
              f"{estimate <= decider.prompt_budget_tokens}")

    scenarios = [
        ("仅头部 工具+system+user", head, DEFAULT_NUM_CTX),
        ("修复前 num_ctx=8192", raw, 8192),
        ("修复前 num_ctx=32768", raw, 32768),
    ]
    scenarios += [(f"压缩后 num_ctx={nctx}", compressed[nctx], nctx)
                  for nctx in (16384, DEFAULT_NUM_CTX) if nctx in compressed]
    for label, messages, num_ctx in scenarios:
        try:
            result = probe(model, messages, tools, num_ctx)
        except Exception as exc:  # 联调用例：单条失败不掩盖其余数据
            result = {"num_ctx": num_ctx, "error": f"{type(exc).__name__}: {exc}"}
        result["场景"] = label
        result["估算"] = estimate_prompt_tokens(messages, definitions)
        print(json.dumps(result, ensure_ascii=False))
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
