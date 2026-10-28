"""模型理解、位置预览和确认。此模块没有平台网络传输或真实执行入口。"""

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import threading
import time
from uuid import uuid4

from .agent import BoundedAgentLoop, ToolRegistry
from .full_control import RuntimeBinding
from .geometry import relative_target
from .models import ExecutionContext, ToolDefinition, ToolParameter, ToolSchema

PLATFORM_PATH = "/screen/uav/operation/control"
MOVE_TYPES = {"move_forward": 202, "move_backward": 203, "move_left": 204, "move_right": 205}
PREVIEW_SYSTEM_PROMPT = (
    "你是飞控预览 Agent。只规划用户明确要求的单个水平相对移动，前后左右均以机头为参考。"
    "只允许调用提供的工具；不能执行无人机，不能声称已移动或已下发。"
    "距离必须由用户明确给出，单位为米，范围大于0且不超过20米；不能猜数、截断或拆分超限距离。"
    "缺少距离、含糊的距离、否定、条件句、询问能力或多个动作都必须先澄清，不能调用移动工具。"
    "例如往前飞10米对应move_forward，distance_m为10。"
    "本轮不申请控制权、不起飞、不调整高度，不执行后续动作。"
)


class StaleReferencePose(ValueError):
    """参考位姿已过期。

    这是**已知、可预期、可重试**的状态（模型推理比位姿有效期还长时就会发生），
    不是服务端内部故障：HTTP 层必须回 ``STALE_POSE``，不能落到 500 兜底。
    """


@dataclass(frozen=True)
class ReferencePose:
    longitude: float
    latitude: float
    altitude_m: float
    heading_deg: float
    observed_at: float

    def validate(self, now: float) -> None:
        if any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v)
               for v in self.__dict__.values()):
            raise ValueError("缺少有效的位置、高度、航向或接收时间")
        if not -180 <= self.longitude <= 180 or not -90 <= self.latitude <= 90:
            raise ValueError("位置超出范围")
        if not -180 <= self.heading_deg <= 360:
            raise ValueError("航向超出范围")
        if not 0 <= now - self.observed_at <= 15:
            raise StaleReferencePose("位置参考已过期，请等待当前设备的新状态")


def platform_request(binding: RuntimeBinding, site_id: str, task_id: str | None,
                     action: str, distance_m: float) -> dict:
    """编译已有 Java DTO；机库 ID 与站点 ID、任务批次与任务 ID 不混用。"""
    binding.validate()
    if not isinstance(site_id, str) or not site_id.strip():
        raise ValueError("缺少平台站点 ID")
    if action not in MOVE_TYPES:
        raise ValueError("当前预览仅支持水平相对移动")
    if isinstance(distance_m, bool) or not isinstance(distance_m, (int, float)) or not math.isfinite(distance_m) or not 0 < distance_m <= 20:
        raise ValueError("平台距离模式仅接受大于0且不超过20米")
    body = {"siteId": site_id, "terminalId": binding.drone_id,
            "type": MOVE_TYPES[action], "mode": 2, "param": distance_m}
    if task_id:
        body["taskId"] = task_id
    return {"method": "POST", "path": PLATFORM_PATH, "body": body}


def _fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     allow_nan=False).encode()).hexdigest()


