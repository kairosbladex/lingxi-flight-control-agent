# 真实平台联调前置条件清单（2026-10-27）

并行交付线 E（平台联调前置）。本文件回答一个问题：**在不碰 `src/` 运行时、不接真实平台的前提下，真实联调还缺哪些外部条件，缺了各自怎么降级。**

结论先行：

- 本仓侧已把“平台应该返回什么、现有只读路径怎么拒绝”固化成完全离线的契约 mock 与冒烟：`python3 tools/integration/platform_conformance_smoke.py` 当前 **PASS 23/23，退出码 0**。
- 真实联调仍卡在外部条件：Java 测试环境与令牌、权威快照字段、控制权租约/心跳、指令回执、电子围栏服务、空域批件、实机与载荷、书面授权。
- **没有 C2 的书面授权，任何人不开启 `--enable-platform-control`，不发送真实飞控指令。**

负责人栏统一留空，由项目负责人指派后填写。截止时间统一建议 **2026-10-28 18:00**（每项可单独提前）。

---

## 0. 本次做了什么 / 没做什么

| 项 | 状态 | 位置 / 证据 |
| :--- | :--- | :--- |
| 契约 mock 套件（权威快照、围栏、控制权、挂载与算法） | 已交付，纯内存/假 opener，无 socket | `tools/integration/` |
| 平台契约一致性冒烟 | 已交付，逐项 PASS/FAIL + 退出码 | `tools/integration/platform_conformance_smoke.py` |
| 复用 MQTT 遥测模拟器 | 已复用，未改行为 | `tools/mocks/mqtt_telemetry_simulator.py` |
| 运行时只读装配验证 | 已覆盖：`build_platform_cockpit(enable_control=False)` 零网络构建 | `src/lingxi_flight_control/platform_runtime.py` |
| 改 `src/` 运行时 | **未做**（本线硬约束） | `git diff --stat src/` 为空 |
| 接真实 Java / 围栏 / MQTT / 设备 | **未做** | 无凭据、无授权、无环境 |
| 真实飞行、真实算法启停、真实遥测精度 | **未验证** | 见第 4 节“平台缺失/需对方确认” |

复跑（不需要任何环境变量或凭据）：

```bash
python3 tools/integration/platform_conformance_smoke.py   # 期望 PASS 23/23，exit 0
bash tools/ci/offline_tests.sh                            # 全量离线回归
python3 -m compileall -q src tests tools
git diff --check
```

---

## 1. 冒烟覆盖矩阵（mock ↔ 现有代码路径）

| 分组 | 断言 | 消费的现有代码 |
| :--- | :--- | :--- |
| A1 | 只读快照严格解码；`observed_at` 取采样/接收较早者；owner/租约/版本可见 | `ktd_snapshot.KtdSnapshotProvider`、`ktd_platform.KtdHttpTransport` |
| A2 | `route_status=null`：11 个飞行动作被 `ROUTE_STATE_SOURCE_UNAVAILABLE` 拦截；只读带降级标记 | `adapters/ktd_platform_adapter.KtdPlatformAdapter` |
| A3 | `route_status` 字段缺失（平台未知）：适配器拒绝 + 严格读取器 `AUTHORITATIVE_STATE_INCOMPLETE` | `ktd_platform_adapter`、`ktd_snapshot._state` |
| A4–A6 | 控制权他人 / 租约过期 / 租约缺失 → `SNAPSHOT_AUTHORIZATION_CONFLICT` | `ktd_snapshot._control`、`_authorization` |
| A7 | 心跳续租 +300s；停更 301s 后拒绝 | `control_authority_mock` + `ktd_snapshot._control` |
| A8 | `availability=available` 但 `capability=false` → 拒绝 | `ktd_snapshot._authorization` |
| A9 | 距离 0.01 / 20 米接受；0.009 / 20.001 拒绝 `DISTANCE_OUT_OF_RANGE` | `ktd_platform.KtdPlatformExecutor._compile` |
| A10 | D10 机型拒绝；PZ 用 `height_m`；DJI 缺 `altitude_m` 拒绝 `FLIGHT_POSE_UNAVAILABLE` | `ktd_snapshot` 机型白名单、`_compile` |
| A11 | 严格 v1 只接受 64 位十六进制 `snapshot_version`；字面串/短哈希/负数/整数拒绝 | `ktd_snapshot._fact_version` |
| B1–B5 | 围栏 clear / blocked / invalid / timeout / unavailable 五类；超时与不可用只降级警告、不阻断 | `ktd_path.KtdPathChecker`、`flight_policy.evaluate_path` / `check_path` |
| C1–C3 | 控制权 obtain / release / 抢占 / 过期清理；非 owner 不能代释放 | `control_authority_mock` 契约 |
| D1 | 未挂载镜头/探照灯按 4.3.2 原文拒绝；清单缺失保持“未核验” | `flight_policy.payload_mount_message` |
| D2 | 标注到达 → `started` 确认；离开 → `stopped` 确认；六阶段分开记录 | `MemoryFlightControlSimulator.apply_task_event`、`ai_lifecycle` |
| E1 | MQTT 遥测采样与低电量告警 | `tools/mocks/mqtt_telemetry_simulator.py` |
| F1 | 平台运行时只读装配：构建零网络；`/state`、`/context` 可用；控制动作不可用；预览 `PLATFORM_DISABLED` | `PlatformRuntimeConfig`、`build_platform_cockpit`、`CockpitService` |

