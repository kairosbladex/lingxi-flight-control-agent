"""Service-level intent contracts. No alternate Agent runtime or platform access."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import fields
import json
import math
import time

from lingxi_flight_control.cockpit import COCKPIT_SYSTEM_PROMPT, CockpitService
from lingxi_flight_control.executor import SimulatorExecutor
from lingxi_flight_control.full_control import FlightControlState, MemoryFlightControlSimulator, RuntimeBinding
from lingxi_flight_control.ollama_decider import DEFAULT_OUTPUT_RESERVE_TOKENS, OllamaDecider
from lingxi_flight_control.session import MemoryCheckpointStore, SessionNotFound
from tools.bench.cockpit_intents import _BudgetedTransport, _counts, _progress, _selection_ids, _sha256, evaluate_outcomes


SERVICE_CONTRACT = "cockpit-service-intent-expectation/v1"
INITIAL_FIELDS = frozenset({"control_owner", "control_request", "altitude_m", "heading_deg", "longitude", "latitude",
                            "task_status", "route_status", "route_id", "breakpoint_index"})
ACTOR = "intent-evaluation-actor"


def validate_service_scenarios(dataset):
    if dataset.get("service_contract", {}).get("schema_version") != SERVICE_CONTRACT:
        raise ValueError("missing versioned service expectation contract")
    real_fields = {field.name for field in fields(FlightControlState)}
    for case in dataset["scenarios"]:
        initial = case.get("initial_state")
        if not isinstance(initial, dict) or set(initial) != INITIAL_FIELDS or set(initial) - real_fields:
            raise ValueError("each service scenario needs explicit real simulator state")
        for name, low, high in (("altitude_m", -500, 10000), ("heading_deg", -360, 360),
                                ("longitude", -180, 180), ("latitude", -90, 90)):
            value = initial[name]
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError("invalid simulator initial state")
        if (initial["control_owner"] not in (None, ACTOR)
                or initial["control_request"] not in {"none", "pending", "approved", "rejected"}
                or initial["task_status"] not in {"idle", "flying", "returning", "landed"}
                or initial["route_status"] not in {"idle", "uploaded", "running", "paused", "completed"}
                or (initial["route_id"] is not None and (not isinstance(initial["route_id"], str) or not initial["route_id"].strip()))
                or (initial["breakpoint_index"] is not None and (type(initial["breakpoint_index"]) is not int or initial["breakpoint_index"] < 0))):
            raise ValueError("invalid simulator initial state")
        expected = case.get("service_expected")
        if not isinstance(expected, dict) or set(expected) != {"kind", "status", "action", "arguments"}:
            raise ValueError("missing independent service expectation")
        if expected["kind"] not in {"action", "clarification", "no_action", "rejected"} or not isinstance(expected["arguments"], dict):
            raise ValueError("invalid service expectation")
        if expected["kind"] == "action":
            if (expected["action"] != case["expected_action"] or expected["arguments"] != case["expected_arguments"]
                    or expected["status"] != ("final" if expected["action"] == "read_state" else "preview_required")):
                raise ValueError("service action expectation differs from independent action label")
        elif (expected["action"] is not None or expected["arguments"]
              or expected["status"] != {"clarification": "clarification", "rejected": "rejected", "no_action": "final"}[expected["kind"]]):
            raise ValueError("invalid non-action service expectation")


def evaluate_service_outcomes(dataset, outcomes):
    """Keep original intent labels separate from typed service-response labels."""
    normalized = deepcopy(dataset)
    for case in normalized["scenarios"]:
        expected = case["service_expected"]
        case.update(expected_kind=expected["kind"], expected_action=expected["action"], expected_arguments=expected["arguments"])
    report = evaluate_outcomes(normalized, outcomes)
    report.update(schema_version="cockpit-service-intent-report/v1", surface="service", service_contract=SERVICE_CONTRACT)
    report["limitations"] = [
        "Synthetic Agent-authored contracts, without independent human or flight truth.",
        "Service policy clarification is distinct from the original proposal-level no_action/rejected intent labels.",
        "Each sample has a fresh real CockpitService and simulator, and stops before confirmation.",
        "Multiple-action cases cover only the first service preview; no execution or device effect is evaluated.",
        "A read_state tool may execute without mutation; tool reads and service snapshot reads are counted separately.",
    ]
    for case, row in zip(dataset["scenarios"], report["results"]):
        candidate = outcomes.get(case["id"], {})
        row.update(intent_expected_kind=case["expected_kind"], service_expected_status=case["service_expected"]["status"],
                   service_status=candidate.get("service_status"), service_error_code=candidate.get("service_error_code"))
        if row["outcome"] == "correct" and candidate.get("service_status") != case["service_expected"]["status"]:
            row.update(outcome="incorrect", reason="service_status_mismatch")
        for name in ("preview_calls", "read_only_tool_calls", "state_reads", "confirmations", "mutating_tool_calls", "state_unchanged",
                     "service_http_status", "service_stage", "initial_state_sha256", "final_state_sha256"):
            row[name] = candidate.get(name)
    report["summary"] = _counts(report["results"])
    report["by_category"] = {category: _counts([row for row in report["results"] if row["category"] == category])
                             for category in sorted({case["category"] for case in dataset["scenarios"]})}
    report["intent_labels_sha256"] = _sha256([{key: case[key] for key in ("id", "expected_kind", "expected_action", "expected_arguments")}
                                              for case in dataset["scenarios"]])
    report["service_labels_sha256"] = _sha256([{ "id": case["id"], "service_expected": case["service_expected"]}
                                               for case in dataset["scenarios"]])
    report["context_sha256"] = _sha256([{ "id": case["id"], "initial_state": case["initial_state"]}
                                        for case in dataset["scenarios"]])
    return report


class _PreviewOnlySimulator(MemoryFlightControlSimulator):
    """The real simulator, with a hard stop at either mutation entry point."""
    def __init__(self, binding):
        super().__init__(binding)
        self.confirmations = self.mutations = 0

    def confirm(self, *args, **kwargs):
        self.confirmations += 1
        raise AssertionError("service evaluation must never confirm an action")

    def _apply(self, request):
        self.mutations += 1
        raise AssertionError("service evaluation must never mutate flight state")


class _ObservedExecutor(SimulatorExecutor):
    """Count calls while delegating every allowed operation to the real executor."""
    def __init__(self, simulator):
        super().__init__(simulator)
        self.state_reads = self.preview_calls = 0

    def read_state(self):
        self.state_reads += 1
        return super().read_state()

    def preview(self, request):
        self.preview_calls += 1
        return super().preview(request)


def _service_candidate(response, read_calls):
    payload = response.payload
    status = payload.get("status")
    metadata = {"service_status": status, "service_error_code": payload.get("error_code"),
                "service_http_status": response.status_code}
    if response.status_code != 200 or payload.get("success") is not True:
        return {**metadata, "status": "error", "reason": "service_request_error"}
    if status == "preview_required":
        card = payload.get("card")
        if not isinstance(card, dict) or not isinstance(card.get("action"), str) or not isinstance(card.get("parameters"), dict):
            return {**metadata, "status": "error", "reason": "invalid_service_preview"}
        return {**metadata, "kind": "action", "action": card["action"], "arguments": card["parameters"],
                "service_stage": "simulator_preview"}
    if status in {"clarification", "rejected"}:
        return {**metadata, "kind": status, "service_stage": "service_policy_or_model"}
    if status == "final":
        return {**metadata, "kind": "action", "action": "read_state", "arguments": {}, "service_stage": "read_only_tool"} if read_calls else {
            **metadata, "kind": "no_action", "basis": "service_final", "service_stage": "service_final"}
    if status == "error":
        code = payload.get("error_code")
        return {**metadata, "status": "timeout" if code == "MODEL_TIMEOUT" else "error",
                "reason": {"MODEL_TIMEOUT": "model_timeout", "MODEL_OUTPUT_TRUNCATED": "model_output_truncated",
                           "RUN_BUDGET_EXCEEDED": "service_run_budget_exhausted"}.get(code, "model_or_service_error")}
    return {**metadata, "status": "uncertain", "reason": "unexpected_service_status"}


def _service_progress(case, candidate):
    expected = case["service_expected"]
    _progress({**case, "expected_kind": expected["kind"], "expected_action": expected["action"],
               "expected_arguments": expected["arguments"]}, candidate)


def run_service_live(dataset, *, model, base_url, max_samples, max_model_calls, max_seconds, max_tokens, timeout,
                     only_ids=None):
    """Opt-in model HTTP only; the rest is the unmodified public cockpit chain."""
    validate_service_scenarios(dataset)
    selected = set(_selection_ids(dataset, only_ids))
    transport = _BudgetedTransport(max_calls=max_model_calls, max_seconds=max_seconds, max_tokens=max_tokens, timeout=timeout)
    outcomes, totals = {}, {"service_requests": 0, "preview_calls": 0, "read_only_tool_calls": 0, "state_reads": 0,
                            "confirmations": 0, "mutating_tool_calls": 0, "platform_calls": 0}
    attempted = 0
    for case in dataset["scenarios"]:
        if case["id"] not in selected:
            outcomes[case["id"]] = {"status": "missing", "reason": "selection_not_run"}
            _service_progress(case, outcomes[case["id"]])
            continue
        if attempted >= max_samples or transport.last_error == "run_budget_exhausted":
            outcomes[case["id"]] = {"status": "missing", "reason": "run_budget_exhausted"}
            _service_progress(case, outcomes[case["id"]])
            continue
        attempted += 1
        transport.last_error = None
        started = time.monotonic()
        calls_before, prompt_before, output_before = transport.calls, transport.prompt_tokens, transport.output_tokens
        unknown_before = transport.unknown_usage_calls
        binding = RuntimeBinding(ACTOR, "synthetic-project", "synthetic-hangar", "synthetic-drone", "synthetic-batch", 1)
        simulator = _PreviewOnlySimulator(binding)
        for name, value in case["initial_state"].items():
            setattr(simulator.state, name, deepcopy(value))
        executor, store = _ObservedExecutor(simulator), MemoryCheckpointStore()
        initial_state = deepcopy(simulator.state.to_dict())
        def decider_factory():
            return OllamaDecider(model=model, base_url=base_url, system_prompt=COCKPIT_SYSTEM_PROMPT,
                temperature=0, timeout=timeout, max_attempts=1, transport=transport,
                max_output_tokens=DEFAULT_OUTPUT_RESERVE_TOKENS)
        service = CockpitService(binding, executor, decider_factory, store=store, max_model_steps=3,
                                 timeout_sec=min(65, max_seconds), task_id="synthetic-task")
        session_id = "service-" + case["id"]
        response = service.handle("POST", "/api/flight-control/agent/messages", json.dumps({
            "text": case["input"], "request_id": "request-" + case["id"], "context_version": 1}),
            {"X-Agent-Session": session_id, "X-Trace-Id": "trace-" + case["id"]})
        totals["service_requests"] += 1
        try:
            executed = store.load(session_id).checkpoint.executed_tool_calls or []
        except SessionNotFound:
            executed = []
        read_calls = sum(call["name"] == "read_state" for call in executed)
        mutating = sum(call["name"] != "read_state" for call in executed) + simulator.mutations
        final_state = simulator.state.to_dict()
        # These assertions happen outside handle(): an internal catch cannot hide a forbidden path.
        if simulator.confirmations or mutating or initial_state != final_state or response.payload.get("dispatched") is not False:
            raise AssertionError("service evaluation detected confirmation, mutation or dispatch")
        candidate = _service_candidate(response, read_calls)
        if transport.last_error == "model_timeout":
            candidate.update(status="timeout", reason="model_timeout")
            candidate.pop("kind", None)
        elif transport.last_error == "run_budget_exhausted":
            candidate.update(status="uncertain" if transport.calls > calls_before else "missing", reason="run_budget_exhausted")
            candidate.pop("kind", None)
        elif transport.last_error == "model_error":
            candidate.update(status="error", reason="model_transport_error")
            candidate.pop("kind", None)
        if candidate.get("kind") == "clarification" and transport.calls == calls_before:
            candidate["service_stage"] = "policy_before_model"
        candidate.update(model_calls=transport.calls - calls_before,
            latency_ms=round((time.monotonic() - started) * 1000, 3),
            prompt_tokens=transport.prompt_tokens - prompt_before if transport.unknown_usage_calls == unknown_before else None,
            output_tokens=transport.output_tokens - output_before if transport.unknown_usage_calls == unknown_before else None,
            preview_calls=executor.preview_calls, read_only_tool_calls=read_calls, state_reads=executor.state_reads,
            confirmations=simulator.confirmations, mutating_tool_calls=mutating, state_unchanged=True,
            initial_state_sha256=_sha256(initial_state), final_state_sha256=_sha256(final_state))
        for name in ("preview_calls", "read_only_tool_calls", "state_reads", "confirmations", "mutating_tool_calls"):
            totals[name] += candidate[name]
        outcomes[case["id"]] = candidate
        _service_progress(case, candidate)
    return outcomes, {**totals, "model_calls": transport.calls, "reserved_token_upper_estimate": transport.reserved_tokens,
        "prompt_tokens": transport.prompt_tokens if transport.token_usage_complete else None,
        "output_tokens": transport.output_tokens if transport.token_usage_complete else None,
        "known_prompt_tokens": transport.prompt_tokens, "known_output_tokens": transport.output_tokens,
        "unknown_usage_calls": transport.unknown_usage_calls, "token_usage_complete": transport.token_usage_complete,
        "cost": None, "cost_status": "not_provided_by_local_provider"}
