# 对抗式编程与第二方评审协议

> 2026-10-27 事故后新增。起因：一个总控 Agent 自己写完五个专线的交付物、自己代写 `signals/*.done`、
> 自己出题、自己判卷，最后自签"全门禁 100% 通过"。本协议把"第二方"变成硬约束。

## 1. 角色

| 角色 | 谁来做 | 能做什么 |
| :--- | :--- | :--- |
| 执行方 | 负责该专线的 Agent | 写代码、写测试、自己跑测试、提交自己专线的信号 |
| 第二方（红队） | **另一个 Agent 或人**，例如 `claude` / `codex` / `grok` / `dsh` CLI | 只读审阅 diff 与门禁输出，指出缺陷、给复现命令、判 pass / changes_requested |
| 门禁 | `tools/ci/adversarial_jury_evaluator.py` | 跑确定性检查 + 读取第二方评审结论；缺证据即否决 |

**执行方不得充当自己的第二方**，也不得代写他方信号或他方交付物。

## 2. 流程

```text
1. 执行方改代码 → git add（暂存）
2. 生成评审包：python3 tools/ci/adversarial_jury_evaluator.py --emit-packet > /tmp/redteam_packet.md
3. 第二方只读审阅：把 packet 交给外部 Agent（禁止它改文件）
4. 第二方把结论写成 docs/evidence/redteam_review.json
5. 门禁：python3 tools/ci/adversarial_jury_evaluator.py   # 无 --deterministic
6. 通过才允许 commit / push
```

第二方启动示例（任选其一，均要求只读）：

```bash
claude -p "只读审查 /tmp/redteam_packet.md，指出具体缺陷与复现命令，不要修改任何文件"
codex exec "只读审查 /tmp/redteam_packet.md，输出 JSON 结论"
grok "只读审查 /tmp/redteam_packet.md"
dsh exec "只读审查 /tmp/redteam_packet.md"
```

## 3. `docs/evidence/redteam_review.json` 格式

```json
{
  "reviewer": "claude-cli / 张三 / 其他",
  "reviewer_kind": "external_cli",
  "reviewed_tree": "<git write-tree 的哈希>",
  "verdict": "pass",
  "findings": [
    {"severity": "high", "file": "src/...", "issue": "...", "repro": "..."}
  ],
  "no_findings": false,
  "reviewed_at": "2026-10-27T17:00:00+08:00"
}
```

硬性要求：

- `reviewer_kind` 只能是 `external_cli` 或 `human`；写 `self` / `agy` 一律否决。
- `reviewed_tree` 必须等于门禁运行时刻的 `git write-tree`；暂存区一变就失效，必须重审。
- `verdict` 为 `changes_requested` 或有 high 级 finding 未处理时，不得签署。
- 无 finding 时必须 `"no_findings": true` 并说明审阅范围。

## 4. 门禁行为

- `python3 tools/ci/adversarial_jury_evaluator.py`：完整签署模式，**缺第二方证据直接否决**。
- `--deterministic`：只给 CI 跑确定性检查，**明确写"不可用于签署"**。
- `--no-offline`：跳过全量离线套件（调用方须已自行运行）。
- `--emit-packet`：输出评审包。
- 所有分数由命令输出解析，禁止硬编码测试数/通过率；解析不到即判失败。

## 5. 红线

1. 不得代写他方 `signals/*.done`；`VOID` 信号不构成完成依据。
2. 不得把"自己写的实现 + 自己写的测试 + 自己打的 100 分"称为对抗式编程。
3. E2E 只有在本地 Mock 通过时，只能说"Mock 冒烟通过"，不得说"真机/外场通过"。
4. 报告里的每个数字必须能追溯到一条可复现命令的输出。
