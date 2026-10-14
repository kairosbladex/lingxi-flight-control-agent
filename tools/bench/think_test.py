"""对比 thinking 开关对多步规划与澄清行为的影响。"""
import json, urllib.request, sys
BASE="http://192.168.130.238:11434"
TOOLS=[
 {"type":"function","function":{"name":"relative_move","description":"受限相对移动，distance_m≤100。",
   "parameters":{"type":"object","properties":{
     "direction":{"type":"string","enum":["forward","backward","left","right","up","down"]},
     "distance_m":{"type":"number"},"idempotency_key":{"type":"string"}},
     "required":["direction","distance_m","idempotency_key"]}}},
 {"type":"function","function":{"name":"hover","description":"原地悬停指定时长（秒）。",
   "parameters":{"type":"object","properties":{"duration_s":{"type":"number"},"idempotency_key":{"type":"string"}},
     "required":["duration_s","idempotency_key"]}}}]
SYS=("你是无人机飞控执行 Agent。只能通过工具操作无人机。用户一次要求多个动作时，你必须依次全部完成，"
     "每次只调用一个工具，拿到结果后继续下一个，全部完成后才给出最终答复。")

def chat(model,msgs,think,timeout=900):
    body={"model":model,"messages":msgs,"stream":False,"options":{"temperature":0},"tools":TOOLS}
    if think is not None: body["think"]=think
    req=urllib.request.Request(BASE+"/api/chat",data=json.dumps(body).encode(),
        headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(req,timeout=timeout) as r: return json.loads(r.read())

def multi(model,think,user="先向前飞10米，然后原地悬停30秒"):
    msgs=[{"role":"system","content":SYS},{"role":"user","content":user}]
    seen=[];steps=0;fin=False;think_chars=0
    for _ in range(5):
        d=chat(model,msgs,think); m=d.get("message",{}); steps+=1
        think_chars+=len(m.get("thinking") or "")
        tc=m.get("tool_calls") or []
        if not tc: fin=True;break
        seen.append(tc[0]["function"]["name"])
        msgs.append({"role":"assistant","content":"","tool_calls":tc})
        msgs.append({"role":"tool","content":json.dumps({"status":"accepted"})})
    return seen,fin,steps,think_chars

for m,think in [("qwen3.5:9b",False),("qwen3.5:9b",True),("qwen3.5:4b",True)]:
    try:
        seen,fin,steps,tc=multi(m,think)
        print(json.dumps({"model":m,"think":think,"chain":seen,"final":fin,"steps":steps,
                          "thinking_chars":tc,"multi_step_ok":seen[:2]==["relative_move","hover"] and fin},
                         ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"model":m,"think":think,"error":str(e)},ensure_ascii=False))
    sys.stdout.flush()
