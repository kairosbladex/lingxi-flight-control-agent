"""V1.4 3.2.1 / 3.2.2：触发词 → 航线类型、目标几何 → 允许航线/拦截的纯规则库。

本仓归属与接线状态（2026-10-27 纠正）
====================================
**本模块是纯规则库 / 参考实现，当前未接线到 cockpit / service / agent 等生产路径**，
本仓没有任何生产代码 import 它；目前只有单元测试（以及后续显式集成方）会调用。

V1.4 3.2.1 / 3.2.2 的职责归属在本仓与规划 Agent（``lingxi-agent``）之间**尚未裁决**：
设计规范要求地面规划阶段由规划 Agent 执行，而本仓既有
``target_resolver.TargetGeometry`` + ``handoff_contract._parse_targets`` 已在承担
3.2.2 的目标几何校验。在裁决前必须遵守：

* 本模块不得作为运行时唯一真源；
* 不得与 ``target_resolver`` / ``handoff_contract`` 对同一输入并行做强制校验，
  避免双真源与两套拒绝话术；
* 只允许作为纯判定辅助：输入「文本触发词 / 标注几何」，输出航线类型或规范话术，
  不解析自然语言、不做几何求解、不生成航点、不调平台；
* 拒绝话术的单一真源仍是 ``flight_policy``（本模块 ``MESSAGE_REJECT_*`` 只是别名，
  不得再写重复字面量）。

验收口径与分层建议见 ``docs/ACCEPTANCE_FLIGHT-V14.md`` 的「2026-10-27 纠正」段落。

规范来源
========
``/Users/wangxiaolin/Desktop/灵犀平台自然语言智能控制功能设计规范V1.4.docx``：

* 3.2.1 表1「测绘与巡检任务触发词映射」
* 3.2.2 表2「目的地静态标注与航线强制映射」
* 第五章「异常判断与反馈话术明细（测试用例库）」表6——本库用到的拒绝话术原文
* 1.1 能力矩阵——飞行中「仅支持指点飞行/环绕（单目标）」的能力边界

设计边界
========
* 纯函数、确定性、无外部依赖、不联网、不调平台、不做几何求解：不插值、
  不生成航点坐标、不判断禁飞区，只回答「该用哪种航线 / 是否必须拒绝」。
* 拒绝话术逐字保持规范原文（全角标点不改写）；3.2.2 表格单元格自带的中文
  引号在常量中已去掉，只保留句子本体。
* 规范未定义的行为不臆造话术：触发词完全未命中、或命中规范未定义取舍的冲突
  组合时返回 ``route_type=None`` / ``rejection_code=None``（``needs_clarification``
  为 True），由上游追问；``geometry`` / ``target_count`` / ``orbit_requested`` /
  ``volume_message_variant`` 非法属于调用契约错误，直接 ``ValueError``。

规范未定义处的假设（均不影响逐字话术）
======================================
1. 表2 的体拦截话术在 3.2.2 为「当前版本不支持立体目标，请标注为点或面。」，
   第五章表6 R8 为「当前版本不支持立体目标规划，请将其标注为点或面。」。
   对外 ``message`` 默认采用表6 完整版；``map_geometry_to_route`` /
   ``decide_route`` 的 ``volume_message_variant='table_2'`` 可取 3.2.2 短版。
   两版都是规范原文，选择权交给调用方/上游章节。
2. 3.2.2 只规定 体/混合/多线/多面 的拦截，未给「几何体航线」独立话术；表1
   注记「网格航线和几何体航线直接拒绝」，第五章表6 R6 给出统一话术，故几何体
   航线复用 ``MESSAGE_REJECT_GRID``（同属「拦截，不解析 / 手动接管」）。
3. 同一句文本命中多个表1 触发词时，按表1 出现顺序取第一个（确定性优先，
   不新增冲突追问话术）；测绘建图类优先于巡检巡查类。
4. **冲突处理（规范未定义、产品待裁决的假设）**：3.2.1 测绘建图类要求「强制
   匹配对应航线」，而 3.2.2 表2 对「点」只允许航点/环绕、对「线」只允许航点。
   两者同时给出且不兼容时（如「正射影像 + 点」「倾斜摄影 + 线」），本库**不静默
   选一边**，返回 ``conflict=True`` 的待追问判定（``route_type=None``、
   ``rejection_code=None``、``conflict_reason`` 说明冲突），由上游追问用户；
   这是规范未定义的取舍，须由产品/规范负责人裁决后再固化为默认行为。
   测绘建图类 + 「面」不算冲突：表2 面行航线为「面状航线（正射全覆盖）」，
   与测绘航行语义一致，保留表1 强制匹配的测绘航线。
5. 3.2.1 巡检巡查类行明写「根据静态标注类型自适应」，故巡检触发词命中时由
   表2 几何类型决定航线（点 → 航点，指明环绕则环绕；线 → 航点；面 → 面状）。
   这是表1 原文要求，不是本库自定假设。
6. 表2 点行用分号并列「悬停60秒/云台垂直向下」与「环绕1圈/云台朝向目标」；
   本库按航线类型二选一，不叠加（环绕航线不带「悬停60秒」规则）。
7. 表2 只在点行写明「多点串联」允许，故 ``target_count`` 的数量拦截只对
   线（>1 → 多线拦截）与面（>1 → 多面拦截）生效，点可大于 1。
8. 表1 巡检行原文「挨个检查、绕一圈」→ 航点航线 / 环绕航线是二选一。本库在
   **没有几何信息**时把「挨个检查」解释为航点航线、「绕一圈」解释为环绕航线；
   有几何信息时按表2 自适应（绕一圈 + 面 → 面状航线）。逐词归属是解释性假设。
9. **多点顺序不在本模块处理**：本模块不接收有序目标列表、不排序、不生成航点；
   ``RouteDecision.target_count`` 只记录数量，表2 点行的「严格按用户顺序飞」
   以逐字规则片段形式交给上游。真正的顺序保真由计划/航线生成层负责。
10. 1.1 能力矩阵飞行中「仅支持指点飞行/环绕（单目标）」：规范只给能力边界，
    没有逐字拒绝话术。本库 ``in_flight_target_count_supported`` 与
    ``decide_in_flight_route`` 只做布尔/结构化判界，拒绝时 ``message`` 留空，
    由 4.x 的拦截/追问流程提供话术，不得复用本模块的地面规划话术。
11. 网格航线 / 几何体航线 / 螺旋飞等不支持词的文本匹配会跳过被否定的出现
    （如「不要用网格航线，改用正射影像」不拦截网格），避免把否定句当正句。
12. ``FLIGHT_RULES_*`` 是从表1/表2 单元格**逐字切分**出的规则片段（保留原
    标点），不是完整句式；调用方按片段绑定执行细节，不得改写片段文本。
"""
from __future__ import annotations

