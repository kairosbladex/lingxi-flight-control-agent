# V1.4 平台适配器接线记录（2026-10-27）

## 已完成

- `KtdHttpTransport` 固定了三个 Agent 边界：算法粒度启停、测绘中断事务、任务事件增量读取；请求带项目范围、typed schema 和幂等键。
- `HttpMappingInterruptionGateway` 只接受完整原子回执；网络异常、非 typed 回执和字段不完整都返回未知/不可用，保留“不得下发后续飞控动作”语义。
- `PlatformTaskEventBridge` 将平台 snake/camel 字段解码为 `TaskEvent`，事件源失败不会伪造成空事件。
- `route_mapping.decide_route` 已接入驾驶舱飞行中入口；网格、立体/几何航线在入口统一拒绝，并覆盖“用网格飞”触发词。
- 复合目标追问支持平台声明的 `battery_percent + endurance_mileage`，按 20% 安全余量估算，缺少续航事实时保留显式距离假设。
- `fixtures/v14-platform-adapter-v1.json` 固化三类请求/回执的离线回放形状。

## 实际验证

- `bash tools/ci/offline_tests.sh && git diff --check`：1338 项通过，0 失败、0 跳过。
- `test_mapping_interruption.py`、`test_task_events.py`、`test_v14_phrase_gaps.py` 定向回归通过。

## 未验证 / 不宣称

- 当前 `ktd_backend` 尚未提供这三个 `/lingxi/agent/*` 平台端点的可运行实现；本次只完成 Agent 端 typed 适配和 fail-closed 接线，不宣称真实事务、事件流或算法启停已打通。
- 未连接共享 UAT/生产、真实 MQTT、视频分析服务或设备；没有发送真实控制/算法请求。

## 回滚

- 回滚提交 `89c4aa4` 可移除本次平台适配器和航线入口接线；保留 `9f1524f` 的第一阶段 AI 配置与“仅替换”能力。
