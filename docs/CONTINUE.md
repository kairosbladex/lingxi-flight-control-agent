# 继续工作

2026-10-20 最新请求：在独立 10086 前端准备真实自然语言试飞。[10086 交付记录](ACCEPTANCE_FLIGHT-10086.md)：已新增同源助手目录，388 文件哈希核验，694 项离线测试及 34/28 场景浏览器夹具通过。当前部署面板仍只支持模拟；未发设备指令，真实接入等待有效登录上下文、指定设备及动作范围，不把历史预览或本次静态部署算实飞完成。

当前目标以2026-10-17用户最新指令为准：默认右下角气泡，点击打开传统聊天窗口，复用自然语言理解、澄清、确认及原Java适配链。此软件切片已实现并完成离线交付；UI `ecc7798e2f3da81c007bfa7031c7f1cc61fd0507` 已推送核验。完整驾驶舱仅为 `?mode=cockpit` 兼容入口，设备详情/天气和全驾驶舱对齐停止扩展。不要因下文历史目标继续扩大UI。

2026-10-20新增 **Route A：不改 ktd_backend 的测试控制链路**（默认关闭，未连测试环境）。Agent 直连 ktd 现有 `/screen/uav/...` 旧控制接口，授权由现有只读接口拼装并标注 `legacy-read-api`；开放 DJI 机库的 6 向位移、拍照、开始/停止录像、7 个云台动作和左右转向。转向使用杆量短脉冲 + `/test/getUdpData` 航向回读闭环，默认 3° 容差、30 秒/80 脉冲上限，误差反向增大或航向不跟随会立即停止，结束时回中并删除运动记录；摇杆持续姿态以 `KtdPlatformExecutor.stick()` 脚本原语提供，不改变 59 项动作契约，不是模型工具。写请求需同时打开 `--enable-platform-control` 与 `--enable-legacy-control`，位移还需 `--allow-legacy-pose-unverified`（旧接口无姿态，目标点由 Java 服务端计算，Agent 不执行 20 米隐含爬升门禁）；转向/云台不需要该开关，但转向要求姿态调试接口可用。新增 `ktd_legacy.py`、`tests/test_ktd_legacy.py` 32 项，完整离线 **669/669**；无 ktd 代码改动、无 MQTT/db 直连、无真实设备请求。边界、配置与测试环境前置见[Route A 说明](legacy-ktd-control.md)。

最新[气泡聊天证据](evidence/chat-bubble-20261017.json)：root独立复跑后端619/619、前端140/140及构建、1个HTTP综合回归、27气泡/22实际Agent-Java适配链/115兼容场景。新 `FlightChatWidget.vue` 首开加载，隐藏不重置、不重发；局部样式可独立嵌入，IME/Escape、移动短视口和长会话隐藏期间返回后的最新消息均验证。最终自有假Java链为12 Agent HTTP/6脚本模型/22快照GET/1回执GET/1控制POST，确认前及取消后0POST，手动回执不重发。fixture仅固定合成配置/自有端口，最新源hash `21bc3a88…`；原Javaconverter SHA `19f13c59…` 未变。所有最终报告与10张PNG随UI提交保存，115旧入口JSON另保存，其大图仍在原临时run目录。无新真实模型评测、共享部署、业务写入或设备动作，真实控制默认关闭。

2026-10-19新增**可选的整份计划执行模式**（仅模拟器，默认关闭）：`tools/cockpit_server.py --enable-plan-execution` 时，模型把复合指令拆成最多5步，前端一张计划卡、用户一次确认，系统按顺序逐步骤 `preview → confirm`；任一步失败/异常/控制权或状态版本变化即停止，不自动重试。新增 `src/lingxi_flight_control/plan.py` 与 `tests/test_cockpit_plan_execution.py`，离线 **632/632**。真实模型 `qwen3.5:9b` 探测：“往前飞10米，并帮我拍张照”生成2步计划卡，一次确认后模拟器执行前进10米+拍照（`photo_count=1`、位置变化、revision=2），0设备/平台请求；计划阶段3次模型HTTP约43.2秒，确认阶段0次模型调用约3毫秒，见[探测证据](evidence/flight-plan-mode-20261019.json)。平台模式构造时拒绝启用；Java 拍照/录像尚无完成回执，真实顺序执行仍需 Java 侧补齐。工具面与话术见[飞控工具与话术清单](flight-control-tool-catalog.md)。

