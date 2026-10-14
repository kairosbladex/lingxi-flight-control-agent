"""验证 Ollama 是否对工具目录前缀命中 KV cache——决定分组优化是否值得做。"""
import json,sys,time,urllib.request
sys.path.insert(0,"tools/bench")
from tool_selection import build_tools
BASE="http://192.168.130.238:11434"
tools=build_tools()

def call(model,user,label):
    body={"model":model,"messages":[{"role":"system","content":"你是无人机飞控执行 Agent，只能通过工具操作无人机。"},
          {"role":"user","content":user}],"stream":False,"options":{"temperature":0},"tools":tools}
    req=urllib.request.Request(BASE+"/api/chat",data=json.dumps(body).encode(),
        headers={"Content-Type":"application/json"})
    t0=time.time()
    with urllib.request.urlopen(req,timeout=900) as r: d=json.loads(r.read())
    wall=time.time()-t0
    print(json.dumps({"步骤":label,"prompt_tokens":d.get("prompt_eval_count",0),
        "prefill_s":round(d.get("prompt_eval_duration",0)/1e9,2),
        "decode_tok_s":round(d.get("eval_count",0)/max(d.get("eval_duration",1)/1e9,1e-9),1),
        "wall_s":round(wall,2)},ensure_ascii=False)); sys.stdout.flush()

model=sys.argv[1] if len(sys.argv)>1 else "qwen3.5:9b"
call(model,"读取当前状态","1 首次（冷 prefill）")
call(model,"读取当前状态","2 完全相同重放")
call(model,"读取当前状态","3 再次相同重放")
call(model,"拍张照片","4 用户消息变了、工具集不变")
