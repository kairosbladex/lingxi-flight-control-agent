"""59 工具选择准确率：检验模型在工具面扩大后是否退化（未来功能迭代的核心风险）。

用法（工作项 5 的检索前后对比，两条命令都跑一遍）：
    PYTHONPATH=src python3 tools/bench/tool_selection.py qwen3.5:9b              # 全量 59 工具
    PYTHONPATH=src python3 tools/bench/tool_selection.py qwen3.5:9b --selector   # 按意图检索候选子集

两条命令的 46 条用例、工具描述与 SYS 完全一致，唯一变量是「是否按意图检索」，
因此准确率与 prompt 开销可以直接对比。
"""
import json, re, statistics, sys, time, urllib.request
BASE="http://192.168.130.238:11434"

def num(d="", lo=None, hi=None):
    s={"type":"number"}
    if d: s["description"]=d
    return s
def st(d=""):
    s={"type":"string"}
    if d: s["description"]=d
    return s

# name: (描述, 必填参数, 可选参数)
SPEC = {
 "control_acquire":("获取无人机控制权",[],[]),
 "control_release":("释放无人机控制权",[],[]),
 "remote_request":("向远端发起接管请求",[],[]),
 "remote_approve":("同意远程接管请求",[],[]),
 "remote_reject":("拒绝远程接管请求",[],[]),
 "set_keyboard_mode":("切换为键盘操作模式",[],[]),
 "set_joystick_mode":("切换为摇杆操作模式",[],[]),
 "set_virtual_button":("绑定虚拟按键",["button","action"],[]),
 "move_forward":("向机头正前方平移",["distance_m"],[]),
 "move_backward":("向机尾后方平移",["distance_m"],[]),
 "move_left":("向机身左侧平移",["distance_m"],[]),
 "move_right":("向机身右侧平移",["distance_m"],[]),
 "move_up":("垂直上升",["distance_m"],[]),
 "move_down":("垂直下降",["distance_m"],[]),
 "turn_left":("原地向左偏航转向，不位移",["angle_deg"],[]),
 "turn_right":("原地向右偏航转向，不位移",["angle_deg"],[]),
 "emergency_stop":("紧急停止所有动作",[],[]),
 "takeoff":("起飞到指定高度",["altitude_m"],[]),
 "land":("在当前点垂直降落",[],[]),
 "land_alternate":("飞往备降点降落",[],[]),
 "return_home":("返航回到起飞点",[],[]),
 "cancel_return_home":("取消正在进行的返航",[],[]),
 "route_pause":("暂停当前航线任务",[],[]),
 "route_resume":("恢复被暂停的航线任务",[],[]),
 "continue_from_breakpoint":("从航线断点继续执行",[],["index"]),
 "point_flight":("飞往指定经纬度和高度",["longitude","latitude","altitude_m"],[]),
 "point_orbit":("围绕指定经纬度盘旋",["longitude","latitude","radius_m"],[]),
 "upload_route":("上传航线到无人机",["route_id","waypoints"],[]),
 "execute_route":("执行已上传的航线",[],[]),
 "set_move_speed":("设置水平移动速度",["value"],[]),
 "set_roll_speed":("设置横滚角速度",["value"],[]),
 "set_pitch_speed":("设置俯仰角速度",["value"],[]),
 "set_vertical_speed":("设置垂直升降速度",["value"],[]),
 "set_yaw_speed":("设置偏航角速度",["value"],[]),
 "reset_parameters":("恢复默认飞行参数",[],[]),
 "camera_electro_optical":("切换可见光相机",[],[]),
 "camera_zoom":("设置光学变焦倍数",["value"],[]),
 "camera_infrared":("切换红外热成像镜头",[],[]),
 "camera_wide":("切换广角镜头",[],[]),
 "gimbal_up":("云台向上转动",[],[]),
 "gimbal_down":("云台向下转动",[],[]),
 "gimbal_left":("云台向左转动",[],[]),
 "gimbal_right":("云台向右转动",[],[]),
 "gimbal_digital_zoom":("设置云台数字变焦倍数",["value"],[]),
 "gimbal_reset":("云台回中复位",[],[]),
 "gimbal_45":("云台俯仰至-45度",[],[]),
 "gimbal_90":("云台俯仰至-90度",[],[]),
 "camera_lock_focus":("锁定对焦",[],[]),
 "camera_panorama":("全景拍摄",[],[]),
 "camera_photo":("拍摄单张照片",[],[]),
 "camera_start_recording":("开始录像",[],[]),
 "camera_stop_recording":("停止录像",[],[]),
 "infrared_set_limits":("设置红外测温上下限",["min_c","max_c"],[]),
 "payload_megaphone":("控制喊话器",[],["enabled"]),
 "payload_searchlight":("控制探照灯",[],["enabled"]),
 "payload_methane":("控制甲烷检测",[],["enabled"]),
 "payload_dropper":("控制投放器",[],["payload"]),
 "payload_dynamic":("控制动态载荷开关",["payload","enabled"],[]),
 "read_state":("读取当前飞行状态",[],[]),
}

def build_tools():
    tools=[]
    for name,(desc,req,opt) in SPEC.items():
        props={}
        for p in req:
            props[p]=num() if p in ("distance_m","angle_deg","altitude_m","value","min_c","max_c",
                                    "radius_m","longitude","latitude","index") else st()
        for p in opt:
            props[p]=num() if p=="index" else (st() if p=="payload" else {"type":"boolean"})
        if name=="upload_route": props["waypoints"]={"type":"array","items":{"type":"object"}}
        tools.append({"type":"function","function":{"name":name,"description":desc,
            "parameters":{"type":"object","properties":props,"required":req}}})
    return tools