2026-10-19前端计划卡已独立适配并推送 UI **`3249401`**：`flight_plan` 卡片/回执类型、严格步骤校验、计划步骤展示、逐步骤结果与停止原因；前端 **144/144**、构建通过。Java 侧按用户要求不改 ktd_backend，改为在 `zhuoyi-ai-platfrom` 复刻独立飞控模块；三层契约、可复刻/不可直接复制部分、J1–J4 阶段与授权证据链见[飞控功能独立适配](agent-flight-control-isolation.md)。J1 已在 `zhuoyi-ai-platfrom` 分支 `feature/agent-flight-control`（从 `origin/prod@e40d672` 创建）完成并推送 **`e4e37e8`**：命令/回执契约、动作白名单、单向状态机、幂等内存日志与离线 transport；`./mvnw -B verify` **20/20**，隔离实例 18091 启用返回 `submitted`/真实 bid/tid/`not_verified`，18092 默认禁用返回 `503 FLIGHT_CONTROL_DISABLED`，两实例已停止。

2026-10-19 J2 完成本地提交 **`b7dcd14`**（未推送，待操作级授权）：新增严格只读 `HttpKtdSnapshotClient`，绑定操作者、项目/站点/机库/无人机/任务、快照版本、5秒内遥测、在线/空中、完整位姿、`permissions`/`capabilities`/`action_availability`、控制 owner 与租约，命令授权窗不得超过快照有效期或租约；新增只保护 `/api/v1/flight-control` 的可信网关头（Bearer 凭据通过后才信任项目/操作者头）。`./mvnw -B verify` **33/33**；隔离实例用本地假快照 18093 + Java 18091 验证合法请求 `submitted`、错误网关 `401`、快照版本不匹配 `403 SNAPSHOT_BINDING_MISMATCH`，实例已停止。仍无 MQTT、设备、Postgres；J3/J4 未做，真实写入仍需单独授权。

2026-10-19 J3 完成本地提交 **`d8454d2`**（未推送）：新增 `DjiCommandEncoder` 与严格 `DjiReceiptParser`。Encoder 按现有 `DJUtil` 语义构造相机/录像/云台 `services` 报文和六向移动 `fly_to_point`，保留 20 米相对高度门禁与 2–10000 米目标高限制；Parser 覆盖 `services_reply` 与 `fly_to_point_progress`，成功/进行中携带非零 result 映射 `conflict`，拒绝重复键、尾随内容、时间越界和非法标识。冻结 fixture `src/test/resources/flightcontrol/dji-receipts-v1.json`（10 案例 + provenance）。`./mvnw -B verify` **41/41**；命令/回执往返按 `bid/tid` 与 `fly_to_id` 关联。仍不发布 MQTT、不控制设备；`gatewaySn`/`payloadIndex` 及厂商/负载航权等待留给 J4。

2026-10-19 J3 已推送远端 `d8454d2`。J4 完成本地提交 **`1abacb8`**（未推送）：新增 `DjiMessagePublisher`、`DjiCommandJournal` 端口及 fail-closed 默认实现；`DjiFlightControlTransport` 严格按 `encode → journal.prepared → publish → journal.published` 执行，发布/日志异常保留 `DISPATCH_OUTCOME_UNKNOWN` 且不自动重试，prepared 日志失败时不发布，回执按 `bid/tid/fly_to_id` 关联。`./mvnw -B verify` **49/49**；仅用 `RecordingDjiMessagePublisher`，**未连接任何真实 Broker**，transport 尚未接入 `FlightControlService`/HTTP；`gatewaySn`/`payloadIndex`、厂商/负载航权、Postgres 日志和真实发布器仍需 J5 与单独授权。

2026-10-19 J4 已推送远端 `1abacb8`。J5a 完成本地提交 **`332bc5d`**（未推送）：`DjiFlightControlTransportAdapter` 接入 `FlightControlService`，新增 `DjiDeviceBindingProvider`（默认 fail-closed）与 `DispatchCommand` 的 snapshot/device 字段；pre-send/journal 失败落 `failed`，发布未知落 `unknown`。新增 `PostgresFlightControlStore`、手工 DDL `src/main/resources/db/flightcontrol/flight_control_command.sql`，配置 `zhuoyi.flight-control.store=postgres` + JDBC；H2 PostgreSQL 模式 4 项 store 测试，**真实 PostgreSQL 未实测**。`./mvnw -B verify` **57/57**；隔离实例 18091（内存 transport）`submitted`，18092（DJI transport 开启但无权威绑定）`503 DEVICE_BINDING_UNAVAILABLE`，实例已停止。仍无真实 Broker、权威 gateway/payload、Broker 回执入口和航权链；J5b 需单独授权。

