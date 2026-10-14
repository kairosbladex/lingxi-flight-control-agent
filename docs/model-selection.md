# 飞控 Agent 大脑选型：192.168.130.238

选型结论：**`qwen3.5:9b`**。以下是全部实测依据，可重新验证。

## 硬件与服务

- 主机 `AI-lingxi` / `192.168.130.238`，SSH 别名 `ai-lingxi`
- GPU：2 × NVIDIA L20（46GB）+ 4 × Tesla T4（15GB）
- 推理服务：Ollama **0.21.0**，监听 `0.0.0.0:11434`，本机可直连，无需隧道
- 同机其他服务：`8000` WebODM 航测、`8080` 业务 API —— 与 LLM 无关
- 该机为多人共用（5 个登录会话，load average ≈ 12），选型必须考虑显存占用

## 候选与声明能力

来自 `/api/show` 的 `capabilities`：

| 模型 | 架构 | 总参 | 权重 | 声明能力 | 上下文 |
|---|---|---|---|---|---|
| qwen3.5:4b | qwen35 稠密 | 4.7B | 3.39GB | completion, vision, **tools**, thinking | 256K |
| **qwen3.5:9b** | **qwen35 稠密** | **9.7B** | **6.59GB** | completion, vision, **tools**, thinking | **256K** |
| qwen3-vl:8b-instruct | qwen3vl | 8.8B | 6.14GB | completion, vision, **tools** | 256K |
| qwen3.6:latest | qwen35moe | 36.0B | 23.94GB | completion, vision, **tools**, thinking | 256K |
| qwen3.5:122b | qwen35moe | 125.1B | 81.37GB | completion, vision, **tools**, thinking | 256K |
| qwen2.5:3b | qwen2 | 3.1B | 1.93GB | completion, **tools** | 32K |
| deepseek-r1:8b | qwen3 | 8.2B | 5.23GB | completion, thinking（**无 tools**） | 128K |

`deepseek-r1:8b` 实测调用带 `tools` 的 `/api/chat` 直接返回 **HTTP 400** —— 没有工具模板，
无法作为 Agent 大脑，首先排除。

## 吞吐实测（`tools/bench/throughput.py`）

| 模型 | 解码 tok/s | prefill tok/s | 冷启动 |
|---|---|---|---|
| qwen3.5:4b | 37.8 | 287 | 4.6s |
| qwen3-vl:8b-instruct | 28.1 | 210 | 11.2s |
| **qwen3.5:9b** | **23.5** | **187** | **36.7s** |
| qwen3.6:latest | 8.8 | 46 | 184.5s |
| qwen3.5:122b | 未测（见下） | — | — |

**关键规律：决定解码速度的是权重文件大小，不是活跃参数量。** MoE 的活跃参数只降低计算量，
但每一 token 仍要把全部权重从显存流一遍；一旦模型装不进单卡，就要跨 PCIe 逐层取权重，代价极高。

- `qwen3.5:9b` 6.59GB → 装得进单张 L20（空闲 22.4GB），**23.5 tok/s**
- `qwen3.6:latest` 23.94GB → 必须跨卡，**8.8 tok/s**，prefill 仅 46 tok/s
- `qwen3.5:122b` 81.37GB → 必须横跨全部 6 张卡（含 4 张慢 T4），且会挤掉同机其他模型

按 `qwen3.6` 的 prefill 46 tok/s 推算，一个 1500 token 的 Agent 上下文每步仅预填充就要 **33 秒**，
多步循环下不可用。`122b` 还要在此基础上再差数倍，因此**没有加载**（该机为 5 人共用的生产机，
加载 81GB 会挤占全部显存，收益不抵代价）。

## 决策质量实测（`tools/bench/agent_quality.py`）

9 项检查：多步规划、停止纪律、不伪造执行、歧义澄清、越权拒绝、超限参数、幂等键、口语泛化。

| 模型 | 得分 | 主要失分 |
|---|---|---|
| qwen3.5:4b | 7/9 | 多步规划、歧义澄清 |
| **qwen3.5:9b** | **7/9** | 多步规划、歧义澄清 |
| qwen3-vl:8b-instruct | 6/9 | 额外放行 500 米超限参数 |
| qwen2.5:3b | 4/9 | 超限参数、幂等键为空、多步规划 |

