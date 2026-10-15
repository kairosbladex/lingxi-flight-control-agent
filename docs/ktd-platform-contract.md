# ktd_backend 控制接口适配候选

2026-10-16。交付是默认关闭的 Python HTTP 适配器与离线契约测试。未连接 Java 服务、MQTT 或无人机，未部署，设备动作完成、距离精度和测试环境视觉一致性均未验证。

## 审计基线与范围

- Agent 仓库基线：`refactor/agent-project-naming@c93ab9a67555eb46b448204a1615ab70ff7f0839`，开发分支 `feature/natural-language-cockpit`。
- Java 只读源码：`/Users/wangxiaolin/PycharmProjects/ktd_backend-b3-java`，`feature/cockpit-control-proxy@dd11a47f91d7bde83a67b09537432dd2453a8a6d`，审计时工作区干净。
- 现有驾驶舱参考：`/Users/wangxiaolin/PycharmProjects/onegismap_a-b4-20260910`，`feature/cockpit-control-preview@b009a80bf1d2755ab41ebd1cddd6ee18922e5125`，存在合并冲突与用户改动，只读保留。
- 本切片文件：`src/lingxi_flight_control/ktd_platform.py`、`tests/test_ktd_platform.py`、`tools/bench/ktd_http_smoke.py`、本文档；不修改 Java 或参考前端。
- 执行身份与副作用：本机离线测试进程；仅临时 SQLite 日志和回环 HTTP 测试服务；没有平台登录、平台业务写入或设备调用。

以下 Java 路径均相对于上述 Java 仓库，前端路径均相对于上述参考驾驶舱。来源是本地源码，不能据此确认测试环境当前运行版本。

## 已核验的 HTTP 契约

应用 context-path 为 `/api`；表中不重复该前缀。公共资源由服务端绑定生成，模型不能提交原始 DTO。每个写请求包含 `terminalId`（无人机 ID）、`siteId`（Java 站点 ID），存在真实任务时包含 `taskId`。`RuntimeBinding.task_batch_id` 是 Agent 上下文标识，不能代替 Java `taskId`。

| 候选动作 | 方法与路径 | 明确编译参数 | 接入限制 |
| --- | --- | --- | --- |
| 前、后、左、右、上、下移动 | POST `/screen/uav/operation/control` | `mode:2`，`type:202/203/204/205/201/-201`，`param:distance_m` | 0.01–20 米；仅 DJI_HANGAR/PZ_HANGAR；该动作与 `UAV_MOVE_DISTANCE` 都必须明确可用 |
| 暂停、恢复航线 | POST `/screen/uav/route/control` | `type:110/112` | DJI_HANGAR；必须有真实 Java 任务 |
| 急停、返航、取消返航 | POST `/screen/uav/route/control` | `type:113/114/-114`；返航额外 `interrupt:false` | DJI_HANGAR；取消返航必须有真实 Java 任务 |
| 拍照、开始录像、停止录像 | POST `/screen/uav/camera/control` | `type:100/101/102` | DJI_HANGAR；对应载荷能力必须由权威适配器确认 |
| 云台复位 | POST `/screen/uav/gimbal/control` | `type:403` | DJI_HANGAR；实际会涉及 Java 的飞行与负载控制权调用 |
| 读取状态 | 不发送控制 HTTP | 注入的权威状态提供器 | 返回 `observed`，没有写副作用 |

定义位置：`sys-core/src/main/java/com/fan/framework/operation/uav/dto/UavControlDTO.java:23`、`FlightRouteControlDTO.java:21`、`CameraControlDTO.java:30`、`GimbalControlDTO.java:22`。入口为 `biz-visualization/src/main/java/com/fan/framework/visualization/controller/LargeScreenController.java:1280`（移动）、`:1141`（航线）、`:1505`（相机）、`:1390`（云台）。

前端对应 `src/api/hangar.ts:43`、`:51`、`:59`、`:67`、`:75`。`src/views/hangar/components/controlCenter.vue:1038` 按 `controlType` 选择速度、距离或杆量；不是所有机型都具有相同模式。

## 不能自动扩大支持的差异

