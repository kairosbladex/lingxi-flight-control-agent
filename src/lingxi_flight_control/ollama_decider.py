"""把 238 GPU 服务器上的模型接成飞控 Agent 的决策大脑。

本模块是 `ModelDecider` 的一个真实实现：把 `ToolRegistry` 的工具契约下发给模型，
再把模型的输出收敛回受约束的 `ModelDecision`。它只做协议转换，不含任何业务判断；
安全边界仍然全部由 `BoundedAgentLoop` 和 `ToolRegistry` 持有。

设计取舍：
- 只支持原生 function calling。依赖正则从自由文本里抠参数会让模型输出绕过 schema 校验。
- 模型返回 tool_call 时丢弃同轮的散文，保证 `ModelDecision` 的互斥契约成立。
- 模型什么都没返回时抛错而不是猜，交由循环 fail-closed 处理。
- **prompt 预算由本模块自己管住。** Ollama 在 prompt 超过 `num_ctx` 时静默丢掉**最前面**的
  token——也就是 system prompt 里的硬性约束——并且不报错。所以每次发送前都用保守的字符
  估算核对预算，超了就按确定策略压缩，绝不把超长 prompt 交出去。
"""

from __future__ import annotations

import json
import math
import re
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
from .telemetry import Telemetry
from .tool_selector import SELECTION_MODES, tool_name as _definition_name

DEFAULT_BASE_URL = "http://192.168.130.238:11434"
DEFAULT_MODEL = "qwen3.5:9b"

#: 默认上下文窗口。qwen3.5:9b 原生声明 256K，但 KV cache 随 num_ctx 线性占显存，
#: 开满只是白烧显存。实测（docs/model-selection.md）59 工具 + system prompt = 3525 tokens，
#: 再叠 20 次 read_state 观察的完整对话 = 14913 tokens；32768 给到 2 倍余量，
#: 同时只有原生上限的 1/8。
DEFAULT_NUM_CTX = 32768

#: 预留给模型生成（含 thinking）的 token。prompt 与生成必须一起装进 num_ctx，
#: 真实循环里一次决策的 eval_count 实测只有几百，2048 足够宽裕。
DEFAULT_OUTPUT_RESERVE_TOKENS = 2048

#: 回放给模型的最近观察条数上限；更早的压成明确占位。
#: 它把稳态 prompt 增长封顶（每次决策只多一条观察），预算闸门则处理单条超大观察。
DEFAULT_KEEP_RECENT_OBSERVATIONS = 12

#: 被压掉的观察是否回放「工具名 + 状态」摘要（而不是一句裸占位）。
#: 设 False 只为 A/B 复现修复前的行为，生产默认开——理由见 docs/model-selection.md。
DEFAULT_OMISSION_SUMMARY = True

#: 保守字符估算：中日韩字符按 1 token/字，其余按 2 字符/token。
#: 实测（238 上的 qwen3.5:9b）：59 工具 + system prompt = 3525 tokens，本估算给 7449（+111%）；
#: 单条 read_state 回执 1386 字符 ≈ 569 tokens，本估算给 704（+24%）。
#: 两个方向都偏高，保证估算值不会低于真实 prompt（宁可早压，不可静默截断）。
_NON_CJK_CHARS_PER_TOKEN = 2.0
_CJK_CHARACTER = re.compile(r"[\u2e80-\u9fff\u3000-\u303f\uff00-\uffef]")
#: 每条消息的模板开销（<|im_start|>role ... <|im_end|>）。
_MESSAGE_OVERHEAD_TOKENS = 8
#: tools 段的模板包装开销。
_TOOLS_OVERHEAD_TOKENS = 24


class PromptBudgetExceeded(ValueError):
    """压缩到底也装不进预算：宁可硬失败，也不让服务端静默截断 system prompt。"""


class ModelTimeoutError(ValueError):
    """模型 HTTP 请求超时；兼容既有捕获 ValueError 的调用方。"""


class ModelOutputTruncatedError(ValueError):
    """生成被截断，已有工具字段也不能作为完整意图使用。"""


