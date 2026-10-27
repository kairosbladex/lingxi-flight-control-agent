# V1.4 2.2.1 六十六项飞控动作控制权矩阵（证据）

- **证据日期**：2026-10-27
- **工作树 / 分支**：`/Users/wangxiaolin/PycharmProjects/.worktrees/lingxi-v14-line-c` · `feat/v14-line-c`（基于 `test@c8f45d5`）
- **被测代码**：`src/lingxi_flight_control/full_control.py`（模型侧控制面的 `FlightControlAction` 枚举，共 66 项）
- **测试文件**：`tests/test_v14_action_authority_matrix.py`
- **规范依据**：设计规范 2.2.1 —— 飞行中所有控制指令（微操 / 载荷 / 急停 / 一键返航）必须校验控制权，无豁免。
- **证据边界**：仅覆盖模型侧 `MemoryFlightControlSimulator.preview/confirm`（内存模拟器）；不覆盖 KTD / 卓翼平台执行器，不联网、不触真实设备。

## 一、结论摘要

| 统计项 | 数量 | 说明 |
| :--- | ---: | :--- |
| 动作总数 | 66 | `len(FlightControlAction) == 66`，枚举常量值互不重复 |
| 豁免 N | 5 | 控制权管理 4 项 + 只读状态 1 项，均有代码依据（见 3.3） |
| 受控 M | 61 | preview 与 confirm 两阶段均强制控制权校验，话术逐字一致 |
| 本次发现缺陷 K | 0 | 未发现「应拦截却放行」「话术不一致」「豁免无依据」 |
| 漏放动作 | 0 | 61 项逐项断言，无一漏过控制权校验（fail-closed） |

**结论**：按当前模型侧实现，66 项动作中 61 项受控动作在无控制权时 preview 与 confirm 两阶段都被拦截，
错误码 `CONTROL_NOT_OWNED`、话术原文 `您尚未获取无人机控制权，请先申请后再下发指令。` 完全一致；
5 项豁免均有可指认的代码依据，不依赖「测试失败后追认」。

## 二、验证方法（两阶段如何构造成立）

受控动作的控制权校验在 `full_control.py` 的两个位置：`preview` 的 716-718 行与 `confirm` 的 992-996 行；
`_apply` 是唯一改变飞行状态的入口，只由 `confirm`（1044 行）调用。因此验证方式为：

1. **preview 阶段**：新建模拟器（`control_owner=None`），用**合法参数 + 合法前置状态**直接 `preview`，
   断言 `status=rejected`、`error_code=CONTROL_NOT_OWNED`、`message` 逐字等于话术、`confirmation_id is None`、
   `state.to_dict()` 前后不变。参数合法性依据 `_validate_parameters`（448-544 行）与 `normalize_parameters`；
   航线类动作的前置状态依据 `_ROUTE_REQUIRED_STATES` / `_route_precondition_error`（406-410、642-669 行）。
2. **confirm 阶段**：先走 `control_acquire` 的完整 preview/confirm 合法取得控制权 → `preview` 目标动作拿到确认卡
   （此处断言必须为 `preview_required`，若拿不到卡用例直接失败而**不是跳过**，避免测不到 confirm 分支）→
   走 `control_release` 的完整 preview/confirm 收回控制权 → `confirm` 旧确认卡，断言拒绝、
   `error_code=CONTROL_NOT_OWNED`、话术逐字一致、状态无副作用，且再次 confirm 返回 `CONFIRMATION_NOT_FOUND`
   （证明确认卡被撤销，不能等重新持权后补执行）。
3. **豁免动作**不参与上述断言，单独用用例固化其放行行为与代码依据，并断言 `_CONTROL_FREE_ACTIONS`
   与证据中的豁免表完全一致——代码改了豁免面而证据没改，测试先失败。
4. **矩阵完整性**：分类互斥且覆盖全集；任何新增动作若未进参数表/豁免表，用例直接失败（fail-closed）。

