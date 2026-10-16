# 自然语言飞行控制验收记录

状态：进行中，未完成真实设备控制或完整线上界面验收。需求来源为本会话用户目标；实现边界与全部历史证据见 [交付说明](natural-language-cockpit.md)。继续任务不需要重建AgentLoop、会话或工具注册体系。

| 范围 | 当前代码或证据 | 验收边界 |
| --- | --- | --- |
| Agent意图、澄清与确认 | `feature/natural-language-cockpit@e0ef1e744b13fd723b12962ea7414b060df2bcf2`；离线619/619；已推送核验 | 原loop/会话/确认/journal接通独立vendor请求；默认关闭，旧59动作及prompt不变；一次厂商结果不授予持续航权或飞行续接 |
| 指定前端 | `lingxi-flight-agent-ui@21b9239a0e81e194e771bcacfa408f624d87efdd`；140项、115个离线浏览器场景；已推送核验 | 同助手的typed请求卡和独立手动结果，旧102场景保留，无自动查询/重发/飞行续接；设备详情/天气及完整线上像素/业务未验收 |
| 服务级模型候选 | 修复前102条：90匹配/6不匹配/6超时；prompt v1.1相同6条定向全部匹配，其余96条missing | 同一真实服务/模拟器预览，0确认/写入；59动作仅指标签覆盖，不是59项模型全对，不作整批A/B或飞行准确率结论 |
| Java候选 | `feature/flight-agent-platform-snapshot@e623312c173fd698657a39fa22e4ad98c0630a80`，仅本地提交；生产代码仍为af034e1 | 11模块232/232；本轮仅增真实converter导出测试/13案例/2快照，不改变生产；Agent已接入口，持续航权、MySQL、Broker与设备效果未验证 |
| Java正式线 | 缓存 `prod=98a8da4bf13f4ba623fb816da1a5cb9b09f8453a` | GitLab下载/推送已拒绝，未重试；不称最新远端prod |
| 原Java工作树 | `/Users/wangxiaolin/IdeaProjects/ktd_backend`，`feature/geo-video-sync-recompute@858f6deec67d93734242a52845215a4d78736d6d` | 本轮重新核对干净且未改 |

本轮 [typed请求链证据](evidence/cockpit-vendor-request-20261017.json) 覆盖29个新增测试方法和最终619/619；[正式回环HTTP](../artifacts/vendor-control-smoke-20261017-final/report.json) 为46 Agent/51自有假Java HTTP、3vendor POST、6脚本模型决定、8份严格18字段报告，25份source前后稳定。原样Java fixture/56正文hash、10源码/测试提交字节及23当次XML由root独立核对；Maven232/232来自Java worker运行，root没有重复执行。所有工程数字与真实模型/业务/设备验收分开；本轮真实模型、平台/业务写入、MQTT/设备和部署/重启均0。以下保留既往阶段的原始验收快照。

UI独立复核见 [根Agent记录](evidence/cockpit-vendor-ui-20261017.json)：root重跑140项、构建及diff，核对最终115场景JSON、67份source/run/build hash、193份可恢复归档hash、11份indexed提交字节，并目视1920确认、414成功和错method警告三图。最终224夹具API/31夹具确认，0禁止/设备请求/浏览器异常；20新单测、13新浏览器场景，旧102保留。Git只存12份相关文件/2360722字节，其他完整原图可按UI provenance中的 `/tmp/lingxi-vendor-ui-20261017.6TxuXx` 恢复，不称全部截图跨主机持久化。浏览器runner由UI worker运行，root未重复执行。

Agent新增17个测试方法，完整唯一门禁 `bash tools/ci/offline_tests.sh` 564/564，socket守护通过；运动28/28、回执17/17，独立23组合成行为全部通过。对完全明确的运动/转向序列核对下一未完成步骤；每步仍要确认，重复确认/查询不多推进，新方向替换旧锚点，LF/CRLF连续补参不丢方向。混合/未知表达沿用原模型路径，不是完整语言解析器。[顺序证据](evidence/cockpit-ordered-motion-20261016.json)保留真实红绿、源码哈希及边界，未重跑模型评测。

随后追加9个数量门禁测试方法/45子例，最终完整573/573；范围/备选/上下界和明确约数不能由模型选数出卡，同值跨动作不能借证据。英文mile不再被m前缀当作米；精确位移附独立速度限制、单位换算与补答保持。独立20组/22轮全部符合预期，0确认/派发/状态变化；[数量证据](evidence/cockpit-quantity-bounds-20261016.json)记录全部真实红绿与最终哈希。仅合成模型提议的策略验证，原100条模型报告不变。

