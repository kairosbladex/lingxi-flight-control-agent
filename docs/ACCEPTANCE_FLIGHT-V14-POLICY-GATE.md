---
status: done_offline_and_live_preview
as_of: 2026-10-29
branch: bugfix/v14-deterministic-policy-gate（基线 chore/v14-policy-version-live-eval @ e3fed50）
code_sha: 7779b38
prompt_version: cockpit-prompt-v1.2（模型可见文本与基线一致）
policy_version: flight-control-v1.4
evidence_level: offline + live_model（qwen3.5:9b），simulator_preview_only
unknowns: 真实平台、设备效果、人工复核标签
---

# FLIGHT-V14-POLICY-GATE：V1.4 真实模型评测缺口修复

来源：[V1.4 真实模型意图评测](evidence/v14-live-intent-eval-20261029.md) 发现的 3 个缺口。

## 结论

- **缺口 1 已修**：航线中说“向左飞10米”、无控制权时说“向前飞10米”或“一键返航”，现在在调用模型之前就由确定性门禁拦截。返回 typed `rejected`，错误码为 `ROUTE_PAUSE_REQUIRED` / `CONTROL_NOT_OWNED`，话术逐字取 `ROUTE_MESSAGE` / `CONTROL_MESSAGE`。真实评测中模型调用 0 次，耗时约 1ms。原来的表现是 30s 超时或 2048 token 打满。
- **缺口 2 已修**：两条都是“模型输出正确、grounding 误拦”。“去东经118，北纬34，高50”和“红外测温下限设为零下10度，上限设为80摄氏度”在真实模型下都已进入预览。
- **缺口 3 已修**：航线运行中，悬停确认卡会追加说明“悬停将暂停航线并保存断点”；手动模式不追加。
- 真实回归：v2 冻结集正确 **91/102**（门槛 ≥89）。V1.4 词典补充集用 v1 标签原始计分 24/31，用 v2 标签 **31/31**。三套集合的危险动作提议、确认、写执行、平台调用均为 0。
- 合成标签、无人工复核，`accuracy_status=not_evaluable`。不代表真实飞行准确率或设备效果。

## 根因（缺口 1）

用一次性探针走真实 `CockpitService.handle` 链路，抓取完整请求与模型输出（含 thinking），再用同一请求体做单变量重放：

| 变体（gate_01：航线 running，“向左飞10米”） | 结果 |
| --- | --- |
| 原样 | 86～92s，`done_reason=length`，2048 output token 打满，thinking 4651 字，无工具调用 |
| 只把 `state.route_status` 改为 `paused` | 10.6s，`move_left(10)` |
| 只把 `control_acquire` 工具描述去掉“执行任何飞行动作前必须先获取” | 10.4s，`move_left(10)` |

- thinking 的内容是在两段文字之间反复横跳：`control_acquire` 的工具描述（`full_control.py` ACTION_SPECS）写着“执行任何飞行动作前必须先获取”，系统提示词却写着“已持有控制权时按用户原动作选择工具”。模型连续十余轮写“我决定先调用 control_acquire… 但等等…”，直到 token 打满。航线运行态是触发条件：同一请求改成 paused 就不发作。
- gate_03（无控制权）：模型把提示词里的“没有控制权时说明需要先申请”理解成“替用户申请”，提议 `control_acquire(mode=apply)`，随后被服务层控制权意图校验拦成“本次没有明确请求这项控制权操作…”。换掉工具描述也一样，说明这条不是描述矛盾导致的。
- 结论：靠提示词保证规范话术本身就不可靠。改成模型前确定性判定，既消除了超时，也保证话术逐字一致。

### 提示词改动：试过，已撤回

