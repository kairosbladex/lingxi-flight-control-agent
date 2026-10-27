# V1.4 阶段四当前实现记录（2026-10-27）

## 已完成

- Agent 已支持 `point_flight` / `point_orbit` 的 typed 参数、路径几何和平台命令映射；环绕会核对当前水平距离与请求半径，平台不接收半径时拒绝不一致请求并给出降级说明。
- 移动和环绕路径继续经过有界电子围栏/路径检查；`blocked` 拒绝下发，`timeout`/`unavailable` 保留显式降级语义。
- 复合两个目标的计划支持平台声明 `battery_percent + endurance_mileage` 时的续航估算和规范追问；缺少续航事实时才使用显式 2000 米距离假设。
- `route_mapping` 已接入驾驶舱飞行中不支持航线预检，网格/几何体/螺旋触发词统一拦截。

## 实际验证

- `bash tools/ci/offline_tests.sh && git diff --check`：1338 项通过，0 失败、0 跳过。
- 相关提交：`88d8838`（续航追问）、`418477e`（航线规则边界）、`7234443`（平台事件增量读取）。

## 未验证 / 不宣称

- `ktd_backend` 尚未提供已核验的 point/orbit 写能力、三维模型障碍回执或 `endurance_mileage` 真实快照字段；真实平台/设备行为仍未验证。
- 未连接共享环境、真实 MQTT 或无人机。
