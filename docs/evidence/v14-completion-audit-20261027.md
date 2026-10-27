# V1.4 多端完成审计（2026-10-27）

本表按用户目标逐项核对当前源码与验证证据；“部分完成”表示代码契约已落地，但真实平台来源或设备证据仍缺失。

| 要求 | 当前证据 | 状态 | 缺口/边界 |
| --- | --- | --- | --- |
| 静态标注 AI 多选、体标注隐藏 | 地图前端 `ffa17c147`、后端 `34c33a13a2` | 已完成代码部分 | 未做真实登录/跨服务联调 |
| 标注 AI 真实算法粒度启停 | Agent `HttpAlgorithmLifecycleGateway`、后端现有平台审计 | 部分完成 | HanSui/Zhimou/Lingmou 当前没有可核验逐算法 started/stopped API |
| 仅替换冲突动作 | Agent `9f1524f` 与离线测试 | 已完成 | 真实平台生命周期仍受上项限制 |
| 测绘六步事务 | 后端 `c2253dbd96`、SQL、Agent typed gateway | 部分完成 | 默认拼图停止适配器返回 unavailable，真实外部停止未验证 |
| 测绘确认卡/恢复置灰 | UI `eebc44c` 与 Agent mapping reservation | 已完成代码部分 | 宿主回调与真实后端事务未联调 |
| 任务事件桥/账本 | 后端 `de5fa6b708`、`4fdb4efe53`；Agent `PlatformTaskEventBridge` | 部分完成 | PZ MQTT/动态电量尚未发布三类真实事件 |
| 权威 route_status | Agent 严格读取与 fail-closed | 未完成平台部分 | Java 没有完整 authority/telemetry/version/owner 契约，不能安全推导 |
| point/orbit 与半径语义 | Agent `_compile_fly`、`path_geometry`、契约测试 | 已完成 Agent 部分 | Java 能力清单和真实设备轨迹未验证 |
| 三维模型避障 | Agent 有界路径/围栏 checker | 部分完成 | 后端三维障碍回执与真实模型服务未形成 typed 端点 |
| 电量风险追问 | Agent `endurance_mileage` + `battery_percent` | 已完成 Agent 部分 | 真实平台尚未返回续航事实 |
| route_mapping 入口裁决 | `418477e` 已接 cockpit 飞行中预检 | 已完成入口预检 | 地面规划 Agent 归属与完整交接链仍需裁决 |

## 验证

- 飞控 Agent：`bash tools/ci/offline_tests.sh && git diff --check`，1339 项通过，0 失败、0 跳过。
- 后端：`mvn -pl biz-inspection -am -DskipTests compile` 成功；中断契约 3 项、事件契约 2 项通过。
- 独立飞控前端：177/177 通过并构建成功；地图前端此前 176/176 通过并构建成功。

## 交付边界

- 未连接共享 UAT/生产、真实 MQTT、真实视频分析服务或无人机。
- 未 push 共享分支；`.workbuddy-ai/` 与 `dist-v13/` 为既有未跟踪目录，保留不动。
