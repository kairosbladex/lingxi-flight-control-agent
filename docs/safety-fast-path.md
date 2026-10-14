# 安全快路径

急停这类动作不能走模型。这不是性能优化，是安全要求。

## 为什么

两个独立的理由，各自都足够充分：

### 1. 延迟

实测同一句「急停」，同一台 238 服务器，`qwen3.5:9b`：

| 路径 | 墙钟 | 结果 | 无人机状态 |
|---|---|---|---|
| 走模型（首次） | **17.943s** | final | emergency_stopped |
| 走模型（缓存已热） | **9.048s** | final | emergency_stopped |
| **安全快路径** | **0.001s** | final | emergency_stopped |

走模型要 9 秒才发出急停——因为确认轮和执行轮各要一次 LLM 调用，而每次都要先把
59 个工具的 schema 过一遍。**9 秒对无人机是灾难。** 快路径 1 毫秒，约 17000 倍。

### 2. 可用性

| 模型服务不可达时 | 墙钟 | 结果 | 无人机状态 |
|---|---|---|---|
| 走模型 | 0.001s | **tool_error** | **idle（没有停下）** |
| **安全快路径** | **0.000s** | final | **emergency_stopped** |

**模型服务挂掉时，走模型的链路连急停都发不出去，无人机还在飞。**
安全动作不能依赖一个可能不可用的外部服务。

## 设计：只做精确匹配

`SafetyFastPath` 把输入归一化（去标点、去空白、转小写）后做**精确比对**，
命中短语表才返回工具调用，否则返回 `None` 交回模型。

**绝不使用子串包含。** 中文否定句会让子串匹配出人命：

| 输入 | 子串匹配（旧实现） | 精确匹配（现在） |
|---|---|---|
| 不要返航 | ⚠️ `return_home` | 交回模型 |
| 别降落 | ⚠️ `land` | 交回模型 |
| 能不能返航 | ⚠️ `return_home` | 交回模型 |
| 先别返航 | ⚠️ `return_home` | 交回模型 |
| 取消返航 | `cancel_return_home`（靠顺序侥幸） | `cancel_return_home`（短语表） |

实测旧 `parse_flight_control_text` 在 16 条否定/疑问说法里**误触发 9 条**。
已修复：它现在先走同一份精确短语表，再对否定/犹疑措辞一律拒绝。修复后误触发 0 条。

## 短语表

定义在 `safety.DEFAULT_SAFETY_PHRASES`，是**单一真源**，`SafetyFastPath`、
`SafetyFastPathDecider` 和 `parse_flight_control_text` 共用：

| 动作 | 例句 |
|---|---|
| `emergency_stop` | 急停、紧急停止、马上停止、停止所有动作、刹停 |
| `return_home` | 返航、一键返航、自动返航、立即返航 |
| `cancel_return_home` | 取消返航、停止返航、终止返航 |
| `land` | 降落、原地降落、立即降落 |

**起飞刻意不在表里**（记录在 `safety.EXCLUDED_ACTIONS`）：快路径只能发空参数，而起飞需要
`altitude_m`；替用户猜一个起飞高度是危险的，且起飞是深思熟虑的规划动作、不是时间敏感的安全动作。
它交给模型处理，常规解析仍会给出带默认高度的合法请求。

同一说法映射到两个动作会在构造时直接报错——不允许靠顺序碰运气。

## 属性测试守住的不变量

`safety.py` 是绕过模型的承重件，而短语表是**鼓励按业务扩充**的。`tests/test_safety_properties.py`
对**表中每一条短语**自动生成变体来验证，新增短语会自动被覆盖（不用 hypothesis：项目零依赖，
且中文否定构式可枚举，穷举覆盖更实）。

| 类别 | 不变量 |
|---|---|
| 覆盖 | 每条短语（含标点/空白/大小写变体）必须被识别为正确动作 |
| **否定安全** | 每条短语的否定、犹疑、疑问、转述变体一律**不得**被识别（>20 变体/短语） |
| 精确性 | 任何多余材料都破坏匹配；嵌入更长句子中不得命中 |
| 结构性 | 归一化幂等、无同说法映射两动作、动作名真实存在且在驾驶舱工具面内 |
| 交叉一致 | 不与其它动作的文本标记冲突；`parse_flight_control_text` 与快路径结论一致且请求合法 |
| 模糊 | 2 万条随机文本 + 逐字变异：识别成功当且仅当归一化后正好是表里的短语 |

### 这套测试抓到的两个真实缺陷

1. **`parse_flight_control_text` 的否定误触发远不止「不」。** 原先的子串包含 + 手写否定词表
   漏了「勿 / 禁止 / 免 / 莫」等：实测 `请勿急停` → **emergency_stop**、`禁止返航` → **return_home**、
   `勿返航` → **return_home**、`免降落` → **land**。否定说法无法穷举，维护词表是打地鼠。
   已改成**精确匹配**（键归一化），结构上不可能命中；`_NEGATION_MARKERS` 整个删掉。
2. **`takeoff` 差点留在快路径里。** 快路径发空参数，而 `takeoff` 需要 `altitude_m`；
   `parse_flight_control_text("起飞")` 会返回一个**延迟爆炸**的坏请求（直到 `to_dict()`/`validate()`
   才抛错）。已移出快路径，并加了「快路径认领的动作必须真的不需要参数」这条属性。

## 用法

```python
from lingxi_flight_control import (
    BoundedAgentLoop, MemoryFlightControlSimulator, OllamaDecider,
    RuntimeBinding, SafetyFastPathDecider, build_full_control_tool_registry,
)

simulator = MemoryFlightControlSimulator(
    RuntimeBinding("actor-1", "project-1", "hangar-1", "drone-01", "batch-1", 1))
decider = SafetyFastPathDecider(OllamaDecider(model="qwen3.5:9b"))   # 安全动作短路
loop = BoundedAgentLoop(decider, build_full_control_tool_registry(simulator), max_steps=8)
```

安全动作命中时**完全不发模型调用**，执行完也不再回问模型，直接给确定性收尾语，
因此整条链路与模型服务解耦。

## 配套修复：急停不再要求控制权

原实现要求所有动作先持有控制权，**急停也被 `CONTROL_NOT_OWNED` 拒绝**——
而急停恰恰是「控制权不在自己手上」时最需要它。现已把 `emergency_stop` 加入
`_CONTROL_FREE_ACTIONS`（连同 `CONTROL_ACQUIRE` / `REMOTE_*`）。

只豁免急停：它**只停止运动**，不会引入新的风险。`return_home` / `land` 会驱动自主飞行，
与他人操控可能冲突，因此仍要求持有控制权。

## 已知边界

- **覆盖面是刻意收窄的。** 短语表只收无歧义短指令。任何不确定的说法都交回模型，
  宁可慢也不能错。表可以按业务扩充，但每加一条都要问：这个说法的否定形式会不会误命中？
- **确认仍然是两阶段。** 循环要求先建立确认绑定再执行，所以安全动作需要两次本地
  `run()` 调用（0 次模型调用，总耗时毫秒级）。操作员自己下达的指令即是授权，
  调用方第二次传 `confirmed=True` 即可。
- **动作必须在工具面内。** 若注册表里没有该动作（例如只挂了 `relative_move`/`hover` 的
  最小执行器），快路径会交回模型兜底，而不是凭空返回一个未注册的调用。

## 复现

```bash
python3 tools/bench/safety_latency.py qwen3.5:9b
PYTHONPATH=src python3 tools/flight_agent_demo.py --cockpit "急停"
```
