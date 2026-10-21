"""驾驶舱工具注册表测试：59 个动作经模型可调用的白名单工具闭环执行。"""

from __future__ import annotations

import unittest

from lingxi_flight_control import (
    BoundedAgentLoop,
    DecisionKind,
    FlightControlAction,
    FlightControlActionRequest,
    FlightControlStatus,
    LoopStatus,
    MemoryFlightControlSimulator,
    ModelDecision,
    RuntimeBinding,
    ToolCall,
    action_manifest,
    action_tool_definitions,
    action_tool_names,
    build_full_control_tool_registry,
)
from lingxi_flight_control.agent import _contains_unsafe_content
from lingxi_flight_control.full_control import ACTION_SPECS, _spec

READ_STATE = FlightControlAction.READ_STATE.value
ACQUIRE = FlightControlAction.CONTROL_ACQUIRE.value
MOVE_FORWARD = FlightControlAction.MOVE_FORWARD.value


def binding(version: int = 1, drone: str = "drone-1") -> RuntimeBinding:
    return RuntimeBinding("actor-1", "project-1", "hangar-1", drone, "batch-1", version)


def call(name: str, **arguments) -> ModelDecision:
    return ModelDecision(DecisionKind.TOOL_CALL, tool_call=ToolCall(name, arguments))


def final(message: str = "完成") -> ModelDecision:
    return ModelDecision(DecisionKind.FINAL, message=message)


class ScriptedDecider:
    """按脚本依次返回决定；待确认的调用在未执行前会被重发。

    确认被要求后循环会重新调用 `decide()`，模型必须重发**同一个**工具调用，
    `confirmation_binding` 才匹配得上、动作才会执行。真实模型遵循同一契约。

    判定「还没执行」只能看观察数：循环在确认检查**之前**就追加 `tool_calls`，
    确认轮次会让该列表重复增长，靠列表长度无法判断。
    """

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


def drive(loop, command: str, *, max_rounds: int = 12):
    """驱动带确认的循环：每次澄清都以 confirmed=True 续跑。"""
    checkpoint = None
    result = None
    for _ in range(max_rounds):
        result = loop.run(command, checkpoint=checkpoint, confirmed=checkpoint is not None)
        checkpoint = result.checkpoint
        if result.status is not LoopStatus.CLARIFICATION:
            return result
    return result


class RegistryShapeTests(unittest.TestCase):
    def setUp(self):
        self.simulator = MemoryFlightControlSimulator(binding())
        self.registry = build_full_control_tool_registry(self.simulator)

    def test_every_cockpit_action_becomes_one_tool(self):
        names = {definition.name for definition in self.registry.definitions()}
        self.assertEqual(names, set(action_tool_names()))
        self.assertEqual(len(names), len(FlightControlAction))

    def test_handlers_and_runtime_never_cross_the_model_boundary(self):
        for definition in self.registry.definitions():
            self.assertIsNone(definition.handler, definition.name)
        self.assertEqual(self.registry.runtime_context(), self.simulator.runtime_context())

    def test_only_read_state_skips_confirmation(self):
        unconfirmed = {d.name for d in self.registry.definitions() if not d.requires_confirmation}
        self.assertEqual(unconfirmed, {READ_STATE})
        for definition in self.registry.definitions():
            if definition.requires_confirmation:
                self.assertTrue(definition.side_effect, definition.name)

    def test_specs_cover_actions_and_manifest_exposes_parameters(self):
        self.assertEqual(set(ACTION_SPECS), set(FlightControlAction))
        manifest = {item["action"]: item for item in action_manifest()["tools"]}
        self.assertEqual(set(manifest), set(action_tool_names()))
        # 参数清单来自单一真源，之前 SET_MOVE_SPEED 这类动作的参数是漏的。
        self.assertEqual(manifest[FlightControlAction.SET_MOVE_SPEED.value]["parameter_names"], ["value"])
        self.assertEqual(manifest[FlightControlAction.UPLOAD_ROUTE.value]["parameter_names"],
                         ["route_id", "waypoints"])
        self.assertEqual(manifest[MOVE_FORWARD]["parameter_names"], ["distance_m"])
        self.assertEqual(manifest[FlightControlAction.EMERGENCY_STOP.value]["parameter_names"], [])
        domains = {item["domain"] for item in manifest.values()}
        self.assertEqual(domains, {"control", "flight", "route", "parameters",
                                   "camera", "infrared", "payload", "state"})

    def test_action_tool_definitions_carry_real_schemas(self):
        definitions = {item["name"]: item for item in action_tool_definitions()}
        self.assertEqual(set(definitions), set(action_tool_names()))
        move = definitions[MOVE_FORWARD]["parameters"]
        self.assertEqual(move["required"], ["distance_m"])
        self.assertEqual(move["properties"]["distance_m"]["maximum"], 1000)
        self.assertFalse(move["additionalProperties"])
        self.assertEqual(definitions[FlightControlAction.SET_YAW_SPEED.value]["parameters"]["properties"]
                         ["value"]["maximum"], 360)

    def test_spec_rejects_undeclared_required_parameter(self):
        with self.assertRaises(ValueError):
            _spec("flight", "坏契约", {"a": ACTION_SPECS[MOVE_FORWARD].parameters["distance_m"]}, ("b",))