def estimate_tokens(text: str, *, estimator: Callable[[str], int] | None = None) -> int:
    """不依赖 tokenizer 的保守 token 估算；``estimator`` 可注入自定义计数函数。"""
    if estimator is not None:
        return max(0, int(estimator(text)))
    if not text:
        return 0
    cjk = len(_CJK_CHARACTER.findall(text))
    return int(math.ceil(cjk + (len(text) - cjk) / _NON_CJK_CHARS_PER_TOKEN))


def _message_text(message: Mapping[str, Any]) -> str:
    parts = [str(message.get("content") or "")]
    tool_calls = message.get("tool_calls")
    if tool_calls:
        parts.append(json.dumps(tool_calls, ensure_ascii=False, sort_keys=True, default=str))
    return "\n".join(parts)


def estimate_prompt_tokens(
    messages: Sequence[Mapping[str, Any]],
    definitions: Sequence[ToolDefinition] = (),
    *,
    estimator: Callable[[str], int] | None = None,
) -> int:
    """估算一次请求的 prompt tokens：对话消息 + 下发的 tools 契约。

    tools 也算进 prompt——Ollama 会把 function 声明渲染进模板，59 个驾驶舱工具实测
    就占掉 3213~3525 tokens，漏掉它会让预算形同虚设。
    """
    total = 0
    for message in messages:
        total += estimate_tokens(_message_text(message), estimator=estimator) + _MESSAGE_OVERHEAD_TOKENS
    if definitions:
        tools_json = json.dumps(build_tools_payload(definitions), ensure_ascii=False, sort_keys=True)
        total += estimate_tokens(tools_json, estimator=estimator) + _TOOLS_OVERHEAD_TOKENS
    return total


#: 一次响应的生成读数。`done_reason="length"` 表示生成被窗口截断——那正是
#: ``output_reserve`` 太小才会出现的故障，因此它必须是可读的一等遥测。
_GENERATION_FIELDS = ("done_reason", "eval_count", "prompt_eval_count")


