# 真实执行器适配层（工作项 1）

## 这次交付的到底是什么（先说结论，别误读）

**本次交付的是「可注入执行器挂点 + 报文夹具（替代验收）」，不是「已接真实平台」。**

本仓库当前环境**没有真机、没有 MQTT/串口凭据、没有真实平台报文样本**（见「环境确认」一节）。
因此：

| 能力 | 状态 | 证据 |
| --- | --- | --- |
| `RuntimeExecutor` 协议（预览/确认两阶段 + 状态读取） | 已交付 | `src/lingxi_flight_control/executor.py` |
| `SimulatorExecutor`（包住现有模拟器，零行为变化） | 已交付 | `tests/test_executor.py::SimulatorExecutorEquivalenceTests` |
| 任意执行器注入 `build_full_control_tool_registry` | 已交付 | `tests/test_executor.py::ExecutionModePassthroughTests` |
| `execution_mode` 由执行器提供（不再写死 `simulator`） | 已交付 | 同上 + `test_full_control.py` 既有断言不变 |
| 回环执行器（本机闭环，证明挂点真的通了） | 已交付 | `LoopbackExecutor`，`--cockpit --executor loopback` |
| 报文夹具回放（缺录制即拒绝，绝不合成成功） | 已交付 | `FixtureExecutor` + `fixtures/cockpit_basic.json` |
| **接入真实平台** | **未交付** | 无凭据；真机报文夹具待现场录制 |

**替代验收声明**：`fixtures/cockpit_basic.json` 的 `source` 字段是 `simulator-export`，
即由模拟器回执导出的报文形状，**不是**真机/MQTT 现场录制。测试
`test_shipped_fixture_is_labelled_a_simulator_export` 会断言这一点，任何人把夹具
冒充成真机录制都会红。夹具里没有任何硬编码成功：`FixtureExecutor` 只回放文件内容，
文件里没有的请求一律 `FIXTURE_NOT_FOUND` 拒绝。

## 环境确认

开工前已确认（本会话）：`http://192.168.130.238:11434` 只有模型服务（Ollama 0.21.0，
`qwen3.5:9b`），**没有**无人机/飞控平台端点、MQTT broker 或串口设备，也没有任何凭据。
按目标的降级方案处理，并在此明确标注为替代验收。

## 协议

```python
@runtime_checkable
class RuntimeExecutor(Protocol):
    execution_mode: str                                            # 对外声明的执行模式
    def runtime_context(self) -> ExecutionContext: ...             # 运行时绑定
    def preview(self, request) -> FlightControlReceipt: ...        # 预览：不改状态
    def confirm(self, confirmation_id, *, command_id, idempotency_key,
                confirmation_binding, context_version) -> FlightControlReceipt: ...
    def read_state(self) -> Mapping[str, Any]: ...                 # 状态读取
```

协议里**没有**平台细节（连接、重试、编解码）。真实平台适配器只要实现这五个成员，
就能原样塞进 `build_full_control_tool_registry`，59 个工具的动作名、参数契约与回执字段
一个都不用动。

```python
from lingxi_flight_control import MemoryFlightControlSimulator, RuntimeBinding, build_full_control_tool_registry

binding = RuntimeBinding("actor-1", "project-1", "hangar-1", "drone-1", "batch-1", 1)
registry = build_full_control_tool_registry(MemoryFlightControlSimulator(binding))  # 裸模拟器仍然可用
```

## execution_mode 从哪来

以前 `execution_mode` 在 6 处写死 `"simulator"`，现在统一由执行器提供，且**状态、回执、
驾驶舱契约三处必须一致**：

| 位置 | 现在取值 |
| --- | --- |
| 回执 `FlightControlReceipt.to_dict()["execution_mode"]` | `state.execution_mode` |
| 状态 `FlightControlState.to_dict()["execution_mode"]` | `self.execution_mode` |
| `MemoryFlightControlSimulator.snapshot()["execution_mode"]` | `state.execution_mode` |
| `action_tool_definitions(execution_mode=...)` / `action_manifest(execution_mode=...)` | 调用方传入，默认仍是 `"simulator"` |

