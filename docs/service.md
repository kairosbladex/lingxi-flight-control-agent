# 服务化 + 检查点持久化

本文件描述工作项 2 落地后的对外契约：**一次任务可以跨请求、跨进程续跑**，确认握手不再
依赖「同一个进程里活着同一个 `AgentCheckpoint` 对象」。

涉及文件：

| 文件 | 作用 |
| --- | --- |
| `src/lingxi_flight_control/session.py` | 会话记录、`CheckpointStore`（内存/文件）、`SessionRunner` |
| `src/lingxi_flight_control/service.py` | `AgentService`（纯路由，socket-free）+ `build_http_server`（薄壳） |
| `tools/agent_service.py` | 常驻服务 CLI（真实模型） |
| `tools/bench/service_e2e.py` | 真实 HTTP 端到端脚本（**不是 unittest 模块**） |
| `tests/test_session.py`、`tests/test_agent_service.py` | 离线单测（不开任何 socket） |

---

## 1. HTTP 契约

所有响应体都是 JSON 对象，`Content-Type: application/json; charset=utf-8`，
序列化统一 `ensure_ascii=False, sort_keys=True`，`HTTP/1.1` 且始终带 `Content-Length`。
请求路径的查询串会被忽略，`/healthz/` 这类结尾斜杠等价于无斜杠。

### 1.1 路由表

| 方法 | 路径 | 成功 | 失败 |
| --- | --- | --- | --- |
| `GET` | `/healthz` | 200 `{"status":"ok","execution_mode":…,"sessions":N}` | 405（其它方法） |
| `POST` | `/sessions/{session_id}/messages` | 200 = `SessionRunner` 响应（见 1.3） | 400 / 404 / 405 |
| `GET` | `/sessions/{session_id}` | 200 会话摘要 | 404 / 400 |
| `DELETE` | `/sessions/{session_id}` | 200 `{"deleted":true}` | 404 / 400 |
| 其它 | 任意 | — | 404 `NOT_FOUND` |
| 已知路径 + 不支持的方法 | — | — | 405 `METHOD_NOT_ALLOWED` |

`session_id` 必须匹配 `^[A-Za-z0-9._-]{1,64}$`（文件存储把它当文件名用，这是路径穿越
防线）。路径里的百分号编码会先解码再校验，所以 `/sessions/a%2Fb/messages` 得到 400
`SESSION_ERROR`，`/sessions/../../etc/passwd/messages` 直接是未知路径 404。

### 1.2 请求体（`POST .../messages`）

```json
{"text": "接管控制权，然后向前飞10米，再拍张照片",
 "confirmed": false,
 "request_id": "req-1",
 "context": null}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `text` | string，必填、非空白 | 用户指令；同一 session 内改变 `text` 会**丢弃旧检查点与旧请求日志**，从头开始新任务 |
| `confirmed` | bool，默认 `false` | 调用方声明「本次放行确认」；语义见第 4 节 |
| `request_id` | string 或 null | 幂等键；同一个 session 内重复出现时直接回放已存响应（第 3 节） |
| `context` | object 或 null | 运行时上下文；由 `BoundedAgentLoop` 校验新鲜度，服务层只做「是不是对象」的类型检查 |

400 `INVALID_REQUEST` 覆盖：JSON 不可解析 / 体不是对象 / `text` 缺失或空白 / 类型不对
（`confirmed` 不是 bool、`request_id` 不是 string、`context` 不是对象）/ 体不是 UTF-8。

### 1.3 响应（`SessionRunner.handle` 的稳定键）

```json
{"session_id": "bench-cockpit-baseline",
 "status": "clarification",
 "message": "请确认执行工具 control_acquire，参数：{}",
 "step_count": 0,
 "max_steps": 24,
 "executed": [],
 "tool_call": {"name": "control_acquire", "arguments": {}},
 "error": null,
 "checkpoint_revision": 1,
 "request_id": "req-1"}
