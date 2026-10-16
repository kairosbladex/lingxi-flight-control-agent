# 自然语言飞行控制验收记录

状态：进行中，未完成真实设备控制或完整线上界面验收。需求来源为本会话用户目标；实现边界与全部历史证据见 [交付说明](natural-language-cockpit.md)。继续任务不需要重建AgentLoop、会话或工具注册体系。

| 范围 | 当前代码或证据 | 验收边界 |
| --- | --- | --- |
| Agent意图、澄清与确认 | `feature/natural-language-cockpit`，产品实现含 `dff8be9`；此前离线547/547 | 本轮产品未改、未重测；非模型准确率或真实飞行 |
| 指定前端 | `lingxi-flight-agent-ui@ff2b91b6d54524044c5ce97f6af3641b52dfd1c0`；此前116项、77个离线浏览器场景 | 普通专业/简控底部布局已验证；完整线上像素和业务仍未验收 |
| Java候选 | `feature/flight-agent-platform-snapshot@5f4ce20710606fde7c2730ee6c6a5b0de27cc743`，本地提交 | 2026-10-16 20:35:32，11模块165/165离线；非MySQL、Broker或设备验收 |
| Java正式线 | 缓存 `prod=98a8da4bf13f4ba623fb816da1a5cb9b09f8453a` | GitLab下载/推送已拒绝，未重试；不称最新远端prod |
| 原Java工作树 | `/Users/wangxiaolin/IdeaProjects/ktd_backend`，`feature/geo-video-sync-recompute@858f6deec67d93734242a52845215a4d78736d6d` | 本轮重新核对干净且未改 |

最新交付补齐“发送前持久绑定实际连接 → 固定客户端发布 → 同来源回执事务”。新增38项工程测试覆盖重连窗口、跨连接认领、旧空绑定、幂等、早到回执与迁移兼容；23个源码/测试/DDL哈希在最终运行前后及提交中一致。[结构化证据](evidence/cockpit-mqtt-connection-binding-20261016.json)记录完整命令、19个suite及来源。Java只本地提交；本Agent仓库仅增加说明和证据，可按原语义分支提交推送，不合并、建MR或部署。

真实控制继续受 `VENDOR_AUTHORITY_UNAVAILABLE`、`CONTROL_LINK_FRESHNESS_UNAVAILABLE`、`UAV_MOVE_DISTANCE=false` 与tracking默认关闭约束。尚需有来源的员工/DRC/`control_source`关系、机型固件与原始回包生命周期、同版本MySQL兼容，以及普通驾驶舱完整线上对照。业务库迁移、共享部署和具体设备动作仍需覆盖该动作的明确授权；离线候选不提供这些授权。

本轮无业务写入、真实MQTT、设备动作、在线模型调用、共享部署或重启。回退用独立revert，保留新增nullable列、历史命令与审计；不补造旧连接、不自动重发。后续先复用已归档证据处理上述缺项，只有代码或输入变化再运行受影响门禁；旧模型评测不能改称当前代码准确率。