def generation_telemetry(response: Mapping[str, Any] | None) -> dict[str, Any]:
    """从一次模型响应里抽出生成读数；缺字段时返回 ``None`` 而不是抛错。

    联调脚本与评测都用它读 ``done_reason``/``eval_count``，避免各自硬编码字段名。
    """
    if not isinstance(response, Mapping):
        return {field: None for field in _GENERATION_FIELDS}
    return {field: response.get(field) for field in _GENERATION_FIELDS}


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
    "请提供", "请说明", "请先", "请补充", "请给出", "告诉我", "澄清", "哪种",
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
    except urllib.error.HTTPError as exc:
        # The legacy loop persists exception text. Never read or echo upstream
        # bodies/reason phrases; retain only the HTTP status and release the body.
        try:
            exc.close()
        except Exception:  # pragma: no cover - cleanup must not expose another upstream error
            pass
        raise ValueError(f"model server rejected the request: HTTP {exc.code}") from None
    except TimeoutError as exc:
        # 冷启动加载模型可能需要数分钟；报错要能区分「加载慢」和「服务挂了」。
        raise ModelTimeoutError(
            f"model server did not respond within {timeout:.0f}s "
            "(a cold model load can exceed this; raise timeout or set keep_alive to keep it resident)"
        ) from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise ModelTimeoutError(
                f"model server did not respond within {timeout:.0f}s "
                "(a cold model load can exceed this; raise timeout or set keep_alive to keep it resident)"
            ) from exc
        raise ValueError("model server is unreachable") from None
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
    # “后退多少米？”等自然补参常省略第二人称，不能因此结束会话并丢掉原意图。
    return is_question and len(text) <= 200


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
        num_ctx: int = DEFAULT_NUM_CTX,
        output_reserve: int = DEFAULT_OUTPUT_RESERVE_TOKENS,
        max_output_tokens: int | None = None,
        keep_recent_observations: int = DEFAULT_KEEP_RECENT_OBSERVATIONS,
        omission_summary: bool = DEFAULT_OMISSION_SUMMARY,
        keep_alive: str | int | None = None,
        max_attempts: int = 2,
        transport: Transport | None = None,
        clarify_detector: Callable[[str], bool] | None = None,
        token_estimator: Callable[[str], int] | None = None,
        result_projector: Callable[[str, Any], Any] | None = None,
        telemetry: Telemetry | None = None,
        tool_selector: Any = None,
    ):
        if not model or not model.strip():
            raise ValueError("model name is required")
        if not base_url or not base_url.strip():
            raise ValueError("base_url is required")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        # num_ctx 必须已知：不知道窗口大小就无法管住预算，而服务端超限只会静默截断。
        if isinstance(num_ctx, bool) or not isinstance(num_ctx, int) or num_ctx <= 0:
            raise ValueError("num_ctx must be a positive integer so the prompt budget is known")
        if isinstance(output_reserve, bool) or not isinstance(output_reserve, int) or output_reserve < 0:
            raise ValueError("output_reserve must be a non-negative integer")
        if output_reserve >= num_ctx:
            raise ValueError("num_ctx must exceed output_reserve so the prompt has a budget")
        if max_output_tokens is not None and (
                isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int)
                or not 0 < max_output_tokens <= output_reserve):
            raise ValueError("max_output_tokens must be positive and fit the output reserve")
        if (isinstance(keep_recent_observations, bool)
                or not isinstance(keep_recent_observations, int) or keep_recent_observations < 0):
            raise ValueError("keep_recent_observations must be a non-negative integer")
        if not isinstance(omission_summary, bool):
            raise ValueError("omission_summary must be a boolean")
        if token_estimator is not None and not callable(token_estimator):
            raise ValueError("token_estimator must be callable")
        if result_projector is not None and not callable(result_projector):
            raise ValueError("result_projector must be callable")
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
        self._output_reserve = output_reserve
        self._max_output_tokens = max_output_tokens
        self._keep_recent_observations = keep_recent_observations
        self._omission_summary = omission_summary
        self._token_estimator = token_estimator
        self._result_projector = result_projector
        if telemetry is not None and not isinstance(telemetry, Telemetry):
            raise ValueError("telemetry must be a Telemetry instance")
        # 默认安静：不注入时就一个事件都不发。
        self._telemetry = telemetry if telemetry is not None else Telemetry.disabled()
        if tool_selector is not None and not callable(getattr(tool_selector, "select", None)):
            raise ValueError("tool_selector must provide a select() method")
        self._tool_selector = tool_selector
        # 工具检索读数：这次下发的是全量还是子集、子集多大、检索层出过几次错。
        self.last_tool_selection = "full"
        self.last_selected_tools: int | None = None
        self.selector_errors = 0
        self._keep_alive = keep_alive
        self._max_attempts = int(max_attempts)
        self._transport = transport or http_transport
        self._clarify_detector = clarify_detector or _looks_like_clarification
        self.last_raw_response: Mapping[str, Any] | None = None
        self.empty_responses = 0
        # 预算遥测：压缩必须是可观测的，否则线上分不清「闸门在工作」和「根本没触发」。
        #: 最近一次构建的 prompt 估算 tokens；还没构建过时为 None。
        self.last_prompt_estimate: int | None = None
        #: 最近一次构建时被省略的观察条数。
        self.last_omitted_observations = 0
        #: 累计发生压缩的载荷次数（每次 ``payload()`` 计一次，含重试）。
        self.compressed_payloads = 0
        #: 累计被省略的观察条数。
        self.omitted_observation_turns = 0

    # ---- prompt 预算 -------------------------------------------------------

    @property
    def prompt_budget_tokens(self) -> int:
        """一次请求允许占用的最大 prompt tokens（给生成留出 ``output_reserve``）。"""
        return self._num_ctx - self._output_reserve

    @property
    def last_generation(self) -> dict[str, Any]:
        """最近一次模型响应的生成读数（``done_reason``/``eval_count``/``prompt_eval_count``）。

        ``output_reserve`` 的取值只能靠这个读数来定：``done_reason="length"`` 就是预留不足。
        """
        return generation_telemetry(self.last_raw_response)

    @property
    def telemetry(self) -> Telemetry:
        return self._telemetry

    def estimate_prompt_tokens(
        self, messages: Sequence[Mapping[str, Any]], definitions: Sequence[ToolDefinition] = ()
    ) -> int:
        return estimate_prompt_tokens(messages, definitions, estimator=self._token_estimator)

    # ---- 工具检索 -----------------------------------------------------------

    def _select_tools(
        self, user_message: str, checkpoint: AgentCheckpoint,
        definitions: Sequence[ToolDefinition],
    ) -> tuple[ToolDefinition, ...]:
        """按意图缩小下发的工具面；任何异常都退化成「全量下发」。

        这是刻意的不对称：检索层有 bug 时只能多花 token，**绝不能**变成工具变少
        （模型无法调用没下发的工具，那是能力缺失），也绝不能因此让 run 失败。
        """
        tools = tuple(definitions)
        if self._tool_selector is None:
            self.last_tool_selection = "full"
            self.last_selected_tools = len(tools)
            return tools
        try:
            result = self._tool_selector.select(user_message, tools, checkpoint=checkpoint)
            names = tuple(getattr(result, "names", result))
            mode = str(getattr(result, "mode", "retrieved"))
            if mode not in SELECTION_MODES:
                raise ValueError(f"unknown selection mode: {mode!r}")
            keep = set(names)
            selected = tuple(tool for tool in tools if _definition_name(tool) in keep)
            if not selected:
                raise ValueError("selector returned an empty candidate set")
        except Exception:
            self.selector_errors += 1
            self.last_tool_selection = "full"
            self.last_selected_tools = len(tools)
            return tools
        if len(selected) >= len(tools):
            self.last_tool_selection = "full"
            self.last_selected_tools = len(tools)
            return tools
        self.last_tool_selection = mode
        self.last_selected_tools = len(selected)
        return selected

    #: 摘要里的状态标记：成功回执 ✓，失败回执 ✗。
    _SUMMARY_OK = "✓"
    _SUMMARY_ERROR = "✗"

    @classmethod
    def _summarize(cls, labels: Sequence[tuple[str, bool]]) -> str:
        listed = " ".join(
            f"{name}{cls._SUMMARY_OK if ok else cls._SUMMARY_ERROR}" for name, ok in labels)
        return f"（更早的 {len(labels)} 次工具调用已省略：{listed}）"

    def _omission_candidates(
        self, labels: Sequence[tuple[str, bool]]
    ) -> tuple[tuple[dict[str, Any] | None, str], ...]:
        """省略说明的确定性降级阶梯：完整摘要 → 紧凑占位 → 不放说明。

        裸占位只说「有更早的调用」，模型不知道**做过什么、成没成**，于是容易重复调用已经
        成功过的工具。摘要把「工具名 + 状态」留下来，同时它自己仍然受同一套预算约束：
        **摘要优先被压**——预算不够先牺牲摘要的信息量，观察本身永远晚一步才被丢。

        返回 (候选消息或 None, 档位名)；档位名进遥测，线上据此看到压到了哪一档。
        """
        compact = {"role": "assistant", "content": f"（更早的 {len(labels)} 次工具调用已省略）"}
        if not labels:
            return ((None, "none"),)
        if not self._omission_summary:
            return ((compact, "compact"), (None, "none"))
        return (
            ({"role": "assistant", "content": self._summarize(labels)}, "summary"),
            (compact, "compact"),
            (None, "none"),
        )

    def _fit_observations(
        self,
        head: list[dict[str, Any]],
        turns: list[list[dict[str, Any]]],
        definitions: Sequence[ToolDefinition],
        labels: Sequence[tuple[str, bool]],
    ) -> tuple[list[list[dict[str, Any]]], dict[str, Any] | None, str]:
        """确定性地挑出还能回放的观察轮次，返回 (保留的轮次, 省略说明, 档位名)。

        三层保护：
        1. 只回放最近 ``keep_recent_observations`` 条，把稳态增长封顶；
        2. 预算硬闸门——即便在候选里，装不下就从最旧的继续往前丢；
        3. 省略说明先降级（完整摘要 → 紧凑占位 → 不放），再考虑丢观察。
        system prompt 与当前 user 消息在 ``head`` 里，任何情况下都不参与丢弃。
        """
        cap = self._keep_recent_observations
        kept = list(turns[len(turns) - cap:]) if cap < len(turns) else list(turns)
        while True:
            omitted = len(turns) - len(kept)
            for omission, mode in self._omission_candidates(labels[:omitted]):
                candidate = list(head)
                if omission is not None:
                    candidate.append(omission)
                for turn in kept:
                    candidate.extend(turn)
                if self.estimate_prompt_tokens(candidate, definitions) <= self.prompt_budget_tokens:
                    return kept, omission, mode
            if not kept:
                raise PromptBudgetExceeded(
                    f"prompt budget is {self.prompt_budget_tokens} tokens, but the system prompt "
                    f"and the current user message already need about "
                    f"{self.estimate_prompt_tokens(head, definitions)}; raise num_ctx or shorten "
                    "the system prompt and the user message"
                )
            kept.pop(0)

    # ---- 协议转换 ----------------------------------------------------------

    def messages(
        self,
        user_message: str,
        checkpoint: AgentCheckpoint,
        definitions: Sequence[ToolDefinition] = (),
    ) -> list[dict[str, Any]]:
        """把检查点重建成原生对话，并保证结果装得进 prompt 预算。

        回放依据 `observations`：现在每条观察都自带工具名与参数，所以不需要再去对齐
        另一个列表。这一点很关键——确认轮次会让 `tool_calls` 重复记录，校验失败的尝试
        又只出现在 `observations` 里，靠两个列表 zip 一定错位。
        校验失败的尝试也要回放，模型才知道自己上一轮错在哪。

        system prompt 与当前 user 消息永远保留；更早的观察按 `_fit_observations` 的
        确定策略压缩。这里必须传 `definitions`，否则 59 个工具契约那 3000 多 tokens
        不会计入预算。

        本方法不写遥测（它常被调用方直接拿来检查消息形状）；预算读数由 ``payload()``
        在真正构建待发送载荷时记录。
        """
        messages, _, _, _ = self._rebuild(user_message, checkpoint, definitions)
        return messages

    def _rebuild(
        self,
        user_message: str,
        checkpoint: AgentCheckpoint,
        definitions: Sequence[ToolDefinition],
    ) -> tuple[list[dict[str, Any]], int, str, int]:
        """重建对话并返回 (消息, 被省略的观察条数, 省略档位, 保留的观察条数)。"""
        head: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": user_message},
        ]
        turns: list[list[dict[str, Any]]] = []
        # 与 turns 一一对应的 (工具名, 是否成功)，用来给被压掉的观察写摘要。
        labels: list[tuple[str, bool]] = []
        for observation in checkpoint.observations:
            name = observation.get("tool")
            if not isinstance(name, str) or not name:
                continue
            arguments = observation.get("arguments")
            if "result" in observation:
                payload: Any = observation["result"]
                # 投影只作用于成功回执；error 回执是模型自纠的唯一线索，原样回放。
                if self._result_projector is not None:
                    payload = self._result_projector(name, payload)
            else:
                payload = observation.get("error", {})
            turns.append([
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {
                        "name": name,
                        "arguments": dict(arguments) if isinstance(arguments, Mapping) else {},
                    }}],
                },
                {
                    "role": "tool",
                    "content": json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
                },
            ])
            labels.append((name, "result" in observation))

        kept, omission, mode = self._fit_observations(head, turns, definitions, labels)
        messages = list(head)
        if omission is not None:
            messages.append(omission)
        for turn in kept:
            messages.extend(turn)
        return messages, len(turns) - len(kept), mode, len(kept)

    def payload(
        self, user_message: str, checkpoint: AgentCheckpoint, definitions: Sequence[ToolDefinition]
    ) -> dict[str, Any]:
        # 构建失败时不能把上一次请求的预算读数记到本次失败上。
        self.last_prompt_estimate = None
        self.last_omitted_observations = 0
        # 检索层在预算之前生效：预算、tools 段与 prompt_eval 三者必须看到同一个工具面。
        definitions = self._select_tools(user_message, checkpoint, definitions)
        messages, omitted, omission_mode, kept = self._rebuild(user_message, checkpoint, definitions)
        # 唯一的预算记账点：decide() 每次都经过这里，因此读数就是「刚发出去的那个请求」。
        self.last_prompt_estimate = self.estimate_prompt_tokens(messages, definitions)
        self.last_omitted_observations = omitted
        if omitted:
            self.compressed_payloads += 1
            self.omitted_observation_turns += omitted
            # 「哪一步开始压、压到哪一档」必须线上可见，否则分不清闸门在工作还是没触发。
            self._telemetry.compressed(
                omitted_observations=omitted,
                kept_observations=kept,
                omission_mode=omission_mode,
                prompt_estimate=self.last_prompt_estimate,
                prompt_budget=self.prompt_budget_tokens,
                num_ctx=self._num_ctx,
                output_reserve=self._output_reserve,
                compressed_payloads=self.compressed_payloads,
                omitted_observation_turns=self.omitted_observation_turns,
            )
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "tools": build_tools_payload(definitions),
            "options": {"temperature": self._temperature, "num_ctx": self._num_ctx},
        }
        if self._max_output_tokens is not None:
            body["options"]["num_predict"] = self._max_output_tokens
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
        那是协议问题，应该直接暴露。生成截断同样硬失败，不接受部分输出或重试。
        """
        last_message = ""
        for attempt in range(1, self._max_attempts + 1):
            self.last_raw_response = None
            try:
                body = self.payload(user_message, checkpoint, tools)
                response = self._transport(self._endpoint, body, self._timeout)
                # 错误信封在协议层解释，自定义传输层同样受保护。
                if isinstance(response.get("error"), str) and response["error"]:
                    raise ValueError("model server returned an error response")
                self.last_raw_response = response
                if response.get("done_reason") == "length":
                    raise ModelOutputTruncatedError("model output was truncated")
                decision, empty_reason = self._interpret(response)
            except Exception:
                # 只补遥测，原异常原样上抛；硬失败不重试，也不能误记为空响应。
                self._emit_decide(attempt, None, failed=True)
                raise
            self._emit_decide(attempt, decision)
            if decision is not None:
                return decision
            last_message = empty_reason
            self.empty_responses += 1
            if attempt < self._max_attempts:
                continue
        raise ValueError(
            f"model returned an unusable response after {self._max_attempts} attempts: {last_message}")

    def _emit_decide(self, attempt: int, decision: ModelDecision | None, *,
                     failed: bool = False) -> None:
        """把这次决策的预算读数与生成读数发成一条事件。

        这里回答线上最想知道的三个问题：预算闸门有没有触发（``compressed`` /
        ``omitted_observations``）、离闸门还有多远（``budget_used_ratio`` / ``near_budget``）、
        生成有没有被窗口截断（``generation_truncated`` 即 ``done_reason == "length"``）。
        """
        tool = decision.tool_call.name if decision is not None and decision.tool_call else None
        kind = "error" if failed else ("empty" if decision is None else str(decision.kind.value))
        self._telemetry.decide(
            attempt=attempt,
            kind=kind,
            tool=tool,
            empty_response=decision is None and not failed,
            prompt_estimate=self.last_prompt_estimate,
            prompt_budget=self.prompt_budget_tokens,
            num_ctx=self._num_ctx,
            output_reserve=self._output_reserve,
            omitted_observations=self.last_omitted_observations,
            generation=self.last_generation,
        )

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
