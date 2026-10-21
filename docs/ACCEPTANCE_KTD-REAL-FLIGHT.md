# Python直连原ktd接口：当前交付

最新部署：2026-10-21已更新至ca10023并迁移独立系统服务，详见[10086审计发布记录](ACCEPTANCE_FLIGHT-AUDIT-10086.md)。以下为此前版本历史，停止命令及工作目录以最新记录为准。

后续纠正：下文`UAV_MOVE_DISTANCE=false`只表示平台页面模式，不能据此断言无人机不支持定距移动。原Java已有实现，Python修正开发见[DJI动作映射](ACCEPTANCE_DJI-ACTION-MAPPING.md)；该新版本尚未替换本页记录的线上版本。

2026-10-21最新要求：只改Python/前端，不改Java。**以下当前记录替代文末历史Java候选方案，无需发布Java或执行其历史发布步骤。**

- 10086已切换真实助手，经同源`/flight-control-api/`连接独立Python，复用原Java登录、站点、任务、权限及控制接口。Python `311c3f9`，UI `08fb3e5`，console入口 `0eaeff6`。修复嵌入组件样式丢失，真实页面可见。
- 登录凭据仅转交原平台鉴权，不送模型或日志。服务端核验项目/设备/任务，确认前重验；不自动取权、不自动重试。旧接口权限标记legacy-read-api，不冒充厂商持续航权。
- 验证：Python唯一离线入口713/713（含socket守护），UI150/150、构建通过；console前序263/263。夹具覆盖相机提交、重复确认不重发、权限/任务变化与未知结果。
- 真实页面：天健湖草莓机库登录与设备范围检查通过，返回409 `TASK_REQUIRED`，输入/发送禁用；只读刷新仍正确阻塞。**无执行中任务，原权限接口`UAV_MOVE_DISTANCE=false`，所以“前飞10米再拍照”尚未全链路验收。** 不覆盖能力标志，不开启无位姿降级；旧控制提交仅submitted/not_verified，不能据此自动执行下一步。
- 没有发送真实控制指令，真实位移/媒体/转向/云台效果及实际模型指令链未验证。Java候选未改动、未合并发布，8088/共享Java服务未操作。监控保持暂停。

## 当前保留实例与回滚

- 入口`http://192.168.110.17:10086/afmap/`；独立服务`lingxi-flight-browser-10086.service`，端口18974，MemoryMax384M、MemorySwapMax0、CPUQuota50%、TasksMax32，未开机启用。
- 远端根目录`/home/zhuoyi/ktd/flight-browser-20261021/`，私有Python3.12、`source-direct-fix`及0700权限journal目录。为用户验收保留服务、审计、发布及回滚文件；本地临时18976已停止。
- 停止命令：`ssh test-front 'systemctl --user stop lingxi-flight-browser-10086.service'`。
- 当前静态版本`/home/zhuoyi/ktd/dsm-frontend-10086/releases/python-flight-visible`；回滚将site链接恢复到`/home/zhuoyi/ktd/dsm-frontend-10086/releases/http-draft-hash-20261020-d290f76`。nginx原配置位于`/home/zhuoyi/ktd/flight-browser-20261021/nginx-before.conf`，恢复后先检查再仅重载10086独立nginx。保留账本，不重发unknown命令，不动8088。

---

# 历史方案：原 ktd_backend 飞控助手接入候选（已被替代，不执行）

日期：2026-10-21。用户选择沿用原 ktd_backend；本轮不修改共享 Java 实例、不发送设备命令。

## 已实现

- Java 新入口 `/lingxi/agent/flight-control` 从平台登录态取得员工身份，核对项目领用、机库/无人机/任务绑定及任务执行状态；浏览器不能自报员工、网关凭据或上游 URL。
- 原模拟接口保持原含义。真实前端使用独立组件，复用飞控仓 CockpitSession 和确认/回执 UI，不再使用模拟 ControlAgent。
- Python `--java-gateway` 复用 `build_platform_cockpit` 和严格 KtdSnapshotProvider；禁止 legacy/pose-unverified 降级。每个员工、设备、任务、登录凭据隔离内存会话，最多32个上下文，闲置30分钟过期；SQLite账本按资源隔离，不记录登录凭据。
- 命令确认仍由既有确定性执行器校验权限、状态、有效期和幂等键。网关不自动获取控制权、不自动重试，POST采用固定长度流式请求，防止HTTP客户端透明重发。
- Java代理仅接受同机回环HTTP或固定HTTPS服务；平台登录令牌只由Java内部转交Python，再用于同一平台鉴权，不向模型提供令牌。

