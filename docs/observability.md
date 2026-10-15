# 可观测性：结构化遥测（工作项 3）

## 交付内容

`src/lingxi_flight_control/telemetry.py` 提供事件模型 + 可注入 sink；`OllamaDecider` 与
`BoundedAgentLoop` 在关键点发事件。原先只有 5 个内存读数
（`last_prompt_estimate` / `last_omitted_observations` / `compressed_payloads` /
`omitted_observation_turns` / `last_generation`），现在它们变成可 grep、可聚类的结构化事件。

```python
from lingxi_flight_control import MemorySink, Telemetry, OllamaDecider, BoundedAgentLoop

sink = MemorySink()
telemetry = Telemetry(sink)            # 不传 sink 时默认 StdoutSink：一行一个 JSON 对象
decider = OllamaDecider(model="qwen3.5:9b", telemetry=telemetry)
loop = BoundedAgentLoop(decider, registry, telemetry=telemetry)
```

## 默认安静，默认 sink 是 stdout JSON line

- 不注入 `telemetry` 时，循环与解码器**一个事件都不发**（库不该意外往 stdout 写东西）。
- `Telemetry()` 不传 sink 时用 `StdoutSink`：一行一个 JSON 对象，`sort_keys=True`，
  同一事件字节级可复现。
- **事件里没有时间戳**，这是刻意的：时间戳会破坏「同一次 run 的事件序列可精确复现」，
  而序列可复现是这份遥测能被测试的前提。要时间戳就自己包一层 sink：

```python
import time
from lingxi_flight_control import Telemetry, TelemetryEvent

def timestamped(event: TelemetryEvent) -> None:
    payload = event.to_dict() | {"at": time.time()}
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
```

- `Telemetry(sink, enabled=False)` / `Telemetry.disabled()` 是空实现。

## 硬性保证：遥测不影响决策

`Telemetry.emit()` 做了三层保护，任何一层出问题都只**记数**，绝不抛给调用方：

| 计数器 | 含义 |
| --- | --- |
| `submitted` | 交付给 `emit` 的事件数（含被拦下的） |
| `emitted` | 真正写进 sink 的事件数 |
| `sink_errors` | sink 自己抛异常的次数（日志后端挂了） |
| `schema_errors` | 字段集合或枚举取值不符契约的事件数（编程错误） |
| `internal_errors` | `emit` 内部意外异常的次数（理论上恒为 0，可观测兜底） |

两条最容易被忽略的失败路径，都有专门测试：

- **sink 抛异常**：`test_sink_exception_does_not_change_the_run_result` 用「sink 必炸」
  跑一遍完整 run，和 `Telemetry.disabled()` 的 run 逐字段比对（status / error /
  observations / executed_tool_calls / handler 调用记录），必须完全相同。
- **字段名撞形参**：`emit(event_name, /, **fields)` 的 `event_name` 是位置-only 参数。
  否则 `unknown_tool` 事件的 `name` 字段会和形参撞名，把 `TypeError` 抛进决策路径。
  `test_fields_named_like_emit_parameters_cannot_break_the_run` 覆盖这一点。

预算拒绝、空响应重试、传输/协议错误、三档压缩及确认后执行也使用同一输入，分别关闭遥测、
注入内存 sink、注入必抛异常的 sink；比对请求体、返回结果、异常、重试次数和 handler 调用。
安全拒绝路径另外断言 handler 调用次数为 0。

## 事件清单

所有事件都带 `schema_version`（当前 `1`）和 `event`。字段集合在 `EVENT_FIELDS` 里集中定义，
测试 `test_decide_event_fields_and_generation_readings` 等会逐事件断言字段名与文档一致。

### `decide` —— 每次模型决策尝试（`OllamaDecider.decide`，含重试与发送前预算拒绝）

回答问题：「预算闸门在工作吗、离撞墙还有多远、生成有没有被截断」。