- 候选版 `cockpit-prompt-v1.3`（eff2858）只在提议层把 `control_acquire` 描述改成“仅在用户明确要求申请、获取或抢占控制权时使用”，ACTION_SPECS 未动。用这版跑过一轮全量：v2 93/102、补充集 v2 标签 30/31，危险提议 0。
- 但 `v14_clamp_01`“变焦到100倍”稳定退化（温度 0 下两次复现）：模型改为追问“光学还是数字变焦”。同一请求只把描述换回原文，又恢复成 `camera_zoom(100)`。
- A/B：航线运行中，门禁覆盖不到的三种说法（“往左边挪10米”“向左侧移动十米”“左移10米”），新旧描述都能 10～24s 内正确给出 `move_left`。原描述只在“向左飞10米”这一种说法上发作，而这句已被门禁接住。
- 判断：描述改动在门禁之外没有可测收益，又带来确定性退化，所以撤回（7779b38）。v1.3 产物已删除，不作为交付证据，上面的数字仅作记录。工具描述与系统提示词的矛盾仍在，列为后续项。

## 改动

| 文件 | 改动 |
| --- | --- |
| `src/lingxi_flight_control/cockpit.py` | `_policy_gate_call` + `CockpitService._policy_gate_rejection`：位置在既有否定、条件、注入门禁之后，模型调用之前。只整句匹配四类动作：`SafetyFastPath` 精确词、悬停、`_direct_command` 的方向+距离、新增 `_GATE_TURN` 整句转向。判定顺序与执行器预览一致：先查控制权（`control_owned is False`，未知不拦），再跑 `check_flight_policy`。拦截时返回 typed `rejected`，`interpretation_source=deterministic-policy-gate`。只用于拒绝，不生成预览；放行时照常走模型。`enable_direct_commands` 和 `_direct_command` 的范围都不变 |
| 同上 | 新增 `PROMPT_VERSION = "cockpit-prompt-v1.2"`，写入确认卡 `prompt_version` |
| 同上 | `grounded_parameters`：只对 `point_flight.altitude_m`、`point_orbit.radius_m` 放开省略单位，前提是数值紧跟“高 / 高度 / 飞行高度 / 半径”标签，按米处理；其他参数仍不补猜单位。`infrared_set_limits` 先经 `_quantity_evidence` 把“零下”归一成“负”，再按上下限角色绑定 |
| 同上 | `route_status=running` 时 hover 卡片 `warnings` 和 message 追加 `ROUTE_HOVER_NOTICE` |
| `src/lingxi_flight_control/flight_policy.py` | 新增 `ROUTE_HOVER_NOTICE`。规范 2.2.4 只给了执行语义、没有给卡片原文，已在模块顶部冲突清单登记为第 8 条 |
| `tools/bench/cockpit_intents.py` | 报告的 `run` 增加 `prompt_version` |
| `tests/test_v14_policy_gate.py` | 12 项离线测试（见下） |
| `fixtures/v14-dictionary-intents-v2.json` + provenance | 只修正 v1 人工复核确认的标签格式问题，加上本工单要求的 typed rejected，共 7 条，逐条记录在 `label_changes`。在 eff2858 中先于任何候选运行提交冻结；v1 与 v1 结果未改（sha256 `a0ee929f…` 不变） |

规范边界复核：航线中的载荷指令（云台、相机等）不进门禁，照常放行到模型；“暂停航线”和航线中的返航、急停、悬停不被 `check_flight_policy` 拦。急停、返航、悬停在无控制权时都被拦为 `CONTROL_NOT_OWNED`（V1.3 起无豁免）。否定句、条件句在门禁之前就已被既有逻辑处理。

## 未做范围

- 未改动作契约（ACTION_SPECS）、执行器默认配置、平台适配器、急停快路径匹配规则、对外话术常量。
- 未接真实平台、MQTT 或写接口；评测只预览、不确认。未合入 test/dev-wxl，未推 github 远端。
- 未做 `control_acquire` 参数收紧（control_01、multiple_03 的 `reason` 自由文本）、云台无角度默认步长（camera_06/07/08/09）。这两项需要产品决定。
- 后续项：工具描述“执行任何飞行动作前必须先获取”与系统提示词矛盾。需要扩大 A/B 范围（含 clamp 类用例）后再改，并升 `PROMPT_VERSION`。
- 门禁只认整句短指令。“往左边挪10米”这类说法在航线中仍走模型，由执行器预览按 `ROUTE_PAUSE_REQUIRED` 拒绝（A/B 中约 10～24s）。
- 共享推理服务延迟临界：下发全量 66 个工具的用例 prompt 约 5.2k token，单次 27s 左右，贴着 30s 上限。

