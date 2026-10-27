"""V1.4 2.2.1 控制权强制校验矩阵：逐项遍历 66 项飞控动作。

规范要求（V1.4 2.2.1）：飞行中所有控制指令（微操 / 载荷 / 急停 / 一键返航）
必须校验控制权，**无豁免**。

本模块只做验证，不修改 ``src``：

* 参数化遍历 ``FlightControlAction`` 全部 66 项，不允许有未分类动作；
* 受控动作在「无控制权」时 ``preview`` 与 ``confirm`` 两个阶段都必须拒绝，
  错误码固定为 ``CONTROL_NOT_OWNED``，话术原文固定为
  ``您尚未获取无人机控制权，请先申请后再下发指令。``；
* **跨操作员**：除「无人持权（``control_owner=None``）」外，另构造
  「另一操作员持权（``control_owner='actor-2'``，当前 actor 为 ``actor-1``）」，
  preview 与 confirm 各跑一组，钉住 ``!= self.binding.actor_id`` 不能退化成
  ``is None``（V1.4 表6 R1 的真实场景就是「用户B 在用户A 持权时下发」）；
* 豁免动作单独成表，每一项都给出 **代码依据**；不允许「用例失败就归为豁免」；
* 受控动作数量 = 66 - 豁免数量，漏一项或新增动作未分类都会让本模块直接失败
  （fail-closed）。

代码依据（``src/lingxi_flight_control/full_control.py``，行号已按 HEAD ``b3f1d6a`` 核准）：

- 153-159：``_CONTROL_FREE_ACTIONS`` 只含 4 个控制权管理动作
  （control_acquire / remote_request / remote_approve / remote_reject）；
- 711-715：``read_state`` 在控制权校验**之前**以只读回执早返回（ACCEPTED），
  这是第 5 个代码级豁免；它不在 ``_CONTROL_FREE_ACTIONS`` 里，由独立分支实现；
- 716-718：``preview`` 的无控制权拒绝分支（``CONTROL_NOT_OWNED`` + 话术原文）；
- 993-997：``confirm`` 执行前的控制权复核，拒绝时连同确认卡一起撤销；
- 1044 / 1082-1249：``_apply`` 是 66 项控制指令在 ``confirm`` 内的唯一动作执行入口
  （``confirm`` 是 66 项动作的唯一执行路径）；模型侧另有 ``apply_task_event`` /
  ``resolve_temporary_outcome`` / ``select_algorithms`` 等写入口不属本矩阵。
  因此本矩阵的「两阶段拦截」只保证 66 项控制指令不会产生状态副作用。

证据文档：``docs/evidence/v14-action-authority-matrix-20261027.md``。
"""

from __future__ import annotations

import unittest

from lingxi_flight_control.full_control import (
    FlightControlAction,
    FlightControlActionRequest,
    FlightControlStatus,
    MemoryFlightControlSimulator,
    RuntimeBinding,
    _CONTROL_FREE_ACTIONS,
)

#: V1.4 2.2.1 规定的话术原文；测试里刻意写成字面量而不是 import 常量，
#: 这样改掉 ``flight_policy.CONTROL_MESSAGE`` 也会让矩阵失败。
CONTROL_MESSAGE = "您尚未获取无人机控制权，请先申请后再下发指令。"

EXPECTED_ACTION_COUNT = 66
ACTOR_ID = "actor-1"
#: V1.4 表6 R1 的真实场景是「用户B 在用户A 持权时下发」；另造一个持权的操作员，
#: 用来钉住控制权判定是 ``!= self.binding.actor_id`` 而不是 ``is None``。
OTHER_ACTOR_ID = "actor-2"
BINDING = RuntimeBinding(ACTOR_ID, "project-1", "hangar-1", "drone-1", "batch-1", 1)
CLOCK_VALUE = 100.0

