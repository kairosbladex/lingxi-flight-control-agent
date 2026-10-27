"""V1.4 3.2.1 / 3.2.2：触发词 → 航线类型、目标几何 → 允许航线/拦截的纯规则库。

规范来源
========
``/Users/wangxiaolin/Desktop/灵犀平台自然语言智能控制功能设计规范V1.4.docx``：

* 3.2.1 表1「测绘与巡检任务触发词映射」
* 3.2.2 表2「目的地静态标注与航线强制映射」
* 5.4 表6「异常判断与反馈话术明细（测试用例库）」——本库用到的拒绝话术原文
* 1.1 能力矩阵——飞行中「仅支持指点飞行/环绕（单目标）」的能力边界

设计边界
========
* 纯函数、确定性、无外部依赖、不联网、不调平台、不做几何求解：不插值、
  不生成航点坐标、不判断禁飞区，只回答「该用哪种航线 / 是否必须拒绝」。
* 拒绝话术逐字保持规范原文（全角标点不改写）；3.2.2 表格单元格自带的中文
  引号在常量中已去掉，只保留句子本体。
* 规范未定义的行为不臆造话术：触发词完全未命中时返回 ``route_type=None``
  且 ``rejection_code=None``（``needs_clarification`` 为 True），由上游追问；
  ``geometry`` / ``target_count`` / ``orbit_requested`` 非法属于调用契约错误，
  直接 ``ValueError``。

规范未定义处的假设（均不影响逐字话术）
======================================
1. 表2 的体拦截话术在 3.2.2 为「当前版本不支持立体目标，请标注为点或面。」，
   5.4 表6 R8 为「当前版本不支持立体目标规划，请将其标注为点或面。」。
   本库对外 ``message`` 采用 5.4 完整版（任务要求逐字），同时保留
   ``MESSAGE_REJECT_VOLUME_TABLE_2`` 供按章节取用。
2. 3.2.2 只规定 体/混合/多线/多面 的拦截，未给「几何体航线」独立话术；表1
   注记「网格航线和几何体航线直接拒绝」，5.4 R6 给出统一话术，故几何体航线
   复用 ``MESSAGE_REJECT_GRID``（同属「拦截，不解析 / 手动接管」）。
3. 同一句文本命中多个表1 触发词时，按表1 出现顺序取第一个（确定性优先，
   不新增冲突追问话术）；测绘建图类优先于巡检巡查类。
4. 3.2.1 测绘建图类要求「强制匹配对应航线」，故它命中时优先于表2 的几何
   自适应；巡检巡查类则按「根据静态标注类型自适应」由几何类型决定航线
   （例如「绕一圈」+ 面 → 面状航线，「全域」+ 点 → 航点航线）。
5. 表2 点行用分号并列「悬停60秒/云台垂直向下」与「环绕1圈/云台朝向目标」；
   本库按航线类型二选一，不叠加（环绕航线不带「悬停60秒」规则）。
6. 表2 只在点行写明「多点串联」允许，故 ``target_count`` 的数量拦截只对
   线（>1 → 多线拦截）与面（>1 → 多面拦截）生效，点可大于 1。
"""
from __future__ import annotations

from dataclasses import dataclass

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
# 3.2.2 表2 第一列的标注类型（含表1 注记、表2 异常拦截覆盖的不支持类型）
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

#: 3.2.2 表2 允许自动规划的目标几何（点/线/面）。
SUPPORTED_GEOMETRIES = frozenset({GEOMETRY_POINT, GEOMETRY_LINE, GEOMETRY_AREA})
#: 3.2.2 表2 异常拦截列 / 3.2.1 表1 注记中必须直接拒绝的类型。
REJECTED_GEOMETRIES = frozenset({
    GEOMETRY_VOLUME,
    GEOMETRY_MIXED,
    GEOMETRY_MULTI_LINE,
    GEOMETRY_MULTI_AREA,
    GEOMETRY_GRID,
    GEOMETRY_BODY_ROUTE,
})
GEOMETRIES = SUPPORTED_GEOMETRIES | REJECTED_GEOMETRIES

# ---------------------------------------------------------------------------
# 稳定拒绝码：调用方按 code 分支，不解析 message 文案
# ---------------------------------------------------------------------------

REJECTION_VOLUME = "volume_unsupported"
REJECTION_MIXED = "mixed_targets_unsupported"
REJECTION_MULTI_LINE = "multi_line_unsupported"
REJECTION_MULTI_AREA = "multi_area_unsupported"
REJECTION_GRID_ROUTE = "grid_route_unsupported"
REJECTION_GEOMETRY_ROUTE = "geometry_route_unsupported"

