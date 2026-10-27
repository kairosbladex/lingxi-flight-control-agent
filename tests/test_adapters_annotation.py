"""测试静态标注库网关与虚拟 AI 算法生命周期网关。"""

from __future__ import annotations

import unittest

from lingxi_flight_control.adapters.static_annotation_gateway import (
    GeometryType,
    StaticAnnotation,
    StaticAnnotationGateway,
    TargetGeometryError,
    MULTI_LINE_UNSUPPORTED,
    MULTI_POLYGON_UNSUPPORTED,
    BODY_ANNOTATION_AI_UNSUPPORTED,
    MIXED_TARGET_UNSUPPORTED,
)
from lingxi_flight_control.adapters.virtual_algorithm_gateway import VirtualAlgorithmGateway


class StaticAnnotationGatewayTests(unittest.TestCase):
    def setUp(self):
        self.gateway = StaticAnnotationGateway()

    def test_point_targets_strictly_preserve_user_specified_order(self):
        p1 = StaticAnnotation("p1", "Point 1", GeometryType.POINT, [113.1, 23.1], "v1", order=3)
        p2 = StaticAnnotation("p2", "Point 2", GeometryType.POINT, [113.2, 23.2], "v1", order=1)
        p3 = StaticAnnotation("p3", "Point 3", GeometryType.POINT, [113.3, 23.3], "v1", order=2)
        resolved = self.gateway.validate_and_resolve_targets([p1, p2, p3])
        self.assertEqual([p.target_id for p in resolved], ["p2", "p3", "p1"])

    def test_multi_line_targets_rejected(self):
        l1 = StaticAnnotation("l1", "Line 1", GeometryType.LINE, [[113.1, 23.1], [113.2, 23.2]], "v1")
        l2 = StaticAnnotation("l2", "Line 2", GeometryType.LINE, [[113.3, 23.3], [113.4, 23.4]], "v1")
        with self.assertRaises(TargetGeometryError) as ctx:
            self.gateway.validate_and_resolve_targets([l1, l2])
        self.assertEqual(ctx.exception.code, MULTI_LINE_UNSUPPORTED)

    def test_multi_polygon_targets_rejected(self):
        poly1 = StaticAnnotation("poly1", "Poly 1", GeometryType.POLYGON, [[[113.1, 23.1], [113.2, 23.2]]], "v1")
        poly2 = StaticAnnotation("poly2", "Poly 2", GeometryType.POLYGON, [[[113.3, 23.3], [113.4, 23.4]]], "v1")
        with self.assertRaises(TargetGeometryError) as ctx:
            self.gateway.validate_and_resolve_targets([poly1, poly2])
        self.assertEqual(ctx.exception.code, MULTI_POLYGON_UNSUPPORTED)

    def test_volume_target_rejected(self):
        v = StaticAnnotation("v1", "Volume 1", GeometryType.VOLUME, [[[113.1, 23.1, 10], [113.2, 23.2, 50]]], "v1")
        with self.assertRaises(TargetGeometryError) as ctx:
            self.gateway.validate_and_resolve_targets([v])
        self.assertEqual(ctx.exception.code, BODY_ANNOTATION_AI_UNSUPPORTED)

    def test_mixed_targets_rejected(self):
        p = StaticAnnotation("p1", "Point 1", GeometryType.POINT, [113.1, 23.1], "v1")
        l = StaticAnnotation("l1", "Line 1", GeometryType.LINE, [[113.1, 23.1], [113.2, 23.2]], "v1")
        with self.assertRaises(TargetGeometryError) as ctx:
            self.gateway.validate_and_resolve_targets([p, l])
        self.assertEqual(ctx.exception.code, MIXED_TARGET_UNSUPPORTED)


class VirtualAlgorithmGatewayTests(unittest.TestCase):
    def test_algorithm_lifecycle_departure_keeps_global_and_shared(self):
        # 初始全局配置算法: "车辆检测"
        gw = VirtualAlgorithmGateway(global_algorithms=["车辆检测"])
        self.assertEqual(gw.active_algorithms, frozenset({"车辆检测"}))

        # 到达标注 A: 绑定 "烟火检测" 与 "水域检测"
        newly_a = gw.on_arrive_annotation("anno-A", ["烟火检测", "水域检测"])
        self.assertEqual(newly_a, {"烟火检测", "水域检测"})
        self.assertEqual(gw.active_algorithms, frozenset({"车辆检测", "烟火检测", "水域检测"}))

        # 到达标注 B: 绑定 "水域检测" 与 "人员检测"
        newly_b = gw.on_arrive_annotation("anno-B", ["水域检测", "人员检测"])
        self.assertEqual(newly_b, {"人员检测"})
        self.assertEqual(gw.active_algorithms, frozenset({"车辆检测", "烟火检测", "水域检测", "人员检测"}))

        # 离开标注 A: "烟火检测"被关闭；"水域检测"因被标注 B 共享而保留；"车辆检测"因全局保留
        stopped_a = gw.on_depart_annotation("anno-A")
        self.assertEqual(stopped_a, {"烟火检测"})
        self.assertEqual(gw.active_algorithms, frozenset({"车辆检测", "水域检测", "人员检测"}))

        # 离开标注 B: "水域检测" 与 "人员检测" 均被关闭
        stopped_b = gw.on_depart_annotation("anno-B")
        self.assertEqual(stopped_b, {"水域检测", "人员检测"})
        self.assertEqual(gw.active_algorithms, frozenset({"车辆检测"}))

    def test_telemetry_metadata_format(self):
        gw = VirtualAlgorithmGateway(global_algorithms=["车辆检测"])
        meta = gw.build_telemetry_metadata()
        self.assertEqual(meta["active_algorithms"], ["车辆检测"])
        self.assertEqual(meta["algorithm_count"], 1)
        self.assertTrue(meta["video_detect_enabled"])


if __name__ == "__main__":
    unittest.main()
