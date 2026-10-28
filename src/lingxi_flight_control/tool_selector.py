"""工具检索层：按用户意图缩小下发给模型的工具面，但**绝不减少能力**。

59 个工具的契约全量下发要占 3213~3525 tokens（实测，见 `docs/model-selection.md`）。
本模块用确定性规则挑候选子集，把这份固定开销降下来。

设计的第一原则是**失败方向不对称**：多给一个工具只是多花 token；少给一个工具会让
模型结构上无法完成任务（它不可能调用一个没下发的工具）。因此：

- 关键词**一个领域都没命中就回退全量**（含空消息、含糊消息、「继续」这类接话）。
- **安全底线**：`read_state`（唯一只读工具）与 `emergency_stop`/`return_home`/`land`
  三个安全动作在任何消息下都保留——检索层不允许减少安全能力。
- **前置动作**：驾驶舱要求先持有控制权才能改变状态，所以任何改变状态的动作被选中时，
  必须把 `control_acquire` 一起带上，否则模型无法完成多步任务。
- **已经用过的工具**（已执行 / 待确认 / 已回放的观察）永远保留，续跑不会因为消息里
  没再提它而丢掉工具。
- 检索层自己出异常时，`OllamaDecider` 退化成全量下发（见 `ollama_decider.py`），
  只记 `selector_errors`，绝不让工具变少或让 run 失败。

本模块只决定「下发哪些工具」，不碰 `ToolRegistry` 的校验与 fail-closed，也不参与安全快路径
（急停/返航/降落在 `SafetyFastPathDecider` 里就被短路了，根本到不了检索层）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

from .models import AgentCheckpoint, ToolDefinition

#: 候选面的两种模式：``full`` = 全量下发，``retrieved`` = 命中领域后的子集。
SELECTION_MODES = ("full", "retrieved")

#: 领域词表：消息命中任一关键词，该领域整体进入候选。
#: 用子串匹配是刻意的——这里的误判只会**多给**工具（安全方向），
#: 与安全快路径必须精确匹配的理由正好相反。
DOMAIN_KEYWORDS: Mapping[str, tuple[str, ...]] = {
    "control": (
        "控制权", "接管", "操控", "释放", "远端", "远程", "键盘", "摇杆",
        "虚拟按键", "操作模式", "手动模式",
    ),
    "flight": (
        "飞", "前进", "向前", "后退", "向后", "平移", "左移", "右移", "向左", "向右",
        "上升", "下降", "转向", "左转", "右转", "偏航转向", "起飞", "降落", "备降",
        "返航", "急停", "紧急停止", "紧急制动", "紧急刹车", "悬停", "刹停", "停止所有",
    ),
    "route": (
        "航线", "航点", "断点", "盘旋", "绕点", "上传", "执行航线", "暂停", "恢复航线",
        "经度", "纬度", "坐标", "半径", "测绘",
    ),
    "parameters": ("参数", "速度", "角速度", "默认"),
    "camera": (
        "相机", "镜头", "拍照", "照片", "拍摄", "录像", "变焦", "云台", "对焦", "全景",
        "广角", "可见光", "红外", "放大", "回中", "复位", "俯仰", "向上", "向下",
    ),
    "infrared": ("红外", "测温", "热成像", "温度"),
    "payload": ("载荷", "喊话", "探照灯", "甲烷", "投放", "挂载", "物资"),
    "state": ("状态", "遥测", "读取", "当前", "电量"),
}

#: 候选面下限：低于它就回退全量。命中的领域太少说明消息只碰到了弱关键词，
#: 而过窄的工具面不只是「少给」——它会**诱导模型去调用不匹配的动作**
#: （用户说「位置调整」，候选里只剩下安全动作时会更容易误调到 land）。
#: 全量下发最多花 3.5k tokens，这个代价比调错动作小得多。
SELECTION_MIN_CANDIDATES = 8

#: 任何消息都保留的工具：读状态 + 三个安全动作。检索层不得减少安全能力。
ALWAYS_INCLUDED: tuple[str, ...] = ("read_state", "emergency_stop", "return_home", "land")

#: 前置动作：领域 -> 该领域必须一起下发的工具。
#: 驾驶舱状态机要求先持有控制权（`_CONTROL_FREE_ACTIONS` 之外的动作都会被拒），
#: 少了 control_acquire，模型能看见动作却无法合法执行。
PREREQUISITES: Mapping[str, tuple[str, ...]] = {
    "flight": ("control_acquire",),
    "route": ("control_acquire",),
    "parameters": ("control_acquire",),
    "camera": ("control_acquire",),
    "infrared": ("control_acquire",),
    "payload": ("control_acquire",),
}


@dataclass(frozen=True)
class SelectionResult:
    """一次检索的结果：候选工具名（保持输入顺序）+ 模式 + 命中的领域。"""

    names: tuple[str, ...]
    mode: str
    domains: tuple[str, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        if self.mode not in SELECTION_MODES:
            raise ValueError(f"mode must be one of {list(SELECTION_MODES)}, got {self.mode!r}")

    def __contains__(self, name: Any) -> bool:
        return name in self.names

    def __len__(self) -> int:
        return len(self.names)


class ToolSelector(Protocol):  # pragma: no cover - 结构声明
    def select(self, user_message: str, definitions: Sequence[Any], *,
               checkpoint: AgentCheckpoint | None = None) -> SelectionResult: ...


@lru_cache(maxsize=256)
def default_domain_of(name: str) -> str:
    """工具名 -> 驾驶舱领域（来自 `ACTION_SPECS` 单一真源）。

    非驾驶舱工具或不可识别的名字返回空字符串：它不参与领域匹配，
    但也不会因此被静默丢掉——关键词或回退全量仍会覆盖它。
    """
    try:
        from .full_control import ACTION_SPECS, FlightControlAction
    except ImportError:  # pragma: no cover - 循环/裁剪安装时保守退让
        return ""
    try:
        action = FlightControlAction(name)
    except (TypeError, ValueError):
        from .java_tools import TOOLS
        return TOOLS[name].domain if name in TOOLS else ""
    spec = ACTION_SPECS.get(action)
    return "" if spec is None else spec.domain


def tool_name(definition: Any) -> str:
    """从任意工具定义形状里取出名字（`ToolDefinition` / dict / (name, description)）。"""
    if isinstance(definition, ToolDefinition):
        return definition.name
    if isinstance(definition, Mapping):
        return str(definition.get("name") or "")
    if isinstance(definition, (tuple, list)) and len(definition) >= 2:
        return str(definition[0])
    raise TypeError(f"unsupported tool definition shape: {type(definition).__name__}")


def tool_description(definition: Any) -> str:
    if isinstance(definition, ToolDefinition):
        return definition.description
    if isinstance(definition, Mapping):
        return str(definition.get("description") or "")
    if isinstance(definition, (tuple, list)) and len(definition) >= 2:
        return str(definition[1])
    raise TypeError(f"unsupported tool definition shape: {type(definition).__name__}")


class RuleBasedToolSelector:
    """确定性领域规则检索；不依赖模型、不依赖 tokenizer、可完全复现。"""

    def __init__(self, *, keywords: Mapping[str, Sequence[str]] | None = None,
                 always_include: Sequence[str] | None = None,
                 prerequisites: Mapping[str, Sequence[str]] | None = None,
                 min_candidates: int = SELECTION_MIN_CANDIDATES,
                 domain_of=None):
        if isinstance(min_candidates, bool) or not isinstance(min_candidates, int) \
                or min_candidates < 1:
            raise ValueError("min_candidates must be a positive integer")
        table = DOMAIN_KEYWORDS if keywords is None else keywords
        self._keywords: dict[str, tuple[str, ...]] = {}
        for domain, words in table.items():
            cleaned = tuple(word for word in words if isinstance(word, str) and word)
            if not cleaned:
                raise ValueError(f"domain {domain!r} must have at least one keyword")
            self._keywords[str(domain)] = cleaned
        self._always_include = tuple(
            ALWAYS_INCLUDED if always_include is None else always_include)
        self._min_candidates = int(min_candidates)
        prereq_table = PREREQUISITES if prerequisites is None else prerequisites
        self._prerequisites: dict[str, tuple[str, ...]] = {
            str(domain): tuple(names) for domain, names in prereq_table.items()}
        self._domain_of = domain_of or default_domain_of

    @property
    def keywords(self) -> Mapping[str, tuple[str, ...]]:
        return dict(self._keywords)

    @property
    def min_candidates(self) -> int:
        return self._min_candidates

    @property
    def always_include(self) -> tuple[str, ...]:
        return self._always_include

    def select(self, user_message: str, definitions: Sequence[Any], *,
               checkpoint: AgentCheckpoint | None = None) -> SelectionResult:
        """挑候选工具；任何不确定都回退全量（``mode="full"``）。"""
        try:
            names = tuple(tool_name(item) for item in definitions)
            described = tuple((tool_name(item), tool_description(item)) for item in definitions)
        except TypeError:
            # 认不出的定义形状：原样全量下发，绝不猜着删。
            return SelectionResult(tuple(_safe_names(definitions)), "full", (),
                                   "unrecognized tool definition shape")
        if not names:
            return SelectionResult((), "full", (), "no definitions")
        if any(not name for name in names):
            return SelectionResult(names, "full", (), "tool without a name")

        domains, words = self._match(user_message)
        if not domains:
            # 关键词一个领域都没命中（空消息、含糊消息、「继续」「好的」）：回退全量。
            return SelectionResult(names, "full", (), "no domain keyword matched")

        selected: list[str] = []
        for name, description in described:
            haystack = f"{name} {description}"
            if self._domain_of(name) in domains or any(word in haystack for word in words):
                selected.append(name)
        # 前置动作：改变状态就得先能拿到控制权。
        for domain in domains:
            for required in self._prerequisites.get(domain, ()):
                if required in names:
                    selected.append(required)
        # 已执行 / 待确认 / 已回放的观察：续跑不能因为消息里没再提它就丢掉工具。
        for used in self._used_names(checkpoint):
            if used in names:
                selected.append(used)
        # 安全底线。
        selected.extend(self._always_include)

        keep = set(selected)
        ordered = tuple(name for name in names if name in keep)
        if len(ordered) == len(names):
            return SelectionResult(names, "full", tuple(domains), "every tool matched")
        if len(ordered) < self._min_candidates:
            # 候选面太窄：只碰到了弱关键词，宁可全量。
            return SelectionResult(names, "full", tuple(domains),
                                   "candidate set below min_candidates")
        return SelectionResult(ordered, "retrieved", tuple(domains), "domain keyword matched")

    def _match(self, user_message: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        text = str(user_message or "")
        if not text.strip():
            return (), ()
        domains = tuple(domain for domain, words in self._keywords.items()
                        if any(word in text for word in words))
        words = tuple(dict.fromkeys(word for domain in domains
                                   for word in self._keywords[domain]))
        return domains, words

    @staticmethod
    def _used_names(checkpoint: AgentCheckpoint | None) -> tuple[str, ...]:
        if checkpoint is None:
            return ()
        used: list[str] = []
        for entry in (checkpoint.executed_tool_calls or []):
            if isinstance(entry, Mapping) and isinstance(entry.get("name"), str):
                used.append(entry["name"])
        for entry in (checkpoint.tool_calls or []):
            if isinstance(entry, Mapping) and isinstance(entry.get("name"), str):
                used.append(entry["name"])
        for entry in (checkpoint.observations or []):
            if isinstance(entry, Mapping) and isinstance(entry.get("tool"), str):
                used.append(entry["tool"])
        return tuple(dict.fromkeys(used))


def _safe_names(definitions: Sequence[Any]) -> tuple[str, ...]:
    names: list[str] = []
    for item in definitions:
        try:
            names.append(tool_name(item))
        except TypeError:
            continue
    return tuple(names)
