"""V1.4 3.2.1 / 3.2.2 航线映射与几何拦截规则库的离线单元测试。

对应设计规范（``灵犀平台自然语言智能控制功能设计规范V1.4.docx``）：

* 3.2.1 表1 测绘与巡检任务触发词映射
* 3.2.2 表2 目的地静态标注与航线强制映射
* 第五章「异常判断与反馈话术明细（测试用例库）」表6 相关拒绝话术原文
* 1.1 能力矩阵飞行中单目标边界

全部是纯函数断言：不联网、不调平台、不做几何求解。规范未定义处的假设
（距离/体话术版本/冲突处理/多点顺序归属/飞行中话术缺失）在模块 docstring 与
本文件对应用例里显式标注。
"""
import unittest
from collections import defaultdict

from lingxi_flight_control import flight_policy as policy
from lingxi_flight_control import route_mapping as mapping
from lingxi_flight_control.route_mapping import (
    ANNOTATION_GEOMETRIES,
    FLIGHT_RULES_AREA,
    FLIGHT_RULES_LINE,
    FLIGHT_RULES_ORBIT,
    FLIGHT_RULES_POINT,
    FLIGHT_RULES_SURVEY,
    GEOMETRIES,
    GEOMETRY_AREA,
    GEOMETRY_BODY_ROUTE,
    GEOMETRY_GRID,
    GEOMETRY_LINE,
    GEOMETRY_MIXED,
    GEOMETRY_MULTI_AREA,
    GEOMETRY_MULTI_LINE,
    GEOMETRY_POINT,
    GEOMETRY_VOLUME,
    IN_FLIGHT_MAX_TARGETS,
    IN_FLIGHT_ROUTE_TYPES,
    MESSAGE_REJECT_GEOMETRY_ROUTE,
    MESSAGE_REJECT_GRID,
    MESSAGE_REJECT_MIXED,
    MESSAGE_REJECT_MULTI_AREA,
    MESSAGE_REJECT_MULTI_LINE,
    MESSAGE_REJECT_VOLUME,
    MESSAGE_REJECT_VOLUME_TABLE_2,
    PATROL_TRIGGER_ROUTES,
    REJECTION_GEOMETRY_ROUTE,
    REJECTION_GRID_ROUTE,
    REJECTION_IN_FLIGHT_TARGET,
    REJECTION_MIXED,
    REJECTION_MULTI_AREA,
    REJECTION_MULTI_LINE,
    REJECTION_ONLY_GEOMETRIES,
    REJECTION_VOLUME,
    ROUTE_AREA,
    ROUTE_OBLIQUE,
    ROUTE_ORBIT,
    ROUTE_ORTHO,
    ROUTE_PANORAMA,
    ROUTE_TYPES,
    ROUTE_WAYPOINT,
    SURVEY_ROUTES,
    SURVEY_TRIGGER_ROUTES,
    TRIGGER_CATEGORY_PATROL,
    TRIGGER_CATEGORY_SURVEY,
    RouteDecision,
    decide_in_flight_route,
    decide_route,
    in_flight_target_count_supported,
    map_geometry_to_route,
    map_trigger_to_route,
)


#: 3.2.1 表1 的完整触发词表：与实现做**双向**相等断言，防止漏词或偷加非规范词。
EXPECTED_SURVEY_TRIGGER_ROUTES = (
    ("正射影像", ROUTE_ORTHO),
    ("二维地图", ROUTE_ORTHO),
    ("三维建模", ROUTE_OBLIQUE),
    ("倾斜摄影", ROUTE_OBLIQUE),
    ("全景图", ROUTE_PANORAMA),
    ("360°全景", ROUTE_PANORAMA),
)
EXPECTED_PATROL_TRIGGER_ROUTES = (
    ("沿河", ROUTE_WAYPOINT),
    ("沿路", ROUTE_WAYPOINT),
    ("沿线", ROUTE_WAYPOINT),
    ("全域", ROUTE_AREA),
    ("全面", ROUTE_AREA),
    ("普通面状", ROUTE_AREA),
    ("普查", ROUTE_AREA),
    ("快速扫描", ROUTE_AREA),
    ("挨个检查", ROUTE_WAYPOINT),
    ("绕一圈", ROUTE_ORBIT),
)


