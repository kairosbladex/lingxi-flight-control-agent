# PROMPT_AGY: 跨仓规划信封协议总成、大模型意图评测与合流门禁

## 1. 角色定位与目标
- **执行角色**：业务编排与大模型评测专家 (Lead Orchestrator & Evals Specialist)
- **执行工具**：agy (Antigravity CLI)
- **核心目标**：总成跨仓地面规划信封协议、构建包含 50+ 个典型表达的大模型复合指令评测基准，并统筹组织 10 月 31 日全系统集成合流质量门禁（Convergence Gate Review）。
- **基线约束**：严格遵循 V1.4 规范中“模型只生成结构化建议、确定性层严密校验拦截”的设计分层。

---

## 2. 专属责任区与文件边界
- 跨仓交接信封协议：`src/lingxi_flight_control/handoff_contract.py`
- 大模型意图评测基准：`tests/evals/test_intent_evals.py`
- 评测数据集与证据：`fixtures/evals_dataset_v14.json` 与 `docs/evidence/convergence_gate_20261031.md`

---

## 3. 具体实施任务说明

### Task 1: 跨仓地面规划信封协议总成 (lingxi-flight-handoff/v1)
- **背景**：根据 V1.4 3.1 节规范，地面规划由规划 Agent（`lingxi-agent`）执行，生成任务草稿后通过信封协议移交飞控 Agent。
- **实施要点**：
  1. 锁定信封版本 `schema_version: "lingxi-flight-handoff/v1"`；
  2. 严格校验交接信封中的必要上下文：
     - `actor_id`（操作人员权限令牌）
     - `task_id` 与 `task_type`（巡检任务 vs 测绘任务）
     - `project_id` 与绑定无人机编号 `drone_id`
     - `target_candidates`（点/线/面几何实体列表及其版本号哈希）
     - `snapshot_version`（快照版本，防脏读过期交接）
  3. 完善 `src/lingxi_flight_control/handoff_contract.py`，确保对任何非法、过期或伪造信封抛出明确的拒绝错误码。

### Task 2: 大模型复合指令 50+ 意图拆解自动化评测基准 (Evals Harness)
- **背景**：依据 V1.4 4.3.4 规范，大模型负责将复杂的自然语言指令拆解为顺序或并发动作卡片。
- **实施要点**：
  1. 在 `fixtures/evals_dataset_v14.json` 中建立 50+ 条高覆盖度真实测试语料：
     - **顺序复合**：“先向前飞10米，然后云台向下看45度，拍一张照片”；
     - **并发复合**：“一边向前飞5米，一边播放防溺水语音‘水深危险请勿靠近’”；
     - **隐含参数**：“往左转一点”（映射为小角度微操，并提示参数）；
     - **口语化与方言**：“升高点”、“往后退一点点”、“赶紧停下”；
  2. 编写 `tests/evals/test_intent_evals.py`：
     - 自动化调用模型意图解析器，比对期望拆解出的 action 列表与参数键值对；
     - 设定量化硬门禁：**核心动作拆解准确率 ≥ 95%，参数提取准确率 ≥ 98%**。

### Task 3: 组织 10 月 31 日全系统合流门禁 (Convergence Gate)
- **合流标准核验清单**：
  1. **代码基线门禁**：离线单测 `bash tools/ci/offline_tests.sh` 保持原有 1,115 项用例 100% PASS，新增适配单测与评测用例全部通过；
  2. **仿真联调门禁**：运行 Grok Build 编写的 `python3 tools/harness/e2e_smoke_runner.py`，全链路仿真通过率 100%；
  3. **前端走查门禁**：核验 Claude CLI 提交的前端 13 场景标准话术与标注弹窗验收记录；
  4. **合流交付物**：在 `docs/evidence/convergence_gate_20261031.md` 中汇总全系统多 Agent 交付报告，打上合流标签，宣布进入外场实飞预演阶段。

---

## 4. 验收与交付标准
1. **评测基准达标**：意图拆解通过率达到量化指标，输出自动化评测报告。
2. **跨仓信封零差错**：跨仓交接单测 100% 通过，无类型漂移。
3. **交付回传**：输出评测代码与合流确认文档，汇报给总控大脑。
