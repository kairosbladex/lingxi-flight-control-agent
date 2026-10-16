# ktd_backend 控制接口适配候选

2026-10-16。当前候选包含默认关闭的 Python 控制适配器、Java 只读快照接口、严格的快照提供器和平台启动装配。未连接真实 Java 服务、MQTT 或无人机，未部署，设备动作完成、距离精度和测试环境视觉一致性均未验证。

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

1. **距离实现存在，但能力声明与实现有差异。** `DJHangarOperation.java:494` 从 `UdpDatas` 读取经纬度、绝对高度和 yaw，以 `GeoUtil.calcLatAndlon` 转目标点，再以固定最大速度 5 m/s 调用 `fly_to_point`；`PZHangarOperation.java:316` 使用 `height`，转成厘米后下发。DJI候选要求权威 `altitude_m`，并补充校验相对起飞点 `height_m` 以避免隐含爬升；PZ要求 `height_m`，不能混用高度基准。`D10Operation.java:593` 走摇杆速度路径，候选拒绝 D10。
2. **当前声明不能被候选覆盖为 true。** `LargeScreenController.java:2310` 对 DJI 只声明速度或杆量，对 PZ 只声明速度；`:2322` 仅给 ZHUOYI 距离能力，但 `:1349` 该旧品牌操作器为空。候选必须拒绝这些实际为 false 的距离能力；测试夹具里的 true 是显式构造的条件样本，不是平台已开放证明。
3. **角度不等于距离模式里的时长。** `DJHangarOperation.java:533` 的偏航分支按参数绝对值循环发送角速度，未证明其精确角度语义。因此 `turn_left/right` 暂不编译；连续速度、杆量、键盘模式也暂不支持。
4. **起飞高度基准未对齐。** `FlightRouteControlDTO.takeoffPointAlt` 直接传到 `TakeoffToPointDTO.target_height`；后者明确是 WGS84 绝对高度。现有工具 `takeoff.altitude_m` 的相对起飞语义不能直接透传。起飞暂不编译。DJI `DJHangarOperation.java:282` 明确拒绝当前点降落，故也不映射 `land`。
5. **控制权查询可能写业务。** GET `/uavControlPower/obtain` 会保存控制权，且任务创建者可能自动抢占（`UavControlPowerController.java:50`、`:183`）；不能当只读检查。申请/转让会发消息。候选不实现这些自动接管动作，权威适配器必须在执行前证明已持有控制权。
6. **速度/杆量需要持续心跳。** DJI 控制序列检查所有权、3 秒过期时间（`DJHangarOperation.java:298`、`:352`、`:485`），参考 UI 每秒发 `control/heartbeat/<hangarCode>`（`controlCenter.vue:1825`）。单次 HTTP 无法替代这个生命周期，本候选不创建 MQTT 心跳。
7. **飞点可能包含用户没要求的爬升。** 2026-10-16核对[DJI Dock 2官方指令飞行](https://developer.dji.com/doc/cloud-api-tutorial/cn/api-reference/dock-to-cloud/mqtt/dock/dock2/drc.html)及其[正文资源](https://developer.dji.com/doc/cloud-api-tutorial/assets/js/v-14902db9.9eb6fabc.js)，原文为“如果飞行器相对起飞点高度低于20m，会先上升到20m”。因此DJI距离动作在相对高度未知时返回 `RELATIVE_HEIGHT_UNAVAILABLE`，当前低于20米返回 `DJI_FLYTO_IMPLICIT_CLIMB`。目标低于相对20米另返回 `DJI_LOW_TARGET_UNVERIFIED`：这是项目对未核验行为的保守边界，**不是厂商文档明确禁止低目标高度**。编译出的目标椭球高同时须在官方2–10000米范围。PZ不套用DJI的20米规则；不会改成发送额外爬升动作。

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

`KtdSnapshot` 是必须由**服务端权威适配器**提供的内部类型。新增 `KtdSnapshotProvider` 通过固定 Java 快照 GET 严格解码该类型；在线部署尚未验证。不得把浏览器 pose、浏览器自报 actor、模型输出或 HTTP 2xx 构造成权威快照：

| 字段 | 所需来源和含义 |
| --- | --- |
| `actor_id` | 与服务端请求使用的 Java 登录身份一致；不能用一个共享登录身份冒充不同操作者 |
| `project_id/hangar_id/drone_id/site_id/task_id` | 当前账号有权访问且绑定一致的 Java 资源；真实任务和设备关联也必须已核对 |
| `context_version` | 与 `RuntimeBinding` 一致的会话绑定版本 |
| `version` | 新Java接口使用真实输入内容的64字符十六进制SHA256；兼容旧注入接口的非负整数。权限、状态、能力变化时更新，不按轮询次数编造版本 |
| `observed_at` | UTC Unix 秒；默认 5 秒以内，未来时间、未知或过期拒绝 |
| `control_owner` | Java 当前控制权归属，写请求必须等于 actor |
| `control_lease_expires_at` | 权威 Java 提供的控制权租约到期时间，UTC Unix 秒；所有写动作要求有限数值且严格大于当前时刻；缺失默认 `None` 仅兼容构造，绝不授权写入 |
| `permissions` | 当前账号、项目、设备、任务范围批准的动作名 `frozenset`；逐动作授权 |
| `capabilities` | 当前机型、链路、设备状态实际允许的动作映射；必须是布尔 `True`；距离另需 `UAV_MOVE_DISTANCE=True` |
| `profile` | 实际 Java 操作器路径 `DJI_HANGAR` 或 `PZ_HANGAR`；不能只按展示品牌名称猜测 |
| `state` | 真实在线、在空中状态、经纬度、yaw、相关高度；没有字段即未知，不能填默认安全值 |

移动需要 `online/airborne=True`、有限经纬度和 `yaw_deg`，DJI要求 `altitude_m` 及经上述门禁校验的 `height_m`，PZ要求 `height_m`。航线写操作要求明确在空中。已有 `ACTION_SPECS` 与请求验证先检查工具参数，再叠加平台范围限制。未映射动作返回 `ACTION_UNSUPPORTED`。

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

### Java 只读快照契约的来源

下列是首轮审计提出的契约要求。续轮已在独立Java候选中实现 GET `/lingxi/agent/platform/snapshot` 的只读部分，由 `projectId` 请求头和 `siteId/hangarId/droneId/taskId` 查询参数限定资源；当前共享环境尚未部署此路径。操作者从当前登录令牌派生，不能从查询参数提供。服务端校验项目权限、站点/机库/无人机关联、设备领用及真实任务设备关联后返回不可变投影；不会自动 obtain、发心跳、抢航权、启动视频或保存业务状态。缺少证据的控制条件仍返回拒绝原因。

最小返回内容应包含：

1. `schema_version`、`snapshot_version`、`generated_at`、`expires_at`，以及明确绑定的 `actor_employee_id/project_id/site_id/hangar_id/drone_id/task_id`。生成时间只用于追踪，不能替代以下观测时间。版本须来自可追溯的服务端状态版本；不能固定为 1 或随 HTTP 读取次数伪造。
2. `control_owner_employee_id`、`control_owner_updated_at`、`control_lease_expires_at`、`control_owner_version`。租约过期或冲突必须显式标记；快照 GET 不续租。
3. `telemetry.sampled_at/received_at/source/sequence`、`online/uav_state/airborne/longitude/latitude/yaw_deg`、`altitude_m`（含明确垂直基准）和 `height_m`（含相对哪个起点/地面）。缺失、失联、设备时间不可信、样本超期分别返回原因；不可用当前 HTTP 时间代替。
4. 经真实控制链路解析的 `operation_profile`、当前厂商航权归属、会话/链路健康及其观测时间。服务端明确决定某动作是否依赖心跳，不把“Java 分支没有检查”当作已满足条件。
5. 基于账号授权、项目资源、机型实现、当前链路/模式/状态形成的逐动作 `allowed_actions` 和 `denied_reasons`，并保留 `UAV_MOVE_DISTANCE` 真实值。不能只返回前端按钮配置；不支持、不确定和临时不可用必须区分。

Python提供器在构造 `KtdSnapshot` 前验证租约、采样/接收时间、资源、权限与逐动作可用决定的一致性。`observed_at` 取 `sampled_at/received_at` 较早者，不能取快照获取时间；`task_batch_id` 仍仅属于Agent会话，不冒充Java taskId。未来执行端还需支持 `expected_snapshot_version` 的原子校验，避免“读取时获准、发送前已变更”的窗口；现有控制HTTP没有这个门禁。当前所有真实控制开关继续默认关闭。

## 本地源码续轮与实际接入方式

后续消息来源切片在Java原接收路径保留独立连接代次、manager/client ID、callback Unix毫秒收时及retained/duplicate/实际QoS，并将原MessageData贯通到命令回执旁路。旧无来源入口保持旧业务处理，但不能生成跟踪回执；retained拒绝，duplicate仍由原eventHash幂等。旁路在资源锁后及事务 `beforeCommit` 复核当前连接，失效时回滚该回执写入；遇到已有业务外层事务则略过新旁路，保证旧业务独立。此元数据仅在Java进程内部，不新增浏览器/Agent响应字段，不改变旧日历时间字段，也不改变原帧遥测的既有时间契约。

上述来源判断尚未完成实际发布连接→已鉴权员工→DRC会话→厂商控制源的持久绑定，提交前检查也不能证明提交后网络持续在线。不能将“当前MQTT连接”替代厂商航权或动作许可；Java真实控制否决和默认开关继续保持关闭。具体实现、TDD阶段、源码哈希与门禁结果统一记录在自然语言飞控交付文档的最新消息来源切片。

用户确认以本地源码继续后，核对到主项目 `/Users/wangxiaolin/IdeaProjects/ktd_backend`，其缓存 `origin/prod=e8e3157`。b3副本保存了更新12个提交的正式线缓存 `98a8da4bf13f4ba623fb816da1a5cb9b09f8453a`（2026-10-09）。新Java工作树为 `/Users/wangxiaolin/PycharmProjects/ktd_backend-flight-agent-snapshot`，分支 `feature/flight-agent-platform-snapshot` 从该缓存正式线创建，未搬入test历史。该版本明确为本地缓存，不声称远端最新。原Java主目录、b3及旧前端脏工作区保持原样。

Java响应为 `R.ok(data)`，`schema_version=lingxi-platform-snapshot/v1`。DTO只接受资源标识；服务从登录态获取员工身份。资源校验通过后才读取控制权和遥测。快照/遥测/控制权版本均由可追溯字段计算SHA256；`generated_at` 不参与快照版本。owner必须唯一且匹配当前设备；有效租约为数据库 `updateTime+300秒`，读取不续租。遥测接收时间来自 `putWRJ` 维护的 `LAST_PUSH_TIME`，不使用可能由业务字段更新刷新的缓存时间。`UdpDatas.peekUavSnapshot` 只复制标量，不调用具有清理副作用的旧 `getUAV`。

采样时间只在已核对的DJI/PZ配置路径上使用 `positionTime`；缺失保持null，未知profile拒绝采样来源。没有使用 `timeStamp` 回填或按量级猜单位，因为旧源码中部分机型会把该字段设为服务端当前时间。当前缓存仍会合并多次增量字段，缺少每个字段的原帧证明，不能将缓存投影称为完整可信设备帧。

响应包含59项 `action_availability` 及稳定拒绝原因。`permissions` 表示所有门禁通过后的动作集合；Python要求它与 `available=true` 集合严格一致，且相应capability必须为布尔true。未知动作、重复JSON字段、错类型、绑定不符、未来/缺失/过期时间、矛盾许可、重定向和错误信封均不能升级成授权。Java序列化合成样本及来源哈希保存在 `fixtures/ktd-platform-snapshot-v1.json` 和相邻的 `.provenance.json`，Python提供器已经实际解码这份Java输出。

**当前Java候选仍仅能开放新鲜的 `read_state`。** 厂商航权、控制链路时效、逐字段时效未获证实，所有写动作继续明确拒绝，距离能力仍为false。启动时传入控制开关不会覆盖这些拒绝；单测中的距离许可true只是合成条件，不代表Java已经开放真实飞行。

`tools/platform_cockpit_server.py` 是独立的平台入口，只监听 `127.0.0.1`，默认只读。旧 `tools/cockpit_server.py` 继续是模拟器。以下配置全部由服务端进程环境注入，不写入浏览器、Git或本报告：

| 配置 | 用途 |
| --- | --- |
| `KTD_BASE_URL` | 固定Java origin，可选 `/api`；默认必须HTTPS |
| `KTD_SERVER_TOKEN` | 当前服务端登录凭据，禁止放进 `VITE_` 变量或命令行参数 |
| `KTD_ACTOR_EMPLOYEE_ID` | 必须与Java返回的当前员工身份完全一致 |
| `KTD_PROJECT_ID / KTD_SITE_ID / KTD_HANGAR_ID / KTD_DRONE_ID` | 服务端固定资源范围 |
| `KTD_TASK_ID` | 可选真实Java任务标识，不能使用Agent批次代替 |
| `FLIGHT_AGENT_TASK_BATCH_ID / FLIGHT_AGENT_CONTEXT_VERSION` | Agent运行批次及非负上下文版本；版本默认1 |
| `FLIGHT_AGENT_GATEWAY_TOKEN` | 可信网关凭据，仅在服务端使用 |
| `FLIGHT_AGENT_JOURNAL` | 私有持久SQLite日志绝对路径；父目录需已存在、归当前用户且权限0700 |
| `KTD_ALLOW_INSECURE_HTTP` | 仅显式 `true` 才允许受信内网HTTP；默认false |

平台浏览器请求必须经过可信网关。Vite开发代理现支持在Node进程中同时注入 `FLIGHT_AGENT_GATEWAY_TOKEN` 与 `FLIGHT_AGENT_ACTOR_ID`，员工ID须与后端配置一致；它先删除浏览器伪造的同名头再设置服务器值。此逻辑不进入客户端bundle，不是生产网关部署。

```bash
# 在上述服务端配置已经安全注入后，默认仍只读；本轮没有启动此真实平台入口
PYTHONPATH=src python3 tools/platform_cockpit_server.py --port 8794
# 以下验证只连接脚本自己的两个回环服务，不需要平台配置或凭据
PYTHONPATH=src python3 tools/bench/platform_cockpit_smoke.py
```

HTTP回环已验证：3次Agent HTTP、5次快照GET、1次脚本模型决定、0次确认/控制POST/外部平台/设备请求。Java样本的拒绝贯通到最终Agent回执。另有纯HTTP边界注入用例证明，在显式合成授权且手动确认的条件下，动作仍调用原Java控制路径并只返回 `submitted`，不伪造完成状态。

### 原始报文投影的兼容扩展

同一v1响应允许 `telemetry.source=java-raw-frame`，并携带严格的 `frame` 对象：`schema_version=lingxi-telemetry-frame/v1`、与资源绑定一致的 `profile`、处理器收到的报文文本重新按UTF-8编码所得的 `payload_sha256`、布尔 `complete`。既有 `MessageUtils` 使用默认字符集解码，因此该哈希不等价于已证明的原始MQTT字节哈希。`sequence` 仍为null；不把随机生成的tid/bid当作序号，不返回原报文、topic或设备SN。旧 `java-udp-cache` 形状及样本保留，不能添加frame字段伪装成原帧。

`complete=true` 只说明当前帧来源和资源匹配，并且当前帧的经纬度、yaw、已知飞行状态及机型所需高度均有效：DJI使用绝对高度，PZ使用相对高度。Java还要校验时效和显式离线状态；Python独立检查上述声明与state一致。任一原帧字段缺失保持未知，不借用旧缓存；部分帧仍可供只读展示，但声称同时具有写许可会被拒绝。缺少厂商航权和控制链路证据仍然拒绝所有控制，`UAV_MOVE_DISTANCE`仍为false。

原帧timestamp严格沿用已核对协议单位（DJI毫秒，PZ秒），不猜单位或补当前时间。这里记录的是协议上报时间与服务端处理入口接收时间，未证明设备时钟已校准或该时间就是传感器采样时刻。Python继续取两者较早者为 `observed_at`，并要求5秒时效。浏览器显示这个快照时间和距今时长，不将页面已加载或请求成功当作新鲜设备状态。

实际Repository对DJI/PZ无原帧返回 `TELEMETRY_FRAME_UNAVAILABLE`，包括冷启动和容量淘汰；有帧但来源冲突返回 `TELEMETRY_SOURCE_MISMATCH`，不回填旧合并缓存。缓存最多4096设备，处理重放、乱序、同时间内容冲突和显式离线；有界的淘汰/离线时间边界可能要求新入缓存设备等待更晚的一帧。旧Source默认接口和 `java-udp-cache` 合成样本继续用于兼容验证，不表示新的真实Repository会在DJI/PZ缺帧时降级读取旧对象。

原帧跨语言夹具为 `fixtures/ktd-platform-raw-snapshot-v1.json`，来源提交 `8cc7f1452d52c18d659777833053eb1c7cb58eb7` 及源码哈希记录在相邻provenance文件。追加验证为Python完整466项、快照25项、执行器33项；Java 8模块编译及38项测试通过。没有重跑真实模型评测，也没有使用真实设备输入。

官方还明确区分服务受理与飞点进度：`fly_to_point_progress` 通过网关events上报，包含 `fly_to_id` 与 `wayline_progress/wayline_ok/wayline_failed/wayline_cancel`。`result=0` 或剩余距离为0本身不等于完成。原帧切片尚未提供命令持久关联；下述回执切片补充了候选实现。真实机场/无人机型号与固件、控制源到业务操作者的映射、失权/重连时序和高度基准仍需确定，不能据一次夺权回复生成长期授权。

## 指令回执与显式续接 v1

平台装配使用 `track_commands=True`，但控制总开关仍默认关闭；本切片只允许 DJI 六方向距离动作。未跟踪的直接执行器保留旧接口，不会被当作可查询回执的命令。Java `lingxi.agent.command-tracking.enabled` 默认false，新增表通过人工DDL迁移，不由应用建表。即使打开这两个开关，当前快照的厂商航权/链路与距离能力门禁仍拒绝真实动作。

调用链为用户输入 → 既有 `BoundedAgentLoop` 提议 → 原确认卡与服务端策略检查 → Agent SQLite单次提交屏障 → 既有Java控制POST的tracking分支 → Java持久化实际发送标识 → 既有MQTT发布与回包入口 → 用户手动GET查询。没有第二套Agent运行时，也没有自动轮询、续发、补发或设备重试。

### Java请求和结果

仍使用 `/screen/uav/operation/control`，在原 `terminalId/siteId/taskId/type/mode/param` 之外附加 `tracking`：

```json
{
  "schemaVersion": "lingxi-command-request/v1",
  "commandId": "原确认卡command_id",
  "idempotencyKey": "原幂等键",
  "traceId": "原确认trace_id",
  "snapshotVersion": "已核验快照的64位十六进制版本",
  "expiresAt": 1700000005,
  "hangarId": "服务端绑定机库"
}
```

操作者取Java登录态，项目取已绑定请求头；模型和浏览器不设置执行身份。`expiresAt` 是确认卡与快照时效中较短的授权窗，不是设备回执期限。Java先准备实际 `bid/tid/fly_to_id` 和报文，再提交命令日志，最后发布同一报文。调用异常、崩溃、重启或未知结果均不会由本适配器自动重新发布。底层沿用MQTT QoS1，协议重投与设备端去重尚未真机验证，不能从一次应用提交推断设备恰好执行一次。未解决命令、过期未解决命令和冲突会继续占用资源范围，不能通过换幂等键消除不确定性。

只读查询为 `/lingxi/agent/commands/receipt?commandId=…&siteId=…&hangarId=…&droneId=…`，有真实任务时再带 `taskId`；服务地址的 `/api` 前缀沿用既有配置。读取仍核验当前登录操作者、项目资源和任务绑定，不根据查询字符串授予权限。HTTP和 `R.code` 均成功才解码 `R.data`：

| 字段组 | v1内容 |
| --- | --- |
| 契约 | `schema_version=lingxi-command-receipt/v1` |
| 指令绑定 | `command_id/trace_id/idempotency_key/action/parameters` |
| 资源身份 | `actor_employee_id/project_id/site_id/hangar_id/drone_id/task_id` |
| 状态 | `status/version/submitted_at/updated_at/expires_at` |
| 设备证据 | 可空 `reported_at/received_at/error_code`、`dispatch_ids={bid,tid,fly_to_id}` |
| 效果 | 固定 `device_effect=not_verified` |

时间均为Unix秒。状态保留 `dispatching/submitted/accepted/in_progress/completed/failed/cancelled/expired/unknown/conflict`；设备回执期限为120秒。GET只投影到期，不修改Java数据库或版本。到期/未知不能推出未执行；失败/取消不能推出没有部分位移。设备报告完成也不等于独立测量已证明真实位移精度。

服务回复严格关联原 `bid/tid` 与网关；飞点进度严格关联 `data.fly_to_id` 与网关，不强迫独立事件tid等于请求tid。只接受原文本中的字段，不使用会补随机ID/当前时间的 `ServicesReply` 来解析回执。重复键、尾随JSON、非整数返回码和不合法时间被拒绝。`wayline_ok+result=0` 且上报/接收时间均在命令窗内才可表示完成；同一命令的矛盾终态保持冲突。

Python独立校验原发送的身份、资源、动作、参数、时限和实际发送ID指纹。POST与GET的报告都持久化；跨进程事务拒绝版本回退、同版本矛盾和不可变字段变化。终态不能被迟到的受理/进行中覆盖。发送线程晚返回时不会抹掉先到的完成报告。内部报告及关联指纹不暴露给浏览器或模型。

### Agent公开查询与继续

`GET /api/flight-control/agent/receipt?command_id=…&context_version=…` 使用原会话与受信网关头。成功对象严格13字段：`success/schema_version/execution_mode/command_id/context_version/reported_status/report_source/receipt_version/reported_at/received_at/device_effect/continuation_allowed/trace_id`。固定值分别为 `true`、`flight-command-receipt/v1`、`platform`、`ktd-command-journal` 和 `not_verified`。成功查询不混入普通动作的 `status/dispatched/state`；失败沿用现有错误对象。

每次查询开始即暂停继续权限；失败保留旧报告但不能继续。只有原会话、原epoch、原待查命令、最新查询、有效 `completed` 及重新读取的可用状态同时成立，才恢复原意图。换任务、换资源、重置、新意图、进程重启或其他会话都不能恢复这份意图。重建执行器可读取本地持久化发送记录所绑定的回执，但读取不生成继续权限。

用户点击“继续下一步”仍进入原模型提议与新确认卡；每一步都需要用户确认。提交结果和设备报告分别保留，确认超时可查询原命令，不通过再次确认来猜测结果。

### Java发送连接关联的兼容收紧

Java本地候选 `5f4ce20710606fde7c2730ee6c6a5b0de27cc743` 在原命令行中新增内部发送manager/client/连接UUID代次。真实平台引擎先捕获连接，再将原prepared ID、payload及绑定提交，最后只调用捕获的客户端一次；不走旧发布的重连/异常吞掉路径。HTTP请求和回执v1字段不变，绑定标识不进入浏览器或模型。缺连接返回 `503 COMMAND_CONNECTION_UNAVAILABLE`；事务提交前变化返回 `409 COMMAND_CONNECTION_CHANGED` 并回滚；提交期间或之后变化保留 `unknown / DISPATCH_OUTCOME_UNKNOWN`，不经新连接补发。外部调用可能已经发送，仍不声称设备恰好执行一次。

只有与该命令原发送三字段精确一致、且原入站来源与时效门禁全部通过的回执才可写入。历史行任一绑定缺失均不受理新的受信回执；查询和幂等重复保持原状态，不回填、不重发。旧无来源journal方法保留签名但返回拒绝，旧Consumer发送构造不能提供新tracked执行能力。状态更新不改变绑定。部署前必须人工审核并在原建表后执行增量 `20261016-add-flight-command-connection.sql`，历史行保持null；当前只在隔离H2验证，MySQL未验证，生产不自动迁移。

完整离线证据为[165项门禁与来源记录](evidence/cockpit-mqtt-connection-binding-20261016.json)，0失败/错误/跳过。传输关联不等于员工到DRC和厂商 `control_source` 的权威绑定；相关拒绝原因、能力false与开关默认关闭继续保持。

### 内部厂商请求事实链

Java本地 `0eb831bfefd3fcc40d2a9ab91a3d9102ccf71137` 追加内部 `VendorControlAttemptService`，只接受 `flight_authority_grab/drc_mode_enter/drc_mode_exit`。复用真实资源鉴权、原journal/scope锁、事务、prepared ID、捕获连接发布及原入站旁路，没有新的HTTP或Agent入口。独立VO采用 `schema_version=lingxi-vendor-control-result/v1`、`kind=vendor_service_result`，成功为 `request_succeeded`；`vendor_authority/drc_link=unknown`、`device_effect=not_verified`。不将其投影成movement完成或长期控制许可，旧回执v1不变。

enter凭据来自可选服务端 `DrcBrokerProvider`，无默认实现；缺失或到期拒绝。首次prepared固定凭据和独立设备client_id，仅内存到实际发布；序列化排除原始payload，journal保存脱敏且不可重放的信封。客户端ID不作为员工、Java订阅者或control_source身份证明。重试不重新取凭据、换连接或补发。

授权guard在获得资源锁后、beforeCommit、捕获客户端实际发布前重读账号/员工、资源、gateway/SN、控制型号和平台owner版本，并在读取前后检查授权与认证期限。正常owner心跳更新也可能使旧尝试失效；提交后失效保留unknown并占scope，无恢复API。最后guard与外部发布不原子，不能证明持续授权或恰好执行一次。合法结果必须匹配实际method、bid/tid、网关、原发送连接、当前入站来源和命令时间窗；非零int为失败，矛盾结果conflict。request_succeeded/failed释放vendor请求scope，unknown/expired/conflict不释放。

新增人工增量 `20261016-add-flight-command-vendor-request.sql`：nullable request_kind/request_method/request_result，fly_to_id改nullable。vendor不伪造fly_to_id，movement仍要求真实ID；历史kind/method空仅兼容movement，不回填。仅隔离H2验证，未对MySQL执行。11模块208/208及root核验见[独立证据](evidence/cockpit-vendor-control-20261016.json)，不代表公开取权链、真实Broker、持续航权、部署或设备效果已验收。距离能力false、两项控制拒绝原因及tracking默认关闭保持。

### 部署前剩余条件与回退

2026-10-16补充[官方请求schema核验](evidence/dji-control-request-schema-20261016.json)：夺权 `flight_authority_grab` 的Data为null，不能携带actor或浏览器控制源；进入DRC的 `mqtt_broker.client_id` 用于设备连接中转MQTT，不是Java订阅连接的身份。飞机 `control_source` 是只读变化推送，已核文档没有给出浏览器UUID如何输入及绑定员工的版本契约。关联夺权/进入回复仅证明该次请求结果，不能直接设置持续航权。现有 `UavControlPowerTask.clean` 与 `UavControlPowerProcessor` 确实按平台心跳维护owner、超过300秒清理；保留这个平台策略，同时与MQTT认证期限、DRC心跳及厂商航权分开。可先实现本地控制尝试的typed请求/结果溯源，不必等待真机样本；权威actor许可仍需独立映射及失效契约，不能据上述ID相似性放行。

本切片只形成离线候选。人工需先审核DDL、按正式线流程部署候选、验证同版本MySQL行为，并提供有来源的实际机型/固件、原始回包、设备时钟、业务操作者到厂商航权的绑定和链路状态，再设计隔离真机验收。当前不把测试中的已验证内部值对象当作生产HTTP授权已放行。

回退优先关闭Agent控制开关与Javatracking开关，保留日志和未解决命令；不自动删除审计表、不将未知命令标成功、不重发。未带tracking的旧Java调用保持原业务分支。新增本地版本可通过独立revert提交回退，不改写共享历史。

## 独立厂商请求的Agent确认契约（2026-10-17）

Java候选已有默认关闭的专用POST `/screen/uav/operation/control/vendor-request` 和GET `/lingxi/agent/commands/vendor-receipt`。Agent沿用原会话、五个确认字段、pending与SQLite journal，追加typed `VendorControlRequest`，没有新增Agent循环。旧59项动作、`control_acquire/release`、键鼠/摇杆模式和movement回执v1保持原含义。

服务端 `FLIGHT_AGENT_VENDOR_METHODS` 是JSON字符串数组，默认 `[]`，仅允许无重复的 `flight_authority_grab/drc_mode_enter/drc_mode_exit`。启动器另有 `--enable-vendor-requests`，默认关闭；只有两者同时配置才增加单独的 `request_vendor_control(method)` 工具行，domain为 `vendor_request`。这行仅说明配置允许提议，不能代替Java员工 `taskManage:Issued`、资源和owner鉴权；也不会开启 `--enable-platform-control`、改变原拒绝原因或距离能力。本文是离线候选契约，不是启用真实设备的授权。

确认卡新增 `kind=vendor_control_request`、`action=request_vendor_control`、`parameters={method:…}`，仍用原五字段确认与取消。用户必须单独明确厂商航权申请、进入DRC或退出DRC；模型替换成别的方法、能力询问、否定、历史或伪造审批材料不出卡。预览不提交控制POST；确认不再调用模型改参数。取消、新输入、重置、快照内容/版本变化及控制权变化使旧卡失效。

卡和POST授权期限保守取原卡TTL、原快照 `observed_at+5` 与平台owner租约最小值，不能按重新获取/生成快照时间续期。Java新POST要求 `now<expiresAt<=now+5`；Agent进一步收紧到原遥测时效。这一额外收紧来自Agent，不能声称Java vendor service本身已有独立的遥测过期门禁。`snapshotVersion`、原期限及完整typed请求持久绑定，不在重复确认或重启恢复时重造期限。

POST为平面 `schemaVersion=lingxi-vendor-control-request/v1`、`method/commandId/idempotencyKey/traceId/siteId/hangarId/droneId/snapshotVersion/expiresAt`，有真实task才附 `taskId`；不发送员工、broker、client、原始报文或 `tracking`。GET由原journal推导method和资源，不能由浏览器指定。HTTP使用固定路径、严格重复键JSON解析、无重定向和无自动重试。先持久unknown屏障，再最多一次POST；即使返回错误信封或解析失败，也不能推断“肯定未发”而重发。

Java vendor VO恰好25字段，独立schema为 `lingxi-vendor-control-result/v1`、kind为 `vendor_service_result`，发送标识只有bid/tid，没有fly_to_id。状态为 `dispatching/submitted/request_succeeded/failed/expired/unknown/conflict`。result_code是可空的有符号32位整数：0对应请求成功、非零对应失败（包括负值），错误码须与 `DEVICE_RESULT_<int>` 一致。实际WebConfig将外层R空值转为空串，但局部VO保留内部null；不能复用movement解码器或只手拼序列化样本。

浏览器只手动查询 `GET /api/flight-control/agent/vendor-receipt?command_id=…&context_version=…`，使用原网关/会话身份，只接受这两个单值参数与空正文。成功严格18字段：`success/schema_version/execution_mode/kind/command_id/context_version/method/reported_status/report_source/receipt_version/reported_at/received_at/result_code/vendor_authority/drc_link/device_effect/continuation_allowed/trace_id`。固定值为 `true`、`vendor-control-receipt/v1`、`platform`、`vendor_service_result`、`ktd-command-journal`、`unknown/unknown`、`not_verified` 和 `false`。不暴露actor、资源内部绑定、幂等键、发送标识、私有报告或凭据。

父确认回执仍是submitted/unknown/rejected/cancelled，不把厂商终态当movement accepted/completed；终态通过分型GET查询。成功只表示本method这一次请求结果，不能授予持续航权、DRC活动状态或飞行续接。厂商结果窗为首次提交加120秒，与5秒快照和owner租约分开；到期仅投影dispatching/submitted，unknown不自动变expired。旧版本不能覆盖新结果，同版本矛盾拒绝；重启可查询已提交记录，但不恢复飞行意图、不重新提交。关闭Agent写开关用于回退时保留journal与未知记录。