冒烟跑通只证明**契约形状与 fail-closed 行为**，不证明真实平台会按这些值返回，也不证明设备执行。

---

## 2. 三块前置条件

### A. Java 平台

#### A1 测试环境地址、只读凭据与可信网关
- **需要什么**：固定 origin（可选 `/api`）、服务端令牌、员工 `empId`、项目/站点/机库/无人机/`taskId` 绑定；网关令牌只由服务端私密注入。
- **为什么（代码路径/契约）**：`platform_runtime.PlatformRuntimeConfig.from_environment` 读取 `KTD_BASE_URL`、`KTD_SERVER_TOKEN`、`KTD_ACTOR_EMPLOYEE_ID`、`KTD_PROJECT_ID`、`KTD_SITE_ID`、`KTD_HANGAR_ID`、`KTD_DRONE_ID`、`KTD_TASK_ID`、`FLIGHT_AGENT_GATEWAY_TOKEN`；`KtdHttpTransport` 默认只允许 HTTPS，内网明文必须显式 `KTD_ALLOW_INSECURE_HTTP=true`。详见 `docs/ktd-platform-contract.md`。
- **验收标准**：同机回环或固定 HTTPS 可通；缺失/错误令牌被拒绝；`/state`、`/context` 能读到真实绑定；服务端日志与仓库不出现令牌。
- **负责人**：（留空）
- **截止时间**：2026-10-28 18:00（建议）
- **拿不到时的降级方案**：继续用 `tools/integration/` mock 与只读装配冒烟；真实联调顺延，不启用写路径。

#### A2 权威快照 v1：端点、版本与时效字段
- **需要什么**：`GET /lingxi/agent/platform/snapshot` 返回 `lingxi-platform-snapshot/v1`；含 `schema_version`、64 位十六进制 `snapshot_version`、`generated_at`、`expires_at`、`telemetry.sampled_at/received_at/source/version`，且 `expires_at = min(sampled_at, received_at) + 5`。
- **为什么（代码路径/契约）**：`ktd_snapshot.KtdSnapshotProvider` 严格解码这些字段；`observed_at` 取采样/接收较早者，未来时间、缺失、过期、`expires` 不等于观测时间 +5 都拒绝（`AUTHORITATIVE_STATE_STALE`）。`docs/ktd-platform-contract.md`「Java 只读快照契约的来源」。
- **验收标准**：真实返回能被 `KtdSnapshotProvider` 解码；版本来自可追溯的服务端状态版本，不随读取次数伪造；5 秒内新鲜。
- **负责人**：（留空）
- **截止时间**：2026-10-28 18:00（建议）
- **拿不到时的降级方案**：保持 fail-closed，不做“无位姿/无时间”降级；只读展示返回不可用，不假装已核验。