## 三、66 行矩阵

### 3.1 逐动作矩阵

| # | 动作 | 枚举常量 | 是否豁免 | preview（无控制权） | confirm（预览后失去控制权） | 对应用例名 |
| ---: | :--- | :--- | :--- | :--- | :--- | :--- |
| 1 | control_acquire | `CONTROL_ACQUIRE` | 是（控制权管理） | 放行：PREVIEW_REQUIRED | 放行：ACCEPTED（effect=actor-1） | `test_exempt_ownership_actions_keep_documented_bypass_in_both_phases (subTest: action=control_acquire)` |
| 2 | control_release | `CONTROL_RELEASE` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=control_release)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=control_release)` |
| 3 | remote_request | `REMOTE_REQUEST` | 是（控制权管理） | 放行：PREVIEW_REQUIRED | 放行：ACCEPTED（effect=pending） | `test_exempt_ownership_actions_keep_documented_bypass_in_both_phases (subTest: action=remote_request)` |
| 4 | remote_approve | `REMOTE_APPROVE` | 是（控制权管理） | 放行：PREVIEW_REQUIRED | 放行：ACCEPTED（effect=approved） | `test_exempt_ownership_actions_keep_documented_bypass_in_both_phases (subTest: action=remote_approve)` |
| 5 | remote_reject | `REMOTE_REJECT` | 是（控制权管理） | 放行：PREVIEW_REQUIRED | 放行：ACCEPTED（effect=rejected） | `test_exempt_ownership_actions_keep_documented_bypass_in_both_phases (subTest: action=remote_reject)` |
| 6 | set_keyboard_mode | `SET_KEYBOARD_MODE` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=set_keyboard_mode)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=set_keyboard_mode)` |
| 7 | set_joystick_mode | `SET_JOYSTICK_MODE` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=set_joystick_mode)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=set_joystick_mode)` |
| 8 | set_virtual_button | `SET_VIRTUAL_BUTTON` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=set_virtual_button)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=set_virtual_button)` |
| 9 | move_forward | `MOVE_FORWARD` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=move_forward)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=move_forward)` |
| 10 | move_backward | `MOVE_BACKWARD` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=move_backward)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=move_backward)` |
| 11 | move_left | `MOVE_LEFT` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=move_left)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=move_left)` |
| 12 | move_right | `MOVE_RIGHT` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=move_right)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=move_right)` |
| 13 | move_up | `MOVE_UP` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=move_up)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=move_up)` |
| 14 | move_down | `MOVE_DOWN` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=move_down)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=move_down)` |
| 15 | turn_left | `TURN_LEFT` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=turn_left)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=turn_left)` |
| 16 | turn_right | `TURN_RIGHT` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=turn_right)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=turn_right)` |
| 17 | emergency_stop | `EMERGENCY_STOP` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=emergency_stop)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=emergency_stop)` |
| 18 | hover | `HOVER` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=hover)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=hover)` |
| 19 | takeoff | `TAKEOFF` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=takeoff)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=takeoff)` |
| 20 | land | `LAND` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=land)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=land)` |
| 21 | land_alternate | `LAND_ALTERNATE` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=land_alternate)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=land_alternate)` |
| 22 | return_home | `RETURN_HOME` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=return_home)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=return_home)` |
| 23 | cancel_return_home | `CANCEL_RETURN_HOME` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=cancel_return_home)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=cancel_return_home)` |
| 24 | route_pause | `ROUTE_PAUSE` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=route_pause)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=route_pause)` |
| 25 | route_resume | `ROUTE_RESUME` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=route_resume)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=route_resume)` |
| 26 | continue_from_breakpoint | `CONTINUE_FROM_BREAKPOINT` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=continue_from_breakpoint)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=continue_from_breakpoint)` |
| 27 | point_flight | `POINT_FLIGHT` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=point_flight)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=point_flight)` |
| 28 | point_orbit | `POINT_ORBIT` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=point_orbit)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=point_orbit)` |
| 29 | upload_route | `UPLOAD_ROUTE` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=upload_route)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=upload_route)` |
| 30 | execute_route | `EXECUTE_ROUTE` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=execute_route)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=execute_route)` |
| 31 | set_move_speed | `SET_MOVE_SPEED` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=set_move_speed)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=set_move_speed)` |
| 32 | set_roll_speed | `SET_ROLL_SPEED` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=set_roll_speed)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=set_roll_speed)` |
| 33 | set_pitch_speed | `SET_PITCH_SPEED` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=set_pitch_speed)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=set_pitch_speed)` |
| 34 | set_vertical_speed | `SET_VERTICAL_SPEED` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=set_vertical_speed)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=set_vertical_speed)` |
| 35 | set_yaw_speed | `SET_YAW_SPEED` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=set_yaw_speed)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=set_yaw_speed)` |
| 36 | reset_parameters | `RESET_PARAMETERS` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=reset_parameters)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=reset_parameters)` |
| 37 | camera_electro_optical | `CAMERA_ELECTRO_OPTICAL` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=camera_electro_optical)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=camera_electro_optical)` |
| 38 | camera_zoom | `CAMERA_ZOOM` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=camera_zoom)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=camera_zoom)` |
| 39 | camera_infrared | `CAMERA_INFRARED` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=camera_infrared)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=camera_infrared)` |
| 40 | camera_wide | `CAMERA_WIDE` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=camera_wide)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=camera_wide)` |
| 41 | gimbal_up | `GIMBAL_UP` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=gimbal_up)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=gimbal_up)` |
| 42 | gimbal_down | `GIMBAL_DOWN` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=gimbal_down)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=gimbal_down)` |
| 43 | gimbal_left | `GIMBAL_LEFT` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=gimbal_left)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=gimbal_left)` |
| 44 | gimbal_right | `GIMBAL_RIGHT` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=gimbal_right)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=gimbal_right)` |
| 45 | gimbal_digital_zoom | `GIMBAL_DIGITAL_ZOOM` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=gimbal_digital_zoom)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=gimbal_digital_zoom)` |
| 46 | gimbal_reset | `GIMBAL_RESET` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=gimbal_reset)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=gimbal_reset)` |
| 47 | gimbal_45 | `GIMBAL_45` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=gimbal_45)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=gimbal_45)` |
| 48 | gimbal_90 | `GIMBAL_90` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=gimbal_90)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=gimbal_90)` |
| 49 | camera_lock_focus | `CAMERA_LOCK_FOCUS` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=camera_lock_focus)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=camera_lock_focus)` |
| 50 | camera_panorama | `CAMERA_PANORAMA` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=camera_panorama)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=camera_panorama)` |
| 51 | camera_photo | `CAMERA_PHOTO` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=camera_photo)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=camera_photo)` |
| 52 | camera_start_recording | `CAMERA_START_RECORDING` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=camera_start_recording)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=camera_start_recording)` |
| 53 | camera_stop_recording | `CAMERA_STOP_RECORDING` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=camera_stop_recording)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=camera_stop_recording)` |
| 54 | infrared_set_limits | `INFRARED_SET_LIMITS` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=infrared_set_limits)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=infrared_set_limits)` |
| 55 | payload_megaphone | `PAYLOAD_MEGAPHONE` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=payload_megaphone)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=payload_megaphone)` |
| 56 | payload_searchlight | `PAYLOAD_SEARCHLIGHT` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=payload_searchlight)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=payload_searchlight)` |
| 57 | payload_methane | `PAYLOAD_METHANE` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=payload_methane)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=payload_methane)` |
| 58 | payload_dropper | `PAYLOAD_DROPPER` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=payload_dropper)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=payload_dropper)` |
| 59 | payload_dynamic | `PAYLOAD_DYNAMIC` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=payload_dynamic)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=payload_dynamic)` |
| 60 | speaker_speak | `SPEAKER_SPEAK` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=speaker_speak)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=speaker_speak)` |
| 61 | speaker_volume | `SPEAKER_VOLUME` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=speaker_volume)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=speaker_volume)` |
| 62 | searchlight_brightness | `SEARCHLIGHT_BRIGHTNESS` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=searchlight_brightness)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=searchlight_brightness)` |
| 63 | task_end_current | `TASK_END_CURRENT` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=task_end_current)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=task_end_current)` |
| 64 | task_end_all | `TASK_END_ALL` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=task_end_all)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=task_end_all)` |
| 65 | ai_algorithm_set | `AI_ALGORITHM_SET` | 否 | 拦截：REJECTED / CONTROL_NOT_OWNED | 拦截：REJECTED / CONTROL_NOT_OWNED（确认卡同时撤销） | `test_preview_blocks_controlled_action_without_authority (subTest: action=ai_algorithm_set)<br>test_confirm_blocks_controlled_action_after_authority_lost (subTest: action=ai_algorithm_set)` |
| 66 | read_state | `READ_STATE` | 是（只读） | 放行：ACCEPTED，不生成确认卡 | 不适用（无确认卡，不产生执行） | `test_read_state_is_read_only_exempt_before_control_check (subTest: action=read_state)` |