Java既有[连接绑定证据](evidence/cockpit-mqtt-connection-binding-20261016.json)继续保留；新[厂商请求事实链证据](evidence/cockpit-vendor-control-20261016.json)对应同一journal内三种typed请求、固定连接发布和严格来源/方法/ID/时间关联。新增43项、原165项回归；root复核20份源码、22份XML的哈希/计数/时间、4份fixture、schema与控制台哈希及提交字节。独立VO成功仅为request_succeeded，航权/DRC仍unknown，没有HTTP/Agent入口或默认凭据provider。owner心跳更新可能使旧尝试失效；提交后unknown继续占scope，无恢复API。迁移仍是人工候选，仅H2验证。

此前UI[87场景记录](evidence/cockpit-simple-stage-20261016/run-B0snq3/evidence.json)、[来源](evidence/cockpit-simple-stage-20261016/provenance.json)与[root核验](evidence/cockpit-simple-stage-20261016.json)继续保留：1920下左810地图/右视频、95/48px坐标条及控制权位置，1280按原比例缩放且修复顶栏背景遮挡；同一canvas/会话/卡片和唯一原确认链不变。

最新三联仪表见[96场景原始记录](evidence/cockpit-simple-instruments-20261016/run-nrD1Gh/evidence.json)、[来源/三组红绿](evidence/cockpit-simple-instruments-20261016/provenance.json)和[root核验](evidence/cockpit-simple-instruments-20261016.json)：120项与构建由root独立重跑；175次夹具API、21次夹具确认，0禁止/设备请求/浏览器错误。九个产品/依赖/素材清单与提交字节相同，55文件/43PNG/12JSON逐份校验。1920/1280仪表外框符合独立源码推导，零值/负角/未知与保卡验证；±180读数文字重叠从各1处修到0。ASL基准未知、原始带符号航向、未知速度字号和端点文字避让均明确为适配；平台缺速度/云台不补造。设备完整内容、天气、真实遥测及完整线上视觉/业务仍未验收。

模型[原始报告](../artifacts/cockpit-intents-100-20261016-v1-prompt-v1.0-current-98178f8/report.json)与[来源/逐类分母](../artifacts/cockpit-intents-100-20261016-v1-prompt-v1.0-current-98178f8/provenance.json)对应20:50:04—21:07:57（台北）、既有局域网GPU `qwen3.5:9b`。p50 6.991秒、p95 30.009秒，已知350891输入/12314输出tokens，总usage及费用未知。覆盖57/59个预期动作；缺投放器和上传航线。危险写提议1项为把“十来米”猜成10米；航线样本仍有paused上下文与execute_route标签歧义；另5项为no_action/clarification差异。原100分母和旧报告均保留。该bench不经过CockpitService，准确率 `not_evaluable`，不能证明近期服务修复或A/B改善。

真实控制继续受 `VENDOR_AUTHORITY_UNAVAILABLE`、`CONTROL_LINK_FRESHNESS_UNAVAILABLE`、`UAV_MOVE_DISTANCE=false` 与tracking默认关闭约束。[补充协议证据](evidence/dji-control-request-schema-20261016.json)明确夺权请求无actor/source字段、进入DRC的client_id为设备连接ID，不能直接绑定Java订阅者或员工；平台owner的300秒确有原心跳清理策略依据，但不是厂商航权租约。尚需有来源的员工/DRC/`control_source`版本及失效契约、机型固件与原始回包生命周期、同版本MySQL兼容，以及普通驾驶舱完整线上对照。业务库迁移、共享部署和具体设备动作仍需覆盖该动作的明确授权；离线候选不提供这些授权。

本续轮模型请求、业务写入、真实MQTT、设备动作、共享部署或重启均0；此前模型104请求和官方静态2GET仍属于原轮证据。Agent/UI语义分支已推送核验，Java仅本地提交且不重试拒绝推送，不合并或建MR。回退用独立revert，保留nullable列、历史命令及原模型证据，不补造身份或重发。下一步可推进本地隔离MySQL兼容及固定源码UI余项；本机MySQL26.7.0不是已确认的生产版本，当前尚未启动DB或连接业务库。只有相关代码/输入变化才重跑受影响门禁，工程测试不能提升为业务/设备验收。