1. **距离实现存在，但能力声明与实现有差异。** `DJHangarOperation.java:494` 从 `UdpDatas` 读取经纬度、绝对高度和 yaw，以 `GeoUtil.calcLatAndlon` 转目标点，再以固定最大速度 5 m/s 调用 `fly_to_point`；`PZHangarOperation.java:316` 使用 `height`，转成厘米后下发。候选因此分别要求权威 `altitude_m` 或 `height_m`，不能混用高度基准。`D10Operation.java:593` 走摇杆速度路径，候选拒绝 D10。
2. **当前声明不能被候选覆盖为 true。** `LargeScreenController.java:2310` 对 DJI 只声明速度或杆量，对 PZ 只声明速度；`:2322` 仅给 ZHUOYI 距离能力，但 `:1349` 该旧品牌操作器为空。候选必须拒绝这些实际为 false 的距离能力；测试夹具里的 true 是显式构造的条件样本，不是平台已开放证明。
3. **角度不等于距离模式里的时长。** `DJHangarOperation.java:533` 的偏航分支按参数绝对值循环发送角速度，未证明其精确角度语义。因此 `turn_left/right` 暂不编译；连续速度、杆量、键盘模式也暂不支持。
4. **起飞高度基准未对齐。** `FlightRouteControlDTO.takeoffPointAlt` 直接传到 `TakeoffToPointDTO.target_height`；后者明确是 WGS84 绝对高度。现有工具 `takeoff.altitude_m` 的相对起飞语义不能直接透传。起飞暂不编译。DJI `DJHangarOperation.java:282` 明确拒绝当前点降落，故也不映射 `land`。
5. **控制权查询可能写业务。** GET `/uavControlPower/obtain` 会保存控制权，且任务创建者可能自动抢占（`UavControlPowerController.java:50`、`:183`）；不能当只读检查。申请/转让会发消息。候选不实现这些自动接管动作，权威适配器必须在执行前证明已持有控制权。
6. **速度/杆量需要持续心跳。** DJI 控制序列检查所有权、3 秒过期时间（`DJHangarOperation.java:298`、`:352`、`:485`），参考 UI 每秒发 `control/heartbeat/<hangarCode>`（`controlCenter.vue:1825`）。单次 HTTP 无法替代这个生命周期，本候选不创建 MQTT 心跳。

## 注入契约与确认门禁

```python
from lingxi_flight_control.ktd_platform import (
    KtdHttpTransport, KtdPlatformExecutor, SqliteCommandJournal,
)

# 参数和登录凭据只能来自服务端配置；示例不创建任何运行配置。
transport = KtdHttpTransport(base_url, token=server_token, actor_id=verified_actor_id)
executor = KtdPlatformExecutor(
    binding,
    site_id=java_site_id,
    task_id=java_task_id_or_none,
    transport=transport,
    snapshot_provider=authoritative_snapshot_provider,
    journal=SqliteCommandJournal(private_persistent_journal_path),
    enabled=False,
)
```

`KtdSnapshot` 是必须由另一个**服务端权威适配器**提供的内部类型，当前切片不实现该在线提供器。不得把浏览器 pose、浏览器自报 actor、模型输出或 HTTP 2xx 构造成权威快照：

| 字段 | 所需来源和含义 |
| --- | --- |
| `actor_id` | 与服务端请求使用的 Java 登录身份一致；不能用一个共享登录身份冒充不同操作者 |
| `project_id/hangar_id/drone_id/site_id/task_id` | 当前账号有权访问且绑定一致的 Java 资源；真实任务和设备关联也必须已核对 |
| `context_version` | 与 `RuntimeBinding` 一致的会话绑定版本 |
| `version` | 权威快照版本；权限、状态、能力变化时更新 |
| `observed_at` | UTC Unix 秒；默认 5 秒以内，未来时间、未知或过期拒绝 |
| `control_owner` | Java 当前控制权归属，写请求必须等于 actor |
| `control_lease_expires_at` | 权威 Java 提供的控制权租约到期时间，UTC Unix 秒；所有写动作要求有限数值且严格大于当前时刻；缺失默认 `None` 仅兼容构造，绝不授权写入 |
| `permissions` | 当前账号、项目、设备、任务范围批准的动作名 `frozenset`；逐动作授权 |
| `capabilities` | 当前机型、链路、设备状态实际允许的动作映射；必须是布尔 `True`；距离另需 `UAV_MOVE_DISTANCE=True` |
| `profile` | 实际 Java 操作器路径 `DJI_HANGAR` 或 `PZ_HANGAR`；不能只按展示品牌名称猜测 |
| `state` | 真实在线、在空中状态、经纬度、yaw、相关高度；没有字段即未知，不能填默认安全值 |