## 明确未完成的实飞条件

10086线上入口尚未切换，共享Java尚未发布本候选。这不是“真机已可用”的交付。

严格执行器当前只开放带Java持久回执的DJI六向位移，保留20米低空/隐含爬升门禁。拍照、转向和云台仍可能被 `TRACKING_UNSUPPORTED` 拒绝；本轮没有绕过它们。**不能把“前飞10米然后拍照”算已完成接入或验收。**

原Java快照默认缺少厂商航权和链路新鲜度证据，写动作仍关闭。已有测试白名单只是一种有到期时间的测试放行，不是权威航权证明；本轮没有设置该白名单、没有打开平台控制开关。

首次实际接入仍须：共享Java由用户发布；准备并核验命令表迁移/实际MySQL、内部服务证书或同机回环、实时原帧来源、当前设备能力及授权。真实模型、多用户同时在线、真实设备移动/拍照和完整Java→Python→Java在线闭环均未验证。

## 配置及发布顺序

1. 发布 Java 候选模块，默认 `lingxi.flight-assistant.enabled=false`。内部服务配置：`lingxi.flight-assistant.url=http://127.0.0.1:<独立端口>`，`lingxi.flight-assistant.gateway-token` 通过私有部署环境注入，不进入Git、聊天或日志。
2. 同机启动 Python：`PYTHONPATH=src python3 tools/platform_cockpit_server.py --java-gateway --port <独立端口>`。只需私有环境配置 `KTD_BASE_URL`、`FLIGHT_AGENT_GATEWAY_TOKEN`、`FLIGHT_AGENT_JOURNAL`；journal父目录权限0700。KTD地址只允许固定源和可选 `/api` 路径；同机HTTP需 `KTD_ALLOW_INSECURE_HTTP=true`。不填写浏览器登录令牌，Java逐请求转交。
3. Java开关开启后先做真实登录只读context核验，Python仍不加 `--enable-platform-control`。确认服务端project/drone/task与当前页面一致，模拟响应必须拒绝。
4. 飞控UI执行 `npm run build` 与 `npx vite build --config vite.ktd.config.ts`；将 `dist/ktd` 发布到10086同源 `/afmap/flight-agent-ui/ktd/`。
5. 平台助手执行 `VITE_REAL_FLIGHT_ASSISTANT=true npm run build`，再按个人10086发布流程更新助手静态目录。缺组件/Java接口时拒绝，不回退模拟。
6. 真实写入前单独核验上述实飞前提及具体动作范围。没有这些证据，不加 `--enable-platform-control`，不把测试白名单当作真实航权事实。

## 验证记录

- Python唯一离线入口：`bash tools/ci/offline_tests.sh`，700项通过，含身份/上下文隔离、容量/过期、拒绝legacy、实际运行时只读装配；socket守护通过。
- 飞控UI `npm test`：150/150；类型检查、普通构建和独立ktd组件构建通过。
- 平台助手 `npm test`：263/263；真实入口开关构建通过，保留既有Cesium构建警告。
- Java首轮768MiB堆编译触发GC内存上限，未启动业务应用；调整Maven堆1408MiB、测试堆256MiB后70项定向测试通过。随后11模块完整指定回归及package成功，JUnit XML汇总242项、0失败/错误/跳过；新增6项网关用例含真实Controller参数绑定→Service→回环HTTP上游。
- 浏览器使用 `tests/fixtures/ktd-host.html`：合成身份p/s/d/t，context→消息→单次确认；确认前没有confirm，确认后文案为“平台已受理，真实飞行效果待验证”。仅本页夹具，无模型、平台或设备请求。
- 本地HTTP夹具端口18976已停止，浏览器已关闭；Java测试使用临时回环端口并在finally中停止。保留Java新工作树供审查，无共享服务变更。

## 回滚

先关闭Java `lingxi.flight-assistant.enabled`，停止本次独立Python实例；保留SQLite/MySQL命令日志，不重发unknown命令。10086前端用未开启真实开关的原助手包恢复。不要删除其它发布目录、切换共享8088或清空命令表。
