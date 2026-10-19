# Agent 飞控功能独立适配

整理日期：2026-10-19。范围：前端计划卡、Agent 侧契约、Java 侧独立飞控模块。

相关提交：

- Agent 后端计划模式：`lingxi-flight-control-agent@557558c`
- 前端计划卡：`lingxi-flight-agent-ui@3249401`

## 一、现状与边界

| 层 | 现状 | 本次结论 |
| --- | --- | --- |
| 前端 | 已支持 `flight_plan` 计划卡、一次确认、逐步骤结果展示；前端 144 项通过、构建通过 | 已完成，可独立验证 |
| Agent 后端 | 模拟器计划模式已完成；平台模式未启用 | 保持默认关闭 |
| Java | 按用户要求**不修改 ktd_backend**；评估在 `zhuoyi-ai-platfrom` 复刻 | 见下文可行性 |

`zhuoyi-ai-platfrom` 当前基线与规则：

- 路径 `/Users/wangxiaolin/IdeaProjects/zhuoyi-ai-platfrom`，分支 `feature/unified-ai-migration`。
- Java 25 / Spring Boot 4.1.1 / Spring JDBC / Flyway / PostgreSQL；单模块。
- `AGENTS.md` 明确：本地测试不得调用正式任务、Outbox、MQTT、飞控或生产服务；不得占用 5008；Java 候选使用独立端口。
- 工作区存在未跟踪的 `docs/CODEX_PROGRESS.md`，属于既有工作，不改动、不提交。

## 二、三层契约（分开适配的边界）

### 2.1 前端 ↔ Agent（已实现）

计划卡：

```json
{
  "kind": "flight_plan",
  "confirmation_id": "...",
  "confirmation_binding": "...",
  "command_id": "...",
  "idempotency_key": "...",
  "context_version": 1,
  "expires_at": 1792404636.29,
  "execution_mode": "simulator",
  "steps": [
    {"action": "move_forward", "parameters": {"distance_m": 10}},
    {"action": "camera_photo", "parameters": {}}
  ]
}
```

确认仍然只发送五个字段：

```json
{"confirmation_id":"...","confirmation_binding":"...","command_id":"...","idempotency_key":"...","context_version":1}
```

计划回执：

```json
{
  "kind": "flight_plan",
  "status": "accepted",
  "plan_id": "...",
  "plan_status": "completed",
  "steps": [
    {"step": 1, "action": "move_forward", "parameters": {"distance_m": 10}, "status": "accepted", "error_code": null, "message": "模拟动作已执行"}
  ],
  "failed_step": null
}
```

前端改动点已落在：

- `src/flightControl.ts`：`FlightPlanCard`、`FlightPlanStepResult`、`readReply`/`validCard`/`resultLabel`。
- `src/CockpitPanel.vue`：按顺序渲染步骤、确认整份计划、展示逐步结果与停止原因。
- `tests/flightControl.test.mjs`：4 组计划卡测试，覆盖一次确认、部分失败、取消、非法卡/回执。

### 2.2 Agent ↔ Java（待建）

建议新增独立、版本化契约，不复用大屏 `/screen/uav/...`：

```text
POST /lingxi/agent/flight-control/commands
GET  /lingxi/agent/flight-control/receipt?commandId=...&siteId=...&hangarId=...&droneId=...
```

请求字段：

```json
{
  "schema_version": "lingxi-agent-flight-control/v1",
  "command_id": "...",
  "idempotency_key": "...",
  "trace_id": "...",
  "action": "camera_photo",
  "parameters": {},
  "site_id": "...",
  "hangar_id": "...",
  "drone_id": "...",
  "task_id": null,
  "snapshot_version": "...",
  "expires_at": 1700000005
}
```

首批动作只做能核验的：

| action | 参考现有实现 | 终态口径 |
| --- | --- | --- |
| `move_forward` / `move_backward` / `move_left` / `move_right` / `move_up` / `move_down` | `UavControlDTO`，`mode=2`，type `202/203/204/205/201/-201` | 等 `fly_to_point_progress` 的 `completed` |
| `camera_photo` | `CameraControlDTO type=100` | services_reply `result=0` 的 `accepted` |
| `camera_start_recording` / `camera_stop_recording` | `type=101/102` | 同上 |
| `gimbal_reset` | `GimbalControlDTO type=403` | 同上 |
| `route_pause` / `route_resume` / `emergency_stop` / `return_home` / `cancel_return_home` | `FlightRouteControlDTO type=110/112/113/114/-114` | 第二期 |

