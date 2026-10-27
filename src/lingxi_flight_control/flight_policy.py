"""V1.3/V1.4 deterministic business rules; inputs come from the bound executor."""
from dataclasses import dataclass, replace
import inspect
import math
import time
from typing import Mapping

POLICY_VERSION = "flight-control-v1.3"
CONTROL_MESSAGE = "您尚未获取无人机控制权，请先申请后再下发指令。"
ROUTE_MESSAGE = "当前正在执行自动航线，请先说‘暂停航线’或‘悬停’再操作。"
MAPPING_MESSAGE = "中断将导致测绘成果失效，是否确认继续？确认后关闭自动拼图，原测绘任务不可恢复。"
MOVES = frozenset({"move_forward", "move_backward", "move_left", "move_right", "move_up", "move_down"})
MICRO_ACTIONS = MOVES | {"turn_left", "turn_right"}
TEMPORARY_ACTIONS = frozenset({"point_flight", "point_orbit"})
PATH_ACTIONS = MOVES | TEMPORARY_ACTIONS
INTERRUPTING_ACTIONS = MICRO_ACTIONS | TEMPORARY_ACTIONS | {"route_pause", "hover", "emergency_stop", "return_home", "land", "land_alternate"}
INTERRUPTING_ACTIONS = INTERRUPTING_ACTIONS | {'task_end_current', 'task_end_all'}
RESUME_ACTIONS = frozenset({"route_resume", "continue_from_breakpoint"})
MAPPING_TASKS = frozenset({"mapping", "orthophoto", "oblique", "high_precision_modeling"})

