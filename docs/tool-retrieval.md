# 工具检索层（工作项 5）

## 交付内容

| 能力 | 位置 |
| --- | --- |
| 确定性规则检索（领域词表 + 名称/描述关键词） | `src/lingxi_flight_control/tool_selector.py` |
| `OllamaDecider(tool_selector=...)` 可注入 | `src/lingxi_flight_control/ollama_decider.py` |
| 检索前后准确率与 token 对比入口 | `tools/bench/tool_selection.py --selector` |
| 真实驾驶舱 e2e 打开检索 | `tools/flight_agent_demo.py --cockpit --selector` |

```python
from lingxi_flight_control import OllamaDecider, RuleBasedToolSelector

decider = OllamaDecider(model="qwen3.5:9b", tool_selector=RuleBasedToolSelector())
# 读数：decider.last_tool_selection ∈ {"full", "retrieved"}
#       decider.last_selected_tools（本次下发的工具数）
#       decider.selector_errors（检索层异常被吞掉、退化成全量的次数）
```

## 为什么方向是不对称的

多下发一个工具只是多花 token；**少下发一个工具会让模型结构上无法完成任务**——它不可能调用
一个没在下发的工具。所以本层所有不确定的分支都倒向「全量」：

1. **关键词一个领域都没命中就回退全量**（空消息、含糊消息、「继续」「好的」这类接话）。
2. **安全底线**：`read_state`（唯一只读工具）与 `emergency_stop` / `return_home` / `land`
   在任何消息下都保留。检索层不允许减少安全能力。
3. **前置动作**：驾驶舱状态机要求先持有控制权（`_CONTROL_FREE_ACTIONS` 之外的动作都会被拒），
   所以任何改变状态的动作进入候选时，必须把 `control_acquire` 一起带上。
   少了它，模型能看见动作却无法合法执行——多步任务会直接断在第一步。
4. **已经用过的工具永远保留**：已执行（`executed_tool_calls`）、待确认（`tool_calls`）、
   已回放的观察（`observations`）里的工具名都并入候选。续跑不会因为消息里没再提它就丢工具。
5. **候选面下限** `min_candidates=8`：低于它就回退全量。命中的领域太少说明只碰到了弱关键词，
   而过窄的工具面不只是「少给」，还会**诱导模型去调用不匹配的动作**（实测见下文
   「投放物资」一例：候选里没有它想调的载荷工具时，模型退而调了 `control_acquire`）。
6. **检索层自己出异常**：`OllamaDecider` 退化成全量下发并只记 `selector_errors`，
   绝不让工具变少，也绝不让 run 失败。

关键词匹配用**子串**是刻意的：这里的误判只会多给工具（安全方向），
与安全快路径必须精确匹配的理由正好相反（那边子串匹配会把「不要返航」读成返航）。

## 候选面统计（确定性，不需要模型）

`tools/bench/tool_selection.py` 的 46 条用例，全量 59 个工具：

| 指标 | 数值 |
| --- | --- |
| 平均候选工具数 | **18.9 / 59**（32%） |
| 候选工具数范围 | 10 ~ 59 |
| 回退全量的用例数 | 1（「读取当前状态」：只命中 state 领域，候选面低于 `min_candidates=8`） |
| 期望工具缺失的用例数 | **0**（46/46 的期望工具都在候选里） |

最后一行是确定性护栏 `tests/test_tool_selector.py::BenchCoverageTests`：不需要模型就能发现
「检索层漏掉了某条用例的期望工具」，不至于等花钱跑完 46 次模型调用才知道准确率掉了。
它同时断言基准用例数仍是 46——数字一变，准确率对比就不可比了。

## 实测：准确率与 prompt 开销（检索前 / 检索后）

```bash
PYTHONPATH=src python3 tools/bench/tool_selection.py qwen3.5:9b              # 全量 59 工具
PYTHONPATH=src python3 tools/bench/tool_selection.py qwen3.5:9b --selector   # 按意图检索
```

两条命令的 46 条用例、工具描述与 system prompt 完全一致，唯一变量是「是否检索」。
实测（`qwen3.5:9b`，Ollama 0.21.0，`temperature=0`，`keep_alive="30m"`，2026-10-15）：

