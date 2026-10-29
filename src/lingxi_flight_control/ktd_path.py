"""Reuse the platform's path-route/with-clean checker; never adopt its reroute.

这一层只做三件事：把动作翻译成折线（``path_geometry``）、用**有界**传输发一次请求、
把响应解析成确定性的 ``PathCheck``。它不实现禁飞区、不做三维碰撞、不采用
``suggest`` / ``alternate`` 之类的绕飞建议，也不为超时创建后台线程或进程。

硬超时契约：``KtdPathChecker`` 自己声明 ``deadline_sec``，并把同一个预算通过
``check_path(..., timeout_sec=)`` 下发给 ``KtdHttpTransport``，最终成为 socket 超时。
传输层不接受单次预算时**不调用**它——没有 socket 超时的调用无法声明有界。
``hard_bounded`` 只有在传输层确实接收单次预算时才为真；否则本类只是普通同步
callable，不能装配到平台执行器。
"""

import time

from .audit import digest, emit
from .flight_policy import PathCheck, finite, timeout_keyword
from .path_geometry import (
    MAX_SEGMENTS, PathGeometryError, PathUnavailable, build_path,
)

#: 单次围栏检查的默认预算；超时按 V1.4 2.2.2 降级，不阻断业务流程。
DEFAULT_DEADLINE_SEC = 3.0
MIN_DEADLINE_SEC, MAX_DEADLINE_SEC = 0.1, 10.0


class KtdPathChecker:
    """平台围栏适配器：几何来自本仓，结论只认平台返回的确定性字段。"""

    # ``flight_policy.check_path`` 用它避免对已有 Ktd 审计重复记账。
    audit_emits = True

    def __init__(self, transport, *, deadline_sec=DEFAULT_DEADLINE_SEC, clock=time.monotonic):
        if not finite(deadline_sec) or not MIN_DEADLINE_SEC <= deadline_sec <= MAX_DEADLINE_SEC:
            raise ValueError("invalid path check deadline")
        if not callable(clock):
            raise ValueError("invalid path check clock")
        self.transport = transport
        self.deadline_sec = float(deadline_sec)
        self._clock = clock
        #: 传输层接收单次预算的关键字；None 表示没有有界调用接口。
        self._timeout_keyword = timeout_keyword(getattr(transport, "check_path", None))

    @property
    def hard_bounded(self):
        """只有传输层真的接收单次预算时才声明硬超时；否则不得进入平台装配。"""
        return self._timeout_keyword is not None

    def __call__(self, binding, action, parameters, state):
        started = self._clock()
        result = self._check(binding, action, parameters, state)
        elapsed = max(0.0, self._clock() - started)
        if result.status in {"clear", "blocked"} and elapsed > self.deadline_sec:
            # 迟到结论不可采信：只说明这次响应超出预算，**不代表调用已被停止**。
            result = PathCheck("timeout", "PATH_CHECK_DEADLINE_EXCEEDED",
                               segments=result.segments, vertical_field=result.vertical_field)
        result = PathCheck(result.status, result.reason, result.segments, result.segment_index,
                           result.vertical_field, int(elapsed * 1000))
        self._audit(binding, action, result, state)
        return result

    def _check(self, binding, action, parameters, state):
        if self.transport is None or not callable(getattr(self.transport, "check_path", None)):
            return PathCheck("unavailable", "PATH_CHECKER_UNAVAILABLE")
        if self._timeout_keyword is None:
            # 传输层没有单次预算参数：无法声明有界，也不调用。
            return PathCheck("unavailable", "PATH_TRANSPORT_UNBOUNDED")
        try:
            geometry = build_path(action, parameters, state)
        except PathGeometryError as error:
            return PathCheck("invalid", error.code)
        except PathUnavailable as error:
            return PathCheck("unavailable", error.reason)
        except Exception:
            return PathCheck("unavailable", "PATH_GEOMETRY_ERROR")
        if geometry is None:
            return PathCheck("unavailable", "PATH_ACTION_UNSUPPORTED")
        if geometry.segments < 1 or geometry.segments > MAX_SEGMENTS:
            return PathCheck("invalid", "INVALID_PATH_GEOMETRY")
        points = [dict(point) for point in geometry.points]
        try:
            response = self._send(binding, points)
        except Exception as error:
            # urllib 会把 socket 超时包成 URLError(reason=timeout)，这里一并识别。
            if _is_timeout(error):
                return PathCheck("timeout", "PATH_CHECK_TIMEOUT",
                                 segments=geometry.segments, vertical_field=geometry.vertical_field)
            return PathCheck("unavailable", "PATH_TRANSPORT_ERROR",
                             segments=geometry.segments, vertical_field=geometry.vertical_field)
        return _parse(response, binding, points, geometry)

    def _send(self, binding, points):
        return self.transport.check_path(binding, points,
                                         **{self._timeout_keyword: self.deadline_sec})

    def _audit(self, binding, action, result, state):
        # 只记录类型、计数和摘要，不记录经纬度、半径或平台响应原文。
        state = state if isinstance(state, dict) else {}
        emit("flight_path_check", action=str(action), status=result.status,
             reason=result.reason or None, segments=result.segments,
             segment_index=result.segment_index, vertical_field=result.vertical_field or None,
             elapsed_ms=result.elapsed_ms, deadline_ms=int(self.deadline_sec * 1000),
             trace_ref=digest(state.get("trace_id")),
             command_ref=digest(state.get("command_id")),
             drone_ref=digest(getattr(binding, "drone_id", None)))