# These are protocol envelopes, not claims about any connected device.
ADJUSTABLE = {
    'speaker_volume': ('value', 0, 100),
    'searchlight_brightness': ('value', 0, 100),
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
    if action in RESUME_ACTIONS and (state.get("mapping_invalidated") is True
                                     or state.get("route_resume_disabled") is True):
        raise PolicyError("MAPPING_NOT_RESUMABLE", "原测绘任务已被中断，成果失效，不可恢复航线。")
    # V1.4 2.2.3：自动航线执行中拦截平移/升降/转向。指点飞行与环绕按 4.1.1
    # 作为“临时变更”接管并保留主线断点，不属于本条拦截范围（见验收文档解释分歧）。
    if (action in MICRO_ACTIONS
            and state.get("route_status") == "running"
            and not mapping_interruption(action, state)):
        raise PolicyError("ROUTE_PAUSE_REQUIRED", ROUTE_MESSAGE)
    if state.get("task_status") == "returning_home" and state.get("low_battery_return") is True and action in MICRO_ACTIONS | TEMPORARY_ACTIONS | RESUME_ACTIONS | {"cancel_return_home"}:
        raise PolicyError("LOW_BATTERY_RETURN_PRIORITY", "当前正在低电量返航，不得覆盖平台返航流程。")


@dataclass(frozen=True)
class PathCheck:
    """围栏检查结果；只有 ``clear`` 表示检查通过。

    ``timeout`` / ``unavailable`` / ``malformed`` 都按 V1.4 2.2.2 降级（跳过该项检查
    并明确警告），``blocked`` 拒绝执行，``invalid`` 表示折线几何本身非法（拒绝执行）。
    """

    status: str  # clear, blocked, unavailable, timeout, malformed, invalid
    reason: str = ""
    segments: int = 0
    segment_index: int | None = None
    vertical_field: str = ""
    elapsed_ms: int | None = None

    @property
    def degraded(self):
        return self.status in DEGRADED_STATUSES


PATH_STATUSES = frozenset({"clear", "blocked", "unavailable", "timeout", "malformed", "invalid"})
DEGRADED_STATUSES = frozenset({"unavailable", "timeout", "malformed"})
#: 降级话术必须让人看出「检查没做成」，不能读成「路径安全」。
DEGRADED_PREFIX = {"unavailable": "电子围栏接口不可用", "timeout": "电子围栏检查超时",
                   "malformed": "电子围栏响应无法解析"}
DEGRADED_WARNING = ("{prefix}（{reason}），已按 V1.4 2.2.2 跳过该项检查并继续业务流程；"
                    "这不代表路径安全或设备避障已验证。")


@dataclass(frozen=True)
class PathOutcome:
    """一次围栏检查的结构化结论；审计只记录这里的字段，不记录坐标原文。"""

    check: PathCheck
    warning: str | None
    action: str

    @property
    def blocked(self):
        return self.check.status == "blocked"


#: provider 契约分层：预算来源不同，能给出的保证也完全不同。
#:
#: - ``hard_bounded``：预算交给底层 transport（socket 超时）真正结束等待；
#: - ``cooperative``：普通同步 callable 自己声明 ``deadline_sec``，只是协作式承诺；
#: - ``unbounded``：没有可核验预算，preview/confirm 直接降级且不调用。
HARD_BOUNDED = "hard_bounded"
COOPERATIVE = "cooperative"
UNBOUNDED = "unbounded"

#: 单次预算必须是有限秒数，并落在可执行范围内。
DEFAULT_PROVIDER_DEADLINE_SEC = 3.0
MIN_PROVIDER_DEADLINE_SEC, MAX_PROVIDER_DEADLINE_SEC = 0.1, 10.0
#: provider 用 ``check_path(..., **kw)`` 显式接收单次预算时允许的关键字。
TIMEOUT_KEYWORDS = ("timeout_sec", "deadline")


@dataclass(frozen=True)
class HardBoundedPathProvider:
    """等待由底层 transport 真正限制的 provider 契约。

    只有两种来源可以进入这一层：

    - 可调用对象显式接收 ``timeout_sec`` / ``deadline`` 关键字（含 ``**kwargs``），
      并把预算交给 socket / 底层传输；
    - 可调用对象显式标记 ``hard_bounded = True`` 并声明 ``deadline_sec``，内部把
      预算下发给硬超时传输（例如 ``KtdPathChecker`` → ``KtdHttpTransport``）。

    **边界**：本仓不创建线程、进程或异步调度任务去中断调用。这里的“硬”指底层
    transport 会结束等待，不是本模块有能力杀死任意 Python 调用。
    """

    call: object
    deadline_sec: float
    keyword: str = ""
    contract: str = HARD_BOUNDED

    @property
    def stops_waiting(self):
        return True


@dataclass(frozen=True)
class CooperativePathProvider:
    """只声明 ``deadline_sec``、没有硬超时传输的普通同步 callable。

    预算只是协作式承诺：provider 违约时本仓**无法**停止等待，也不能把“返回得晚”
    当作“已经停止”。只允许模拟器与离线测试使用（``allow_cooperative=True``），
    不得装配到平台执行器。
    """

    call: object
    deadline_sec: float
    keyword: str = ""
    contract: str = COOPERATIVE

    @property
    def stops_waiting(self):
        return False


@dataclass(frozen=True)
class UnboundedPathProvider:
    """没有任何可核验预算的 provider；preview/confirm 直接降级且不调用。"""

    reason: str = "PATH_CHECKER_UNAVAILABLE"
    contract: str = UNBOUNDED

    @property
    def stops_waiting(self):
        return False


def timeout_keyword(candidate):
    """返回可调用对象接收单次预算的关键字名；不接受预算时返回 None。"""
    if not callable(candidate):
        return None
    try:
        parameters = inspect.signature(candidate).parameters
    except (TypeError, ValueError):
        return None
    for name in TIMEOUT_KEYWORDS:
        parameter = parameters.get(name)
        if parameter is not None and parameter.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY):
            return name
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        return "timeout_sec"
    return None


def _declared_deadline(provider):
    """(预算, 是否声明过)；声明了但不可用时预算为 None。"""
    value = getattr(provider, "deadline_sec", None)
    if value is None:
        return None, False
    if not finite(value) or not MIN_PROVIDER_DEADLINE_SEC <= value <= MAX_PROVIDER_DEADLINE_SEC:
        return None, True
    return float(value), True


def _binds_path_call(candidate, keyword):
    """契约形状必须是 (binding, action, parameters, state, *, <budget>=)。"""
    try:
        inspect.signature(candidate).bind(None, None, None, None, **{keyword: 0.0})
    except (TypeError, ValueError):
        return False
    return True