所有回执固定 `device_effect=not_verified`；`accepted` 只表示设备服务应答，不等于照片已生成或位移已完成。

### 2.3 Agent ↔ 前端

Agent 返回的计划卡与计划回执就是 2.1 的结构；前端不感知 Java 内部实现，也不直接连 Java。

## 三、在 zhuoyi-ai-platfrom 复刻的可行性

### 3.1 可以直接复刻的逻辑

这些代码与 ktd_backend 的 Spring/MyBatis/Redis 耦合较低，适合移植或重写：

- DJI 方法名与 topic 常量（`camera_photo_take`、`camera_recording_start/stop`、`fly_to_point`、`fly_to_point_progress`）。
- `ServicesReply` 的 JSON 形状：`method`、`bid`、`tid`、`gateway`、`timestamp`、`data`。
- 相机/录像/云台/移动的报文构造参数（`payload_index`、`fly_to_id`、`max_speed`、coordinates、height）。
- `DeviceCommandReceipt` 的严格 JSON 解析思路：重复键、根值数量、深度、时间、返回码、矛盾终态。
- 命令状态机、幂等键、摘要持久化、确认绑定、计划顺序执行、失败即停。
- 现有的前端/Agent 契约和测试夹具。

### 3.2 不能直接复制的部分

需要重写适配，不能整文件拷贝：

| ktd_backend 依赖 | 原因 | 平台替代 |
| --- | --- | --- |
| `DJUtil` 静态 `messageEngine` | ktd_backend 的 MQTT 引擎是 static，平台没有该依赖 | 定义 `FlightControlTransport` 端口；测试用内存实现，真实实现后置 |
| `walker-mm` 的 `MessageEngine` / `MessageManager` / `MessageConnectionBinding` | 连接代次、绑定发布、重连语义与厂商链路强耦合 | 真实 MQTT 适配器单独实现，默认关闭 |
| `UAVSiteHangar`、`UAVInfo`、`UAVSite`、`UdpDatas`、`UavControlPower` | MyBatis 实体/缓存/遥测对象 | 通过只读 ktd_backend 快照接口获取资源、遥测、owner/lease，或平台自建只读模型 |
| `LargeScreenController` | 站点/任务/品牌解析、Redis 全景任务状态等展示逻辑 | 抽出 `FlightControlResourceResolver`，不复制控制器 |
| `DJHangarOperation` / `PZHangarOperation` | 继承品牌操作器并依赖 `site`、`params`、静态命令缓存 | 只复刻相机/移动所需的分支；品牌差异单独适配 |

### 3.3 建议的平台模块

```text
com.zhuoyi.aiplatform.flightcontrol
  FlightControlController          // POST /api/v1/flight-control/commands, GET /receipt
  FlightControlService             // 授权、幂等、状态机、计划执行
  FlightControlCommand             // v1 请求
  FlightControlReceipt             // v1 回执
  FlightControlStateMachine        // dispatching/submitted/accepted/completed/failed/unknown/conflict
  FlightControlStore               // InMemory + Postgres
  FlightControlTransport           // 端口：send / receipt
  InMemoryFlightControlTransport   // 本地测试与 fixture 回放
  MqttFlightControlTransport       // 真实链路，默认关闭，后续阶段
  KtdReadOnlyClient                // 只读快照：资源/遥测/owner/lease
  DjiPayloadBuilder                // 相机/录像/云台/移动报文
```

接入平台现有边界：

- `TrustedGatewayFilter` 增加 `/api/v1/flight-control` 前缀；项目/操作者只接受可信网关头。
- 使用平台的 `InMemory*`/`Postgres*` 双实现和 Flyway 迁移，保持现有测试模式。
- 新配置默认关闭；真实 MQTT 适配器不进入本地测试。
- 不占用 5008，不修改 ktd_backend，不部署、不合并共享环境。

### 3.4 授权与证据链

相机命令在 ktd_backend 现有实现里会先调用 `flight_authority_grab` 和 `payload_authority_grab`，但不等待结果。复刻到平台后必须显式串起来：

