"""V1.3 deterministic business rules; inputs come from the bound executor."""
from dataclasses import dataclass, replace
import math
from typing import Mapping

POLICY_VERSION = "flight-control-v1.3"
CONTROL_MESSAGE = "您尚未获取无人机控制权，请先申请后再下发指令。"
ROUTE_MESSAGE = "当前正在执行自动航线，请先说‘暂停航线’或‘悬停’再操作。"
MAPPING_MESSAGE = "中断将导致测绘成果失效，是否确认继续？确认后关闭自动拼图，原测绘任务不可恢复。"
MOVES = frozenset({"move_forward", "move_backward", "move_left", "move_right", "move_up", "move_down"})
MICRO_ACTIONS = MOVES | {"turn_left", "turn_right"}
TEMPORARY_ACTIONS = frozenset({"point_flight", "point_orbit"})
PATH_ACTIONS = MOVES | TEMPORARY_ACTIONS
INTERRUPTING_ACTIONS = MICRO_ACTIONS | TEMPORARY_ACTIONS | {"route_pause", "emergency_stop", "return_home", "land", "land_alternate"}
RESUME_ACTIONS = frozenset({"route_resume", "continue_from_breakpoint"})
MAPPING_TASKS = frozenset({"mapping", "orthophoto", "oblique", "high_precision_modeling"})

# These are protocol envelopes, not claims about any connected device.
ADJUSTABLE = {
    "camera_zoom": ("value", 1, 30),
    "gimbal_digital_zoom": ("value", 1, 30),
    "set_move_speed": ("value", 0.1, 50),
    "set_vertical_speed": ("value", 0.1, 50),
    "set_roll_speed": ("value", 0.1, 360),
    "set_pitch_speed": ("value", 0.1, 360),
    "set_yaw_speed": ("value", 0.1, 360),
    "gimbal_up": ("angle_deg", 0.1, 90),
    "gimbal_down": ("angle_deg", 0.1, 90),
}


class PolicyError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def finite(value):
    return type(value) in (float, int) and math.isfinite(value)


def proposal_parameters(action, parameters):
    """Accept the user's raw setting; only the executor may clamp it."""
    parameters = dict(parameters)
    if action in ADJUSTABLE:
        key = ADJUSTABLE[action][0]
        parameters[key] = replace(parameters[key], minimum=None, maximum=None,
                                  exclusive_minimum=False)
    return parameters


def normalize_parameters(action, parameters, state):
    result, adjustments = dict(parameters), []
    if action not in ADJUSTABLE:
        return result, adjustments
    key, protocol_low, protocol_high = ADJUSTABLE[action]
    if key not in result:
        return result, adjustments
    value = result[key]
    if not finite(value):
        raise PolicyError("INVALID_PARAMETERS", "参数必须是有限数值。")
    limits = state.get("device_limits", {})
    limit = limits.get(action) if isinstance(limits, Mapping) else None
    if limit is None:
        if not protocol_low <= value <= protocol_high:
            raise PolicyError("DEVICE_LIMITS_UNAVAILABLE", "未取得当前设备参数表，不能猜测设备极值。")
        return result, adjustments
    if (not isinstance(limit, Mapping) or not finite(limit.get("min"))
            or not finite(limit.get("max")) or limit["min"] > limit["max"]):
        raise PolicyError("DEVICE_LIMITS_UNAVAILABLE", "当前设备参数表无效。")
    low, high = max(protocol_low, limit["min"]), min(protocol_high, limit["max"])
    if action in {"gimbal_up", "gimbal_down"}:
        pitch = state.get("gimbal_pitch_deg")
        pitch_limits = limits.get("gimbal_pitch_deg")
        if not finite(pitch) or not isinstance(pitch_limits, Mapping):
            raise PolicyError("DEVICE_LIMITS_UNAVAILABLE", "缺少当前云台角度或俯仰限值。")
        lower, upper = pitch_limits.get("min"), pitch_limits.get("max")
        if not finite(lower) or not finite(upper) or not -90 <= lower <= pitch <= upper <= 90:
            raise PolicyError("DEVICE_LIMITS_UNAVAILABLE", "当前云台角度或俯仰限值无效。")
        high = min(high, upper - pitch if action == "gimbal_up" else pitch - lower)
    if low > high:
        raise PolicyError("DEVICE_LIMIT_REACHED", "当前设备已到达可用极限，未执行调整。")
    effective = min(high, max(low, value))
    if effective != value:
        result[key] = effective
        adjustments.append({"parameter": key, "requested": value, "effective": effective,
                            "minimum": low, "maximum": high,
                            "message": f"当前设备允许范围为{low:g}至{high:g}，已将{value:g}调整为{effective:g}；确认后执行。"})
    return result, adjustments


def mapping_interruption(action, state):
    return (state.get("task_type") in MAPPING_TASKS
            and not state.get("mapping_invalidated", False)
            and state.get("route_status") in {"running", "paused", "point_flight", "point_orbit"}
            and action in INTERRUPTING_ACTIONS)


def check_flight_policy(action, state):
    if action in RESUME_ACTIONS and state.get("mapping_invalidated") is True:
        raise PolicyError("MAPPING_NOT_RESUMABLE", "原测绘任务已被中断，成果失效，不可恢复航线。")
    if action in MICRO_ACTIONS and state.get("route_status") == "running" and not mapping_interruption(action, state):
        raise PolicyError("ROUTE_PAUSE_REQUIRED", ROUTE_MESSAGE)
    if state.get("task_status") == "returning_home" and state.get("low_battery_return") is True and action in MICRO_ACTIONS | TEMPORARY_ACTIONS | RESUME_ACTIONS | {"cancel_return_home"}:
        raise PolicyError("LOW_BATTERY_RETURN_PRIORITY", "当前正在低电量返航，不得覆盖平台返航流程。")


@dataclass(frozen=True)
class PathCheck:
    status: str  # clear, blocked, unavailable
    reason: str = ""


def check_path(provider, binding, action, parameters, state):
    """A missing fence service degrades only this check, never authorization."""
    if action not in PATH_ACTIONS:
        return None
    try:
        result = provider(binding, action, dict(parameters), dict(state)) if provider else None
    except Exception:
        result = None
    if isinstance(result, PathCheck) and result.status == "blocked":
        raise PolicyError("PATH_BLOCKED", "指令已拦截，平台检测到路径存在障碍物、禁飞区或限高，为了安全未执行移动。")
    if not isinstance(result, PathCheck) or result.status != "clear":
        return "电子围栏接口不可用，已按V1.3跳过该项检查；不代表路径安全或设备避障已验证。"
    return None
