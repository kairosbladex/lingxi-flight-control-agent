# 灵犀智能助手聚合入口（任务下发 + 飞控共用一个页面）

2026-10-20。目标：在**不改 `ktd_backend`、不改 `onegismap_a` 源码**的前提下，让任务下发
Agent（`lingxi-agent`）和飞控 Agent（本仓库）共用一个页面入口。

答案是可以，但共用的是**外壳和入口**：两个助手仍然是两个服务、两套会话、两套确认与
授权。聚合层只做同源静态路由和反向代理，不解析业务请求、不持有凭证、不跨 iframe 传递
消息。

## 拓扑

```
浏览器
  └─ /agent-hub/            聚合宿主页（本仓库 web/agent-hub/index.html）
       ├─ iframe /task-dispatch/    任务下发前端（默认挂 lingxi-agent 自带页）
       │     └─ /api/chat|health|status|settings|speech|... → 127.0.0.1:5055
       └─ iframe /flight-agent-ui/  飞控前端（lingxi-flight-agent-ui 构建产物）
             └─ /api/flight-control/...                    → 127.0.0.1:8794
```

- 两个 iframe **懒加载**：首次切换才请求对应助手；切回后保留 iframe 和会话，不重载。
- 顶部切换条、`?view=task|flight` 深链、`Alt+1` / `Alt+2` 快捷键。
- `?task=<url>&flight=<url>` 可覆盖两个 iframe 地址（只接受 http/https 或站内路径）。
- 宿主页不设置 `postMessage`、不读取 iframe 内容、不注入脚本。

## 文件

| 文件 | 作用 |
| --- | --- |
| `web/agent-hub/index.html` | 聚合宿主页，单文件、无构建、无第三方依赖 |
| `tools/agent_hub_server.py` | 本地/隔离实例用的同源代理，只用标准库，默认只监听回环 |
| `deploy/agent-hub/nginx.conf` | 内网共享部署的 nginx 配置示例 |
| `tests/test_agent_hub_server.py` | 路由分类、路径穿越、上游校验、响应头、宿主页约定 |

## 本地启动（隔离验证实例）

三个进程都只监听 `127.0.0.1`：

```bash
# 1) 飞控模拟器服务（默认模拟器，每个动作需确认）
PYTHONPATH=src python3 tools/cockpit_server.py --port 8794

# 2) 任务下发 Agent（示例：最小 ASGI 启动，不预热模型）
#    需要按其自身文档配置在线服务密钥；本地只做页面/接口冒烟时可用占位值。
cd ../lingxi-agent
RUNTIME_DB_PATH=/tmp/lingxi-hub/runtime.sqlite \
TIANDITU_TK=<本地占位> TIANDITU_POI_TK=<本地占位> \
.venv/bin/python -c "import uvicorn; from app.asgi import create_asgi_app; \
uvicorn.run(create_asgi_app(), host='127.0.0.1', port=5055, log_level='warning')"

# 3) 聚合入口
cd ../lingxi-flight-control-agent
PYTHONPATH=src python3 tools/agent_hub_server.py --port 8800 \
  --flight-ui-dir ../lingxi-flight-agent-ui/dist \
  --flight-api http://127.0.0.1:8794 \
  --task-ui http://127.0.0.1:5055 \
  --task-api http://127.0.0.1:5055
```

打开 <http://127.0.0.1:8800/agent-hub/>。飞控前端先构建：

```bash
cd ../lingxi-flight-agent-ui && npm run build
```

如果不用构建产物，可省略 `--flight-ui-dir`，代理会转发到 Vite dev 上游
（默认 `http://127.0.0.1:5173`，其 `base` 已经是 `/flight-agent-ui/`）。

代理端也可以直接托管任务下发构建产物：

```bash
python3 tools/agent_hub_server.py --port 8800 \
  --task-ui-dir ../lingxi-console/dist --task-api http://127.0.0.1:5055
```

## 内网共享部署

使用 `deploy/agent-hub/nginx.conf`，替换注释里的四个路径/上游。要点：

