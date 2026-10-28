import json, time, urllib.request, sys
BASE="http://127.0.0.1:11434"
def gen(model, prompt="用一句话说明无人机相对移动指令的执行前提。", n=128):
    body={"model":model,"messages":[{"role":"user","content":prompt}],"stream":False,
          "options":{"temperature":0,"num_predict":n}}
    req=urllib.request.Request(BASE+"/api/chat",data=json.dumps(body).encode(),
                               headers={"Content-Type":"application/json"})
    t0=time.time()
    with urllib.request.urlopen(req,timeout=900) as r: d=json.loads(r.read())
    wall=time.time()-t0
    ec=d.get("eval_count",0); ed=d.get("eval_duration",1)/1e9
    return {"model":model,"load_s":round(d.get("load_duration",0)/1e9,1),
            "eval_count":ec,"decode_tok_s":round(ec/ed,1) if ed else None,
            "wall_s":round(wall,1),
            "prompt_tok_s":round(d.get("prompt_eval_count",0)/(d.get("prompt_eval_duration",1)/1e9),1)}
for m in sys.argv[1:]:
    try: print(json.dumps(gen(m),ensure_ascii=False))
    except Exception as e: print(json.dumps({"model":m,"error":str(e)},ensure_ascii=False))
    sys.stdout.flush()