| 字段 | 含义 |
| --- | --- |
| `attempt` | 第几次尝试（空响应会重试，`max_attempts` 默认 2） |
| `kind` | `tool_call` / `final` / `clarify` / `empty`（模型什么都没给）/ `error`（本次尝试抛异常） |
| `tool` | 本轮调用的工具名，或 `null` |
| `empty_response` | 本次是否为空响应；仅此类响应允许有界重试，硬错误为 `false` |
| `prompt_estimate` | 本请求 prompt 的保守估算 tokens；构建失败时为 `null` |
| `prompt_budget` | `num_ctx - output_reserve` = 32768 - 2048 = **30720** |
| `num_ctx` / `output_reserve` | 两个真源参数，便于按配置聚合 |
| `budget_used_ratio` | `prompt_estimate / prompt_budget`，保留 4 位小数 |
| `near_budget` | `budget_used_ratio >= 0.9` |
| `omitted_observations` | 本请求被压掉的观察条数 |
| `compressed` | `omitted_observations > 0` |
| `done_reason` | Ollama 的停止原因 |
| `eval_count` / `prompt_eval_count` | 生成 token 数 / 服务端看到的 prompt token 数 |
| `generation_truncated` | `done_reason == "length"`，即生成被窗口截断 |

传输异常、服务端错误信封、协议解析错误及发送前预算拒绝都会产生一条 `kind=error`。
原异常继续上抛，不新增重试；由循环捕获时随后产生 `reason=model_error` 的 `fail_closed`，
`message` 保留原有失败原因。未收到本次响应时生成字段为 `null`，构建失败时预算估算及
`budget_used_ratio` 为 `null`，不会沿用上一次成功请求的读数。预算拒绝不会调用传输层。

### `compressed` —— 预算闸门真的压了观察（`OllamaDecider.payload`）

回答问题：「哪一步开始压、压到哪一档」。只有 `omitted_observations > 0` 时才发。

| 字段 | 含义 |
| --- | --- |
| `omitted_observations` | 本次被压掉的观察条数 |
| `kept_observations` | 本次仍然回放的观察条数（上限 `keep_recent_observations=12`） |
| `omission_mode` | `summary`（工具名+状态摘要）/ `compact`（裸占位）/ `none`（连占位都不放） |
| `prompt_estimate` / `prompt_budget` / `num_ctx` / `output_reserve` | 同上 |
| `compressed_payloads` | 累计发生压缩的载荷次数 |
| `omitted_observation_turns` | 累计被省略的观察条数 |

### `fail_closed` —— 任何硬失败的总闸门

`BoundedAgentLoop` 每一次 fail-closed 退出都发一条；`reason` 取自稳定词表
`FAIL_CLOSED_REASONS`（词表外的取值会被记为 `schema_errors`，不会到 sink）：

| `reason` | 代码路径 |
| --- | --- |
| `model_error` | 模型返回不可用响应，或输出越过安全边界 |
| `invalid_arguments` | 参数不是 JSON 对象 / schema 本身有问题 / 无法安全绑定 |
| `unknown_tool` | 模型调用了白名单外的工具 |
| `unsafe_output` | 工具回执含越界内容或不可序列化 |
| `runtime_context_error` | 运行时上下文非法 |
| `checkpoint_mismatch` | 检查点 `user_input` 与本次请求不一致 |
| `tool_exception` | handler（含执行器）抛异常 |
| `stale_context` | 检查点绑定的运行时上下文已过期 |
| `tool_error` | 其它硬失败（保守兜底） |

字段：`reason`、`message`（截断到 `MAX_MESSAGE_CHARS = 500`）、`tool`、`step`。
`step` 取失败当时的 `checkpoint.step_count`（首次决策前为 0，续跑保留已有计数）；
专用事件使用同一计数，不为了打点增加步骤。显式启用 `continue_on_tool_error=True` 时，
可继续的 handler 异常保留在检查点观察中，不误报为已 fail-closed 退出。

### `unknown_tool` / `stale_context` —— 两个专用事件

这两类失败在 `fail_closed` 之外**额外**发一条字段更贴切的专用事件：

- `unknown_tool`：`name`（越界工具名）、`message`、`step`。
- `stale_context`：`reason`（固定 `stale_context`）、`message`、`step`。