from dataclasses import dataclass

#: 拒绝话术单一真源：规范原文常量统一由 ``flight_policy`` 持有（M2 集成评审修复）。
#: 本模块只做别名，不允许再写重复字面量，避免改一处静默分叉。
from .flight_policy import (
    BODY_TARGET_MESSAGE,
    BODY_TARGET_STANDARD_MESSAGE,
    COMPLEX_ROUTE_MANUAL_MESSAGE,
    MIXED_TARGET_MESSAGE,
)

# ---------------------------------------------------------------------------
# 3.2.1 表1 / 3.2.2 表2 的航线类型原文
# ---------------------------------------------------------------------------

#: 3.2.1 表1 测绘建图类航线。
ROUTE_ORTHO = "正射航线"
ROUTE_OBLIQUE = "倾斜摄影航线"
ROUTE_PANORAMA = "全景航线"
#: 3.2.1 表1 巡检巡查类 / 3.2.2 表2 允许的航线。
ROUTE_WAYPOINT = "航点航线"
ROUTE_AREA = "面状航线"
ROUTE_ORBIT = "环绕航线"

#: 测绘建图类航线（3.2.1 表1 第一行）。
SURVEY_ROUTES = frozenset({ROUTE_ORTHO, ROUTE_OBLIQUE, ROUTE_PANORAMA})
#: 巡检巡查类航线（3.2.1 表1 第二行）。
PATROL_ROUTES = frozenset({ROUTE_WAYPOINT, ROUTE_AREA, ROUTE_ORBIT})
#: 本库定义的全部航线类型。
ROUTE_TYPES = SURVEY_ROUTES | PATROL_ROUTES

# ---------------------------------------------------------------------------
# 3.2.2 表2 第一列的标注类型，以及表1 注记/数量条件产生的「仅拒绝」标记
# ---------------------------------------------------------------------------

GEOMETRY_POINT = "点"
GEOMETRY_LINE = "线"
GEOMETRY_AREA = "面"
GEOMETRY_VOLUME = "体"
GEOMETRY_MIXED = "混合类型"
GEOMETRY_MULTI_LINE = "多线"
GEOMETRY_MULTI_AREA = "多面"
GEOMETRY_GRID = "网格航线"
GEOMETRY_BODY_ROUTE = "几何体航线"

