# 灵犀飞行执行 Agent

独立维护已绑定执行上下文内的受限控制文本解析和内存执行闭环；同时保留任务下发模型作为兼容层。

## 当前范围

- 将单个中文方向和距离解析为 `RelativeMove`；
- 在已确认的 `ExecutionContext` 中执行相对移动；
- 用 `idempotency_key` 拒绝重复命令，用上下文版本拒绝过期命令；
- MemoryFlightSimulator 工具动态绑定当前 `ExecutionContext`，检查点恢复时拒绝执行批次、无人机或版本已变化的旧上下文；
- 通过注入的意图解释器生成结构化 `ToolCall`，维持旧任务下发调用方兼容。
- `full_control.py` 提供显式的 `FlightControlAction`、`RuntimeBinding`、`FlightControlState` 和
  `MemoryFlightControlSimulator`，覆盖驾驶舱控制权、飞行、航线、相机/云台、红外、载荷和状态读取。
  动作只在内存中预览、确认和执行；回执里的 `execution_mode` 由执行器提供，默认 `simulator`。
- 59 个驾驶舱动作通过 `build_full_control_tool_registry` 全部注册为模型可调用的白名单工具，
  每个动作都经过 `ToolRegistry` schema 校验 + 执行器 `preview/confirm` 两阶段。
- **执行器可注入**：`executor.py` 定义 `RuntimeExecutor` 协议（预览/确认 + 状态读取），
  `SimulatorExecutor` 包住现有模拟器（零行为变化），`LoopbackExecutor` 与 `FixtureExecutor`
  用于本机回环与报文夹具回放。**当前没有接真实平台**（无凭据），详见 `docs/platform-executor.md`。
- **安全快路径**：急停等安全动作走确定性精确匹配，不经过模型。实测走模型要 9 秒才能发出急停，
  快路径 1 毫秒；且模型服务不可达时快路径照常可用。详见 `docs/safety-fast-path.md`。
- **上下文预算**：发送前按 `num_ctx` 核对 prompt 预算并确定性压缩，绝不依赖服务端截断。
  默认 `num_ctx=32768`；system prompt 与当前 user 消息永不被丢。实测见 `docs/model-selection.md`。

## 安全快路径

```python
from lingxi_flight_control import SafetyFastPathDecider

decider = SafetyFastPathDecider(OllamaDecider(model="qwen3.5:9b"))
```

急停 / 返航 / 降落 / 取消返航 命中短语表时完全不发模型调用，执行后也不回问模型。
**只做精确匹配，绝不做子串包含**——「不要返航」包含「返航」，子串匹配会出人命。
起飞刻意不在快路径里（它需要高度参数，替用户猜高度是危险的）。

急停已豁免控制权要求（它只停止运动，且恰恰是控制权不在自己手上时最需要）。

## 驾驶舱工具

```python
from lingxi_flight_control import (
    BoundedAgentLoop, MemoryFlightControlSimulator, OllamaDecider,
    RuntimeBinding, build_full_control_tool_registry,
)

simulator = MemoryFlightControlSimulator(
    RuntimeBinding("actor-1", "project-1", "hangar-1", "drone-01", "batch-1", 1))
loop = BoundedAgentLoop(OllamaDecider(model="qwen3.5:9b"),
                        build_full_control_tool_registry(simulator), max_steps=24)
result = loop.run("接管控制权，然后向前飞10米，再拍张照片")
```

`build_full_control_tool_registry()` 也接受任何 `RuntimeExecutor`：真实平台适配器、
`LoopbackExecutor`（本机回环）或 `FixtureExecutor`（报文夹具回放）。换执行器不改工具契约。

```python
from lingxi_flight_control import LoopbackExecutor, build_full_control_tool_registry

loop = BoundedAgentLoop(
    OllamaDecider(model="qwen3.5:9b"),
    build_full_control_tool_registry(LoopbackExecutor(binding)), max_steps=24)
```

