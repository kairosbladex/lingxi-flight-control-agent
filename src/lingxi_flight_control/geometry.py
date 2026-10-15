"""按机头航向计算球面相对位移；不读取遥测或发送控制指令。"""

import math

# 与平台 GeoUtil.calcLatAndlon 使用的球半径一致。
EARTH_RADIUS_M = 6_371_393.0
HORIZONTAL_BEARINGS = {"forward": 0, "backward": 180, "left": -90, "right": 90}


def relative_target(longitude: float, latitude: float, heading_deg: float,
                    direction: str, distance_m: float) -> tuple[float, float]:
    values = (longitude, latitude, heading_deg, distance_m)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
        raise ValueError("位置、航向和距离必须是有限数字")
    if not -180 <= longitude <= 180 or not -90 <= latitude <= 90 or distance_m <= 0:
        raise ValueError("位置或距离超出范围")
    if direction not in HORIZONTAL_BEARINGS:
        raise ValueError("不支持的水平移动方向")
    lon, lat = math.radians(longitude), math.radians(latitude)
    bearing = math.radians((heading_deg + HORIZONTAL_BEARINGS[direction]) % 360)
    arc = distance_m / EARTH_RADIUS_M
    lat2 = math.asin(max(-1.0, min(1.0, math.sin(lat) * math.cos(arc)
                                  + math.cos(lat) * math.sin(arc) * math.cos(bearing))))
    lon2 = lon + math.atan2(math.sin(bearing) * math.sin(arc) * math.cos(lat),
                            math.cos(arc) - math.sin(lat) * math.sin(lat2))
    return (math.degrees(lon2) + 180) % 360 - 180, math.degrees(lat2)
