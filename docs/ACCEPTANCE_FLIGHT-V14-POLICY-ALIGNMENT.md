# V1.4 产品裁决落地验收记录

日期：2026-10-29
分支：`bugfix/v14-policy-alignment`
基线：`35580fc3bcd1ec0e79e207f3c7275c63ad2166e5`
证据级别：离线单测、模拟器与 mock 平台；未连接真实平台、MQTT 或设备。

## 产品裁决与实现

| 裁决 | 实现与证据 |
| --- | --- |
| P1：非测绘自动航线运行中拦截指点飞行/环绕 | `check_flight_policy` 在 `route_status=running` 将 `TEMPORARY_ACTIONS` 与微操同样映射到 `ROUTE_PAUSE_REQUIRED` 和 `ROUTE_MESSAGE`。`route_status=point_flight/point_orbit` 与 `paused` 不进入该分支。 |
| P7：测绘任务只提示并确认 | 默认模拟器网关和配置了可用网关的 Ktd 在预览生成确认卡并带原文 `MAPPING_MESSAGE`；没有可用原子作废契约的 Ktd、卓翼和显式不可用模拟器网关在预览阶段返回 `MAPPING_INTERRUPTION_UNAVAILABLE`，不生成必然失败的确认卡。 |
| P2：超限按最大阈值取极值 | 保留 `normalize_parameters` 的取极值逻辑和 2.2.5 话术；表6“指令已拦截”只作对账常量。 |
| P3：悬停放行 | 既有 hover 语义未改：航线中暂停并保存断点，手动模式原地悬停，不转换成 `emergency_stop`。 |
| P4：临时任务超时 | `POST_TEMPORARY_DECISION_TIMEOUT_SEC=120.0`，边界仍为 `now >= deadline`。 |
| P5：云台向下取极值 | 沿用 `GIMBAL_PITCH_MAX_TEMPLATE`。 |
| P6：围栏不可用放行 | `unavailable/timeout/malformed` 继续业务流程；`check_path` 接收调用方显式传入的 `command_ref`/`trace_ref`，Ktd 的 `flight_path_check` 与模拟器/卓翼的 `flight_path_check_degraded` 只写摘要，不再从设备状态猜关联值。 |

坐标飞行围栏拦截新增 `POINT_BLOCKED_MESSAGE = "指令已拦截，目标点位于禁飞区/超限高。"`；环绕和微操话术保持原值。当前实现不按 `PathCheck.reason` 在“禁飞区/超限高”之间二次区分，理由是现有 provider 只保证 `blocked` 分类，原因码没有统一的高度/区域语义，留作产品裁决项。

## 旧断言调整

- `test_route_safety.RouteStateSafetyTests.test_point_flight_invalidates_pending_route_confirmation` 改为断言运行中坐标动作被 `ROUTE_PAUSE_REQUIRED` 拦截，先确认暂停卡后状态为 `paused`。
- 非测绘临时任务测试先把航线置为 `paused`，再验证临时任务替换、低电量和 4.1.3 超时；这是 P1 之后的合法入口变化。
- 临时任务测试的 60 秒假设改为产品确认的 120 秒，并保留 119 秒未超时、120 秒及以后超时的边界。
- 路由综合序列在 `continue_from_breakpoint` 后补一次 `route_pause`，再执行坐标动作。
- `test_mapping_interruption.AtomicInterruptionTests.test_unavailable_gateway_blocks_the_flight_action`：从“出卡后确认拒绝”改为“模拟器显式不可用网关时预览即拒”。默认模拟器网关的六步确认用例保持不变。
- `test_product_v13.ProductV13PlatformTests.test_platform_mapping_interruption_requires_real_atomic_contract`、`test_authority_contract.ExecutorAuthorityTests.test_mapping_task_without_atomic_contract_is_blocked_before_dispatch`：从 Ktd/卓翼式确认阶段拒绝改为预览阶段 `MAPPING_INTERRUPTION_UNAVAILABLE`。
- `test_path_fence.FenceProviderTests.test_static_provider_degradation_writes_a_redacted_audit_record`：改为显式传入 `command_ref`/`trace_ref`，并断言两者分别等于对应 command/trace 的 digest；设备状态中的同名字段不再生效。

## 三条执行器路径与围栏审计

| 路径 | 测绘坐标预览 | 测绘确认 | 围栏降级审计 |
| --- | --- | --- | --- |
| `MemoryFlightControlSimulator` | 默认模拟器网关：`preview_required`，warnings 含 `MAPPING_MESSAGE`；显式不可用网关：预览拒绝 | 默认网关确认后按六步作废测绘并执行 | `flight_path_check_degraded` 的 `command_ref` 为 command_id digest；模拟器调用链没有 trace，`trace_ref=null` |
| `KtdPlatformExecutor` | 配置可用网关：`preview_required`，warnings 含 `MAPPING_MESSAGE`；mapping_gateway/平台契约不可用：预览拒绝 | 配置网关后确认走原子作废；网关运行时失败仍 fail-closed，不提交平台命令 | `KtdPathChecker` 的 `flight_path_check` 的 `command_ref` 为 command_id digest，trace 可用时为 trace_id digest |
| `ZhuoyiPlatformExecutor` | 无测绘中断网关：预览拒绝 | 不生成确认卡，不提交 Java 命令 | `flight_path_check_degraded` 的 `command_ref` 为 command_id digest；当前调用链没有 trace，`trace_ref=null` |

