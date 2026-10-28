"""真实多步循环的逐步成本：验证 KV cache 在观察累积时是否持续命中。"""
import json,sys,time
sys.path.insert(0,".")
from lingxi_flight_control import (BoundedAgentLoop, ExecutionContext, MemoryFlightSimulator, OllamaDecider)

class Metered:
    def __init__(self,inner): self.inner=inner
    def decide(self,msg,cp,tools):
        t0=time.time(); d=self.inner.decide(msg,cp,tools); wall=time.time()-t0
        r=self.inner.last_raw_response or {}
        print(f"  第{cp.step_count}步 prefill={r.get('prompt_eval_duration',0)/1e9:5.2f}s "
              f"decode={r.get('eval_count',0)}tok 墙钟={wall:5.2f}s "
              f"-> {(d.kind.value if hasattr(d.kind,'value') else d.kind)}"
              f"{' '+d.tool_call.name if d.tool_call else ''}"); sys.stdout.flush()
        return d

model=sys.argv[1] if len(sys.argv)>1 else "qwen3.5:9b"
sim=MemoryFlightSimulator(ExecutionContext("e","d",0,True))
dec=OllamaDecider(model=model,think=False,keep_alive="30m")
cmd="向前飞10米，然后上升5米，再向左平移3米，最后原地悬停20秒"
print(f"模型={model}\n指令={cmd}")
r=BoundedAgentLoop(Metered(dec),sim.tool_registry(),max_steps=8).run(cmd)
print(f"结果={r.status.value} 已执行={[c['name'] for c in r.checkpoint.tool_calls]}")
print(f"位置 x={sim.position.x_m} y={sim.position.y_m} z={sim.position.z_m}")