#: 3.2.2 表2 第一列真正列出的标注几何类型（点/线/面/体/混合类型）。
ANNOTATION_GEOMETRIES = frozenset({
    GEOMETRY_POINT,
    GEOMETRY_LINE,
    GEOMETRY_AREA,
    GEOMETRY_VOLUME,
    GEOMETRY_MIXED,
})
#: 不是标注几何，而是 3.2.1 注记 / 数量条件产生的「直接拒绝」标记。
#: 仍接受在本库 ``geometry`` 槽位，作为统一拒绝入口，但在常量命名上显式区分。
REJECTION_ONLY_GEOMETRIES = frozenset({
    GEOMETRY_MULTI_LINE,
    GEOMETRY_MULTI_AREA,
    GEOMETRY_GRID,
    GEOMETRY_BODY_ROUTE,
})
#: 3.2.2 表2 允许自动规划的目标几何（点/线/面）。
SUPPORTED_GEOMETRIES = frozenset({GEOMETRY_POINT, GEOMETRY_LINE, GEOMETRY_AREA})
#: 3.2.2 表2 异常拦截列 / 3.2.1 注记中必须直接拒绝的类型。
REJECTED_GEOMETRIES = frozenset({GEOMETRY_VOLUME, GEOMETRY_MIXED}) | REJECTION_ONLY_GEOMETRIES
#: ``geometry`` 槽位接受的全部取值（标注几何 + 仅拒绝标记）。
GEOMETRIES = ANNOTATION_GEOMETRIES | REJECTION_ONLY_GEOMETRIES

# ---------------------------------------------------------------------------
# 稳定拒绝码：调用方按 code 分支，不解析 message 文案
# ---------------------------------------------------------------------------

REJECTION_VOLUME = "volume_unsupported"
REJECTION_MIXED = "mixed_targets_unsupported"
REJECTION_MULTI_LINE = "multi_line_unsupported"
REJECTION_MULTI_AREA = "multi_area_unsupported"
REJECTION_GRID_ROUTE = "grid_route_unsupported"
REJECTION_GEOMETRY_ROUTE = "geometry_route_unsupported"
#: 1.1 能力矩阵飞行中单目标边界；规范未给话术，message 留空。
REJECTION_IN_FLIGHT_TARGET = "in_flight_target_unsupported"

# ---------------------------------------------------------------------------
# 拒绝话术：单一真源在 flight_policy，本模块只保留旧常量名做别名
# ---------------------------------------------------------------------------
#
# M2 集成评审修复：以下四句规范原文常量统一由 ``flight_policy`` 持有，
# route_mapping 不再复制字面量；旧公开常量名保留以兼容既有调用/测试。

#: 3.2.2 表2 体行「异常拦截」原文（短版）；= flight_policy.BODY_TARGET_STANDARD_MESSAGE。
MESSAGE_REJECT_VOLUME_TABLE_2 = BODY_TARGET_STANDARD_MESSAGE
#: 第五章表6 R8「期望系统返回」原文（完整版，默认对外话术）；= flight_policy.BODY_TARGET_MESSAGE。
MESSAGE_REJECT_VOLUME = BODY_TARGET_MESSAGE
#: 3.2.2 表2 混合类型行「异常拦截」原文；= flight_policy.MIXED_TARGET_MESSAGE。
MESSAGE_REJECT_MIXED = MIXED_TARGET_MESSAGE
#: 3.2.2 表2 线行「异常拦截」原文；flight_policy 无对应常量，唯一真源在本模块。
MESSAGE_REJECT_MULTI_LINE = "暂不支持同时巡检多条线"
#: 3.2.2 表2 面行「异常拦截」原文；flight_policy 无对应常量，唯一真源在本模块。
MESSAGE_REJECT_MULTI_AREA = "暂不支持同时巡检多个面"
#: 第五章表6 R6「期望系统返回」原文；= flight_policy.COMPLEX_ROUTE_MANUAL_MESSAGE。
MESSAGE_REJECT_GRID = COMPLEX_ROUTE_MANUAL_MESSAGE
#: 表1 注记要求几何体航线直接拒绝；规范未给独立话术，复用统一话术（见模块假设 2）。
MESSAGE_REJECT_GEOMETRY_ROUTE = COMPLEX_ROUTE_MANUAL_MESSAGE

# ---------------------------------------------------------------------------
# 3.2.1 表1 触发词 → 航线类型（按表内出现顺序，命中即按序优先）
# ---------------------------------------------------------------------------

#: 触发词大类名（与表1 第一列一致）。
TRIGGER_CATEGORY_SURVEY = "测绘建图类"
TRIGGER_CATEGORY_PATROL = "巡检巡查类"

