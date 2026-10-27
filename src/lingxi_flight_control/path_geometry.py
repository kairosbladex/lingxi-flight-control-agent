"""V1.4 路径几何：只生成待校验折线，不决定放行，也不采用平台的绕飞建议。

本模块做三件确定性的事：

1. 把「六向微操 / 指点飞行 / 环绕」翻译成**待校验折线**（点序列 + 分段数）；
2. 明确每个点用的是哪个高度字段（相对高度还是与飞控高度同基准的绝对高度），
   不允许把两个基准混进同一条折线；
3. 几何参数本身非法时抛 ``PathGeometryError``（拒绝执行），
   只是**算不出来**时抛 ``PathUnavailable``（按 V1.4 降级并警告，不阻断）。

这里不判断禁飞区、不做三维碰撞、不调用平台，也不产生任何后台资源。
"""

from dataclasses import dataclass

from .geometry import HORIZONTAL_BEARINGS, horizontal_distance_m, offset_target
from .flight_policy import finite

#: 环绕闭合曲线的最少分段数（V1.4 4.3.1「检查环绕半径路径安全」的落地口径）。
ORBIT_SEGMENTS = 36
#: 单次围栏请求允许的最大分段数，避免超长折线把上游和本进程拖住。
MAX_SEGMENTS = 64

#: 各动作实际下发的高度基准：/screen/uav/fly/control 用 altitude_m（见 _compile_fly），
#: 六向微操按相对高度下发（PZ 用 height_m）。折线必须用与指令相同的基准，
#: 否则「路径检查通过」说的就是另一条高度上的路径。
_MOVE_FIELD_ORDER = ("height_m", "altitude_m")
_FLY_FIELD_ORDER = ("altitude_m", "height_m")
#: 各基准的合法区间，和平台快照校验保持一致，不额外发明数值。
VERTICAL_RANGES = {"height_m": (-500.0, 10000.0), "altitude_m": (-500.0, 10000.0)}
#: 相对高度不允许穿到地面以下。
VERTICAL_FLOOR = {"height_m": 0.0, "altitude_m": None}
#: 指点飞行的目标高度走 /screen/uav/fly/control，官方范围 2–10000 米。
FLY_TARGET_ALTITUDE_RANGE = (2.0, 10000.0)
MAX_RADIUS_M = 5000.0
MAX_DISTANCE_M = 1000.0
#: 请求半径与当前水平距离的允许偏差。超过这个值，围栏检查的圆就不是将要飞的圆。
ORBIT_RADIUS_TOLERANCE_M = 1.0


class PathGeometryError(ValueError):
    """几何输入非法：确定性拒绝，不执行动作。"""

    def __init__(self, code, message="路径几何参数无效，未执行。"):
        super().__init__(message)
        self.code = code


class PathUnavailable(Exception):
    """算不出待校验折线：按 V1.4 降级，明确警告，不冒充路径安全。"""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class PathGeometry:
    """待校验折线；``points`` 是平台 DTO 形状，``segments`` 是相邻点对数量。"""

    points: tuple
    vertical_field: str
    entry_segment: bool = False
    closed: bool = False

    @property
    def segments(self) -> int:
        return len(self.points) - 1


def _coordinate(value, code):
    if not finite(value) or not -180.0 <= value <= 180.0:
        raise PathGeometryError(code)
    return float(value)


def _latitude(value, code):
    if not finite(value) or not -90.0 <= value <= 90.0:
        raise PathGeometryError(code)
    return float(value)


def resolve_vertical(state, order=_MOVE_FIELD_ORDER):
    """按动作的基准顺序取高度；两个字段都没有就是「算不出来」，不是 0 米。"""
    for field_name in order:
        value = state.get(field_name)
        if finite(value):
            return float(value), field_name
    raise PathUnavailable("PATH_VERTICAL_UNAVAILABLE")


def _vertical_within(field_name, value):
    low, high = VERTICAL_RANGES[field_name]
    floor = VERTICAL_FLOOR[field_name]
    if not finite(value) or not low <= value <= high:
        raise PathGeometryError("PATH_ALTITUDE_OUT_OF_RANGE")
    if floor is not None and value < floor:
        raise PathGeometryError("PATH_ALTITUDE_OUT_OF_RANGE")
    return float(value)


def _pose(state):
    longitude = state.get("longitude")
    latitude = state.get("latitude")
    if not finite(longitude) or not finite(latitude):
        raise PathUnavailable("PATH_POSE_UNAVAILABLE")
    if not -180.0 <= longitude <= 180.0 or not -90.0 <= latitude <= 90.0:
        raise PathGeometryError("INVALID_PATH_GEOMETRY")
    return float(longitude), float(latitude)


