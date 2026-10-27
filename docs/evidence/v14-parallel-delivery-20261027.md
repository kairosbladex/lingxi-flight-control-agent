# V1.4 三线并行交付与对抗式评审记录（2026-10-27）

**执行方式**：DSH 作为总控大脑，拆 3 条独立交付线，各自独立 git worktree + 分支互不干扰；每条线完成后由**另一条线的 Agent** 做只读第二方评审，评审不通过则回修，直到签署通过后才合流。

## 一、三条线

| 线 | 任务 | 分支 / worktree | 最终 commit | 新增测试 | 写作用域 |
| :-: | :--- | :--- | :--- | ---: | :--- |
| A | 4.4 表6 话术逐字对齐、4.3.4 复合歧义追问、4.1.3 超时兜底、4.2 提示可达、状态投影 | `feat/v14-line-a` · `.worktrees/lingxi-v14-line-a` | `5d58477` | 90（`test_v14_phrase_gaps.py`） | `flight_policy.py`、`cockpit.py`、`task_events.py`、`full_control.py`、该测试 |
| B | 3.2.1 触发词→航线、3.2.2 几何→允许航线/拦截（纯规则库） | `feat/v14-line-b` · `.worktrees/lingxi-v14-line-b` | `07794fc` | 61（`test_v14_route_mapping.py`） | `route_mapping.py`（新）、该测试、`ACCEPTANCE_FLIGHT-V14.md` |
| C | 2.2.1 六十六项动作控制权参数化矩阵（只加测试与证据） | `feat/v14-line-c` · `.worktrees/lingxi-v14-line-c` | `f2efdd9` | 8 用例 / 314 subTest | `test_v14_action_authority_matrix.py`（新）、矩阵证据文档（新） |

## 二、对抗式评审（环形交叉，非自评）

| 被审线 | 评审方 | 首轮结论 | 首轮关键 finding | 回修 | 复审 |
| :-: | :--- | :--- | :--- | :--- | :--- |
| A | line-b | **changes_requested** | **HIGH**：4.1.3 超时兜底没有生产触发路径（`resolve_temporary_outcome` 不传 now，`timed_out` 恒 False）；4.2/4.3.2 话术因快照不投影而不可达 | `20db494` 打通生产入口 + 投影 | **pass** |
| B | line-c | **changes_requested** | 3 medium：裸词「几何体」误拒普通描述；表1/表2 冲突静默选边；「多点顺序保真」不可证伪 | `2c8af76` 收窄匹配+否定窗口+conflict 契约+改名 | **pass** |
| C | line-b | pass（1 medium） | 矩阵只构造 `owner=None`，钉不住 `!= actor` 退化 | `f2efdd9` 补 61×2 跨操作员 subTest + 变异验证 | **pass** |
| 集成 | line-c | **changes_requested** | 3.2.1/3.2.2 与 `target_resolver`/`handoff` 双实现；4 条话术双份真源；几何体航线漏拦截 | `5d58477`、`07794fc` | 见下 |

评审方法：只读 diff + 对照规范 docx 原文逐字核对 + **变异测试**（把校验改成空转/改成 `is None`，看测试是否真的失败）+ 复跑全量套件。评审产物为各 worktree 内未跟踪的 `docs/evidence/redteam_review.json`（`reviewer_kind=external_cli`、`reviewed_tree` 等于被审树）。

## 三、合流与门禁

- 合流分支：`test`；三次 `--no-ff` merge + 两次回修 merge
- 合流后全量离线套件：**Ran 1322 tests, OK**（基线 1163 + 三线新增 159）
- 门禁：`python3 tools/ci/adversarial_jury_evaluator.py`（完整签署模式，要求第二方评审文件与当前树一致）
- 成绩单：`docs/evidence/jury_scorecard_v14.json`

## 四、已接线范围与仍待裁决

1. **3.2.1/3.2.2 仍需归属裁决**：`route_mapping.py` 已接入 cockpit 的飞行中不支持航线预检，仅处理网格/几何体/螺旋等明确拒绝词；地面规划仍由规划 Agent（`lingxi-agent`）承担，目标几何仍由 `target_resolver`/`handoff_contract` 负责。
2. **4.1.3 超时入口**：`resolve_temporary_outcome` 是模拟器/测试入口，HTTP/agent 生产路径尚未接线（docstring 已写实）。
3. **真实平台/真机**：电子围栏真实接口、权威快照、挂载能力事件源、控制权中台、真实 MQTT 均未联调；本批全部证据仅覆盖内存模拟器与本地 Mock。
4. **规范内部冲突（待产品裁决）**：表6 与 2.2.3/2.2.5 的话术差异、变焦三种写法、测绘可中断性、体目标两版话术，已列在 `flight_policy.py` docstring「规范内部冲突清单」；实现未单方选边。
5. **残余 low**（评审记录在案，不影响本轮签署）：否定启发式跨子句边界、`conflict=True` 的上游追问话术未接、`IN_FLIGHT_ROUTE_TYPES` 未参与运行时判定、矩阵证据行号以符号为准。

## 五、复现命令

```bash
# 全量离线
bash tools/ci/offline_tests.sh

# 门禁（完整签署模式）
python3 tools/ci/adversarial_jury_evaluator.py

# 三线专项
PYTHONPATH=src:tests python3 -m unittest discover -s tests -p 'test_v14_*.py'
```