#### A3 控制权 owner、租约与心跳
- **需要什么**：`control_owner_employee_id`、`control_owner_updated_at`、`control_lease_expires_at`、`control_owner_version`；权威租约语义为 `updated_at + 300s`；明确谁发 MQTT 心跳、断更多久清理、读取是否续租。
- **为什么（代码路径/契约）**：`ktd_snapshot._control` 要求 lease 恰好等于 `updated_at + 300`；`ktd_platform.KtdPlatformExecutor._validate_snapshot` 返回 `CONTROL_NOT_OWNED` / `CONTROL_LEASE_UNAVAILABLE` / `CONTROL_LEASE_EXPIRED`；`authority_contract.py` 把 owner/lease 列为 core 字段。
- **验收标准**：快照能读到 owner 与有限租约；租约不因 GET 续期；他人员工 ID 与当前 `empId` 可比对；心跳停更后写动作被拒绝。
- **负责人**：（留空）
- **截止时间**：2026-10-28 18:00（建议）
- **拿不到时的降级方案**：只读 `read_state`；所有写动作继续 `CONTROL_LEASE_UNAVAILABLE` 拒绝，不用“当前时间 + 默认 TTL”补租约。

#### A4 逐动作权限、能力标志、机型白名单与距离范围
- **需要什么**：59 项动作的 `permissions`、`capabilities`、`action_availability`（available + reasons）；真实 `UAV_MOVE_DISTANCE` 值；`profile` 只能是 `DJI_HANGAR` / `PZ_HANGAR`；DJI 提供 `altitude_m` 与 `height_m`，PZ 提供 `height_m`；确认 DJI 相对高度低于 20 米时是否仍先爬升到 20 米。
- **为什么（代码路径/契约）**：`ktd_snapshot._authorization` 要求 `permissions == available 集合` 且可用动作 capability 必须为 `true`；`ktd_platform._compile` 强制 0.01–20 米（`DISTANCE_OUT_OF_RANGE`）、机型白名单（`DISTANCE_MODE_UNAVAILABLE`/`PROFILE_UNSUPPORTED`）、DJI 20 米隐含爬升（`DJI_FLYTO_IMPLICIT_CLIMB`）与目标椭球高 2–10000 米。
- **验收标准**：距离 0.01/20 米可预览、0.009/20.001 被拒绝；PZ 不套用 DJI 20 米规则；能力标志与按钮配置不一致时以服务端逐动作决定为准。
- **负责人**：（留空）
- **截止时间**：2026-10-28 18:00（建议）
- **拿不到时的降级方案**：只开放已书面确认的六向位移；拍照/转向/云台/指点/环绕继续拒绝，不用静态 capability 补缺。

#### A5 指令回执 v1（tracking）与 Java 侧开关/DDL
- **需要什么**：`tracking` 是否已部署；`lingxi.agent.command-tracking.enabled` 是否打开；命令表是否按人工 DDL 迁移；`/lingxi/agent/commands/receipt` 是否返回与请求绑定的状态；unknown 是否保留、不自动重发。
- **为什么（代码路径/契约）**：`ktd_platform.KtdHttpTransport.submit` 的 tracking 分支、`command_receipts.py`、`KtdPlatformExecutor.confirm` 的“先写 unknown 再 submit”；契约见 `docs/ktd-platform-contract.md`「指令回执与显式续接 v1」。
- **验收标准**：一次动作只产生一次可查询回执；`submitted` 不等于设备完成；超时/5xx 停在 `unknown`，重启不重发；回执字段与身份/资源/动作/参数绑定。
- **负责人**：（留空）
- **截止时间**：2026-10-28 18:00（建议）
- **拿不到时的降级方案**：不启用 `track_commands` 写路径；只做只读联调；已提交命令保留 journal，人工核对。

#### A6 航线事实：`route_status` / `route_id` / `flight_status`
- **需要什么**：平台真实取值域（`idle`/`running`/`paused`/…），并承诺**不用字面 `"unknown"` 代替 null**；`route_id`、`breakpoint_index` 是否返回；自动航线执行中微操的拦截口径。
- **为什么（代码路径/契约）**：`adapters/ktd_platform_adapter.py` 只对 `route_status is None` fail-closed；严格读取器把 `state.route_status` 缺失视为 `AUTHORITATIVE_STATE_INCOMPLETE`，但字面 `"unknown"` 会被当成普通字符串。`authority_contract.ROUTE_GATED_ACTIONS` 要求微操/指点/环绕能证明“当前不在自动航线”。
- **验收标准**：真实快照中微操前 `route_status` 有确定值；null/缺失时 11 个飞行动作全部拒绝；若平台必须用 `"unknown"` 表达未知，先提代码变更（本线不改 `src/`）。
- **负责人**：（留空）
- **截止时间**：2026-10-28 18:00（建议）
- **拿不到时的降级方案**：只读联调；微操/指点/环绕继续拒绝，不做“看起来没在飞所以可以动”的推断。