1. 可信网关认证项目/操作者。
2. 只读快照核验站点、机库、无人机、任务绑定、在线/空中、遥测新鲜度、控制 owner/lease。
3. 请求 `flight_authority_grab`，等待 `request_succeeded`。
4. 请求 `payload_authority_grab`（需要新增可跟踪方法），等待 `request_succeeded`。
5. 在短授权窗内发送相机/云台/移动命令，并保存 `bid/tid`、连接绑定和幂等键。
6. 回执查询按终态判定；`unknown`/超时/冲突不自动重发。

任何一步缺少可验证事实，命令应保持关闭或拒绝。`accepted` 不能升级成 `completed`；`completed` 也不能证明独立测量的位移精度。

### 3.5 分阶段建议

| 阶段 | 内容 | 可验证结果 |
| --- | --- | --- |
| J1 | 平台内 `flightcontrol` 模块骨架、契约、状态机、内存 transport | **已完成**：本地 Maven 20/20；无 MQTT/设备 |
| J2 | 只读快照客户端、资源/遥测/owner 绑定、可信网关头 | **已完成**：全量 33/33；隔离实例用本地假快照验证 |
| J3 | DJI 相机/录像/云台/移动报文与回执解析 | **已完成**：全量 41/41；冻结 fixture 回放与命令/回执往返 |
| J4 | 真实 MQTT transport，默认关闭 | **已完成离线适配**：全量 49/49；录制发布器，不连接 Broker |
| J5a | 接入服务/HTTP + Postgres 日志 | **已完成离线闭环**：全量 57/57；默认内存 store/transport |
| J5b | 真实 Broker、权威绑定、航权链 | 需单独授权；真实设备验证另计 |

不建议一次复刻全部 59 个动作，也不建议直接复制 ktd_backend 的控制器/操作器整文件。先把“相机 + 移动 + 云台复位”做通，再接航线和其余动作。

### 3.6 J1 落地记录（2026-10-19）

- 仓库/分支：`zhuoyi-ai-platfrom` 的 `feature/agent-flight-control`，从已核验 `origin/prod@e40d672` 创建。
- 本地提交：`e4e37e8`（已推送，远端 SHA 已核验）。
- 新增 `com.zhuoyi.aiplatform.flightcontrol`：
  - `CommandRequest` / `ReceiptResponse` v1 契约；
  - `FlightControlAction` 白名单（6 向移动、拍照、开始/停止录像、云台复位）；
  - 单向状态机与终态规则；
  - 幂等内存命令日志、离线 `InMemoryFlightControlTransport`；
  - `POST /api/v1/flight-control/commands`、`GET /api/v1/flight-control/receipt`；
  - `zhuoyi.flight-control.enabled` 默认 `false`，TTL 默认 120 秒。
- 验证：
  - `./mvnw -B verify`：**20/20**，构建通过；`git diff --check` 通过。
  - 隔离实例 18091（启用）：`camera_photo` 返回 `submitted`、真实 `bid/tid`、`device_effect=not_verified`、`continuation_allowed=false`；回执查询一致。
  - 隔离实例 18092（默认禁用）：返回 `503 FLIGHT_CONTROL_DISABLED`。
  - 非法动作 `takeoff`：返回 `400 INVALID_COMMAND_REQUEST`。
  - 两个实例均已停止，端口释放；无 MQTT、无设备、无 Postgres。
- 明确未做：真实 MQTT transport、Postgres 持久化、可信网关头、快照版本绑定、厂商航权/负载航权等待。`snapshot_version` 当前只校验格式，启用标志只代表软件演练。

### 3.7 J2 落地记录（2026-10-19）

- 本地提交：`b7dcd14`（未推送，待操作级授权）。
- 新增只读快照绑定：
  - `FlightControlSnapshotClient` 端口 + `HttpKtdSnapshotClient`，调用现有 `GET {base}/lingxi/agent/platform/snapshot`；
  - 严格校验 `schema_version`、`snapshot_version`、操作者、项目/站点/机库/无人机/任务绑定；
  - `observed_at` 取 telemetry 的 `sampled_at/received_at` 较早者，要求 ≤5 秒且不晚于当前；
  - 逐动作校验 `permissions`、`capabilities`、`action_availability`；
  - 移动另外要求在线、空中、完整位姿、`UAV_MOVE_DISTANCE=true`，DJI 需要 `altitude_m/height_m`，PZ 需要 `height_m`；
  - 控制 owner 必须等于当前操作者，租约必须存在且未过期；
  - 命令授权窗 `expires_at` 不得超过快照有效期或控制租约。