class SpecWordingTests(unittest.TestCase):
    """逐字核对规范原文常量，防止后续被无意改写。"""

    def test_route_type_constants_are_verbatim(self):
        self.assertEqual(ROUTE_ORTHO, "正射航线")
        self.assertEqual(ROUTE_OBLIQUE, "倾斜摄影航线")
        self.assertEqual(ROUTE_PANORAMA, "全景航线")
        self.assertEqual(ROUTE_WAYPOINT, "航点航线")
        self.assertEqual(ROUTE_AREA, "面状航线")
        self.assertEqual(ROUTE_ORBIT, "环绕航线")
        self.assertEqual(
            ROUTE_TYPES,
            {"正射航线", "倾斜摄影航线", "全景航线", "航点航线", "面状航线", "环绕航线"},
        )

    def test_geometry_constants_are_verbatim_and_partitioned(self):
        self.assertEqual(
            (GEOMETRY_POINT, GEOMETRY_LINE, GEOMETRY_AREA, GEOMETRY_VOLUME,
             GEOMETRY_MIXED, GEOMETRY_MULTI_LINE, GEOMETRY_MULTI_AREA,
             GEOMETRY_GRID, GEOMETRY_BODY_ROUTE),
            ("点", "线", "面", "体", "混合类型", "多线", "多面", "网格航线", "几何体航线"),
        )
        # 表2 第一列的标注几何与表1 注记/数量产生的「仅拒绝」标记必须显式区分。
        self.assertEqual(
            ANNOTATION_GEOMETRIES,
            {GEOMETRY_POINT, GEOMETRY_LINE, GEOMETRY_AREA, GEOMETRY_VOLUME, GEOMETRY_MIXED},
        )
        self.assertEqual(
            REJECTION_ONLY_GEOMETRIES,
            {GEOMETRY_MULTI_LINE, GEOMETRY_MULTI_AREA, GEOMETRY_GRID, GEOMETRY_BODY_ROUTE},
        )
        self.assertEqual(ANNOTATION_GEOMETRIES & REJECTION_ONLY_GEOMETRIES, frozenset())
        self.assertEqual(GEOMETRIES, ANNOTATION_GEOMETRIES | REJECTION_ONLY_GEOMETRIES)

    def test_rejection_messages_are_verbatim(self):
        self.assertEqual(
            MESSAGE_REJECT_GRID,
            "当前版本暂不支持自动规划复杂路径或网格航线，请手动接管。",
        )
        self.assertEqual(MESSAGE_REJECT_MIXED, "单次任务暂不支持混合目标巡检，请分次下发。")
        self.assertEqual(
            MESSAGE_REJECT_VOLUME,
            "当前版本不支持立体目标规划，请将其标注为点或面。",
        )
        # 3.2.2 表2 体行的短版同样逐字保留，供按章节取用。
        self.assertEqual(
            MESSAGE_REJECT_VOLUME_TABLE_2,
            "当前版本不支持立体目标，请标注为点或面。",
        )
        self.assertEqual(MESSAGE_REJECT_MULTI_LINE, "暂不支持同时巡检多条线")
        self.assertEqual(MESSAGE_REJECT_MULTI_AREA, "暂不支持同时巡检多个面")
        self.assertEqual(
            MESSAGE_REJECT_GEOMETRY_ROUTE,
            "当前版本暂不支持自动规划复杂路径或网格航线，请手动接管。",
        )

    def test_rejection_messages_alias_flight_policy_single_source(self):
        # M2 集成评审：话术常量不得在 route_mapping 与 flight_policy 各写一份；
        # 旧公开名必须是 flight_policy 常量的同一个对象（改 flight_policy 即全局生效）。
        self.assertIs(mapping.MESSAGE_REJECT_GRID, policy.COMPLEX_ROUTE_MANUAL_MESSAGE)
        self.assertIs(mapping.MESSAGE_REJECT_GEOMETRY_ROUTE, policy.COMPLEX_ROUTE_MANUAL_MESSAGE)
        self.assertIs(mapping.MESSAGE_REJECT_MIXED, policy.MIXED_TARGET_MESSAGE)
        self.assertIs(mapping.MESSAGE_REJECT_VOLUME, policy.BODY_TARGET_MESSAGE)
        self.assertIs(mapping.MESSAGE_REJECT_VOLUME_TABLE_2, policy.BODY_TARGET_STANDARD_MESSAGE)

    def test_module_docstring_declares_reference_only_and_scoped_integration(self):
        # 集成评审：归属/接线口径必须写实，不能把预检当成地面规划真源。
        doc = mapping.__doc__ or ""
        for fragment in ("纯规则库", "参考实现", "接入 cockpit", "不替代地面规划", "尚未裁决",
                         "target_resolver", "handoff_contract"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, doc)

    def test_flight_rule_fragments_are_verbatim_cell_slices(self):
        # 这些是表1/表2 单元格的逐字切片（保留原标点），不是重新改写的句子。
        self.assertEqual(
            FLIGHT_RULES_SURVEY,
            ("强制匹配对应航线。", "开启等距/等时拍照，全局拍照模式优先。"),
        )
        self.assertEqual(
            FLIGHT_RULES_POINT,
            (
                "多点串联：严格按用户顺序飞。",
                "到点默认悬停60秒，云台垂直向下；",
                "若指明环绕，则顺时针环绕1圈，云台朝向目标。",
            ),
        )
        self.assertEqual(
            FLIGHT_RULES_ORBIT,
            ("若指明环绕，则顺时针环绕1圈，云台朝向目标。",),
        )
        self.assertEqual(
            FLIGHT_RULES_LINE,
            ("航点航线（强制端点）", "单线单任务：就近选择线的一端开始，飞向另一端，不折返。"),
        )
        self.assertEqual(
            FLIGHT_RULES_AREA,
            ("面状航线（正射全覆盖）", "单面单任务：自动生成“弓”字形航线，满铺扫描。"),
        )


