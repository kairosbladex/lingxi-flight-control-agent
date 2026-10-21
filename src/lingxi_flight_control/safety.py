"""安全快路径：把急停这类安全动作从模型回路里摘出来，走确定性短路。

为什么需要它：

- **延迟**。走 LLM 工具调用，光 59 个工具的 schema 预填充就要 0.9~4 秒，加解码约 2.5~5 秒
  才能发出「紧急停止」。无人机等不起。
- **可用性**。模型服务不可达时，整条 Agent 链路就死了——包括急停。安全动作不能有这种依赖。

设计取舍：**只做精确匹配，绝不做子串包含。** 子串匹配在中文否定句上会出人命：
「不要返航」包含「返航」，「别降落」包含「降落」。实测旧的正则路径在 16 条否定/疑问说法里
误触发 9 条（见 docs/safety-fast-path.md）。识别不确定时一律交回模型，宁可慢也不能错。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .models import DecisionKind, ModelDecision, ToolCall, ToolDefinition

#: 归一化时要剔除的标点，避免「急停！」「急停。」匹配不上。
_PUNCTUATION = "。．.！!？?~～、,，;；:：\"'“”‘’()（）【】[]　"

DEFAULT_SAFETY_PHRASES: Mapping[str, tuple[str, ...]] = {
    "emergency_stop": (
        "急停", "紧急停止", "紧急制动", "紧急刹车", "一键急停",
        "马上停", "立马停", "马上停止", "立即停止", "立刻停止",
        "停止所有动作", "全部停止", "所有动作停止", "刹停",
    ),
    "return_home": (
        "返航", "一键返航", "自动返航", "立即返航", "马上返航", "开始返航", "执行返航", "回来吧",
    ),
    "cancel_return_home": (
        "取消返航", "停止返航", "终止返航", "取消自动返航", "取消一键返航",
    ),
    "land": (
        "降落", "一键降落", "立即降落", "马上降落", "开始降落", "执行降落", "原地降落",
    ),
}

#: 不在快路径里的动作及其原因。写下来是为了让「为什么没有它」这件事可查，
#: 而不是等谁加进去之后再靠测试发现。
EXCLUDED_ACTIONS: Mapping[str, str] = {
    "takeoff": "起飞需要高度参数，快路径只能给出空参数；替用户猜一个起飞高度是危险的，"
               "而且起飞是深思熟虑的规划动作，不是时间敏感的安全动作。它交给模型处理。",
}


def normalize(text: Any) -> str:
    """归一化：去标点、去空白、转小写。只用于精确比对。"""
    cleaned = str(text or "")
    for character in _PUNCTUATION:
        cleaned = cleaned.replace(character, "")
    return "".join(cleaned.split()).lower()


class SafetyFastPath:
    """把一句话确定为安全动作，或明确地不确定。

    返回 `None` 表示「不是已知的安全动作」，调用方应继续走模型，而不是猜测。
    """

    def __init__(self, phrases: Mapping[str, Sequence[str]] | None = None):
        table = DEFAULT_SAFETY_PHRASES if phrases is None else phrases
        self._by_phrase: dict[str, str] = {}
        for name, phrase_list in table.items():
            if not isinstance(name, str) or not name:
                raise ValueError("safety action name must be a non-empty string")
            if not phrase_list:
                raise ValueError(f"safety action {name} has no phrases")
            for phrase in phrase_list:
                key = normalize(phrase)
                if not key:
                    raise ValueError(f"safety action {name} has an empty phrase")
                existing = self._by_phrase.get(key)
                if existing is not None and existing != name:
                    # 同一个说法映射到两个动作，必须由调用方消歧，不能靠顺序碰运气。
                    raise ValueError(f"phrase {phrase!r} maps to both {existing} and {name}")
                self._by_phrase[key] = name
        self._actions = frozenset(table)

    @property
    def actions(self) -> frozenset[str]:
        return self._actions

    @property
    def phrases(self) -> Mapping[str, str]:
        return dict(self._by_phrase)

    def recognize(self, text: Any) -> ToolCall | None:
        """精确匹配。不是已知安全动作时返回 None。"""
        name = self._by_phrase.get(normalize(text))
        if name is None:
            return None
        return ToolCall(name, {})

    __call__ = recognize


class SafetyFastPathDecider:
    """`ModelDecider` 装饰器：安全动作不发模型调用，其余原样转发。

    安全动作完成后不再回问模型，直接给出确定性收尾语，因此整条安全链路
    完全不依赖模型服务是否可用。
    """

    def __init__(self, inner, fast_path: SafetyFastPath | None = None, *,
                 closing_message: str = "安全指令 {action} 已下发，结果见工具回执。"):
        self._inner = inner
        self._fast_path = fast_path or SafetyFastPath()
        if "{action}" not in closing_message:
            raise ValueError("closing_message must contain {action}")
        self._closing_message = closing_message
        self.safety_hits = 0
        self.fallbacks = 0

    def decide(
        self, user_message: str, checkpoint, tools: tuple[ToolDefinition, ...]
    ) -> ModelDecision:
        call = self._fast_path.recognize(user_message)
        if call is None:
            self.fallbacks += 1
            return self._inner.decide(user_message, checkpoint, tools)

        if call.name not in {definition.name for definition in tools}:
            # 动作在工具面之外（例如只挂了最小执行器），交回模型而不是凭空失败。
            self.fallbacks += 1
            return self._inner.decide(user_message, checkpoint, tools)

        executed = {entry.get("name") for entry in (checkpoint.executed_tool_calls or [])}
        if call.name in executed:
            # 已经执行过，收尾即可；再发一次会无限重复执行。
            self.safety_hits += 1
            return ModelDecision(
                DecisionKind.FINAL,
                message=self._closing_message.format(action=call.name),
            )

        self.safety_hits += 1
        return ModelDecision(DecisionKind.TOOL_CALL, tool_call=call)
