"""安全快路径对照：走模型 vs 走确定性短路，以及模型不可用时的可用性。"""
import sys, time
sys.path.insert(0, "src")
from lingxi_flight_control import (BoundedAgentLoop, DEFAULT_BASE_URL, MemoryFlightControlSimulator,
                                   OllamaDecider, RuntimeBinding, SafetyFastPathDecider,
                                   build_full_control_tool_registry)

BIND = lambda: RuntimeBinding("actor-1", "p", "h", "d", "b", 1)
MODEL = sys.argv[1] if len(sys.argv) > 1 else "qwen3.5:9b"


def drive(loop, command, rounds=8):
    cp = None
    for _ in range(rounds):
        r = loop.run(command, checkpoint=cp, confirmed=cp is not None)
        cp = r.checkpoint
        if r.status.value != "clarification":
            return r
    return r


def bench(label, decider, command="急停"):
    sim = MemoryFlightControlSimulator(BIND())
    loop = BoundedAgentLoop(decider, build_full_control_tool_registry(sim), max_steps=8)
    t0 = time.time()
    r = drive(loop, command)
    wall = time.time() - t0
    state = sim.state.task_status
    print(f"{label:<34} 墙钟={wall:7.3f}s  结果={r.status.value:<10} 状态={state}")
    return wall


print(f"模型={MODEL}\n")
warm = bench("走模型（预热一次）", OllamaDecider(model=MODEL, base_url=DEFAULT_BASE_URL, keep_alive="30m"))
model_wall = bench("走模型（缓存已热）", OllamaDecider(model=MODEL, base_url=DEFAULT_BASE_URL, keep_alive="30m"))
fast_wall = bench("安全快路径",
                  SafetyFastPathDecider(OllamaDecider(model=MODEL, base_url=DEFAULT_BASE_URL)))

print(f"\n模型服务不可达（端口 1 必然拒绝连接）：")
dead = "http://127.0.0.1:1"
bench("走模型", OllamaDecider(model=MODEL, base_url=dead, timeout=3.0))
bench("安全快路径", SafetyFastPathDecider(OllamaDecider(model=MODEL, base_url=dead, timeout=3.0)))

print(f"\n加速比（模型热路径 vs 快路径）: {model_wall/max(fast_wall,1e-6):.0f}x")