### 3.2 分类与统计

| 分类 | 数量 | 动作 |
| :--- | ---: | :--- |
| 受控（两阶段都拦截） | 61 | ai_algorithm_set, camera_electro_optical, camera_infrared, camera_lock_focus, camera_panorama, camera_photo, camera_start_recording, camera_stop_recording, camera_wide, camera_zoom, cancel_return_home, continue_from_breakpoint, control_release, emergency_stop, execute_route, gimbal_45, gimbal_90, gimbal_digital_zoom, gimbal_down, gimbal_left, gimbal_reset, gimbal_right, gimbal_up, hover, infrared_set_limits, land, land_alternate, move_backward, move_down, move_forward, move_left, move_right, move_up, payload_dropper, payload_dynamic, payload_megaphone, payload_methane, payload_searchlight, point_flight, point_orbit, reset_parameters, return_home, route_pause, route_resume, searchlight_brightness, set_joystick_mode, set_keyboard_mode, set_move_speed, set_pitch_speed, set_roll_speed, set_vertical_speed, set_virtual_button, set_yaw_speed, speaker_speak, speaker_volume, takeoff, task_end_all, task_end_current, turn_left, turn_right, upload_route |
| 豁免（控制权管理） | 4 | control_acquire, remote_approve, remote_reject, remote_request |
| 豁免（只读状态） | 1 | read_state |
| 合计 | 66 | — |
| **缺陷 K** | **0** | 见第四节 |