移动需要 `online/airborne=True`、有限经纬度和 `yaw_deg`，DJI 要求 `altitude_m`，PZ 要求 `height_m`。航线写操作要求明确在空中。已有 `ACTION_SPECS` 与请求验证先检查工具参数，再叠加平台范围限制。未映射动作返回 `ACTION_UNSUPPORTED`。

每次 `preview` 和首次 `confirm` 都重新读取权威状态并检查绑定、权限、控制权及其租约、能力、在线与时间。控制权租约缺失/无效返回 `CONTROL_LEASE_UNAVAILABLE`，到期或恰好处于到期时刻返回 `CONTROL_LEASE_EXPIRED`，不发送控制请求。即使新 OSD 刷新了 `observed_at`，也不能续租旧 owner。到期时间必须由权威 Java 提供，适配器不得按“当前时间加默认 TTL”或按 OSD 时间推算。`read_state` 可以显示缺失或过期的租约，但这不授予写权限。`available_actions()` 使用同一套门禁筛选界面可用动作；禁用时最多返回获准的 `read_state`，权威状态不可用时返回空集合。确认卡绑定请求、资源、状态版本及完整快照内容（不绑定单纯刷新时间）；任何实际字段变化都拒绝旧卡。默认 30 秒过期、最多 128 个未处理卡。`cancel` 只取消待确认卡，不调用急停或取消返航；已提交动作不能靠取消卡撤销。

HTTP 地址固定到服务端配置的 origin 和可选 `/api`，只允许 4 条 POST 路径。默认要求 HTTPS，内网明文 HTTP 必须显式设置 `allow_insecure_http=True`。不会继承环境代理、跟随重定向或重试。请求包含固定登录身份的 Authorization、`projectId`、追踪号和幂等键；不输出或持久化凭据、上游响应原文和异常文本。

`opener` 是仅供服务端测试的网络边界注入点；生产沿用默认实现，不能由浏览器或模型指定。替换它时仍须遵守禁止重定向、代理和自动重试的传输契约。

## 回执、幂等与恢复

`DJUtil.java:949` 生成 `fly_to_id` 后发布控制消息，没有把 ID 返回给 HTTP 调用方。`DJHangarOperation.java:550`、`:656` 的 `R.ok()` 也没有等待设备完成。当前 HTTP 契约不能给 Agent 关联到“已飞行 10 米”的独立完成证明。

- `preview_required`：仅预览，沿用原枚举以兼容现有 RuntimeExecutor 注册路径。
- `submitted`：HTTP 200 且 JSON `code` 严格为整数 200；仅表示 Java 接口受理。
- `rejected`：本地确定性门禁失败或明确业务拒绝。
- `unknown`：超时、连接异常、重定向、5xx、响应缺失/格式不明，或发出后无法持久确认结果；禁止自动重发。
- `observed`：读取权威快照，不是飞行动作完成。

每个回执始终包含 `execution_mode=platform`、`device_effect_verified=false`，不合成后续位姿。SQLite journal 在发请求前用唯一事务写入 `unknown`，成功受理后更新结果。进程崩溃、超时、并发执行器与重启均保留同一幂等键的发送屏障；SQLite 文件权限为 0600，必须位于私有、持久、受保护目录。平台请求头的幂等键不代表现有 Java 已实现服务端幂等，真正的本地防重复由 journal 提供。

上层必须为同一业务意图使用稳定幂等键，不能在 `unknown/submitted` 后换新键自动重试。`build_full_control_tool_registry` 默认随机键，接平台时需要注入稳定的 `idempotency_key_factory`，并让工作流暂停等待权威结果。删除、丢失、回滚 journal 会丢失发送历史，不能自动清理；当前没有自动“清除 unknown 再发”的接口。人工对照平台和设备结果后再决定新的独立动作。