#### A7 算法粒度启停接口（当前缺失）
- **需要什么**：按算法 ID 的启动/停止接口及回执；或书面确认平台只有任务级 `videoDetect` 总开关，没有算法粒度生命周期。
- **为什么（代码路径/契约）**：`ai_lifecycle.AlgorithmLifecycleGateway` 协议要求 `start/stop` 回执；`ktd_platform.preview` 对 `ai_algorithm_set` 固定返回 `ALGORITHM_LIFECYCLE_UNAVAILABLE`，不把任务级总开关冒充算法运行状态。
- **验收标准**：有接口时能给 `started`/`stopped` 回执并与标注到达/离开对账；没有接口时平台书面确认，前端与模型话术保持不变。
- **负责人**：（留空）
- **截止时间**：2026-10-28 18:00（建议）
- **拿不到时的降级方案**：算法生命周期只在模拟器验证（`execution_mode=simulator`/mock），不声称平台已支持。

---

### B. 空域与实机

#### B1 空域批件 / 临时空域许可
- **需要什么**：批件文号、有效时段、区域边界、高度上限、报备回执；如涉及机场净空区，给净空审批结论。
- **为什么（代码路径/契约）**：真机飞行的外部合法性前提；本仓任何代码都不能替代空域审批。围栏只检查“是否碰到限制区”，不生成批件。
- **验收标准**：批件在计划飞行时段内有效，坐标/高度覆盖计划航线；报备回执可查。
- **负责人**：（留空）
- **截止时间**：2026-10-28 18:00（建议）
- **拿不到时的降级方案**：只做地面/仿真演练与只读联调，不真飞；不把 mock 围栏 clear 当空域许可。

#### B2 电子围栏服务可用性与五类实测
- **需要什么**：`POST /electronicFence/path-route/with-clean` 的地址、账号、项目/操作者绑定；由平台方实测 `clear` / `blocked` / `invalid` / 超时 / 不可用五类，并给出正常延迟基线。
- **为什么（代码路径/契约）**：`ktd_path.KtdPathChecker` 默认 3 秒硬预算；`flight_policy.check_path` 对 `clear` 放行、`blocked`/`invalid` 拒绝、`timeout`/`unavailable`/`malformed` 只降级警告（V1.4 2.2.2“不可用即跳过不阻断”）。
- **验收标准**：五类都能复现；超时/不可用返回明确告警且不阻断业务；折线点数/分段数、`projectId`/`operatorId`、`snapshot` 回显与请求一致。
- **负责人**：（留空）
- **截止时间**：2026-10-28 18:00（建议）
- **拿不到时的降级方案**：围栏检查降级为警告，保留“不代表路径安全”的提示；不把“接口不可用”说成“路径安全”。

#### B3 实机机型、固件、机库与无人机 SN
- **需要什么**：计划使用的机型/固件版本、机库编号、无人机 SN、当前在线状态、可飞时段；确认与 Java `profile` 对应关系。
- **为什么（代码路径/契约）**：`ktd_snapshot` 只接受 `DJI_HANGAR` / `PZ_HANGAR`；`ktd_platform._compile` 的高度基准、20 米规则、跟踪回执能力都按机型分支；`KtdHttpTransport` 写请求带 `terminalId`/`siteId`/`taskId`。
- **验收标准**：绑定资源与现场设备一致；机型/固件能映射到唯一 profile；不把展示品牌名当操作器路径。
- **负责人**：（留空）
- **截止时间**：2026-10-28 18:00（建议）
- **拿不到时的降级方案**：不真飞；只做只读快照与 mock 联调。

#### B4 挂载镜头/载荷清单（`mounted_lenses` / `mounted_payloads`）
- **需要什么**：当前机库/无人机实际挂载的镜头（electro_optical / infrared / wide）与载荷（megaphone / searchlight / methane / dropper）；平台是否提供查询接口或快照字段。
- **为什么（代码路径/契约）**：`flight_policy.payload_mount_message` 只在清单**存在**时按 4.3.2 原文拒绝缺失镜头/探照灯；清单缺失表示“未核验”。注意：现有 v1 快照 `ktd_snapshot._POLICY_FIELDS` **没有** `mounted_lenses`/`mounted_payloads`，模拟器状态才有，所以这是平台侧新增字段/接口需求。
- **验收标准**：平台能返回清单或明确“不提供”；不提供时话术保持“未核验”，不宣称未挂载。
- **负责人**：（留空）
- **截止时间**：2026-10-28 18:00（建议）
- **拿不到时的降级方案**：不宣称已挂载/未挂载；红外、广角、探照灯动作由平台能力标志与执行器继续兜底拒绝。