def _point(longitude, latitude, altitude, source):
    return {"longitude": longitude, "latitude": latitude, "altitude": altitude, "source": source}


def _distance(parameters):
    value = parameters.get("distance_m")
    if not finite(value) or not 0.0 < value <= MAX_DISTANCE_M:
        raise PathGeometryError("INVALID_PATH_GEOMETRY")
    return float(value)


def _move_vertical(state):
    """围栏折线的高度必须是飞机将要飞的那一个基准。

    大疆距离微操最终调用 fly_to_point，高度取遥测绝对高 ``altitude_m``。
    若改用相对高 ``height_m``，围栏会在另一条高度上给出通过。
    普宙按下发的相对高检查。没有机型时保持原顺序，避免把未知基准改名。
    """
    profile = state.get("profile")
    if profile == "DJI_HANGAR":
        value = state.get("altitude_m")
        if not finite(value):
            raise PathUnavailable("PATH_ALTITUDE_DATUM_MISMATCH")
        return float(value), "altitude_m"
    if profile == "PZ_HANGAR":
        value = state.get("height_m")
        if not finite(value):
            raise PathUnavailable("PATH_ALTITUDE_DATUM_MISMATCH")
        return float(value), "height_m"
    return resolve_vertical(state)


def micro_move_path(action, parameters, state):
    """六向微操折线：水平方向相对机头航向，升降只改所选高度基准。"""
    direction = _action_value(action).removeprefix("move_")
    distance = _distance(parameters)
    longitude, latitude = _pose(state)
    vertical, field_name = _move_vertical(state)
    start = _point(longitude, latitude, vertical, "old")
    if direction in {"up", "down"}:
        target_altitude = _vertical_within(
            field_name, vertical + distance * (1.0 if direction == "up" else -1.0))
        end = _point(longitude, latitude, target_altitude, "new")
    elif direction in HORIZONTAL_BEARINGS:
        yaw = state.get("yaw_deg")
        if not finite(yaw):
            # 没有机头航向就无法把「向前」落到地理坐标上，不能猜正北。
            raise PathUnavailable("PATH_HEADING_UNAVAILABLE")
        if abs(latitude) >= 89.0:
            raise PathGeometryError("INVALID_PATH_GEOMETRY")
        target_longitude, target_latitude = offset_target(
            longitude, latitude, float(yaw) + HORIZONTAL_BEARINGS[direction], distance)
        if abs(target_latitude) >= 89.0 or not -90.0 <= target_latitude <= 90.0 or not -180.0 <= target_longitude <= 180.0:
            raise PathGeometryError("INVALID_PATH_GEOMETRY")
        end = _point(target_longitude, target_latitude, vertical, "new")
    else:
        raise PathGeometryError("INVALID_PATH_GEOMETRY")
    return PathGeometry((start, end), field_name)


def point_flight_path(parameters, state):
    """指点飞行折线：目标点经纬度 + 与飞控 height 同基准的目标高度。"""
    longitude = _coordinate(parameters.get("longitude"), "INVALID_PATH_GEOMETRY")
    latitude = _latitude(parameters.get("latitude"), "INVALID_PATH_GEOMETRY")
    altitude = parameters.get("altitude_m")
    low, high = FLY_TARGET_ALTITUDE_RANGE
    if not finite(altitude) or not low <= altitude <= high:
        raise PathGeometryError("PATH_ALTITUDE_OUT_OF_RANGE")
    current_longitude, current_latitude = _pose(state)
    vertical, field_name = resolve_vertical(state, _FLY_FIELD_ORDER)
    if field_name != "altitude_m":
        # 目标高度是飞控 height 基准，当前位姿只有相对高度时无法拼成一条折线。
        raise PathUnavailable("PATH_ALTITUDE_DATUM_MISMATCH")
    start = _point(current_longitude, current_latitude, vertical, "old")
    end = _point(longitude, latitude, float(altitude), "new")
    return PathGeometry((start, end), field_name)



@dataclass(frozen=True)
class OrbitCommand:
    """环绕指令能否按请求半径执行。

    大疆 poi_mode_enter 不接收半径，实际半径是进入瞬间与目标的水平距离。
    aligned 为假时不能下发：否则围栏检查过的圆和飞机将要飞的圆不是同一个。
    """

    requested_m: float
    actual_m: float | None
    aligned: bool
    code: str
    message: str


