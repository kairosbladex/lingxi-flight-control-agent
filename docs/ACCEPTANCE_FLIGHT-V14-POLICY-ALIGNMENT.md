# V1.4 产品裁决落地验收记录

日期：2026-10-29  
分支：`bugfix/v14-policy-alignment`  
基线：`35580fc3bcd1ec0e79e207f3c7275c63ad2166e5`  
证据级别：离线单测、模拟器与 mock 平台；未连接真实平台、MQTT 或设备。

## 产品裁决与实现

| 裁决 | 实现与证据 |
| --- | --- |
| P1：非测绘自动航线运行中拦截指点飞行/环绕 | `check_flight_policy` 在 `route_status=running` 将 `TEMPORARY_ACTIONS` 与微操同样映射到 `ROUTE_PAUSE_REQUIRED` 和 `ROUTE_MESSAGE`。`route_status=point_flight/point_orbit` 与 `paused` 不进入该分支。 |
| P7：测绘任务只提示并确认 | `mapping_interruption` 例外保留。模拟器、Ktd、卓翼预览均生成确认卡并带原文 `MAPPING_MESSAGE`；Ktd/卓翼缺少原子作废契约时，确认阶段仍 fail-closed，不发送飞控写入。 |
| P2：超限按最大阈值取极值 | 保留 `normalize_parameters` 的取极值逻辑和 2.2.5 话术；表6“指令已拦截”只作对账常量。 |
| P3：悬停放行 | 既有 hover 语义未改：航线中暂停并保存断点，手动模式原地悬停，不转换成 `emergency_stop`。 |
| P4：临时任务超时 | `POST_TEMPORARY_DECISION_TIMEOUT_SEC=120.0`，边界仍为 `now >= deadline`。 |
| P5：云台向下取极值 | 沿用 `GIMBAL_PITCH_MAX_TEMPLATE`。 |
| P6：围栏不可用放行 | `unavailable/timeout/malformed` 继续业务流程；Ktd 既有 `flight_path_check` 审计补充 trace/command 摘要，模拟器/卓翼等策略层补 `flight_path_check_degraded`。记录不含坐标原文。 |

坐标飞行围栏拦截新增 `POINT_BLOCKED_MESSAGE = "指令已拦截，目标点位于禁飞区/超限高。"`；环绕和微操话术保持原值。当前实现不按 `PathCheck.reason` 在“禁飞区/超限高”之间二次区分，理由是现有 provider 只保证 `blocked` 分类，原因码没有统一的高度/区域语义，留作产品裁决项。

## 旧断言调整

- `test_route_safety.RouteStateSafetyTests.test_point_flight_invalidates_pending_route_confirmation` 改为断言运行中坐标动作被 `ROUTE_PAUSE_REQUIRED` 拦截，先确认暂停卡后状态为 `paused`。
- 非测绘临时任务测试先把航线置为 `paused`，再验证临时任务替换、低电量和 4.1.3 超时；这是 P1 之后的合法入口变化。
- 临时任务测试的 60 秒假设改为产品确认的 120 秒，并保留 119 秒未超时、120 秒及以后超时的边界。
- 路由综合序列在 `continue_from_breakpoint` 后补一次 `route_pause`，再执行坐标动作。
- 测绘中断相关 `MAPPING_MESSAGE` 断言未改写；Ktd 原子契约不可用时的确认阶段拒绝断言保持不变。

## 三条执行器路径与围栏审计

| 路径 | 测绘坐标预览 | 测绘确认 | 围栏降级审计 |
| --- | --- | --- | --- |
| `MemoryFlightControlSimulator` | `preview_required`，warnings 含 `MAPPING_MESSAGE` | 模拟原子网关确认后按六步作废测绘并执行 | 策略层 `flight_path_check_degraded`，含动作/状态/原因码及脱敏关联摘要 |
| `KtdPlatformExecutor` | `preview_required`，warnings 含 `MAPPING_MESSAGE` | 无原子契约时 `MAPPING_INTERRUPTION_UNAVAILABLE`，不提交平台命令 | `KtdPathChecker` 的 `flight_path_check`，补 trace/command 摘要 |
| `ZhuoyiPlatformExecutor` | `preview_required`，warnings 含 `MAPPING_MESSAGE` | 无原子契约时确认拒绝，不提交 Java 命令 | 策略层 `flight_path_check_degraded`，含脱敏关联摘要 |

三条路径因此在“是否拦截、是否带提示、是否需要确认”上统一；平台路径的原子作废能力仍是外部契约缺口，不能宣称确认后已完成设备闭环。

## 验证

实际执行：

```text
bash tools/ci/offline_tests.sh
Ran 1391 tests ... OK
git diff --check
```

已覆盖：运行中 point_flight/point_orbit 拦截、测绘提示卡、暂停态和临时任务、hover 语义、点飞行新话术、120 秒边界、降级审计；冻结 `fixtures/*-v1.json`/`*-v2.json` 未修改。

未执行：真实 `qwen3.5:9b` 探针、v14 补充集与 102 条模型评测、变异检查、真实 Ktd/卓翼/设备闭环。原因是本部分只改变确定性策略与话术，当前任务没有启用共享推理服务或真实平台；模型文本和 `PROMPT_VERSION` 未改。

## Git 与清理

- 本分支只包含本部分相关源码、测试和本验收记录；`.workbuddy-ai/`、`artifacts/evals-dataset-v14-20261028-v1-prompt-v1.2-offline/`、`lingxi_agent_src_*` 等既有未跟踪文件未暂存、未删除。
- `/tmp` 未保留任务脚本或规范文本；没有启动常驻进程、容器或真实模型服务。
- 本记录对应代码提交 SHA 在提交完成后回填；远端推送后用 `git ls-remote origin bugfix/v14-policy-alignment` 核对。