### 3.3 豁免代码依据（逐项）

| 动作 | 豁免类别 | 代码依据 | 豁免理由（不豁免会怎样） |
| :--- | :--- | :--- | :--- |
| `control_acquire` | 控制权管理 | `full_control.py:153-159`（`_CONTROL_FREE_ACTIONS`）；效果见 `_apply` 1103-1104 行 | 获取控制权本身尚无控制权，若要求先持权则死锁，永远无法取得控制权 |
| `remote_request` | 控制权管理 | 同上；效果见 `_apply` 1105-1106 行 | 远程控制申请是「请求接管」的第一步，申请人此时不持权；要求持权则无法发起申请 |
| `remote_approve` | 控制权管理 | 同上；效果见 `_apply` 1107-1108 行 | 审批远程控制请求属于控制权管理，只改写 `control_request` 状态，不产生飞行指令 |
| `remote_reject` | 控制权管理 | 同上；效果见 `_apply` 1109-1110 行 | 同上；拒绝请求不产生飞行指令，也不改变 `control_owner` |
| `read_state` | 只读状态 | `full_control.py:711-715` 早返回（`ACCEPTED`，话术「读取模拟状态」） | 状态查询不是控制指令：不生成确认卡、不调用 `_apply`、不改变飞行状态；要求持权会让监视/交接方无法读取状态 |

