# 模型飞控最小链路：仅预览和确认

2026-10-15。用户本轮授权止于解析、预览、确认，不包含真机指令、共享部署或真实控制权申请。

## 实现

`FlightPreviewAgent` 复用 `BoundedAgentLoop`、`OllamaDecider` 和白名单工具契约。
模型仅输出四个水平相对移动动作及 `distance_m`，单次范围为 `0 < distance_m <= 20`。
缺距离、含糊距离、否定、超限或复合动作应澄清。模型异常不回退正则，不合成成功。
确认保存已展示的动作，不再次调用模型更换参数，也不恢复运行模型循环。

`geometry.relative_target` 使用平台 `GeoUtil.calcLatAndlon` 同一球半径 6371393 米。
航向 0/90/180/270 度分别朝北/东/南/西，左右后相对机头偏转计算。
完整驾驶舱模拟器同样使用此几何计算，修正原先“前进永远增加纬度”的实现。

纯函数 `platform_request` 编译现有 `POST /screen/uav/operation/control` 的 DTO：
前进 `type=202`、距离 `mode=2`、`param=10`；其他水平动作使用 203/204/205。
`siteId` 显式来自平台站点 ID，`terminalId` 来自设备绑定；不能把机库 ID 当站点 ID，
不能把任务批次当任务 ID。只有显式提供真实任务 ID 才生成 `taskId`。

此处只是接口请求格式适配。不同厂商对距离模式的支持仍需接入前核验。
`FlightPreviewAgent` 没有 transport、凭据、发送函数、配置开关或真实执行入口；
工具 handler 是禁止执行的占位。所有成功回执都带
`execution_mode=preview`、`dispatch_allowed=false`、`dispatched=false`。
确认结果固定为 `confirmed_not_dispatched`，不改变位置、控制权或业务数据。

## HTTP 与页面

`lingxi-agent/app/runtime/flight_preview_api.py` 提供适配器；ASGI 新增：

- `POST /api/flight-control/agent/preview`
- `POST /api/flight-control/agent/confirm`
- `POST /api/flight-control/agent/cancel`

沿用已认证 actor、项目/设备权限及 `/context` 生成的上下文版本。换机期间迟到的模型结果拒绝返回；
卡片绑定模型动作、参数、操作者、设备、位置、请求号，120秒到期，新意图替换旧卡。
重复确认不再次问模型，不再次产生动作；取消后的卡不能确认。

前端 `FlightControlAgentPanel.vue` 的文字入口改为 `/agent/preview`，
通过原 `/lingxi/agent/flight-control` 代理路径连接；该代理需保留服务端认证上下文。
下方原有手工按钮仍走模拟接口，旧 `/agent` 规则入口保留兼容，不被新文字入口调用。
确认卡用中文展示动作、距离、参考航向和目标位置；确认按钮明确标注不下发。

位置来自当前设备 `subject0201 / track` 页面消息的 `lng/lat/alt/yaw`，
使用本机接收时间校验15秒新鲜度，不用其他设备消息，不用默认0值补未知位置。
这是 `displayed_reference`，不能冒充服务器权威遥测；推理超时导致位置变旧时重新预览。
本轮仅支持水平位移，高度原样保留，不推断高度基准，也不申请控制权。

## 独立预览服务的错误码

`StandalonePreviewService` 的错误码是前端唯一可依赖的机器可读契约，与
`docs/service.md` 的约定一致：**循环抛出的 `ValueError` 是「请求不合法」→ 400，
500 只兜未预期异常**。

| 场景 | HTTP | `error_code` |
| --- | --- | --- |
| 请求体非 JSON、字段缺失或多出未知字段 | 400 | `INVALID_REQUEST` |
| 循环抛出的 `ValueError`（含同一 `idempotency_key` 换指令或位置） | 400 | `INVALID_REQUEST` |
| 确认卡不存在／已过期／与预览不一致 | 400 | `INVALID_REQUEST` |
| `confirmation_binding` 不是 64 位摘要 | 400 | `INVALID_CONFIRMATION` |
| 位置或航向越界、位姿字段非法 | 422 | `INVALID_POSE` |
| **参考位姿超过 TTL（含模型推理期间过期）** | 409 | `STALE_POSE` |
| 上下文版本已变化 | 409 | `STALE_CONTEXT` |
| 该无人机尚未写入位姿 | 404 | `POSE_NOT_FOUND` |
| 预览卡不存在或已被替代 | 404 | `NOT_FOUND` |
| 方法不支持 | 405 | `METHOD_NOT_ALLOWED` |
| 未预期异常（兜底） | 500 | `INTERNAL_ERROR` |

**2026-10-15 修正**：模型推理结束后 `preview.py` 会再校验一次位姿，过期时抛出的
`StaleReferencePose` 原先没有单独映射，落到了 500 `INTERNAL_ERROR`；同一条件在
`POST /pose`、`GET /pose` 上一直是 409 `STALE_POSE`。现在三处统一为 409，
源码级类型是 `preview.StaleReferencePose`（`ValueError` 子类，向后兼容）。
客户端因此可以按文档「推理超时导致位置变旧时重新预览」自动刷新位姿并重试一次。

## 验证与证据边界

- 飞控项目全量离线：307项通过，包含4个航向、左右后、跨日期线、真实模拟器转向后前进、
  20米上限、未知工具、模型错误、过期/篡改/重复确认、新意图失效、网络连接禁止。
- `lingxi-agent` 全量：1497项，4项跳过、11项预期失败；ground intent guard 通过。
  此结果运行于保留原有脏文件的工作区；新入口另外有独立定向回归。
- 前端定向：8项通过；`build:test` 通过；相关 ESLint 无错误，保留原有 `@openStatus` 命名警告。
- 隔离 Chromium 页面使用实际 Vue 组件，通过 ASGI TestClient 到真实 `qwen3.5:9b`：
  “往前飞10米”解析为 `move_forward(10)`；参考经纬度113/23、航向90度、高度50米，
  目标经度约113.0000976927、纬度约22.99999999997、高度50米。
  点击确认得到 `confirmed_not_dispatched`，记录到的真实设备请求为0。
- 真实模型另外4条：往前飞 / 不要往前飞10米 / 往前飞30米 /
  先往前飞10米再向右飞5米，均澄清，无确认卡，无下发。

这些是虚构设备、页面参考位置上的软件验证，不是测试环境登录验收、真实遥测精度或飞行验收。
未部署、未连接飞控接口，未验证设备距离模式能力。确认卡目前在内存中，重启后失效。
后续若授权真实执行，必须先接服务端权威设备/站点与能力校验、遥测刷新、控制权、
位移完成回执和明确的独立发送授权，不能把本轮“确认预览”复用为发指令授权。

复现：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'
PYTHONPATH=src python3 tools/bench/flight_preview.py --live --text '往前飞10米' --heading 90
```