class Table1TriggerMappingTests(unittest.TestCase):
    """3.2.1 表1：触发词 → 航线类型。"""

    def test_trigger_tables_match_spec_exactly(self):
        # 双向相等：不允许漏掉表1 触发词，也不允许偷偷加入非规范触发词。
        self.assertEqual(SURVEY_TRIGGER_ROUTES, EXPECTED_SURVEY_TRIGGER_ROUTES)
        self.assertEqual(PATROL_TRIGGER_ROUTES, EXPECTED_PATROL_TRIGGER_ROUTES)

    def test_every_table_1_row_has_at_least_two_triggers_from_implementation(self):
        # 依赖实现里的触发词表分组，而不是测试文件另抄一份；清空实现表会失败。
        survey_by_route = defaultdict(list)
        for word, route in SURVEY_TRIGGER_ROUTES:
            survey_by_route[route].append(word)
        self.assertGreaterEqual(len(survey_by_route[ROUTE_ORTHO]), 2)
        self.assertGreaterEqual(len(survey_by_route[ROUTE_OBLIQUE]), 2)
        self.assertGreaterEqual(len(survey_by_route[ROUTE_PANORAMA]), 2)
        patrol_by_route = defaultdict(list)
        for word, route in PATROL_TRIGGER_ROUTES:
            patrol_by_route[route].append(word)
        self.assertGreaterEqual(len(patrol_by_route[ROUTE_WAYPOINT]), 4)
        self.assertGreaterEqual(len(patrol_by_route[ROUTE_AREA]), 5)
        self.assertGreaterEqual(len(patrol_by_route[ROUTE_ORBIT]), 1)

    def test_survey_row_ortho(self):
        for word in ("正射影像", "二维地图"):
            with self.subTest(trigger=word):
                decision = map_trigger_to_route(f"去天健湖做{word}")
                self.assertEqual(decision.route_type, ROUTE_ORTHO)
                self.assertEqual(decision.trigger_category, TRIGGER_CATEGORY_SURVEY)
                self.assertEqual(decision.matched_triggers, (word,))
                self.assertEqual(decision.flight_rules, FLIGHT_RULES_SURVEY)
                self.assertTrue(decision.accepted)
                self.assertFalse(decision.rejected)
                self.assertEqual(decision.message, "")

    def test_survey_row_oblique(self):
        for word in ("三维建模", "倾斜摄影"):
            with self.subTest(trigger=word):
                decision = map_trigger_to_route(f"对矿区做{word}")
                self.assertEqual(decision.route_type, ROUTE_OBLIQUE)
                self.assertEqual(decision.flight_rules, FLIGHT_RULES_SURVEY)

    def test_survey_row_panorama(self):
        for word in ("全景图", "360°全景"):
            with self.subTest(trigger=word):
                decision = map_trigger_to_route(f"拍一张{word}")
                self.assertEqual(decision.route_type, ROUTE_PANORAMA)
                self.assertEqual(decision.flight_rules, FLIGHT_RULES_SURVEY)

    def test_patrol_row_along(self):
        for word in ("沿河", "沿路", "沿线"):
            with self.subTest(trigger=word):
                decision = map_trigger_to_route(f"巡{word}检查")
                self.assertEqual(decision.route_type, ROUTE_WAYPOINT)
                self.assertEqual(decision.trigger_category, TRIGGER_CATEGORY_PATROL)

    def test_patrol_row_area(self):
        for word in ("全域", "全面", "普通面状"):
            with self.subTest(trigger=word):
                decision = map_trigger_to_route(f"对片区做{word}巡检")
                self.assertEqual(decision.route_type, ROUTE_AREA)
                self.assertEqual(decision.flight_rules, FLIGHT_RULES_AREA)

    def test_patrol_row_survey_scan(self):
        for word in ("普查", "快速扫描"):
            with self.subTest(trigger=word):
                decision = map_trigger_to_route(f"对该区域{word}")
                self.assertEqual(decision.route_type, ROUTE_AREA)

    def test_patrol_row_point_inspection(self):
        decision = map_trigger_to_route("对几个塔基挨个检查")
        self.assertEqual(decision.route_type, ROUTE_WAYPOINT)
        self.assertEqual(decision.matched_triggers, ("挨个检查",))

        decision = map_trigger_to_route("围绕1号水塔绕一圈")
        self.assertEqual(decision.route_type, ROUTE_ORBIT)
        self.assertEqual(decision.flight_rules, FLIGHT_RULES_ORBIT)

    def test_survey_triggers_take_priority_over_patrol(self):
        decision = map_trigger_to_route("沿河做正射影像")
        self.assertEqual(decision.route_type, ROUTE_ORTHO)
        self.assertEqual(decision.trigger_category, TRIGGER_CATEGORY_SURVEY)
        self.assertEqual(decision.matched_triggers, ("正射影像",))

    def test_multiple_triggers_use_table_order(self):
        decision = map_trigger_to_route("先倾斜摄影再正射影像")
        self.assertEqual(decision.route_type, ROUTE_ORTHO)
        self.assertEqual(decision.matched_triggers, ("正射影像", "倾斜摄影"))

    def test_unmatched_text_needs_clarification(self):
        decision = map_trigger_to_route("去天健湖看看")
        self.assertIsNone(decision.route_type)
        self.assertIsNone(decision.rejection_code)
        self.assertEqual(decision.message, "")
        self.assertFalse(decision.accepted)
        self.assertFalse(decision.rejected)
        self.assertTrue(decision.needs_clarification)
        self.assertFalse(decision.conflict)

    def test_grid_and_body_route_are_rejected_in_trigger_mapping(self):
        grid = map_trigger_to_route("用网格航线飞")
        self.assertEqual(grid.rejection_code, REJECTION_GRID_ROUTE)
        self.assertEqual(grid.message, MESSAGE_REJECT_GRID)
        self.assertIsNone(grid.route_type)

        body = map_trigger_to_route("用几何体航线飞")
        self.assertEqual(body.rejection_code, REJECTION_GEOMETRY_ROUTE)
        self.assertEqual(body.message, MESSAGE_REJECT_GEOMETRY_ROUTE)

    def test_ordinary_geometry_description_is_not_rejected(self):
        # M1 回归：普通描述句里的「几何体」不是「几何体航线」，不得套网格话术。
        for text in ("检查建筑几何体是否变形", "评估这栋楼的几何体"):
            with self.subTest(text=text):
                decision = map_trigger_to_route(text)
                self.assertIsNone(decision.rejection_code)
                self.assertTrue(decision.needs_clarification)

    def test_negated_unsupported_mentions_do_not_reject(self):
        # 否定句：「不要用网格航线，改用正射影像」→ 正射航线，不能按网格话术拦截。
        decision = map_trigger_to_route("不要用网格航线，改用正射影像")
        self.assertIsNone(decision.rejection_code)
        self.assertEqual(decision.route_type, ROUTE_ORTHO)
        self.assertNotIn("网格航线", decision.matched_triggers)

        decision = map_trigger_to_route("不要用几何体航线")
        self.assertIsNone(decision.rejection_code)
        self.assertTrue(decision.needs_clarification)