### 两个必须记下的结论

**1. 「多步规划失败」是提示词问题，不是模型能力问题。**
初版 system prompt 下，所有模型在「先向前飞10米，然后原地悬停30秒」上都只做第一步就收尾。
在提示词里显式写清「用户一次要求多个动作时，必须依次全部完成」之后，`qwen3.5:9b` 在
`think=false` 下即完成完整链路：`relative_move → hover → final`。
该纪律已写进 `ollama_decider.DEFAULT_SYSTEM_PROMPT`，不要删。

**2. 安全不能指望模型，确定性护栏才是承重墙。**
实测「向前飞500米」时，模型直接构造了 `distance_m: 500` 的调用，
是 `ToolRegistry` 的 schema 校验（上限 100）把它拦下的，模拟器未移动。
这与 S4/S5/S6 的失分一致：模型会犯错，但 `BoundedAgentLoop` 是 fail-closed 的，
模型失误降级为「拒绝/报错」，而不是「不安全动作」。

## 端到端实测（`tools/flight_agent_demo.py`）

真实模型 + `BoundedAgentLoop` + `MemoryFlightSimulator`：

```
用户: 先向前飞10米，然后原地悬停30秒
  [第1轮思考 3.4s] tool_call: relative_move {"direction":"forward","distance_m":10,...}
  [第2轮思考 3.0s] tool_call: hover {"duration_s":30,...}
  [第3轮思考 3.8s] final: 已完成：1. 向前飞行 10 米 2. 原地悬停 30 秒
最终状态: final    模拟器位置: x=0.0 y=10.0 z=0.0
```

四条用例全部行为正确：单步、多步、超限拒绝、双向移动。开启 `think=true` 亦验证可用
（首轮 16.9s，后续 2.4~3.2s）。

上下文预算改动后复跑完整驾驶舱（`--cockpit`，59 工具 + 预览/确认两阶段，`num_ctx=32768` 默认）：

| 命令 | 结果 | 步骤 | 已执行 |
|---|---|---|---|
| 接管控制权，然后向前飞10米，再拍张照片 | final | 4/24 | control_acquire → move_forward → camera_photo |
| 切到红外镜头并设置测温范围-10到120度 | final | 5/24 | camera_infrared → control_acquire → camera_infrared → infrared_set_limits |
| 读取当前状态 | final | 2/24 | read_state |
| 急停 | final（快路径 0.0s） | 2/24 | emergency_stop |

4/4 通过：模型自主完成「取控制权 → 移动 → 拍照」，并在第一个 `camera_infrared` 回执
`CONTROL_NOT_OWNED` 后自己补上 `control_acquire` 纠正。冷启动期第一次整轮运行有一条命令
卡在 180s 超时上（fail-closed，`步骤数 0/24`、未执行任何动作），模型驻留后复跑通过。

## 上下文预算：默认 `num_ctx = 32768`，超预算先压缩再发送

### 现象：8192 会静默丢掉 system prompt

Ollama 在 prompt 超过 `num_ctx` 时**不报错**，直接丢掉**最前面**的 token——也就是
system prompt 里的硬性约束。把口令标记 `ZEBRA-7391` 写进 system prompt，再发同一条
20 次 `read_state` 观察的对话（42 条消息，每次回执 1386 字符）：

| 场景 | prompt_eval_count | 答出口令 | 说明 |
|---|---|---|---|
| 59 工具 + system prompt + user（无观察） | 3567 | ✅ | 固定头部实测成本 |
| 20 次观察，`num_ctx=8192` | **8097** | ❌ | 被压在上限，system prompt 被丢弃，**无任何报错** |
| 20 次观察，`num_ctx=32768` | **14987** | ✅ | 完整 |

每次观察回放（assistant `tool_calls` + tool 回执）实测 ≈ **571 prompt tokens**
（`(14987-3567)/20`；比按 1386 字符 / 3.7 粗算的 357 高一截——`command_id`/`idempotency_key`
这类十六进制串 tokenize 得很差）。20 次观察本身就占 1.1 万 tokens。所以 `num_ctx=8192`
只留得下约 8 条观察（`(8192-3567)/571 ≈ 8`），**第 9 次工具调用起就开始静默丢弃最前面的内容**
——回执口径越小这个数字越大，但丢的自始至终是 system prompt。

