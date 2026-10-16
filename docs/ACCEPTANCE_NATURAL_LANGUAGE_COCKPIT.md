# 自然语言飞行控制验收记录

状态：进行中，未完成真实设备控制或完整线上界面验收。需求来源为本会话用户目标；实现边界与全部历史证据见 [交付说明](natural-language-cockpit.md)。继续任务不需要重建AgentLoop、会话或工具注册体系。

| 范围 | 当前代码或证据 | 验收边界 |
| --- | --- | --- |
| Agent意图、澄清与确认 | `feature/natural-language-cockpit@64b9a1c32d0de3122b98c7d0c072be0468df57b4`；离线564/564，已推送核验 | 明确运动顺序、连续补参方向、确认/回执推进已验证；非完整语言理解或真实飞行准确率 |
| 指定前端 | `lingxi-flight-agent-ui@e8096317ed885ce255c77667f238e6743d0ce8c7`；117项、87个离线浏览器场景；已推送核验 | 专业全窗地图、简控双舞台/坐标条/控制权入口已验证；设备完整内容/轮播、天气、三列仪表及完整线上像素/业务仍未验收 |
| 当前模型候选回放 | Agent `98178f8`；原100条合成输入：83符合、7不匹配、10超时 | 仅建议层；104请求，0确认/写工具；不验证服务策略或真实飞行准确率 |
| Java候选 | `feature/flight-agent-platform-snapshot@0eb831bfefd3fcc40d2a9ab91a3d9102ccf71137`，仅本地提交 | 2026-10-16 22:05:37，11模块208/208离线；内部厂商请求事实链，非公开取权、持续航权、MySQL、Broker或设备验收 |
| Java正式线 | 缓存 `prod=98a8da4bf13f4ba623fb816da1a5cb9b09f8453a` | GitLab下载/推送已拒绝，未重试；不称最新远端prod |
| 原Java工作树 | `/Users/wangxiaolin/IdeaProjects/ktd_backend`，`feature/geo-video-sync-recompute@858f6deec67d93734242a52845215a4d78736d6d` | 本轮重新核对干净且未改 |

Agent新增17个测试方法，完整唯一门禁 `bash tools/ci/offline_tests.sh` 564/564，socket守护通过；运动28/28、回执17/17，独立23组合成行为全部通过。对完全明确的运动/转向序列核对下一未完成步骤；每步仍要确认，重复确认/查询不多推进，新方向替换旧锚点，LF/CRLF连续补参不丢方向。混合/未知表达沿用原模型路径，不是完整语言解析器。[顺序证据](evidence/cockpit-ordered-motion-20261016.json)保留真实红绿、源码哈希及边界，未重跑模型评测。

Java既有[连接绑定证据](evidence/cockpit-mqtt-connection-binding-20261016.json)继续保留；新[厂商请求事实链证据](evidence/cockpit-vendor-control-20261016.json)对应同一journal内三种typed请求、固定连接发布和严格来源/方法/ID/时间关联。新增43项、原165项回归；root复核20份源码、22份XML的哈希/计数/时间、4份fixture、schema与控制台哈希及提交字节。独立VO成功仅为request_succeeded，航权/DRC仍unknown，没有HTTP/Agent入口或默认凭据provider。owner心跳更新可能使旧尝试失效；提交后unknown继续占scope，无恢复API。迁移仍是人工候选，仅H2验证。

最新UI见[87场景记录](evidence/cockpit-simple-stage-20261016/run-B0snq3/evidence.json)、[来源](evidence/cockpit-simple-stage-20261016/provenance.json)与[root核验](evidence/cockpit-simple-stage-20261016.json)：1920下左810地图/右视频、95/48px坐标条及控制权位置，1280按原比例缩放且修复顶栏背景遮挡。保持同一canvas/会话/卡片，控制权按钮仍走唯一原确认链，布局不发API/启流或自动确认。117项与构建由root独立重跑，32张PNG、10份JSON含实际红灯已归档；161次夹具API、21次夹具确认，0越界/设备请求/浏览器错误。助手位置、模式标签/按钮文字为明确适配；完整设备内容/轮播、天气、仪表与线上像素仍待验收。

模型[原始报告](../artifacts/cockpit-intents-100-20261016-v1-prompt-v1.0-current-98178f8/report.json)与[来源/逐类分母](../artifacts/cockpit-intents-100-20261016-v1-prompt-v1.0-current-98178f8/provenance.json)对应20:50:04—21:07:57（台北）、既有局域网GPU `qwen3.5:9b`。p50 6.991秒、p95 30.009秒，已知350891输入/12314输出tokens，总usage及费用未知。覆盖57/59个预期动作；缺投放器和上传航线。危险写提议1项为把“十来米”猜成10米；航线样本仍有paused上下文与execute_route标签歧义；另5项为no_action/clarification差异。原100分母和旧报告均保留。该bench不经过CockpitService，准确率 `not_evaluable`，不能证明近期服务修复或A/B改善。

真实控制继续受 `VENDOR_AUTHORITY_UNAVAILABLE`、`CONTROL_LINK_FRESHNESS_UNAVAILABLE`、`UAV_MOVE_DISTANCE=false` 与tracking默认关闭约束。[补充协议证据](evidence/dji-control-request-schema-20261016.json)明确夺权请求无actor/source字段、进入DRC的client_id为设备连接ID，不能直接绑定Java订阅者或员工；平台owner的300秒确有原心跳清理策略依据，但不是厂商航权租约。尚需有来源的员工/DRC/`control_source`版本及失效契约、机型固件与原始回包生命周期、同版本MySQL兼容，以及普通驾驶舱完整线上对照。业务库迁移、共享部署和具体设备动作仍需覆盖该动作的明确授权；离线候选不提供这些授权。

本续轮模型请求、业务写入、真实MQTT、设备动作、共享部署或重启均0；此前模型104请求和官方静态2GET仍属于原轮证据。Agent/UI语义分支已推送核验，Java仅本地提交且不重试拒绝推送，不合并或建MR。回退用独立revert，保留nullable列、历史命令及原模型证据，不补造身份或重发。下一步可推进本地隔离MySQL兼容及固定源码UI余项；本机MySQL26.7.0不是已确认的生产版本，当前尚未启动DB或连接业务库。只有相关代码/输入变化才重跑受影响门禁，工程测试不能提升为业务/设备验收。
