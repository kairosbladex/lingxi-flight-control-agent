"""量化工具目录规模带来的预填充成本——这是「加功能」最直接的代价。"""
import json,sys,time,urllib.request
sys.path.insert(0,"tools/bench")
from tool_selection import build_tools
BASE="http://127.0.0.1:11434"

def measure(model, tools, timeout=1800):
    msgs=[{"role":"system","content":"你是无人机飞控执行 Agent，只能通过工具操作无人机。"},
          {"role":"user","content":"读取当前状态"}]
    body={"model":model,"messages":msgs,"stream":False,"options":{"temperature":0},"tools":tools}
    req=urllib.request.Request(BASE+"/api/chat",data=json.dumps(body).encode(),
        headers={"Content-Type":"application/json"})
    t0=time.time()
    with urllib.request.urlopen(req,timeout=timeout) as r: d=json.loads(r.read())
    wall=time.time()-t0
    pc=d.get("prompt_eval_count",0); pd=d.get("prompt_eval_duration",1)/1e9
    ec=d.get("eval_count",0); ed=d.get("eval_duration",1)/1e9
    return {"model":model,"工具数":len(tools),"prompt_tokens":pc,
            "prefill_tok_s":round(pc/pd,1) if pd else None,
            "prefill_s":round(pd,1),
            "decode_tok_s":round(ec/ed,1) if ed else None,
            "load_s":round(d.get("load_duration",0)/1e9,1),
            "总墙钟_s":round(wall,1)}

full=build_tools()
small=full[:2]
for model in sys.argv[1:]:
    for label,tools in (("2工具",small),("59工具",full)):
        try:
            r=measure(model,tools); r["场景"]=label
            print(json.dumps(r,ensure_ascii=False)); sys.stdout.flush()
        except Exception as e:
            print(json.dumps({"model":model,"场景":label,"error":str(e)},ensure_ascii=False)); sys.stdout.flush()
