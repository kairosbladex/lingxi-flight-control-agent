"""地图静态标注库数据读取网关 (StaticAnnotationGateway)。

依据 V1.4 2.1 与 3.2.2 规范：
废弃旧版后台巡检区域库，所有巡检目的地统一使用地图静态标注库。
严格落实几何校验门禁：
- 点目标：多点按用户指定的 order 严格保留顺序，绝不静默按地理距离重排；
- 线目标：仅支持单线，强制端点起飞，多线输入立即返回 MULTI_LINE_UNSUPPORTED；
- 面目标：仅支持单面全覆盖，多面输入立即返回 MULTI_POLYGON_UNSUPPORTED；
- 体目标：立体目标严格拦截，返回 BODY_ANNOTATION_AI_UNSUPPORTED 或 UNSUPPORTED_TARGET_GEOMETRY；
- 混合目标：点+线、线+面等混合输入严格拦截，返回 MIXED_TARGET_UNSUPPORTED。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

MULTI_LINE_UNSUPPORTED = "MULTI_LINE_UNSUPPORTED"
MULTI_POLYGON_UNSUPPORTED = "MULTI_POLYGON_UNSUPPORTED"
BODY_ANNOTATION_AI_UNSUPPORTED = "BODY_ANNOTATION_AI_UNSUPPORTED"
UNSUPPORTED_TARGET_GEOMETRY = "UNSUPPORTED_TARGET_GEOMETRY"
MIXED_TARGET_UNSUPPORTED = "MIXED_TARGET_UNSUPPORTED"
EMPTY_TARGET_UNSUPPORTED = "EMPTY_TARGET_UNSUPPORTED"


class GeometryType(str, Enum):
    POINT = "point"
    LINE = "line"
    POLYGON = "polygon"
    VOLUME = "volume"


class TargetGeometryError(ValueError):
    """几何校验门禁异常。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self)}


@dataclass(frozen=True)
class StaticAnnotation:
    """静态标注对象。"""
    target_id: str
    name: str
    geometry_type: GeometryType
    coordinates: Sequence[Any]
    version: str
    order: int = 0
    configured_algorithms: Sequence[str] = field(default_factory=tuple)


class StaticAnnotationGateway:
    """静态标注库网关，负责读取与几何安全门禁校验。"""

    def __init__(self, annotations: Sequence[StaticAnnotation] | None = None):
        self._annotations = list(annotations or [])

    def load_annotations(self, raw_items: Sequence[Mapping[str, Any]]) -> list[StaticAnnotation]:
        """从外部原始数据加载静态标注对象。"""
        loaded = []
        for item in raw_items:
            geom = GeometryType(item["geometry_type"].lower())
            loaded.append(StaticAnnotation(
                target_id=str(item["target_id"]),
                name=str(item.get("name", "")),
                geometry_type=geom,
                coordinates=list(item.get("coordinates", [])),
                version=str(item.get("version", "v1")),
                order=int(item.get("order", 0)),
                configured_algorithms=tuple(item.get("configured_algorithms", [])),
            ))
        self._annotations = loaded
        return loaded

    def validate_and_resolve_targets(self, targets: Sequence[StaticAnnotation]) -> list[StaticAnnotation]:
        """对输入的标注列表执行 V1.4 严格几何门禁校验。"""
        if not targets:
            raise TargetGeometryError(EMPTY_TARGET_UNSUPPORTED, "目标标注列表不能为空")

        geom_types = {t.geometry_type for t in targets}

        # 1. 体标注拦截
        if GeometryType.VOLUME in geom_types:
            raise TargetGeometryError(BODY_ANNOTATION_AI_UNSUPPORTED, "立体目标不支持自动规划与AI算法绑定")

        # 2. 混合目标拦截
        if len(geom_types) > 1:
            raise TargetGeometryError(MIXED_TARGET_UNSUPPORTED, "暂不支持同时巡检点、线、面混合目标")

        single_geom = next(iter(geom_types))

        # 3. 多线目标拦截
        if single_geom == GeometryType.LINE:
            if len(targets) > 1:
                raise TargetGeometryError(MULTI_LINE_UNSUPPORTED, "仅支持单条巡检线路，不支持多线并发输入")
            return list(targets)

        # 4. 多面目标拦截
        if single_geom == GeometryType.POLYGON:
            if len(targets) > 1:
                raise TargetGeometryError(MULTI_POLYGON_UNSUPPORTED, "仅支持单面全覆盖，不支持多面并发输入")
            return list(targets)

        # 5. 点目标严格按用户指定的 order 保留顺序，绝不静默按地理距离重排
        if single_geom == GeometryType.POINT:
            sorted_points = sorted(targets, key=lambda t: t.order)
            return sorted_points

        raise TargetGeometryError(UNSUPPORTED_TARGET_GEOMETRY, f"不支持的几何类型: {single_geom}")