#: 3.2.1 表1 测绘建图类：触发词 → 航线类型。
SURVEY_TRIGGER_ROUTES = (
    ("正射影像", ROUTE_ORTHO),
    ("二维地图", ROUTE_ORTHO),
    ("三维建模", ROUTE_OBLIQUE),
    ("倾斜摄影", ROUTE_OBLIQUE),
    ("全景图", ROUTE_PANORAMA),
    ("360°全景", ROUTE_PANORAMA),
)

#: 3.2.1 表1 巡检巡查类：触发词 → 表1 直接给出的航线类型。
PATROL_TRIGGER_ROUTES = (
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

#: 3.2.2 表2 点行「若指明环绕」的明确措辞；「绕一圈」同时来自表1。
ORBIT_TRIGGER_WORDS = ("环绕", "绕一圈")
#: 3.2.1 表1 注记 / 第五章表6 R6：网格航线直接拒绝（只认航线措辞，避免误伤
#: 「网格化片区」这类正常区域词）。
GRID_ROUTE_WORDS = ("网格航线", "网格飞")
#: 3.2.1 表1 注记：几何体航线直接拒绝（只认完整「几何体航线」，不把普通
#: 描述句里的「几何体」误判为航线，如「检查建筑几何体是否变形」）。
BODY_ROUTE_WORDS = ("几何体航线",)
#: 第五章表6 R6 的「绕楼螺旋飞」同属不支持路径；只认「螺旋飞」避免误伤螺旋桨。
COMPLEX_PATH_WORDS = ("螺旋飞",)

#: 否定词：出现在不支持词/触发词之前的小窗口内时，该次出现不计为正句提及。
#: 只做局部窗口匹配，不假装理解整句否定语义。
_NEGATION_MARKERS = (
    "不要", "不用", "别", "勿", "禁止", "避免", "不是", "不采用",
    "无需", "不必", "不选", "而非", "不按", "不搞",
)
_NEGATION_WINDOW = 5

# ---------------------------------------------------------------------------
# 3.2.2 表2「巡飞规则（核心逻辑）」的逐字规则片段（从单元格切分，保留标点）
# ---------------------------------------------------------------------------

#: 3.2.1 表1 测绘建图类「航线决策及规则」原文（按单元格换行切分）。
FLIGHT_RULES_SURVEY = (
    "强制匹配对应航线。",
    "开启等距/等时拍照，全局拍照模式优先。",
)
#: 3.2.2 表2 点行规则片段（整格原文按句读切分，供上游绑定）。
FLIGHT_RULES_POINT = (
    "多点串联：严格按用户顺序飞。",
    "到点默认悬停60秒，云台垂直向下；",
    "若指明环绕，则顺时针环绕1圈，云台朝向目标。",
)
#: 3.2.2 表2 点行「指明环绕」片段。
FLIGHT_RULES_ORBIT = (
    "若指明环绕，则顺时针环绕1圈，云台朝向目标。",
)
#: 3.2.2 表2 线行：允许航线 + 巡飞规则原文。
FLIGHT_RULES_LINE = (
    "航点航线（强制端点）",
    "单线单任务：就近选择线的一端开始，飞向另一端，不折返。",
)
#: 3.2.2 表2 面行：允许航线 + 巡飞规则原文。
FLIGHT_RULES_AREA = (
    "面状航线（正射全覆盖）",
    "单面单任务：自动生成“弓”字形航线，满铺扫描。",
)

#: 1.1 能力矩阵：飞行中控制阶段仅支持指点飞行/环绕（单目标）。
IN_FLIGHT_MAX_TARGETS = 1
IN_FLIGHT_ROUTE_TYPES = frozenset({ROUTE_WAYPOINT, ROUTE_ORBIT})


@dataclass(frozen=True)
class RouteDecision:
    """一次航线判定的不可变结果。

    * ``route_type``：允许执行时的航线类型（``ROUTE_*`` 常量之一）；拒绝、
      冲突待追问或未命中时为 ``None``。
    * ``rejection_code``：拒绝时的稳定错误码（``REJECTION_*`` 常量之一）；
      允许、冲突或未命中时为 ``None``。
    * ``message``：拒绝话术（规范原文）；允许/冲突/未命中时为空字符串。
    * ``geometry``：判定依据的静态标注几何类型（或仅拒绝标记）；未提供时 None。
    * ``matched_triggers``：命中的表1 触发词，按表1 出现顺序；拒绝时也可能
      带上「网格航线」等命中词。
    * ``trigger_category``：命中的表1 大类（测绘建图类 / 巡检巡查类）。
    * ``flight_rules``：需下游绑定的巡飞规则片段（3.2.1/3.2.2 原文切片）。
    * ``target_count``：目标数量（默认 1）；点可 >1，线/面 >1 会被拦截。
    * ``conflict`` / ``conflict_reason``：3.2.1 与 3.2.2 在规范未定义处冲突时，
      记录冲突事实与原因，``needs_clarification`` 为 True，由上游追问。
    """

    route_type: str | None = None
    geometry: str | None = None
    rejection_code: str | None = None
    message: str = ""
    matched_triggers: tuple[str, ...] = ()
    trigger_category: str | None = None
    flight_rules: tuple[str, ...] = ()
    target_count: int = 1
    conflict: bool = False
    conflict_reason: str = ""

    @property
    def accepted(self) -> bool:
        """是否给出了可执行的航线类型。"""
        return self.route_type is not None

    @property
    def rejected(self) -> bool:
        """是否命中规范里的强制拦截项。"""
        return self.rejection_code is not None

    @property
    def needs_clarification(self) -> bool:
        """既未命中允许规则、也未命中拦截规则，应由上游追问。"""
        return not self.accepted and not self.rejected


def _rejection(
    code: str,
    message: str,
    *,
    geometry: str | None = None,
    matched_triggers: tuple[str, ...] = (),
    target_count: int = 1,
) -> RouteDecision:
    return RouteDecision(
        route_type=None,
        geometry=geometry,
        rejection_code=code,
        message=message,
        matched_triggers=tuple(matched_triggers),
        target_count=target_count,
    )


def _validated_text(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be a non-empty string")
    return text.strip()


def _validated_geometry(geometry: str) -> str:
    if not isinstance(geometry, str) or geometry not in GEOMETRIES:
        raise ValueError(f"unknown geometry: {geometry!r}")
    return geometry


def _validated_target_count(target_count: int) -> int:
    if isinstance(target_count, bool) or not isinstance(target_count, int) or target_count < 1:
        raise ValueError("target_count must be a positive integer")
    return target_count


def _validated_orbit_requested(orbit_requested: bool | None) -> None:
    if orbit_requested is not None and not isinstance(orbit_requested, bool):
        raise ValueError("orbit_requested must be True, False or None")


def _validated_volume_variant(variant: str) -> str:
    if variant not in {"table_6", "table_2"}:
        raise ValueError("volume_message_variant must be 'table_6' or 'table_2'")
    return variant


def _flight_rules(route_type: str | None, geometry: str | None) -> tuple[str, ...]:
    if route_type in SURVEY_ROUTES:
        return FLIGHT_RULES_SURVEY
    if route_type == ROUTE_ORBIT:
        return FLIGHT_RULES_ORBIT
    if route_type == ROUTE_AREA:
        return FLIGHT_RULES_AREA
    if route_type == ROUTE_WAYPOINT:
        if geometry == GEOMETRY_POINT:
            return FLIGHT_RULES_POINT
        if geometry == GEOMETRY_LINE:
            return FLIGHT_RULES_LINE
    return ()


def _unnegated_mention(text: str, word: str) -> bool:
    """word 在 text 中至少出现一次且该次出现前 5 字内没有否定词。"""
    start = 0
    while True:
        index = text.find(word, start)
        if index < 0:
            return False
        prefix = text[max(0, index - _NEGATION_WINDOW):index]
        if not any(marker in prefix for marker in _NEGATION_MARKERS):
            return True
        start = index + len(word)


def _first_unnegated(text: str, words) -> str | None:
    for word in words:
        if _unnegated_mention(text, word):
            return word
    return None


def _match_triggers(text: str):
    survey = tuple(
        (word, route) for word, route in SURVEY_TRIGGER_ROUTES
        if _unnegated_mention(text, word)
    )
    patrol = tuple(
        (word, route) for word, route in PATROL_TRIGGER_ROUTES
        if _unnegated_mention(text, word)
    )
    return survey, patrol


def _unsupported_route_kind_rejection(
    text: str,
    *,
    geometry: str | None = None,
    target_count: int = 1,
) -> RouteDecision | None:
    """表1 注记 / 第五章表6 R6：整类不支持的航线，直接拒绝且不解析。"""
    grid_word = _first_unnegated(text, GRID_ROUTE_WORDS)
    if grid_word is not None:
        return _rejection(
            REJECTION_GRID_ROUTE, MESSAGE_REJECT_GRID,
            geometry=geometry, matched_triggers=(grid_word,), target_count=target_count,
        )
    body_word = _first_unnegated(text, BODY_ROUTE_WORDS)
    if body_word is not None:
        return _rejection(
            REJECTION_GEOMETRY_ROUTE, MESSAGE_REJECT_GEOMETRY_ROUTE,
            geometry=geometry, matched_triggers=(body_word,), target_count=target_count,
        )
    complex_word = _first_unnegated(text, COMPLEX_PATH_WORDS)
    if complex_word is not None:
        return _rejection(
            REJECTION_GRID_ROUTE, MESSAGE_REJECT_GRID,
            geometry=geometry, matched_triggers=(complex_word,), target_count=target_count,
        )
    return None


def _adapt_patrol_route(
    candidate: str,
    geometry: str,
    orbit_requested: bool | None,
) -> str:
    """3.2.1 巡检类「根据静态标注类型自适应」：收敛到表2 允许的航线。"""
    if geometry == GEOMETRY_POINT:
        wants_orbit = orbit_requested is True or (
            orbit_requested is None and candidate == ROUTE_ORBIT
        )
        return ROUTE_ORBIT if wants_orbit else ROUTE_WAYPOINT
    if geometry == GEOMETRY_LINE:
        return ROUTE_WAYPOINT
    if geometry == GEOMETRY_AREA:
        return ROUTE_AREA
    # 体/混合/多线/多面/网格/几何体已在调用方提前拒绝。
    return candidate


def _conflict_decision(
    route_type: str,
    geometry: str,
    target_count: int,
    matched_triggers: tuple[str, ...],
) -> RouteDecision:
    allowed = (
        f"{ROUTE_WAYPOINT} / {ROUTE_ORBIT}" if geometry == GEOMETRY_POINT else ROUTE_WAYPOINT
    )
    reason = (
        f"表1 测绘建图类触发词强制匹配{route_type}，但表2 对「{geometry}」只允许{allowed}；"
        "规范未定义这一冲突的取舍（产品待裁决的假设），本库不静默选边，返回待上游追问。"
    )
    return RouteDecision(
        route_type=None,
        geometry=geometry,
        matched_triggers=matched_triggers,
        trigger_category=TRIGGER_CATEGORY_SURVEY,
        target_count=target_count,
        conflict=True,
        conflict_reason=reason,
    )


def map_trigger_to_route(text: str) -> RouteDecision:
    """3.2.1 表1：只按文本触发词判定航线类型（不结合目标几何）。

    * 测绘建图类：正射影像/二维地图 → 正射航线；三维建模/倾斜摄影 → 倾斜摄影
      航线；全景图/360°全景 → 全景航线。
    * 巡检巡查类：沿河/沿路/沿线 → 航点航线；全域/全面/普通面状 → 面状航线；
      普查/快速扫描 → 面状航线；挨个检查 → 航点航线；绕一圈 → 环绕航线。
    * 表1 注记与第五章表6 R6：命中网格航线/几何体航线/螺旋飞时直接拒绝，
      ``route_type`` 为 ``None`` 并带原文 ``message``；被否定词修饰的出现跳过。
    * 文本未命中任何触发词时返回空 :class:`RouteDecision`（``needs_clarification``
      为 True），不臆造追问话术。

    无几何信息时「挨个检查」→航点、「绕一圈」→环绕是本库的解释性假设（见模块
    假设 8）；需要按标注类型二选一时请用 :func:`decide_route` 传入 geometry。
    """
    text = _validated_text(text)

    unsupported = _unsupported_route_kind_rejection(text)
    if unsupported is not None:
        return unsupported

    survey, patrol = _match_triggers(text)
    if survey:
        route_type = survey[0][1]
        return RouteDecision(
            route_type=route_type,
            matched_triggers=tuple(word for word, _ in survey),
            trigger_category=TRIGGER_CATEGORY_SURVEY,
            flight_rules=_flight_rules(route_type, None),
        )
    if patrol:
        route_type = patrol[0][1]
        return RouteDecision(
            route_type=route_type,
            matched_triggers=tuple(word for word, _ in patrol),
            trigger_category=TRIGGER_CATEGORY_PATROL,
            flight_rules=_flight_rules(route_type, None),
        )
    return RouteDecision()


def map_geometry_to_route(
    geometry: str,
    *,
    target_count: int = 1,
    orbit_requested: bool | None = None,
    volume_message_variant: str = "table_6",
) -> RouteDecision:
    """3.2.2 表2：按静态标注几何类型判定允许航线或强制拦截。

    * 点：航点航线（多点串联按用户顺序）或环绕航线（``orbit_requested=True``
      或上层已判定「指明环绕」）；到点悬停 60 秒、云台垂直向下，环绕则顺时
      针 1 圈、云台朝向目标。
    * 线：航点航线（强制端点，就近一端到另一端，不折返）；``target_count > 1``
      或几何类型为多线时拒绝，话术「暂不支持同时巡检多条线」。
    * 面：面状航线（「弓」字形满铺扫描）；``target_count > 1`` 或几何类型为
      多面时拒绝，话术「暂不支持同时巡检多个面」。
    * 体 / 混合类型 / 网格航线 / 几何体航线：直接拒绝并给规范原文话术。
      体行话术默认取第五章表6 完整版，``volume_message_variant='table_2'``
      可换取 3.2.2 表2 短版（两版都是规范原文）。
    * 未知几何类型属于调用契约错误，抛 ``ValueError``（不当作拦截话术）。

    ``geometry`` 槽位接受 3.2.2 表2 的标注几何（点/线/面/体/混合类型），也接受
    3.2.1 注记/数量条件产生的「仅拒绝」标记（多线/多面/网格航线/几何体航线）；
    后者不是标注几何，只是在统一拒绝入口上显式区分（见
    ``ANNOTATION_GEOMETRIES`` / ``REJECTION_ONLY_GEOMETRIES``）。
    """
    geometry = _validated_geometry(geometry)
    target_count = _validated_target_count(target_count)
    _validated_orbit_requested(orbit_requested)
    _validated_volume_variant(volume_message_variant)

    if geometry == GEOMETRY_VOLUME:
        message = (
            MESSAGE_REJECT_VOLUME_TABLE_2
            if volume_message_variant == "table_2"
            else MESSAGE_REJECT_VOLUME
        )
        return _rejection(REJECTION_VOLUME, message, geometry=geometry, target_count=target_count)
    if geometry == GEOMETRY_MIXED:
        return _rejection(REJECTION_MIXED, MESSAGE_REJECT_MIXED,
                          geometry=geometry, target_count=target_count)
    if geometry == GEOMETRY_MULTI_LINE:
        return _rejection(REJECTION_MULTI_LINE, MESSAGE_REJECT_MULTI_LINE,
                          geometry=geometry, target_count=target_count)
    if geometry == GEOMETRY_MULTI_AREA:
        return _rejection(REJECTION_MULTI_AREA, MESSAGE_REJECT_MULTI_AREA,
                          geometry=geometry, target_count=target_count)
    if geometry == GEOMETRY_GRID:
        return _rejection(REJECTION_GRID_ROUTE, MESSAGE_REJECT_GRID,
                          geometry=geometry, target_count=target_count)
    if geometry == GEOMETRY_BODY_ROUTE:
        return _rejection(REJECTION_GEOMETRY_ROUTE, MESSAGE_REJECT_GEOMETRY_ROUTE,
                          geometry=geometry, target_count=target_count)

    if geometry == GEOMETRY_LINE and target_count > 1:
        return _rejection(REJECTION_MULTI_LINE, MESSAGE_REJECT_MULTI_LINE,
                          geometry=geometry, target_count=target_count)
    if geometry == GEOMETRY_AREA and target_count > 1:
        return _rejection(REJECTION_MULTI_AREA, MESSAGE_REJECT_MULTI_AREA,
                          geometry=geometry, target_count=target_count)

    if geometry == GEOMETRY_POINT:
        route_type = ROUTE_ORBIT if orbit_requested is True else ROUTE_WAYPOINT
        return RouteDecision(
            route_type=route_type,
            geometry=geometry,
            flight_rules=_flight_rules(route_type, geometry),
            target_count=target_count,
        )
    if geometry == GEOMETRY_LINE:
        return RouteDecision(
            route_type=ROUTE_WAYPOINT,
            geometry=geometry,
            flight_rules=FLIGHT_RULES_LINE,
            target_count=target_count,
        )
    if geometry == GEOMETRY_AREA:
        return RouteDecision(
            route_type=ROUTE_AREA,
            geometry=geometry,
            flight_rules=FLIGHT_RULES_AREA,
            target_count=target_count,
        )
    raise ValueError(f"unsupported geometry: {geometry!r}")  # pragma: no cover - 已由集合校验覆盖


def decide_route(
    text: str,
    geometry: str | None = None,
    *,
    target_count: int = 1,
    orbit_requested: bool | None = None,
    volume_message_variant: str = "table_6",
) -> RouteDecision:
    """合并 3.2.1 表1 与 3.2.2 表2 的航线判定。

    优先级（规范未写明时本库的确定性取舍，见模块 docstring 假设 3/4/5）：

    1. 文本或几何类型命中表1 注记 / 表2 拦截项（网格航线、几何体航线、螺旋飞、
       体、混合类型、多线、多面）→ 直接拒绝，不解析其它信息；被否定的不支持词
       出现跳过（如「不要用网格航线」）。
    2. 几何类型为 线/面 且 ``target_count > 1`` → 多线/多面拦截。
    3. 表1 测绘建图类触发词命中：
       - 几何为「面」→ 采用表1 强制匹配的测绘航线（与表2 面状正射全覆盖语义一致）；
       - 几何为「点」或「线」→ 规范未定义冲突，返回 ``conflict=True`` 的待追问
         判定（不静默选边），由上游追问产品待裁决的取舍；
       - 未给几何 → 采用表1 强制匹配的测绘航线。
    4. 表1 巡检巡查类触发词命中 → 表1 原文「根据静态标注类型自适应」：点 → 航点
       （指明环绕则环绕）、线 → 航点、面 → 面状；未给几何类型时保留表1 直接航线。
    5. 仅给几何类型、无触发词 → 表2 默认航线（点/线 → 航点，面 → 面状）。
    6. 两者都没有 → 空判定，由上游追问。

    「指明环绕」= 调用方显式传 ``orbit_requested=True``，或文本含 3.2.2 点行
    的环绕措辞（``ORBIT_TRIGGER_WORDS``，如「环绕」「绕一圈」）。
    """
    text = _validated_text(text)
    _validated_target_count(target_count)
    _validated_orbit_requested(orbit_requested)
    _validated_volume_variant(volume_message_variant)
    if geometry is not None:
        geometry = _validated_geometry(geometry)

    if orbit_requested is None and any(word in text for word in ORBIT_TRIGGER_WORDS):
        orbit_requested = True

    unsupported = _unsupported_route_kind_rejection(
        text, geometry=geometry, target_count=target_count)
    if unsupported is not None:
        return unsupported

    geometry_decision: RouteDecision | None = None
    if geometry is not None:
        geometry_decision = map_geometry_to_route(
            geometry,
            target_count=target_count,
            orbit_requested=orbit_requested,
            volume_message_variant=volume_message_variant,
        )
        if geometry_decision.rejected:
            return geometry_decision

    survey, patrol = _match_triggers(text)
    if survey:
        route_type = survey[0][1]
        matched = tuple(word for word, _ in survey)
        if geometry in {GEOMETRY_POINT, GEOMETRY_LINE}:
            return _conflict_decision(route_type, geometry, target_count, matched)
        return RouteDecision(
            route_type=route_type,
            geometry=geometry,
            matched_triggers=matched,
            trigger_category=TRIGGER_CATEGORY_SURVEY,
            flight_rules=_flight_rules(route_type, geometry),
            target_count=target_count,
        )
    if patrol:
        candidate = patrol[0][1]
        if geometry is not None:
            route_type = _adapt_patrol_route(candidate, geometry, orbit_requested)
        else:
            route_type = candidate
        return RouteDecision(
            route_type=route_type,
            geometry=geometry,
            matched_triggers=tuple(word for word, _ in patrol),
            trigger_category=TRIGGER_CATEGORY_PATROL,
            flight_rules=_flight_rules(route_type, geometry),
            target_count=target_count,
        )
    if geometry_decision is not None:
        return geometry_decision
    return RouteDecision(target_count=target_count)


def in_flight_target_count_supported(target_count: int) -> bool:
    """1.1 能力矩阵：飞行中控制阶段仅支持单目标（指点飞行/环绕）。

    规范未给该限制的逐字拒绝话术，因此本函数只做布尔判界，不生成 message；
    需要结构化判定请用 :func:`decide_in_flight_route`。
    """
    target_count = _validated_target_count(target_count)
    return target_count <= IN_FLIGHT_MAX_TARGETS


def decide_in_flight_route(
    geometry: str,
    *,
    target_count: int = 1,
    orbit_requested: bool | None = None,
) -> RouteDecision:
    """1.1 能力矩阵：飞行中只支持单目标的指点飞行/环绕。

    * 点 + 单目标：航点航线（``orbit_requested=True`` 则环绕航线）；
    * 非点几何或多目标：``rejection_code=REJECTION_IN_FLIGHT_TARGET``，
      ``message`` 为空字符串——规范只给能力边界、未给逐字话术，上游必须走
      4.x 的拦截/追问流程，不得改写或复用本模块的地面规划话术。
    """
    geometry = _validated_geometry(geometry)
    target_count = _validated_target_count(target_count)
    _validated_orbit_requested(orbit_requested)

    if geometry != GEOMETRY_POINT or target_count > IN_FLIGHT_MAX_TARGETS:
        return RouteDecision(
            route_type=None,
            geometry=geometry,
            rejection_code=REJECTION_IN_FLIGHT_TARGET,
            message="",
            target_count=target_count,
        )
    route_type = ROUTE_ORBIT if orbit_requested is True else ROUTE_WAYPOINT
    return RouteDecision(
        route_type=route_type,
        geometry=geometry,
        flight_rules=_flight_rules(route_type, geometry),
        target_count=target_count,
    )
