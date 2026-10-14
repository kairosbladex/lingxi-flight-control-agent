"""把 238 GPU 服务器上的模型接成飞控 Agent 的决策大脑。

本模块是 `ModelDecider` 的一个真实实现：把 `ToolRegistry` 的工具契约下发给模型，
再把模型的输出收敛回受约束的 `ModelDecision`。它只做协议转换，不含任何业务判断；
安全边界仍然全部由 `BoundedAgentLoop` 和 `ToolRegistry` 持有。

设计取舍：
- 只支持原生 function calling。依赖正则从自由文本里抠参数会让模型输出绕过 schema 校验。
- 模型返回 tool_call 时丢弃同轮的散文，保证 `ModelDecision` 的互斥契约成立。
- 模型什么都没返回时抛错而不是猜，交由循环 fail-closed 处理。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol
import urllib.error
import urllib.request

from .models import (
    AgentCheckpoint,
    DecisionKind,
    ModelDecision,
    ToolCall,
    ToolDefinition,
    parameter_json_schema,
)

DEFAULT_BASE_URL = "http://192.168.130.238:11434"
DEFAULT_MODEL = "qwen3.5:9b"

#: 提示词要显式写清多步纪律。实测省略这句时，9b 在多动作请求上会只做第一步就收尾。
DEFAULT_SYSTEM_PROMPT = """你是灵犀无人机飞控执行 Agent。你负责理解用户意图，并调用工具操作无人机。

硬性约束：
1. 你只能通过工具操作无人机。执行结论只能来自工具返回，不能由你自己断言。
2. 回复中禁止出现「执行成功」「已成功执行」「successfully executed」这类断言性表述——
   它们会被安全护栏判为伪造执行结论。需要总结时，只客观复述工具回执里的状态与数据。