### 修复：`OllamaDecider` 自己管住预算

`src/lingxi_flight_control/ollama_decider.py` 每次发送前用**不依赖 tokenizer 的保守字符估算**
核对预算：

- 估算口径：中日韩字符 1 token/字，其余 2 字符/token，另加每条消息 8 tokens 模板开销；
  tools 契约一并计入——59 个工具实测就占 3213~3525 tokens，漏掉它预算形同虚设。
  保守度实测：固定头部估 7521 / 实测 3567（+111%），整条 20 次观察对话估 22321 / 实测 14987（+49%）。
- 预算 = `num_ctx - output_reserve`（默认预留 2048 tokens 给生成）。
- 压缩策略（确定性、可测试）：
  1. system prompt 与当前 user 消息**永不丢弃**；
  2. 只回放最近 12 条观察，更早的压成明确占位`（更早的 N 次工具调用已省略）`；
  3. 仍超预算就从最旧的候选观察继续往前丢；连头部都装不下时抛 `PromptBudgetExceeded`，
     **绝不把超长 prompt 发出去让服务端静默截断**。

压缩后的实测（同一条 20 次观察对话，口令标记仍在）：

| 配置 | 回放观察 | 估算 tokens | 预算 | prompt_eval_count | 口令 |
|---|---|---|---|---|---|
| `num_ctx=8192` | — | 7521（仅头部） | 6144 | **拒绝发送**（`PromptBudgetExceeded`） | — |
| `num_ctx=16384` | 9/20 | 14204 | 14336 | 8728 | ✅ |
| `num_ctx=32768`（默认） | 12/20 | 16424 | 30720 | **10440** | ✅ |

`num_ctx=8192` 被拒绝是刻意的：59 个工具的契约 + system prompt 的保守估算已超出它的预算，
这个窗口**本来就装不下**；明确报错（并提示调大 `num_ctx`）比静默丢掉硬性约束安全。


### 生成预留：`output_reserve = 2048` 的实测依据

`prompt + 生成` 要一起装进 `num_ctx`，所以预算必须给生成留出 `output_reserve`。留小了不会
报错——Ollama 只是把生成截断，响应里的 `done_reason` 变成 `length`，这一次决策随即不可用；
留大了则白白压低 prompt 预算。这个数只能实测，所以 `OllamaDecider.last_generation` 把
`done_reason` / `eval_count` / `prompt_eval_count` 暴露成一等读数。

`tools/bench/generation_reserve.py` 用**生产同一条 `payload()` 路径**（同一 system prompt、
同一套 59 工具、同一 `num_ctx`、同一预算闸门与投影）发 5 条代表性请求：

| 场景 | prompt 估算 | 实测 prompt_eval_count | 实测 eval_count | done_reason |
|---|---|---|---|---|
| 单动作（接管控制权） | 7477 | 3532 | 33 | stop |
| 8 动作复合 | 7530 | 3577 | 198 | stop |
| 状态汇报（读取当前状态） | 7478 | 3533 | 37 | stop |
| 歧义澄清（做测绘任务） | 7477 | 3533 | 226 | stop |
| 长规划思考 | 7532 | 3566 | **395** | stop |

真实循环里的最坏值比这更小：`--cockpit` 四命令最坏 **334**（"读取当前状态"那条 14 字段答复），
8 动作复合循环最坏 328，16 观察长循环 59~187，**全部 `done_reason=stop`**。为了摸清「思考」
长度的尾巴，另外单独测了一次无工具闲聊（同一模型、同一 `num_ctx=32768`、`temperature=0`）：

```bash
curl -s http://192.168.130.238:11434/api/chat -H 'Content-Type: application/json' \
  -d '{"model":"qwen3.5:9b","messages":[{"role":"user","content":"你好"}],"stream":false,"options":{"num_ctx":32768,"temperature":0},"keep_alive":"30m"}' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["done_reason"], d["eval_count"])'
# -> stop 780
```