def _is_timeout(error):
    return isinstance(error, TimeoutError) or isinstance(getattr(error, "reason", None), TimeoutError)


def _parse(response, binding, points, geometry):
    """平台响应必须逐项对得上请求，否则按 malformed 降级，不猜。"""
    segments = geometry.segments
    if response is None or getattr(response, "status_code", None) != 200:
        return PathCheck("unavailable", "PATH_CHECK_REJECTED", segments, None, geometry.vertical_field)
    payload = getattr(response, "payload", None)
    if not isinstance(payload, dict) or payload.get("code") != 200:
        return PathCheck("unavailable", "PATH_CHECK_REJECTED", segments, None, geometry.vertical_field)
    data = payload.get("data")
    if not isinstance(data, dict):
        return PathCheck("malformed", "PATH_RESPONSE_MALFORMED", segments, None, geometry.vertical_field)
    if data.get("projectId") != binding.project_id or data.get("operatorId") != binding.actor_id:
        # 返回了别的项目/操作者的结论，不能算作本次检查。
        return PathCheck("unavailable", "PATH_RESPONSE_SCOPE_MISMATCH", segments, None,
                         geometry.vertical_field)
    if data.get("snapshot") != points:
        return PathCheck("malformed", "PATH_RESPONSE_SNAPSHOT_MISMATCH", segments, None,
                         geometry.vertical_field)
    invalid = data.get("invalid")
    entries = data.get("segment")
    if not isinstance(invalid, list) or not isinstance(entries, list) or len(entries) != segments:
        return PathCheck("malformed", "PATH_RESPONSE_MALFORMED", segments, None, geometry.vertical_field)
    if invalid:
        # 平台明确列出非法点/段；不解析其内部结构，也不读 suggest 绕飞。
        return PathCheck("blocked", "PATH_INVALID_SEGMENT", segments, _invalid_index(invalid),
                         geometry.vertical_field)
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or entry.get("status") not in {"ok", "ng"}:
            return PathCheck("malformed", "PATH_RESPONSE_MALFORMED", segments, index,
                             geometry.vertical_field)
        if entry["status"] == "ng":
            return PathCheck("blocked", "PATH_SEGMENT_BLOCKED", segments, index,
                             geometry.vertical_field)
        if entry.get("converge") != "ln_outside":
            # 无法确认折线在限制区外侧，只能降级，不能说 clear。
            return PathCheck("unavailable", "PATH_CONVERGE_UNVERIFIED", segments, index,
                             geometry.vertical_field)
    return PathCheck("clear", "", segments, None, geometry.vertical_field)


def _invalid_index(invalid):
    for item in invalid:
        if isinstance(item, dict) and type(item.get("index")) is int and item["index"] >= 0:
            return item["index"]
    return None