class RegistryExecutionTests(unittest.TestCase):
    def setUp(self):
        self.simulator = MemoryFlightControlSimulator(binding())
        self.registry = build_full_control_tool_registry(self.simulator)
        self.loop = BoundedAgentLoop(ScriptedDecider([]), self.registry, max_steps=8)

    def run_script(self, command, decisions):
        loop = BoundedAgentLoop(ScriptedDecider(decisions), self.registry, max_steps=8)
        return drive(loop, command)

    def test_mutating_tool_does_not_execute_without_confirmation(self):
        loop = BoundedAgentLoop(ScriptedDecider([call(MOVE_FORWARD, distance_m=10)]), self.registry)
        result = loop.run("向前飞10米")
        self.assertIs(result.status, LoopStatus.CLARIFICATION)
        self.assertIn(MOVE_FORWARD, result.message)
        self.assertEqual(self.simulator.state.latitude, 0.0)   # 未执行
        self.assertIsNotNone(result.checkpoint.confirmation_binding)

    def test_acquire_then_move_runs_through_preview_and_confirm(self):
        result = self.run_script("接管控制权并向前飞10米", [
            call(ACQUIRE), call(MOVE_FORWARD, distance_m=10), final("已接管并前进10米"),
        ])
        self.assertIs(result.status, LoopStatus.FINAL)
        self.assertEqual(self.simulator.state.control_owner, "actor-1")
        self.assertAlmostEqual(self.simulator.state.latitude, 0.0000899266130030023)
        self.assertEqual([c["name"] for c in result.checkpoint.executed_tool_calls],
                         [ACQUIRE, MOVE_FORWARD])
        # tool_calls 会为确认轮次重复记录，真正执行了几次要看 executed_tool_calls。
        self.assertEqual([c["name"] for c in result.checkpoint.tool_calls],
                         [ACQUIRE, ACQUIRE, MOVE_FORWARD, MOVE_FORWARD])
        # 已执行记录必须自带参数，否则回放历史时无法与观察结果正确配对。
        self.assertEqual(result.checkpoint.executed_tool_calls[1]["arguments"], {"distance_m": 10})

    def test_flight_action_is_rejected_until_control_is_acquired(self):
        result = self.run_script("直接向前飞10米", [call(MOVE_FORWARD, distance_m=10), final("好了")])
        self.assertIs(result.status, LoopStatus.FINAL)
        receipt = result.checkpoint.observations[0]["result"]
        self.assertEqual(receipt["status"], FlightControlStatus.REJECTED.value)
        self.assertEqual(receipt["error_code"], "CONTROL_NOT_OWNED")
        self.assertEqual(self.simulator.state.latitude, 0.0)

    def test_read_state_needs_no_confirmation_and_returns_telemetry(self):
        loop = BoundedAgentLoop(ScriptedDecider([call(READ_STATE), final("已读取")]), self.registry)
        result = loop.run("读取当前状态")
        self.assertIs(result.status, LoopStatus.FINAL)
        self.assertEqual(result.checkpoint.step_count, 2)
        receipt = result.checkpoint.observations[0]["result"]
        self.assertEqual(receipt["status"], FlightControlStatus.ACCEPTED.value)
        self.assertEqual(receipt["execution_mode"], "simulator")
        self.assertIn("altitude_m", receipt["state"])

    def test_schema_rejects_bad_arguments_before_the_handler_runs(self):
        cases = [
            (MOVE_FORWARD, {"distance_m": 5000}),                 # 超上限
            (MOVE_FORWARD, {"distance_m": 10, "extra": 1}),        # 多余字段
            (FlightControlAction.SET_YAW_SPEED.value, {}),         # 缺必填
            (FlightControlAction.CAMERA_ZOOM.value, {"value": 99}),  # 超上限
        ]
        for name, arguments in cases:
            simulator = MemoryFlightControlSimulator(binding())
            registry = build_full_control_tool_registry(simulator)
            loop = BoundedAgentLoop(ScriptedDecider([call(name, **arguments)]), registry)
            result = drive(loop, "测试")
            # 关键不变量：非法参数绝不触达 handler、状态版本不变。
            # 模型会拿到校验错误并有机会改正（脚本用完后收尾），但改不了就进不去。
            self.assertEqual(simulator.state.revision, 0, f"{name} {arguments}")
            self.assertEqual(result.checkpoint.executed_tool_calls, [], f"{name} {arguments}")
            self.assertIsNotNone(result.checkpoint.observations[0].get("error"), name)

    def test_idempotency_key_factory_is_honoured(self):
        seen = []

        def factory(action, arguments):
            key = f"biz-{len(seen)}"
            seen.append((action, dict(arguments)))
            return key

        simulator = MemoryFlightControlSimulator(binding())
        registry = build_full_control_tool_registry(
            simulator, command_id_factory=lambda: "cmd-fixed", idempotency_key_factory=factory)
        drive(BoundedAgentLoop(ScriptedDecider([call(ACQUIRE), final("ok")]), registry), "接管")

        self.assertEqual([name for name, _ in seen], [ACQUIRE])
        # 业务侧提供的稳定幂等键确实透传到了模拟器：同一键再调一次会判为重复。
        replay = simulator.preview(FlightControlActionRequest("cmd-fixed", "biz-0", ACQUIRE, {}, 1))
        self.assertIs(replay.status, FlightControlStatus.DUPLICATE)

    def test_runtime_binding_change_is_detected_mid_conversation(self):
        result = self.run_script("接管控制权", [call(ACQUIRE), final("ok")])
        self.assertIs(result.status, LoopStatus.FINAL)
        checkpoint = result.checkpoint
        self.simulator.bind(binding(version=2, drone="drone-2"))
        # 换绑后旧的检查点必须被判为过期，而不是继续执行。
        resumed = BoundedAgentLoop(ScriptedDecider([final("不应到达")]), self.registry).run(
            "接管控制权", checkpoint=checkpoint, confirmed=True)
        self.assertIs(resumed.status, LoopStatus.TOOL_ERROR)
        self.assertEqual(resumed.error["code"], "stale_context")