结论：**最坏一次生成实测 780（无工具闲聊），Agent 决策最坏 395；`done_reason` 全是 `stop`，
`length` 一次都没出现。** 2048 对最坏生成是 2.6 倍余量、对最坏 Agent 决策是 5.2 倍，
占用率 ≤38%，因此**维持 2048**：

- 不调小（例如 1024）：对同一句 `你好` 就只有 1.3 倍余量，而 9b 的思考长度波动很大，
  一旦截断就是「整次决策不可用」，赌不起；
- 不调大：只会白压 prompt 预算（预算 = `num_ctx - output_reserve`），而实测离上限还远。

触发调整的条件写在这里：一旦 `decider.last_generation["done_reason"] == "length"`，
或 `eval_count` 逼近 2048，就按实测值调大 `output_reserve`（预算随之下降）。

### 压缩是可观测的

压缩不能是静默的——否则线上分不清「闸门在工作」和「根本没触发」。`OllamaDecider` 在
`payload()`（唯一构建待发送载荷的地方）记录读数：

| 读数 | 含义 |
|---|---|
| `last_prompt_estimate` | 最近一次待发送 prompt 的估算 tokens（还没构建过为 `None`） |
| `last_omitted_observations` | 最近一次省略的观察条数 |
| `compressed_payloads` | 累计发生压缩的载荷次数（含空响应重试） |
| `omitted_observation_turns` | 累计省略的观察条数 |
| `last_generation` | 最近一次响应的 `done_reason`/`eval_count`/`prompt_eval_count`（生成预留的依据） |

```python
decider = OllamaDecider(model="qwen3.5:9b")
BoundedAgentLoop(decider, registry).run("读取当前状态")
print(decider.last_prompt_estimate, decider.last_omitted_observations,
      decider.compressed_payloads, decider.omitted_observation_turns,
      decider.last_generation)
```

换 `SafetyFastPathDecider` 包装时读数只在真的走了模型的那一步前进（快路径短路不计数），
`tools/flight_agent_demo.py --cockpit` 会逐步打印 `[prompt预算]`、`[生成]` 与收尾的累计行。

### 真实长循环验证（`tools/bench/long_loop_budget.py`）

合成对话只能证明闸门逻辑，这里让**真实 `BoundedAgentLoop`** 先由脚本驱动 16 次
`read_state`（每步都是一条真实回执），第 17 步交给真实模型，问的正是 system prompt 里的口令：

| 指标 | 实测 |
|---|---|
| 循环结果 | `final`，16 条观察，真实模型只被调用 1 次 |
| 答复 | `ZEBRA-7391`——**口令正确，system prompt 没被丢掉** |
| 省略观察 | 4（16 − 回放上限 12） |
| 估算 tokens（三档） | 不压不投影 19361 → 只压缩 16424 → **压缩+投影 15008** |
| 实测 `prompt_eval_count` | **9156**（只压缩 10421；不压不投影是 34 条消息） |
| 累计读数 | `compressed_payloads=1`、`omitted_observation_turns=4` |

开投影后同一条驾驶舱 e2e 逐步读数也随之下降（第 3 步 9139 → 8369，
第 4 步 9961 → 8878），而「读取当前状态」的答复仍然完整报出 14 个字段——
`state` 域刻意保留了全部状态块。

窗口收紧时闸门继续往前丢观察，头部始终保留（离线计算，不发请求）：

| `num_ctx` | 回放观察 | 估算 tokens | 预算 |
|---|---|---|---|
| 32768（默认，仅策略上限） | 12/16 | 16424 | 30720 |
| 16384 | 9/16 | 14204 | 14336 |
| 12288 | 3/16 | 9764 | 10240 |

### 默认值取舍：为什么是 32768

KV cache 实测（`/api/ps` 的 `size_vram`；同一模型换 `num_ctx` 会重新加载）：

| `num_ctx` | 驻留显存 | 相对 8192 |
|---|---|---|
| 8192 | 8.97 GB | — |
| 32768 | 10.01 GB | +1.04 GB（+11.6%） |

差分给出有效边际成本 ≈ 41 KiB/token（KV cache 与随窗口增长的 compute buffer 都线性于 `num_ctx`）。
据此外推：65536 ≈ 11.4 GB，131072 ≈ 14.2 GB，原生 262144 ≈ 19.7 GB——已逼近单张 L20 的空闲显存
（22.4 GB），而这台机器是 5 人共用的生产机，「256K 上下文」实际开不起来。