def _budget_keyword(candidate):
    """既声明了单次预算关键字，又能按 4 参数 provider 形状调用时返回关键字名。"""
    keyword = timeout_keyword(candidate)
    if keyword is None or not _binds_path_call(candidate, keyword):
        return None
    return keyword


def classify_path_provider(provider):
    """在**调用前**判定 provider 属于哪一层契约；调用方据此决定是否允许调用。

    - 显式接收 ``timeout_sec`` / ``deadline``，或标记 ``hard_bounded = True``
      且声明了 ``deadline_sec`` → :class:`HardBoundedPathProvider`；
    - 只声明 ``deadline_sec`` 的普通同步 callable → :class:`CooperativePathProvider`；
    - 其余（``None``、签名不匹配、预算不可用、显式否认硬超时）→
      :class:`UnboundedPathProvider`。

    **只声明 ``deadline_sec`` 不等于有界**：普通 callable 无法被本仓强制中断，
    因此默认不得进入平台执行路径。
    """
    if provider is None:
        return UnboundedPathProvider("PATH_CHECKER_UNAVAILABLE")
    declared, has_declaration = _declared_deadline(provider)
    if has_declaration and declared is None:
        # 声明了 0、负数、无穷或超出范围的预算：等于没有可用预算。
        return UnboundedPathProvider("PATH_CHECKER_UNBOUNDED")
    method = getattr(provider, "check_path", None)
    keyword = _budget_keyword(method)
    if keyword is not None:
        deadline = declared if declared is not None else DEFAULT_PROVIDER_DEADLINE_SEC
        return HardBoundedPathProvider(method, deadline, keyword)
    if callable(provider):
        keyword = _budget_keyword(provider)
        if keyword is not None:
            deadline = declared if declared is not None else DEFAULT_PROVIDER_DEADLINE_SEC
            return HardBoundedPathProvider(provider, deadline, keyword)
    marker = getattr(provider, "hard_bounded", None)
    if marker is True and callable(provider) and declared is not None:
        # provider 自己把预算下发给硬超时 transport（如 KtdPathChecker）。
        return HardBoundedPathProvider(provider, declared)
    if marker is False:
        # 显式声明“我做不到硬超时”：不允许退化成协作式而混入平台路径。
        return UnboundedPathProvider("PATH_CHECKER_UNBOUNDED")
    if callable(provider) and declared is not None:
        return CooperativePathProvider(provider, declared)
    return UnboundedPathProvider("PATH_CHECKER_UNBOUNDED")


def platform_path_provider_rejection(provider):
    """平台执行器的装配期校验；返回拒绝原因，``None`` 表示可以装配。

    只有「只声明 ``deadline_sec`` 的普通同步 callable」会被直接拒绝：它对外给出了
    预算承诺，却没有底层硬超时可以兑现，装配到平台路径就会产生虚假的硬超时保证。
    ``None`` 与完全无界的 provider 允许装配，但 preview/confirm 一律降级为
    ``PATH_CHECKER_UNAVAILABLE`` 且不调用——保留 V1.4「围栏不可用则降级继续」语义。
    """
    if provider is None:
        return None
    if isinstance(classify_path_provider(provider), CooperativePathProvider):
        return ("PATH_CHECKER_UNAVAILABLE: cooperative-only path provider cannot be "
                "assembled into a platform executor")
    return None


def _invoke_bounded(bounded, binding, action, parameters, state):
    if bounded.keyword:
        return bounded.call(binding, action, parameters, state,
                            **{bounded.keyword: bounded.deadline_sec})
    return bounded.call(binding, action, parameters, state)