def assess_orbit_command(parameters, state):
    """比较请求半径和当前水平距离；位姿缺失时拒绝，不能猜一个半径。"""
    center_longitude = _coordinate(parameters.get("longitude"), "INVALID_PATH_GEOMETRY")
    center_latitude = _latitude(parameters.get("latitude"), "INVALID_PATH_GEOMETRY")
    radius = parameters.get("radius_m")
    if not finite(radius) or not 0.0 < float(radius) <= MAX_RADIUS_M:
        raise PathGeometryError("INVALID_PATH_GEOMETRY")
    requested = float(radius)
    try:
        pose_longitude, pose_latitude = _pose(state)
    except PathUnavailable:
        return OrbitCommand(requested, None, False, "ORBIT_POSE_UNAVAILABLE",
                            "缺少当前经纬度，无法确认进入环绕时的实际半径，本次未下发。")
    actual = horizontal_distance_m(pose_longitude, pose_latitude, center_longitude, center_latitude)
    if abs(actual - requested) <= ORBIT_RADIUS_TOLERANCE_M:
        return OrbitCommand(
            requested, actual, True, "",
            "当前水平距离与请求半径一致。大疆进入环绕不接收半径参数，实际半径等于进入时的水平距离；"
            "围栏按该半径检查，设备是否沿该圆飞行尚未验证。")
    return OrbitCommand(
        requested, actual, False, "ORBIT_RADIUS_NOT_COMMANDED",
        f"当前距目标水平距离为{actual:.1f}米，请求半径为{requested:.1f}米。"
        "大疆进入环绕不接收半径，实际半径由进入时的水平距离决定，"
        "不能按已检查的请求半径飞行。请先指点飞到该半径后再环绕。本次未下发。")


def orbit_path(parameters, state):
    """环绕闭合折线：目标中心 + 半径 + 当前高度，36 段加闭合边，可选进入段。"""
    center_longitude = _coordinate(parameters.get("longitude"), "INVALID_PATH_GEOMETRY")
    center_latitude = _latitude(parameters.get("latitude"), "INVALID_PATH_GEOMETRY")
    radius = parameters.get("radius_m")
    if not finite(radius) or not 0.0 < radius <= MAX_RADIUS_M:
        raise PathGeometryError("INVALID_PATH_GEOMETRY")
    radius = float(radius)
    vertical, field_name = resolve_vertical(state, _FLY_FIELD_ORDER)
    vertical = _vertical_within(field_name, vertical)
    ring = []
    for index in range(ORBIT_SEGMENTS + 1):
        # 第 36 个点回到 0 度方位角，就是闭合边，不额外伪造终点。
        longitude, latitude = offset_target(center_longitude, center_latitude,
                                            index * (360.0 / ORBIT_SEGMENTS), radius)
        if not -90.0 <= latitude <= 90.0:
            raise PathGeometryError("INVALID_PATH_GEOMETRY")
        ring.append(_point(longitude, latitude, vertical, "new"))
    entry = None
    try:
        pose_longitude, pose_latitude = _pose(state)
    except (PathUnavailable, PathGeometryError):
        # 位姿缺失或不可信只影响「能不能额外校验起始段」，不否决用户请求本身。
        pose_longitude = pose_latitude = None
    on_commanded_circle = False
    if pose_longitude is not None:
        actual = horizontal_distance_m(
            pose_longitude, pose_latitude, center_longitude, center_latitude)
        on_commanded_circle = abs(actual - radius) <= ORBIT_RADIUS_TOLERANCE_M
    if (pose_longitude is not None and not on_commanded_circle
            and (pose_longitude, pose_latitude) != (ring[0]["longitude"], ring[0]["latitude"])):
        # 还不在请求半径上时，保留飞到正北起点的进入段，供几何检查使用。
        # 已经在请求半径上时不能再画这段：大疆从当前位置进入环绕，不会先飞到正北。
        entry = _point(pose_longitude, pose_latitude, vertical, "old")
    points = (entry, *ring) if entry is not None else tuple(ring)
    if len(points) - 1 > MAX_SEGMENTS:
        raise PathGeometryError("INVALID_PATH_GEOMETRY")
    return PathGeometry(points, field_name, entry_segment=entry is not None, closed=True)


def _action_value(action):
    """接受 str 或 FlightControlAction；枚举的 str() 不是动作名，不能直接拼。"""
    value = getattr(action, "value", action)
    return value if isinstance(value, str) else str(value)


def build_path(action, parameters, state):
    """按动作选择折线生成方式；非路径动作返回 None。"""
    value = _action_value(action)
    if value in {"move_forward", "move_backward", "move_left", "move_right",
                 "move_up", "move_down"}:
        return micro_move_path(value, parameters, state)
    if value == "point_flight":
        return point_flight_path(parameters, state)
    if value == "point_orbit":
        return orbit_path(parameters, state)
    return None