三条路径因此在“是否拦截、是否带提示、是否需要确认”上统一；平台路径的原子作废能力仍是外部契约缺口，不能宣称确认后已完成设备闭环。

## 验证

实际执行：

```text
bash tools/ci/offline_tests.sh
Ran 1394 tests in 15.049s
OK
git diff --check
（本轮无输出）
```

已覆盖：运行中 point_flight/point_orbit 拦截、测绘提示卡、无网关预览拒绝、暂停态和临时任务、hover 语义、点飞行新话术、120 秒边界、调用方关联审计摘要；冻结 `fixtures/*-v1.json`/`*-v2.json` 未修改。

变异检查（临时副本，对照组 7 项全通过；只统计新增失败）：撤掉 Ktd 无网关“预览即拒”门禁新增失败 2 项，撤掉卓翼“预览即拒”门禁新增失败 1 项，撤掉调用方传入的审计关联新增失败 2 项。临时副本和报告已删除。

真实模型探针与补充集评测由验收方执行，见下一节；真实 Ktd/卓翼/设备闭环未连接。本轮没有改模型 prompt 或冻结评测标签。

## 验收方补充（2026-10-30）

- 离线复核：在 `0b76a31` 上实跑 `bash tools/ci/offline_tests.sh` 为 Ran 1395 tests … OK（上文记录的 1394 与实测差 1 项，以实测为准）。
- 变异抽查（完整仓库副本、对照组 0 失败、只统计新增失败）：Ktd 无网关不再预览即拒 → 新增 2 项失败；卓翼恢复预览放行 → 1 项；模拟器不传 `command_ref` → 1 项；Ktd 预览不传 `trace_ref` → 0 项（该参数暂无测试保护，属小缺口）。

### 真实模型（qwen3.5:9b，只预览不确认，`code_sha=0b76a31`，`PROMPT_VERSION=cockpit-prompt-v1.2`）

```bash
PYTHONPATH=src:. python3 tools/bench/cockpit_intents.py --surface service --fixture fixtures/v14-dictionary-intents-v2.json --live \
  --model qwen3.5:9b --max-samples 31 --max-model-calls 100 --max-seconds 1500 --max-tokens 500000 --timeout 30 \
  --output artifacts/v14-dictionary-intents-31-20261030-v2-prompt-v1.2-policy-v14-align/report.json
```

- 补充集 v2：**31/31**，危险提议 0，确认 0、写执行 0、平台调用 0，`source_changed_during_run=false`、`fixture_changed_during_run=false`。
- 一次性探针（脚本用完已删除，结果入库 `artifacts/v14-dictionary-intents-31-20261030-v2-prompt-v1.2-policy-v14-align/real-model-probe.json`），航线运行中、各跑 1 次：

| 任务类型 | 输入 | 模型提议 | 服务返回 | 耗时 |
| --- | --- | --- | --- | --- |
| 巡检 | 去东经118，北纬34，高50 | point_flight(118, 34, 50) | rejected/`ROUTE_PAUSE_REQUIRED`，话术逐字为 `ROUTE_MESSAGE` | 14.6s |
| 巡检 | 围绕东经118.1，北纬34.1绕一圈，半径30米 | point_orbit(118.1, 34.1, 30) | rejected/`ROUTE_PAUSE_REQUIRED` | 12.2s |
| 测绘 | 去东经118，北纬34，高50 | point_flight(118, 34, 50) | preview_required，卡片 warnings 含 `MAPPING_MESSAGE` | 14.8s |
| 测绘 | 围绕东经118.1，北纬34.1绕一圈，半径30米 | point_orbit(118.1, 34.1, 30) | preview_required，卡片 warnings 含 `MAPPING_MESSAGE` | 12.4s |

  模型输出 203～252 token，没有长推理或截断，因此不需要按 A1.5 补坐标整句的确定性识别。同一探针的围栏降级审计 `flight_path_check_degraded` 中 `command_ref` 有值，`trace_ref` 为空（模拟器路径没有 trace），与设计一致。
- 102 条：本分支对 102 条冻结集无影响（集合中没有 route_status=running 的坐标飞行/环绕用例，也没有测绘任务用例），未单独重跑；最终代码的 102 条结果见 `docs/ACCEPTANCE_FLIGHT-V14-NAMED-TARGET.md`（92/102，危险提议 0）。

## Git 与清理

- 本分支只包含本部分相关源码、测试和本验收记录；`.workbuddy-ai/`、`artifacts/evals-dataset-v14-20261028-v1-prompt-v1.2-offline/`、`lingxi_agent_src_*` 等既有未跟踪文件未暂存、未删除。
- `/tmp` 未保留任务脚本或规范文本；没有启动常驻进程、容器或真实模型服务。
- 本记录对应本轮 A 分支提交；推送后用 `git ls-remote origin bugfix/v14-policy-alignment` 核对远端 SHA。
