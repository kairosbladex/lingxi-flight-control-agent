# 继续工作

当前目标是自然语言理解、澄清、确认、指定前端与原Java控制接口的完整流程；整体仍在进行中。

从 [当前验收记录](ACCEPTANCE_NATURAL_LANGUAGE_COCKPIT.md) 继续，契约见 [Java接入说明](ktd-platform-contract.md)，历史实现与UI来源见 [交付说明](natural-language-cockpit.md)。Java本地 `5f4ce20710606fde7c2730ee6c6a5b0de27cc743` 对应[发送连接绑定证据](evidence/cockpit-mqtt-connection-binding-20261016.json)、165/165离线门禁；不重复获取/推送已被拒绝的Java仓库，不改原Java工作树。

最新UI为已推送核验的 `be3a0b03b439803e9a55df058fcda8c9b330beba`：专业全窗地图、监控小地图和68/91px居中顶栏，117项测试、构建及82场景通过，[最终JSON/PNG与来源](evidence/cockpit-stage-layout-20261016.provenance.json)已持久归档。剩余简控左810地图/右视频外围、设备轮播/内容和三列仪表可继续按固定正式线 `35574cba` 做本地对齐；原参考工作区有冲突，仅用git show读固定提交。

当前100条[模型候选报告](../artifacts/cockpit-intents-100-20261016-v1-prompt-v1.0-current-98178f8/report.json)及[来源](../artifacts/cockpit-intents-100-20261016-v1-prompt-v1.0-current-98178f8/provenance.json)为83符合/7不匹配/10超时、104请求。仅建议层，不经过CockpitService或确认/执行；危险写提议1、预期动作57/59覆盖、route_06状态歧义仍保留。后续v2可补缺失动作/明确状态，不能重写v1或把这轮当近期服务修复的效果证明。

厂商[请求schema核验](evidence/dji-control-request-schema-20261016.json)支持先做本地typed控制尝试账本、先持久化再绑定发送、严格关联结果/心跳/控制源观察；无需等待真机样本才能写这些离线候选。夺权Data为null，DRC client_id是设备连接ID，已核文档未定义浏览器UUID到员工的权威映射；平台owner300秒有心跳清理源码依据，但不是厂商租约。完整actor航权仍需独立版本/失效契约，不能从一次请求成功或ID相似性放行。

剩余验收包括权威航权/DRC控制源、真实固件回包、MySQL兼容与完整普通驾驶舱线上对照。真实执行和共享部署开关继续关闭；不要将传输连接关联、模拟器、模型候选或单元测试提升为真机效果。后续复用现有循环、会话和确认路径；业务迁移、共享部署与具体设备验收须单独覆盖授权。