# ---------------------------------------------------------------------------
# 拒绝话术：逐字取自规范原文
# ---------------------------------------------------------------------------

#: 3.2.2 表2 体行「异常拦截」原文。
MESSAGE_REJECT_VOLUME_TABLE_2 = "当前版本不支持立体目标，请标注为点或面。"
#: 5.4 表6 R8「期望系统返回」原文；对外话术采用此完整版。
MESSAGE_REJECT_VOLUME = "当前版本不支持立体目标规划，请将其标注为点或面。"
#: 3.2.2 表2 混合类型行「异常拦截」原文（去掉单元格自带的引号）。
MESSAGE_REJECT_MIXED = "单次任务暂不支持混合目标巡检，请分次下发。"
#: 3.2.2 表2 线行「异常拦截」原文。
MESSAGE_REJECT_MULTI_LINE = "暂不支持同时巡检多条线"
#: 3.2.2 表2 面行「异常拦截」原文。
MESSAGE_REJECT_MULTI_AREA = "暂不支持同时巡检多个面"
#: 5.4 表6 R6「期望系统返回」原文；网格航线直接拒绝。
MESSAGE_REJECT_GRID = "当前版本暂不支持自动规划复杂路径或网格航线，请手动接管。"
#: 表1 注记要求几何体航线直接拒绝；规范未给独立话术，复用统一话术（见模块假设 2）。
MESSAGE_REJECT_GEOMETRY_ROUTE = "当前版本暂不支持自动规划复杂路径或网格航线，请手动接管。"

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
#: 3.2.1 表1 注记 / 5.4 表6 R6：网格航线直接拒绝。
#: 只匹配航线措辞，避免把「网格化片区」这类正常地名/区域词误判为拦截。
GRID_ROUTE_WORDS = ("网格航线", "网格飞")
#: 3.2.1 表1 注记：几何体航线直接拒绝。
BODY_ROUTE_WORDS = ("几何体航线", "几何体")
#: 5.4 表6 R6 的「绕楼螺旋飞」同属不支持路径，给出同一句原文话术。
#: 用「螺旋飞」而非「螺旋」，避免把「检查螺旋桨」误判为不支持的航线。
COMPLEX_PATH_WORDS = ("螺旋飞",)

# ---------------------------------------------------------------------------
# 3.2.2 表2「巡飞规则（核心逻辑）」的机器可读拆分，供下游绑定执行细节
# ---------------------------------------------------------------------------

#: 3.2.1 表1 测绘建图类「航线决策及规则」原文。
FLIGHT_RULES_SURVEY = (
    "强制匹配对应航线",
    "开启等距/等时拍照",
    "全局拍照模式优先",
)
#: 3.2.2 表2 点行：默认航点航线规则。
FLIGHT_RULES_POINT = (
    "多点串联：严格按用户顺序飞",
    "到点默认悬停60秒",
    "云台垂直向下",
)
#: 3.2.2 表2 点行：指明环绕时的环绕航线规则。
FLIGHT_RULES_ORBIT = (
    "顺时针环绕1圈",
    "云台朝向目标",
)
#: 3.2.2 表2 线行：航点航线（强制端点）规则。
FLIGHT_RULES_LINE = (
    "单线单任务",
    "强制端点",
    "就近选择线的一端开始",
    "飞向另一端，不折返",
)
#: 3.2.2 表2 面行：面状航线（正射全覆盖）规则。
FLIGHT_RULES_AREA = (
    "单面单任务",
    "自动生成“弓”字形航线",
    "满铺扫描",
)

#: 1.1 能力矩阵：飞行中控制阶段仅支持指点飞行/环绕（单目标）。
IN_FLIGHT_MAX_TARGETS = 1
IN_FLIGHT_ROUTE_TYPES = frozenset({ROUTE_WAYPOINT, ROUTE_ORBIT})