def evaluate_path(provider, binding, action, parameters, state, *, clock=time.monotonic,
                  allow_cooperative=False):
    """执行一次围栏检查并返回结构化结论；只有路径类动作才返回非 None。

    契约在**调用前**判定：

    - ``HardBoundedPathProvider``：允许调用，预算由底层 transport 强制结束等待；
    - ``CooperativePathProvider``：默认**不调用**，按 ``PATH_CHECKER_UNAVAILABLE``
      降级；只有 ``allow_cooperative=True``（模拟器 / 离线测试）才允许调用；
    - ``UnboundedPathProvider``：任何情况下都不调用，直接降级。

    本函数不会用线程、进程或异步调度去中断调用，也不把“返回得晚”当作“已经停止”。
    """
    if action not in PATH_ACTIONS:
        return None
    contract = classify_path_provider(provider)
    if isinstance(contract, CooperativePathProvider) and not allow_cooperative:
        # 平台执行路径拒绝协作式 provider：不调用，按接口不可用降级（V1.4 2.2.2）。
        reason = "PATH_CHECKER_UNAVAILABLE"
        return PathOutcome(PathCheck("unavailable", reason),
                           _degraded("unavailable", reason), action)
    if isinstance(contract, UnboundedPathProvider):
        return PathOutcome(PathCheck("unavailable", contract.reason),
                           _degraded("unavailable", contract.reason), action)
    started = clock()
    try:
        result = _invoke_bounded(contract, binding, action, dict(parameters), dict(state))
    except TimeoutError:
        # 有界 transport 超时：底层已停止等待，结果一律不可用。
        return PathOutcome(PathCheck("timeout", "PATH_CHECK_TIMEOUT"),
                           _degraded("timeout", "PATH_CHECK_TIMEOUT"), action)
    except Exception:
        return PathOutcome(PathCheck("unavailable", "PATH_CHECK_ERROR"),
                           _degraded("unavailable", "PATH_CHECK_ERROR"), action)
    elapsed = max(0.0, clock() - started)
    if result is None:
        # 适配器没有给出结论，等价于接口不可用，不是「响应格式错误」。
        return PathOutcome(PathCheck("unavailable", "PATH_CHECK_UNAVAILABLE"),
                           _degraded("unavailable", "PATH_CHECK_UNAVAILABLE"), action)
    if not isinstance(result, PathCheck) or result.status not in PATH_STATUSES:
        return PathOutcome(PathCheck("malformed", "PATH_CHECK_MALFORMED"),
                           _degraded("malformed", "PATH_CHECK_MALFORMED"), action)
    if result.status in {"clear", "blocked"} and elapsed > contract.deadline_sec:
        # 结论迟到：只表示这次响应超出预算、不可采信，**不表示调用已被停止**。
        return PathOutcome(PathCheck("timeout", "PATH_CHECK_DEADLINE_EXCEEDED",
                                     elapsed_ms=int(elapsed * 1000)),
                           _degraded("timeout", "PATH_CHECK_DEADLINE_EXCEEDED"), action)
    result = replace(result, elapsed_ms=result.elapsed_ms if result.elapsed_ms is not None else int(elapsed * 1000))
    if result.status == "clear":
        return PathOutcome(result, None, action)
    if result.status == "blocked":
        return PathOutcome(result, None, action)
    if result.status == "invalid":
        return PathOutcome(result, None, action)
    return PathOutcome(result, _degraded(result.status, result.reason or "PATH_CHECK_UNAVAILABLE"), action)


def _degraded(status, reason):
    return DEGRADED_WARNING.format(prefix=DEGRADED_PREFIX.get(status, DEGRADED_PREFIX["unavailable"]),
                                   reason=reason)


BLOCKED_MESSAGE = ("指令已拦截，平台检测到路径存在障碍物、禁飞区或限高，为了安全未执行移动。")
ORBIT_BLOCKED_MESSAGE = "指令已拦截，环绕路径存在禁飞区。"


def check_path(provider, binding, action, parameters, state, *, allow_cooperative=False):
    """A missing fence service degrades only this check, never authorization.

    ``blocked`` 与几何非法都抛 ``PolicyError``，调用方据此拒绝执行并撤销待确认卡；
    其余失败只返回 warning，业务继续。``allow_cooperative`` 只给模拟器与离线测试
    使用：平台执行器必须保持默认 ``False``，否则协作式 provider 会被误当成有界。
    """
    outcome = evaluate_path(provider, binding, action, parameters, state,
                            allow_cooperative=allow_cooperative)
    if outcome is None:
        return None
    if outcome.check.status == "blocked":
        message = ORBIT_BLOCKED_MESSAGE if action == "point_orbit" else BLOCKED_MESSAGE
        raise PolicyError("PATH_BLOCKED", message)
    if outcome.check.status == "invalid":
        raise PolicyError(outcome.check.reason or "INVALID_PATH_GEOMETRY",
                          "路径几何参数无效，未执行。")
    return outcome.warning