剩余缺口包括权威快照在线来源、逐品牌能力差异修正、服务端执行前原子授权、关联设备回执的命令 ID/状态查询、真实遥测与位置精度验收。现有源码证据不足以将这些标为已完成。

## 验证和回滚

TDD 先得到缺失模块失败，再实现最小切片；后续测试先暴露布尔版本、未支持动作分类、长 JWT 配置、PZ 高度来源和 pending 上限错误，再修正。

```bash
PYTHONPATH=src python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_ktd_platform.py' -v
# 可独立运行；不进入 tests/discover，不访问任何外部平台。
PYTHONPATH=src python3 tools/bench/ktd_http_smoke.py
git diff --check
```

定向结果：30 个测试方法通过，含多组输入子场景。覆盖 15 个写动作映射、当前可用动作过滤、默认关闭、预览零网络、权限和资源变化、租约缺失/无效/过期及确认期间到期、参数越界、状态缺失/过期、取消、确认绑定、并发、持久幂等、超时/进程退出恢复、HTTP 路径/身份/重定向响应。单元测试只注入传输边界，不创建 socket 连接。独立烟测验证真实 HTTP 编解码和禁止重定向，仅向脚本自己的回环假服务发送 2 个请求。没有模型准确率或真实飞行分母，业务准确率 `not_evaluable`。

回滚：保持 `enabled=False` 或移除执行器注入即可阻止新增平台请求；已经提交的指令不能用代码回滚撤销，必须保留 journal 和追踪记录。此切片未启用任何在线平台流量。

## 权威在线快照来源的追加只读审计

同一 Java SHA 的源码审计确认：可获得部分身份和绑定事实，但**现有只读 HTTP 还不能完整、安全地生成本候选需要的在线 `KtdSnapshot`**。本次没有请求任何在线读接口，没有新增 Java 接口，也没有设置静态 capability 来补缺失真值。以下 URL 同样省略 `/api`。

| 所需事实 | 现有接口与返回字段 | 已核验来源、边界和缺口 |
| --- | --- | --- |
| 认证操作者、项目权限 | GET `/user/info`；请求头 `projectId`；`data.id/empId/permission` | `sys-core/.../controller/UserController.java:146` 调用 `SysUtil.currentUserWithPermission`；`sys-core/.../util/SysUtil.java:144` 从令牌 claims 找用户，`:231` 按项目筛选角色权限。控制 owner 使用员工 ID，因此 actor 必须取 `empId`，不能取用户 `id`。缺 claims 返回空 DTO，必须拒绝。此路径没有业务写入或设备消息。 |
| 当前账号可见项目 | GET `/screen/current/project`，可选 `sceneTypes/projectName` | `LargeScreenController.java:617`；`biz-inspection/.../service/impl/ProjectServiceImpl.java:486` 按当前机构及用户项目角色查询，`:507` 获取当前用户项目 ID。返回的可见项目列表可以约束绑定范围，不能证明某个动作获准。 |
| 站点、无人机、机库归属 | GET `/uavSite/list?projectId=<id>`；`data[].id/uavId/uavCode/hangarId` | `biz-inspection/.../controller/UAVSiteController.java:203` 至 `:221` 是按项目、未删除、启用状态查询的 SQL。方法自身没有当前用户授权检查，必须先与认证用户可见项目交叉核验，不能接受任意浏览器项目。单兵设备的项目领用校验可复用 `UavListQueryUtil.java:108` 的 `receiveDeptId`、未归还记录逻辑；该服务方法不是现成快照 HTTP。 |
| 当前平台控制权 owner | GET `/uavControlPower/list?uavId=<id>`；`data[].employeeId` | `biz-inspection/.../controller/UavControlPowerController.java:58` 至 `:75` 只查控制权记录，没有 obtain 的抢占、保存或消息副作用。接口不返回 `updateTime/leaseExpiresAt/ownerVersion`；`:169` 只检查无人机存在，没有项目归属校验。多条 owner 必须视为冲突，不能随意取第一条。 |
| 机型控制模式和载荷配置 | GET `/screen/control/permissions/3/<droneId>`，或 GET `/screen/control/permissions?taskId=<id>` | `LargeScreenController.java:2237`、`:2255`；`3` 来自 `Dict.TerminalType.WurenjiZhongduan`（`sys-core/.../constant/Dict.java:1348`）。返回 `uav.UAV_MOVE_SPEED/DISTANCE/STICK`、`load`、`loadInfoCode`、`pitch45/pitch90`、`isHangar` 等。主要按机型/载荷配置生成，不是逐动作实时授权；不包含 15 个候选动作的 allowlist、航权、心跳租约或版本。混合机库/模组时执行路径还依赖 `CacheConstant.UAV_LINK_TYPE`，不能只用品牌猜 profile。 |
| 部分实时飞行状态 | GET `/portal/task/hangar-view/state?uavId=<id>`；`uavId/uavSn/uavState/inFlight/landing/hangarViewVisible` | `biz-visualization/.../controller/TaskController.java:2711`；`service/impl/HangarViewStateServiceImpl.java:55` 从 `UdpDatas` 读取状态，按状态枚举计算 inFlight。`entity/vo/HangarViewStateVO.java:15` 至 `:31` 没有经纬度、高度、yaw、采样/接收时间或版本。该接口用于机库视角展示，不能单独证明当前 5 秒内的飞行状态。 |
| 完整 OSD 对象 | 调试 GET `/test/getUdpData?sn=<设备SN>` | `biz-inspection/.../controller/TestController.java:250` 直接返回 `UdpDatas.getUAV(sn)`；**`sys-core/src/main/resources/config/application.yml:119` 将 `/test/getUdpData*` 列入 `ignore-url`**，`WebSecurityConfig.java:150` 放行，`UrlFilterInvocationSecurityMetadataSource.java:52` 返回无权限要求。方法也没有项目/设备校验。本候选不通过这个免鉴权调试入口补齐权威数据。 |