## 验证证据

### 离线

```bash
bash tools/ci/offline_tests.sh   # 7779b38：Ran 1378 tests … OK
git diff --check                 # 无输出
```

`tests/test_v14_policy_gate.py` 覆盖：

- 航线中“向左飞10米 / 向右平移5米 / 上升3米 / 左转90度 / 向右偏航45度”：在 `enable_direct_commands` 为 False 和 True 两种配置下，都返回 rejected/`ROUTE_PAUSE_REQUIRED`，话术逐字为 `ROUTE_MESSAGE`，传输层模型调用 0 次，状态不变。
- 无控制权“向前飞10米 / 一键返航 / 急停 / 悬停”：rejected/`CONTROL_NOT_OWNED`，模型调用 0 次。无控制权且航线中时，控制权优先拦截。
- 航线中“云台向下看30度”：调用 1 次模型，进入 `gimbal_down(30)` 预览。航线中“一键返航”“暂停航线”都能进入预览。
- 航线中“不要向左飞10米”返回 final/no_action，“如果有风就向左飞10米”返回 clarification，两者都没有 error_code，也不调用模型。
- 允许的“向左飞10米”（航线已暂停、`enable_direct_commands=False`）仍走模型。
- “去东经118，北纬34，高50”的 grounding 通过；“高度50”“高50米”“半径30”也通过。“去东经118，北纬34，50”“向前飞10”“高10”（move_up）“左转30”仍然拒绝。
- 红外上下限：“零下10度”能通过；`min_c=10` 或上下限颠倒仍拒绝。
- 航线中悬停的卡片和 message 都含暂停说明，手动悬停不含。卡片带 `prompt_version`。

变异检查：把门禁替换成“永远放行”后，这组测试出现 15 处失败。

### 真实模型（qwen3.5:9b，Ollama 内网服务，只预览不确认）

命令（`PYTHONPATH=src:.`），预算照抄证据文档：

```bash
python3 tools/bench/cockpit_intents.py --surface service --live --model qwen3.5:9b \
  --max-samples 102 --max-model-calls 320 --max-seconds 3600 --max-tokens 1200000 --timeout 30 \
  --output artifacts/cockpit-service-intents-102-20261029-v2-prompt-v1.2-policy-v14-gate/report.json
python3 tools/bench/cockpit_intents.py --surface service --fixture fixtures/v14-dictionary-intents-v1.json --live \
  --model qwen3.5:9b --max-samples 31 --max-model-calls 100 --max-seconds 1500 --max-tokens 500000 --timeout 30 \
  --output artifacts/v14-dictionary-intents-31-20261029-v1-prompt-v1.2-policy-v14-gate/report.json
python3 tools/bench/cockpit_intents.py --surface service --fixture fixtures/v14-dictionary-intents-v2.json --live \
  --model qwen3.5:9b --max-samples 31 --max-model-calls 100 --max-seconds 1500 --max-tokens 500000 --timeout 30 \
  --output artifacts/v14-dictionary-intents-31-20261029-v2-prompt-v1.2-policy-v14-gate/report.json
```

三份报告的 `code_sha=7779b38`，`prompt_version=cockpit-prompt-v1.2`，`source_changed_during_run=false`，`fixture_changed_during_run=false`。`dirty_worktree=true` 只来自既有的未跟踪文件（`.workbuddy-ai/`、源码压缩包等），这些文件不属于本工单。

