"""理解用户意图并在确认后调用任务工具。"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import math
from typing import Any, Callable, Protocol

from .models import (
    AgentCheckpoint,
    AgentRunResult,
    DecisionKind,
    DispatchResult,
    DispatchStatus,
    ExecutionContext,
    LoopStatus,
    ModelDecision,
    RelativeMove,
    ToolCall,
    ToolDefinition,
    ToolParameter,
    ToolSchema,
)


def parse_relative_move(text: str, *, idempotency_key: str) -> RelativeMove:
    """把单个受限中文控制句解析为相对移动。

    这里只解析方向和距离；执行上下文、确认、幂等和设备状态由执行层负责。
    """
    import re

    if not text or not text.strip():
        raise ValueError("control text is required")
    match = re.fullmatch(
        r"\s*(?:向?前|前进|往前)\s*(?P<distance>\d+(?:\.\d+)?)\s*米\s*", text
    )
    direction = "forward"
    if not match:
        patterns = {
            "backward": r"(?:向?后|后退|往后)",
            "left": r"(?:向?左|左移)",
            "right": r"(?:向?右|右移)",
            "up": r"(?:上升|向?上)",
            "down": r"(?:下降|向?下)",
        }
        for candidate, prefix in patterns.items():
            match = re.fullmatch(rf"\s*{prefix}\s*(?P<distance>\d+(?:\.\d+)?)\s*米\s*", text)
            if match:
                direction = candidate
                break
    if not match:
        raise ValueError("expected one direction and distance in meters")
    move = RelativeMove(direction, float(match.group("distance")), idempotency_key)
    move.validate()
    return move


class FlightExecutionAgent:
    """飞行执行文本闭环：解析后交给已绑定的内存/平台执行器。"""

    def __init__(self, context: ExecutionContext, executor: Any):
        context.validate()
        self._context = context
        self._executor = executor

    def handle(self, text: str, *, idempotency_key: str):
        move = parse_relative_move(text, idempotency_key=idempotency_key)
        return self._executor.relative_move(self._context, move)


class IntentInterpreter(Protocol):
    def __call__(self, text: str) -> ToolCall: ...


class TaskTool(Protocol):
    def __call__(self, arguments: Mapping[str, Any]) -> Any: ...


class TaskDispatchAgent:
    """把自然语言意图路由到允许的任务工具，工具由调用方注入。"""

    def __init__(self, interpreter: IntentInterpreter, tools: Mapping[str, TaskTool]):
        self._interpreter = interpreter
        self._tools = dict(tools)

    def handle(self, text: str, *, confirmed: bool = False) -> DispatchResult:
        if not text or not text.strip():
            raise ValueError("user text is required")
        tool_call = self._interpreter(text)
        tool_call.validate()
        tool = self._tools.get(tool_call.name)
        if tool is None:
            return DispatchResult(DispatchStatus.REJECTED, tool_call)
        if not confirmed:
            return DispatchResult(DispatchStatus.CONFIRMATION_REQUIRED, tool_call)
        return DispatchResult(
            DispatchStatus.DISPATCHED,
            tool_call,
            output=tool(tool_call.arguments),
        )


class ModelDecider(Protocol):
    def decide(
        self, user_message: str, checkpoint: AgentCheckpoint, tools: tuple[ToolDefinition, ...]
    ) -> ModelDecision: ...


class ToolRegistry:
    """白名单工具及其结构化参数契约。"""

    def __init__(
        self,
        definitions: Mapping[str, ToolDefinition] | None = None,
        *,
        runtime_context_provider: Callable[[], ExecutionContext] | None = None,
    ):
        self._definitions: dict[str, ToolDefinition] = {}
        self._runtime_context_provider = runtime_context_provider
        for definition in (definitions or {}).values():
            self.register(definition)

    def register(self, definition: ToolDefinition) -> None:
        if not definition.name or not definition.name.replace("_", "").isalnum():
            raise ValueError("tool name must be a simple identifier")
        if definition.requires_confirmation and not definition.side_effect:
            raise ValueError("confirmation is only valid for side-effect tools")
        if not callable(definition.handler):
            raise ValueError("tool handler must be callable")
        self._definitions[definition.name] = definition

    def get(self, name: str) -> ToolDefinition | None:
        return self._definitions.get(name)

    def definitions(self) -> tuple[ToolDefinition, ...]:
        # Model receives a contract only; callable handlers never cross the boundary.
        return tuple(
            ToolDefinition(item.name, item.description, item.schema, None,
                           item.side_effect, item.requires_confirmation)
            for item in self._definitions.values()
        )

    def runtime_context(self) -> ExecutionContext | None:
        """读取当前 Runtime 上下文；未绑定上下文的通用注册表返回 None。"""
        if self._runtime_context_provider is None:
            return None
        context = self._runtime_context_provider()
        if not isinstance(context, ExecutionContext):
            raise ValueError("runtime context provider must return ExecutionContext")
        context.validate()
        return context

    def validate(self, call: ToolCall) -> list[str]:
        definition = self.get(call.name)
        if definition is None:
            return [f"unknown tool: {call.name}"]
        schema = definition.schema
        errors: list[str] = []
        arguments = call.arguments
        for field in schema.required:
            if field not in arguments:
                errors.append(f"missing required field: {field}")
        if not schema.additional_properties:
            extras = sorted(set(arguments) - set(schema.properties))
            errors.extend(f"unexpected field: {field}" for field in extras)
        for field, value in arguments.items():
            spec = schema.properties.get(field)
            if spec is None:
                continue
            if isinstance(spec, Mapping):
                spec = ToolParameter(
                    type=spec.get("type", ""),
                    enum=tuple(spec["enum"]) if spec.get("enum") is not None else None,
                    minimum=spec.get("minimum"), maximum=spec.get("maximum"),
                    exclusive_minimum=spec.get("exclusive_minimum", False),
                    min_length=spec.get("min_length"),
                )
            if not _matches_type(value, spec.type):
                errors.append(f"field {field} must be {spec.type}")
                continue
            if spec.min_length is not None and isinstance(value, str) and len(value) < spec.min_length:
                errors.append(f"field {field} must contain at least {spec.min_length} characters")
            if spec.enum is not None and value not in spec.enum:
                errors.append(f"field {field} must be one of {list(spec.enum)}")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                try:
                    finite = math.isfinite(value)
                except (OverflowError, TypeError):
                    finite = False
                if not finite:
                    errors.append(f"field {field} must be finite")
                    continue
                if spec.minimum is not None and (
                    value < spec.minimum or (spec.exclusive_minimum and value == spec.minimum)
                ):
                    comparator = "greater than" if spec.exclusive_minimum else "at least"
                    errors.append(f"field {field} must be {comparator} {spec.minimum}")
                if spec.maximum is not None and value > spec.maximum:
                    errors.append(f"field {field} is above maximum {spec.maximum}")
        return errors


def _matches_type(value: Any, expected: str) -> bool:
    return {
        "string": lambda: isinstance(value, str),
        "number": lambda: isinstance(value, (int, float)) and not isinstance(value, bool),
        "integer": lambda: isinstance(value, int) and not isinstance(value, bool),
        "boolean": lambda: isinstance(value, bool),
        "object": lambda: isinstance(value, Mapping),
        "array": lambda: isinstance(value, (list, tuple)),
    }.get(expected, lambda: False)()


def _contains_unsafe_content(value: Any) -> bool:
    """拒绝模型越过边界提供连接信息、厂商报文或虚假的执行结论。

    注意：这里只能用无歧义的术语。裸词 ``payload`` 曾被列入，但它同时是载荷领域的
    正常词汇（``mounted_payloads()``、``payload_*`` 动作、状态里的 ``payloads`` 字段），
    会把驾驶舱的合法回执整条判为禁止内容。厂商报文的意图已由「厂商指令」「飞控报文」覆盖。
    """
    forbidden = (
        "mqtt", "password", "passwd", "credential", "token", "secret", "私钥", "凭据",
        "厂商指令", "飞控报文", "执行成功", "已成功执行",
        "execution successful", "execution succeeded", "successfully executed",
    )
    if isinstance(value, str):
        text = value.lower()
        return any(term in text for term in forbidden)
    if isinstance(value, Mapping):
        return any(_contains_unsafe_content(k) or _contains_unsafe_content(v) for k, v in value.items())
    if isinstance(value, (list, tuple, set)):
        return any(_contains_unsafe_content(item) for item in value)
    return False


class BoundedAgentLoop:
    """模型决定、白名单校验、受控工具执行和检查点更新的有限循环。"""

    def __init__(self, decider: ModelDecider, registry: ToolRegistry, *, max_steps: int = 8,
                 continue_on_tool_error: bool = False):
        if max_steps <= 0:
            raise ValueError("max_steps must be positive")
        self._decider = decider
        self._registry = registry
        self._max_steps = max_steps
        self._continue_on_tool_error = continue_on_tool_error

    def run(
        self,
        user_message: str,
        *,
        context: Mapping[str, Any] | ExecutionContext | None = None,
        runtime_context: Mapping[str, Any] | ExecutionContext | None = None,
        checkpoint: AgentCheckpoint | None = None,
        confirmed: bool = False,
    ) -> AgentRunResult:
        if not user_message.strip():
            raise ValueError("user text is required")
        if _contains_unsafe_content(user_message) or not _json_safe(user_message):
            raise ValueError("user input contains forbidden or non-serializable content")

        context_payload = _context_payload(context)
        if _contains_unsafe_content(context_payload) or not _json_safe(context_payload):
            raise ValueError("context contains forbidden or non-serializable content")
        try:
            requested_runtime = _coerce_runtime_context(runtime_context, "runtime_context")
            context_runtime = _coerce_runtime_context(context, "context")
            if requested_runtime is not None and context_runtime is not None and requested_runtime != context_runtime:
                raise _StaleRuntimeContext("runtime context does not match context")
            current_runtime = self._registry.runtime_context()
            if current_runtime is not None and requested_runtime is not None and current_runtime != requested_runtime:
                raise _StaleRuntimeContext("runtime context does not match the registered runtime")
            if current_runtime is not None and context_runtime is not None and current_runtime != context_runtime:
                raise _StaleRuntimeContext("context does not match the registered runtime")
            current_runtime = current_runtime or requested_runtime or context_runtime
        except _StaleRuntimeContext as exc:
            if checkpoint is not None:
                return self._stale_context(checkpoint, str(exc))
            checkpoint = AgentCheckpoint(
                user_input=user_message, context={}, messages=[], tool_calls=[], observations=[],
                max_steps=self._max_steps,
            )
            return self._stale_context(checkpoint, str(exc))
        except (TypeError, ValueError) as exc:
            if checkpoint is not None:
                return self._error(checkpoint, f"invalid runtime context: {exc}")
            checkpoint = AgentCheckpoint(
                user_input=user_message, context={}, messages=[], tool_calls=[], observations=[],
                max_steps=self._max_steps,
            )
            return self._error(checkpoint, f"invalid runtime context: {exc}")

        if checkpoint is None:
            checkpoint = AgentCheckpoint(
                user_input=user_message, context=context_payload, messages=[], tool_calls=[],
                observations=[], max_steps=self._max_steps,
                runtime_context=current_runtime.to_dict() if current_runtime else None,
            )
        if checkpoint.user_input != user_message:
            return self._error(checkpoint, "checkpoint user input does not match")
        try:
            stored_runtime = _checkpoint_runtime_context(checkpoint)
        except ValueError as exc:
            return self._stale_context(checkpoint, f"checkpoint runtime context is invalid: {exc}")
        if current_runtime is not None:
            if stored_runtime is None:
                return self._stale_context(checkpoint, "checkpoint has no runtime context binding")
            if stored_runtime != current_runtime:
                return self._stale_context(checkpoint, "checkpoint runtime context is stale")
        if confirmed:
            checkpoint.confirmed = True
        if self._needs_survey_clarification(checkpoint.user_input):
            checkpoint.status = LoopStatus.CLARIFICATION
            message = "请明确选择正射影像或倾斜摄影的测绘方式。"
            checkpoint.messages.append({"role": "assistant", "kind": "clarify", "content": message})
            return AgentRunResult(LoopStatus.CLARIFICATION, checkpoint, message=message)
        while checkpoint.step_count < checkpoint.max_steps:
            freshness_error = self._check_runtime_freshness(checkpoint)
            if freshness_error is not None:
                return freshness_error
            try:
                decision = self._decider.decide(user_message, checkpoint, self._registry.definitions())
                if not isinstance(decision, ModelDecision):
                    raise ValueError("model must return ModelDecision")
                if _contains_unsafe_content(decision.message) or _contains_unsafe_content(
                    decision.tool_call.arguments if decision.tool_call else None
                ):
                    raise ValueError("model output contains forbidden execution or connection data")
                decision.validate()
            except Exception as exc:
                return self._error(checkpoint, str(exc))
            if decision.kind == DecisionKind.FINAL:
                checkpoint.step_count += 1
                checkpoint.status = LoopStatus.FINAL
                checkpoint.messages.append({"role": "assistant", "kind": "final", "content": decision.message})
                return AgentRunResult(LoopStatus.FINAL, checkpoint, message=decision.message)
            if decision.kind == DecisionKind.CLARIFY:
                checkpoint.step_count += 1
                checkpoint.status = LoopStatus.CLARIFICATION
                checkpoint.messages.append({"role": "assistant", "kind": "clarify", "content": decision.message})
                return AgentRunResult(LoopStatus.CLARIFICATION, checkpoint, message=decision.message)
            call = decision.tool_call
            assert call is not None
            freshness_error = self._check_runtime_freshness(checkpoint)
            if freshness_error is not None:
                return freshness_error
            if not _json_safe(call.arguments):
                return self._error(checkpoint, "tool arguments must be JSON-serializable", call)
            checkpoint.tool_calls.append({"name": call.name, "arguments": dict(call.arguments)})
            definition = self._registry.get(call.name)
            if definition is None:
                # 调用不存在的工具是模型越界，不是参数写错，保持硬失败。
                return self._error(checkpoint, f"unknown tool: {call.name}", call)
            try:
                errors = self._registry.validate(call)
            except Exception as exc:
                return self._error(checkpoint, f"invalid tool schema or arguments: {exc}", call)
            if errors:
                # 参数写错可以自纠：把校验错误作为观察结果回喂，让模型改，而不是让整个 run 死掉。
                # 这里必须计步，否则模型一直给错参数就会死循环。一次失败也打断授权链，
                # 免得旧的确认在中间出了岔子之后仍被沿用。
                checkpoint.step_count += 1
                error = {"code": "tool_error", "message": "; ".join(errors), "tool": call.name}
                checkpoint.last_error = error
                checkpoint.confirmed = False
                checkpoint.observations.append(
                    {"tool": call.name, "arguments": dict(call.arguments), "error": error})
                checkpoint.status = LoopStatus.OBSERVATION
                continue
            try:
                binding = _binding(checkpoint, call)
            except (TypeError, ValueError) as exc:
                return self._error(checkpoint, f"tool arguments cannot be bound safely: {exc}", call)
            if (definition.side_effect or definition.requires_confirmation) and (
                not checkpoint.confirmed or checkpoint.confirmation_binding != binding
            ):
                # 确认只是用户授权握手，任务本身没有推进，因此不计入 max_steps；
                # 但它自己需要一个上限，否则模型每次换一个调用就能无限往复。
                checkpoint.confirmation_rounds += 1
                if checkpoint.confirmation_rounds > checkpoint.max_steps:
                    break
                checkpoint.confirmation_binding = binding
                checkpoint.confirmed = False
                checkpoint.status = LoopStatus.CLARIFICATION
                message = f"请确认执行工具 {call.name}，参数：{json.dumps(dict(call.arguments), ensure_ascii=False, sort_keys=True)}"
                checkpoint.messages.append({"role": "assistant", "kind": "confirmation_required", "content": message})
                return AgentRunResult(LoopStatus.CLARIFICATION, checkpoint, message=message, tool_call=call)
            freshness_error = self._check_runtime_freshness(checkpoint)
            if freshness_error is not None:
                return freshness_error
            try:
                output = definition.handler(call.arguments)
            except Exception as exc:
                error = {"code": "tool_exception", "message": str(exc), "tool": call.name}
                checkpoint.last_error = error
                checkpoint.status = LoopStatus.TOOL_ERROR
                checkpoint.observations.append(
                    {"tool": call.name, "arguments": dict(call.arguments), "error": error})
                if not self._continue_on_tool_error:
                    return AgentRunResult(LoopStatus.TOOL_ERROR, checkpoint, tool_call=call, error=error)
                continue
            if _contains_unsafe_content(output) or not _json_safe(output):
                return self._error(checkpoint, "tool output is forbidden or not JSON-serializable", call)
            checkpoint.step_count += 1
            checkpoint.confirmed = False
            checkpoint.confirmation_binding = None
            checkpoint.executed_tool_calls.append(
                {"name": call.name, "arguments": dict(call.arguments), "binding": binding})
            # 观察结果自带工具名与参数。失败尝试也会进这个列表，而它不在
            # executed_tool_calls 里，所以不能靠两个列表对齐来回放——见 OllamaDecider.messages。
            checkpoint.observations.append(
                {"tool": call.name, "arguments": dict(call.arguments), "result": output})
            checkpoint.status = LoopStatus.OBSERVATION
            checkpoint.messages.append({"role": "tool", "name": call.name, "content": output})
        checkpoint.status = LoopStatus.MAX_STEPS_EXCEEDED
        # 撞上限时把最后一个错误带上，调用方才知道是「卡在参数错误」而不是「模型话太多」。
        return AgentRunResult(LoopStatus.MAX_STEPS_EXCEEDED, checkpoint,
                              message="达到最大步骤数，已停止。", error=checkpoint.last_error)

    @staticmethod
    def _needs_survey_clarification(text: str) -> bool:
        return any(word in text for word in ("测绘", "测量", "做测绘任务")) and not any(
            word in text for word in ("正射影像", "倾斜摄影")
        )

    @staticmethod
    def _error(checkpoint: AgentCheckpoint, message: str, call: ToolCall | None = None) -> AgentRunResult:
        error = {"code": "tool_error", "message": message}
        checkpoint.last_error = error
        checkpoint.status = LoopStatus.TOOL_ERROR
        return AgentRunResult(LoopStatus.TOOL_ERROR, checkpoint, tool_call=call, error=error)

    @staticmethod
    def _stale_context(checkpoint: AgentCheckpoint, message: str) -> AgentRunResult:
        error = {"code": "stale_context", "message": message}
        checkpoint.last_error = error
        checkpoint.confirmed = False
        checkpoint.confirmation_binding = None
        checkpoint.status = LoopStatus.TOOL_ERROR
        return AgentRunResult(LoopStatus.TOOL_ERROR, checkpoint, error=error)

    def _check_runtime_freshness(self, checkpoint: AgentCheckpoint) -> AgentRunResult | None:
        try:
            current_runtime = self._registry.runtime_context()
        except (TypeError, ValueError) as exc:
            return self._error(checkpoint, f"invalid runtime context: {exc}")
        if current_runtime is None:
            return None
        try:
            stored_runtime = _checkpoint_runtime_context(checkpoint)
        except ValueError as exc:
            return self._stale_context(checkpoint, f"checkpoint runtime context is invalid: {exc}")
        if stored_runtime is None:
            return self._stale_context(checkpoint, "checkpoint has no runtime context binding")
        if stored_runtime != current_runtime:
            return self._stale_context(checkpoint, "runtime context changed while resuming")
        return None


_RUNTIME_CONTEXT_FIELDS = frozenset({"execution_id", "drone_id", "context_version", "confirmed"})


class _StaleRuntimeContext(ValueError):
    """Internal marker for a valid binding that no longer matches Runtime."""


def _context_payload(value: Mapping[str, Any] | ExecutionContext | None) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, ExecutionContext):
        return value.to_dict()
    if isinstance(value, Mapping):
        return dict(value)
    raise ValueError("context must be an object")


def _coerce_runtime_context(
    value: Mapping[str, Any] | ExecutionContext | None, field_name: str
) -> ExecutionContext | None:
    if value is None:
        return None
    if isinstance(value, ExecutionContext):
        value.validate()
        return value
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be an ExecutionContext object")
    present = _RUNTIME_CONTEXT_FIELDS.intersection(value)
    if not present:
        return None
    if present != _RUNTIME_CONTEXT_FIELDS:
        missing = sorted(_RUNTIME_CONTEXT_FIELDS - present)
        raise ValueError(f"{field_name} is missing {', '.join(missing)}")
    return ExecutionContext.from_mapping(value)


def _checkpoint_runtime_context(checkpoint: AgentCheckpoint) -> ExecutionContext | None:
    if checkpoint.runtime_context is not None:
        return ExecutionContext.from_mapping(checkpoint.runtime_context)
    return _coerce_runtime_context(checkpoint.context, "checkpoint context")


def _binding(checkpoint: AgentCheckpoint, call: ToolCall) -> str:
    payload = json.dumps({"user_input": checkpoint.user_input, "context": dict(checkpoint.context),
                          "runtime_context": dict(checkpoint.runtime_context or {}),
                          "tool": call.name, "arguments": dict(call.arguments)},
                         ensure_ascii=False, sort_keys=True, allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json_safe(value: Any) -> bool:
    try:
        json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError, OverflowError):
        return False
    return True
