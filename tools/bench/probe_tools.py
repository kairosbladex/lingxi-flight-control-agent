"""探测 238 上各模型的工具调用能力与延迟。"""
import json, time, urllib.request, sys

BASE = "http://127.0.0.1:11434"

TOOLS = [
    {"type": "function", "function": {
        "name": "relative_move",
        "description": "在已绑定的飞行模拟器中执行一次受限相对移动。",
        "parameters": {"type": "object", "properties": {
            "direction": {"type": "string", "enum": ["forward","backward","left","right","up","down"]},
            "distance_m": {"type": "number", "description": "距离，米，0-100"},
            "idempotency_key": {"type": "string"}},
            "required": ["direction","distance_m","idempotency_key"]}}},
    {"type": "function", "function": {
        "name": "hover",
        "description": "在当前位置执行指定时长的受限悬停。",
        "parameters": {"type": "object", "properties": {
            "duration_s": {"type": "number"},
            "idempotency_key": {"type": "string"}},
            "required": ["duration_s","idempotency_key"]}}},
]

SYS = "你是无人机飞控执行 Agent。只能通过工具操作无人机，不得声称已执行。用户要求飞行时调用对应工具。"

TASKS = [
    ("向前飞10米", "relative_move"),
    ("原地悬停30秒", "hover"),
    ("后退5米", "relative_move"),
]

def chat(model, messages, tools=None, timeout=240):
    body = {"model": model, "messages": messages, "stream": False,
            "options": {"temperature": 0}}
    if tools: body["tools"] = tools
    req = urllib.request.Request(BASE + "/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    return d, time.time() - t0

def probe(model):
    out = {"model": model}
    # warm-up / capability check
    try:
        d, dt = chat(model, [{"role":"system","content":SYS},{"role":"user","content":"向前飞10米"}], TOOLS)
    except Exception as e:
        return {**out, "error": f"{type(e).__name__}: {e}"}
    msg = d.get("message", {})
    calls = msg.get("tool_calls") or []
    out["warmup_s"] = round(dt, 1)
    out["native_tool_call"] = bool(calls)
    out["first_call"] = calls[0]["function"] if calls else None
    if not calls:
        out["raw"] = (msg.get("content") or "")[:180]
    out["load_s"] = round(d.get("total_duration",0)/1e9, 1) or None
    # correctness across tasks
    ok = 0
    for text, expect in TASKS:
        try:
            d2, dt2 = chat(model, [{"role":"system","content":SYS},{"role":"user","content":text}], TOOLS)
            c = (d2.get("message",{}).get("tool_calls") or [])
            if c and c[0]["function"]["name"] == expect: ok += 1
        except Exception:
            pass
    out["accuracy"] = f"{ok}/{len(TASKS)}"
    return out

if __name__ == "__main__":
    for m in sys.argv[1:]:
        r = probe(m)
        print(json.dumps(r, ensure_ascii=False))
        sys.stdout.flush()