```

- `status`：`LoopStatus` 的值（`tool_call`/`observation`/`final`/`clarification`/`tool_error`/`max_steps_exceeded`）；
- `executed`：`checkpoint.executed_tool_calls` 的工具名列表（确认轮次重复记录的
  `tool_calls` 不算，只有真正执行的才进这里）；
- `tool_call`：本次结果关联的调用（待确认或出错的那一个），没有就是 `null`；
- `error`：`checkpoint.last_error` 原样返回，没有就是 `null`；
- `checkpoint_revision`：本次写回后会话文件的 `revision`。

### 1.4 错误映射（HTTP 层不发明成功）

| 内部原因 | HTTP | 载荷 |
| --- | --- | --- |
| 会话过期 | **200** | `{"status":"stale_context","error_code":"SESSION_EXPIRED",…}` |
| 会话被篡改 | 400 | `{"status":"error","error_code":"SESSION_TAMPERED",…}` |
| 其它 `SessionError`（含非法 `session_id`） | 400 | `{"status":"error","error_code":"SESSION_ERROR",…}` |
| 请求不合法（含循环抛出的 `ValueError`） | 400 | `{"status":"error","error_code":"INVALID_REQUEST",…}` |
| 会话不存在 | 404 | `{"status":"error","error_code":"NOT_FOUND",…}`（DELETE 额外带 `"deleted": false`） |
| 未知工具 / 工具 handler 抛异常 / 运行时上下文过期 | **200** | 原样返回循环结果：`status: "tool_error"`，`error.code` 为 `tool_error` / `tool_exception` / `stale_context` |
| 未预期异常（兜底） | 500 | `{"status":"error","error_code":"INTERNAL_ERROR",…}` |

最后两行是重点：循环已经 fail-closed 的结论**原样**上报，HTTP 层不改判、不补成功。
`GET /sessions/{id}` 对过期会话也给 200 `stale_context`（与 POST 同一张映射表，保持一致）；
`DELETE` 对过期或损坏的会话仍然删除并返回 200——删除是恢复路径，不是执行路径。

### 1.5 示例

```bash
# 启动（simulator 后端，会话目录 .agent-sessions）
PYTHONPATH=src python3 tools/agent_service.py --port 8791

curl -s http://127.0.0.1:8791/healthz
# {"execution_mode": "simulator", "sessions": 0, "status": "ok"}

# 请求 1：只做确认握手
curl -s -X POST http://127.0.0.1:8791/sessions/s-1/messages \
     -H 'Content-Type: application/json' \
     -d '{"text":"接管控制权，然后向前飞10米，再拍张照片","confirmed":false,"request_id":"req-1"}'
# {"checkpoint_revision":1,"error":null,"executed":[],"max_steps":24,
#  "message":"请确认执行工具 control_acquire，参数：{}","request_id":"req-1",
#  "session_id":"s-1","status":"clarification","step_count":0,
#  "tool_call":{"arguments":{},"name":"control_acquire"}}

# 请求 2：放行并续跑
curl -s -X POST http://127.0.0.1:8791/sessions/s-1/messages \
     -H 'Content-Type: application/json' \
     -d '{"text":"接管控制权，然后向前飞10米，再拍张照片","confirmed":true,"request_id":"req-2"}'
# {"checkpoint_revision":2,"error":null,
#  "executed":["control_acquire","move_forward","camera_photo"],…,"status":"final"}

curl -s http://127.0.0.1:8791/sessions/s-1
# {"checkpoint_revision":2,"expires_at":…,"session_id":"s-1","status":"final",
#  "step_count":4,"user_input":"接管控制权，然后向前飞10米，再拍张照片"}

curl -s -X DELETE http://127.0.0.1:8791/sessions/s-1
# {"deleted": true}
```

---

## 2. CheckpointStore：schema 与 TTL

```python
class CheckpointStore(Protocol):          # session.py，@runtime_checkable
    def save(self, session_id, checkpoint, *, request=None, response=None,
             ttl_sec=None) -> SessionRecord: ...
    def load(self, session_id) -> SessionRecord: ...      # 不存在 -> SessionNotFound；过期 -> SessionExpired
    def delete(self, session_id) -> None: ...             # 不存在时静默
    def list_sessions(self) -> tuple[str, ...]: ...       # 含已过期条目，顺序稳定
```

`save` 是 **upsert**：每次自增 `revision`，`created_at` 保持不变，`updated_at = now`，
`expires_at = now + ttl_sec`（默认 `DEFAULT_TTL_SEC = 1800` 秒）。`request` 只用来提供
`request_id`；`request_log` 追加 `{"request_id": …, "response": {…}}`，滚动保留最后
`MAX_REQUEST_LOG = 50` 条。两个实现都会深拷贝检查点（内存实现用 `copy.deepcopy`，
文件实现用 JSON 往返），因此存储与循环持有的可变对象不再共享状态。

### 2.1 文件布局与 fail-closed

```
<store-dir>/<session_id>.json      # 一个会话一个文件；目录默认 .agent-sessions
```

```json
{"schema_version": 1,
 "session_id": "s-1",
 "checkpoint": { "...": "AgentCheckpoint.to_dict() 原样落地" },
 "created_at": 1760000000.0,
 "updated_at": 1760000012.5,
 "expires_at": 1760001812.5,
 "revision": 2,
 "request_log": [{"request_id": "req-1", "response": {"status": "clarification", "...": "..."}}]}
