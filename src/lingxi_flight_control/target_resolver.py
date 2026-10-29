"""V1.4 静态标注目标解析的 typed 适配器边界。

本仓**不实现**地图静态标注库，只定义“飞控 Agent 需要从平台拿到什么”：

- 查询身份：``project_id`` / ``actor_id`` / 资源绑定 / 原始目标名或目标 ID；
- 返回：``target_id``、名称、几何类型、经纬度、高度、版本、来源；
- 同名目标返回候选列表，绝不自动挑第一个；
- 缺目标、体标注、混合类型、缺高度、解析服务不可用都返回稳定错误码。

``parse_command`` 只负责抽取意图和原始目标文本，本模块负责把“原始目标”
换成可执行坐标；解析结果必须记录 ``source``、目标 ``version`` 与 ``binding``，
以便确认卡失效与审计。

线/面只属于地面规划链路（由任务下发 Agent 负责），飞行中的指点/环绕只接受
单点目标；多目标请求不在这里偷偷拆成多个真实动作。
"""
from __future__ import annotations

import math
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

CONTRACT_VERSION = "lingxi-target-resolver/v1"

TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
TARGET_AMBIGUOUS = "TARGET_AMBIGUOUS"
TARGET_RESOLVER_UNAVAILABLE = "TARGET_RESOLVER_UNAVAILABLE"
UNSUPPORTED_TARGET_GEOMETRY = "UNSUPPORTED_TARGET_GEOMETRY"
MIXED_TARGET_UNSUPPORTED = "MIXED_TARGET_UNSUPPORTED"
TARGET_ALTITUDE_REQUIRED = "TARGET_ALTITUDE_REQUIRED"
TARGET_VERSION_UNAVAILABLE = "TARGET_VERSION_UNAVAILABLE"
TARGET_BINDING_MISMATCH = "TARGET_BINDING_MISMATCH"
INVALID_TARGET_QUERY = "INVALID_TARGET_QUERY"
MULTI_TARGET_UNSUPPORTED = "MULTI_TARGET_UNSUPPORTED"


class TargetGeometry(str, Enum):
    POINT = "point"
    LINE = "line"
    POLYGON = "polygon"
    VOLUME = "volume"
    MIXED = "mixed"


#: 飞行中控制阶段只接受单点目标（V1.4 能力矩阵：仅指点飞行/环绕，单目标）。
FLIGHT_INTENTS = frozenset({"point_flight", "point_orbit"})
GROUND_INTENTS = frozenset({"ground_plan"})
INTENTS = FLIGHT_INTENTS | GROUND_INTENTS


class TargetResolverError(ValueError):
    """稳定错误码 + 可选候选；不携带地图服务原文。"""

    def __init__(self, code: str, *, candidates: Sequence["TargetCandidate"] = ()):
        super().__init__(code)
        self.code = code
        self.candidates = tuple(candidates)


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 128 and value.strip() == value


@dataclass(frozen=True)
class TargetQuery:
    project_id: str
    actor_id: str
    binding: Mapping[str, str]
    intent: str
    name: str | None = None
    target_id: str | None = None

    def validate(self) -> None:
        if not _identifier(self.project_id) or not _identifier(self.actor_id):
            raise TargetResolverError(INVALID_TARGET_QUERY)
        if self.intent not in INTENTS:
            raise TargetResolverError(INVALID_TARGET_QUERY)
        if not isinstance(self.binding, Mapping) or not self.binding:
            raise TargetResolverError(INVALID_TARGET_QUERY)
        if any(not _identifier(key) or not _identifier(value) for key, value in self.binding.items()):
            raise TargetResolverError(INVALID_TARGET_QUERY)
        # 名称与 ID 二选一：同时给会掩盖“用户说的到底是哪个”。
        if (self.name is None) == (self.target_id is None):
            raise TargetResolverError(INVALID_TARGET_QUERY)
        if self.name is not None and (not isinstance(self.name, str) or not 0 < len(self.name) <= 128):
            raise TargetResolverError(INVALID_TARGET_QUERY)
        if self.target_id is not None and not _identifier(self.target_id):
            raise TargetResolverError(INVALID_TARGET_QUERY)