class Table2GeometryMappingTests(unittest.TestCase):
    """3.2.2 表2：目标几何 → 允许航线 / 强制拦截。"""

    def test_point_default_waypoint_and_hover_rules(self):
        decision = map_geometry_to_route(GEOMETRY_POINT)
        self.assertEqual(decision.route_type, ROUTE_WAYPOINT)
        self.assertEqual(decision.geometry, GEOMETRY_POINT)
        self.assertEqual(decision.target_count, 1)
        self.assertEqual(decision.flight_rules, FLIGHT_RULES_POINT)
        self.assertTrue(decision.accepted)
        self.assertFalse(decision.rejected)

    def test_point_explicit_orbit_is_clockwise_once(self):
        decision = map_geometry_to_route(GEOMETRY_POINT, orbit_requested=True)
        self.assertEqual(decision.route_type, ROUTE_ORBIT)
        self.assertEqual(decision.flight_rules, FLIGHT_RULES_ORBIT)
        # 假设 6：环绕航线二选一，不叠加「悬停60秒/云台垂直向下」。
        self.assertNotIn("到点默认悬停60秒，云台垂直向下；", decision.flight_rules)

    def test_multi_point_contract_reports_count_without_claiming_ordering(self):
        # M3 修正：本模块不接收有序目标列表、不排序；只记录数量并把表2 的
        # 「严格按用户顺序飞」作为规则片段交给上游。因此这里只断言数量契约，
        # 不声称顺序保真（顺序由计划/航线生成层负责）。
        single = map_geometry_to_route(GEOMETRY_POINT)
        multi = map_geometry_to_route(GEOMETRY_POINT, target_count=3)
        self.assertEqual(multi.route_type, ROUTE_WAYPOINT)
        self.assertEqual(multi.target_count, 3)
        self.assertEqual(single.target_count, 1)
        self.assertNotEqual(single, multi)
        self.assertFalse(multi.rejected)
        self.assertIn("多点串联：严格按用户顺序飞。", multi.flight_rules)

    def test_line_waypoint_forced_endpoints(self):
        decision = map_geometry_to_route(GEOMETRY_LINE)
        self.assertEqual(decision.route_type, ROUTE_WAYPOINT)
        self.assertEqual(decision.geometry, GEOMETRY_LINE)
        self.assertEqual(decision.flight_rules, FLIGHT_RULES_LINE)
        self.assertIn("航点航线（强制端点）", decision.flight_rules)
        self.assertIn("单线单任务：就近选择线的一端开始，飞向另一端，不折返。",
                      decision.flight_rules)

    def test_line_multi_target_is_rejected_verbatim(self):
        decision = map_geometry_to_route(GEOMETRY_LINE, target_count=2)
        self.assertEqual(decision.rejection_code, REJECTION_MULTI_LINE)
        self.assertEqual(decision.message, "暂不支持同时巡检多条线")
        self.assertIsNone(decision.route_type)
        self.assertTrue(decision.rejected)

    def test_multi_line_geometry_is_rejected_verbatim(self):
        decision = map_geometry_to_route(GEOMETRY_MULTI_LINE)
        self.assertEqual(decision.rejection_code, REJECTION_MULTI_LINE)
        self.assertEqual(decision.message, MESSAGE_REJECT_MULTI_LINE)

    def test_area_route_is_bow_shape_full_cover(self):
        decision = map_geometry_to_route(GEOMETRY_AREA)
        self.assertEqual(decision.route_type, ROUTE_AREA)
        self.assertEqual(decision.geometry, GEOMETRY_AREA)
        self.assertEqual(decision.flight_rules, FLIGHT_RULES_AREA)
        self.assertIn("面状航线（正射全覆盖）", decision.flight_rules)
        self.assertIn("单面单任务：自动生成“弓”字形航线，满铺扫描。", decision.flight_rules)

    def test_area_multi_target_is_rejected_verbatim(self):
        decision = map_geometry_to_route(GEOMETRY_AREA, target_count=2)
        self.assertEqual(decision.rejection_code, REJECTION_MULTI_AREA)
        self.assertEqual(decision.message, "暂不支持同时巡检多个面")
        self.assertIsNone(decision.route_type)

    def test_multi_area_geometry_is_rejected_verbatim(self):
        decision = map_geometry_to_route(GEOMETRY_MULTI_AREA)
        self.assertEqual(decision.rejection_code, REJECTION_MULTI_AREA)
        self.assertEqual(decision.message, MESSAGE_REJECT_MULTI_AREA)

    def test_volume_defaults_to_table_6_wording(self):
        decision = map_geometry_to_route(GEOMETRY_VOLUME)
        self.assertEqual(decision.rejection_code, REJECTION_VOLUME)
        self.assertEqual(decision.message, "当前版本不支持立体目标规划，请将其标注为点或面。")
        self.assertIsNone(decision.route_type)
        self.assertTrue(decision.rejected)
        self.assertFalse(decision.accepted)
        self.assertFalse(decision.needs_clarification)

    def test_volume_table_2_wording_is_selectable(self):
        # L1：两版都是规范原文，短版必须由调用方取得到。
        decision = map_geometry_to_route(GEOMETRY_VOLUME, volume_message_variant="table_2")
        self.assertEqual(decision.message, MESSAGE_REJECT_VOLUME_TABLE_2)
        self.assertEqual(decision.message, "当前版本不支持立体目标，请标注为点或面。")

    def test_invalid_volume_variant_raises(self):
        for variant in ("5.4", "", None, 2020):
            with self.subTest(variant=variant):
                with self.assertRaises(ValueError):
                    map_geometry_to_route(GEOMETRY_VOLUME, volume_message_variant=variant)

    def test_mixed_geometry_is_rejected_verbatim(self):
        decision = map_geometry_to_route(GEOMETRY_MIXED)
        self.assertEqual(decision.rejection_code, REJECTION_MIXED)
        self.assertEqual(decision.message, "单次任务暂不支持混合目标巡检，请分次下发。")

    def test_grid_geometry_is_rejected_verbatim(self):
        decision = map_geometry_to_route(GEOMETRY_GRID)
        self.assertEqual(decision.rejection_code, REJECTION_GRID_ROUTE)
        self.assertEqual(
            decision.message,
            "当前版本暂不支持自动规划复杂路径或网格航线，请手动接管。",
        )

    def test_body_route_geometry_is_rejected(self):
        decision = map_geometry_to_route(GEOMETRY_BODY_ROUTE)
        self.assertEqual(decision.rejection_code, REJECTION_GEOMETRY_ROUTE)
        self.assertEqual(decision.message, MESSAGE_REJECT_GEOMETRY_ROUTE)

    def test_unknown_geometry_raises(self):
        for geometry in ("区域", "point", "", None):
            with self.subTest(geometry=geometry):
                with self.assertRaises(ValueError):
                    map_geometry_to_route(geometry)

    def test_invalid_target_count_raises(self):
        for count in (0, -1, True, 1.5, "2"):
            with self.subTest(target_count=count):
                with self.assertRaises(ValueError):
                    map_geometry_to_route(GEOMETRY_POINT, target_count=count)

    def test_invalid_orbit_requested_raises(self):
        for value in ("yes", 1, 0):
            with self.subTest(orbit_requested=value):
                with self.assertRaises(ValueError):
                    map_geometry_to_route(GEOMETRY_POINT, orbit_requested=value)