class SafetyRegressionTests(unittest.TestCase):
    """两个真实缺陷的回归：载荷词误杀、换绑后上下文不同步。"""

    def test_runtime_context_follows_bind(self):
        simulator = MemoryFlightControlSimulator(binding())
        before = simulator.runtime_context()
        simulator.bind(binding(version=2, drone="drone-2"))
        after = simulator.runtime_context()
        self.assertNotEqual(before, after)
        self.assertEqual(after.drone_id, "drone-2")
        self.assertEqual(after.context_version, 2)
        # 父类 API 持有的上下文也必须同步，否则两套接口打架。
        self.assertEqual(simulator.context, after)

    def test_every_action_receipt_passes_the_unsafe_content_check(self):
        """裸词 payload 曾被列入禁用表，会把驾驶舱回执整条判为禁止内容。"""
        arguments = {
            FlightControlAction.SPEAKER_SPEAK: {"text": "安全提示"},
            FlightControlAction.SPEAKER_VOLUME: {"value": 50},
            FlightControlAction.SEARCHLIGHT_BRIGHTNESS: {"value": 50},
            FlightControlAction.AI_ALGORITHM_SET: {"algorithm": "烟火", "enabled": True},
            FlightControlAction.SET_VIRTUAL_BUTTON: {"button": "b1", "action": "press"},
            FlightControlAction.TURN_LEFT: {"angle_deg": 90},
            FlightControlAction.TURN_RIGHT: {"angle_deg": 90},
            FlightControlAction.TAKEOFF: {"altitude_m": 50},
            FlightControlAction.POINT_FLIGHT: {"longitude": 116.4, "latitude": 39.9, "altitude_m": 50},
            FlightControlAction.POINT_ORBIT: {"longitude": 116.4, "latitude": 39.9, "radius_m": 200},
            FlightControlAction.UPLOAD_ROUTE: {"route_id": "r1",
                                               "waypoints": [{"longitude": 116.4, "latitude": 39.9}]},
            FlightControlAction.INFRARED_SET_LIMITS: {"min_c": -10, "max_c": 120},
            FlightControlAction.PAYLOAD_DYNAMIC: {"payload": "megaphone", "enabled": True},
            FlightControlAction.PAYLOAD_DROPPER: {"payload": "box"},
            FlightControlAction.CONTINUE_FROM_BREAKPOINT: {"index": 1},
        }
        for action in (FlightControlAction.SET_MOVE_SPEED, FlightControlAction.SET_VERTICAL_SPEED):
            arguments[action] = {"value": 5}
        for action in (FlightControlAction.SET_ROLL_SPEED, FlightControlAction.SET_PITCH_SPEED,
                       FlightControlAction.SET_YAW_SPEED):
            arguments[action] = {"value": 30}
        for action in (FlightControlAction.CAMERA_ZOOM, FlightControlAction.GIMBAL_DIGITAL_ZOOM):
            arguments[action] = {"value": 5}
        for action in (FlightControlAction.MOVE_FORWARD, FlightControlAction.MOVE_BACKWARD,
                       FlightControlAction.MOVE_LEFT, FlightControlAction.MOVE_RIGHT,
                       FlightControlAction.MOVE_UP, FlightControlAction.MOVE_DOWN):
            arguments[action] = {"distance_m": 10}

        simulator = MemoryFlightControlSimulator(binding())
        registry = build_full_control_tool_registry(simulator)
        # Exercise route receipts with a real upload/start/pause/resume sequence,
        # rather than letting enum ordering fabricate a paused route from idle.
        route_actions = (FlightControlAction.UPLOAD_ROUTE, FlightControlAction.EXECUTE_ROUTE,
                         FlightControlAction.ROUTE_PAUSE, FlightControlAction.ROUTE_RESUME)
        ordered_actions = list(route_actions) + [action for action in FlightControlAction
                                               if action not in route_actions]
        decisions = [call(ACQUIRE)] + [call(action.value, **arguments.get(action, {}))
                                      for action in ordered_actions]
        loop = BoundedAgentLoop(ScriptedDecider(decisions + [final("ok")]), registry, max_steps=200)
        result = drive(loop, "跑完所有动作", max_rounds=400)

        reasons = [observation.get("error") for observation in result.checkpoint.observations
                   if "error" in observation]
        self.assertEqual(reasons, [])
        for observation in result.checkpoint.observations:
            if "result" in observation:
                self.assertFalse(_contains_unsafe_content(observation["result"]),
                                 observation["tool"])
                if observation["tool"] in {action.value for action in route_actions}:
                    self.assertEqual(observation["result"]["status"], "accepted")
        executed = {c["name"] for c in result.checkpoint.tool_calls}
        self.assertEqual(executed, set(action_tool_names()))


if __name__ == "__main__":
    unittest.main()
