"""运行时执行器适配层测试：execution_mode 透传、fail-closed、报文夹具回放。

这些测试不碰真机：`FixtureExecutor` 回放的是**夹具文件**，而夹具的来源由
`source` 字段自证（当前只有 `simulator-export`，即由模拟器回执导出的报文形状）。
任何「夹具里没有 → 合成成功」的路径都必须失败，见
`test_missing_recording_fails_closed_and_never_confirms`。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from lingxi_flight_control import (
    BoundedAgentLoop,
    DecisionKind,
    FixtureError,
    FixtureExecutor,
    FlightControlAction,
    FlightControlActionRequest,
    FlightControlReceipt,
    FlightControlStatus,
    LoopStatus,
    LoopbackExecutor,
    MemoryFlightControlSimulator,
    ModelDecision,
    RuntimeBinding,
    RuntimeExecutor,
    SimulatorExecutor,
    ToolCall,
    action_manifest,
    action_tool_definitions,
    build_full_control_tool_registry,
    coerce_executor,
    export_fixture,
    load_fixture,
    validate_execution_mode,
)

ACQUIRE = FlightControlAction.CONTROL_ACQUIRE.value
MOVE_FORWARD = FlightControlAction.MOVE_FORWARD.value
CAMERA_PHOTO = FlightControlAction.CAMERA_PHOTO.value
READ_STATE = FlightControlAction.READ_STATE.value

_FIXTURE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "cockpit_basic.json")
#: 驾驶舱基线口令的动作序列，夹具与 e2e 必须覆盖同一串。
_COCKPIT_SEQUENCE = ((ACQUIRE, {}), (MOVE_FORWARD, {"distance_m": 10}), (CAMERA_PHOTO, {}), (READ_STATE, {}))
_RECEIPT_FIELDS = ("status", "action", "execution_mode", "context_version", "state",
                   "error_code", "message")


def binding(version: int = 1) -> RuntimeBinding:
    return RuntimeBinding("actor-1", "project-1", "hangar-1", "drone-1", "batch-1", version)


def request(action, parameters=None, sequence=1, context_version=1) -> FlightControlActionRequest:
    return FlightControlActionRequest(f"cmd-{sequence}", f"key-{sequence}", action,
                                      dict(parameters or {}), context_version)


def drive(executor, requests):
    """按 preview → confirm 两阶段驱动执行器，返回每步回执的字典。"""
    receipts = []
    for item in requests:
        card = executor.preview(item)
        if card.status is FlightControlStatus.PREVIEW_REQUIRED:
            card = executor.confirm(
                card.confirmation_id,
                command_id=item.command_id,
                idempotency_key=item.idempotency_key,
                confirmation_binding=card.confirmation_binding,
                context_version=item.context_version,
            )
        receipts.append(card.to_dict())
    return receipts


def call(name: str, **arguments) -> ModelDecision:
    return ModelDecision(DecisionKind.TOOL_CALL, tool_call=ToolCall(name, arguments))


def final(message: str = "完成") -> ModelDecision:
    return ModelDecision(DecisionKind.FINAL, message=message)


class ScriptedDecider:
    """按脚本依次返回决定；待确认的调用在未执行前会被重发（与模型契约一致）。"""

    def __init__(self, decisions):
        self._decisions = list(decisions)
        self._pending = None
        self._pending_at = None

    def decide(self, _user_message, checkpoint, _tools):
        observed = len(checkpoint.observations)
        if self._pending is not None and observed == self._pending_at:
            return self._pending
        self._pending = None
        if not self._decisions:
            return final()
        decision = self._decisions.pop(0)
        if decision.tool_call is not None:
            self._pending = decision
            self._pending_at = observed
        return decision


def drive_loop(loop, command: str, *, max_rounds: int = 12):
    checkpoint = None
    result = None
    for _ in range(max_rounds):
        result = loop.run(command, checkpoint=checkpoint, confirmed=checkpoint is not None)
        checkpoint = result.checkpoint
        if result.status is not LoopStatus.CLARIFICATION:
            return result
    return result


class SimulatorExecutorEquivalenceTests(unittest.TestCase):
    def test_simulator_executor_reports_simulator_mode(self):
        executor = SimulatorExecutor(MemoryFlightControlSimulator(binding(), clock=lambda: 100.0))
        self.assertIsInstance(executor, RuntimeExecutor)
        self.assertEqual(executor.execution_mode, "simulator")
        self.assertEqual(executor.read_state()["execution_mode"], "simulator")

    def test_simulator_executor_is_field_by_field_identical_to_the_simulator(self):
        """零行为变化：包一层之后回执逐字段一致，状态也一致。"""
        sequence = [request(ACQUIRE), request(MOVE_FORWARD, {"distance_m": 10}, sequence=2)]
        direct = MemoryFlightControlSimulator(binding(), clock=lambda: 100.0)
        wrapped = SimulatorExecutor(MemoryFlightControlSimulator(binding(), clock=lambda: 100.0))

        direct_receipts = drive(direct, sequence)
        wrapped_receipts = drive(wrapped, sequence)

        for index, (left, right) in enumerate(zip(direct_receipts, wrapped_receipts)):
            with self.subTest(step=index):
                self.assertEqual({k: left[k] for k in _RECEIPT_FIELDS},
                                 {k: right[k] for k in _RECEIPT_FIELDS})
        self.assertEqual(direct.state.to_dict(), wrapped.read_state())
        self.assertEqual(direct.snapshot(), {"execution_mode": "simulator",
                                             "binding": binding().to_dict(),
                                             "state": wrapped.read_state()})

    def test_build_registry_accepts_bare_simulator_and_any_runtime_executor(self):
        simulator = MemoryFlightControlSimulator(binding())
        for backend in (simulator, SimulatorExecutor(simulator), LoopbackExecutor(binding()),
                        FixtureExecutor(export_fixture(binding(), _COCKPIT_SEQUENCE))):
            with self.subTest(backend=type(backend).__name__):
                registry = build_full_control_tool_registry(backend)
                self.assertEqual(len(registry.definitions()), len(FlightControlAction))
                self.assertEqual(registry.runtime_context(),
                                 coerce_executor(backend).runtime_context())

    def test_build_registry_rejects_a_backend_that_is_not_an_executor(self):
        with self.assertRaises(TypeError):
            build_full_control_tool_registry(object())


class ExecutionModePassthroughTests(unittest.TestCase):
    def test_mode_flows_from_executor_to_receipts_state_and_contract(self):
        executor = LoopbackExecutor(binding())
        registry = build_full_control_tool_registry(executor)
        loop = BoundedAgentLoop(
            ScriptedDecider([call(ACQUIRE), call(MOVE_FORWARD, distance_m=10), final("已接管并前进10米")]),
            registry)
        result = drive_loop(loop, "接管控制权并向前飞10米")

        self.assertIs(result.status, LoopStatus.FINAL)
        receipts = [observation["result"] for observation in result.checkpoint.observations]
        self.assertEqual([receipt["execution_mode"] for receipt in receipts],
                         ["loopback", "loopback"])
        # 状态与契约都必须跟着执行器走，否则 UI/回放层会看到互相矛盾的模式。
        self.assertEqual(executor.read_state()["execution_mode"], "loopback")
        self.assertEqual(action_manifest("loopback")["execution_mode"], "loopback")
        self.assertTrue(all(item["execution_mode"] == "loopback"
                            for item in action_tool_definitions("loopback")))
        # 默认值不变：没注入执行器时仍然是 simulator。
        self.assertEqual(action_manifest()["execution_mode"], "simulator")
        self.assertTrue(all(item["execution_mode"] == "simulator"
                            for item in action_tool_definitions()))

    def test_rebinding_the_runtime_keeps_the_executor_mode(self):
        """换绑会重置状态；执行模式属于执行后端，不能跟着掉回 simulator。"""
        executor = LoopbackExecutor(binding())
        executor.simulator.bind(RuntimeBinding("actor-1", "project-1", "hangar-1",
                                               "drone-2", "batch-2", 2))
        self.assertEqual(executor.execution_mode, "loopback")
        self.assertEqual(executor.read_state()["execution_mode"], "loopback")
        receipts = drive(executor, [request(ACQUIRE, context_version=2)])
        self.assertEqual(receipts[0]["status"], FlightControlStatus.ACCEPTED.value)
        self.assertEqual(receipts[0]["execution_mode"], "loopback")

    def test_loopback_mode_cannot_smuggle_a_forbidden_term(self):
        """execution_mode 会随回执回给模型；含禁用词会让每个动作硬失败，构建期就拦住。"""
        for bad in ("mqtt", "Mqtt-live", "credential", "飞控报文"):
            with self.subTest(mode=bad):
                with self.assertRaises(ValueError):
                    validate_execution_mode(bad)
        self.assertEqual(validate_execution_mode("loopback"), "loopback")
        self.assertEqual(validate_execution_mode("recorded"), "recorded")
        with self.assertRaises(ValueError):
            SimulatorExecutor(MemoryFlightControlSimulator(binding()), execution_mode="mqtt")

    def test_execution_mode_must_be_a_simple_non_empty_token(self):
        for bad in ("", "  ", " simulator", "simulator ", "SIMULATOR", 7, None, "a b"):
            with self.subTest(mode=bad):
                with self.assertRaises(ValueError):
                    validate_execution_mode(bad)


class ExecutorFailClosedTests(unittest.TestCase):
    def test_executor_exception_becomes_tool_error(self):
        class ExplodingExecutor(LoopbackExecutor):
            def preview(self, request):
                raise RuntimeError("platform link down")

        registry = build_full_control_tool_registry(ExplodingExecutor(binding()))
        loop = BoundedAgentLoop(ScriptedDecider([call(READ_STATE), final("读过")]), registry)
        result = drive_loop(loop, "读取当前状态")

        self.assertIs(result.status, LoopStatus.TOOL_ERROR)
        self.assertEqual(result.error["code"], "tool_exception")
        self.assertIn("platform link down", result.error["message"])
        self.assertEqual(result.checkpoint.executed_tool_calls, [])

    def test_confirmed_action_that_explodes_on_confirm_leaves_state_untouched(self):
        class ExplodingConfirmExecutor(LoopbackExecutor):
            """只在飞行动作上炸：ACQUIRE 要能正常执行，否则建立不了前置状态。"""

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._previewed: dict[str, str] = {}

            def preview(self, item):
                card = super().preview(item)
                if card.status is FlightControlStatus.PREVIEW_REQUIRED:
                    self._previewed[card.confirmation_id] = FlightControlAction(item.action).value
                return card

            def confirm(self, confirmation_id, **kwargs):
                if self._previewed.get(confirmation_id) == MOVE_FORWARD:
                    raise RuntimeError("platform link down")
                return super().confirm(confirmation_id, **kwargs)

        executor = ExplodingConfirmExecutor(binding())
        registry = build_full_control_tool_registry(executor)
        loop = BoundedAgentLoop(
            ScriptedDecider([call(ACQUIRE), call(MOVE_FORWARD, distance_m=10), final("完成")]),
            registry)
        result = drive_loop(loop, "接管控制权并向前飞10米")

        self.assertIs(result.status, LoopStatus.TOOL_ERROR)
        self.assertEqual(result.error["code"], "tool_exception")
        # ACQUIRE 已经确认执行（revision=1），爆炸的 MOVE_FORWARD 不能留下任何位移。
        state = executor.read_state()
        self.assertEqual(state["revision"], 1)
        self.assertEqual(state["last_action"], ACQUIRE)
        self.assertEqual(state["latitude"], 0.0)

    def test_preview_rejection_never_reaches_confirm(self):
        class RejectingExecutor(LoopbackExecutor):
            """除 ACQUIRE 外一律拒绝，用来验证「预览拒绝不进确认」。"""

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.previewed: dict[str, str] = {}
                self.confirmed_actions: list[str | None] = []

            def preview(self, item):
                card = super().preview(item)
                if card.status is FlightControlStatus.PREVIEW_REQUIRED:
                    self.previewed[card.confirmation_id] = FlightControlAction(item.action).value
                    if FlightControlAction(item.action).value == MOVE_FORWARD:
                        return FlightControlReceipt(
                            status=FlightControlStatus.REJECTED, execution_mode=self.execution_mode,
                            action=card.action, command_id=card.command_id,
                            idempotency_key=card.idempotency_key,
                            confirmation_id=card.confirmation_id,
                            confirmation_binding=card.confirmation_binding,
                            context_version=card.context_version, state=dict(self.read_state()),
                            error_code="PLATFORM_REJECTED", message="平台拒绝执行",
                        )
                return card

            def confirm(self, confirmation_id, **kwargs):
                self.confirmed_actions.append(self.previewed.get(confirmation_id))
                return super().confirm(confirmation_id, **kwargs)

        executor = RejectingExecutor(binding())
        registry = build_full_control_tool_registry(executor)
        loop = BoundedAgentLoop(
            ScriptedDecider([call(ACQUIRE), call(MOVE_FORWARD, distance_m=10), final("平台拒绝")]),
            registry)
        result = drive_loop(loop, "接管控制权并向前飞10米")

        self.assertIs(result.status, LoopStatus.FINAL)
        self.assertEqual(executor.confirmed_actions, [ACQUIRE], "被拒的动作绝不能进确认")
        rejection = result.checkpoint.observations[1]["result"]
        self.assertEqual(rejection["status"], FlightControlStatus.REJECTED.value)
        self.assertEqual(rejection["error_code"], "PLATFORM_REJECTED")
        self.assertEqual(rejection["execution_mode"], "loopback")
        # 只有 ACQUIRE 生效，被拒的动作没有产生位移。
        self.assertEqual(executor.read_state()["latitude"], 0.0)
        self.assertEqual(executor.read_state()["revision"], 1)


def _normalized(receipt: dict) -> dict:
    """去掉「模式标签」与纯循环记账后的回执，用于夹具回放与模拟器直跑对比。

    ``last_command_id`` 是循环记账（``project_observation_result`` 也把它列为内部字段），
    导出夹具与测试用的是不同的 command_id，不属于回放差异。
    """
    payload = {key: receipt[key] for key in _RECEIPT_FIELDS if key != "execution_mode"}
    payload["state"] = {key: value for key, value in receipt["state"].items()
                        if key not in {"execution_mode", "last_command_id"}}
    return payload


class FixtureExportTests(unittest.TestCase):
    def test_exported_fixture_replays_the_recorded_messages_field_by_field(self):
        fixture = export_fixture(binding(), _COCKPIT_SEQUENCE, execution_mode="recorded",
                                 clock=lambda: 100.0)
        self.assertEqual(fixture["schema_version"], 1)
        self.assertEqual(fixture["source"], "simulator-export")
        executor = FixtureExecutor(fixture, clock=lambda: 100.0)

        replayed = drive(executor, [request(action, parameters, sequence=index)
                                    for index, (action, parameters) in enumerate(_COCKPIT_SEQUENCE, 1)])

        # 同一条序列直接跑模拟器：夹具回放必须逐字段一致（只有模式名不同）。
        plain_executor = SimulatorExecutor(MemoryFlightControlSimulator(binding(), clock=lambda: 100.0))
        direct = drive(plain_executor, [request(action, parameters, sequence=index)
                                        for index, (action, parameters) in enumerate(_COCKPIT_SEQUENCE, 1)])
        for index, (left, right) in enumerate(zip(replayed, direct)):
            with self.subTest(step=index):
                self.assertEqual(_normalized(left), _normalized(right))
                self.assertEqual(left["execution_mode"], "recorded")
                self.assertEqual(right["execution_mode"], "simulator")
        self.assertEqual(executor.read_state()["execution_mode"], "recorded")

    def test_replay_never_mutates_the_fixture_payload(self):
        """回放会就地改内层状态；夹具对象必须完全不受影响（嵌套块曾按引用共享）。"""
        fixture = export_fixture(binding(), _COCKPIT_SEQUENCE, execution_mode="recorded",
                                 clock=lambda: 100.0)
        snapshot = json.dumps(fixture, ensure_ascii=False, sort_keys=True)
        drive(FixtureExecutor(fixture, clock=lambda: 100.0),
              [request(action, parameters, sequence=index)
               for index, (action, parameters) in enumerate(_COCKPIT_SEQUENCE, 1)])
        self.assertEqual(json.dumps(fixture, ensure_ascii=False, sort_keys=True), snapshot)

    def test_missing_recording_fails_closed_and_never_confirms(self):
        fixture = export_fixture(binding(), ((ACQUIRE, {}),), execution_mode="recorded")

        class CountingFixtureExecutor(FixtureExecutor):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.confirm_calls = 0

            def confirm(self, *args, **kwargs):
                self.confirm_calls += 1
                return super().confirm(*args, **kwargs)

        executor = CountingFixtureExecutor(fixture, clock=lambda: 100.0)
        receipts = drive(executor, [request(ACQUIRE, sequence=1),
                                    request(MOVE_FORWARD, {"distance_m": 10}, sequence=2)])
        self.assertEqual(receipts[0]["status"], FlightControlStatus.ACCEPTED.value)
        self.assertEqual(receipts[1]["status"], FlightControlStatus.REJECTED.value)
        self.assertEqual(receipts[1]["error_code"], "FIXTURE_NOT_FOUND")
        self.assertEqual(executor.confirm_calls, 1, "只有夹具里有的动作才允许进确认")
        self.assertEqual(executor.read_state()["latitude"], 0.0)

    def test_recorded_rejection_is_replayed_verbatim_not_upgraded(self):
        fixture = export_fixture(binding(), ((ACQUIRE, {}),), execution_mode="recorded",
                                 clock=lambda: 100.0)
        before = fixture["recordings"][0]["state"]
        fixture["recordings"].append({
            "action": MOVE_FORWARD, "parameters": {"distance_m": 10},
            "status": FlightControlStatus.REJECTED.value, "error_code": "PLATFORM_LIMIT",
            "message": "平台侧限速拒绝", "state": dict(before),
        })
        executor = FixtureExecutor(fixture, clock=lambda: 100.0)
        receipts = drive(executor, [request(ACQUIRE, sequence=1),
                                    request(MOVE_FORWARD, {"distance_m": 10}, sequence=2)])

        rejection = receipts[1]
        self.assertEqual(rejection["status"], FlightControlStatus.REJECTED.value)
        self.assertEqual(rejection["error_code"], "PLATFORM_LIMIT")
        self.assertEqual(rejection["message"], "平台侧限速拒绝")
        # 录制是拒绝，内层绝不能留下位移。
        self.assertEqual(executor.read_state()["latitude"], before["latitude"])
        self.assertEqual(executor.read_state()["revision"], before["revision"])

    def test_fixture_loader_validates_schema_source_and_state_keys(self):
        good = export_fixture(binding(), ((ACQUIRE, {}),))
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "fixture.json")

            def write(payload):
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False)
                return load_fixture(path)

            self.assertEqual(write(good)["schema_version"], 1)

            broken = json.loads(json.dumps(good))
            broken["schema_version"] = 99
            with self.assertRaises(FixtureError):
                write(broken)

            broken = json.loads(json.dumps(good))
            del broken["source"]
            with self.assertRaises(FixtureError):
                write(broken)

            broken = json.loads(json.dumps(good))
            broken["recordings"][0]["state"]["unknown_field"] = 1
            with self.assertRaises(FixtureError):
                write(broken)

            broken = json.loads(json.dumps(good))
            broken["runtime"]["actor_id"] = ""
            with self.assertRaises(FixtureError):
                write(broken)

            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            with self.assertRaises(FixtureError):
                load_fixture(path)

            with self.assertRaises(FixtureError):
                load_fixture(os.path.join(folder, "missing.json"))

    def test_shipped_fixture_is_labelled_a_simulator_export(self):
        """诚实性护栏：仓库里的夹具必须自证来源，不能冒充真机录制。"""
        fixture = load_fixture(_FIXTURE_PATH)
        self.assertEqual(fixture["source"], "simulator-export")
        self.assertEqual(validate_execution_mode(fixture["execution_mode"]), "recorded")
        recorded = {(item["action"], json.dumps(item["parameters"], sort_keys=True))
                    for item in fixture["recordings"]}
        expected = {(action, json.dumps(parameters, sort_keys=True))
                    for action, parameters in _COCKPIT_SEQUENCE}
        self.assertTrue(expected <= recorded, f"夹具缺少动作: {expected - recorded}")
        for item in fixture["recordings"]:
            self.assertEqual(item["status"], FlightControlStatus.ACCEPTED.value)
            self.assertIsInstance(item["state"], dict)

    def test_fixture_executor_cannot_be_built_from_a_mode_the_gate_forbids(self):
        fixture = export_fixture(binding(), ((ACQUIRE, {}),))
        fixture["execution_mode"] = "mqtt-live"
        with self.assertRaises(ValueError):
            FixtureExecutor(fixture)


if __name__ == "__main__":
    unittest.main()