#: 每个动作的合法参数。依据 ``_validate_parameters``（full_control.py:448-544）
#: 与 ``normalize_parameters``（flight_policy.py），取值都落在协议允许区间内。
#: 参数必须合法，否则 preview 会先因参数错误拒绝，测不到控制权分支。
ACTION_PARAMETERS: dict[FlightControlAction, dict] = {
    # 控制权管理
    FlightControlAction.CONTROL_ACQUIRE: {},
    FlightControlAction.CONTROL_RELEASE: {},
    FlightControlAction.REMOTE_REQUEST: {},
    FlightControlAction.REMOTE_APPROVE: {},
    FlightControlAction.REMOTE_REJECT: {},
    # 输入模式
    FlightControlAction.SET_KEYBOARD_MODE: {},
    FlightControlAction.SET_JOYSTICK_MODE: {},
    FlightControlAction.SET_VIRTUAL_BUTTON: {"button": "a", "action": "press"},
    # 飞行微操
    FlightControlAction.MOVE_FORWARD: {"distance_m": 10},
    FlightControlAction.MOVE_BACKWARD: {"distance_m": 10},
    FlightControlAction.MOVE_LEFT: {"distance_m": 10},
    FlightControlAction.MOVE_RIGHT: {"distance_m": 10},
    FlightControlAction.MOVE_UP: {"distance_m": 10},
    FlightControlAction.MOVE_DOWN: {"distance_m": 10},
    FlightControlAction.TURN_LEFT: {"angle_deg": 15},
    FlightControlAction.TURN_RIGHT: {"angle_deg": 15},
    # 安全动作（急停不豁免）
    FlightControlAction.EMERGENCY_STOP: {},
    FlightControlAction.HOVER: {},
    FlightControlAction.TAKEOFF: {"altitude_m": 30},
    FlightControlAction.LAND: {},
    FlightControlAction.LAND_ALTERNATE: {},
    FlightControlAction.RETURN_HOME: {},
    FlightControlAction.CANCEL_RETURN_HOME: {},
    # 航线
    FlightControlAction.ROUTE_PAUSE: {},
    FlightControlAction.ROUTE_RESUME: {},
    FlightControlAction.CONTINUE_FROM_BREAKPOINT: {},
    FlightControlAction.POINT_FLIGHT: {"longitude": 113.4, "latitude": 34.8, "altitude_m": 50},
    FlightControlAction.POINT_ORBIT: {"longitude": 113.4, "latitude": 34.8, "radius_m": 20},
    FlightControlAction.UPLOAD_ROUTE: {
        "route_id": "route-1",
        "waypoints": [
            {"longitude": 113.4, "latitude": 34.8},
            {"longitude": 113.5, "latitude": 34.9},
        ],
    },
    FlightControlAction.EXECUTE_ROUTE: {},
    # 参数
    FlightControlAction.SET_MOVE_SPEED: {"value": 8},
    FlightControlAction.SET_ROLL_SPEED: {"value": 20},
    FlightControlAction.SET_PITCH_SPEED: {"value": 20},
    FlightControlAction.SET_VERTICAL_SPEED: {"value": 4},
    FlightControlAction.SET_YAW_SPEED: {"value": 60},
    FlightControlAction.RESET_PARAMETERS: {},
    # 相机 / 云台
    FlightControlAction.CAMERA_ELECTRO_OPTICAL: {},
    FlightControlAction.CAMERA_ZOOM: {"value": 4},
    FlightControlAction.CAMERA_INFRARED: {},
    FlightControlAction.CAMERA_WIDE: {},
    FlightControlAction.GIMBAL_UP: {},
    FlightControlAction.GIMBAL_DOWN: {},
    FlightControlAction.GIMBAL_LEFT: {},
    FlightControlAction.GIMBAL_RIGHT: {},
    FlightControlAction.GIMBAL_DIGITAL_ZOOM: {"value": 3},
    FlightControlAction.GIMBAL_RESET: {},
    FlightControlAction.GIMBAL_45: {},
    FlightControlAction.GIMBAL_90: {},
    FlightControlAction.CAMERA_LOCK_FOCUS: {},
    FlightControlAction.CAMERA_PANORAMA: {},
    FlightControlAction.CAMERA_PHOTO: {},
    FlightControlAction.CAMERA_START_RECORDING: {},
    FlightControlAction.CAMERA_STOP_RECORDING: {},
    # 红外 / 载荷
    FlightControlAction.INFRARED_SET_LIMITS: {"min_c": -10, "max_c": 80},
    FlightControlAction.PAYLOAD_MEGAPHONE: {},
    FlightControlAction.PAYLOAD_SEARCHLIGHT: {},
    FlightControlAction.PAYLOAD_METHANE: {},
    FlightControlAction.PAYLOAD_DROPPER: {},
    FlightControlAction.PAYLOAD_DYNAMIC: {"payload": "laser", "enabled": True},
    FlightControlAction.SPEAKER_SPEAK: {"text": "交通安全"},
    FlightControlAction.SPEAKER_VOLUME: {"value": 50},
    FlightControlAction.SEARCHLIGHT_BRIGHTNESS: {"value": 50},
    FlightControlAction.TASK_END_CURRENT: {},
    FlightControlAction.TASK_END_ALL: {},
    FlightControlAction.AI_ALGORITHM_SET: {"algorithm": "smoke", "enabled": True},
    # 只读
    FlightControlAction.READ_STATE: {},
}