```

写入是**原子**的：先在同一个目录写临时文件、`flush + fsync`，再 `os.replace` 覆盖目标，
失败时清理临时文件。读取时以下情况一律 `SessionTampered`（绝不半信半疑地续跑）：

- JSON 不可解析 / 不是 UTF-8；
- `schema_version != SESSION_SCHEMA_VERSION (=1)`，或字段缺失；
- `checkpoint` 结构不合法，或 `runtime_context` 存在但不是合法的
  `ExecutionContext` 映射（缺字段、`context_version` 为负、`confirmed` 非 `True` 等）；
- 文件内容里的 `session_id` 与文件名不一致；
- `request_log` 条目有未知字段、`request_id` 非字符串、`response` 非对象。

`save` 如果发现已有文件损坏，同样抛 `SessionTampered`，**不静默覆盖**：要恢复就先
`DELETE /sessions/{id}`（服务层允许删除损坏会话）。

TTL 语义：`load` 在 `now >= expires_at` 时抛 `SessionExpired`（边界值即过期）。
过期条目不会被自动删除（`list_sessions` 仍列得出来），由 `SessionRunner` 在发现过期时
显式 `delete` 后从零开始，见第 4 节。

---

## 3. 幂等保证（重复提交不会产生第二次副作用）

`SessionRunner.handle` 里，`request_id` 的处理**排在所有执行之前**：

1. 加载会话（不存在→新会话；过期→删除后当新会话）；
2. 若 `request_id` 非空且该会话的 `request_log` 里已有同键响应，直接返回那份响应，
   **循环一次都不跑**（服务层与客户端拿到的字节内容一致，包括 `checkpoint_revision`）；
3. 否则才驱动循环，并把结果连同响应写回存储。

因此「同一个确认提交两次」的第二次：不调用模型、不调用工具、不改变状态。
`tests/test_session.py::SessionRunnerTests::test_same_request_id_is_replayed_without_running_the_loop`
断言响应逐字段相同且 `simulator.revision` / `camera.photo_count` 不变；
`tools/bench/service_e2e.py` 的请求 3 在真实 HTTP 上做同一件事。

注意两点边界：

- **同键不同文本**：`request_id` 命中优先，仍会返回旧响应。幂等键的语义就是「这个请求
  我处理过了」，所以客户端不该复用 `request_id` 表达不同意图。
- 响应日志是「有界追加」：每次保存都会追加一条，最多留 50 条；检索时同键取最后一条。

---

## 4. 「跨进程续跑」具体指什么

旧的确认握手把待确认动作存在**进程内存里的 `AgentCheckpoint` 对象**上：换一个进程，
待确认的动作就没了。现在待确认状态落在会话文件里：

```
请求1 (进程A)                     磁盘 <store-dir>/s-1.json                请求2 (进程B)
POST confirmed=false   ──写入──▶   checkpoint.confirmation_binding = h
                                   checkpoint.user_input           = "…"
                                   checkpoint.runtime_context      = {drone-01,…}
                                   revision = 1               ◀──读取──  POST confirmed=true
                                                                          │ BoundedAgentLoop 重新校验：
                                                                          │ user_input / context /
                                                                          │ runtime_context / tool call
                                                                          ▼ 全部一致才执行
                                                                   执行结果写回 revision = 2
