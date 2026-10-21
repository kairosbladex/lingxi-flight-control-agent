"""结构化审计只记录类型、数值和关联摘要，不保存凭据或业务原文。"""
import hashlib
import json
import math
import os
import re
import time


def digest(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:24] if value else None


def identifier(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value) else None


def numbers(values):
    if not isinstance(values, dict):
        return {}
    return {key: value for key, value in values.items() if key in {
        'distance_m', 'angle_deg', 'value', 'min_c', 'max_c', 'type', 'mode',
        'param', 'cameraMode', 'controlMode', 'stepPercentage', 'zoomRatio', 'interrupt'}
        and isinstance(value, (int, float)) and math.isfinite(value)}


def tool_parameters(action, values):
    result=numbers(values)
    from .java_tools import TOOLS
    spec=TOOLS.get(action)
    if spec and isinstance(values,dict):
        # 仅保留注册枚举/布尔，不记录地理位置、图像目标坐标或任意文本。
        for key,parameter in spec.parameters.items():
            value=values.get(key)
            if parameter.enum is not None and isinstance(value,str) and value in parameter.enum:
                result[key]=value
            elif parameter.type=='boolean' and type(value) is bool:
                result[key]=value
    return result


def emit(event, **fields):
    # 上层只传固定字段，stdout由服务管理器持久化；失败不触发业务重发。
    try:
        print(json.dumps({'event': event, 'audit_version': 1, 'time': time.time(),
            'code_sha': identifier(os.environ.get('FLIGHT_AGENT_CODE_SHA')), **fields},
            ensure_ascii=False, allow_nan=False), flush=True)
    except (OSError, ValueError):
        pass


def gateway_event(operation, body, headers, response, elapsed_ms):
    try:
        request = json.loads(body) if body else {}
    except (ValueError, TypeError):
        request = {}
    if not isinstance(request, dict):
        request = {}
    headers = {str(k).lower(): v for k,v in (headers or {}).items()}
    payload = response.payload
    card = payload.get('card') or {}
    if not isinstance(card, dict):
        card = {}
    emit('flight_gateway', operation=operation, status=response.status_code,
        result=identifier(payload.get('status')), error_code=identifier(payload.get('error_code')),
        interpretation_source=identifier(payload.get('interpretation_source')),
        task_ref=digest(headers.get('x-flight-task')), drone_ref=digest(headers.get('x-flight-drone')),
        session_ref=digest(headers.get('x-agent-session')), input_ref=digest(request.get('text')),
        request_id=identifier(request.get('request_id')),
        command_id=identifier(card.get('command_id') or payload.get('command_id') or request.get('command_id')),
        trace_id=identifier(payload.get('trace_id')),
        action=identifier(card.get('action') or payload.get('action')),
        parameters=tool_parameters(card.get('action') or payload.get('action'),
                                   card.get('parameters') or payload.get('parameters')),
        device_effect=identifier(payload.get('device_effect')),
        platform_ui_distance_mode=payload.get('platform_ui_distance_mode') is True,
        elapsed_ms=elapsed_ms)