class CombinedDecisionTests(unittest.TestCase):
    """3.2.1 表1 + 3.2.2 表2 合并判定。"""

    def test_patrol_triggers_adapt_to_geometry(self):
        # 表1 巡检行原文「根据静态标注类型自适应」，几何优先属规范要求而非冲突。
        cases = (
            ("沿河巡查", GEOMETRY_POINT, ROUTE_WAYPOINT),
            ("沿河巡查", GEOMETRY_LINE, ROUTE_WAYPOINT),
            ("沿河巡查", GEOMETRY_AREA, ROUTE_AREA),
            ("全域普查", GEOMETRY_POINT, ROUTE_WAYPOINT),
            ("全域普查", GEOMETRY_AREA, ROUTE_AREA),
            ("普查", GEOMETRY_POINT, ROUTE_WAYPOINT),
            ("挨个检查", GEOMETRY_POINT, ROUTE_WAYPOINT),
            ("绕一圈", GEOMETRY_POINT, ROUTE_ORBIT),
            ("绕一圈", GEOMETRY_AREA, ROUTE_AREA),
        )
        for text, geometry, expected in cases:
            with self.subTest(text=text, geometry=geometry):
                decision = decide_route(text, geometry)
                self.assertEqual(decision.route_type, expected)
                self.assertEqual(decision.geometry, geometry)
                self.assertFalse(decision.conflict)
                self.assertTrue(decision.accepted)

    def test_patrol_trigger_geometry_rules(self):
        point = decide_route("沿河巡查", GEOMETRY_POINT)
        self.assertEqual(point.flight_rules, FLIGHT_RULES_POINT)
        line = decide_route("沿河巡查", GEOMETRY_LINE)
        self.assertEqual(line.flight_rules, FLIGHT_RULES_LINE)
        area = decide_route("沿河巡查", GEOMETRY_AREA)
        self.assertEqual(area.flight_rules, FLIGHT_RULES_AREA)

    def test_explicit_orbit_request_overrides_patrol_trigger(self):
        decision = decide_route("沿河巡查", GEOMETRY_POINT, orbit_requested=True)
        self.assertEqual(decision.route_type, ROUTE_ORBIT)
        self.assertEqual(decision.flight_rules, FLIGHT_RULES_ORBIT)

    def test_orbit_wording_is_inferred_from_text(self):
        # 3.2.2 点行「若指明环绕」：文本含环绕措辞时按环绕航线判定。
        decision = decide_route("环绕1号水塔一圈", GEOMETRY_POINT)
        self.assertEqual(decision.route_type, ROUTE_ORBIT)
        self.assertEqual(decision.flight_rules, FLIGHT_RULES_ORBIT)

    def test_common_words_do_not_false_positive_rejections(self):
        # 「螺旋桨」「网格化片区」「普通几何体描述」是正常词，不是不支持的航线。
        for text in ("检查螺旋桨", "巡查网格化片区", "检查建筑几何体是否变形"):
            with self.subTest(text=text):
                decision = map_trigger_to_route(text)
                self.assertIsNone(decision.rejection_code)
                self.assertTrue(decision.needs_clarification)

    def test_survey_with_area_keeps_survey_route(self):
        # 表2 面行航线是「面状航线（正射全覆盖）」，与测绘建图语义一致。
        for text, expected in (
            ("正射影像", ROUTE_ORTHO),
            ("倾斜摄影", ROUTE_OBLIQUE),
            ("全景图", ROUTE_PANORAMA),
        ):
            with self.subTest(text=text):
                decision = decide_route(text, GEOMETRY_AREA)
                self.assertEqual(decision.route_type, expected)
                self.assertFalse(decision.conflict)
                self.assertTrue(decision.accepted)

    def test_survey_with_point_or_line_is_conflict_awaiting_clarification(self):
        # M2 修正：3.2.1 测绘类「强制匹配」与 3.2.2 点/线允许航线不兼容，规范
        # 未定义取舍 -> 不静默选边，返回 conflict 待追问，由产品/规范负责人裁决。
        for text, geometry in (
            ("正射影像", GEOMETRY_POINT),
            ("倾斜摄影", GEOMETRY_LINE),
            ("360°全景", GEOMETRY_POINT),
        ):
            with self.subTest(text=text, geometry=geometry):
                decision = decide_route(text, geometry)
                self.assertIsNone(decision.route_type)
                self.assertIsNone(decision.rejection_code)
                self.assertTrue(decision.conflict)
                self.assertTrue(decision.needs_clarification)
                self.assertIn("规范未定义", decision.conflict_reason)
                self.assertIn("产品待裁决", decision.conflict_reason)
                self.assertEqual(decision.message, "")
                self.assertEqual(decision.matched_triggers, (text,))

    def test_survey_without_geometry_keeps_table_1_route(self):
        decision = decide_route("正射影像")
        self.assertEqual(decision.route_type, ROUTE_ORTHO)
        self.assertFalse(decision.conflict)

    def test_geometry_rejections_win_over_trigger_mapping(self):
        volume = decide_route("正射影像", GEOMETRY_VOLUME)
        self.assertEqual(volume.rejection_code, REJECTION_VOLUME)
        mixed = decide_route("沿河巡查", GEOMETRY_MIXED)
        self.assertEqual(mixed.rejection_code, REJECTION_MIXED)
        multi_line = decide_route("沿线巡查", GEOMETRY_LINE, target_count=2)
        self.assertEqual(multi_line.rejection_code, REJECTION_MULTI_LINE)
        multi_area = decide_route("全域普查", GEOMETRY_AREA, target_count=2)
        self.assertEqual(multi_area.rejection_code, REJECTION_MULTI_AREA)

    def test_text_route_kind_rejection_wins_over_geometry(self):
        decision = decide_route("用网格航线飞", GEOMETRY_AREA)
        self.assertEqual(decision.rejection_code, REJECTION_GRID_ROUTE)
        self.assertEqual(decision.message, MESSAGE_REJECT_GRID)
        self.assertEqual(decision.geometry, GEOMETRY_AREA)

    def test_complex_path_words_rejected_with_verbatim_message(self):
        decision = decide_route("绕楼螺旋飞")
        self.assertEqual(decision.rejection_code, REJECTION_GRID_ROUTE)
        self.assertEqual(decision.message, MESSAGE_REJECT_GRID)
        self.assertEqual(decision.matched_triggers, ("螺旋飞",))

    def test_geometry_only_defaults(self):
        self.assertEqual(decide_route("去看看", GEOMETRY_POINT).route_type, ROUTE_WAYPOINT)
        self.assertEqual(decide_route("去看看", GEOMETRY_LINE).route_type, ROUTE_WAYPOINT)
        self.assertEqual(decide_route("去看看", GEOMETRY_AREA).route_type, ROUTE_AREA)

    def test_nothing_matched_needs_clarification(self):
        decision = decide_route("随便飞飞")
        self.assertTrue(decision.needs_clarification)
        self.assertIsNone(decision.route_type)
        self.assertIsNone(decision.rejection_code)
        self.assertEqual(decision.message, "")
        self.assertEqual(decision.matched_triggers, ())
        self.assertIsNone(decision.trigger_category)
        self.assertFalse(decision.conflict)

    def test_decide_route_passes_volume_variant(self):
        decision = decide_route("去看看", GEOMETRY_VOLUME, volume_message_variant="table_2")
        self.assertEqual(decision.message, MESSAGE_REJECT_VOLUME_TABLE_2)

    def test_decide_route_validates_contract(self):
        for text in ("", "  ", None, 123):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    decide_route(text)
        with self.assertRaises(ValueError):
            decide_route("沿河巡查", "区域")
        with self.assertRaises(ValueError):
            decide_route("沿河巡查", GEOMETRY_LINE, target_count=0)
        with self.assertRaises(ValueError):
            decide_route("沿河巡查", orbit_requested="yes")
        with self.assertRaises(ValueError):
            decide_route("沿河巡查", volume_message_variant="5.4")

    def test_decisions_are_deterministic_and_hashable(self):
        first = decide_route("沿河巡查", GEOMETRY_LINE)
        second = decide_route("沿河巡查", GEOMETRY_LINE)
        self.assertEqual(first, second)
        self.assertEqual(hash(first), hash(second))
        self.assertIsInstance(first, RouteDecision)