```

- **进程 A 与进程 B 不共享任何 Python 对象**：各自新建 `FileCheckpointStore`、
  `SessionRunner`、执行器，只有文件是共享的。检查点的续跑完全由
  `AgentCheckpoint.to_dict()/from_dict()` 的 JSON 往返支撑。
- 每次请求（无论成功、`clarification`、`tool_error`、`max_steps_exceeded`）都会写回
  检查点。特别是失败路径也要落盘：循环在 `stale_context` 时会清空
  `confirmed`/`confirmation_binding`，如果这次不写回，磁盘上就会残留一个「已经不该
  再被使用的待确认绑定」。
- 单次请求内最多驱动 `max_rounds`（默认 12）轮；撞上限时返回 `clarification`，
  下一个请求可以继续接手，不会无限自转。

### 4.1 `confirmed` 的确切语义（判断取舍，写在这里以免误读）

按工作项要求，运行器完全照 `tools/flight_agent_demo.py::run_cockpit` 的方式驱动循环：
`loop.run(text, context=context, checkpoint=checkpoint, confirmed=(confirmed or checkpoint is not None))`，
并且「调用方是否放行」= `confirmed=True` **或** 存储里的检查点已经停在 `clarification`
（上一请求留下的握手）。于是：

- 新会话 + `confirmed=false` → 只做握手，返回 `clarification`，不执行任何动作；
- 新会话 + `confirmed=true` → 在这一次请求里连续推进，直到 `final` 或另一个 `clarification`；
- 已存在待确认检查点 + `confirmed=false` → 视为「调用方已放行」继续推进（与
  `run_cockpit` 的 auto-confirm 同构）。

也就是说 `confirmed` 是**客户端声明的意图**，不是鉴权凭据：任何知道 `session_id` 的调用方
本来就可以发 `confirmed=true`，多一个字段拦不住它。真正的授权判定在循环里，见第 5 节。
如果业务上需要「每个动作都必须由人确认一次」，要在服务之前加一层人工确认/审批，
而不是依赖这个布尔字段。

---

## 5. 授权边界没有变化

HTTP 层**没有新增任何绕过路径**。会不会执行一个工具调用，仍然只由 `BoundedAgentLoop`
的确认绑定说了算：绑定哈希覆盖 `user_input` + `context` + `runtime_context` + 工具名 +
工具参数（`agent._binding`），`preview`/`confirm` 两阶段还要再校验上下文版本、幂等键、
确认绑定与 TTL。会话持久化只是把**同一个检查点原样喂回去**：

- 改过 `runtime_context`（例如把 `drone_id` 改成别的无人机）→ 循环判 `stale_context`，
  返回 `status: "tool_error"`，一个动作都不执行
  （`tests/test_session.py::SessionRunnerTests::test_tampered_runtime_binding_fails_closed_and_clears_the_binding`）；
- `text` 与检查点的 `user_input` 不一致 → 直接丢弃检查点，当新任务处理；
- `confirmed=false` 不会让任何**尚未被绑定校验通过**的调用执行；
- 用户提交的 `context` 会被循环与注册表当前的运行时上下文比对，不一致走 `stale_context`。

服务层额外做的只有三件事：类型校验请求体、把 `SessionError` 家族翻译成状态码、
把循环结果原样序列化。它不看工具名、不做白名单判断、不构造工具调用。

---

## 6. 可复现命令与实测结果

以下命令都在仓库根目录、`PYTHONPATH=src` 下运行。

```bash
# 1) 全量离线单测（含守护测试本身）
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'

# 2) 无网络守护：把 socket 连接入口换成抛错，再跑一遍全套
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_offline_guard.py'

# 3) 只跑本次新增的两个模块
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_session.py'
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_agent_service.py'

# 4) 语法检查（两个工具脚本）
python3 -m py_compile tools/agent_service.py tools/bench/service_e2e.py

# 5) 真实 HTTP 端到端：离线脚本决策器（不需要模型）
PYTHONPATH=src python3 tools/bench/service_e2e.py --offline-scripted

# 6) 同上，但在两个请求之间整段重启服务（跨进程续跑的实测）
PYTHONPATH=src python3 tools/bench/service_e2e.py --offline-scripted --restart-between-requests

# 7) 真实模型端到端（会调用 238 共享端点；串行、超时 900s、keep_alive=30m）
PYTHONPATH=src python3 tools/bench/service_e2e.py --model <模型名> --base-url <端点>

