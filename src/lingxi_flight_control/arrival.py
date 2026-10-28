"""到位判定只看命令发出后的另一次位置读数，不看平台回执状态。

位移、指点飞行、环绕进入之后，如果没有新的位置样本，或者样本对不上
预计落点，调用方必须停止，不能自动发下一条。
"""

from typing import Mapping

from .flight_policy import MOVES, PATH_ACTIONS, finite
from .geometry import horizontal_distance_m, relative_target
from .path_geometry import ORBIT_RADIUS_TOLERANCE_M

#: 水平落点和高度的允许误差。模拟器按同一套球面公式落点，1 米内视为同一点。
ARRIVAL_TOLERANCE_M = 1.0
_FRESH_KEYS = ("revision", "snapshot_version", "observed_at")


def arrival_observed(action, parameters, before, after) -> bool:
    """``after`` 必须是命令前样本之后的新读数，并且落在该动作的预计位置。"""
    if action not in PATH_ACTIONS:
        return True
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        return False
    if not isinstance(parameters, Mapping) or not _fresh(before, after):
        return False
    try:
        if action == "point_flight":
            return _at_point(parameters, after)
        if action == "point_orbit":
            return _on_orbit(parameters, after)
        if action in MOVES:
            return _after_move(action, parameters, before, after)
    except (TypeError, ValueError):
        return False
    return False


def _fresh(before, after) -> bool:
    """同一份快照里的坐标不能当作到位回读。"""
    for key in _FRESH_KEYS:
        if key in before and key in after and before.get(key) != after.get(key) and after.get(key) is not None:
            return True
    return False


def _heading(state):
    for key in ("heading_deg", "yaw_deg"):
        value = state.get(key)
        if finite(value):
            return float(value)
    return None


def _pair(state, key):
    value = state.get(key)
    return float(value) if finite(value) else None


def _horizontal(before):
    longitude, latitude = _pair(before, "longitude"), _pair(before, "latitude")
    if longitude is None or latitude is None:
        return None
    return longitude, latitude


def _close_horizontal(left, right) -> bool:
    return horizontal_distance_m(left[0], left[1], right[0], right[1]) <= ARRIVAL_TOLERANCE_M


def _vertical_fields(before, after, delta):
    checked = False
    for key in ("altitude_m", "height_m"):
        start, end = _pair(before, key), _pair(after, key)
        if start is None or end is None:
            continue
        checked = True
        if abs(end - (start + delta)) > ARRIVAL_TOLERANCE_M:
            return False
    return checked


def _after_move(action, parameters, before, after) -> bool:
    distance = parameters.get("distance_m")
    if not finite(distance) or distance <= 0:
        return False
    distance = float(distance)
    start = _horizontal(before)
    end = _horizontal(after)
    if start is None or end is None:
        return False
    if action in {"move_up", "move_down"}:
        delta = distance if action == "move_up" else -distance
        return _close_horizontal(start, end) and _vertical_fields(before, after, delta)
    heading = _heading(before)
    if heading is None:
        return False
    expected = relative_target(start[0], start[1], heading, action.removeprefix("move_"), distance)
    return _close_horizontal(expected, end) and _vertical_fields(before, after, 0.0)


def _at_point(parameters, after) -> bool:
    target = _horizontal(parameters)
    observed = _horizontal(after)
    altitude = _pair(parameters, "altitude_m")
    observed_altitude = _pair(after, "altitude_m")
    if target is None or observed is None or altitude is None or observed_altitude is None:
        return False
    return (_close_horizontal(target, observed)
            and abs(observed_altitude - altitude) <= ARRIVAL_TOLERANCE_M)


def _on_orbit(parameters, after) -> bool:
    center = _horizontal(parameters)
    observed = _horizontal(after)
    radius = parameters.get("radius_m")
    if center is None or observed is None or not finite(radius) or radius <= 0:
        return False
    distance = horizontal_distance_m(observed[0], observed[1], center[0], center[1])
    return abs(distance - float(radius)) <= ORBIT_RADIUS_TOLERANCE_M