选 **32768** 的理由：实测 14987 tokens 的完整对话装得下（2.37 倍余量），默认策略进一步压到
10440；KV cache 只比 8192 多 1.04 GB，是同机 5 人共用付得起的代价；只有原生上限的 1/8，
不为用不到的深度白占显存。不选 16384：压缩后虽能用（8728），但留给大回执和长用户消息的余量太小；
不选 65536 以上：prompt 已被 `keep_recent_observations=12` 封顶在 ~10.4K，更大的窗口只是白占 KV cache。

### 回执投影：从源头压低每次观察

预算闸门保证「不截断」，投影再从源头把每次观察本身变小。`FlightControlReceipt` 是驾驶舱 UI
的对外契约（`test_full_control.py` 逐字段断言），所以投影做在「观察 → 模型 prompt」的回放层：
检查点里仍保存完整回执，模型看到的是 `project_observation_result()` 的投影，按
`OllamaDecider(result_projector=project_observation_result)` 注入。

只丢**确定**没用的东西：

- 请求回声与循环内部字段（`command_id`/`idempotency_key`/`confirmation_*`/`expires_at`/
  `last_command_id`）——确认握手由 `BoundedAgentLoop` 自己持有，模型从不经手；
- `parameters`/`camera`/`infrared`/`payloads`/`virtual_buttons` 五个重块，只在对应域出现；
- 连续遥测（控制权/高度/经纬度/电量/任务与航线状态……）任何域都保留；
- `read_state`（域 `state`）保留全部状态块——它的唯一目的就是「把状态说全」；
- 不认识的动作/形状**原样返回**，错误回执也永远原样回放。

实测（59 工具栏，单条回执字符数）：

| 动作（域） | 原回执 | 投影后 | 比例 |
|---|---|---|---|
| `read_state`（state） | 1386 | 1150 | 83% |
| `move_forward`（flight） | 1406 | 746 | **53%** |
| `camera_photo`（camera） | 1406 | 901 | 64% |
| `infrared_set_limits`（infrared） | 1413 | 845 | 60% |
| `set_move_speed`（parameters） | 1408 | 886 | 63% |
| `payload_searchlight`（payload） | 1413 | 769 | 54% |
| `control_acquire`（control） | 1526 | 767 | **50%** |
| `route_pause`（route） | 1520 | 738 | 49% |

20 次 `read_state` 观察的回放估算 16621 → 15025 tokens（`read_state` 是最不利于投影的一档，
因为它刻意保留全部状态块；真实任务里 flight/camera 域的动作占多数，收益接近对半）。

**没有做的更狠一档**：连 `state` 也换成「摘要状态」（控制权/高度/经纬度/电量/任务与航线 +
相机 3 字段 + 红外 3 字段）可压到 435 字符（31%），代价是模型回答「读取当前状态」时只能
报告这些字段——`read_state` 的语义就是「说全」，所以留作可选。

## 运维注意

- **`num_ctx` 默认 32768，且必须显式已知。** 不知道窗口大小就管不住预算，因此
  `OllamaDecider(num_ctx=None)` 会被拒绝；窗口小于「59 工具 + system prompt」的保守估算
  （59 工具配置下阈值约 `num_ctx=9600`，`num_ctx=8192` 必然命中）会抛 `PromptBudgetExceeded`，
  而不是静默丢掉 system prompt。压缩策略见上一节。
- **`output_reserve=2048` 有实测台账，改之前先看数据。** 最坏一次生成 780（无工具闲聊）、
  Agent 决策最坏 395，`done_reason` 全是 `stop`；触发调大的信号是 `last_generation` 里
  出现 `done_reason="length"` 或 `eval_count` 逼近 2048。
