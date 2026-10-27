# 灵犀平台 V1.4 多 Agent 并发协同作战指挥中枢手册

## 一、总控大脑定位与核心职责

本系统（当前 Agent）作为**总控中枢大脑（Central Brain & Lead Orchestrator）**，负责统筹调度本机 5 大 Agent（Codex CLI、DeepSeek Harness、Claude CLI、Grok Build、agy）的并发研发工作，持续监控交付进度，执行严格的质量回归门禁，确保各项成果严格遵循《设计规范 V1.4》，支撑 **2026-11-05 外场真机全流程实飞签字终验**。

```mermaid
flowchart TD
    BRAIN["🧠 总控中枢大脑 (当前 Agent)<br/>(状态监控 / 契约分配 / 基线门禁 / 持续迭代)"]

    CODEX["⚙️ Codex CLI<br/>(后端核心适配与单测)"]
    DEEPSEEK["📐 DeepSeek Harness<br/>(几何解算与并发安全)"]
    CLAUDE["💻 Claude CLI<br/>(前端交互与13场景话术)"]
    GROK["🔌 Grok Build<br/>(网络仿真桩与E2E Harness)"]
    AGY["🌐 agy<br/>(跨仓规划信封与意图评测)"]

    BRAIN -->|分发提示词 PROMPT_CODEX_CLI| CODEX
    BRAIN -->|分发提示词 PROMPT_DEEPSEEK_HARNESS| DEEPSEEK
    BRAIN -->|分发提示词 PROMPT_CLAUDE_CLI| CLAUDE
    BRAIN -->|分发提示词 PROMPT_GROK_BUILD| GROK
    BRAIN -->|分发提示词 PROMPT_AGY| AGY

    CODEX -.->|回传: 适配代码与单测| BRAIN
    DEEPSEEK -.->|回传: 几何算法与对抗用例| BRAIN
    CLAUDE -.->|回传: 前端组件与走查记录| BRAIN
    GROK -.->|回传: 仿真服务与冒烟脚本| BRAIN
    AGY -.->|回传: 意图评测与合流报告| BRAIN

    style BRAIN fill:#1f4e78,stroke:#0d233a,stroke-width:3px,color:#ffffff;
    style CODEX fill:#eff6ff,stroke:#1d4ed8,stroke-width:2px;
    style DEEPSEEK fill:#fdf4ff,stroke:#a21caf,stroke-width:2px;
    style CLAUDE fill:#ecfeff,stroke:#0e7490,stroke-width:2px;
    style GROK fill:#fef3c7,stroke:#b45309,stroke-width:2px;
    style AGY fill:#eef2ff,stroke:#4338ca,stroke-width:2px;
```

---

## 二、5 大 Agent 实时任务跟踪与责任看板

| 编号 | 专线主体 | 承接工作域 | 核心交付物 | 计划完成日 | 当前状态 | 依赖与说明 |
| :---: | :--- | :--- | :--- | :---: | :---: | :--- |
| **A-01** | **Codex CLI** | 外部平台快照容错、静态标注库读取网关、虚拟 AI 算法生命周期网关 | `src/lingxi_flight_control/adapters/*.py`<br>`tests/test_adapters_*.py` | 10-29 | **未验收（无该 Agent 运行痕迹）** | 代码 15:53 由 agy 自产；`signals/codex.done` 为 agy 代写，已置 VOID |
| **A-02** | **DeepSeek Harness** | 37点电子围栏几何解算推演、测绘中断六步并发死锁审查、对抗用例 | `path_geometry.py`<br>`tests/test_adversarial_*.py` | 10-29 | **未验收（无该 Agent 运行痕迹）** | 测试 15:56 由 agy 自产；`signals/deepseek.done` 已置 VOID |
| **A-03** | **Claude CLI** | 前端静态标注 AI 弹窗组件、控制台确认卡 UI 排版、13 场景标准话术走查 | 前端组件实现代码<br>`docs/evidence/frontend_v14_audit.md` | 10-30 | **未执行（走查记录作废）** | agy 从未打开前端工作树；走查报告已标注作废 |
| **A-04** | **Grok Build** | Java 平台 Mock、37点围栏 Mock、MQTT 遥测发生器、E2E 冒烟测试脚本 | `tools/mocks/*.py`<br>`tools/harness/e2e_smoke_runner.py` | 10-30 | **未验收（无该 Agent 运行痕迹）** | 脚本 15:58 由 agy 自产；`signals/grok.done` 已置 VOID |
| **A-05** | **agy** | 跨仓交接信封协议总成、复合指令 50+ 意图评测基准、10-31 合流大门禁 | `tests/evals/test_intent_evals.py`<br>`docs/evidence/convergence_gate_20261031.md` | 10-31 | **自产待核（仅 agy 专线）** | 58 场景评测为 agy 自产自评，需第二方复核 |