于是「统计所有硬失败」grep `fail_closed`，「只看模型越界」grep `unknown_tool`，
「只看检查点过期」grep `stale_context`，三者不冲突。

## 阈值与阈值依据

| 阈值 | 取值 | 依据（全部来自实测） |
| --- | --- | --- |
| `prompt_budget` | `num_ctx - output_reserve` = 32768 - 2048 = **30720** | `docs/model-selection.md`：Ollama 超窗会静默丢掉最前面的 system prompt，所以自己管住预算 |
| 实测 prompt 峰值 | **9169 / 30720**（约 30%） | `long_loop_budget` 16 观察实测；`--cockpit` 四口令实测峰值 9426 |
| `DEFAULT_NEAR_BUDGET_RATIO` | **0.9**（即 27648） | 实测峰值只有预算的 30%，所以 90% 不是正常波动，是真的接近撞墙 |
| 最坏生成 | `eval_count=780`、Agent 决策 395，`done_reason` 全 `stop` | `docs/model-selection.md`：`output_reserve=2048` 由此定；`generation_truncated` 是它的守护指标 |
| `keep_recent_observations` | 12 | 8 动作复合口令下 12/20/无上限行为一致，降到 6 且用裸占位会把收尾打成 fail-closed |
| `MAX_MESSAGE_CHARS` | 500 | 事件是日志：一条超长模型/平台输出不该把日志打爆 |

## 复现命令

```bash
# 离线测试（24 条遥测用例：字段契约、序列确定性、sink 异常不影响结果）
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_telemetry.py'

# 全量离线回归（含禁止网络连接的守护测试）
bash tools/ci/offline_tests.sh

# 真实模型 e2e + 结构化遥测（stdout JSON line），然后 grep 三类读数
PYTHONPATH=src python3 tools/flight_agent_demo.py --cockpit --telemetry > /tmp/cockpit.jsonl 2>&1
grep '"event": "decide"' /tmp/cockpit.jsonl | head -3          # 预算 / 生成读数
grep '"event": "compressed"' /tmp/cockpit.jsonl | head -3      # 哪一步开始压、压到哪一档
grep '"near_budget": true' /tmp/cockpit.jsonl                  # 该告警的情况（当前实测无）
```

收尾验收（2026-10-15，Python 3.13.2，本机离线）：基线 `ea133a4` 的遥测 **16/16**、
全量 **265/265** 通过；补齐异常尝试与实际失败步骤后，以上两条离线命令分别为
**24/24**、**273/273** 通过，网络守护通过。原有事件字段集合不变，新增 `kind=error` 取值。
本轮未重跑真实模型；下述为先前已有的模型实测记录。

实测（本仓库，`qwen3.5:9b`，2026-10-15，`--cockpit --telemetry`，四口令全部 `final`）：
共 **18** 条 `decide` 事件，全部 `done_reason=stop`、`generation_truncated=false`、
`near_budget=false`；`prompt_budget=30720`、`budget_used_ratio` 峰值 **0.3068**
（`prompt_estimate` 峰值 9426），`eval_count` 峰值 **334**，
`prompt_eval_count` 峰值 4961（服务端真实值；估算 9426 比它高约 1.9 倍，与
`docs/model-selection.md` 记录的保守口径一致）。本批口令没有触发 `compressed`
（观察条数未超过 `keep_recent_observations=12`），压缩路径由
`tools/bench/long_loop_budget.py` 与
`test_compressed_event_fires_only_when_observations_are_omitted` 覆盖。
每次命令结束后 demo 会打印 `[遥测]` 计数行，本批四条的 `sink 异常 / 字段违约 / 内部异常` 全为 0。

## 未做的事

- 没有接 Prometheus/OTel 导出器，也没有打点聚合服务：本次只交付「事件 + sink 挂点」。
- 没有事件采样/限流：当前事件量是「一次模型调用一条」，暂不需要。
- 没有把 `max_steps_exceeded` 做成事件（它是截停不是违规）；需要时按 `fail_closed` 同款加。