- **不要显式传 `think: false`。** 对 `qwen3.5:9b` + Ollama 0.21.0，显式关闭思考会让
  **工具调用完全失效**——模型返回空响应（`content=""`、无 `tool_calls`、`done_reason=stop`）。
  省略该字段时思考照常开启（实测 110 字思考）、工具调用正常且略快：

  | 载荷 | 工具调用 | 墙钟 |
  |---|---|---|
  | 省略 `think` | ✅ 正常（含 110 字思考） | 4.9~5.2s |
  | `think: false` | ❌ 空响应 | — |
  | `think: true` | ✅ 正常 | 5.7~5.9s |

  `OllamaDecider.think` 默认即 `None`（不发送）。空响应还会触发一次有界重试；
  重试只针对「决策」，此时没有任何工具被执行，因此不会重复副作用。
- **冷启动会超时。** 9b 冷加载 36.7s；若需先换出已驻留的 36B，实测超过 180s 默认超时。
  生产建议 `keep_alive="30m"` 常驻，或调大 `timeout`；超时错误信息会直接提示这两条出路。
- **延迟有波动。** 同机其他人加载模型时，同样请求从 3.4s 涨到 21.3s。延迟敏感的飞控链路
  应独占该模型或另置实例。
- 该机是共用资源，批量评测请串行执行，避免同时拉起多个大模型。

## 面向功能迭代：为什么应该继续用 9b，而不是换更大的

问题不在于「更大是否更聪明」——更大确实更聪明。问题在于 **Agent 循环的成本结构与
「加功能」的方向正好相反**。

### 1. 工具目录越大，大模型的劣势越被放大

`tools/bench/tool_selection.py`（59 个真实驾驶舱动作，46 条用例）实测：

| 模型 | 59 工具选择准确率 |
|---|---|
| **qwen3.5:9b** | **43/46（93.5%）** |

3 个错例中有 2 个是指令本身缺少必填参数（"上传航线"缺 `route_id`/`waypoints`，
"绕点盘旋半径200米"缺经纬度），属于无法调用而非选错工具。真正的选择失误只有 1 例。

**9b 在完整的 59 工具面上已经不是瓶颈。** 提升空间不在模型，在工具描述与检索方式。

### 2. 每加一个工具，每一步都要多付一次预填充

`tools/bench/prefill_cost.py` 实测：

| 模型 | 2 工具 | 59 工具 |
|---|---|---|
| prompt tokens | 307 | **3213** |
| qwen3.5:9b prefill | 0.5s（560.9 tok/s） | **4.4s（728.6 tok/s）** |
| qwen3.6:latest prefill | 2.8s（110.0 tok/s） | **16.4s（195.6 tok/s）** |
| qwen3.5:9b decode | 25.3 tok/s | 26.0 tok/s |
| qwen3.6:latest decode | 8.0 tok/s | 9.4 tok/s |
| **同样请求总墙钟** | — | **9b 6.2s vs 36B 20.4s（3.3 倍）** |

换算下来 **每增加 1 个工具 ≈ 51 prompt tokens**：

| 工具数 | prompt tokens | 9b 预填充 | 36B 预填充 |
|---|---|---|---|
| 59 | 3213 | 4.4s | 16.4s |
| 100 | 5303 | 7.3s | 27.1s |
| 200 | 10402 | 14.3s | 53.2s |
| 400 | 20598 | 28.3s | 105.3s |

**关键点：Agent 循环每一步都要重算整个上下文，预填充是 O(上下文)。**
观察结果还会不断累积，所以每步成本随迭代单调上涨。
大模型 3.7 倍慢的预填充意味着：**功能加得越多，换大模型的代价越大，而不是越小。**

### 2b. 但这笔预填充成本被 KV cache 吃掉了（推翻了分组下发的必要性）

`tools/bench/cache_probe.py` 实测：工具目录作为稳定前缀会被缓存。

| 步骤 | prompt tokens | prefill | 墙钟 |
|---|---|---|---|
| 首次 | 3213 | **4.02s** | 11.83s |
| 完全相同重放 | 3213 | **0.90s** | 2.46s |
| 用户消息变了、工具集不变 | 3212 | **0.92s** | 2.56s |

`tools/bench/loop_cost.py` 在真实 5 步循环里进一步确认 prefill 基本恒定、不随观察累积增长：

```
第1步 prefill=0.98s 墙钟=3.37s -> relative_move
第2步 prefill=0.36s 墙钟=2.72s -> relative_move
第3步 prefill=0.53s 墙钟=2.87s -> relative_move
第4步 prefill=0.68s 墙钟=2.58s -> hover
第5步 prefill=0.76s 墙钟=3.44s -> final
```

