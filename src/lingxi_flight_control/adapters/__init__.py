"""Adapters package for external platforms, annotation gateways, and virtual algorithm lifecycles."""
from .ktd_platform_adapter import KtdPlatformAdapter, RouteStatusUnavailableError
from .static_annotation_gateway import StaticAnnotationGateway, TargetGeometryError
from .virtual_algorithm_gateway import VirtualAlgorithmGateway

__all__ = [
    "KtdPlatformAdapter",
    "RouteStatusUnavailableError",
    "StaticAnnotationGateway",
    "TargetGeometryError",
    "VirtualAlgorithmGateway",
]