class FlightPreviewAgent:
    """每个已认证操作者和设备上下文一份实例；确认只记录审阅结果。"""

    def __init__(self, binding: RuntimeBinding, decider, *, clock=time.time, ttl_sec=120):
        binding.validate()
        self.binding = binding
        self.decider = decider
        self.clock, self.ttl_sec = clock, ttl_sec
        self._cards: dict[str, dict] = {}
        self._requests: dict[str, tuple[str, dict]] = {}
        self._lock = threading.RLock()

    def _registry(self):
        runtime = ExecutionContext(f"{self.binding.project_id}:{self.binding.hangar_id}:{self.binding.task_batch_id}",
                                   self.binding.drone_id, self.binding.context_version, True)
        registry = ToolRegistry(runtime_context_provider=lambda: runtime)
        for name, direction in zip(MOVE_TYPES, ("前", "后", "左", "右")):
            registry.register(ToolDefinition(
                name, f"按机头方向向{direction}平移指定米数，仅供预览",
                ToolSchema(properties={"distance_m": ToolParameter("number", minimum=0, maximum=20,
                                                                   exclusive_minimum=True)},
                           required=("distance_m",)),
                self._never_execute, side_effect=True, requires_confirmation=True))
        return registry

    @staticmethod
    def _never_execute(_arguments):
        raise RuntimeError("真实指令发送已禁用")

    @staticmethod
    def _envelope(**fields):
        return {"execution_mode": "preview", "dispatch_allowed": False, "dispatched": False, **fields}

    def preview(self, text: str, *, command_id: str, idempotency_key: str,
                pose: ReferencePose, site_id: str, task_id: str | None = None) -> dict:
        # 仅在这一设备实例内串行理解；不同操作者的模型请求互不阻塞。
        with self._lock:
            now = self.clock()
            self._prune(now)
            pose.validate(now)
            if not command_id.strip() or not idempotency_key.strip():
                raise ValueError("缺少请求标识")
            fingerprint = _fingerprint([text, command_id, pose.__dict__, site_id, task_id])
            previous = self._requests.get(idempotency_key)
            if previous:
                if previous[0] != fingerprint:
                    raise ValueError("同一请求标识不能用于不同指令或位置")
                return deepcopy(previous[1])
            self._cards.clear()
            self._requests.clear()
            loop = BoundedAgentLoop(self.decider, self._registry(), max_steps=3)
            result = loop.run(text, context={"reference": "nose", "distance_limit_m": 20})
            if result.tool_call is None or result.checkpoint.confirmation_binding is None:
                message = result.message if result.status.value == "clarification" else "模型未生成有效的移动预览，请重试或明确方向和距离。"
                return self._envelope(status="clarification", message=message,
                                      agent_status=result.status.value)
            call = result.tool_call
            pose.validate(self.clock())
            distance = call.arguments["distance_m"]
            request = platform_request(self.binding, site_id, task_id, call.name, distance)
            longitude, latitude = relative_target(pose.longitude, pose.latitude, pose.heading_deg,
                                                   call.name.removeprefix("move_"), distance)
            card = self._envelope(
                status="preview_required", action=call.name, parameters=dict(call.arguments),
                command_id=command_id, idempotency_key=idempotency_key,
                confirmation_id=uuid4().hex, context_version=self.binding.context_version,
                reference_frame="body", pose_source="displayed_reference",
                origin=dict(pose.__dict__),
                target={"longitude": longitude, "latitude": latitude, "altitude_m": pose.altitude_m},
                platform_request=request, expires_at=self.clock() + self.ttl_sec,
                message="已生成移动预览，确认后仅记录结果，不下发无人机指令。")
            # 绑定身份、模型解释、参数、参考位置和待发送内容；浏览器不能替换任何一项。
            card["confirmation_binding"] = _fingerprint([self.binding.to_dict(), text,
                                                          result.checkpoint.confirmation_binding, card])
            self._cards[card["confirmation_id"]] = card
            self._requests[idempotency_key] = (fingerprint, card)
            return deepcopy(card)

    def _prune(self, now):
        self._cards = {key: card for key, card in self._cards.items() if card["expires_at"] > now}
        self._requests = {key: value for key, value in self._requests.items()
                          if value[1]["confirmation_id"] in self._cards}

    def confirm(self, confirmation_id: str, *, command_id: str, idempotency_key: str,
                confirmation_binding: str, context_version: int, cancel=False) -> dict:
        with self._lock:
            card = self._cards.get(confirmation_id)
            if card is None:
                raise ValueError("确认卡不存在或已被新的意图替代")
            if context_version != self.binding.context_version:
                raise ValueError("设备上下文已经变化，请重新预览")
            if any(card[key] != value for key, value in {
                "command_id": command_id, "idempotency_key": idempotency_key,
                "confirmation_binding": confirmation_binding}.items()):
                raise ValueError("确认与原预览不一致")
            if self.clock() >= card["expires_at"]:
                raise ValueError("确认卡已过期，请重新预览")
            if card["status"] == "preview_required":
                card["status"] = "cancelled" if cancel else "confirmed_not_dispatched"
                card["message"] = "已取消预览。" if cancel else "已确认，未下发。仅记录本次预览。"
            return deepcopy(card)