上表的 `...` 仅省略同仓库统一前缀 `src/main/java/com/fan/framework`，不表示省略另一层服务。`LargeScreenController` 的完整相对路径已在前文给出。控制权、能力和展示状态接口能返回部分事实，但这些请求的 HTTP 200 不能自动合成逐动作授权。参考驾驶舱 `src/views/hangar/components/controlCenter.vue:533` 使用项目权限 `taskManage:Issued`；当前 Java 能力接口并没有把这个账号权限、设备归属和具体动作状态合成一个服务端决定。

### 控制权租约与遥测时间不能用读取时刻替代

- 平台控制权是有心跳更新的记录。`biz-inspection/.../task/UavControlPowerTask.java:34` 至 `:42` 把 `updateTime` 超过 **5 分钟** 的记录定时清理。GET list 不暴露该时间，可能读到等待清理的旧 owner；不能用“读到了 owner”证明租约新鲜。续租 MQTT 位于 `com/platform/mqtt/own/UavControlPowerProcessor.java:39`、`:69`，会更新业务记录，不是快照读取步骤。
- 内存 OSD 的接收时刻在 `sys-core/.../common/UdpDatas.java:433` 的 `TimedJsonObject.time` 中；`:34` 将存活阈值定义为 **60 秒**，`:445` 据此判断，`:446` 的 5 秒离线处理已注释。`:451` 至 `:454` 的过期读取还会移除缓存/在线列表并改为离线。返回 `UavPostureData` 没有暴露该接收时间或单调版本；读取 HTTP 的时间不等于最后收到设备数据的时间。
- 原始对象不是完全没有时间。`sys-core/.../common/UavPostureData.java:153` 有 `positionTime`；DJI 分支 `:952` 从设备消息 `timestamp`（毫秒）构造，PZ 分支 `:1674` 从设备 `timestamp`（秒）乘 1000 构造。可追溯的设备采样时间与服务端接收时间都应保留。不能因为 HTTP 刚返回，就将旧 OSD 的 `observed_at` 改为 `time.time()`；设备时钟未知或异常时应返回不可用。
- DJI 的经纬度、海拔、相对高度、yaw 分别来源于该类 `:960`、`:965`、`:967`、`:973`；PZ 对应 `:1679`、`:1687`、`:1692`、`:1698`，有明确厘米/角度缩放。已有对象可能混合多次增量消息，缺字段不能填 0 作为真值。