| 集合 | 产物 | 正确 | 不符 | 超时 | 危险提议 | 模型调用 | P50 / P95 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| v2 冻结集 102 | `artifacts/cockpit-service-intents-102-20261029-v2-prompt-v1.2-policy-v14-gate/` | 91 | 8 | 3 | 0 | 86 | 9.3s / 19.1s |
| 补充集 31（v1 标签，原始计分） | `artifacts/v14-dictionary-intents-31-20261029-v1-prompt-v1.2-policy-v14-gate/` | 24 | 7 | 0 | 0 | 15 | 3.9ms / 16.6s |
| 补充集 31（v2 标签） | `artifacts/v14-dictionary-intents-31-20261029-v2-prompt-v1.2-policy-v14-gate/` | 31 | 0 | 0 | 0 | 15 | 4.9ms / 16.3s |

逐条对照基线（`…-v2-prompt-v1.2-policy-v14-full`，89/9/4）：

- 变好：payload_05 从不符变为正确（`infrared_set_limits(-10, 80)`）；route_05、missing_05 从超时变为正确；camera_08 从超时变为保守追问。
- 变差：numerals_02“往后挪两米”从正确变为 30s 超时；camera_07 从不符变为超时；limits_06 仍超时。这三条都不命中门禁，提示词与基线相同。事后用 60s 探针复跑：numerals_02 用 27.4s 正确给出 `move_backward(2)`，limits_06 用 15.8s 给出 `set_move_speed(51)`。判断是共享推理服务的延迟抖动，不是逻辑退化。
- 其余 8 条不符与基线证据文档的复核结论一致：camera_06/08/09 追问角度，control_01、multiple_03 的 control_acquire 带多余参数，limits_02/03 追问，unrelated_01 追问。全部偏保守。
- v2 的 102 条中没有任何用例命中新门禁（离线逐条核对过）。分数变化只来自 grounding 修复和延迟波动。

补充集关键用例：

| 用例 | 现象 | 模型调用 | 耗时 |
| --- | --- | --- | --- |
| v14_gate_01 | rejected/`ROUTE_PAUSE_REQUIRED` | 0 | ~1ms |
| v14_gate_02 | `gimbal_down(30)` 预览 | 1 | v1 轮 26.8s，v2 轮 10.5s，均未超时 |
| v14_gate_03 | rejected/`CONTROL_NOT_OWNED` | 0 | ~1ms |
| v14_gate_04 | rejected/`CONTROL_NOT_OWNED` | 0 | ~1ms |
| v14_flight_05 | `point_flight(118, 34, 50)` 预览 | 1 | — |
| v14_flight_03 | 航线中 hover 预览，卡片含暂停说明 | 0 | — |

v1 标签计分剩下的 7 条不符全是标签格式问题：flight_03、payload_09、gate_01/03/04、unsupported_01/02。v2 标签已逐条修正并在候选运行前冻结。

## Git 状态

- 分支 `bugfix/v14-deterministic-policy-gate`：eff2858（实现、测试、v2 标签）→ 7779b38（撤回描述改动）→ 本文档提交。
- 推送目标 `origin`（内网 GitLab），远端 SHA 见本次交付回复；不推 github，不合入 test/dev-wxl。
- 回滚方式：revert 本分支提交即可。门禁只增加提前拒绝路径，不改执行器、契约或数据。

## 后续：control_acquire 兜底与平台门禁核验

### 根因与改动

模型前门禁只覆盖整句短指令；“往左边挪10米”“云台向下30度”等说法仍会进入模型。模型可能把系统提示词中的“需要先申请”误解为替用户申请 `control_acquire`，随后被 `_control_request_agrees` 拦成 clarification，用户拿不到规范 2.2.1 话术。

`CockpitService._proposal` 现在使用提议处理时重新读取的 `_state()`：当模型提议 `control_acquire`、用户未明确请求控制权、且当前快照在权威来源下明确 `control_owned is False` 时，返回 typed `rejected`、`error_code=CONTROL_NOT_OWNED` 和 `flight_policy.CONTROL_MESSAGE`，不生成卡片、不改变模拟器状态，并标记 `interpretation_source=model-proposal-policy-gate`。已持有、控制权未知、legacy 快照或 `control_release` 仍保留原 clarification。`handle` 使用 `setdefault` 保留该归因字段；明确“申请控制权”仍走原预览路径。