CASES=[
 ("接管控制权","control_acquire"),("释放控制权","control_release"),
 ("切换到摇杆模式","set_joystick_mode"),("切换到键盘模式","set_keyboard_mode"),
 ("请求远程接管","remote_request"),("同意远程接管","remote_approve"),
 ("向前飞10米","move_forward"),("向左平移5米","move_left"),("左转90度","turn_left"),
 ("向右转45度","turn_right"),("上升20米","move_up"),("下降15米","move_down"),
 ("紧急停止","emergency_stop"),("起飞到100米","takeoff"),("降落","land"),
 ("飞往备降点降落","land_alternate"),("返航","return_home"),("取消返航","cancel_return_home"),
 ("暂停航线","route_pause"),("恢复航线","route_resume"),("从断点继续","continue_from_breakpoint"),
 ("飞到经度116.4纬度39.9高度50米","point_flight"),("绕点盘旋半径200米","point_orbit"),
 ("上传航线","upload_route"),("执行航线","execute_route"),
 ("把水平移动速度设为8","set_move_speed"),("设置偏航角速度60","set_yaw_speed"),
 ("设置横滚角速度30","set_roll_speed"),("恢复默认参数","reset_parameters"),
 ("切换红外镜头","camera_infrared"),("切换广角镜头","camera_wide"),
 ("放大到5倍","camera_zoom"),("云台向上","gimbal_up"),("云台回中","gimbal_reset"),
 ("云台俯仰到-45度","gimbal_45"),("拍张照片","camera_photo"),
 ("开始录像","camera_start_recording"),("停止录像","camera_stop_recording"),
 ("锁定对焦","camera_lock_focus"),("全景拍摄","camera_panorama"),
 ("设置红外测温范围-10到120","infrared_set_limits"),
 ("打开喊话器","payload_megaphone"),("打开探照灯","payload_searchlight"),
 ("打开甲烷检测","payload_methane"),("投放物资","payload_dropper"),
 ("读取当前状态","read_state"),
]
SYS=("你是无人机飞控执行 Agent，只能通过工具操作无人机。用户一次要求多个动作时必须依次全部完成。"
     "从下列工具中选出最匹配的一个并调用。")

def chat(model,msgs,tools,timeout=900):
    body={"model":model,"messages":msgs,"stream":False,"options":{"temperature":0},
          "tools":tools,"keep_alive":"30m"}
    req=urllib.request.Request(BASE+"/api/chat",data=json.dumps(body).encode(),
        headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(req,timeout=timeout) as r: return json.loads(r.read())

def _project_imports():
    """延迟导入项目模块，缺 PYTHONPATH 时给出可操作的提示而不是堆栈。"""
    try:
        from lingxi_flight_control.ollama_decider import estimate_tokens
        from lingxi_flight_control.tool_selector import RuleBasedToolSelector
    except ImportError as exc:
        raise SystemExit(f"需要 PYTHONPATH=src 才能估算 token / 使用 --selector：{exc}")
    return estimate_tokens, RuleBasedToolSelector()

def select_tools(text, tools, selector):
    """按意图挑候选子集；返回 (下发的工具, 模式)。"""
    descriptors=[(t["function"]["name"], t["function"]["description"]) for t in tools]
    result=selector.select(text, descriptors)
    if result.mode != "retrieved":
        return tools, result.mode
    keep=set(result.names)
    return [t for t in tools if t["function"]["name"] in keep], result.mode

def run(model, use_selector=False):
    estimate_tokens, selector = _project_imports()
    tools=build_tools(); ok=0; bad=[]; t0=time.time()
    subset_sizes=[]; tool_token_estimates=[]; prompt_eval_counts=[]; fallbacks=0
    for text,expect in CASES:
        subset, mode = select_tools(text, tools, selector) if use_selector else (tools, "full")
        if mode != "retrieved":
            fallbacks += 1
        try:
            d=chat(model,[{"role":"system","content":SYS},{"role":"user","content":text}],subset)
            tc=(d.get("message",{}).get("tool_calls") or [])
            got=tc[0]["function"]["name"] if tc else None
            prompt_eval_counts.append(d.get("prompt_eval_count"))
        except Exception as e:
            got=f"ERR:{e}"
        if got==expect: ok+=1
        else: bad.append({"输入":text,"期望":expect,"实得":got})
        subset_sizes.append(len(subset))
        # 与线上同一套保守估算口径（含 tools 段的模板开销）。
        tool_token_estimates.append(estimate_tokens(
            json.dumps(subset,ensure_ascii=False,sort_keys=True))+24)
    evals=[value for value in prompt_eval_counts if isinstance(value,int)]
    print(json.dumps({"model":model,"检索":"on" if use_selector else "off",
        "工具数":len(tools),"平均候选工具数":round(sum(subset_sizes)/len(subset_sizes),1),
        "候选工具数范围":[min(subset_sizes),max(subset_sizes)],"回退全量用例数":fallbacks,
        "平均工具段估算tokens":round(sum(tool_token_estimates)/len(tool_token_estimates),1),
        "平均prompt_eval_count":round(sum(evals)/len(evals),1) if evals else None,
        "中位prompt_eval_count":statistics.median(evals) if evals else None,
        "用例数":len(CASES),
        "准确率":f"{ok}/{len(CASES)}", "耗时_s":round(time.time()-t0,1),
        "错例":bad},ensure_ascii=False,indent=1))

if __name__=="__main__":
    args=[a for a in sys.argv[1:] if not a.startswith("--")]
    flags={a for a in sys.argv[1:] if a.startswith("--")}
    unknown=flags-{"--selector"}
    if unknown:
        raise SystemExit(f"未知参数: {sorted(unknown)}（只支持 --selector）")
    run(args[0] if args else "qwen3.5:9b", use_selector="--selector" in flags)
