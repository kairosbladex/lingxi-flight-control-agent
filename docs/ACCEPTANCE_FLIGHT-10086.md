# 10086 飞控入口与实飞准备

日期：2026-10-20。用户目标：在自己部署的 10086 前端测试，并准备今天或明天通过自然语言控制真实无人机。当前完成入口部署与离线验证，未完成在线身份、设备绑定或实飞验收。

## 已完成

- 沿用 `lingxi-console` 已提交版本 `bb4dc786face5bf53c1f96eceaf0c78edd16bfb1`，没有修改应用源码。飞控后端核验基线为 `f135a424b8fb41ad3ca611cce4d8a3ec91be370e`。
- 入口：`http://192.168.110.17:10086/afmap/lingxi-dispatch-preview/standalone.html`。
- 只新增 `/home/zhuoyi/ktd/dsm-frontend-10086/releases/be7990c/lingxi-dispatch-preview`，约 19 MiB；创建前确认不存在。现有 site 指向 releases/be7990c。
- 388 个文件 SHA256 与本机构建一致；按本次遍历顺序生成的校验清单摘要为 `50abd700dde8b79b47b48a35e15219befedb1c84b025ece27741726f509edd79`。线上 HTML 内容与本机一致，7 个直接引用资源均返回成功。
- overview 的 index.html 部署前后摘要保持 `d3985c8cb7f02e8f9ae99f352e7d11c180f461d1c1cab2bb47cc31762b0de9b0`。Nginx 主/工作进程仍为 73627/73629；未改配置、reload 或重启，也未改共享 8088 静态包。
- 10086 原有 API 代理转发到 8088。无认证 GET `/gis_server_a/user/info` 虽返回 200，但身份字段为空，不能算登录成功。

## 实际验证

- 前端 `npm test` 成功，随后 `VITE_TASK_WRITES_ENABLED=false npm run build` 成功（保留 Cesium 的构建警告）。Node 堆上限 2 GiB，shell CPU 时间上限 180 秒。
- `E2E_STANDALONE=1 E2E_PUBLIC_PATH=/afmap/lingxi-dispatch-preview/ node scripts/platform-ui-e2e.mjs`：34 场景通过，56 个夹具请求，外部网络/真实写入为 0。
- `node scripts/platform-ui-e2e.mjs`：28 场景通过，65 个夹具请求，外部网络/任务下发为 0；包含飞控页签、120 米卡片、确认模拟及返回任务页签。
- `bash tools/ci/offline_tests.sh`：694 项，7.455 秒，OK。
- 浏览器原始合成证据分别保留于本机临时目录 `lingxi-ui-e2e-fEZvDh/evidence` 和 `lingxi-ui-e2e-ZgW9SG/evidence`（位于 macOS TMPDIR）。这些结果只证明夹具合同与离线行为。

## 实飞仍缺什么

- 当前部署面板的 `controlApi.ts` 强制 simulator，拒绝非模拟回执。部署该页面不等于接通真实飞控。
- 本仓已有 [Route A](legacy-ktd-control.md)，但没有本轮在线设备验证。其控制权租约是合成值；位移需显式允许未核验姿态，不能因赶时间默认启用。转向依赖实时航向回读，运行期间聊天取消不能中止，需原驾驶舱停止/接管。
- 需用户在 10086 平台登录、选项目；不同域名/端口不共享 localStorage。不要在聊天中提供 token 或密码。
- 需确认本次机库/无人机、现场操作员接管条件及允许动作范围。自然语言“姿态”可能表示机身航向、位置/高度或相机云台，不能替用户任选。
- 下一步先核验选定设备的只读身份/归属/控制权/遥测，再决定真实接口接入。尚未启用真实写开关，也没有起飞、控制权抢占、MQTT 发布或设备动作。

## 资源与回滚

- 两次浏览器夹具均正常退出并关闭临时 HTTP 服务、Chrome 与临时浏览器配置；没有新增常驻服务或端口。
- 新增静态文件保留供用户验收，复用现有 10086 Nginx，无新增进程内存占用。不要停止该原有服务。
- 回滚只需把本次新增的 `lingxi-dispatch-preview` 目录移动到非站点备份目录；不删除原平台文件、不回滚整个 release、不修改 site 链接。
- 两个工作树开工时均干净；本轮仅在飞控仓保存此验收记录及 CONTINUE 链接，前端源码保持原样。