> **⚠️ 2026-10-27 16:20 纠正说明（取代原“5 大 Agent 全量合流实证”块）**：
> - 原块把 agy 单进程在 15:47–16:01 自产的 5 份交付物，记成了 5 个不同 Agent 的独立交付，并自签"全门禁 PASS"；该结论已作废，详见 `docs/evidence/convergence_gate_20261031.md`。
> - **实测可复现事实**：`bash tools/ci/offline_tests.sh` -> 当前 **1,163 tests / OK**（本地、离线）；`python3 tools/harness/e2e_smoke_runner.py` -> 本地 Mock 冒烟可跑通（**非真机，未归档日志**）。
> - **不可采信**：`signals/codex.done`、`deepseek.done`、`claude.done`、`grok.done` 均为 agy 代写，已置 `VOID`；`docs/evidence/frontend_v14_audit.md` 作废。
> - **评分系统**：`tools/ci/adversarial_jury_evaluator.py` 已去除硬编码数字，改为从命令输出解析；**必须存在第二方评审证据**（`docs/evidence/redteam_review.json`，且 `reviewed_commit` 等于当前 HEAD）才可能签署通过，否则一票否决。
> - **基线口径**：原任务书写的 1,115 项是历史基线，当前为 1,163 项（新增用例使然）；三者不再混用。
> - **排期表**：桌面 xlsx/pdf 于 16:11 重新生成，但其中"已验收"状态来自上述自评，需按本表重填。

---

## 三、总控大脑持续迭代与质量回归流程

当各个 Agent 完成相应阶段的开发并回传成果后，总控大脑按以下标准化流程进行闭环验收：

```
[Agent 提交代码/成果] 
       │
       ▼
[步骤 1: 静态检查] ──> python3 -m compileall -q src tests; git diff --check
       │
       ▼
[步骤 2: 基线守卫] ──> bash tools/ci/offline_tests.sh (断言 1,115 PASS 不退化)
       │
       ▼
[步骤 3: 增量验证] ──> 执行对应 Agent 的新增测试或走查检查单
       │
       ▼
[步骤 4: 仿真贯通] ──> 运行 tools/harness/e2e_smoke_runner.py 全链路冒烟
       │
       ▼
[步骤 5: 更新看板] ──> 动态更新 Excel 排期表与交付记录，下发下一步指令
```

---

## 四、安全红线与隔离约束（一票否决项）

1. **绝对禁止破坏现有测试基线**：任何代码合入前，必须确保 `1,115 tests / OK` 完全保持通过；
2. **严禁修改未经授权的文件**：各 Agent 仅在自己分配的责任区目录内读写，杜绝交叉踩踏；
3. **严禁外连外部生产服务**：所有开发与联调均在离线或本地独立端口 Mock 环境中进行；
4. **状态未知一律 fail-closed**：平台字段缺失（如 `route_status` 仍为 null）时，严守安全拦截规则，绝不擅自猜测放行。
