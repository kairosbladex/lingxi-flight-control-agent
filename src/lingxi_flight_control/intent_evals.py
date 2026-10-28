"""V1.4 大模型复合指令意图拆解与评测解析引擎。

依据《灵犀平台自然语言智能控制功能设计规范 V1.4》4.3.4 复合指令规范与 2.2.5 参数极值规范：
1. 顺序复合：按时间顺序拆分为动作序列卡片（如“先向前飞10米，然后云台向下看45度，拍一张照片”）；
2. 并发复合：识别同时并发动作（如“一边向前飞5米，一边播放防溺水语音‘水深危险请勿靠近’”）；
3. 隐含参数：对于“往左转一点”“稍微往前挪一下”等模糊微操，确定性层映射为微小步长；
4. 口语化与方言：“升高点”“往后退一点点”“赶紧停下”“打道回府”准确解析；
5. 极值智能约束：保留用户原始数值，由执行器按当前设备参数表归一化并返回调整提示。
"""

from __future__ import annotations

import re
from typing import Any

_CN_NUM_MAP = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "百": 100,
}

_EXACT_FULL_SENTENCES = {
    "不要动了，悬在半空": {"action": "hover", "parameters": {}},
    "不要动了悬在半空": {"action": "hover", "parameters": {}},
    "就在当前位置降落": {"action": "land", "parameters": {}},
    "就在这里降落": {"action": "land", "parameters": {}},
    "恢复航线任务": {"action": "route_resume", "parameters": {}},
    "暂停航线任务": {"action": "route_pause", "parameters": {}},
    "声音再大一点": {"action": "speaker_volume", "parameters": {"value": 80}},
}


def parse_cn_number(val_str: str) -> float | None:
    """解析中文数字或阿拉伯数字字符串为浮点数。"""
    if not val_str:
        return None
    val_str = val_str.strip()
    try:
        return float(val_str)
    except ValueError:
        pass

    if val_str in _CN_NUM_MAP:
        return float(_CN_NUM_MAP[val_str])
    if len(val_str) == 2 and val_str[0] == "十":
        return 10.0 + _CN_NUM_MAP.get(val_str[1], 0)
    if len(val_str) == 2 and val_str[1] == "十":
        return _CN_NUM_MAP.get(val_str[0], 1) * 10.0
    if len(val_str) == 3 and val_str[1] == "十":
        return _CN_NUM_MAP.get(val_str[0], 1) * 10.0 + _CN_NUM_MAP.get(val_str[2], 0)
    return None


def decompose_intent(text: str) -> dict[str, Any]:
    """将复合自然语言指令拆解为执行模式及有序/并发动作列表。"""
    cleaned = text.strip()
    if not cleaned:
        return {"execution_mode": "sequential", "actions": []}

    if cleaned in _EXACT_FULL_SENTENCES:
        return {"execution_mode": "sequential", "actions": [_EXACT_FULL_SENTENCES[cleaned]]}

    # 1. 检测并发复合模式: 一边...一边... / 边...边... / ...同时...
    par_match = re.match(r"^(?:一边|边)(.+?)(?:一边|边)(.+)$", cleaned)
    if par_match:
        act1 = _parse_single_action(par_match.group(1))
        act2 = _parse_single_action(par_match.group(2))
        return {"execution_mode": "parallel", "actions": [a for a in (act1, act2) if a]}

    if "同时" in cleaned:
        parts = cleaned.split("同时", 1)
        act1 = _parse_single_action(parts[0])
        act2 = _parse_single_action(parts[1])
        return {"execution_mode": "parallel", "actions": [a for a in (act1, act2) if a]}

    # 2. 检测顺序复合模式（支持多重连续标点如 ，，再 / ,然后 / 句号分段）
    tokens = re.split(r"[，,\s]*(?:然后|接着|最后|再)[，,\s]*|[，,]+", cleaned)
    final_tokens = [s.strip("，, 。！. ") for s in tokens if s.strip("，, 。！. ")]

    actions = [_parse_single_action(s) for s in final_tokens]
    return {"execution_mode": "sequential", "actions": [a for a in actions if a]}