#### B5 现场人员、保险与应急处置
- **需要什么**：飞手、观察员、保险、应急返航/急停责任人、现场通信方式；明确“谁有权按急停/返航”。
- **为什么（代码路径/契约）**：`emergency_stop`/`return_home` 仍需要控制权与确认卡；代码不能替代现场安全责任与空域指挥。
- **验收标准**：名单与联系方式可查；急停/返航责任人到岗；保险覆盖计划机型与区域。
- **负责人**：（留空）
- **截止时间**：2026-10-28 18:00（建议）
- **拿不到时的降级方案**：不真飞；只做地面/仿真演练。

#### B6 首次动作范围与平台限制确认
- **需要什么**：书面确认首次真飞允许的动作白名单（建议只做六向位移 0.01–20 米 + 急停/返航）；确认 DJI 20 米隐含爬升、目标椭球高 2–10000 米、PZ 相对高基准；确认拍照/转向/云台是否仍被平台拒绝。
- **为什么（代码路径/契约）**：`ktd_platform._compile` 的距离/高度门禁与 `SUPPORTED_ACTIONS`；`docs/ACCEPTANCE_KTD-REAL-FLIGHT.md` 明确“前飞 10 米再拍照”尚未全链路验收。
- **验收标准**：白名单写入授权书；白名单外动作继续 `ACTION_UNSUPPORTED`/`TRACKING_UNSUPPORTED` 拒绝；不把预览成功当设备完成。
- **负责人**：（留空）
- **截止时间**：2026-10-28 18:00（建议）
- **拿不到时的降级方案**：不真飞；只做只读与离线冒烟。

---

### C. 测试环境与授权

#### C1 test/uat 环境与只读接入窗口
- **需要什么**：test/uat 地址、可登录账号（只读优先）、允许的联调时段、是否允许与生产隔离；是否需要 VPN/跳板机。
- **为什么（代码路径/契约）**：`PlatformRuntimeConfig.from_environment` + `build_platform_cockpit(enable_control=False)` 是只读装配入口；`KtdHttpTransport` 固定 origin、不跟随重定向、不继承代理。
- **验收标准**：只读装配可启动；`/state`、`/context` 返回真实绑定；日志无凭据；不影响生产。
- **负责人**：（留空）
- **截止时间**：2026-10-28 18:00（建议）
- **拿不到时的降级方案**：只跑离线 mock；前端与文档交付不阻塞。

#### C2 书面授权 `--enable-platform-control` 与动作白名单
- **需要什么**：书面授权（谁授权、授权范围、有效时段、允许动作白名单、撤回方式）；明确“未授权不开启”。
- **为什么（代码路径/契约）**：`tools/platform_cockpit_server.py` 的 `--enable-platform-control` 默认关闭；`KtdPlatformExecutor(enabled=False)` 时 `available_actions()` 只返回 `read_state`、预览返回 `PLATFORM_DISABLED`；AGENTS.md 与 `docs/ktd-platform-contract.md` 均要求逐动作授权。
- **验收标准**：授权书写明动作、机型、时段、责任人；关闭开关即回退；授权缺失时任何写接口都不可用。
- **负责人**：（留空）
- **截止时间**：2026-10-28 18:00（建议）
- **拿不到时的降级方案**：**绝不开启真实写接口**；只做只读 + 契约冒烟。

#### C3 凭据注入、HTTPS/回环与 journal 目录
- **需要什么**：`KTD_SERVER_TOKEN`、`FLIGHT_AGENT_GATEWAY_TOKEN` 的服务端注入方式；HTTPS 证书或同机回环方案；私有持久 SQLite journal 目录（绝对路径、0700、仅当前用户）。
- **为什么（代码路径/契约）**：`platform_runtime.build_platform_cockpit` 校验 journal 父目录存在、非符号链接、权限不含 077、属主为当前用户；`KtdHttpTransport` 默认 HTTPS，明文 HTTP 需 `KTD_ALLOW_INSECURE_HTTP=true`；凭据不得进 Git/聊天/浏览器。
- **验收标准**：凭据只存在于服务端环境；journal 文件权限 0600；重启后 unknown 记录仍在、不自动重发。
- **负责人**：（留空）
- **截止时间**：2026-10-28 18:00（建议）
- **拿不到时的降级方案**：不启动平台入口；只跑离线 mock。