- 只对外暴露聚合入口；两个上游继续只监听回环。
- `/task-dispatch/` 反向代理到任务下发服务根路径；`/api/*` 保留完整路径。
- `/api/flight-control/` 单独分流，不能被 `/api/chat` 规则吃掉。
- 聊天可能持续数十秒，配置 `proxy_read_timeout 180s` 和 `proxy_buffering off`。
- 不要给入口加 `X-Frame-Options`，否则无法被平台页面再嵌入。
- 部署层不改 `onegismap_a` 源码；如果要在平台页面内部直接出现该入口，仍需平台侧
  一个挂载点或注入脚本，那属于平台代码/部署产物变更。

## 验收记录（2026-10-20，本机隔离实例）

实际启动：飞控模拟器 `127.0.0.1:8794`、任务下发最小 ASGI `127.0.0.1:5055`、
聚合入口 `127.0.0.1:8800`；真实 Chrome headless 打开聚合入口。

| 检查 | 结果 |
| --- | --- |
| `GET /agent-hub/` | 200 HTML |
| `GET /flight-agent-ui/` | 200 HTML（构建产物） |
| `GET /task-dispatch/` | 200，页面完整渲染（标题与正文可见） |
| `GET /api/status`、`/api/settings/mode` | 200（任务下发上游） |
| `GET /api/flight-control/context` | 200（点开飞控气泡后发出） |
| 未知路径 `/admin` | 404，不代理 |
| 路径穿越 `/agent-hub/../README.md` | 404 |
| 首屏懒加载 | 飞控 iframe 在首次切换前 `src` 为空 |
| 切换与返回 | 任务页切到飞控再切回，两个 iframe 均未重载 |
| `?view=flight` 深链 | 直接激活飞控，任务页仍未加载 |
| 浏览器控制台 | 0 错误；API 0 个 4xx/5xx |

离线门禁：`bash tools/ci/offline_tests.sh` **694/694 通过**（含新增 25 项聚合入口测试），
`git diff --check` 无输出；socket 守卫通过。浏览器脚本：
`node tools/bench/agent_hub_e2e.mjs`（可选开发工具，需要本机 Chrome）。

**没有验证的部分**：真实模型对话、任务草稿下发、真机/平台写接口、内网 nginx 部署；
这些仍按各自 Agent 的授权与验证边界执行。聚合页成功只证明"一个页面能同时打开两个
助手"，不代表任何业务动作已被执行或已获得授权。

## 边界（必须保持）

- **会话不共用**：飞控确认卡绑定上下文版本与幂等键，任务下发草稿绑定项目/机库；
  两套会话混用会让确认打到错误的能力域。宿主页只切换视图，不做会话合并。
- **授权不共用**：飞控工具白名单、控制权租约、执行器（默认模拟器）与任务下发的
  下发权限是两套；共用页面不改变任何一条。
- **确认不互相代劳**：两个 iframe 之间不转发消息，一个助手的确认不能替另一个确认。
- **执行器默认关闭**：飞控仍是模拟器；`KtdPlatformExecutor` 默认关闭，真实设备控制
  需要覆盖具体动作的授权。
- **跨源覆盖仅用于本地调试**：`?task=` / `?flight=` 指向其它源时，浏览器会按正常
  CORS 规则处理，且两套前端的 Cookie/登录态不共享。
- **本代理不替代身份网关**：任务下发服务若以 `RUNTIME_PROFILE=platform` 运行，要求
  可信网关注入 `X-Agent-Gateway` 并由服务端解析操作者；聚合入口只做同源转发，
  不注入身份。生产环境仍应保留原有的 Java/网关注入链路，浏览器只跟聚合入口说话。
- **只证明页面聚合**：验收结果不代表模型对话、任务草稿下发、真实平台写入或设备执行
  已被验证；这些仍按各自 Agent 的授权节点执行。

## 资源清理

验证结束后停止本次启动的三个进程与 Chrome，并删除临时目录：

```bash
lsof -nP -iTCP:8794 -iTCP:5055 -iTCP:8800 -sTCP:LISTEN
# 按 PID 停止上述三个进程；再清理 /tmp/lingxi-hub-e2e
```