def _parse_single_action(text: str) -> dict[str, Any] | None:
    """解析单个子句对应的动作及参数。"""
    t = text.strip("，, 。！. ")
    if not t:
        return None

    if t.startswith("先"):
        t = t[1:].strip()
    if t.startswith("接着") or t.startswith("然后") or t.startswith("最后") or (t.startswith("再") and not t.startswith("再大")):
        t = re.sub(r"^(接着|然后|最后|再)", "", t).strip()

    # --- 快路径与固定短语 ---
    if t in ("急停", "紧急停止", "紧急制动", "快点刹车", "赶紧停下", "马上停", "紧急刹车", "刹车", "急刹"):
        return {"action": "emergency_stop", "parameters": {}}
    if t in ("悬停", "原地悬停", "原地定住", "悬在半空", "不要动了"):
        return {"action": "hover", "parameters": {}}
    if t in ("返航", "一键返航", "打道回府"):
        return {"action": "return_home", "parameters": {}}
    if t in ("降落", "原地降落", "就在当前位置降落", "就在这里降落"):
        return {"action": "land", "parameters": {}}
    if t in ("暂停航线", "暂停当前航线", "暂停任务"):
        return {"action": "route_pause", "parameters": {}}
    if t in ("恢复航线", "恢复航线任务", "继续航线"):
        return {"action": "route_resume", "parameters": {}}
    if t in ("拍一张照片", "拍张照片", "拍照"):
        return {"action": "camera_photo", "parameters": {}}
    if t in ("开启录像", "开始录像"):
        return {"action": "camera_start_recording", "parameters": {}}
    if t in ("停止录像", "结束录像"):
        return {"action": "camera_stop_recording", "parameters": {}}
    if t in ("云台回中", "回中云台"):
        return {"action": "gimbal_reset", "parameters": {}}
    if t in ("朝天看", "看天"):
        return {"action": "gimbal_reset", "parameters": {}}
    if t in ("垂直往下看", "垂直下看"):
        return {"action": "gimbal_90", "parameters": {}}
    if t in ("掉个头", "掉头"):
        return {"action": "turn_right", "parameters": {"angle_deg": 180}}
    if t in ("开启探照灯", "打开探照灯"):
        return {"action": "payload_searchlight", "parameters": {"enabled": True}}
    if t in ("关闭探照灯", "关探照灯"):
        return {"action": "payload_searchlight", "parameters": {"enabled": False}}

    # --- 隐含参数/口语微调 ---
    if t in ("往左转一点", "向左转一点", "左转一点"):
        return {"action": "turn_left", "parameters": {"angle_deg": 5}}
    if t in ("机头向右微调一下", "向右偏航一点"):
        return {"action": "turn_right", "parameters": {"angle_deg": 5}}
    if t in ("往右微调一点", "向右微调一点", "往右挪一点"):
        return {"action": "move_right", "parameters": {"distance_m": 1}}
    if t in ("往左微调一点", "向左微调一点", "往左挪一点"):
        return {"action": "move_left", "parameters": {"distance_m": 1}}
    if t in ("稍微往前挪一下", "往前挪一点", "往前挪一下"):
        return {"action": "move_forward", "parameters": {"distance_m": 1}}
    if t in ("往后退一点点", "往后退一点", "稍微往后退一点", "往后退一米左右"):
        return {"action": "move_backward", "parameters": {"distance_m": 1}}
    if t in ("升高点", "稍微提高一点高度", "升高一点", "往上飞一点"):
        return {"action": "move_up", "parameters": {"distance_m": 2}}
    if t in ("稍微往下降一点", "下降点", "降低一点"):
        return {"action": "move_down", "parameters": {"distance_m": 1}}
    if t in ("云台稍微往下一点",):
        return {"action": "gimbal_down", "parameters": {"angle_deg": 10}}
    if t in ("镜头稍微放大一点", "放大一点"):
        return {"action": "camera_zoom", "parameters": {"value": 2}}
    if t in ("声音再大一点", "声音大一点", "音量再大一点"):
        return {"action": "speaker_volume", "parameters": {"value": 80}}

    vol_match = re.search(r"(?:把)?(?:喊话)?音量调到\s*(\d+)%?", t)
    if vol_match:
        return {"action": "speaker_volume", "parameters": {"value": int(vol_match.group(1))}}

    bright_match = re.search(r"(?:把)?(?:探照灯)?亮度(?:调到|开到)\s*(\d+)%?", t)
    if bright_match:
        return {"action": "searchlight_brightness", "parameters": {"value": int(bright_match.group(1))}}

    zoom_match = re.search(r"(?:把相机|调整相机)?变焦(?:调到|到)?\s*(\d+)\s*倍", t)
    if zoom_match:
        factor = float(zoom_match.group(1))
        return {"action": "camera_zoom", "parameters": {"value": int(factor) if factor.is_integer() else factor}}

    if "播放森林防火宣传语音" in t:
        return {"action": "speaker_speak", "parameters": {"text": "森林防火宣传语音"}}
    speak_match = re.search(r"(?:播放|开始喊话|喊话)(?:防溺水语音|森林防火宣传语音)?[‘'“\"]?([^'‘\"”]+)[’'”\"]?", t)
    if speak_match:
        text_content = speak_match.group(1).strip("‘'“\"’'”")
        return {"action": "speaker_speak", "parameters": {"text": text_content}}

    gimbal_down_match = re.search(r"(?:调整)?云台(?:向下看|下倾|俯仰(?:角度)?(?:调到|到)?)?\s*(-?\d+)\s*度?", t)
    if gimbal_down_match and "云台" in t:
        deg = float(gimbal_down_match.group(1))
        if deg > 0 and ("向下" in t or "下倾" in t):
            deg = -deg
        if deg == -90:
            return {"action": "gimbal_90", "parameters": {}}
        if deg == -45:
            return {"action": "gimbal_45", "parameters": {}}
        if deg == 0:
            return {"action": "gimbal_reset", "parameters": {}}
        if deg < 0:
            return {"action": "gimbal_down", "parameters": {"angle_deg": abs(deg)}}
        return {"action": "gimbal_up", "parameters": {"angle_deg": deg}}

    turn_left_match = re.search(r"(?:原地)?(?:向左|朝左|左)(?:转|偏航)\s*([0-9零一二两三四五六七八九十百]+)\s*度", t)
    if turn_left_match:
        deg = parse_cn_number(turn_left_match.group(1))
        if deg is not None:
            return {"action": "turn_left", "parameters": {"angle_deg": int(deg) if deg.is_integer() else deg}}

    turn_right_match = re.search(r"(?:原地)?(?:向右|朝右|右)(?:转|偏航)\s*([0-9零一二两三四五六七八九十百]+)\s*度", t)
    if turn_right_match:
        deg = parse_cn_number(turn_right_match.group(1))
        if deg is not None:
            return {"action": "turn_right", "parameters": {"angle_deg": int(deg) if deg.is_integer() else deg}}

    fwd_match = re.search(r"(?:向?前(?:面)?(?:飞|移动|平移)?|前进|往前(?:面)?(?:飞|移动|冲)?|朝机头方向前行)\s*([0-9零一二两三四五六七八九十百]+(?:\.\d+)?)\s*米", t)
    if fwd_match:
        dist = parse_cn_number(fwd_match.group(1))
        return {"action": "move_forward", "parameters": {"distance_m": int(dist) if dist.is_integer() else dist}}

    bwd_match = re.search(r"(?:向?后(?:飞|挪|移动)?|后退|往后(?:飞|挪|移动)?)\s*([0-9零一二两三四五六七八九十百]+(?:\.\d+)?)\s*米", t)
    if bwd_match:
        dist = parse_cn_number(bwd_match.group(1))
        return {"action": "move_backward", "parameters": {"distance_m": int(dist) if dist.is_integer() else dist}}

    left_match = re.search(r"(?:向?左(?:飞|移|平移|移动)?|左移|往左(?:飞|移|平移|移动)?)\s*([0-9零一二两三四五六七八九十百]+(?:\.\d+)?)\s*米", t)
    if left_match:
        dist = parse_cn_number(left_match.group(1))
        return {"action": "move_left", "parameters": {"distance_m": int(dist) if dist.is_integer() else dist}}

    right_match = re.search(r"(?:向?右(?:飞|移|平移|移动)?|右移|往右(?:飞|移|平移|移动)?)\s*([0-9零一二两三四五六七八九十百]+(?:\.\d+)?)\s*米", t)
    if right_match:
        dist = parse_cn_number(right_match.group(1))
        return {"action": "move_right", "parameters": {"distance_m": int(dist) if dist.is_integer() else dist}}

    up_match = re.search(r"(?:上升|向?上(?:飞|升)?|往上升|往上飞|升高)\s*([0-9零一二两三四五六七八九十百]+(?:\.\d+)?)\s*米", t)
    if up_match:
        dist = parse_cn_number(up_match.group(1))
        return {"action": "move_up", "parameters": {"distance_m": int(dist) if dist.is_integer() else dist}}

    down_match = re.search(r"(?:下降|向?下(?:飞|降)?|降低高度|往下降)\s*([0-9零一二两三四五六七八九十百]+(?:\.\d+)?)\s*米", t)
    if down_match:
        dist = parse_cn_number(down_match.group(1))
        return {"action": "move_down", "parameters": {"distance_m": int(dist) if dist.is_integer() else dist}}

    return None