#### C4 Java 命令表迁移与发布审批
- **需要什么**：命令表人工 DDL 的执行人/窗口/回滚；Java 候选模块发布审批；`lingxi.agent.command-tracking.enabled` 开关责任人。
- **为什么（代码路径/契约）**：`docs/ktd-platform-contract.md` 明确 DDL 人工迁移、未对 MySQL 执行、tracking 默认 false；`SqliteCommandJournal` 只解决 Agent 侧幂等，不替代 Java 持久回执。
- **验收标准**：DDL 在目标库执行并核验；发布后 tracking GET 可查；回滚不删除账本、不把 unknown 标成功。
- **负责人**：（留空）
- **截止时间**：2026-10-28 18:00（建议）
- **拿不到时的降级方案**：不启用 tracking 写路径；只读联调。

#### C5 发布/回滚/值班与观察窗口
- **需要什么**：发布授权人、回滚步骤、停止命令、值班人、观察窗口；明确 10086 等共享实例是否允许触碰。
- **为什么（代码路径/契约）**：`docs/ACCEPTANCE_KTD-REAL-FLIGHT.md` 记录了独立服务、停止命令与回滚目录；共享 8088 与生产实例必须保持不动。
- **验收标准**：停止命令可执行；回滚目录存在；值班人确认；共享实例无变更。
- **负责人**：（留空）
- **截止时间**：2026-10-28 18:00（建议）
- **拿不到时的降级方案**：不发布、不真飞；只在本地跑离线证据。

#### C6 验收边界与第二方评审
- **需要什么**：书面确认本轮只做“只读 + 契约冒烟”还是包含真实飞控；指定第二方评审人；确认 `docs/ACCEPTANCE_KTD-REAL-FLIGHT.md` 的未完成项不被当作已完成。
- **为什么（代码路径/契约）**：本仓规则要求未验证部分明确说明；`docs/ACCEPTANCE_KTD-REAL-FLIGHT.md` 明确“HTTP 受理/确认卡/`confirmed_not_dispatched` 都不代表设备执行”。
- **验收标准**：验收单写明动作、证据、未验证项；第二方评审签署；不把 mock PASS 当真实联调 PASS。
- **负责人**：（留空）
- **截止时间**：2026-10-28 18:00（建议）
- **拿不到时的降级方案**：只交付离线证据与前置清单；真实联调标记为未开始。

---

## 3. 平台缺失 / 需对方确认的字段

| 字段/能力 | 当前本仓侧状态 | 需要谁确认 | 拿不到的影响 |
| :--- | :--- | :--- | :--- |
| `control_owner_updated_at` / `control_lease_expires_at` / `control_owner_version` | 严格读取器要求；GET `/uavControlPower/list` 不返回这些时间 | Java 平台 | 租约无法证明新鲜，写动作全部拒绝 |
| `telemetry.sampled_at` / `received_at` / `version` | 严格读取器按 5 秒时效校验；缺一即拒绝 | Java 平台 | 无法证明遥测新鲜，只读也可能不可用 |
| 59 项 `permissions` / `capabilities` / `action_availability` | 严格读取器要求三者一致；按钮配置不算逐动作授权 | Java 平台 | 无法生成可用动作集合，写动作拒绝 |
| `UAV_MOVE_DISTANCE` 真实值 | 10086 当前为 false（见验收文档） | Java 平台 | 距离动作 `DISTANCE_MODE_UNAVAILABLE`，只能只读 |
| `profile` 与高度基准 | 只接受 `DJI_HANGAR` / `PZ_HANGAR`；DJI 需 `altitude_m`+`height_m`，PZ 需 `height_m` | Java 平台 + 实机 | 机型不明即拒绝，不能猜品牌 |
| `route_status` 字面 `"unknown"` | 适配器只对 `None` fail-closed；字面串会被当普通值 | Java 平台（或后续代码变更） | 可能把“未知”当“可执行”，需平台保证不用字面 `"unknown"` |
| `route_id` / `breakpoint_index` | 快照可选字段；断点必须有 route_id | Java 平台 | 恢复/继续类动作无法证明航线绑定 |
| 指令回执 v1 / `tracking` | Java 候选默认关闭；DDL 未对 MySQL 执行 | Java 平台 | 只能拿到 submitted/unknown，不能关联设备结果 |
| `mounted_lenses` / `mounted_payloads` | **v1 快照白名单没有这两个字段**，只在模拟器状态与话术层 | Java 平台（新增字段/查询接口） | 只能保持“未核验”，不能判断真实挂载 |
| 算法粒度启停 | 平台只有任务级 `videoDetect` 总开关 | Java 平台 | `ai_algorithm_set` 固定 `ALGORITHM_LIFECYCLE_UNAVAILABLE` |
| 厂商航权 / DRC 链路新鲜度 | 快照无对应字段；`control_owner` 不等于厂商航权 | Java 平台 + 厂商 | 不能证明持续授权，写动作保持关闭 |