# 8) 常驻服务 + 手测
PYTHONPATH=src python3 tools/agent_service.py --port 8791
curl -s http://127.0.0.1:8791/healthz
```

### 实测（本次运行记录）

| 项目 | 结果 |
| --- | --- |
| `unittest discover` 全量 | 265 个用例，`OK` |
| `test_offline_guard` | 1 个用例，`OK`（0 次连接尝试） |
| `test_session.py` | 34 个用例，`OK` |
| `test_agent_service.py` | 21 个用例，`OK` |
| `py_compile tools/agent_service.py tools/bench/service_e2e.py` | 通过（无输出） |
| `service_e2e.py --offline-scripted` | 退出码 0；请求1 `clarification`、请求2 `final`、重放响应逐字段相同；`executed = ["control_acquire","move_forward","camera_photo"]`；模拟器 `revision=3`、`photo_count=1`、`latitude≈9.009e-05`；重放前后状态摘要不变 |
| `service_e2e.py --offline-scripted --restart-between-requests` | 退出码 0；重启后 `/healthz` 报 `sessions=1`，`GET /sessions/bench-cockpit-baseline` 读到 `status=clarification`、`revision=1`（来自磁盘），请求2 续跑到 `final` |
| `service_e2e.py --timeout 900`（**真实模型** `qwen3.5:9b`，Lead 复跑） | 退出码 0；请求1 → `clarification`（`revision=1`、`executed=[]`、待确认 `control_acquire`）；请求2 → `final`、`executed=["control_acquire","move_forward","camera_photo"]`、`step_count=4`；请求3 原样重发请求2 → 响应逐字段相同，模拟器状态 `revision=3 / photo_count=1 / latitude≈9.009e-05` 重放前后不变（无第二次副作用） |
| `service_e2e.py --timeout 900 --restart-between-requests`（**真实模型**，Lead 复跑） | 退出码 0；请求1 之后整段重启（新执行器 + 新运行器 + 新 HTTP server，只共享会话目录），重启后 `/healthz` → `{"execution_mode":"simulator","sessions":1,"status":"ok"}`，`GET /sessions/...` 从磁盘读到 `status=clarification`、`revision=1`，请求2 续跑到 `final`，动作序列一致，重放响应逐字段相同 |
| `tools/agent_service.py --executor loopback --port 8899` + `curl` | `GET /healthz` → 200 `{"execution_mode":"loopback","sessions":0,"status":"ok"}`；`PUT /healthz` → 405；空白 `text` → 400 `INVALID_REQUEST`；会话目录被自动创建 |

**未实测（标注为未测）**：

- 多进程/多副本并发竞争同一会话文件的行为未测（当前实现是「最后写入者覆盖」，
  没有跨进程锁；服务设计上假设单写者）。这里不声称任何并发保证；
- TTL 到点后磁盘文件被外部清理程序回收的行为未测（本实现不主动回收过期文件）。
- 真实平台执行器（MQTT/串口）未接：`--executor` 目前只有 `simulator` 与 `loopback`，
  见 `docs/platform-executor.md`。

**过程披露**：实现该工作项的过程中，一次 CLI 手测误发了 1 次真实模型调用
（`read_state`，串行、只读、无副作用）；随后的验证只用离线路径，
上面的真实模型 e2e 由 Lead 复跑并记录。

---

## 7. 已知取舍（判断调用）

1. **过期不自动删除**：`load` 抛 `SessionExpired`，`list_sessions` 仍列出该 id。
   `SessionRunner` 在发现过期时先 `delete` 再从零开始，这样旧的 `request_log` 不会被
   新会话继承（否则一个早已过期的 `request_id` 会把新请求答成旧响应）。磁盘上的过期
   文件因此最多留到下一次同 id 请求或人工 `DELETE`。
2. **`GET` 过期会话返回 200 `stale_context`**：与 POST 使用同一张错误映射表，避免
   「同一个内部原因在两个路由上给两种状态码」。
3. **`DELETE` 允许删除过期/损坏会话**：它是恢复路径，删掉损坏文件是唯一不需要人工登录
   机器的修复方式。
4. **`request_log` 追加而不去重**：按工作项「appends entries」的字面要求实现；检索同键
   取最后一条。实际上运行器的幂等命中不会再次保存，所以日志里的 `request_id` 在实践中
   是唯一的。
5. **`checkpoint_revision` 在保存前推算**：`save` 必然使 `revision` 自增 1（或新会话为 1），
   运行器据此先构造响应、再保存，从而保证**存进日志的响应与返回给客户端的响应逐字段
   一致**（否则幂等重放会差一个版本号）。存储实现若违反该契约，运行器会把返回值的
   `checkpoint_revision` 改回存储里的真实值。
6. **HTTP 适配层不写单测**：`build_http_server` 会真的 bind/listen socket，为了满足
   「单测 100% 无 socket」，它只由 CLI 与 `tools/bench/service_e2e.py` 覆盖。
