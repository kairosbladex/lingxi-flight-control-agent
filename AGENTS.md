# lingxi-flight-control-agent 工作规则

本文件做两件事：把 engineering skills 接到本仓，并记录本仓已核验的事实性边界。
完整的审批、授权、验证分层与 Git 规则沿用全局 `~/.agents/AGENTS.md` 与 `~/AGENTS.md`，本文件不重复、不扩大，也不替代它们。

## 项目定位（来自 README 与现有实现，非新增约定）

- 本仓负责已绑定执行上下文内的受限控制文本解析与内存执行闭环；巡检任务规划、草稿与下发交接属于任务下发 Agent（兼容目录 `lingxi-agent`），两者职责与权限隔离。
- 默认执行器是模拟器（`SimulatorExecutor` / `MemoryFlightControlSimulator`）；`KtdPlatformExecutor` 默认关闭，缺少服务端权威快照契约时不得启用。
- 未获覆盖具体动作的授权，不接生产 MQTT、不启用真实平台写接口、不改变 59 项动作契约与默认执行器配置。
- HTTP 受理、模型预览、`confirmed_not_dispatched` 回执都不代表设备已执行。
- 对应前端工作树 `/Users/wangxiaolin/PycharmProjects/lingxi-flight-agent-ui` 的改动遵循该仓自身规则；该工作树根目录目前没有 `AGENTS.md`，动前端前先确认其当前分支与规则，不要假定本文件的规则可以外推。

## Skills 使用

- 默认**仅用户显式请求时启用**，不作为工单、实现或排障的默认前置/后续步骤：
  `grill-with-docs`、`grilling`、`improve-codebase-architecture`、`code-review` 及其多评审工作流。
- 可按任务自动选用：`tdd`、`diagnosing-bugs`、`codebase-design`、`domain-modeling`。
- `implement-spec` 与 `code-review` 会拉起并行子 Agent：仅用户明确要求多 Agent 时才用，默认走单线 `implement`。
- `to-spec` / `to-tickets` / `triage` 的工单落点见 [docs/agents/issue-tracker.md](docs/agents/issue-tracker.md)；**不要创建 GitHub/GitLab issue**。
- 技能不得扩大范围或覆盖本仓安全、验证与 Git 约束。只读 review 默认只报告，明确要求修复才修改。
- 读到词汇表/领域文档不等于获得编辑授权；只有任务明确包含领域文档变更时才改 CONTEXT/ADR。
- 不要为使用技能而顺带搭建文档、做访谈或跑架构巡检。

## 按任务读取

- 维护工单时才读 `docs/agents/issue-tracker.md`；处理标签时才读 `docs/agents/triage-labels.md`；涉及领域语义或架构决策时才读 `docs/agents/domain.md` 指向的文档。
- 同会话已读且未变化的内容不重复读取；引用链接不是全部必读清单。

## 验证

只读咨询/审查不跑业务测试。代码、配置或脚本修改后按影响执行：

```bash
bash tools/ci/offline_tests.sh
git diff --check
```

- `tools/ci/offline_tests.sh` 是本地与 CI 共用的唯一真源，等价于 `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'`。
- 整套用例必须完全离线；`tests/test_offline_guard.py` 会断言没有 socket 连接尝试。真实模型联调在 `tools/flight_agent_demo.py` 与 `tools/bench/*`，不在 discover 范围。
- 文档、规则、技能接线这类改动只检查格式、链接引用与规则一致性，不跑业务测试。
- 未验证部分必须明确说明；单元测试通过不代表真实闭环或设备执行成功。

## Git 与交接

- 完成必要验证后只暂存本次相关文件，创建小而明确的中文 commit（可保留 `feat:`/`fix:`/`docs:` 等前缀），push 当前语义开发分支并核验远端分支 SHA。
- 不夹带无关脏文件、不改写历史、不使用破坏性 Git 命令。保留用户与同事的既有改动。
- 需要跨会话继续或外部验收的交付：在 `docs/` 下按工单号维护验收记录，写清做了什么、未做范围、验证证据与 Git 状态；证据文件按 `docs/evidence/<topic>-<YYYYMMDD>.*` 命名。
- 纯问答、只读审查及无需交接的小修改不创建上述文档。
