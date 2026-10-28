# 文档入口与管理规则

本页是 `docs/` 的唯一导航入口。当前事实以 [当前状态快照](status/CURRENT.md) 为准；验收记录、证据文件和对话复盘不能单独覆盖当前状态。

## 先看这里

| 目的 | 入口 | 使用规则 |
| --- | --- | --- |
| 当前项目状态 | [status/CURRENT.md](status/CURRENT.md) | 只写当前基线、已验证、阻塞和下一步 |
| 继续工作 | [CONTINUE.md](CONTINUE.md) | 保留历史交接线索；新结论先写入当前状态 |
| 稳定契约 | [ktd-platform-contract.md](ktd-platform-contract.md)、[platform-executor.md](platform-executor.md)、[route-state-safety.md](route-state-safety.md) | 描述接口、权限、状态和 fail-closed 边界 |
| 运行与操作 | [service.md](service.md)、[standalone-gateway.md](standalone-gateway.md)、[natural-language-cockpit.md](natural-language-cockpit.md) | 描述启动、请求和本地验证方式 |
| 验收快照 | `ACCEPTANCE_*.md` | 绑定一个版本、范围和验证时间，不替代当前状态 |
| 原始证据 | [evidence/](evidence/) | 结论必须能回到命令、代码 SHA 和产物；无独立证据不得升级状态 |
| 对话复盘 | [reviews/](reviews/) | 记录目标、授权、事实、验证和未决项，不替代产品证据 |
| Agent 规范 | [agents/](agents/) | 约束任务拆分、领域和工单维护方式 |
| Prompt 版本 | [prompts/](prompts/) | 只作为输入版本记录，不作为运行结果或业务真值 |

## 状态和证据等级

新增验收或证据文档时，文件开头使用以下字段（YAML front matter 或等价表格均可）：

```yaml
status: verified | partial | blocked | obsolete | draft | future
as_of: YYYY-MM-DD
code_sha: <commit or working-tree>
scope: <代码、服务、平台或设备范围>
evidence_level: offline[, mock[, integration[, shared[, device]]]]
unknowns: <尚未验证的关键事实>
next_action: <下一步及其前置条件>
```

`offline`、`mock`、`integration`、`shared`、`device` 是证据层级，不是完成度。HTTP 受理、模拟器通过、单元测试通过和 `submitted` 回执都不能单独证明设备效果。

## 维护约定

1. 当前结论只在 `status/CURRENT.md` 更新；历史文档保留原样并标注 `as_of` 和 `code_sha`。
2. 验收文档写“当时验证了什么”，不要回写成现在仍然成立的能力；如果被新基线替代，标 `obsolete` 或 `superseded_by`。
3. evidence 文件只追加新的证据，不覆盖旧报告；报告正文必须列出命令、产物、未知项和副作用。
4. `.workbuddy-ai/memory/` 只用于对话线索，不能作为产品真相源，也不能替代代码、测试或平台回执。
5. 新文档先挂到本索引，再提交；暂不批量搬迁旧文件，避免破坏现有引用。

## 当前已知整理项

- `docs/CONTINUE.md` 仍是长时间线，短期保留以兼容历史链接；当前状态请看 `status/CURRENT.md`。
- `docs/evidence/convergence_gate_20261031.md` 和 `frontend_v14_audit.md` 已明确作废，不能作为合流门禁。
- `docs/agent-flight-control-isolation.md` 引用的 `dji-receipts-v1.json` 属于外部 Java 工作树，本仓只保留来源说明，不把它当作本仓 fixture。
- 2026-10-28 生成的部分评测产物仍未跟踪，且记录的是旧代码 SHA；未登记到当前状态前不能作为发布结论。
- `docs/agents/CENTRAL_BRAIN_DISPATCH.md` 与 `docs/prompts/MASTER_LOOP_PROMPT*.md` 含 2026-10-31/11-05 的计划日期；在到期前应标为 `future`/`draft`，不能当作当前排期或已完成事实。
