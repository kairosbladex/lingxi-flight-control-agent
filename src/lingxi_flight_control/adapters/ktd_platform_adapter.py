"""平台快照与状态容错适配器 (KtdPlatformAdapter)。

依据 V1.4 2.2.3 规范：
在自动航线中必须拦截微操指令。当外部平台快照中 route_status 为 null 时，
非只读微操路径严格执行 fail-closed，抛出精准的 typed 错误码 ROUTE_STATE_SOURCE_UNAVAILABLE；
对于只读/查询类载荷指令，允许降级放行并保留诊断上下文。
"""

from __future__ import annotations

from typing import Any, Mapping

ROUTE_STATE_SOURCE_UNAVAILABLE = "ROUTE_STATE_SOURCE_UNAVAILABLE"

# 需由 route_status 严格把关的飞行微操与任务动作
MICRO_AND_FLIGHT_ACTIONS = frozenset({
    "move_forward", "move_backward", "move_left", "move_right",
    "move_up", "move_down", "turn_left", "turn_right", "hover",
    "point_flight", "point_orbit",
})

# 允许在 route_status 缺失时降级放行的只读或轻量查询动作
READ_ONLY_ACTIONS = frozenset({
    "read_state", "camera_zoom_query", "gimbal_attitude_query",
    "battery_query", "device_capabilities",
})


class RouteStatusUnavailableError(ValueError):
    """当 route_status 缺失且请求非只读动作时抛出精准 typed 异常。"""

    def __init__(self, message: str = "route_status is null or unavailable from platform snapshot",
                 *, diagnostic: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = ROUTE_STATE_SOURCE_UNAVAILABLE
        self.diagnostic = dict(diagnostic or {})

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "diagnostic": self.diagnostic,
        }


class KtdPlatformAdapter:
    """外部 Java 平台接口与快照健壮适配器。"""

    def __init__(self, *, fallback_policy: str = "fail_closed"):
        self.fallback_policy = fallback_policy

    def adapt_snapshot(self, raw_snapshot: Mapping[str, Any],
                       requested_action: str | None = None) -> dict[str, Any]:
        """解析平台快照，对 route_status 缺失执行安全门禁。"""
        snapshot = dict(raw_snapshot)
        route_status = snapshot.get("route_status")

        if route_status is None:
            # 检查请求动作
            if requested_action in MICRO_AND_FLIGHT_ACTIONS:
                raise RouteStatusUnavailableError(
                    f"Action '{requested_action}' blocked: platform route_status is unavailable",
                    diagnostic={
                        "raw_route_status": None,
                        "action": requested_action,
                        "fail_closed": True,
                    }
                )
            
            # 若是只读查询动作，标记降级诊断信息并放行
            snapshot["_degraded_route_status"] = True
            snapshot["_route_status_unavailable_reason"] = "PLATFORM_SNAPSHOT_NULL"

        return snapshot

    def format_route_control_payload(self, command_type: int) -> dict[str, Any]:
        """格式化航线控制下发报文 (暂停 120/恢复 122/返航 105/急停 113)。"""
        return {"type": command_type}

    def format_micro_operation_payload(self, op_type: int, mode: int = 2, param: float = 0.0) -> dict[str, Any]:
        """格式化微操下发报文 (移动 201/转向 202)。"""
        return {"type": op_type, "mode": mode, "param": param}