#: 让航线类动作的 preview 合法成立所需的前置状态，取自
#: ``_ROUTE_REQUIRED_STATES`` / ``_route_precondition_error``（full_control.py:406-410、
#: 648-669）。矩阵验证的是控制权而不是航线状态，所以先按代码要求准备好状态，
#: 否则会在控制权检查通过后才因航线状态被拒，测不出 confirm 阶段的控制权复核。
ROUTE_PRECONDITION_STATE: dict[FlightControlAction, dict] = {
    FlightControlAction.ROUTE_PAUSE: {"route_id": "route-1", "route_status": "running"},
    FlightControlAction.ROUTE_RESUME: {"route_id": "route-1", "route_status": "paused"},
    FlightControlAction.CONTINUE_FROM_BREAKPOINT: {
        "route_id": "route-1", "route_status": "paused", "breakpoint_index": 1},
    FlightControlAction.EXECUTE_ROUTE: {"route_id": "route-1", "route_status": "uploaded"},
}

#: 代码级豁免 1：控制权管理动作。依据 full_control.py:153-159 的
#: ``_CONTROL_FREE_ACTIONS``，以及 ``_apply`` 里只改写控制权/请求字段、不产生飞行指令
#: 的处理分支（full_control.py:1095-1104）。
EXEMPT_OWNERSHIP_ACTIONS = frozenset({
    FlightControlAction.CONTROL_ACQUIRE,
    FlightControlAction.REMOTE_REQUEST,
    FlightControlAction.REMOTE_APPROVE,
    FlightControlAction.REMOTE_REJECT,
})

#: 代码级豁免 2：只读状态查询。依据 full_control.py:711-715：它在控制权检查
#: （716 行）之前直接返回 ``ACCEPTED``，不生成确认卡、不改变飞行状态。
EXEMPT_READ_ONLY_ACTIONS = frozenset({FlightControlAction.READ_STATE})

EXEMPT_ACTIONS = EXEMPT_OWNERSHIP_ACTIONS | EXEMPT_READ_ONLY_ACTIONS
CONTROLLED_ACTIONS = frozenset(FlightControlAction) - EXEMPT_ACTIONS

#: 证据文档里「对应用例名」列引用的用例 id；subTest 维度用 ``action=`` 标出。
PREVIEW_CASE = "test_preview_blocks_controlled_action_without_authority"
CONFIRM_CASE = "test_confirm_blocks_controlled_action_after_authority_lost"
#: 跨操作员（另一操作员持权）两组用例；见证据文档 3.4。
CROSS_PREVIEW_CASE = "test_preview_blocks_controlled_action_when_other_operator_holds_control"
CROSS_CONFIRM_CASE = "test_confirm_blocks_controlled_action_when_other_operator_holds_control"
EXEMPT_CASE = "test_exempt_ownership_actions_keep_documented_bypass_in_both_phases"
READ_ONLY_CASE = "test_read_state_is_read_only_exempt_before_control_check"


