# V1.4 阶段三后端事件账本记录（2026-10-27）

## 已完成

- `ktd_backend` 新增 `GET /lingxi/agent/platform/events`，按项目/站点/机库/无人机/任务固定绑定读取增量。
- 事件账本只接受 `low_battery_return`、`recharged_online`、`temporary_task_completed` 三类 typed 事件，具备 event ID 幂等、序号冲突和旧序号拒绝规则。
- 指定绑定没有已核验事件来源时返回 `503/TASK_EVENT_SOURCE_UNAVAILABLE`；已有账本但没有新事件时才返回空数组，避免把来源缺失当成“没有事件”。
- Agent `PlatformTaskEventBridge` 和 `KtdPlatformExecutor.read_task_events()` 已接入该读取契约。

## 实际验证

- 后端提交：`de5fa6b708 feat: 增加平台任务事件账本读取契约`。
- `mvn -pl biz-inspection -am -DskipTests compile`：BUILD SUCCESS。
- `mvn -pl biz-inspection -am -Dtest=LingxiTaskEventContractTest -Dsurefire.failIfNoSpecifiedTests=false test`：2 项通过。
- 飞控 Agent 离线门禁：1336 项通过，0 失败、0 跳过。

## 未验证 / 不宣称

- 当前 PZ MQTT 与动态电量回调尚未把真实事件发布到该账本；本提交提供存储/读取边界和 fail-closed 语义，不宣称真实事件广播已接通。
- `route_status` 权威快照、充电上线和临时任务完成的真实来源仍需平台侧补齐。