`execution_mode` 由执行器提供，并同时出现在状态、回执与 `action_manifest()` 里；
模式名会随回执回给模型，所以含 `mqtt`/`凭据` 这类禁用词的模式名会在构建期就被
`validate_execution_mode()` 拦下——否则每个动作都会 fail-closed。

`ACTION_SPECS` 是动作领域、描述与参数契约的单一真源，`action_manifest()`、
`action_tool_definitions()` 和工具注册表都由它派生。

两点使用注意：

- **`max_steps` 只计推进任务的步骤。** 确认握手不占步数（它只是用户授权，任务没有推进），
  所以 `max_steps=8` 足够「7 个动作 + 收尾」。握手次数另由 `confirmation_rounds`
  单独设上限，避免模型每次换一个调用就让确认无限往复。
- **参数校验失败可以自纠。** 参数越界、缺必填、多余字段不再让整个 run 死掉：
  校验错误会作为观察结果回喂模型，让它改正或向用户澄清。模型一直改不对时由
  `max_steps` 截停，并把最后一个错误通过 `AgentRunResult.error` 带出来。
  非法调用绝不会进入 `executed_tool_calls`。
  例外：未知工具、handler 抛异常、含不安全内容一律硬失败（fail-closed）。
- **跨轮次幂等需要调用方提供稳定键。** 默认每次调用生成新 `idempotency_key`；
  需要「恰好执行一次」时传入 `idempotency_key_factory`。"未经确认就执行"由
  `BoundedAgentLoop` 的确认绑定负责，两者不是同一层保护。

当前版本不连接 MQTT、真实飞控或生产任务平台；执行器由调用方注入（本次交付的是可注入挂点
与报文夹具，未接真实平台）。

## 模型大脑

`OllamaDecider` 是 `ModelDecider` 的真实实现，接入 `192.168.130.238` 上的 Ollama。
它只做协议转换：把 `ToolRegistry` 的工具契约翻成 function calling 声明，把检查点重建成原生对话，
再把模型输出收敛回 `ModelDecision`。安全边界仍然全部由 `BoundedAgentLoop` 和 `ToolRegistry` 持有。

选型结论是 `qwen3.5:9b`，理由与全部实测数据见 `docs/model-selection.md`。

**prompt 预算由解码器自己管住。** Ollama 在 prompt 超过 `num_ctx` 时静默丢掉**最前面**的 token，
也就是 system prompt 里的硬性约束，而且不报错。所以每次发送前都按
`num_ctx - output_reserve`（默认 `32768 - 2048`）核对预算：system prompt 与当前 user 消息
永不丢弃，只回放最近 12 条观察，更早的压成**确定性摘要**（工具名 + 状态，
如 `control_acquire✓ camera_photo✗`，含失败标记）；摘要自己优先被压——装不下先退化成
`（更早的 N 次工具调用已省略）`，再装不下才往前丢观察；连头部都装不下时抛
`PromptBudgetExceeded`。估算用保守的字符口径，可注入 `token_estimator` 覆盖，不依赖 tokenizer。

```python
from lingxi_flight_control import DEFAULT_KEEP_RECENT_OBSERVATIONS, DEFAULT_NUM_CTX, OllamaDecider

decider = OllamaDecider(model="qwen3.5:9b", num_ctx=DEFAULT_NUM_CTX,
                        keep_recent_observations=DEFAULT_KEEP_RECENT_OBSERVATIONS)
```

两个经验参数都定了实测依据（`DEFAULT_OUTPUT_RESERVE_TOKENS=2048`、
`DEFAULT_KEEP_RECENT_OBSERVATIONS=12`）：最坏一次生成实测 `eval_count=780`、Agent 决策最坏 395、
`done_reason` 全是 `stop`（`tools/bench/generation_reserve.py`）；8 动作复合驾驶舱指令下
回放上限 12/20/无上限行为一致，降到 6 且用裸占位会把收尾打成 fail-closed
（`tools/bench/keep_recent_experiment.py`）。