def build_simulator(action: FlightControlAction, *,
                    owner: str | None = None) -> MemoryFlightControlSimulator:
    """构造「动作前置条件已满足、且持权人可指定（默认无人持权）」的模拟器。"""
    simulator = MemoryFlightControlSimulator(BINDING, clock=lambda: CLOCK_VALUE)
    if owner is not None:
        simulator.state.control_owner = owner
    # 飞行中的位姿/算法配置是动作的合法前置条件，与控制权无关。
    simulator.state.altitude_m = 100.0
    simulator.state.configured_algorithms = ["smoke"]
    for field_name, value in ROUTE_PRECONDITION_STATE.get(action, {}).items():
        setattr(simulator.state, field_name, value)
    return simulator


def make_request(action: FlightControlAction, tag: str) -> FlightControlActionRequest:
    return FlightControlActionRequest(
        command_id=f"{action.value}-{tag}",
        idempotency_key=f"{action.value}-{tag}",
        action=action,
        parameters=dict(ACTION_PARAMETERS[action]),
        context_version=BINDING.context_version,
    )


def preview_action(simulator: MemoryFlightControlSimulator,
                   action: FlightControlAction, tag: str):
    return simulator.preview(make_request(action, tag))


def confirm_action(simulator: MemoryFlightControlSimulator,
                   action: FlightControlAction, tag: str, card):
    request = make_request(action, tag)
    return simulator.confirm(
        card.confirmation_id,
        command_id=request.command_id,
        idempotency_key=request.idempotency_key,
        confirmation_binding=card.confirmation_binding,
        context_version=BINDING.context_version,
    )


def acquire_control(simulator: MemoryFlightControlSimulator):
    """走完整 preview/confirm 取得控制权，不直接改内部状态。"""
    return preview_action(simulator, FlightControlAction.CONTROL_ACQUIRE, "matrix-acquire")


def release_control(simulator: MemoryFlightControlSimulator):
    """走完整 preview/confirm 释放控制权，模拟「预览后控制权被收回」。"""
    return preview_action(simulator, FlightControlAction.CONTROL_RELEASE, "matrix-release")