- 可信网关头：`FlightControlGatewayFilter` 只保护 `/api/v1/flight-control`；校验 `Authorization: Bearer`，项目/操作者头只在该凭据通过后才被信任。未配置网关凭据时返回 `503 LX-GATEWAY-AUTH-NOT-CONFIGURED`。
- 配置：
  - `ZHUOYI_GATEWAY_BEARER_TOKEN`
  - `ZHUOYI_FLIGHT_CONTROL_SNAPSHOT_BASE_URL`
  - `ZHUOYI_FLIGHT_CONTROL_SNAPSHOT_BEARER_TOKEN`
  - `ZHUOYI_FLIGHT_CONTROL_SNAPSHOT_ALLOW_INSECURE_HTTP`（仅内网/回环，默认 false）
- 验证：
  - `./mvnw -B verify`：**33/33**，构建通过；`git diff --check` 通过。
  - 隔离实例：本地假快照 18093 + Java 18091；合法网关与快照 → `submitted`；错误网关凭据 → `401`；快照版本不匹配 → `403 SNAPSHOT_BINDING_MISMATCH`。
  - 实例已停止，端口释放；仍无 MQTT、无设备、无 Postgres。
- 明确未做：Postgres 命令日志、厂商航权/负载航权等待、DJI 报文构造与回执解析、真实 MQTT。当前启用标志仍只是软件演练；快照端点使用 ktd_backend 候选实现，共享环境尚未部署。

### 3.8 J3 落地记录（2026-10-19）

- 本地提交：`d8454d2`（未推送）。
- `DjiCommandEncoder`：纯报文构造，不发布。
  - 相机：`camera_photo_take`、`camera_recording_start`、`camera_recording_stop`，data 只带 `payload_index`；
  - 云台：`gimbal_reset`，data 带 `payload_index`、`reset_mode=0`；
  - 移动：`fly_to_point`，按当前经纬度、altitude、yaw 计算目标点，`max_speed=5`；
  - 报文形状：`method/tid/bid/gateway/data/timestamp/need_reply`，topic `thing/product/{gateway}/services`；
  - 保留 20 米相对高度门禁：低于 20 米返回 `DJI_FLYTO_IMPLICIT_CLIMB`，目标低于 20 米返回 `DJI_LOW_TARGET_UNVERIFIED`；
  - 目标椭球高限制 2–10000 米。
- `DjiReceiptParser`：严格解析。
  - `services_reply`：相机/云台/`fly_to_point`，`result=0` → `accepted`，非零 → `failed`；
  - `events/fly_to_point_progress`：`wayline_progress/in_progress`、`wayline_ok/completed`、`wayline_failed/failed`、`wayline_cancel/cancelled`，成功/进行中携带非零 result → `conflict`；
  - 拒绝重复键、尾随内容、topic/gateway 不一致、时间越界、缺失或非法 bid/tid、负返回码。
- 冻结 fixture：[dji-receipts-v1.json](../src/test/resources/flightcontrol/dji-receipts-v1.json) 及相邻 provenance；10 个合成案例，来源为现有 ktd_backend `DJUtil`/`DJMethod`/`DJTopic`/`DeviceCommandReceipt`。
- 验证：
  - `./mvnw -B verify`：**41/41**，构建通过；`git diff --check` 通过；
  - J3 新增 8 项：encoder 4、parser 2、命令/回执往返 2；
  - 往返测试证明相机回执按 `bid/tid`、移动进度按 `fly_to_id` 关联。
- 明确未做：
  - 报文尚未接入 `FlightControlTransport`，没有发布 MQTT、没有设备动作；
  - `gatewaySn` 与 `payloadIndex` 当前由调用方提供，快照契约尚未返回这两个事实；J4 必须从权威资源取得，不能靠机型猜测；
  - 厂商航权/负载航权等待、真实连接绑定和发送屏障仍在 J4。

### 3.9 J4 落地记录（2026-10-19）

- 本地提交：`1abacb8`（未推送）。
- 新增端口与默认实现：
  - `DjiMessagePublisher`：只声明 `publish(topic, payload)`，不包含任何 Broker 地址、凭据或连接；
  - `DjiCommandJournal`：`prepared/published/unknown` 三阶段持久屏障；
  - `UnconfiguredDjiMessagePublisher` / `UnconfiguredDjiCommandJournal`：fail-closed，分别返回 `DJI_TRANSPORT_UNAVAILABLE`、`DJI_JOURNAL_UNAVAILABLE`。