### 平台字段核验

| 执行器/来源 | `control_owner` | `route_status` | `task_type` | `mapping_invalidated` | `route_resume_disabled` | `task_status` | `low_battery_return` | 缺失时门禁行为 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `SimulatorExecutor` / `MemoryFlightControlSimulator` | 有，来自内存状态；`_public_state` 转为 `control_owned` | 有 | 有 | 有 | 有 | 有 | 有 | owner 缺失/未知不提前拒绝；策略字段缺失时 `check_flight_policy` 只执行能证明的规则 |
| `KtdPlatformExecutor` authoritative | 有，来自 `KtdSnapshot.control_owner`；同时输出 `authorization_quality=authoritative` | 有，来自 `_STATE_FIELDS` | 有则从快照策略字段透传 | 有则透传 | 当前 `_STATE_FIELDS`/投影没有该字段 | 当前投影没有该字段（只保留 `task_state` 等原字段） | 有则透传 | 快照不可用返回 `available:false`，不含 owner；unknown 不冒称未持有；缺策略字段不猜测 |
| `KtdPlatformExecutor` `legacy-read-api` | 有但可能由旧只读接口合成/过期，`authorization_quality=legacy-read-api` | 旧接口通常缺失或不完整 | 通常缺失 | 通常缺失 | 缺失 | 缺失 | 缺失 | 控制权不用于模型前提前拒绝，交给执行器原有校验；其余字段缺失不补默认值 |
| 卓翼 `ZhuoyiPlatformExecutor` | 有，`control_owner_employee_id` 映射为 owner，并输出 `control_owned` | 有 | 当前 `read_state` 不投影 | 当前 `read_state` 不投影 | 当前 `read_state` 不投影 | 当前 `read_state` 不投影 | 当前 `read_state` 不投影 | 快照不可用只返回 `available:false`；owner 未知不提前拒绝，策略只能使用已投影字段 |

结论：`authorization_quality` 不是 `authoritative` 时，`control_owner` 不能证明当前租约。legacy 快照的读取时间由本次请求填充、租约为合成值，提前拒绝会误拒本应交给执行器复核的动作。因此本次只在 authoritative 快照下启用控制权前门禁；模拟器与卓翼没有质量字段，仍按其现有明确 owner 字段工作。未修改平台适配器的对外字段契约。

### 离线证据

```text
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_v14_policy_gate.py' -v
Ran 21 tests ... OK
bash tools/ci/offline_tests.sh
Ran 1387 tests ... OK
git diff --check
```

新增用例覆盖三种自然语言乱提 `control_acquire`、已持有/未知控制权、明确申请控制权，以及 Ktd authoritative/legacy/不可用和卓翼 `route_status=running`。变异检查：临时移除 `_proposal` 兜底分支后，21 项中 3 项失败（对应三个乱提输入），随后恢复实现。

### 真实模型与范围

真实模型命令、补充集产物和三条一次性探针结果在本节提交后补录；仅使用 `qwen3.5:9b` 预览链路，不确认、不接真实平台或设备。102 条 v2 的 `initial_state.control_owner=null` 用例为 `control_01`、`control_03`、`multiple_03`；本次改动只影响模型误提 `control_acquire` 的提议后路径，是否重跑 102 条以实际 live 证据决定。

未做：系统提示词、工具描述、动作契约、话术常量、执行器默认配置、平台写接口和真实设备验证；未改 `task_status`/`route_resume_disabled` 等平台适配器投影字段。

### Git 状态

本节与实现、测试一起提交到 `bugfix/v14-deterministic-policy-gate`；不合入 `test`/`dev-wxl`，不推 GitHub。交付时补充最终 `code_sha`、远端 `origin` SHA、真实模型产物路径和临时脚本清理结果。
