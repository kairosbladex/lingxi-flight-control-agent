"""Agent client for the zhuoyi-ai-platfrom flight-control API.

The executor owns the same preview/confirm contract as the simulator, but a confirmed step is
submitted to the Java service and polled until a terminal receipt. It never treats HTTP 2xx or
``submitted`` as device completion.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import hashlib
import json
import math
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

from .full_control import (
    FlightControlAction,
    FlightControlActionRequest,
    FlightControlReceipt,
    FlightControlStatus,
    RuntimeBinding,
    _CONTROL_FREE_ACTIONS,
)
from .models import ExecutionContext

STATE_SCHEMA = "lingxi-agent-flight-state/v1"
COMMAND_SCHEMA = "lingxi-agent-flight-control/v1"
_TERMINAL = frozenset({"failed", "unknown", "expired", "conflict", "cancelled", "rejected"})
_SUCCESS = frozenset({"accepted", "completed"})


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


class ZhuoyiPlatformError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ZhuoyiPlatformExecutor:
    """RuntimeExecutor for the Java Agent flight-control API."""

    execution_mode = "platform"
    supports_plan_execution = True

    def __init__(
        self,
        binding: RuntimeBinding,
        *,
        base_url: str,
        gateway_token: str,
        site_id: str,
        task_id: str | None = None,
        transport: Callable[[str, str, bytes | None, Mapping[str, str], float], tuple[int, Mapping[str, Any]]] | None = None,
        timeout: float = 5.0,
        receipt_timeout: float = 10.0,
        poll_interval: float = 0.2,
        preview_ttl_sec: float = 120.0,
        allow_insecure_http: bool = False,
        clock: Callable[[], float] = time.time,
    ):
        binding.validate()
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url is required")
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.query or parsed.fragment:
            raise ValueError("base_url must be an http(s) origin without query or fragment")
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme == "http" and not loopback and not allow_insecure_http:
            raise ValueError("plain HTTP is only allowed for loopback or explicitly insecure deployments")
        for value in (gateway_token, site_id):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("gateway_token and site_id are required")
        if task_id is not None and (not isinstance(task_id, str) or not task_id.strip()):
            raise ValueError("task_id must be null or a non-empty string")
        if timeout <= 0 or receipt_timeout <= 0 or poll_interval <= 0 or preview_ttl_sec <= 0:
            raise ValueError("timeouts must be positive")
        self.binding = binding
        self._base_url = base_url.rstrip("/")
        self._gateway_token = gateway_token
        self._site_id = site_id
        self._task_id = task_id
        self._transport = transport or self._http_transport
        self._timeout = float(timeout)
        self._receipt_timeout = float(receipt_timeout)
        self._poll_interval = float(poll_interval)
        self._preview_ttl = float(preview_ttl_sec)
        self._clock = clock
        self._pending: dict[str, dict[str, Any]] = {}
        self._last_state: Mapping[str, Any] | None = None

    # ---- RuntimeExecutor -------------------------------------------------

    @property
    def base_url(self) -> str:
        return self._base_url

    def runtime_context(self) -> ExecutionContext:
        execution_id = (f"{self.binding.project_id}:{self.binding.hangar_id}:"
                        f"{self.binding.task_batch_id}")
        return ExecutionContext(execution_id, self.binding.drone_id,
                                self.binding.context_version, True)

    def read_state(self) -> Mapping[str, Any]:
        state, error = self._state()
        if state is None:
            return {"execution_mode": "platform", "available": False,
                    "error_code": error or "AUTHORITATIVE_STATE_UNAVAILABLE"}
        owner = state.get("control_owner_employee_id")
        return {
            "execution_mode": "platform",
            "available": True,
            "snapshot_version": state.get("snapshot_version"),
            "control_owner": owner,
            "control_owned": owner == self.binding.actor_id,
            "online": state.get("online", False),
            "airborne": state.get("airborne"),
            "longitude": state.get("longitude"),
            "latitude": state.get("latitude"),
            "altitude_m": state.get("altitude_m"),
            "height_m": state.get("height_m"),
            "yaw_deg": state.get("yaw_deg"),
            "profile": state.get("profile"),
            "permissions": list(state.get("permissions") or []),
            "action_availability": dict(state.get("action_availability") or {}),
            "observed_at": state.get("observed_at"),
            "revision": state.get("snapshot_version"),
        }

    def preview(self, request: FlightControlActionRequest) -> FlightControlReceipt:
        request.validate()
        if request.context_version is not None and request.context_version != self.binding.context_version:
            return self._receipt(FlightControlStatus.STALE_CONTEXT, request, {},
                                 error_code="STALE_CONTEXT", message="上下文版本已经变化")
        action = FlightControlAction(request.action)
        state, error = self._state()
        if state is None:
            return self._receipt(FlightControlStatus.REJECTED, request, {},
                                 error_code=error or "AUTHORITATIVE_STATE_UNAVAILABLE",
                                 message="权威设备状态不可用")
        if action == FlightControlAction.READ_STATE:
            return self._receipt(FlightControlStatus.ACCEPTED, request, state,
                                 message="读取平台状态")
        if action not in _CONTROL_FREE_ACTIONS and state.get("control_owner_employee_id") != self.binding.actor_id:
            return self._receipt(FlightControlStatus.REJECTED, request, state,
                                 error_code="CONTROL_NOT_OWNED", message="请先获取平台控制权")
        availability = (state.get("action_availability") or {}).get(action.value) or {}
        if not availability.get("available") or availability.get("reasons"):
            return self._receipt(FlightControlStatus.REJECTED, request, state,
                                 error_code="ACTION_NOT_AVAILABLE",
                                 message="当前设备不允许该动作")
        snapshot_version = state.get("snapshot_version")
        if not isinstance(snapshot_version, str) or len(snapshot_version) != 64:
            return self._receipt(FlightControlStatus.REJECTED, request, state,
                                 error_code="SNAPSHOT_VERSION_UNAVAILABLE",
                                 message="权威快照版本不可用")
        confirmation_id = uuid4().hex
        confirmation_binding = self._binding_hash(request, snapshot_version)
        expires_at = self._clock() + self._preview_ttl
        self._pending[confirmation_id] = {
            "request": request,
            "snapshot_version": snapshot_version,
            "confirmation_binding": confirmation_binding,
            "expires_at": expires_at,
            "state": state,
        }
        return self._receipt(FlightControlStatus.PREVIEW_REQUIRED, request, state,
                             confirmation_id=confirmation_id,
                             confirmation_binding=confirmation_binding,
                             expires_at=datetime.fromtimestamp(expires_at, timezone.utc).isoformat(),
                             message="平台动作已预览，等待确认")

    def confirm(self, confirmation_id: str, *, command_id: str, idempotency_key: str,
                confirmation_binding: str, context_version: int) -> FlightControlReceipt:
        pending = self._pending.pop(confirmation_id, None)
        if pending is None:
            request = FlightControlActionRequest(command_id, idempotency_key,
                                                 FlightControlAction.READ_STATE, {}, context_version)
            return self._receipt(FlightControlStatus.REJECTED, request, {},
                                 error_code="CONFIRMATION_NOT_FOUND", message="确认卡不存在")
        request: FlightControlActionRequest = pending["request"]
        if (command_id != request.command_id or idempotency_key != request.idempotency_key
                or confirmation_binding != pending["confirmation_binding"]
                or context_version != self.binding.context_version):
            return self._receipt(FlightControlStatus.REJECTED, request, {},
                                 confirmation_id=confirmation_id,
                                 confirmation_binding=pending["confirmation_binding"],
                                 error_code="CONFIRMATION_MISMATCH", message="确认绑定不匹配")
        if self._clock() >= pending["expires_at"]:
            return self._receipt(FlightControlStatus.EXPIRED, request, {},
                                 confirmation_id=confirmation_id,
                                 confirmation_binding=pending["confirmation_binding"],
                                 error_code="CONFIRMATION_EXPIRED", message="确认卡已经过期")
        body = {
            "schema_version": COMMAND_SCHEMA,
            "command_id": request.command_id,
            "idempotency_key": request.idempotency_key,
            "trace_id": request.command_id,
            "action": request.action.value if isinstance(request.action, FlightControlAction) else request.action,
            "parameters": dict(request.parameters),
            "site_id": self._site_id,
            "hangar_id": self.binding.hangar_id,
            "drone_id": self.binding.drone_id,
            "task_id": self._task_id,
            "snapshot_version": pending["snapshot_version"],
            "expires_at": int(self._clock()) + 3,
        }
        status, payload = self._call("POST", "/api/v1/flight-control/commands", body)
        if status != 200:
            code = str(payload.get("code") or payload.get("error_code") or "PLATFORM_REJECTED")
            return self._receipt(FlightControlStatus.REJECTED, request, {},
                                 confirmation_id=confirmation_id,
                                 confirmation_binding=pending["confirmation_binding"],
                                 error_code=code, message="平台拒绝了本次命令")
        receipt = payload
        if not self._terminal(receipt):
            receipt = self._poll(request.command_id, receipt)
        java_status = str(receipt.get("status"))
        mapped = self._map_status(java_status)
        if mapped is FlightControlStatus.REJECTED and java_status == "submitted":
            return self._receipt(FlightControlStatus.REJECTED, request, self.read_state(),
                                 confirmation_id=confirmation_id,
                                 confirmation_binding=pending["confirmation_binding"],
                                 error_code="RECEIPT_PENDING",
                                 message="平台已受理但未取得终态，禁止重复确认")
        return self._receipt(mapped, request, self.read_state(),
                             confirmation_id=confirmation_id,
                             confirmation_binding=pending["confirmation_binding"],
                             error_code=receipt.get("error_code") or (None if mapped in {FlightControlStatus.ACCEPTED, FlightControlStatus.DUPLICATE} else java_status),
                             message=f"平台回执：{java_status}；设备效果未验证")

    def cancel(self, confirmation_id: str, *, command_id: str, idempotency_key: str,
               confirmation_binding: str, context_version: int) -> FlightControlReceipt:
        pending = self._pending.pop(confirmation_id, None)
        if pending is None:
            request = FlightControlActionRequest(command_id, idempotency_key,
                                                 FlightControlAction.READ_STATE, {}, context_version)
            return self._receipt(FlightControlStatus.REJECTED, request, {},
                                 error_code="CONFIRMATION_NOT_FOUND", message="确认卡不存在")
        request = pending["request"]
        return self._receipt(FlightControlStatus.CANCELLED, request, self.read_state(),
                             confirmation_id=confirmation_id,
                             confirmation_binding=pending["confirmation_binding"],
                             message="平台动作已取消，未提交")

    # ---- HTTP and mapping -------------------------------------------------

    def _state(self) -> tuple[Mapping[str, Any] | None, str | None]:
        query = {"siteId": self._site_id, "hangarId": self.binding.hangar_id,
                 "droneId": self.binding.drone_id}
        if self._task_id is not None:
            query["taskId"] = self._task_id
        status, payload = self._call("GET", "/api/v1/flight-control/state", query=query)
        if status != 200:
            return None, str(payload.get("code") or payload.get("error_code") or "AUTHORITATIVE_STATE_UNAVAILABLE")
        if payload.get("schema_version") != STATE_SCHEMA:
            return None, "AUTHORITATIVE_STATE_UNAVAILABLE"
        self._last_state = payload
        return payload, None

    def _poll(self, command_id: str, receipt: Mapping[str, Any]) -> Mapping[str, Any]:
        deadline = self._clock() + self._receipt_timeout
        while self._clock() < deadline:
            if receipt.get("continuation_allowed") or str(receipt.get("status")) in _TERMINAL:
                return receipt
            time.sleep(self._poll_interval)
            status, payload = self._call(
                "GET", "/api/v1/flight-control/receipt", query={"commandId": command_id})
            if status != 200:
                return {"status": "unknown", "error_code": str(payload.get("code") or "RECEIPT_QUERY_FAILED")}
            receipt = payload
        return receipt

    @staticmethod
    def _terminal(receipt: Mapping[str, Any]) -> bool:
        return bool(receipt.get("continuation_allowed")) or str(receipt.get("status")) in _TERMINAL

    @staticmethod
    def _map_status(status: str) -> FlightControlStatus:
        if status in _SUCCESS:
            return FlightControlStatus.ACCEPTED
        if status == "cancelled":
            return FlightControlStatus.CANCELLED
        if status == "expired":
            return FlightControlStatus.EXPIRED
        return FlightControlStatus.REJECTED

    def _receipt(self, status: FlightControlStatus, request: FlightControlActionRequest,
                 state: Mapping[str, Any], *, confirmation_id: str | None = None,
                 confirmation_binding: str | None = None, error_code: str | None = None,
                 message: str = "", expires_at: str | None = None) -> FlightControlReceipt:
        action = request.action.value if isinstance(request.action, FlightControlAction) else str(request.action)
        return FlightControlReceipt(
            status=status,
            execution_mode="platform",
            action=action,
            command_id=request.command_id,
            idempotency_key=request.idempotency_key,
            confirmation_id=confirmation_id,
            confirmation_binding=confirmation_binding,
            context_version=self.binding.context_version,
            state=dict(state),
            error_code=error_code,
            message=message,
            expires_at=expires_at,
        )

    def _binding_hash(self, request: FlightControlActionRequest, snapshot_version: str) -> str:
        payload = {
            "binding": self.binding.to_dict(),
            "request": request.to_dict(),
            "snapshot_version": snapshot_version,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _call(self, method: str, path: str, body: Mapping[str, Any] | None = None, *,
              query: Mapping[str, str] | None = None) -> tuple[int, Mapping[str, Any]]:
        url = self._base_url + path
        if query:
            url += "?" + urlencode(query)
        encoded = None
        headers = {
            "Authorization": f"Bearer {self._gateway_token}",
            "X-Project-Id": self.binding.project_id,
            "X-Actor-Id": self.binding.actor_id,
            "Accept": "application/json",
        }
        if body is not None:
            encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            status, payload = self._transport(method, url, encoded, headers, self._timeout)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            return 0, {"error_code": "PLATFORM_UNAVAILABLE"}
        if not isinstance(payload, Mapping):
            return status, {"error_code": "INVALID_PLATFORM_RESPONSE"}
        return status, payload

    @staticmethod
    def _http_transport(method: str, url: str, body: bytes | None,
                        headers: Mapping[str, str], timeout: float) -> tuple[int, Mapping[str, Any]]:
        request = Request(url, data=body, headers=dict(headers), method=method)
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(request, timeout=timeout) as response:
                raw = response.read()
                status = response.status
        except HTTPError as error:
            status = error.code
            raw = error.read()
        payload = json.loads(raw.decode("utf-8")) if raw else {}
        return status, payload