@dataclass(frozen=True)
class TargetCandidate:
    target_id: str
    name: str
    geometry_type: TargetGeometry
    longitude: float | None
    latitude: float | None
    altitude_m: float | None
    version: str | None
    source: str
    project_id: str

    def validate(self) -> None:
        if not _identifier(self.target_id) or not isinstance(self.name, str) or not self.name:
            raise TargetResolverError(INVALID_TARGET_QUERY)
        if not isinstance(self.geometry_type, TargetGeometry):
            raise TargetResolverError(UNSUPPORTED_TARGET_GEOMETRY)
        if not _identifier(self.source) or not _identifier(self.project_id):
            raise TargetResolverError(INVALID_TARGET_QUERY)

    def coordinates(self) -> tuple[float, float]:
        lon, lat = self.longitude, self.latitude
        if (type(lon) not in (int, float) or type(lat) not in (int, float)
                or not math.isfinite(lon) or not math.isfinite(lat)
                or not -180 <= lon <= 180 or not -90 <= lat <= 90):
            raise TargetResolverError(UNSUPPORTED_TARGET_GEOMETRY)
        return float(lon), float(lat)


@dataclass(frozen=True)
class ResolvedTarget:
    """解析结果：坐标、来源、版本与绑定必须一起留存。"""

    target_id: str
    name: str
    geometry_type: TargetGeometry
    longitude: float
    latitude: float
    altitude_m: float
    version: str
    source: str
    project_id: str
    actor_id: str
    intent: str
    binding: Mapping[str, str] = field(default_factory=dict)
    contract_version: str = CONTRACT_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "name": self.name,
            "geometry_type": self.geometry_type.value,
            "longitude": self.longitude,
            "latitude": self.latitude,
            "altitude_m": self.altitude_m,
            "version": self.version,
            "source": self.source,
            "project_id": self.project_id,
            "actor_id": self.actor_id,
            "intent": self.intent,
            "binding": dict(self.binding),
            "contract_version": self.contract_version,
        }


@runtime_checkable
class TargetResolver(Protocol):
    """可注入的地图静态标注适配器；本仓不提供默认实现。"""

    def resolve(self, query: TargetQuery) -> Sequence[TargetCandidate]:
        ...


class FixtureTargetResolver:
    """只读合成标注解析器；用于本地离线验证，不连接真实地图服务。"""

    def __init__(self, candidates: Sequence[TargetCandidate]):
        self._candidates = tuple(candidates)
        self.project_id = (self._candidates[0].project_id if self._candidates else None)

    @classmethod
    def from_json(cls, path: str | Path) -> "FixtureTargetResolver":
        """从冻结 JSON 夹具加载候选；非法夹具直接失败，不猜测字段。"""
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        raw_candidates = payload.get("candidates") if isinstance(payload, Mapping) else None
        if not isinstance(raw_candidates, Sequence) or isinstance(raw_candidates, (str, bytes)):
            raise ValueError("target fixture candidates are required")
        candidates = []
        for item in raw_candidates:
            if not isinstance(item, Mapping):
                raise ValueError("target fixture candidate must be an object")
            candidates.append(TargetCandidate(
                target_id=item.get("target_id"), name=item.get("name"),
                geometry_type=_geometry(item.get("geometry_type")),
                longitude=item.get("longitude"), latitude=item.get("latitude"),
                altitude_m=item.get("altitude_m"), version=item.get("version"),
                source=item.get("source"), project_id=item.get("project_id")))
        return cls(candidates)

    def resolve(self, query: TargetQuery) -> Sequence[TargetCandidate]:
        """按名称或 ID 精确过滤候选；不做模糊匹配或相似度排序。"""
        query.validate()
        return tuple(candidate for candidate in self._candidates
                     if candidate.project_id == query.project_id
                     and (query.target_id is None or candidate.target_id == query.target_id)
                     and (query.name is None or candidate.name == query.name))


def _geometry(value: Any) -> TargetGeometry:
    if isinstance(value, TargetGeometry):
        return value
    try:
        return TargetGeometry(str(value))
    except (TypeError, ValueError):
        raise TargetResolverError(UNSUPPORTED_TARGET_GEOMETRY) from None