**结论：不要做工具分组渐进下发。** 3213 tokens 只在首次真付 4 秒，之后只评估新增的几百 token；
分组下发省下的仅是一次性成本，却要为每步多付一整轮解码（约 1.5~2.5s），稳态下是净亏。
（本节早期版本曾建议分组下发，该结论已被上述测量推翻。）

真正该做的是保证前缀稳定并让模型常驻：`keep_alive="30m"`，避免缓存反复失效后重付首调成本。

### 3. 安全动作不能走 LLM 工具调用

59 工具下一次工具调用，首次预填充 4s、稳态约 0.9s，加解码仍需约 **2.5~5 秒**才能发出"紧急停止"。
对无人机而言这不可接受。而项目里已有的 `parse_flight_control_text` 正则是**微秒级**。

**结论：正则快路径不是待清理的历史包袱，而是必须保留的安全通道。**
LLM 负责意图理解与规划，安全关键动作走确定性短路。

## 最终建议

**控制回路继续用 `qwen3.5:9b`。** 理由：59 工具选择准确率已有 93.5% 余量、
预填充被 KV cache 摊平后每步稳定在 3 秒级、安全动作本就不该走 LLM。

该做的事情按优先级：

1. **驾驶舱动作已全部接入**（`build_full_control_tool_registry`，59 个工具）——
   实测模型能自主完成"取控制权 → 移动 → 拍照"以及"被拒绝后自纠权重试"。
2. **保证模型常驻**（`keep_alive`），让工具目录前缀的缓存一直有效。
3. **安全快路径保留**——`parse_flight_control_text` 继续拦截急停/返航/降落这类动作。
4. **`max_steps` 只计推进任务的步骤**（确认握手不计），实测 5 个动作的复合指令在默认
   `max_steps=8` 下只用 6 步完成。参数校验失败会回喂模型自纠，不再整轮失败。

**什么时候才值得上更大的模型：** 出现 9b 明确扛不住的场景时——长程多航段规划、
视觉推理融进控制回路、或工具数超过 100 且实测选择准确率显著下滑。
即便到那时，也应该作为**独立的慢档**引入，而不是替换控制回路的 9b。

### 36B 的定位与显存前提

`qwen3.6:latest`（23.94GB）只有在**能独占一张完整 L20（46GB）**时才值得重新评估：
整块装进单卡就没有跨卡惩罚，延迟会大幅改善。当前两张 L20 各被
`dinov2_yolo_torch2` 视觉任务占用 16.5GB / 22.5GB，只剩 22.4GB，
36B 必然跨卡，因此实测 9.4 tok/s。用户已指示暂不验证 36B。

`qwen3.5:122b`（81.37GB）需独占两张 L20（92GB）才放得下，且预填充会按上表比例进一步恶化，
不适合控制回路；若将来用于离线规划需先腾出两张 L20。

## 复现

```bash
python3 tools/bench/throughput.py qwen3.5:9b qwen3.6:latest      # 吞吐
python3 tools/bench/agent_quality.py qwen3.5:9b qwen3.5:4b       # 决策质量
python3 tools/bench/tool_selection.py qwen3.5:9b                 # 59 工具选择准确率
python3 tools/bench/prefill_cost.py qwen3.5:9b qwen3.6:latest    # 预填充成本
python3 tools/bench/cache_probe.py qwen3.5:9b                    # KV cache 命中
PYTHONPATH=src python3 tools/bench/loop_cost.py qwen3.5:9b       # 真实循环逐步成本
PYTHONPATH=src python3 tools/bench/context_budget.py qwen3.5:9b  # 上下文预算 / 截断对比
PYTHONPATH=src python3 tools/bench/generation_reserve.py qwen3.5:9b  # 生成预留（done_reason/eval_count）
PYTHONPATH=src python3 tools/bench/long_loop_budget.py qwen3.5:9b # 真实长循环里的压缩验证
PYTHONPATH=src python3 tools/flight_agent_demo.py                # 端到端（最小执行器）
PYTHONPATH=src python3 tools/flight_agent_demo.py --cockpit      # 端到端（59 动作驾驶舱）
```
