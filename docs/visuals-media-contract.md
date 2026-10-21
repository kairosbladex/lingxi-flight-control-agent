# 驾驶舱媒体展示契约

本候选为驾驶舱提供独立、只读的媒体描述。它不属于模型工具，不取得控制权，不启流，不调用 `startVideo`，也不下载、代理或验证媒体内容。`available=true` 只表示存在经过来源策略检查的播放候选，不表示直播正在运行。

本轮 Agent 开发基线为 `feature/natural-language-cockpit@3d72d578f667ba0f01bba5dbb3b544ec63b2d753`。运行身份为本机离线开发与合成 HTTP 测试操作者；预算为45分钟、75次开发工具调用。实际 Java 源码、converter 合成样本、现有驾驶舱会话与传输接口是输入；输出为只读 provider、HTTP 路由、配置、测试和本文。允许的副作用是本地代码、临时 SQLite 与回环 HTTP 测试；共享部署、真实媒体播放、设备控制不在本轮验证内。

## 调用与资源绑定

浏览器沿用既有 Agent 客户端与可信网关，读取：

```text
GET /api/flight-control/visuals?context_version=1
```

请求体必须为空，query 必须恰好包含一个 `context_version`，且为非负 JavaScript 安全整数。浏览器不能提供 URL、项目、站点、机库、无人机、任务或额外 query。既有 `X-Agent-Gateway`、`X-Agent-Actor` 与 `X-Agent-Session` 校验先于 Java 读取；网关身份仅由服务端代理注入。

Python 使用服务端固定的 KTD 基址、登录身份与资源绑定，调用：

```text
GET /lingxi/agent/platform/visuals?siteId=…&hangarId=…&droneId=…&taskId=…
```

`taskId` 未绑定时省略。该读取使用既有 KTD transport 的禁止重定向、禁止继承代理、有限超时与无自动重试行为。播放 URL 不会成为 Python 的请求目标。

Java 响应 `data.schema_version` 为 `lingxi-platform-visuals/v1`，包含 `actor_employee_id`、`project_id`、`site_id`、`hangar_id`、`drone_id`、`task_id`、`generated_at`、`streams`。Python 逐项验证资源及操作者；未知字段、重复 JSON 键、类型错误与不完整描述均拒绝。读取期间释放会话锁，返回前再次校验绑定和会话 epoch；重置或替换意图后迟到的描述返回 `STALE_CONTEXT`，不携带播放 URL。

Java原始HTTP路由的成功与错误响应也显式使用`Cache-Control: no-store`，不依赖已关闭默认缓存头的全局配置。这里的只读指不启流、控制设备或修改业务实体；复用的旧实体查询在缓存未命中时可能回填Redis，本轮没有连接真实数据库或缓存验证该行为。

## 浏览器 DTO

成功响应恰好有8个顶层字段：

| 字段 | 含义 |
| --- | --- |
| `success` | 固定 `true` |
| `schema_version` | 固定 `flight-visuals/v1` |
| `execution_mode` | 固定 `platform` |
| `context_version` | 与请求和当前服务端绑定一致 |
| `resource_binding` | 恰好包含 `project_id/site_id/hangar_id/drone_id/task_id`；任务可为 `null` |
| `generated_at` | Java 生成描述的 Unix 秒，不是画面采样时间 |
| `streams` | 固定顺序 `site/hangar/drone`，各一项 |
| `trace_id` | 本次读取追踪标识 |

每条 stream 恰好包含 `kind/available/sources/reason_codes`。每条 source 恰好包含 `format/url/expires_at`；格式仅 `hls/flv/webrtc/mp4`，有效期未提供时保留 `null`。不从 URL query、机型、provider 名字或其他播放格式推测有效期与新地址。Java 的 `rtmp` 字段在旧路径中可能装载推流地址，因此不属于此契约。

`generated_at` 必须不晚于 Python 当前时钟，且不早于30秒前。这是描述读取的时间校验，与流在线状态和链接有效期无关。已明确过期的播放 URL 被移除；未知有效期保持未知。来源只留在这一响应内，不进入 `state`、模型 prompt、工具 allowlist、会话历史、checkpoint、命令 journal 或应用日志。GET 该路径的成功和失败响应都携带 `Cache-Control: no-store`。

## 服务端来源策略

| 环境变量 | 默认值 | 约束 |
| --- | --- | --- |
| `FLIGHT_AGENT_MEDIA_ALLOWED_ORIGINS` | `[]` | JSON数组，最多32个精确 origin，如 `["https://media.example.test"]` |
| `FLIGHT_AGENT_MEDIA_ALLOW_INSECURE_HTTP` | `false` | 仅接受 `true/false`，独立于 `KTD_ALLOW_INSECURE_HTTP` |

origin 比较包含协议、规范化主机和端口；只规范化主机大小写与 HTTP(S) 默认端口。拒绝通配符、域名后缀匹配、URL路径、userinfo、query、fragment，以及浏览器可能另解为 IP 的非标准数字主机名。允许的播放 URL 仍原样返回，不改写签名。

播放 URL 仅接受 HTTP(S)，拒绝 userinfo、控制字符、空白、反斜线、fragment、非 ASCII URI 和有歧义的主机/端口。HTTP 必须同时由独立开关及来源白名单明确允许。默认白名单为空，所有播放地址都会被移除；该默认值不会阻止读取不含 URL 的不可用描述。

流级固定原因如下；不会把上游异常或 URL 拼到错误文案中：