| 指标 | 检索前（全量） | 检索后（子集） | 变化 |
| --- | --- | --- | --- |
| 准确率 | **43/46** | **43/46** | 持平（验收要求两边都 ≥43/46） |
| 平均候选工具数 | 59.0 | **18.9** | −68% |
| 平均工具段估算 tokens（保守口径） | 5531.0 | **1729.9** | −68.7% |
| 平均 `prompt_eval_count`（服务端真实值） | **3235.0** | **1166.0** | **−64.0%** |
| 中位 `prompt_eval_count` | 3234.0 | 1323.5 | −59.1% |
| 46 次调用总耗时 | 495.8s | 284.3s | −42.7% |

准确率持平、开销大幅下降。**错例并不相同**，如实记录：

| 用例 | 期望 | 检索前实得 | 检索后实得 |
| --- | --- | --- | --- |
| 从断点继续 | `continue_from_breakpoint` | `read_state`（错） | 正确 |
| 绕点盘旋半径200米 | `point_orbit` | `read_state`（错） | `null`（错） |
| 上传航线 | `upload_route` | `null`（错） | `null`（错） |
| 投放物资 | `payload_dropper` | 正确 | `control_acquire`（错） |

「投放物资」是本层设计里最值得注意的一例：`payload_dropper` **在候选集里**（覆盖率测试
保证了这一点），但候选面变小后模型改调了 `control_acquire`。这正是不把工具面压得过窄
（`min_candidates=8`）的理由，也是为什么 46 条用例的平均候选数是 18.9 而不是 5。

`prompt_eval_count` 的下降（3235 → 1166）大于「工具段估算」的下降（5531 → 1730）看起来
矛盾，原因是两者口径不同：估算按保守字符口径给全量工具面算出的 5531 比服务端真实值高约
1.7 倍（`docs/model-selection.md` 记录了同一口径的偏高幅度），而 `prompt_eval_count` 是
服务端真实计数，还额外包含 system prompt 与消息模板的固定开销。

## 真实驾驶舱 e2e

```bash
PYTHONPATH=src python3 tools/flight_agent_demo.py --cockpit --selector
```

四口令全部 `final`，动作序列仍是
`['control_acquire','move_forward','camera_photo']`；逐步打印新增一行
`[工具检索] retrieved 候选 N 个工具`。这条 e2e 验证的是前置动作规则真的有效：
「接管控制权，然后向前飞10米，再拍张照片」里 `control_acquire`/`move_forward`/`camera_photo`
三个工具必须同时在候选里，模型才能把多步任务走完。

## 不动的东西

- **安全快路径完全不经过检索层**：急停/返航/降落/取消返航在 `SafetyFastPathDecider` 里就被
  短路了，根本到不了 `OllamaDecider`。`tests/test_tool_selector.py` 用「一调就炸」的 selector
  证明 `急停` 命中时 selector 调用次数为 0。
- **`ToolRegistry` 的校验与 fail-closed 一个字没动**：检索只决定「下发哪些工具」，
  白名单、schema 校验、确认绑定、未知工具硬失败全部照旧。测试里覆盖了
  「带检索时未知工具仍然硬失败」与「registry 校验结果不变」。
- **上下文预算口径没变**：预算、`tools` 段与 `prompt_eval` 看到的是同一个（缩小后的）工具面。
  `payload()` 里检索先于压缩生效，`last_prompt_estimate` 因此是真实下发面的估算。

## 未做与风险

- **词表是手写的，不是学出来的**。领域词表覆盖不了的说法会走「未命中 → 全量」，
  代价是那次请求没有节省；不会漏工具。新增动作时需要同步维护词表（有测试兜底：
  关键词命中不了任何领域时行为是「全量」而不是「空集」）。
- **没有做语义相似度检索**（embedding/BM25）。理由是目标是确定性与可测试性：规则检索
  不依赖模型，也不依赖 tokenizer，输入相同结果必然相同。
- **没有按 tokens 动态定候选规模**：当前是「领域命中 + 兜底」，不做 top-k 截断——
  截断会引入「k 取小了就丢能力」的风险，与第一原则冲突。