---

## 4. 可直接复制转发的中文短话术（≤25 行）

```text
【灵犀飞控 · 真实平台联调前置｜请 10-28 18:00 前书面确认】
一、Java 平台（对接 ktd_snapshot.py / ktd_platform.py / ktd-platform-contract.md）
1) 测试环境地址、只读凭据、GET /lingxi/agent/platform/snapshot 的 64 位 snapshot_version。
2) 员工 empId 与 project/site/hangar/drone/taskId 绑定；taskId 必须是真实 Java 任务。
3) 控制权 owner/updated_at/lease=updated_at+300s；MQTT 心跳由谁发、断更多久清理。
4) 逐动作 permissions/capabilities/action_availability；UAV_MOVE_DISTANCE 当前真实值。
5) 机型 profile（DJI_HANGAR/PZ_HANGAR）、高度基准；DJI 20m 隐含爬升是否仍生效。
6) 指令回执 v1（tracking）是否部署、DDL 是否人工执行；哪些字段现在给不了要书面列出。
二、空域与实机
1) 空域批件/临时许可：起止时间、坐标范围、高度上限、报备回执编号。
2) 电子围栏地址与账号；clear/blocked/invalid/超时/不可用五类由平台方实测确认。
3) 实机机型、固件、机库/无人机 SN、挂载镜头与载荷清单（mounted_lenses/mounted_payloads）。
4) 现场飞手、观察员、保险、应急返航/急停责任人；允许的首次动作范围。
5) 算法粒度启停：有接口给接口与回执；没有请书面确认只有任务级 videoDetect 总开关。
三、测试环境与授权
1) test/uat 环境与部署窗口；本轮只读接入，不开启 --enable-platform-control。
2) HTTPS 证书或同机回环方案；KTD_SERVER_TOKEN 等仅走服务端私密注入，不进 Git/聊天。
3) 私有 journal 目录（0700 绝对路径）与命令表迁移审批；unknown 命令不重发、保留账本。
4) 发布/回滚授权人与观察窗口；验收边界书面确认：只读 + 契约冒烟，不发真实飞控指令。
5) 回复请按「谁 / 何时 / 给什么」；缺项走降级方案（只读 + 离线 mock），不阻塞前端/文档。
参考：docs/evidence/platform-integration-prereq-20261027.md（三块清单与验收标准）。
```

---

## 5. 验证记录与 Git 状态

- `python3 tools/integration/platform_conformance_smoke.py`：**PASS 23/23，exit 0**（2026-10-27 本机，纯离线，无 socket）。
- `bash tools/ci/offline_tests.sh`：全量离线回归通过（基线 1322 项；本线未改 `src/`、未新增 discover 用例）。
- `python3 -m compileall -q src tests tools`：通过。
- `git diff --check`：通过。
- 本线未修改 `src/`、未接真实平台、未推送；`git diff --stat src/` 为空。
- 提交为小而明确的中文 commit；commit SHA 记录在交接消息中，避免文档自引用。

**未验证**：真实 Java 返回、真实围栏五类、真实控制权心跳、真实设备位移/拍照/转向/云台、真实算法启停、真实 MQTT 遥测精度、空域批件有效性。冒烟 PASS 只代表契约形状与现有 fail-closed 行为一致。
