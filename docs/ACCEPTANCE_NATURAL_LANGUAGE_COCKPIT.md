# 自然语言飞行控制验收记录

状态：进行中，未完成真实设备控制或完整线上界面验收。需求来源为本会话用户目标；实现边界与全部历史证据见 [交付说明](natural-language-cockpit.md)。继续任务不需要重建AgentLoop、会话或工具注册体系。

| 范围 | 当前代码或证据 | 验收边界 |
| --- | --- | --- |
| Agent意图、澄清与确认 | `feature/natural-language-cockpit`，产品实现含 `dff8be9`；此前离线547/547 | 本轮产品未改、未重测；非模型准确率或真实飞行 |
| 指定前端 | `lingxi-flight-agent-ui@be3a0b03b439803e9a55df058fcda8c9b330beba`；本轮117项、82个离线浏览器场景；已推送核验 | 专业全窗地图/监控小地图/顶栏及普通专业/简控底部布局已验证；完整线上像素和业务仍未验收 |
| 当前模型候选回放 | Agent `98178f8`；原100条合成输入：83符合、7不匹配、10超时 | 仅建议层；104请求，0确认/写工具；不验证服务策略或真实飞行准确率 |
| Java候选 | `feature/flight-agent-platform-snapshot@5f4ce20710606fde7c2730ee6c6a5b0de27cc743`，本地提交 | 2026-10-16 20:35:32，11模块165/165离线；非MySQL、Broker或设备验收 |
| Java正式线 | 缓存 `prod=98a8da4bf13f4ba623fb816da1a5cb9b09f8453a` | GitLab下载/推送已拒绝，未重试；不称最新远端prod |
| 原Java工作树 | `/Users/wangxiaolin/IdeaProjects/ktd_backend`，`feature/geo-video-sync-recompute@858f6deec67d93734242a52845215a4d78736d6d` | 本轮重新核对干净且未改 |

Java既有交付补齐“发送前持久绑定实际连接 → 固定客户端发布 → 同来源回执事务”。新增38项工程测试覆盖重连窗口、跨连接认领、旧空绑定、幂等、早到回执与迁移兼容；23个源码/测试/DDL哈希在最终运行前后及提交中一致。[结构化证据](evidence/cockpit-mqtt-connection-binding-20261016.json)记录完整命令、19个suite及来源。Java只本地提交，本轮产品未改、未重测。

最新UI结果见[原始82场景记录](evidence/cockpit-stage-layout-20261016/final/evidence.json)和[源码/截图来源](evidence/cockpit-stage-layout-20261016.provenance.json)：真实Vue红灯后修复专业地图及68/91px顶栏；切换监控保同一canvas/会话/卡片，布局操作不发API/启流或自动确认。117项与构建由root独立重跑，20张最终PNG、3份JSON及4份红灯文件已归档；152次夹具API、21次夹具确认，0越界/设备请求/浏览器错误。简控完整外围、设备内容/轮播、三列仪表仍待对齐。

模型[原始报告](../artifacts/cockpit-intents-100-20261016-v1-prompt-v1.0-current-98178f8/report.json)与[来源/逐类分母](../artifacts/cockpit-intents-100-20261016-v1-prompt-v1.0-current-98178f8/provenance.json)对应20:50:04—21:07:57（台北）、既有局域网GPU `qwen3.5:9b`。p50 6.991秒、p95 30.009秒，已知350891输入/12314输出tokens，总usage及费用未知。覆盖57/59个预期动作；缺投放器和上传航线。危险写提议1项为把“十来米”猜成10米；航线样本仍有paused上下文与execute_route标签歧义；另5项为no_action/clarification差异。原100分母和旧报告均保留。该bench不经过CockpitService，准确率 `not_evaluable`，不能证明近期服务修复或A/B改善。

真实控制继续受 `VENDOR_AUTHORITY_UNAVAILABLE`、`CONTROL_LINK_FRESHNESS_UNAVAILABLE`、`UAV_MOVE_DISTANCE=false` 与tracking默认关闭约束。[补充协议证据](evidence/dji-control-request-schema-20261016.json)明确夺权请求无actor/source字段、进入DRC的client_id为设备连接ID，不能直接绑定Java订阅者或员工；平台owner的300秒确有原心跳清理策略依据，但不是厂商航权租约。尚需有来源的员工/DRC/`control_source`版本及失效契约、机型固件与原始回包生命周期、同版本MySQL兼容，以及普通驾驶舱完整线上对照。业务库迁移、共享部署和具体设备动作仍需覆盖该动作的明确授权；离线候选不提供这些授权。

本轮有104次既有模型服务请求和2次官方静态GET；业务写入、真实MQTT、设备动作、共享部署或重启均0。Agent仅增加说明/证据，可按原语义分支提交推送，不合并、建MR或部署。回退用独立revert，保留新增nullable列、历史命令与原模型证据；不补造旧连接、不自动重发。后续先复用已归档证据处理缺项，只有代码或输入变化再运行受影响门禁；模型候选回放不能改称服务或真实飞行准确率。