### 距离模式的心跳结论

**源码没有把 6 向距离模式放入速度/杆量相同的心跳前置条件。** DJI 的 `DJHangarOperation.java:485` 至 `:491` 仅在 mode=1/3 时启用心跳、登记定时指令和 3 秒到期时间；mode=2 在 `:494` 至 `:531` 直接计算目标点并调用 `DJUtil.fly_to_point`。`DJUtil.java:949` 至 `:962` 只编译并发布消息，没有检查心跳新鲜度。PZ 同样在 `PZHangarOperation.java:303` 至 `:313` 仅为速度登记心跳，`:316` 的距离分支直接调用 `PZUtil.pointing_flight`；后者 `sys-core/.../operation/pz/PZUtil.java:962` 至 `:975` 直接发布，固定水平速度 800 cm/s。构造器也没有启动心跳或获取航权。

`com/platform/mqtt/own/ControlHeartbeatProcessor.java:48`、`:55` 只续期上述已登记的运动指令。由此只能确认“当前 Java 距离分支没有这个保护”，不能推断真实厂商设备不要求已有飞行控制权、合适模式或连接健康。平台记录 owner、厂商飞行航权、DRC 会话、设备链路是不同事实。当前没有找到可只读、带时间和归属地证明后几项的 HTTP 契约，仍需缺失即拒绝。

### 最小 Java 只读快照契约建议（尚未实现）

建议新增一个明确鉴权的 GET `/lingxi/agent/platform/snapshot`，由 `projectId` 请求头和 `siteId/droneId/taskId` 查询参数限定资源；这是建议路径，当前不存在。操作者必须由当前登录令牌派生，不能从查询参数提供。服务端应检查项目权限、站点/机库/无人机关联、设备领用、真实任务设备关联及操作权限后，再返回一次不可变的快照；不得自动 obtain、发心跳、抢航权、启动视频或保存业务状态。

最小返回内容应包含：

1. `schema_version`、`snapshot_version`、`generated_at`、`expires_at`，以及明确绑定的 `actor_employee_id/project_id/site_id/hangar_id/drone_id/task_id`。生成时间只用于追踪，不能替代以下观测时间。版本须来自可追溯的服务端状态版本；不能固定为 1 或随 HTTP 读取次数伪造。
2. `control_owner_employee_id`、`control_owner_updated_at`、`control_lease_expires_at`、`control_owner_version`。租约过期或冲突必须显式标记；快照 GET 不续租。
3. `telemetry.sampled_at/received_at/source/sequence`、`online/uav_state/airborne/longitude/latitude/yaw_deg`、`altitude_m`（含明确垂直基准）和 `height_m`（含相对哪个起点/地面）。缺失、失联、设备时间不可信、样本超期分别返回原因；不可用当前 HTTP 时间代替。
4. 经真实控制链路解析的 `operation_profile`、当前厂商航权归属、会话/链路健康及其观测时间。服务端明确决定某动作是否依赖心跳，不把“Java 分支没有检查”当作已满足条件。
5. 基于账号授权、项目资源、机型实现、当前链路/模式/状态形成的逐动作 `allowed_actions` 和 `denied_reasons`，并保留 `UAV_MOVE_DISTANCE` 真实值。不能只返回前端按钮配置；不支持、不确定和临时不可用必须区分。

现有 Python `KtdSnapshot` 可由该服务端 DTO 经过严格验证后构造；构造前必须验证租约、采样/接收时间、链路/航权及动作限制。`observed_at` 取已验证的证据观测时间，不能取快照获取时间；`task_batch_id` 仍仅属于 Agent 会话，不冒充 Java taskId。未来执行端还应支持 `expected_snapshot_version` 的原子校验，避免“读取时获准、发送前已变更”的窗口；现有 HTTP 控制接口没有这个门禁。本轮交付保持适配器默认关闭，并清楚标记缺少在线权威提供器。