| 原因 | 含义 |
| --- | --- |
| `NOT_CONFIGURED` | 没有持久播放描述 |
| `FORMAT_UNKNOWN` | 无法从持久字段确定支持的格式 |
| `INVALID_DESCRIPTOR` | Java 描述字段或 URI 不合法 |
| `RESOURCE_NOT_AVAILABLE` | 关联媒体资源不存在或未通过绑定检查 |
| `MEDIA_ORIGIN_NOT_ALLOWED` | 未获服务端来源白名单允许 |
| `MEDIA_PROTOCOL_UNSUPPORTED` | 不是获准的 HTTP(S) URL |
| `MEDIA_URL_INVALID` | URL 结构不合法或存在歧义 |
| `MEDIA_URL_EXPIRED` | 权威描述明确给出的有效期已到 |

只有至少一条 source 通过检查时 `available=true`，同时 `reason_codes=[]`；否则 `sources=[]` 并给出固定原因。模拟器、未配置 provider 返回503 `VISUALS_UNAVAILABLE`；KTD 权限拒绝返回403 `VISUALS_FORBIDDEN`。超时、资源不匹配、坏响应和过旧描述保留各自固定错误码，不回放旧 URL。

## 前端接入要求

媒体描述使用独立展示状态，不能合入 Agent 的 `state` 或聊天记录。只有人工点击播放才建立媒体连接；初始化、切换标签和刷新描述均不能调用 `startVideo`。停止、资源变更、会话重置、权限拒绝或过期时销毁播放器、清除 URL、取消未完成请求；迟到响应须同时匹配当前资源和请求序号。

前端使用 `CockpitMediaSession` 独立保存展示状态，每路最多8个不同 `(format,url)` 候选；同格式的不同地址可单独选择。浏览器请求媒体时不携带 KTD bearer、Agent 网关凭据、Cookie 或 Referer。HLS 的变体、分段和密钥请求也限制到选中播放来源的 origin，FLV/MP4同样使用受限fetch，禁止重定向。HLS/FLV依赖浏览器MSE；MP4下载上限64MiB。未知有效期保持未知，已知期限按至多60秒的计时分片持续检查，到期清理播放器。

RTC只接受已从旧项目核对的ZLMediaKit HTTP(S) `/index/api/webrtc?app=…&stream=…&type=play`：POST原始SDP，接收 `{code:0,sdp:…}`，只申请视频`recvonly`，不使用摄像头/麦克风或STUN/TURN。HTTP来源白名单不能约束SDP中的ICE目标，因此前端在`setRemoteDescription`前另查每个候选：必须与信令源的字面IPv4/IPv6相同、端口1–65535；DNS/mDNS、异地址、无候选和`remote-candidates`返回固定的`MEDIA_ICE_SOURCE_UNVERIFIED`。连接地址只能为未指定地址或同IP，相关地址字段如出现也须同IP。ICE端口由协商决定，这不是浏览器UDP网络隔离；多CDN、域名RTC或其他协议需另行核验，前端可改选已有HLS/FLV。

错误不会附带原始媒体地址或上游文本，原生视频错误也不被猜测为403或CORS的确定结论。地图使用前端本地离线Cesium/NaturalEarthII，低分辨率且无地形，不进入此DTO。

## 验证与交付边界

定向测试：

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -p '*visuals.py' -v
```

测试穿过真实 provider、KTD transport、CockpitService、平台装配与 HTTP 入口；仅外部 Java/模型响应、时间及临时存储使用合成边界。覆盖默认拒绝、精确来源、HTTP独立开关、签名不改写、显式过期、错误分类、权限/资源不匹配、会话隔离、迟到响应、无缓存响应头，以及 URL 不进入模型/日志/checkpoint。实际 Java converter 样本另作为原样输入核对，不能据此认定真实媒体在线。

本轮冻结时定向测试21项通过，完整离线套件521项通过（包括禁止网络连接的离线守护）。HTTP 响应头单测使用真实 `RequestHandler` 与内存 socket 边界；另已完成一次本地回环 HTTP 响应头核对。首次完整运行曾因回环单测触发离线守护而失败，随后调整了测试边界，没有放宽守护。完整命令为 `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py' -v`。

冻结的仓库样本为 `fixtures/ktd-platform-visuals-v1.json`，SHA256 为 `144a532319ebdfe16b2113eedd6683a35045dcd95c38a73ae9071ef35ca9a72d`。Java target 原文件 SHA256 为 `f24474985c70543d23298c54d1109da2565772209aafac1b837efd74a9ec64f1`；仓库副本仅增加末尾换行，JSON 内容相同。所有资源与播放地址都是 Java 测试生成的合成数据。

相邻 [provenance](../fixtures/ktd-platform-visuals-v1.provenance.json) 绑定Java本地提交`57c96a5ca9f117c0668c8ad6ae2fb7fb8a3d8bcb`及源码哈希。根Agent另将真实Java样本经过实际Python transport/provider/Cockpit公共DTO，再送入真实前端`readVisuals`：8个顶层字段、site/hangar/drone候选与签名原字节均通过；这条合成契约验证没有访问真实播放地址。前端107项单测、73类完整页面场景及16类真实本机媒体播放场景通过，详见 [冻结证据](evidence/cockpit-visuals-20261016.json)。

本实现没有改变59项模型工具契约、飞行确认或回执生命周期。关闭来源白名单即可停止发出播放候选；撤回本次提交可回到未提供 visuals 的旧行为，无数据库迁移。共享环境是否已部署该 Java 读取接口、真实媒体服务的鉴权/跨域/可播放性，以及真实飞行效果，仍需各自的运行证据。
