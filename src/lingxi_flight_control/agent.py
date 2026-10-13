"""理解用户意图并在确认后调用任务工具。"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from typing import Any, Protocol

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

    def __init__(self, definitions: Mapping[str, ToolDefinition] | None = None):
        self._definitions: dict[str, ToolDefinition] = {}
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
                )
            if not _matches_type(value, spec.type):
                errors.append(f"field {field} must be {spec.type}")
                continue
            if spec.enum is not None and value not in spec.enum:
                errors.append(f"field {field} must be one of {list(spec.enum)}")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if spec.minimum is not None and value < spec.minimum:
                    errors.append(f"field {field} is below minimum {spec.minimum}")
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
    """拒绝模型越过边界提供连接信息、厂商报文或虚假的执行结论。"""
    forbidden = (
        "mqtt", "password", "passwd", "credential", "token", "secret", "私钥", "凭据",
        "厂商指令", "飞控报文", "payload", "执行成功", "已成功执行",
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

    def run(self, user_message: str, *, context: Mapping[str, Any] | None = None,
            checkpoint: AgentCheckpoint | None = None, confirmed: bool = False) -> AgentRunResult:
        if not user_message.strip():
            raise ValueError("user text is required")
        if _contains_unsafe_content(user_message) or not _json_safe(user_message):
            raise ValueError("user input contains forbidden or non-serializable content")
        if checkpoint is None and context is not None and (
            _contains_unsafe_content(context) or not _json_safe(context)
        ):
            raise ValueError("context contains forbidden or non-serializable content")
        checkpoint = checkpoint or AgentCheckpoint(
            user_input=user_message, context=dict(context or {}), messages=[], tool_calls=[],
            observations=[], max_steps=self._max_steps,
        )
        if checkpoint.user_input != user_message:
            return self._error(checkpoint, "checkpoint user input does not match")
        if confirmed:
            checkpoint.confirmed = True
        if self._needs_survey_clarification(checkpoint.user_input):
            checkpoint.status = LoopStatus.CLARIFICATION
            message = "请明确选择正射影像或倾斜摄影的测绘方式。"
            checkpoint.messages.append({"role": "assistant", "kind": "clarify", "content": message})
            return AgentRunResult(LoopStatus.CLARIFICATION, checkpoint, message=message)
        while checkpoint.step_count < checkpoint.max_steps:
            checkpoint.step_count += 1
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
                checkpoint.status = LoopStatus.FINAL
                checkpoint.messages.append({"role": "assistant", "kind": "final", "content": decision.message})
                return AgentRunResult(LoopStatus.FINAL, checkpoint, message=decision.message)
            if decision.kind == DecisionKind.CLARIFY:
                checkpoint.status = LoopStatus.CLARIFICATION
                checkpoint.messages.append({"role": "assistant", "kind": "clarify", "content": decision.message})
                return AgentRunResult(LoopStatus.CLARIFICATION, checkpoint, message=decision.message)
            call = decision.tool_call
            assert call is not None
            if not _json_safe(call.arguments):
                return self._error(checkpoint, "tool arguments must be JSON-serializable", call)
            checkpoint.tool_calls.append({"name": call.name, "arguments": dict(call.arguments)})
            definition = self._registry.get(call.name)
            try:
                errors = self._registry.validate(call)
            except Exception as exc:
                return self._error(checkpoint, f"invalid tool schema or arguments: {exc}", call)
            if errors:
                return self._error(checkpoint, "; ".join(errors), call)
            try:
                binding = _binding(checkpoint, call)
            except (TypeError, ValueError) as exc:
                return self._error(checkpoint, f"tool arguments cannot be bound safely: {exc}", call)
            if (definition.side_effect or definition.requires_confirmation) and (
                not checkpoint.confirmed or checkpoint.confirmation_binding != binding
            ):
                checkpoint.confirmation_binding = binding
                checkpoint.confirmed = False
                checkpoint.status = LoopStatus.CLARIFICATION
                message = f"请确认执行工具 {call.name}，参数：{json.dumps(dict(call.arguments), ensure_ascii=False, sort_keys=True)}"
                checkpoint.messages.append({"role": "assistant", "kind": "confirmation_required", "content": message})
                return AgentRunResult(LoopStatus.CLARIFICATION, checkpoint, message=message, tool_call=call)
            try:
                output = definition.handler(call.arguments)
            except Exception as exc:
                error = {"code": "tool_exception", "message": str(exc), "tool": call.name}
                checkpoint.last_error = error
                checkpoint.status = LoopStatus.TOOL_ERROR
                checkpoint.observations.append({"tool": call.name, "error": error})
                if not self._continue_on_tool_error:
                    return AgentRunResult(LoopStatus.TOOL_ERROR, checkpoint, tool_call=call, error=error)
                continue
            if _contains_unsafe_content(output) or not _json_safe(output):
                return self._error(checkpoint, "tool output is forbidden or not JSON-serializable", call)
            checkpoint.confirmed = False
            checkpoint.confirmation_binding = None
            checkpoint.executed_tool_calls.append({"name": call.name, "binding": binding})
            checkpoint.observations.append({"tool": call.name, "result": output})
            checkpoint.status = LoopStatus.OBSERVATION
            checkpoint.messages.append({"role": "tool", "name": call.name, "content": output})
            if checkpoint.step_count >= checkpoint.max_steps:
                break
        checkpoint.status = LoopStatus.MAX_STEPS_EXCEEDED
        return AgentRunResult(LoopStatus.MAX_STEPS_EXCEEDED, checkpoint, message="达到最大步骤数，已停止。")

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


def _binding(checkpoint: AgentCheckpoint, call: ToolCall) -> str:
    payload = json.dumps({"user_input": checkpoint.user_input, "context": dict(checkpoint.context), "tool": call.name,
                          "arguments": dict(call.arguments)}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json_safe(value: Any) -> bool:
    try:
        json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError, OverflowError):
        return False
    return True