def select_candidate(candidates: Sequence[TargetCandidate], query: TargetQuery) -> TargetCandidate:
    """从候选中确定唯一目标；任何不确定都抛错，不猜。"""
    query.validate()
    if not candidates:
        raise TargetResolverError(TARGET_NOT_FOUND)
    validated = []
    for candidate in candidates:
        if not isinstance(candidate, TargetCandidate):
            raise TargetResolverError(INVALID_TARGET_QUERY)
        candidate.validate()
        validated.append(candidate)
    if any(candidate.geometry_type is TargetGeometry.MIXED for candidate in validated):
        raise TargetResolverError(MIXED_TARGET_UNSUPPORTED, candidates=validated)
    if any(candidate.project_id != query.project_id for candidate in validated):
        raise TargetResolverError(TARGET_BINDING_MISMATCH, candidates=validated)

    selected = validated
    if query.target_id is not None:
        selected = [c for c in validated if c.target_id == query.target_id]
        if not selected:
            raise TargetResolverError(TARGET_NOT_FOUND)
    if len(selected) > 1:
        # 同名目标：返回候选，由用户指定；不按距离/名称相似度自动挑。
        raise TargetResolverError(TARGET_AMBIGUOUS, candidates=selected)

    candidate = selected[0]
    if candidate.geometry_type is TargetGeometry.VOLUME:
        raise TargetResolverError(UNSUPPORTED_TARGET_GEOMETRY, candidates=selected)
    if query.intent in FLIGHT_INTENTS and candidate.geometry_type is not TargetGeometry.POINT:
        # 线/面属于地面规划；飞行中只接受单点目标。
        raise TargetResolverError(UNSUPPORTED_TARGET_GEOMETRY, candidates=selected)
    if not _identifier(candidate.version):
        raise TargetResolverError(TARGET_VERSION_UNAVAILABLE, candidates=selected)
    if candidate.geometry_type is TargetGeometry.POINT:
        altitude = candidate.altitude_m
        if type(altitude) not in (int, float) or not math.isfinite(altitude) or altitude <= 0:
            # 不使用默认高度，也不借用航线高度。
            raise TargetResolverError(TARGET_ALTITUDE_REQUIRED, candidates=selected)
    candidate.coordinates()
    return candidate


def resolve_target(resolver: Any, query: TargetQuery) -> ResolvedTarget:
    """调用注入适配器并固化结果；适配器缺失或异常都按“不可用”阻断。"""
    query.validate()
    if resolver is None or not callable(getattr(resolver, "resolve", None)):
        raise TargetResolverError(TARGET_RESOLVER_UNAVAILABLE)
    try:
        candidates = resolver.resolve(query)
    except TargetResolverError:
        raise
    except Exception:
        raise TargetResolverError(TARGET_RESOLVER_UNAVAILABLE) from None
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        raise TargetResolverError(TARGET_RESOLVER_UNAVAILABLE)
    candidate = select_candidate(candidates, query)
    longitude, latitude = candidate.coordinates()
    return ResolvedTarget(
        target_id=candidate.target_id, name=candidate.name,
        geometry_type=candidate.geometry_type, longitude=longitude, latitude=latitude,
        altitude_m=float(candidate.altitude_m), version=candidate.version,
        source=candidate.source, project_id=candidate.project_id, actor_id=query.actor_id,
        intent=query.intent, binding=MappingProxyType(dict(query.binding)),
    )


def query_from_command(call: Mapping[str, Any], *, project_id: str, actor_id: str,
                       binding: Mapping[str, str], intent: str) -> TargetQuery:
    """把 ``parse_command`` 的输出转成查询；只搬运原始名称，不补坐标。

    坐标指令已经给出经纬度，不需要（也不允许）走静态标注解析；
    把它当目标查询会让“坐标目标”这类占位名被误当成真实标注。
    """
    if not isinstance(call, Mapping):
        raise TargetResolverError(INVALID_TARGET_QUERY)
    if "longitude" in call or "latitude" in call:
        raise TargetResolverError(INVALID_TARGET_QUERY)
    name = call.get("target")
    target_id = call.get("targetId")
    if name is not None and (not isinstance(name, str) or not name.strip()):
        raise TargetResolverError(INVALID_TARGET_QUERY)
    return TargetQuery(project_id=project_id, actor_id=actor_id, binding=dict(binding),
                       intent=intent, name=name if isinstance(name, str) else None,
                       target_id=target_id if isinstance(target_id, str) else None)


def require_single_target(targets: Sequence[Any], *, intent: str) -> None:
    """多目标请求必须显式拒绝，不能在本仓拆成多个真实动作。"""
    if not isinstance(targets, Sequence) or isinstance(targets, (str, bytes)):
        raise TargetResolverError(INVALID_TARGET_QUERY)
    if len(targets) > 1:
        raise TargetResolverError(MULTI_TARGET_UNSUPPORTED)
    if len(targets) == 0:
        raise TargetResolverError(TARGET_NOT_FOUND)
    if intent not in INTENTS:
        raise TargetResolverError(INVALID_TARGET_QUERY)
