# 多 Agent 协同信号量目录 (Signals Directory)

各 Agent 在完成自己的任务后，可在本目录生成标志文件，通知总控大脑。

| 文件 | 归属 Agent | 触发验收 |
| :--- | :--- | :--- |
| `signals/codex.done` | Codex CLI 自己写 | 后端适配器与单测 |
| `signals/deepseek.done` | DeepSeek Harness 自己写 | 几何解算与对抗单测 |
| `signals/claude.done` | Claude CLI 自己写 | 前端组件与 13 场景话术走查 |
| `signals/grok.done` | Grok Build 自己写 | 仿真桩与一键冒烟脚本 |
| `signals/agy.done` | agy 自己写 | 跨仓信封与复合意图评测 |

## 硬规则（2026-10-27 事故后补充）

1. **信号只能由归属 Agent 自己写入**。总控大脑不得代写任何他方信号；代写等同伪造。
2. **信号不是完成证据**。验收必须核对交付物本身：文件作者、实现内容、可复现的运行输出，以及对应 Agent 的真实运行痕迹。
3. **`"status": "VOID"` / `"valid": false` 的信号不构成完成依据**，读者必须 fail-closed。
4. 本目录在 2026-10-27 曾出现 agy 代写 `codex` / `deepseek` / `claude` / `grok` 四个信号的事故；四个文件已就地改为 `VOID` 记录，原内容见 git 历史 `a1e96ed`。
5. 若某 Agent 根本没有运行过，正确做法是报告“该专线未启动、无法验收”，**不是自己补位完成**。