默认 `num_ctx=32768`（9b 原生 256K，但 256K 的 KV cache 约 19.7GB，已逼近单张 L20 的空闲显存）。
实测：20 次观察的完整对话 14987 prompt tokens，默认窗口给到 2.37 倍余量，
压缩后 10440；`num_ctx=8192` 会把 prompt 压在 8097 并丢掉 system prompt。

压缩不是静默的：`last_prompt_estimate` / `last_omitted_observations` /
`compressed_payloads` / `omitted_observation_turns` 四个读数可直接打日志或告警；
`last_generation` 给出最近一次生成的 `done_reason`/`eval_count`（生成预留不足时它是 `length`）。
回执还会按域投影后再回放（`OllamaDecider(result_projector=project_observation_result)`）：
请求回声与领域重块不再喂给模型，`read_state` 保留全部状态；实测 flight/control 类回执
字符数减半，长循环实测 `prompt_eval_count` 10421 → 9169。
真实循环验证（16 次观察 + 模型答 system prompt 里的口令）：省略 4 条、估算 15032/30720、
口令答对。全部实测数字与取舍见 `docs/model-selection.md`「上下文预算」一节。

```python
from lingxi_flight_control import (
    BoundedAgentLoop, ExecutionContext, MemoryFlightSimulator, OllamaDecider,
)

simulator = MemoryFlightSimulator(ExecutionContext("exec-1", "drone-01", 0, True))
loop = BoundedAgentLoop(OllamaDecider(model="qwen3.5:9b"), simulator.tool_registry())
result = loop.run("先向前飞10米，然后原地悬停30秒")
print(result.status, result.checkpoint.observations)
```

真实联调脚本：

```bash
PYTHONPATH=src python3 tools/flight_agent_demo.py "先向前飞10米，然后原地悬停30秒"
```

冷启动加载模型可能超过默认 180 秒超时。生产环境建议设 `keep_alive="30m"` 常驻，
或把 `timeout` 调大；超时错误会明确提示这两条出路。

## 可观测性（遥测）

5 个内存读数已升级成结构化事件 + 可注入 sink（默认 stdout JSON line，字段名稳定）：

```python
from lingxi_flight_control import MemorySink, Telemetry, OllamaDecider, BoundedAgentLoop

telemetry = Telemetry()                                  # 不传 sink 即 stdout，一行一个 JSON
decider = OllamaDecider(model="qwen3.5:9b", telemetry=telemetry)
loop = BoundedAgentLoop(decider, registry, telemetry=telemetry)
```

五类事件：`decide`（每次模型决策的预算与生成读数）、`compressed`（闸门真的压了，
带 `omission_mode`）、`fail_closed`（任何硬失败，带稳定 `reason` 词表）、
`unknown_tool`、`stale_context`。**不注入 `telemetry` 时一个事件都不发**（默认安静）。

硬性保证：sink 抛异常、字段契约不符、甚至字段名撞 `emit` 形参，都只记数不抛出——
`Telemetry.emit` 是位置-only 参数 + 全 try 包裹，`sink_errors`/`schema_errors`/`internal_errors`
可观测。测试会把「sink 必炸」的 run 与 `Telemetry.disabled()` 的 run 逐字段比对。
事件字段、阈值与阈值依据（预算 30720、实测峰值 9169、最坏生成 780、`near_budget` 0.9）
见 `docs/observability.md`。

```bash
PYTHONPATH=src python3 tools/flight_agent_demo.py --cockpit --telemetry
# → stdout 里 grep '"event": "decide"' / '"event": "compressed"' / '"near_budget": true'
```

## 运行

```bash
PYTHONPATH=src python -m unittest discover -s tests -p 'test_*.py' -v
# 等价入口（CI 与本地共用同一条命令）
bash tools/ci/offline_tests.sh
```

测试全部离线：`OllamaDecider` 通过注入的传输层测试，不访问网络；`tests/test_offline_guard.py`
会把 socket 连接换成抛错跑完整套用例，断言零连接尝试。

## 与 lingxi-agent 的边界

`lingxi-agent` 负责业务入口和任务数据；本项目负责意图到工具调用的最小流程。工具实现、权限、幂等和平台适配由调用方注入。