class InFlightBoundaryTests(unittest.TestCase):
    """1.1 能力矩阵：飞行中仅支持单目标指点飞行/环绕。"""

    def test_single_target_limit(self):
        self.assertEqual(IN_FLIGHT_MAX_TARGETS, 1)
        self.assertTrue(in_flight_target_count_supported(1))
        self.assertFalse(in_flight_target_count_supported(2))
        self.assertFalse(in_flight_target_count_supported(3))

    def test_invalid_target_count_raises(self):
        for count in (0, -1, True, "1"):
            with self.subTest(target_count=count):
                with self.assertRaises(ValueError):
                    in_flight_target_count_supported(count)

    def test_in_flight_route_types(self):
        self.assertIn(ROUTE_WAYPOINT, IN_FLIGHT_ROUTE_TYPES)
        self.assertIn(ROUTE_ORBIT, IN_FLIGHT_ROUTE_TYPES)
        for route in (ROUTE_ORTHO, ROUTE_OBLIQUE, ROUTE_PANORAMA, ROUTE_AREA):
            self.assertNotIn(route, IN_FLIGHT_ROUTE_TYPES)

    def test_in_flight_single_point_supported(self):
        waypoint = decide_in_flight_route(GEOMETRY_POINT)
        self.assertEqual(waypoint.route_type, ROUTE_WAYPOINT)
        self.assertTrue(waypoint.route_type in IN_FLIGHT_ROUTE_TYPES)
        self.assertEqual(waypoint.target_count, 1)
        self.assertEqual(waypoint.flight_rules, FLIGHT_RULES_POINT)
        orbit = decide_in_flight_route(GEOMETRY_POINT, orbit_requested=True)
        self.assertEqual(orbit.route_type, ROUTE_ORBIT)
        self.assertTrue(orbit.route_type in IN_FLIGHT_ROUTE_TYPES)
        self.assertEqual(orbit.flight_rules, FLIGHT_RULES_ORBIT)

    def test_in_flight_multi_or_non_point_rejected_without_invented_wording(self):
        # 规范只给能力边界、没有逐字话术：message 必须为空并由上游 4.x 流程补话术。
        for geometry, count in (
            (GEOMETRY_POINT, 2),
            (GEOMETRY_LINE, 1),
            (GEOMETRY_AREA, 1),
            (GEOMETRY_VOLUME, 1),
        ):
            with self.subTest(geometry=geometry, target_count=count):
                decision = decide_in_flight_route(geometry, target_count=count)
                self.assertEqual(decision.rejection_code, REJECTION_IN_FLIGHT_TARGET)
                self.assertEqual(decision.message, "")
                self.assertIsNone(decision.route_type)
                self.assertTrue(decision.rejected)

    def test_in_flight_route_validates_contract(self):
        with self.assertRaises(ValueError):
            decide_in_flight_route("区域")
        with self.assertRaises(ValueError):
            decide_in_flight_route(GEOMETRY_POINT, target_count=0)
        with self.assertRaises(ValueError):
            decide_in_flight_route(GEOMETRY_POINT, orbit_requested="yes")


if __name__ == "__main__":
    unittest.main()