2026-10-19 J5a 已推送远端 `332bc5d`。J5b-1 完成本地提交 **`02eea83`**（未推送）：`FlightControlSnapshot` 增加可选 `gatewaySn`/`payloadIndex`，`HttpKtdSnapshotClient` 解析快照顶层 `gateway_sn`/`payload_index`；新增 `SnapshotDjiDeviceBindingProvider`，任一字段缺失即 `503 DEVICE_BINDING_UNAVAILABLE`，默认 Spring bean 改为该 provider。`./mvnw -B verify` **60/60**；隔离实例验证：假快照带两字段 + DJI transport 时绑定通过、随后默认 journal `DJI_JOURNAL_UNAVAILABLE` 并落 `failed`；缺字段时在创建命令前拒绝。当前真实 ktd 快照尚未返回这两个字段，仍无真实 Broker/回执入口/航权链；J5b-2 需单独授权。

2026-10-19 **Agent 全流程软件闭环已打通**（fixture 后端）：Agent **`5ba8f3e`** + Java **`9ea6660`**（均为本地提交）。真实模型 `qwen3.5:9b` 将“往前飞10米，并帮我拍张照”生成 2 步平台计划，一次确认后 Agent 返回 `plan_status=completed`；Java fixture 回执第 1 步 `completed`、第 2 步 `accepted`，均 `device_effect=not_verified`。Agent 离线 **637/637**、Java **62/62**；证据[agent-e2e-zhuoyi-20261019.json](evidence/agent-e2e-zhuoyi-20261019.json)。Java 使用 fixture 快照/设备模拟器，未连 Broker、真实设备或真实 PostgreSQL；亚秒级回执不代表设备执行。J5b-2 真实链路仍需外部条件与单独授权。

2026-10-17最新Agent产品 **`e0ef1e744b13fd723b12962ea7414b060df2bcf2`** 已推送核验：原loop/session/card/pending/SQLite journal接通独立vendor请求确认链，默认关闭且方法allowlist空；原59动作、prompt v1.1与movement门禁不变。最终唯一离线 **619/619**、socket守护及diff通过；29个新方法覆盖完整服务、默认关闭、错方法/键鼠串用、取消、时效、幂等并发/重启/未知不重发及原Java13案例。正式自有回环HTTP46次Agent/51次假Java、3次vendor POST、6次脚本模型决定、8份严格18字段报告，25份source前后hash稳定，真实平台/模型/设备0。见[完整请求链证据](evidence/cockpit-vendor-request-20261017.json)及[正式smoke](../artifacts/vendor-control-smoke-20261017-final/report.json)。

Java最新本地 **`e623312c173fd698657a39fa22e4ad98c0630a80`** 仅追加真实HTTP fixture导出测试/证据，生产仍为 `af034e1`；11模块232/232、10份源码/测试及23份XML由root逐项核对，未重复跑Maven、未重试推送或部署。原样13案例/2快照/56份HTTP正文已复制 [fixture](../fixtures/ktd-vendor-http-v1.json) 和相邻provenance（SHA `c73c7be7…`）。独立POST/分型GET复用原service/journal与鉴权，Agent现已接入确认；所有生产控制默认开关和持续航权/链路未知门禁继续保持。

UI最新 **`21b9239a0e81e194e771bcacfa408f624d87efdd`** 已推送核验、工作区干净：140项/构建及115离线浏览器场景（旧102+新13vendor），224夹具API/31确认、0禁止/设备/浏览器错误。root独立重跑单测/构建、核验最终原始JSON/67份source-run-build hash/11份提交字节和三张vendor图，见[UI复核](evidence/cockpit-vendor-ui-20261017.json)。厂商结果手动独立查询，18字段/七状态/signed32，authority/link未知且无continue；取消和明确零发送拒绝清查询引用。Git保留最终原始JSON、3关键PNG及红绿/来源/恢复索引；193份重复完整run原图可从 `/tmp/lingxi-vendor-ui-20261017.6TxuXx` 按UI provenance逐项恢复，未删除或改写。原模型报告、固定旧UI及原Java工作树保持。

employee/DRC/control_source的来源、版本及失效契约仍是后续真实控制前提，本轮不为聊天入口扩大此项实现；设备详情/天气不再推进。当前5秒确认窗只针对一次厂商请求，成功不能解锁movement或恢复飞行意图；持续航权、默认Broker provider、真实固件、MySQL及设备效果仍缺证据。前端typed卡与结果使用原确认链及独立手动查询，不新增控制按钮。下文是历史快照，不能将其中“无HTTP/Agent入口”的旧结论或全驾驶舱后续项当作本轮范围。