class ControlAuthorityMatrixTests(unittest.TestCase):
    """V1.4 2.2.1：66 项动作逐项验证控制权强制校验。"""

    maxDiff = None

    # ------------------------------------------------------------------ 矩阵完整性
    def test_enum_has_66_actions_and_matrix_classification_is_complete(self):
        actions = list(FlightControlAction)
        self.assertEqual(len(actions), EXPECTED_ACTION_COUNT, "FlightControlAction 动作数变化，矩阵必须同步更新")
        self.assertEqual(len(set(actions)), EXPECTED_ACTION_COUNT, "枚举常量值必须唯一")

        # 豁免必须与代码里的豁免集合一致：代码改了而证据没改，这里先失败。
        self.assertEqual(set(EXEMPT_OWNERSHIP_ACTIONS), set(_CONTROL_FREE_ACTIONS),
                         "豁免动作表与 full_control._CONTROL_FREE_ACTIONS 不一致")
        self.assertEqual(len(EXEMPT_ACTIONS), 5, "豁免动作 = 4 项控制权管理 + 1 项只读状态")
        self.assertFalse(EXEMPT_READ_ONLY_ACTIONS & _CONTROL_FREE_ACTIONS,
                         "read_state 由独立早返回豁免，不应混入 _CONTROL_FREE_ACTIONS")
        self.assertEqual(len(CONTROLLED_ACTIONS), 61, "受控动作必须 = 66 - 5")

        # 分类互斥且覆盖全集（fail-closed：没有第三个类别、没有漏项）。
        self.assertEqual(EXEMPT_ACTIONS & CONTROLLED_ACTIONS, frozenset())
        self.assertEqual(EXEMPT_ACTIONS | CONTROLLED_ACTIONS, frozenset(actions))

        # 规范点名的类别不得被豁免。
        for action in (
            FlightControlAction.EMERGENCY_STOP,
            FlightControlAction.RETURN_HOME,
            FlightControlAction.PAYLOAD_MEGAPHONE,
            FlightControlAction.PAYLOAD_DROPPER,
            FlightControlAction.MOVE_FORWARD,
            FlightControlAction.CAMERA_PHOTO,
            # 释放控制权本身也要求先持权，避免「谁都能替别人释放」。
            FlightControlAction.CONTROL_RELEASE,
        ):
            self.assertIn(action, CONTROLLED_ACTIONS, f"{action.value} 不应被豁免")
        self.assertNotIn(FlightControlAction.READ_STATE, CONTROLLED_ACTIONS)

        # 每个动作都必须有合法参数才能测到控制权分支。
        for action in actions:
            with self.subTest(action=action.value):
                self.assertIn(action, ACTION_PARAMETERS, f"{action.value} 缺少矩阵参数")
                make_request(action, "validate").validate()

    # ------------------------------------------------------------------ preview 阶段
    def test_preview_blocks_controlled_action_without_authority(self):
        for action in sorted(CONTROLLED_ACTIONS, key=lambda item: item.value):
            with self.subTest(action=action.value):
                simulator = build_simulator(action)
                before = simulator.state.to_dict()
                receipt = preview_action(simulator, action, "preview")
                self.assertEqual(receipt.status, FlightControlStatus.REJECTED, receipt.to_dict())
                self.assertEqual(receipt.error_code, "CONTROL_NOT_OWNED", receipt.to_dict())
                self.assertEqual(receipt.message, CONTROL_MESSAGE, receipt.to_dict())
                self.assertIsNone(receipt.confirmation_id, "被拦截的指令不得生成确认卡")
                # 拦截必须发生在任何状态副作用之前。
                self.assertEqual(simulator.state.to_dict(), before)

    # ------------------------------------------------------------------ confirm 阶段
    def test_confirm_blocks_controlled_action_after_authority_lost(self):
        for action in sorted(CONTROLLED_ACTIONS, key=lambda item: item.value):
            with self.subTest(action=action.value):
                simulator = build_simulator(action)

                acquired = acquire_control(simulator)
                self.assertEqual(acquired.status, FlightControlStatus.PREVIEW_REQUIRED, acquired.to_dict())
                acquired = confirm_action(simulator, FlightControlAction.CONTROL_ACQUIRE,
                                          "matrix-acquire", acquired)
                self.assertEqual(acquired.status, FlightControlStatus.ACCEPTED, acquired.to_dict())
                self.assertEqual(simulator.state.control_owner, ACTOR_ID)

                # 持权时 preview 必须能生成确认卡；否则说明矩阵参数/前置状态不合法，
                # 这个用例就测不到 confirm 阶段的控制权复核，应直接失败而不是跳过。
                card = preview_action(simulator, action, "pending")
                self.assertEqual(card.status, FlightControlStatus.PREVIEW_REQUIRED, card.to_dict())
                self.assertIsNotNone(card.confirmation_id)

                # 预览之后失去控制权：走真实的 control_release 两阶段。
                released = release_control(simulator)
                self.assertEqual(released.status, FlightControlStatus.PREVIEW_REQUIRED, released.to_dict())
                released = confirm_action(simulator, FlightControlAction.CONTROL_RELEASE,
                                          "matrix-release", released)
                self.assertEqual(released.status, FlightControlStatus.ACCEPTED, released.to_dict())
                self.assertIsNone(simulator.state.control_owner)

                before = simulator.state.to_dict()
                blocked = confirm_action(simulator, action, "pending", card)
                self.assertEqual(blocked.status, FlightControlStatus.REJECTED, blocked.to_dict())
                self.assertEqual(blocked.error_code, "CONTROL_NOT_OWNED", blocked.to_dict())
                self.assertEqual(blocked.message, CONTROL_MESSAGE, blocked.to_dict())
                self.assertEqual(simulator.state.to_dict(), before, "被拦截的确认不得产生状态副作用")

                # 控制权被收回后，确认卡必须一并撤销，不能留到重新持权后补执行。
                replay = confirm_action(simulator, action, "pending", card)
                self.assertEqual(replay.status, FlightControlStatus.REJECTED, replay.to_dict())
                self.assertEqual(replay.error_code, "CONFIRMATION_NOT_FOUND", replay.to_dict())

    # ------------------------------------------- 跨操作员持权（preview 阶段）
    def test_preview_blocks_controlled_action_when_other_operator_holds_control(self):
        """另一操作员（actor-2）持权时，当前 actor（actor-1）的 preview 必须被拦截。

        校验表达式是 ``control_owner != binding.actor_id``（full_control.py:716），
        本用例钉住它不能退化成 ``control_owner is None``。
        """
        self.assertNotEqual(OTHER_ACTOR_ID, ACTOR_ID)
        for action in sorted(CONTROLLED_ACTIONS, key=lambda item: item.value):
            with self.subTest(action=action.value, control_owner=OTHER_ACTOR_ID):
                simulator = build_simulator(action, owner=OTHER_ACTOR_ID)
                self.assertEqual(simulator.state.control_owner, OTHER_ACTOR_ID)
                before = simulator.state.to_dict()
                receipt = preview_action(simulator, action, "cross-preview")
                self.assertEqual(receipt.status, FlightControlStatus.REJECTED, receipt.to_dict())
                self.assertEqual(receipt.error_code, "CONTROL_NOT_OWNED", receipt.to_dict())
                self.assertEqual(receipt.message, CONTROL_MESSAGE, receipt.to_dict())
                self.assertIsNone(receipt.confirmation_id, "被拦截的指令不得生成确认卡")
                # 拦截必须发生在任何状态副作用之前。
                self.assertEqual(simulator.state.to_dict(), before)

    # ------------------------------------------- 跨操作员持权（confirm 阶段）
    def test_confirm_blocks_controlled_action_when_other_operator_holds_control(self):
        """actor-1 预览后由另一操作员（actor-2）接管控制权，confirm 必须被拦截。

        校验表达式是 ``control_owner != binding.actor_id``（full_control.py:993），
        本用例钉住它不能退化成 ``control_owner is None``。
        """
        self.assertNotEqual(OTHER_ACTOR_ID, ACTOR_ID)
        for action in sorted(CONTROLLED_ACTIONS, key=lambda item: item.value):
            with self.subTest(action=action.value, control_owner=OTHER_ACTOR_ID):
                simulator = build_simulator(action)

                acquired = acquire_control(simulator)
                self.assertEqual(acquired.status, FlightControlStatus.PREVIEW_REQUIRED, acquired.to_dict())
                acquired = confirm_action(simulator, FlightControlAction.CONTROL_ACQUIRE,
                                          "matrix-acquire", acquired)
                self.assertEqual(acquired.status, FlightControlStatus.ACCEPTED, acquired.to_dict())
                self.assertEqual(simulator.state.control_owner, ACTOR_ID)

                # 持权时 preview 必须能生成确认卡；否则用例应直接失败而不是跳过。
                card = preview_action(simulator, action, "cross-pending")
                self.assertEqual(card.status, FlightControlStatus.PREVIEW_REQUIRED, card.to_dict())
                self.assertIsNotNone(card.confirmation_id)

                # 预览之后另一操作员接管控制权。单 binding 模拟器无法经 API 让
                # 第二个 actor 持权（CONTROL_ACQUIRE 只会把 owner 设成当前 actor），
                # 因此直接构造「他人持权」状态，与真实平台里用户B 顶掉用户A 的情形对应。
                simulator.state.control_owner = OTHER_ACTOR_ID
                self.assertEqual(simulator.state.control_owner, OTHER_ACTOR_ID)

                before = simulator.state.to_dict()
                blocked = confirm_action(simulator, action, "cross-pending", card)
                self.assertEqual(blocked.status, FlightControlStatus.REJECTED, blocked.to_dict())
                self.assertEqual(blocked.error_code, "CONTROL_NOT_OWNED", blocked.to_dict())
                self.assertEqual(blocked.message, CONTROL_MESSAGE, blocked.to_dict())
                self.assertEqual(simulator.state.to_dict(), before, "被拦截的确认不得产生状态副作用")

                # 跨操作员被拦截后确认卡同样必须撤销。
                replay = confirm_action(simulator, action, "cross-pending", card)
                self.assertEqual(replay.status, FlightControlStatus.REJECTED, replay.to_dict())
                self.assertEqual(replay.error_code, "CONFIRMATION_NOT_FOUND", replay.to_dict())

    # ------------------------------------------------------- 控制权检查先于业务前置
    def test_control_check_runs_before_route_preconditions(self):
        """未上传航线时也必须先报无控制权，不能先泄露/依赖航线状态。"""
        simulator = MemoryFlightControlSimulator(BINDING, clock=lambda: CLOCK_VALUE)
        blocked = preview_action(simulator, FlightControlAction.ROUTE_PAUSE, "before-route")
        self.assertEqual(blocked.error_code, "CONTROL_NOT_OWNED", blocked.to_dict())

        acquired = acquire_control(simulator)
        self.assertEqual(acquired.status, FlightControlStatus.PREVIEW_REQUIRED, acquired.to_dict())
        acquired = confirm_action(simulator, FlightControlAction.CONTROL_ACQUIRE,
                                  "matrix-acquire", acquired)
        self.assertEqual(acquired.status, FlightControlStatus.ACCEPTED, acquired.to_dict())

        route_receipt = preview_action(simulator, FlightControlAction.ROUTE_PAUSE, "no-route")
        self.assertEqual(route_receipt.status, FlightControlStatus.REJECTED, route_receipt.to_dict())
        self.assertEqual(route_receipt.error_code, "ROUTE_NOT_LOADED", route_receipt.to_dict())

    # ------------------------------------------------------------------ 豁免 1：持权管理
    def test_exempt_ownership_actions_keep_documented_bypass_in_both_phases(self):
        """4 项控制权管理动作的两个阶段都放行；依据 _CONTROL_FREE_ACTIONS。"""
        expected_effects = {
            FlightControlAction.CONTROL_ACQUIRE: ("control_owner", ACTOR_ID),
            FlightControlAction.REMOTE_REQUEST: ("control_request", "pending"),
            FlightControlAction.REMOTE_APPROVE: ("control_request", "approved"),
            FlightControlAction.REMOTE_REJECT: ("control_request", "rejected"),
        }
        self.assertEqual(set(expected_effects), set(EXEMPT_OWNERSHIP_ACTIONS))
        for action, (field_name, expected_value) in expected_effects.items():
            with self.subTest(action=action.value):
                simulator = MemoryFlightControlSimulator(BINDING, clock=lambda: CLOCK_VALUE)
                self.assertIsNone(simulator.state.control_owner)

                card = preview_action(simulator, action, "exempt")
                self.assertEqual(card.status, FlightControlStatus.PREVIEW_REQUIRED, card.to_dict())
                self.assertIsNotNone(card.confirmation_id)

                result = confirm_action(simulator, action, "exempt", card)
                self.assertEqual(result.status, FlightControlStatus.ACCEPTED, result.to_dict())
                self.assertIsNone(result.error_code)
                self.assertEqual(getattr(simulator.state, field_name), expected_value)
                if action is FlightControlAction.CONTROL_ACQUIRE:
                    self.assertEqual(simulator.state.control_owner, ACTOR_ID)
                else:
                    # 远程申请/审批/拒绝只改写 control_request，不得顺带夺取控制权。
                    self.assertIsNone(simulator.state.control_owner)

    # ------------------------------------------------------------------ 豁免 2：只读状态
    def test_read_state_is_read_only_exempt_before_control_check(self):
        """read_state 由 711-715 行早返回豁免：无控制权也 ACCEPTED，但不生成确认卡。"""
        simulator = MemoryFlightControlSimulator(BINDING, clock=lambda: CLOCK_VALUE)
        before = simulator.state.to_dict()
        receipt = preview_action(simulator, FlightControlAction.READ_STATE, "read")

        self.assertEqual(receipt.status, FlightControlStatus.ACCEPTED, receipt.to_dict())
        self.assertIsNone(receipt.error_code)
        self.assertEqual(receipt.message, "读取模拟状态")
        self.assertIsNone(receipt.confirmation_id, "只读查询不生成确认卡")
        self.assertEqual(simulator.state.to_dict(), before)
        # 豁免来源必须明确：它不是 _CONTROL_FREE_ACTIONS 成员，而是更早的只读分支。
        self.assertNotIn(FlightControlAction.READ_STATE, _CONTROL_FREE_ACTIONS)


if __name__ == "__main__":
    unittest.main()