3. 用户一次提出多个动作时，必须依次全部完成：每次只调用一个工具，拿到结果后继续下一个，全部完成后才给出最终答复。
4. 参数超出工具允许范围时不要猜测或截断，直接向用户说明并请求澄清。
5. 需求有歧义（例如没说清测绘方式、目标点、高度）时先提问澄清，不要自行假定。
6. 绝不输出任何凭据、密码、token、连接信息或飞控厂商报文。
7. 严格按工具声明的参数填写，不要添加未声明的字段，也不要臆造参数值。
8. 用中文回复，简洁、只陈述已确认的事实和下一步动作。
"""


class Transport(Protocol):  # pragma: no cover - 结构声明
    def __call__(self, url: str, payload: Mapping[str, Any], timeout: float) -> Mapping[str, Any]: ...


_CLARIFY_MARKERS = (
    "请明确", "请确认", "请问", "需要您", "请您", "无法确定", "不确定",
    "请提供", "请说明", "澄清", "哪种",
)


def tool_payload(definition: ToolDefinition) -> dict[str, Any]:
    """把单个 `ToolDefinition` 翻成 Ollama/OpenAI 兼容的 function 声明。"""
    schema = definition.schema
    functions = {
        "name": definition.name,
        "description": definition.description,
        "parameters": {
            "type": "object",
            "properties": {
                name: parameter_json_schema(parameter) for name, parameter in schema.properties.items()
            },
            "required": list(schema.required),
            "additionalProperties": bool(schema.additional_properties),
        },
    }
    return {"type": "function", "function": functions}


def build_tools_payload(definitions: Sequence[ToolDefinition]) -> list[dict[str, Any]]:
    """下发工具集合。`definitions()` 已剥离 handler，这里只传契约。"""
    payload: list[dict[str, Any]] = []
    for definition in definitions:
        if not isinstance(definition, ToolDefinition):
            raise ValueError("tool definitions must be ToolDefinition instances")
        if definition.schema is None:
            raise ValueError(f"tool {definition.name} has no schema")
        payload.append(tool_payload(definition))
    return payload


def http_transport(url: str, payload: Mapping[str, Any], timeout: float) -> Mapping[str, Any]:
    """默认传输层：POST 一个 JSON 对象并解析 JSON 响应。"""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:  # 4xx/5xx 带上响应体，便于定位模型模板问题
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:400]
        except Exception:  # pragma: no cover - 读取失败时保持原错误
            detail = ""
        raise ValueError(f"model server rejected the request: HTTP {exc.code} {detail}") from exc
    except TimeoutError as exc:
        # 冷启动加载模型可能需要数分钟；报错要能区分「加载慢」和「服务挂了」。
        raise ValueError(
            f"model server did not respond within {timeout:.0f}s "
            "(a cold model load can exceed this; raise timeout or set keep_alive to keep it resident)"
        ) from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise ValueError(
                f"model server did not respond within {timeout:.0f}s "
                "(a cold model load can exceed this; raise timeout or set keep_alive to keep it resident)"
            ) from exc
        raise ValueError(f"model server is unreachable: {exc.reason}") from exc
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("model server returned invalid JSON") from exc
    if not isinstance(data, Mapping):
        raise ValueError("model server returned a non-object response")
    return data


def _coerce_arguments(raw: Any) -> dict[str, Any]:
    """Ollama 有时把 arguments 作为 JSON 字符串返回，两种形态都要接受。"""
    if raw is None:
        return {}
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return {}
        try:
            raw = json.loads(stripped)
        except (TypeError, ValueError) as exc:
            raise ValueError("tool call arguments are not valid JSON") from exc
    if not isinstance(raw, Mapping):
        raise ValueError("tool call arguments must be an object")
    return dict(raw)


def _looks_like_clarification(content: str) -> bool:
    """区分「提问澄清」和「最终答复」。

    `ModelDecision` 的契约里模型只能给文本或工具调用，没有显式的 clarify 信号，
    所以这里用措辞启发式判断；调用方可用 `clarify_detector` 覆盖。
    """
    text = content.strip()
    if not text:
        return False
    if any(marker in text for marker in _CLARIFY_MARKERS):
        return True
    # 以问句收尾且不长，通常是在向用户要信息而不是汇报结论。
    is_question = text.rstrip().endswith(("？", "?"))
    asks_second_person = any(word in text for word in ("您", "你"))
    return is_question and asks_second_person and len(text) <= 200


class OllamaDecider:
    """调用 238 上 Ollama 托管的模型完成一次受限决策。"""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        base_url: str = DEFAULT_BASE_URL,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        temperature: float = 0.0,
        timeout: float = 180.0,
        think: bool | None = None,
        num_ctx: int | None = 8192,
        keep_alive: str | None = None,
        max_attempts: int = 2,
        transport: Transport | None = None,
        clarify_detector: Callable[[str], bool] | None = None,
    ):
        if not model or not model.strip():
            raise ValueError("model name is required")
        if not base_url or not base_url.strip():
            raise ValueError("base_url is required")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if num_ctx is not None and num_ctx <= 0:
            raise ValueError("num_ctx must be positive")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self.model = model
        self._endpoint = base_url.rstrip("/") + "/api/chat"
        self._system_prompt = system_prompt
        self._temperature = float(temperature)
        self._timeout = float(timeout)
        # 默认不发送 think 字段。实测对 qwen3.5:9b + Ollama 0.21.0 显式传 think=False
        # 会让工具调用完全失效（返回空响应）；省略则思考照常开启且工具调用正常。
        self._think = think
        self._num_ctx = num_ctx
        self._keep_alive = keep_alive
        self._max_attempts = int(max_attempts)
        self._transport = transport or http_transport
        self._clarify_detector = clarify_detector or _looks_like_clarification
        self.last_raw_response: Mapping[str, Any] | None = None
        self.empty_responses = 0

    # ---- 协议转换 ----------------------------------------------------------

    def messages(
        self, user_message: str, checkpoint: AgentCheckpoint
    ) -> list[dict[str, Any]]:
        """把检查点重建成原生对话。

        回放依据 `observations`：现在每条观察都自带工具名与参数，所以不需要再去对齐
        另一个列表。这一点很关键——确认轮次会让 `tool_calls` 重复记录，校验失败的尝试
        又只出现在 `observations` 里，靠两个列表 zip 一定错位。
        校验失败的尝试也要回放，模型才知道自己上一轮错在哪。
        """
        messages: list[dict[str, Any]] = [{"role": "system", "content": self._system_prompt}]
        messages.append({"role": "user", "content": user_message})
        for observation in checkpoint.observations:
            name = observation.get("tool")
            if not isinstance(name, str) or not name:
                continue
            arguments = observation.get("arguments")
            messages.append({
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {
                    "name": name,
                    "arguments": dict(arguments) if isinstance(arguments, Mapping) else {},
                }}],
            })
            if "result" in observation:
                payload: Any = observation["result"]
            else:
                payload = observation.get("error", {})
            messages.append({
                "role": "tool",
                "content": json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
            })
        return messages

    def payload(
        self, user_message: str, checkpoint: AgentCheckpoint, definitions: Sequence[ToolDefinition]
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": self.messages(user_message, checkpoint),
            "stream": False,
            "tools": build_tools_payload(definitions),
            "options": {"temperature": self._temperature},
        }
        if self._num_ctx is not None:
            body["options"]["num_ctx"] = self._num_ctx
        if self._think is not None:
            body["think"] = bool(self._think)
        if self._keep_alive is not None:
            body["keep_alive"] = self._keep_alive
        return body

    # ---- 决策 --------------------------------------------------------------

    def decide(
        self, user_message: str, checkpoint: AgentCheckpoint, tools: tuple[ToolDefinition, ...]
    ) -> ModelDecision:
        """向模型要一次决策，空响应时有界重试。

        重试是安全的：空响应意味着模型没有给出任何工具调用，也就没有任何动作被执行，
        因此重试的只是「决策」本身，不会重复执行副作用。非空但结构非法的响应不重试，
        那是协议问题，应该直接暴露。
        """
        last_message = ""
        for attempt in range(1, self._max_attempts + 1):
            body = self.payload(user_message, checkpoint, tools)
            response = self._transport(self._endpoint, body, self._timeout)
            self.last_raw_response = response
            # 错误信封在协议层解释，自定义传输层同样受保护。
            if isinstance(response.get("error"), str) and response["error"]:
                raise ValueError(f"model server error: {response['error']}")
            decision, empty_reason = self._interpret(response)
            if decision is not None:
                return decision
            last_message = empty_reason
            self.empty_responses += 1
            if attempt < self._max_attempts:
                continue
        raise ValueError(
            f"model returned an unusable response after {self._max_attempts} attempts: {last_message}")

    def _interpret(self, response: Mapping[str, Any]) -> tuple[ModelDecision | None, str]:
        """把一次响应解释成决策；响应为空时返回 (None, 原因)。"""
        message = response.get("message")
        if not isinstance(message, Mapping):
            raise ValueError("model response has no message object")

        raw_calls = message.get("tool_calls") or []
        if not isinstance(raw_calls, (list, tuple)):
            raise ValueError("model returned malformed tool_calls")
        if raw_calls:
            first = raw_calls[0]
            if not isinstance(first, Mapping):
                raise ValueError("model returned a malformed tool call")
            function = first.get("function")
            if not isinstance(function, Mapping):
                raise ValueError("tool call has no function object")
            name = function.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("tool call has no function name")
            # 同一轮里的后续工具调用会被循环忽略，这里显式丢弃以免静默丢动作。
            return ModelDecision(
                DecisionKind.TOOL_CALL,
                tool_call=ToolCall(name.strip(), _coerce_arguments(function.get("arguments"))),
            ), ""

        content = message.get("content")
        if not isinstance(content, str):
            content = ""
        content = content.strip()
        if not content:
            # 只有 thinking 没有正文：让循环 fail-closed，而不是替模型编一个答复。
            return None, "model returned neither a tool call nor a message"
        kind = DecisionKind.CLARIFY if self._clarify_detector(content) else DecisionKind.FINAL
        return ModelDecision(kind, message=content), ""