- `DjiFlightControlTransport`：
  - 顺序严格为 `encode → journal.prepared → 绑定 bid/tid/fly_to_id → publish → journal.published`；
  - 发布异常或 published 日志异常统一保留 `DISPATCH_OUTCOME_UNKNOWN`，不自动重试；
  - prepared 日志失败时不发布；
  - `acceptReceipt` 复用 `DjiReceiptParser`，按 `bid/tid/fly_to_id` 关联回执，失败回执携带 `DJI_RESULT_<code>`。
- 配置：`zhuoyi.flight-control.dji-transport.enabled`，默认 `false`。
- 验证：
  - `./mvnw -B verify`：**49/49**，构建通过；`git diff --check` 通过；
  - J4 新增 8 项测试：日志顺序、发布失败 unknown、prepared 失败不发布、published 失败 unknown、未配置 publisher、禁用 transport、未知回执忽略、失败码映射；
  - 仅使用 `RecordingDjiMessagePublisher`，**没有连接任何真实 Broker**。
- 明确未做：
  - `DjiFlightControlTransport` 尚未接入 `FlightControlService`，也没有 HTTP 入口；
  - `gatewaySn` / `payloadIndex` 仍由调用方提供，快照契约没有这两个事实；
  - 厂商航权/负载航权等待、真实连接绑定、Postgres 持久日志均未做；
  - 真实发布器、Broker 地址/凭据和真机验证需要单独的、覆盖具体动作的授权。

### 3.10 J5a 落地记录（2026-10-19）

- 本地提交：`332bc5d`（未推送）。
- 服务接入：
  - 新增 `DjiFlightControlTransportAdapter`，让 `FlightControlService` 在需要时走 DJI transport；
  - `DispatchCommand` 增加 `FlightControlSnapshot` 与 `DjiDeviceBinding`；
  - 新增 `DjiDeviceBindingProvider`，默认 `UnconfiguredDjiDeviceBindingProvider` fail-closed 返回 `DEVICE_BINDING_UNAVAILABLE`；
  - 只有 `transport.requiresDeviceBinding()` 为 true 时才解析 gateway/payload 绑定；内存 transport 不要求。
- 失败落库：
  - `DISPATCH_OUTCOME_UNKNOWN` → 命令 `unknown`；
  - 其他 pre-send/journal 异常 → `failed`，保存错误码；
  - 仍不自动重试；回执查询可看到 failed/unknown。
- Postgres 日志：
  - `PostgresFlightControlStore`（Spring `JdbcTemplate`）；
  - 手工 DDL `src/main/resources/db/flightcontrol/flight_control_command.sql`，应用不自动建表/迁移；
  - 配置 `zhuoyi.flight-control.store=postgres` + `jdbc-url/username/password`；
  - H2 PostgreSQL 模式测试覆盖 create/find/idempotency/version guard/nullable 字段；
  - **真实 PostgreSQL 未实测**，只验证 SQL 兼容形状。
- 验证：
  - `./mvnw -B verify`：**57/57**，构建通过；`git diff --check` 通过；
  - J5a 新增 8 项：服务+DJI adapter 4、Postgres store 4；
  - 隔离实例：18091（DJI transport 关闭）返回 `submitted`；18092（DJI transport 开启但无权威绑定）返回 `503 DEVICE_BINDING_UNAVAILABLE`；两实例已停止。
- 明确未做：
  - 没有真实 `DjiMessagePublisher`（Broker/凭据/连接绑定）；
  - 没有真实 `DjiDeviceBindingProvider`（gateway/payload 权威事实）；
  - 没有从 Broker 接收回执并调用 `acceptReceipt` 的入口；
  - 没有厂商/负载航权等待；
  - 真实 Postgres、真实 Broker、真实设备均未验证。

## 四、下一步

1. 前端计划卡已完成，可在模拟器计划模式下直接验证。
2. **J1–J4 已推送（远端 `1abacb8`）；J5a 本地提交 `332bc5d`**，待确认后再推送。
3. J5b 需要：真实发布器与连接绑定、权威 gateway/payload provider、Broker 回执入口、厂商/负载航权等待、真实 Postgres 验证。J5b 涉及真实链路，必须单独授权。
