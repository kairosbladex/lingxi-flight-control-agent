"""观察回执投影：只把模型下一步真正需要的字段回放回去。

回执是驾驶舱 UI 的对外契约（`test_full_control.py` 逐字段断言），所以压缩只能做在
「观察 → 模型 prompt」这一层：检查点里仍然保存完整回执，模型看到的是投影。

投影规则是「按域」的：
- 请求回声与循环内部字段（command_id / idempotency_key / confirmation_* / expires_at）
  对下一步决策毫无用处，永远丢掉；
- parameters / camera / infrared / payloads / virtual_buttons 这几块体积大但只服务单一领域，
  只在对应领域出现；
- `read_state`（域 `state`）的唯一目的就是「把状态说全」，所以它保留全部状态块；
- **不认识的动作/形状一律原样返回**——宁可多花 token，也不猜着删。
"""

from __future__ import annotations

import json
import unittest

from lingxi_flight_control import (
    MemoryFlightControlSimulator,
    OllamaDecider,
    RuntimeBinding,
    build_full_control_tool_registry,
    project_observation_result,
)
from lingxi_flight_control.models import AgentCheckpoint

#: 请求回声 + 循环内部字段：模型既不需要，也没法用。
ECHO_FIELDS = ("command_id", "idempotency_key", "confirmation_id", "confirmation_binding",
               "expires_at", "execution_mode")
#: 单一领域的重块。
DOMAIN_BLOCKS = ("parameters", "camera", "infrared", "payloads", "virtual_buttons")
#: 每步都可能用到的连续遥测。
TELEMETRY = ("control_owner", "altitude_m", "longitude", "latitude", "battery_percent",
             "task_status", "route_status", "revision")


def payload_size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True))


class ObservationProjectionTests(unittest.TestCase):
    def setUp(self):
        self.simulator = MemoryFlightControlSimulator(
            RuntimeBinding("actor-1", "project-1", "hangar-1", "drone-01", "batch-1", 1))
        self.registry = build_full_control_tool_registry(self.simulator)

    def receipt(self, action, arguments=None):
        return self.registry.get(action).handler(dict(arguments or {}))

    # ---- 永远丢掉的部分 ----------------------------------------------------

    def test_request_echo_and_loop_internals_are_dropped(self):
        projected = project_observation_result("read_state", self.receipt("read_state"))
        for field in ECHO_FIELDS:
            self.assertNotIn(field, projected)
        self.assertNotIn("last_command_id", projected["state"])
        # 判断下一步所需的流程字段一个都不能少。
        for field in ("status", "action", "error_code", "message", "context_version", "state"):
            self.assertIn(field, projected)
        self.assertEqual(projected["status"], "accepted")
        self.assertEqual(projected["action"], "read_state")

    def test_continuous_telemetry_survives_every_domain(self):
        arguments_by_action = {
            "read_state": {},
            "move_forward": {"distance_m": 5},
            "route_pause": {},
            "camera_photo": {},
            "infrared_set_limits": {"min_c": -10, "max_c": 100},
            "set_move_speed": {"value": 5},
            "payload_searchlight": {"enabled": True},
            "control_acquire": {},
        }
        for action, arguments in arguments_by_action.items():
            state = project_observation_result(action, self.receipt(action, arguments))["state"]
            for field in TELEMETRY:
                self.assertIn(field, state, msg=f"{action} 丢了 {field}")

    # ---- 按域保留重块 ------------------------------------------------------

    def test_read_state_keeps_every_state_block(self):
        # read_state 的全部意义就是「说全」，不能在这里省。
        state = project_observation_result("read_state", self.receipt("read_state"))["state"]
        for block in DOMAIN_BLOCKS:
            self.assertIn(block, state)

    def test_flight_observation_drops_narrow_domain_blocks(self):
        state = project_observation_result("move_forward", self.receipt("move_forward", {"distance_m": 5}))["state"]
        for block in DOMAIN_BLOCKS:
            self.assertNotIn(block, state)
        self.assertIn("altitude_m", state)

    def test_camera_and_parameters_keep_only_their_own_blocks(self):
        camera = project_observation_result("camera_photo", self.receipt("camera_photo"))["state"]
        self.assertIn("camera", camera)
        for block in ("parameters", "infrared", "payloads", "virtual_buttons"):
            self.assertNotIn(block, camera)

        speed = project_observation_result(
            "set_move_speed", self.receipt("set_move_speed", {"value": 8}))["state"]
        self.assertIn("parameters", speed)
        for block in ("camera", "infrared", "payloads", "virtual_buttons"):
            self.assertNotIn(block, speed)

    def test_infrared_payload_and_control_domains_keep_their_blocks(self):
        infrared = project_observation_result(
            "infrared_set_limits", self.receipt("infrared_set_limits", {"min_c": -10, "max_c": 100}))["state"]
        self.assertIn("infrared", infrared)
        self.assertNotIn("parameters", infrared)

        payload = project_observation_result(
            "payload_searchlight", self.receipt("payload_searchlight", {"enabled": True}))["state"]
        self.assertIn("payloads", payload)
        self.assertNotIn("camera", payload)

        control = project_observation_result("control_acquire", self.receipt("control_acquire"))["state"]
        self.assertIn("virtual_buttons", control)
        self.assertNotIn("camera", control)

    # ---- 不认识的形状原样返回 ----------------------------------------------

    def test_unknown_action_or_shape_is_returned_unchanged(self):
        foreign = {"status": "accepted", "custom": {"a": 1}}
        self.assertIs(project_observation_result("relative_move", foreign), foreign)
        self.assertIs(project_observation_result("not_a_cockpit_action", foreign), foreign)
        self.assertIs(project_observation_result("read_state", foreign), foreign)
        for shape in ("plain text", 42, None, []):
            self.assertEqual(project_observation_result("read_state", shape), shape)

    def test_missing_state_block_is_returned_unchanged(self):
        odd = {"status": "accepted", "action": "read_state", "command_id": "c"}
        self.assertEqual(project_observation_result("read_state", odd), odd)

    # ---- 体积 --------------------------------------------------------------

    def test_projection_measurably_shrinks_replayed_receipts(self):
        read_state = self.receipt("read_state")
        projected = project_observation_result("read_state", read_state)
        self.assertLess(payload_size(projected), payload_size(read_state))

        flight = self.receipt("move_forward", {"distance_m": 5})
        trimmed_flight = project_observation_result("move_forward", flight)
        self.assertLess(payload_size(trimmed_flight), payload_size(flight) * 0.75)

    def test_cockpit_observations_shrink_the_replayed_prompt(self):
        receipt = self.receipt("read_state")
        checkpoint = AgentCheckpoint(
            user_input="读取当前状态", context={}, messages=[], tool_calls=[],
            observations=[{"tool": "read_state", "arguments": {}, "result": receipt}
                          for _ in range(20)])
        definitions = self.registry.definitions()
        plain = OllamaDecider(model="m", transport=lambda *_: {})
        trimmed = OllamaDecider(model="m", transport=lambda *_: {},
                                result_projector=project_observation_result)
        plain_messages = plain.messages("读取当前状态", checkpoint, definitions)
        trimmed_messages = trimmed.messages("读取当前状态", checkpoint, definitions)
        self.assertLess(trimmed.estimate_prompt_tokens(trimmed_messages, definitions),
                        plain.estimate_prompt_tokens(plain_messages, definitions))


if __name__ == "__main__":
    unittest.main()