> 说明：`read_state` **不在** `_CONTROL_FREE_ACTIONS` 中，而是由 711-715 行更早的只读分支豁免。
> 证据把它单列为第 5 项豁免，并在测试中同时断言 `READ_STATE not in _CONTROL_FREE_ACTIONS`，避免把两处豁免混为一谈。

## 四、缺陷清单

**本次 66 项动作矩阵未发现缺陷（K = 0）**：

- 无「应拦截却放行」：61 项受控动作的 preview / confirm 两阶段逐项拦截；
- 无「话术不一致」：所有拦截话术逐字等于 `您尚未获取无人机控制权，请先申请后再下发指令。`，
  （常量唯一定义在 `src/lingxi_flight_control/flight_policy.py:9`）；
- 无「豁免无依据」：5 项豁免均可指认到具体代码行与业务理由，见 3.3。

### 4.1 观察项（非缺陷，超出本线修改范围，本线不改 `src`）

1. **注释与实际豁免面不一致（文档一致性，低风险）**：`full_control.py:153` 注释写
   「only ownership management is exempt」，但 `read_state` 实际由 711-715 行早返回豁免。
   读接口不产生控制副作用，不构成放行缺陷；建议后续把只读豁免写进注释或显式集合。
2. **远程审批未校验审批者身份（契约风险，未验证）**：`remote_approve` / `remote_reject` 被归为控制权管理豁免
   （153-159 行），`_apply` 只改写 `control_request`（1107-1110 行），不校验审批者是否为当前持权方。
   模拟器单 binding 无法区分审批者，真实平台侧审批鉴权不在本次离线验证范围，未验证，不据此判缺陷。
3. **跨适配器豁免面不一致（范围外，未验证）**：`zhuoyi_platform.py:188` 用同一份 `_CONTROL_FREE_ACTIONS` 做控制权校验，
   因此 `read_state` 在该执行器上仍要求持权，与模拟器的只读豁免行为不一致；
   `_CONTROL_FREE_ACTIONS` 还是被跨模块导入的私有符号（`zhuoyi_platform.py` 的 import 段）。
   属平台适配器一致性问题，不在本线 66 项模型动作矩阵范围内，未做真实平台验证。
4. **矩阵范围说明**：本证据只覆盖 66 个 `FlightControlAction` 的 preview/confirm。模型侧另有
   `apply_task_event` / `resolve_temporary_outcome` / `select_algorithms` 等写入口，它们不属于 66 项控制指令矩阵，
   其控制权边界需另行评审，本证据不为其背书。

## 五、复现命令

```bash
# 在 worktree /Users/wangxiaolin/PycharmProjects/.worktrees/lingxi-v14-line-c 内
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_v14_action_authority_matrix.py' -v

# 全量离线套件（CI 唯一真源）
bash tools/ci/offline_tests.sh
```

## 六、未验证 / 边界

- 仅模型侧内存模拟器；KTD / 卓翼平台执行器的控制权校验、租约过期、设备能力未覆盖。
- 只验证「控制权」维度，不覆盖路径围栏、设备参数极值、测绘中断、幂等/上下文版本等其它拒绝条件。
- 单元测试通过只代表内存模拟器的预览/确认行为，不代表真实平台或设备已执行。
- 豁免判定以本分支当前代码为依据；若后续规范把远程审批纳入持权范围，需重新评估 153-159 行与本节。

---

## 附：本次运行结果

### 1. 矩阵用例（6 个 unittest 用例 / 192 个 subTest 断言）

```text
Ran 6 tests in 0.060s
OK
```

### 2. 全量离线套件 `bash tools/ci/offline_tests.sh`（CI 唯一真源）

```text
+ PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'
Ran 1169 tests in 13.840s
OK
```

（矩阵文件新增 6 个用例；全量 discover 由改动前的 1163 项变为 1169 项，其余用例全部通过。）