换绑（`bind`）会重置状态，但**不会**把执行模式掉回 `simulator`：
`test_rebinding_the_runtime_keeps_the_executor_mode` 覆盖了这一点。

### 为什么模式名要过安全边界检查

`execution_mode` 会随**每一个**回执回给模型，而 `BoundedAgentLoop` 对工具输出跑
`contains_unsafe_content`。如果模式名里带 `mqtt` / `credential` / `凭据` 这类禁用词，
**每一个动作**都会变成 `TOOL_ERROR`。这不是小事，所以 `validate_execution_mode()` 在
构建执行器时就拿同一张禁用词表把它们拦下来：

```python
validate_execution_mode("loopback")     # ok
validate_execution_mode("recorded")     # ok
validate_execution_mode("mqtt-live")    # ValueError：会在每个动作上 fail-closed
```

## 夹具格式

```json
{
  "schema_version": 1,
  "source": "simulator-export",          // 或 platform-recording（真机现场录制）
  "execution_mode": "recorded",
  "runtime": {"actor_id": "...", "project_id": "...", "hangar_id": "...",
              "drone_id": "...", "task_batch_id": "...", "context_version": 1},
  "recordings": [
    {"action": "control_acquire", "parameters": {}, "status": "accepted",
     "error_code": null, "message": "模拟动作已执行", "state": { ...完整状态快照... }}
  ]
}
```

夹具校验（`load_fixture` / `validate_fixture`）会拒绝：schema 版本不符、`source` 非白名单、
模式名含禁用词、runtime 字段缺/多或绑定非法、动作名或状态名未知、`state` 里有未知字段、
同一 `(动作, 参数)` 重复录制、以及**录制状态里的 `execution_mode` 与夹具声明不一致**。

参数匹配用 `canonical_parameters()`：整数与浮点归一（`10` 与 `10.0` 命中同一条录制），
键排序，保证确定性。

回放语义（`FixtureExecutor`）：

1. **两阶段握手不重写**——上下文版本、幂等键、确认绑定、TTL、控制权全部委托给内部
   `MemoryFlightControlSimulator`。那套语义是单一真源，复制一份就会漂移。
2. **预览时要求存在录制**：没有就撤掉待确认卡，返回 `REJECTED` + `FIXTURE_NOT_FOUND`，
   于是 `confirm` 结构上不可能执行到没有依据的动作。
3. **状态与回执以录制为准**：`accepted` 用录制快照覆盖内层状态；非 `accepted` 回滚内层
   状态，绝不把拒绝升级成成功。
4. 录制状态**深拷贝**后再采用：否则回放一个动作会就地改掉夹具里另一个动作的录制内容
   （`test_replay_never_mutates_the_fixture_payload` 覆盖了这个回归）。

## 复现命令

```bash
# 全量离线测试（含 18 条执行器用例）
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'

# 重新导出夹具（会覆盖 fixtures/cockpit_basic.json）
PYTHONPATH=src python3 tools/record_fixture.py fixtures/cockpit_basic.json

# 真实模型 e2e：默认模拟器执行器
PYTHONPATH=src python3 tools/flight_agent_demo.py --cockpit

# 真实模型 e2e：注入回环执行器（同一份 registry，只换后端）
PYTHONPATH=src python3 tools/flight_agent_demo.py --cockpit --executor loopback
```

## 未做的事与下一步

- 2026-10-16 已新增默认关闭的 `KtdPlatformExecutor`，经 `ktd_backend` 既有 HTTP 接口编译
  15项明确写动作。权威状态、控制权租约和逐机型能力仍缺在线契约，不能启用真实设备；
  具体限制与源码证据见 [Java适配候选](ktd-platform-contract.md)。不直连厂商 MQTT/串口。
- 真机报文夹具未录制。现场录制后，把 `source` 改成 `platform-recording`、
  `execution_mode` 改成真实模式名（注意上文禁用词约束），并替换 `recordings`。
- 在线健康检查、断链重连、命令超时与重试策略属于执行器实现，尚未定义。
