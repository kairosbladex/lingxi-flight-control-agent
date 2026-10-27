"""按机头航向计算球面相对位移；不读取遥测或发送控制指令。"""

import math

# 与平台 GeoUtil.calcLatAndlon 使用的球半径一致。
EARTH_RADIUS_M = 6_371_393.0
#: 水平方向相对机头航向的方位角偏移：正前=航向，正右=航向+90（顺时针为正）。
HORIZONTAL_BEARINGS = {"forward": 0, "backward": 180, "left": -90, "right": 90}


def _check_inputs(longitude, latitude, bearing_deg, distance_m):
    values = (longitude, latitude, bearing_deg, distance_m)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
        raise ValueError("位置、方位和距离必须是有限数字")
    if not -180 <= longitude <= 180 or not -90 <= latitude <= 90 or distance_m <= 0:
        raise ValueError("位置或距离超出范围")


def offset_target(longitude: float, latitude: float, bearing_deg: float,
                  distance_m: float) -> tuple[float, float]:
    """从给定点按绝对方位角（正北为0，顺时针为正）推算球面目标点。

    环绕路径需要任意方位角，因此把球面算法收敛到这里，避免出现第二套近似。
    """
    _check_inputs(longitude, latitude, bearing_deg, distance_m)
    lon, lat = math.radians(longitude), math.radians(latitude)
    bearing = math.radians(bearing_deg % 360)
    arc = distance_m / EARTH_RADIUS_M
    lat2 = math.asin(max(-1.0, min(1.0, math.sin(lat) * math.cos(arc)
                                  + math.cos(lat) * math.sin(arc) * math.cos(bearing))))
    lon2 = lon + math.atan2(math.sin(bearing) * math.sin(arc) * math.cos(lat),
                            math.cos(arc) - math.sin(lat) * math.sin(lat2))
    return (math.degrees(lon2) + 180) % 360 - 180, math.degrees(lat2)


def horizontal_distance_m(longitude: float, latitude: float,
                          other_longitude: float, other_latitude: float) -> float:
    """两点水平距离（米）。与 offset_target 使用同一个球半径，避免两套近似。"""
    values = (longitude, latitude, other_longitude, other_latitude)
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
           for value in values):
        raise ValueError("位置必须是有限数字")
    if (not -180 <= longitude <= 180 or not -180 <= other_longitude <= 180
            or not -90 <= latitude <= 90 or not -90 <= other_latitude <= 90):
        raise ValueError("位置超出范围")
    lat1, lat2 = math.radians(latitude), math.radians(other_latitude)
    delta_lat = lat2 - lat1
    delta_lon = math.radians(other_longitude - longitude)
    chord = (math.sin(delta_lat / 2) ** 2
             + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2)
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(chord)))


def relative_target(longitude: float, latitude: float, heading_deg: float,
                    direction: str, distance_m: float) -> tuple[float, float]:
    """方向相对机头航向：forward/backward/left/right 分别对应 0/180/-90/+90 度。"""
    _check_inputs(longitude, latitude, heading_deg, distance_m)
    if direction not in HORIZONTAL_BEARINGS:
        raise ValueError("不支持的水平移动方向")
    return offset_target(longitude, latitude, heading_deg + HORIZONTAL_BEARINGS[direction], distance_m)