@dataclass(frozen=True)
class RouteDecision:
    """一次航线判定的不可变结果。

    * ``route_type``：允许执行时的航线类型（``ROUTE_*`` 常量之一）；拒绝或
      未命中时为 ``None``。
    * ``rejection_code``：拒绝时的稳定错误码（``REJECTION_*`` 常量之一）；
      允许或未命中时为 ``None``。
    * ``message``：拒绝话术（规范原文）；允许或未命中时为空字符串。
    * ``geometry``：判定依据的静态标注几何类型；未提供时为 ``None``。
    * ``matched_triggers``：命中的表1 触发词，按表1 出现顺序；拒绝时也可能
      带上「网格航线」等命中词。
    * ``trigger_category``：命中的表1 大类（测绘建图类 / 巡检巡查类）。
    * ``flight_rules``：需下游绑定的巡飞规则（3.2.1/3.2.2 原文拆分）。
    """

    route_type: str | None = None
    geometry: str | None = None
    rejection_code: str | None = None
    message: str = ""
    matched_triggers: tuple[str, ...] = ()
    trigger_category: str | None = None
    flight_rules: tuple[str, ...] = ()

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
) -> RouteDecision:
    return RouteDecision(
        route_type=None,
        geometry=geometry,
        rejection_code=code,
        message=message,
        matched_triggers=tuple(matched_triggers),
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


def _match_triggers(text: str):
    survey = tuple((word, route) for word, route in SURVEY_TRIGGER_ROUTES if word in text)
    patrol = tuple((word, route) for word, route in PATROL_TRIGGER_ROUTES if word in text)
    return survey, patrol


def _unsupported_route_kind_rejection(text: str, geometry: str | None = None) -> RouteDecision | None:
    """表1 注记 / 5.4 表6 R6：整类不支持的航线，直接拒绝且不解析。"""
    for word in GRID_ROUTE_WORDS:
        if word in text:
            return _rejection(
                REJECTION_GRID_ROUTE, MESSAGE_REJECT_GRID,
                geometry=geometry, matched_triggers=(word,),
            )
    for word in BODY_ROUTE_WORDS:
        if word in text:
            return _rejection(
                REJECTION_GEOMETRY_ROUTE, MESSAGE_REJECT_GEOMETRY_ROUTE,
                geometry=geometry, matched_triggers=(word,),
            )
    for word in COMPLEX_PATH_WORDS:
        if word in text:
            return _rejection(
                REJECTION_GRID_ROUTE, MESSAGE_REJECT_GRID,
                geometry=geometry, matched_triggers=(word,),
            )
    return None


def _adapt_patrol_route(
    candidate: str,
    geometry: str,
    orbit_requested: bool | None,
) -> str:
    """3.2.1「根据静态标注类型自适应」：把表1 的直接航线收敛到表2 允许的航线。"""
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


def map_trigger_to_route(text: str) -> RouteDecision:
    """3.2.1 表1：只按文本触发词判定航线类型（不结合目标几何）。

    * 测绘建图类：正射影像/二维地图 → 正射航线；三维建模/倾斜摄影 → 倾斜摄影
      航线；全景图/360°全景 → 全景航线。
    * 巡检巡查类：沿河/沿路/沿线 → 航点航线；全域/全面/普通面状 → 面状航线；
      普查/快速扫描 → 面状航线；挨个检查 → 航点航线；绕一圈 → 环绕航线。
    * 表1 注记与 5.4 表6 R6：命中网格航线/几何体航线/螺旋时直接拒绝，
      ``route_type`` 为 ``None`` 并带原文 ``message``。
    * 文本未命中任何触发词时返回空 :class:`RouteDecision`（``needs_clarification``
      为 True），不臆造追问话术。
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
    * 未知几何类型属于调用契约错误，抛 ``ValueError``（不当作拦截话术）。
    """
    geometry = _validated_geometry(geometry)
    target_count = _validated_target_count(target_count)
    _validated_orbit_requested(orbit_requested)

    if geometry == GEOMETRY_VOLUME:
        return _rejection(REJECTION_VOLUME, MESSAGE_REJECT_VOLUME, geometry=geometry)
    if geometry == GEOMETRY_MIXED:
        return _rejection(REJECTION_MIXED, MESSAGE_REJECT_MIXED, geometry=geometry)
    if geometry == GEOMETRY_MULTI_LINE:
        return _rejection(REJECTION_MULTI_LINE, MESSAGE_REJECT_MULTI_LINE, geometry=geometry)
    if geometry == GEOMETRY_MULTI_AREA:
        return _rejection(REJECTION_MULTI_AREA, MESSAGE_REJECT_MULTI_AREA, geometry=geometry)
    if geometry == GEOMETRY_GRID:
        return _rejection(REJECTION_GRID_ROUTE, MESSAGE_REJECT_GRID, geometry=geometry)
    if geometry == GEOMETRY_BODY_ROUTE:
        return _rejection(REJECTION_GEOMETRY_ROUTE, MESSAGE_REJECT_GEOMETRY_ROUTE, geometry=geometry)

    if geometry == GEOMETRY_LINE and target_count > 1:
        return _rejection(REJECTION_MULTI_LINE, MESSAGE_REJECT_MULTI_LINE, geometry=geometry)
    if geometry == GEOMETRY_AREA and target_count > 1:
        return _rejection(REJECTION_MULTI_AREA, MESSAGE_REJECT_MULTI_AREA, geometry=geometry)

    if geometry == GEOMETRY_POINT:
        route_type = ROUTE_ORBIT if orbit_requested is True else ROUTE_WAYPOINT
        return RouteDecision(
            route_type=route_type,
            geometry=geometry,
            flight_rules=_flight_rules(route_type, geometry),
        )
    if geometry == GEOMETRY_LINE:
        return RouteDecision(
            route_type=ROUTE_WAYPOINT,
            geometry=geometry,
            flight_rules=FLIGHT_RULES_LINE,
        )
    if geometry == GEOMETRY_AREA:
        return RouteDecision(
            route_type=ROUTE_AREA,
            geometry=geometry,
            flight_rules=FLIGHT_RULES_AREA,
        )
    raise ValueError(f"unsupported geometry: {geometry!r}")  # pragma: no cover - 已由集合校验覆盖


def decide_route(
    text: str,
    geometry: str | None = None,
    *,
    target_count: int = 1,
    orbit_requested: bool | None = None,
) -> RouteDecision:
    """合并 3.2.1 表1 与 3.2.2 表2 的航线判定。

    优先级（规范未写明时本库的确定性取舍，见模块 docstring 假设 3/4）：

    1. 文本或几何类型命中表1 注记 / 表2 拦截项（网格航线、几何体航线、螺旋、
       体、混合类型、多线、多面）→ 直接拒绝，不解析其它信息。
    2. 几何类型为 线/面 且 ``target_count > 1`` → 多线/多面拦截。
    3. 表1 测绘建图类触发词命中 → 采用表1 强制匹配的测绘航线（表1 明写
       「强制匹配对应航线」）。
    4. 表1 巡检巡查类触发词命中 → 表2 几何类型自适应：点 → 航点（指明环绕则
       环绕）、线 → 航点、面 → 面状；未给几何类型时保留表1 直接航线。
    5. 仅给几何类型、无触发词 → 表2 默认航线（点/线 → 航点，面 → 面状）。
    6. 两者都没有 → 空判定，由上游追问。

    「指明环绕」= 调用方显式传 ``orbit_requested=True``，或文本含 3.2.2 点行
    的环绕措辞（``ORBIT_TRIGGER_WORDS``，如「环绕」「绕一圈」）。
    """
    text = _validated_text(text)
    _validated_target_count(target_count)
    _validated_orbit_requested(orbit_requested)
    if geometry is not None:
        geometry = _validated_geometry(geometry)

    if orbit_requested is None and any(word in text for word in ORBIT_TRIGGER_WORDS):
        orbit_requested = True

    unsupported = _unsupported_route_kind_rejection(text, geometry=geometry)
    if unsupported is not None:
        return unsupported

    geometry_decision: RouteDecision | None = None
    if geometry is not None:
        geometry_decision = map_geometry_to_route(
            geometry,
            target_count=target_count,
            orbit_requested=orbit_requested,
        )
        if geometry_decision.rejected:
            return geometry_decision

    survey, patrol = _match_triggers(text)
    if survey:
        route_type = survey[0][1]
        return RouteDecision(
            route_type=route_type,
            geometry=geometry,
            matched_triggers=tuple(word for word, _ in survey),
            trigger_category=TRIGGER_CATEGORY_SURVEY,
            flight_rules=_flight_rules(route_type, geometry),
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
        )
    if geometry_decision is not None:
        return geometry_decision
    return RouteDecision()


def in_flight_target_count_supported(target_count: int) -> bool:
    """1.1 能力矩阵：飞行中控制阶段仅支持单目标（指点飞行/环绕）。

    规范未给该限制的逐字拒绝话术，因此本函数只做布尔判界，不生成 message；
    上层若不支持，必须走 4.x 的拦截/追问流程，不得复用本模块的
    地面规划拒绝话术。
    """
    target_count = _validated_target_count(target_count)
    return target_count <= IN_FLIGHT_MAX_TARGETS
