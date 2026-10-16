# 继续工作

当前目标是自然语言理解、澄清、确认、指定前端与原Java控制接口的完整流程；整体仍在进行中。

从 [当前验收记录](ACCEPTANCE_NATURAL_LANGUAGE_COCKPIT.md) 继续，契约见 [Java接入说明](ktd-platform-contract.md)，历史实现与UI来源见 [交付说明](natural-language-cockpit.md)。Java本地 `0eb831bfefd3fcc40d2a9ab91a3d9102ccf71137` 对应[厂商请求事实链证据](evidence/cockpit-vendor-control-20261016.json)、11模块208/208离线门禁；20源码/22XML/4fixture及schema/console哈希与提交字节已由root核对。不重复获取/推送已被拒绝的Java仓库，不改原Java工作树。

Agent产品已推送 `64b9a1c32d0de3122b98c7d0c072be0468df57b4`：顺序/连续补参修复，完整564/564、定向28/17、独立23组合成行为通过，[红绿证据](evidence/cockpit-ordered-motion-20261016.json)已保留。复用原会话保存明确运动进度，每步仍确认，重复确认/查询不多推进；新不完整反向意图替换旧锚点。未知混合语义仍走模型，不能宣称完整解析或模型准确率提升。

最新UI为已推送核验的 `e8096317ed885ce255c77667f238e6743d0ce8c7`：专业全窗地图、简控左810地图/右视频、坐标条及唯一原控制权入口，117项、构建及87场景通过，[JSON/PNG与root来源](evidence/cockpit-simple-stage-20261016.json)已持久归档；161夹具API/21夹具确认，0越界/设备/浏览器错误。剩余设备详情/轮播、天气和三列仪表可继续按固定正式线 `35574cba` 做本地对齐；缺字段保持unknown，原参考工作区有冲突，仅用git show读固定提交，不导入旧业务取权或自动启流。

当前100条[模型候选报告](../artifacts/cockpit-intents-100-20261016-v1-prompt-v1.0-current-98178f8/report.json)及[来源](../artifacts/cockpit-intents-100-20261016-v1-prompt-v1.0-current-98178f8/provenance.json)为83符合/7不匹配/10超时、104请求。仅建议层，不经过CockpitService或确认/执行；危险写提议1、预期动作57/59覆盖、route_06状态歧义仍保留。后续v2可补缺失动作/明确状态，不能重写v1或把这轮当近期服务修复的效果证明。

厂商[请求schema核验](evidence/dji-control-request-schema-20261016.json)支持的本地typed账本/绑定发布/严格结果链已实现；没有公开HTTP/Agent入口、默认broker凭据provider、持续心跳许可或control_source映射。夺权Data为null，DRC client_id是设备连接ID；平台owner300秒是原心跳清理策略，不是厂商租约。请求成功仅request_succeeded，持续航权/链路仍unknown。下一步公开取权接入必须保留这些边界，并先设计来源/版本/失效与恢复契约；不能把一次成功或ID相似性升级为动作许可。

可先独立验证真实MySQL的DDL、二进制标识、scope锁/事务及迁移兼容。本机有MySQL26.7.0，Docker daemon不可用；尚未启动数据库/连接业务库，生产版本未知。只允许新建临时datadir/socket、回环端口与合成测试库，不启动Java业务Application/DataInitRunner；不把本机版本验证称同版本生产验收。保持H2离线门禁唯一真源，不将可选MySQL集成加入默认离线discover。

剩余验收包括权威航权/DRC控制源、真实固件回包、MySQL兼容与完整普通驾驶舱线上对照。真实执行和共享部署开关继续关闭；不要将传输连接关联、模拟器、模型候选或单元测试提升为真机效果。后续复用现有循环、会话和确认路径；业务迁移、共享部署与具体设备验收须单独覆盖授权。