原服务bench `b7f428c` 的102条/59动作、修复前90匹配/6不匹配/6超时，以及prompt v1.1相同6条定向6/6、其余96条missing全部原样保留；本轮没有重新请求真实模型或改变provider。它们与619工程门禁分开，不推断新模型准确率，见[门禁定向证据](evidence/cockpit-request-authority-20261017.json)和[原全量报告](evidence/cockpit-service-intents-20261016.json)。

从 [当前验收记录](ACCEPTANCE_NATURAL_LANGUAGE_COCKPIT.md) 继续，契约见 [Java接入说明](ktd-platform-contract.md)，历史实现与UI来源见 [交付说明](natural-language-cockpit.md)。Java本地 `0eb831bfefd3fcc40d2a9ab91a3d9102ccf71137` 对应[厂商请求事实链证据](evidence/cockpit-vendor-control-20261016.json)、11模块208/208离线门禁；20源码/22XML/4fixture及schema/console哈希与提交字节已由root核对。不重复获取/推送已被拒绝的Java仓库，不改原Java工作树。

Agent产品已推送 `2284ee6a1f00c6385c2c860f420caacbe901ebc9`：顺序/连续补参后又收紧数量范围、上下界、备选/约数及英文单位边界，完整573/573。此前定向28/17、独立23组见[顺序红绿](evidence/cockpit-ordered-motion-20261016.json)；新9方法/45子例与独立20组22轮见[数量证据](evidence/cockpit-quantity-bounds-20261016.json)。复用原会话保存明确运动进度，每步仍确认，重复确认/查询不多推进；新不完整反向意图替换旧锚点。范围不能让模型选择端点，精确位移附速度上限仍可提议；未知混合语义仍走模型，不能宣称完整解析或模型准确率提升。

最新UI为已推送核验的 `7f14780eb2fca94dca4fa664d26edd246c11d780`：专业全窗地图、简控双舞台/坐标条/唯一控制权入口及三联仪表，120项、构建及96场景通过，[JSON/PNG与root来源](evidence/cockpit-simple-instruments-20261016.json)已归档；175夹具API/21夹具确认，0禁止/设备/浏览器错误。三组真实红灯含结构缺失、未知文字溢出和±180重叠；九个产品/依赖/素材清单与提交哈希一致。ASL基准未知、原始−45°显示、未知速度字号、端点文字避让属明确适配，不能冒充全页像素一致。剩余设备完整内容、天气与实际媒体视角信息可按固定 `35574cba` 继续核对；缺权威字段保持unknown，不将sources地址候选误当相机轮播。原参考工作区有冲突，仅用git show，不导入旧业务取权或自动启流。

当前100条[模型候选报告](../artifacts/cockpit-intents-100-20261016-v1-prompt-v1.0-current-98178f8/report.json)及[来源](../artifacts/cockpit-intents-100-20261016-v1-prompt-v1.0-current-98178f8/provenance.json)为83符合/7不匹配/10超时、104请求。仅建议层，不经过CockpitService或确认/执行；危险写提议1、预期动作57/59覆盖、route_06状态歧义仍保留。后续v2可补缺失动作/明确状态，不能重写v1或把这轮当近期服务修复的效果证明。

厂商[请求schema核验](evidence/dji-control-request-schema-20261016.json)支持的本地typed账本/绑定发布/严格结果链已实现；没有公开HTTP/Agent入口、默认broker凭据provider、持续心跳许可或control_source映射。夺权Data为null，DRC client_id是设备连接ID；平台owner300秒是原心跳清理策略，不是厂商租约。请求成功仅request_succeeded，持续航权/链路仍unknown。下一步公开取权接入必须保留这些边界，并先设计来源/版本/失效与恢复契约；不能把一次成功或ID相似性升级为动作许可。

可先独立验证真实MySQL的DDL、二进制标识、scope锁/事务及迁移兼容。本机有MySQL26.7.0，Docker daemon不可用；尚未启动数据库/连接业务库，生产版本未知。只允许新建临时datadir/socket、回环端口与合成测试库，不启动Java业务Application/DataInitRunner；不把本机版本验证称同版本生产验收。保持H2离线门禁唯一真源，不将可选MySQL集成加入默认离线discover。

剩余验收包括权威航权/DRC控制源、真实固件回包、MySQL兼容与完整普通驾驶舱线上对照。真实执行和共享部署开关继续关闭；不要将传输连接关联、模拟器、模型候选或单元测试提升为真机效果。后续复用现有循环、会话和确认路径；业务迁移、共享部署与具体设备验收须单独覆盖授权。
