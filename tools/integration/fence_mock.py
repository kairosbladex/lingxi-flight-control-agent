"""电子围栏契约 mock：clear / blocked / invalid / timeout / unavailable 五类。

平台围栏（``/electronicFence/path-route/with-clean``）只回答“这条折线是否
碰到限制区”。本 mock 复刻它的响应形状，并给出五类可复现结论：

- ``clear``：全部 ``status=ok`` 且 ``converge=ln_outside``；
- ``blocked``：``invalid`` 非空（或某段 ``status=ng``）→ 调用方必须拒绝执行；
- ``invalid``：折线几何本身非法，**不调用平台**（由 ``path_geometry`` 判定）；
- ``timeout``：传输层抛 ``TimeoutError`` → 按 V1.4 2.2.2 降级；
- ``unavailable``：连接错误/5xx/查无服务 → 降级。

“降级”只跳过这一项检查并给出警告，不阻断业务流程，也不代表路径安全。
"""

from __future__ import annotations

from typing import Any, Mapping

from lingxi_flight_control.ktd_platform import KtdHttpResponse

SCENARIOS = ('clear', 'blocked', 'invalid', 'timeout', 'unavailable')


class FenceContractMock:
    """实现 ``KtdPathChecker`` 需要的 ``check_path(binding, points, *, timeout_sec)``。"""

    def __init__(self, scenario: str):
        if scenario not in SCENARIOS:
            raise ValueError(f'unknown fence scenario: {scenario}')
        self.scenario = scenario
        self.calls: list[dict[str, Any]] = []

    def check_path(self, binding, points, *, timeout_sec: float | None = None) -> KtdHttpResponse:
        if self.scenario == 'invalid':
            # 几何非法时 KtdPathChecker 在调用传输前就返回 invalid，这里被调用即回归。
            raise AssertionError('invalid geometry must not reach the fence transport')
        self.calls.append({
            'project_id': getattr(binding, 'project_id', None),
            'points': len(points), 'timeout_sec': timeout_sec,
        })
        if self.scenario == 'timeout':
            raise TimeoutError('synthetic fence timeout')
        if self.scenario == 'unavailable':
            raise ConnectionError('synthetic fence service unavailable')

        segments = max(len(points) - 1, 1)
        if self.scenario == 'blocked':
            data = {
                'projectId': binding.project_id,
                'operatorId': binding.actor_id,
                'snapshot': points,
                'invalid': [{'index': 0, 'reason': 'COLLISION_NO_FLY_ZONE'}],
                'segment': [{'status': 'ng', 'converge': 'ln_intersect'} for _ in range(segments)],
            }
        else:  # clear
            data = {
                'projectId': binding.project_id,
                'operatorId': binding.actor_id,
                'snapshot': points,
                'invalid': [],
                'segment': [{'status': 'ok', 'converge': 'ln_outside'} for _ in range(segments)],
            }
        return KtdHttpResponse(200, {'code': 200, 'msg': 'path evaluated', 'data': data})


def fence_state(**overrides: Any) -> dict[str, Any]:
    """围栏折线需要的姿态/高度字段；DJI 用 absolute ``altitude_m``。"""
    state: dict[str, Any] = {
        'longitude': 113.0, 'latitude': 23.0, 'yaw_deg': 90.0,
        'altitude_m': 100.0, 'height_m': 50.0, 'profile': 'DJI_HANGAR',
        'airborne': True, 'online': True,
    }
    state.update(overrides)
    return state
