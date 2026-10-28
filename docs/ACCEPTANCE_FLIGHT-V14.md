# FLIGHT-V14 交付审计（V1.4 最终验收）

2026-10-22。依据《灵犀平台自然语言智能控制功能设计规范 V1.4》做逐条验收与安全错误专项检查。
本文是 P1–P5 之后的**最终审计**，不重复各阶段设计细节，只记录：逐条矩阵、专项检查结论、
本轮修复、真实执行过的命令与输出、证据分层、未实现与未验证项、Git 与回滚。

- 规范文档 SHA256：`e4e42b712e3249d1fc09d0968445525c0702808e894290d49df2b70301108c78`
- 仓库：`lingxi-flight-control-agent`，分支 `test`，基线 HEAD `d6d284d`（`feat: 补齐悬停动作与指点/环绕平台映射`）
- 默认执行器：模拟器。`KtdPlatformExecutor` 仍默认关闭。
- 未连接 MQTT / 真实无人机 / 共享 test/uat/prod 服务；未执行任何平台写操作；未 push、未部署、未重启共享服务。
- 保留工作区既有 `.workbuddy-ai/` 与 P1–P5 未提交改动；未使用 `reset --hard`、`checkout --`、`clean`、`stash`。

**结论先行**：V1.4 的确定性规则、typed 契约与模拟器闭环已完成；真实平台与真实设备全部未验证。
本轮审计发现并修复了 2 个真实安全缺口（`route_status` 未知被当成安全、系统提示词把悬停教成急停），
另有 1 项规范解释分歧按产品语义保留并记录。审计之后的两批 P1 修复分别收紧了
**confirm 阶段「先复检围栏、后作废测绘」的顺序**与**围栏 provider 的硬超时边界**；
P5 再用 SQLite 持久化 reservation 消除了「跨进程并发时网关可能调用两次」的窗口。
所有结论都不能证明真实飞行成功。

## 2026-10-28 收口补充

本轮在上述基线之上完成了四项收口：

- 三个驾驶舱入口默认启用 `RuleBasedToolSelector`；检索异常仍回退全量，安全工具和控制权前置动作不被裁掉。
- 计划步骤确认卡把 `requested_parameters`、`adjustments`、`warnings` 透传到前端并逐步展示；前端拒绝有效值与调整结果不一致的卡片。
- 计划游标、步骤结果、绑定哈希、状态版本和 `awaiting_arrival` 状态写入既有 `CheckpointStore`；重启恢复前重新校验绑定，平台没有独立到位证据时不推进后续步骤。
- Zhuoyi 平台入口保持逐项确认，`supports_plan_execution` 在独立到位契约补齐前为关闭状态；`KtdPlatformAdapter` 只接受 `fail_closed`，不提供宽松 `route_status` 回退。

本轮验证：Python 离线套件 **1356 tests OK**，前端单测 **179 tests OK**，前端 `npm run build` 通过。
上述结果仍属于离线、模拟器和隔离夹具证据；真实 Java 事件、终态回执、设备位姿/媒体效果和真实平台写入仍未验证。

## 一、本轮修复（先修代码，再改文档）

| # | 问题 | 风险 | 修复 | 回归用例 |
| --- | --- | --- | --- | --- |
| 1 | 平台快照 `route_status` 为 `null` 时，`move_*`/`turn_*`/`point_flight`/`point_orbit` 仍可生成确认卡 | 无法证明"不在自动航线"，V1.4 2.2.3 门禁失效，属"把未知当安全" | `authority_contract.ROUTE_GATED_ACTIONS` 把 `route_status` 加入这些动作的 `core` 要求；`require_authority` 对 `unknown` 一律 `AUTHORITATIVE_STATE_INCOMPLETE` | `test_unknown_route_status_alone_blocks_micro_and_temporary_actions`、`test_unknown_route_status_blocks_micro_actions_before_any_compile` |
| 2 | Java 声明 `route_status` 不可用时，微操只得到笼统的 `AUTHORITATIVE_STATE_INCOMPLETE` | 缺口不可归因，运维无法区分"平台没有该能力"与"字段被留空" | `CAPABILITY_ACTIONS['route_status']` 增加 8 个微操动作，返回平台自己声明的 `ROUTE_STATE_SOURCE_UNAVAILABLE` | 同上（断言 typed 错误码与零写请求） |
| 3 | 系统提示词写着"手动飞行悬停使用 emergency_stop"，与 V1.4 2.2.4、4.3.1 及确定性实现矛盾 | 非精确表达（如"悬停一下"）走模型时可能被教成急停，急停会改变任务状态 | 提示词改为"手动飞行悬停使用 hover（原地悬停），绝不替换为 emergency_stop" | `test_system_prompt_never_teaches_hover_as_emergency_stop` |

配套的**测试夹具修正**（不是产品行为放宽）：`tests/test_platform_runtime.py` 与
`tests/test_vendor_control.py` 的合成快照原本把 `route_status` 写成 `null` 却断言微操可下发，
正是被修复的漏洞本身。改为 `'idle'` 并注明"本仓合成夹具取值，不是对平台取值的声明"。
未放宽任何产品代码分支。

**保留的解释分歧（未擅自改产品语义）**：V1.4 2.2.3 字面把"其他飞行微操指令"一律拦截，
而 4.3.1 把指点飞行/环绕也归入飞行微操类；但 4.1.1 明确"无人机正在执行临时变更（指点飞行/环绕）时"
可被新指令中断，说明临时任务接管是产品设计。本仓按 4.1.1 保留"自动航线中指点/环绕作为临时变更接管、
主线断点保留"的既有语义，未把 `point_flight`/`point_orbit` 加入 2.2.3 拦截集。
这是**规范内部的解释分歧**，需要产品确认；本轮不据此改行为，只记录。

## 二、V1.4 逐条验收矩阵

执行模式口径：**单元** = 纯函数/契约单测；**模拟器** = 内存执行器闭环；**假传输** = 假 Java/HTTP
协议与状态机；**隔离联调** = 回环假服务 + 真 Agent HTTP；**未实现** = 本仓不承担或无契约。

| 规范条目 | 实现 | 测试 | 证据 | 执行模式 | 真实平台 | 真实设备 | 已知差距 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2.1.1 点/线/面显示 AI 配置、体标注隐藏、多选、非必填 | `ai_lifecycle.AnnotationAlgorithmConfig`；体标注 `BODY_ANNOTATION_AI_UNSUPPORTED` | `test_ai_lifecycle`（31） | `v14-ai-mapping-20261022.json` | 单元 | 前端按钮显隐未接入真实标注库 | 未验证 | 标注库写操作不在本仓 |
| 2.1.2 实时+航点检测、到达开启、离开关闭、全局去重防误关 | 六阶段分离；`annotation_active_set`/`algorithms_to_stop`；只有 `started` 进运行集合 | `test_ai_lifecycle` 去重/共享/防误关/请求≠运行 | 同上 | 单元 + 模拟器 | `ALGORITHM_LIFECYCLE_UNAVAILABLE`（平台只有任务级 videoDetect） | 未验证 | 算法粒度启停接口缺失 |
| 2.2.1 控制权无豁免（含急停/返航/载荷） | `_CONTROL_FREE_ACTIONS` 仅 4 项申请/审批动作；预览与确认各查一次；平台 `_validate_snapshot` 对非只读动作全查 | `test_product_v13.test_all_flight_and_payload_actions_require_control`、`test_control_revoked_between_preview_and_confirm` | `v14-contracts-20261022.json` | 模拟器 + 假传输 | 未验证 | 未验证 | 平台侧真实控制权接口未联调 |
| 2.2.2 围栏前置校验、接口不可用降级 | `evaluate_path` 六态；只有 `clear` 放行，`blocked`/`invalid` 拒绝，其余降级并警告；协作式 provider 不进平台路径 | `test_path_fence`（73） | `v14-fence-orbit-20261022.json` | 单元 + 假传输 | `/electronicFence/path-route/with-clean` 的 37 点折线、`altitude` 基准未验证 | 未验证 | 围栏数据版本字段不存在，用 confirm 重查代替 |
| 2.2.3 自动航线中微操拦截、载荷放行 | `check_flight_policy` 对 `MICRO_ACTIONS` 在 `running` 拒绝；`route_status` 未知时由权威契约 fail-closed（本轮修复） | `test_running_route_blocks_micro_but_allows_payload`、本轮新增 2 项 | 本文第四节 | 模拟器 + 单元 | 平台 `route_status` 恒 null，微操在权威路径被拒绝 | 未验证 | 指点/环绕是否同属拦截集（见第一节解释分歧） |
| 2.2.4 悬停=暂停航线+断点；手动原地悬停 | 模拟器 `HOVER` 不改 `task_status`；平台 `_compile_hover` 按最近运动选 120/122/207，**明确不用 113**；提示词本轮修正 | `test_v13_hover_and_point`（13）、`test_cockpit_hover_is_its_own_action_and_never_emergency_stop` | 本文第四节 | 模拟器 + 假传输 | 悬停折算指令的真实设备效果未验证 | 未验证 | 平台只返回受理 |
| 2.2.5 参数超限取设备极值并提示 | `normalize_parameters` 取协议与设备表交集；卡片三列 requested/parameters/adjustments；无设备表且越界即拒绝 | `test_clamp_uses_current_device_table_and_confirmed_effective_value` | `v14-contracts-20261022.json` | 模拟器 | `device_limits` 是否真实返回未验证 | 未验证 | 坐标/距离/航向不在放宽范围 |
| 3.1 地面规划 11 步 | 本仓不实施；交接契约 `lingxi-flight-handoff/v1` 只定义边界 | `test_handoff_contract`（41） | `v14-cross-repo-20261022.json` | 单元 | 不适用 | 不适用 | 属任务下发 Agent |
| 3.2.1 触发词→航线 | 本仓不实施（规划 Agent） | 规划仓 17 项（P4 记录） | 同上 | 单元 | 不适用 | 不适用 | 本轮未复跑（见第四节） |
| 3.2.2 点/线/面/体/混合强制映射 | `target_resolver` + `handoff_contract` 几何校验；体、混合、多线/多面、飞行中非单点全部拒绝 | `test_target_resolver`（15）、`test_handoff_contract` | 同上 | 单元 | 静态标注解析接口缺失（`STATIC_ANNOTATION_SOURCE_UNAVAILABLE`） | 未验证 | 无真实标注库适配器 |
| 3.3 AI 生命周期（自动加载/去重/冲突追问/体拦截） | `plan_selection` 冲突追问；`ALGORITHM_NOT_CONFIGURED` 整批拒绝；`仅替换` 返回 `ALGORITHM_REPLACE_UNSUPPORTED` | `test_ai_lifecycle` 冲突/原子/未配置 | `v14-ai-mapping-20261022.json` | 单元 + 模拟器 | 标注 AI 配置读取接口未接入 | 未验证 | `仅替换` 未落地为可执行动作 |
| 4.1.1 连续指令中断临时任务；测绘中断保护 | 临时任务原子替换保留主线断点；测绘首屏成果失效警告，确认后六步原子作废 | `test_mapping_interruption`（46，含跨进程 5）、`test_task_events`（15） | 同上 | 模拟器 + 本机多进程 | 真实事务边界未验证 | 未验证 | 六步是内存模拟 |
| 4.1.2 测绘默认不可中断、确认后关拼图、恢复置灰、跨类型禁止 | `INTERRUPTION_STEPS` 固定顺序；`route_resume_disabled` 进策略上下文；跨类型 `HANDOFF_CROSS_TYPE_FORBIDDEN` | `test_confirmed_interruption_runs_all_six_steps_in_order`、`test_old_card_dies_after_the_interruption` | 同上 | 模拟器 + 假传输 | `MAPPING_TRANSACTION_UNAVAILABLE` | 未验证 | 平台缺原子事务接口 |
| 4.1.3 断点/恢复/结束规则 | 主线保留断点、临时任务不独立保留；完成后必须显式选 `resume_main`/`end_all`，不猜默认 | `test_completion_requires_an_explicit_choice`、`test_end_all_choice_clears_the_breakpoint` | 同上 | 模拟器 | 未验证 | 未验证 | 超时按低电量规则由平台事件驱动，未接真实事件 |
| 4.2 低电量返航与充电续飞 | `low_battery_return` 最高优先级；充电后只恢复有效主线；测绘作废或结束全部不续飞 | `test_low_battery_return_has_highest_priority`、`test_recharge_restores_only_the_valid_inspection_mainline` | 同上 | 模拟器 | `UnavailableTaskEventBridge` 返回 `TASK_EVENT_SOURCE_UNAVAILABLE` | 未验证 | 未订阅真实平台事件 |
| 4.3.1 飞行微操词典 | 六向（相对机头）、转向、悬停、急停、坐标指点、环绕；路径几何由 `path_geometry` 生成 | `test_path_fence`、`test_v13_hover_and_point` | `v14-fence-orbit-20261022.json` | 单元 + 模拟器 + 假传输 | 指点/环绕平台能力清单未放行（`ACTION_UNAVAILABLE`） | 未验证 | 环绕半径不下发，仅提示语义差异 |
| 4.3.2 载荷词典 | 云台/变焦/镜头/拍照/录像/喊话/音量/灯光/AI；免暂停航线 | `test_product_v13_actions`（10）、`test_v13_extension`（7） | `v14-ai-mapping-20261022.json` | 模拟器 + 假传输 | 喊话/亮度部分走已查证 type；DRC 灯光传输未加 | 未验证 | 旧文本 type=201 需显式声明能力 |
| 4.3.3 任务控制 | 暂停/恢复/返航/结束当前/结束全部；结束前重复安全检查 | `test_end_current_preserves_main_but_end_all_clears_it`、`test_task_end_platform_safety_check_repeats_before_send` | 同上 | 模拟器 + 假传输 | 只允许 `task_scope=bound_main_only` | 未验证 | 含临时子任务的总结束不冒充支持 |
| 4.3.4 复合指令 | 顺序：逐步确认、任一步失败即停；并发：显式两项卡、预检全过才提交、部分失败不重试不伪造回滚 | `test_parallel_motion_and_speech_are_explicit_and_replayed_once`、`test_parallel_partial_failure_retains_success_and_does_not_retry` | 同上 | 模拟器 | 真实并发调度未接入 | 未验证 | 任意自然语言组合未全量覆盖 |
| 5 异常话术库（13 场景） | 无控制权/航线中强插/超限/遇障/测绘干预/不支持/混合/体目标/体标注 AI 均有固定话术 | 分散于上述用例；话术常量在 `flight_policy`/`ai_lifecycle` | 本文第三节 | 单元 + 模拟器 | 话术未在真实前端验收 | 未验证 | 前端仅类型检查与展示模型单测 |

> **2026-10-27 纠正（3.2.1 / 3.2.2 归属与接线口径）**：本轮并行交付新增
> `src/lingxi_flight_control/route_mapping.py`（配套 `tests/test_v14_route_mapping.py`），
> 作为 V1.4 3.2.1 表1 / 3.2.2 表2 的**纯规则库 / 参考实现**，已接入 cockpit 的飞行中不支持航线预检。需要把口径写实，避免与上表既有结论混淆：
>
> - 当前只有 `CockpitService._in_flight_unsupported` 使用它拦截明确不支持的网格/几何体/螺旋航线；
>   地面规划仍由规划 Agent 承担，目标几何仍由 `target_resolver` + `handoff_contract` 负责，
>   因此不把本模块当作地面规划唯一真源。
> - V1.4 3.2.1 / 3.2.2 在本仓与规划 Agent（`lingxi-agent`）之间的职责归属**尚未裁决**
>   （设计规范要求地面规划阶段由规划 Agent 执行）；裁决前 `route_mapping` 不得作为运行时唯一真源，
>   也不得与 `target_resolver` / `handoff_contract` 对同一输入并行做强制校验（避免双真源与两套拒绝话术）。
> - 分层边界保持：本仓目标/标注解析层（`target_resolver.TargetGeometry` +
>   `handoff_contract._parse_targets`）继续负责几何合法性、体/混合/多目标/顺序与交接契约；
>   `route_mapping` 只负责「触发词→航线类型 / 标注几何→允许航线或规范话术」的纯判定，
>   不解析自然语言、不做几何求解、不生成航点、不调平台。
> - 拒绝话术单一真源仍是 `flight_policy`：`route_mapping.MESSAGE_REJECT_GRID/MIXED/VOLUME/VOLUME_TABLE_2`
>   分别是 `COMPLEX_ROUTE_MANUAL_MESSAGE` / `MIXED_TARGET_MESSAGE` / `BODY_TARGET_MESSAGE` /
>   `BODY_TARGET_STANDARD_MESSAGE` 的别名（`assertIs` 用例固定，改一处即全局生效）。
>
> 本段只补充说明与分层建议，不改本文既有通过/未验证结论、分母与证据链。

## 三、安全错误专项检查（13 项）

| # | 检查项 | 结论 | 证据 |
| --- | --- | --- | --- |
| 1 | hover 是否被错误映射为 emergency_stop | **已修复**：确定性路径 `悬停` → `hover`；平台 `_compile_hover` 明确不使用 113；提示词曾教成急停，本轮修正并加回归 | `_compile_hover` 注释与分支；`test_system_prompt_never_teaches_hover_as_emergency_stop`；`test_cockpit_hover_is_its_own_action_and_never_emergency_stop` |
| 2 | 无控制权时是否有急停/返航/载荷豁免 | **无豁免**：模拟器 `_CONTROL_FREE_ACTIONS` 仅申请/审批 4 项；平台除 `read_state` 外全查；预览与确认各查一次，撤销控制权后确认被拒且状态不变 | `test_all_flight_and_payload_actions_require_control`、`test_control_revoked_between_preview_and_confirm` |
| 3 | route_status unknown 是否被当成安全状态 | **本轮修复**：`route_status` 为 null 时微操/指点/环绕原先放行；现按 `AUTHORITATIVE_STATE_INCOMPLETE` 拒绝，平台声明不可用时返回 `ROUTE_STATE_SOURCE_UNAVAILABLE` | 新增 2 项用例；`test_unknown_values_are_reported_and_block_their_actions` |
| 4 | 围栏 timeout 是否阻塞请求 | **不阻塞**：超时降级为 `timeout` 并给警告，迟到 `clear` 改判超时；只有 `blocked`/`invalid` 拒绝 | `test_path_fence.FenceProviderTests` 六态 |
| 5 | 围栏 unavailable 是否绕过控制权或确认 | **不绕过**：围栏在控制权与任务状态之后独立执行，降级只跳过围栏这一项 | `test_degraded_fence_still_requires_control_and_confirmation` |
| 6 | unknown receipt 是否被当成成功 | **不是**：`device_completion_verified()` 恒 False；`retry_allowed` 只对明确 `failed` 为真；Ktd 提交前先落 `unknown` 占位，Zhuoyi 把受理态映射为 `UNKNOWN + RECEIPT_PENDING` | `test_receipt_contract`（15）、`test_ktd_platform` 回执用例 |
| 7 | 未确认动作是否被执行 | **否**：模拟器与平台 `confirm` 都必须命中待确认卡且校验绑定哈希/上下文版本；`confirmation_id` 不存在返回 `CONFIRMATION_NOT_FOUND` | `test_cockpit_receipts`、`test_product_v13` 确认链用例 |
| 8 | 参数是否在模型层被偷偷截断 | **否**：`proposal_parameters` 去掉 schema 的 min/max，模型只能提原始值；截断只在确定性 `normalize_parameters` 发生并写入 `adjustments` | `test_clamp_uses_current_device_table_and_confirmed_effective_value`；提示词明写"不得自行截断" |
| 9 | 体目标、混合目标是否被偷偷转换 | **否**：`TargetGeometry.VOLUME`→`UNSUPPORTED_TARGET_GEOMETRY`；`MIXED`→`MIXED_TARGET_UNSUPPORTED`；飞行中非单点拒绝；交接层同名/多目标/顺序不合法分别拒绝，不自动挑、不静默排序 | `test_target_resolver`、`test_handoff_contract` 几何/顺序用例 |
| 10 | AI 未配置是否被替换成近似算法 | **否**：未配置算法整批 `ALGORITHM_NOT_CONFIGURED`，无部分绑定；`UnavailableAnnotationAlgorithmGateway` 抛错而不返回空配置 | `test_unknown_algorithm_is_rejected_not_replaced`、`test_unconfigured_algorithm_never_runs` |
| 11 | 测绘中断是否存在"先飞行、后作废"顺序错误 | **无**：平台 `confirm` 在 `_compile` 之后、任何 HTTP 提交之前调用原子网关，失败即抛出且零下发；模拟器在 `_apply` 之前调用 | `test_interruption_happens_before_the_flight_action`、`test_unavailable_gateway_blocks_the_flight_action` |
| 12 | 旧临时任务事件是否可能污染当前任务 | **否**：`temporary_task_completed` 的 `temporary_task_id` 与当前不一致即 `STALE_TEMPORARY_TASK_EVENT`；重复 `event_id` 幂等、序号倒退 `STALE_TASK_EVENT`、绑定不符 `TASK_EVENT_BINDING_MISMATCH` | `test_old_temporary_completion_is_rejected`、`test_duplicate_and_out_of_order_events_are_idempotent_or_rejected` |
| 13 | 旧确认卡是否可能跨版本继续执行 | **否**：上下文版本、幂等键、`policy_context`、权威快照哈希、确认绑定哈希任一变化即 `STALE_CONTEXT`/`POLICY_STATE_CHANGED`/`AUTHORITATIVE_STATE_CHANGED`；测绘中断后旧卡必失效 | `test_old_card_dies_after_the_interruption`、`test_route_starts_after_motion_preview` |

## 四、本轮真实执行的验证

本轮所有命令均在前台执行；每次采纳的结果都对 `src/**/*.py`、`tests/**/*.py` 做运行前后
整树哈希比对，确认结果对应同一份工作树（采纳的运行均为 `TREE_UNCHANGED`）。

### 1. Python 飞控仓（唯一离线真源）

```bash
bash tools/ci/offline_tests.sh
git diff --check
python3 -m compileall -q src tests
PYTHONPATH=src:tests python3 -m unittest -v test_path_fence test_mapping_interruption
```

```
Ran 1085 tests in 14.041s

OK
```

- `git diff --check` 无输出（退出码 0）。
- `python3 -m compileall -q src tests` 无输出（退出码 0）。
- 定向用例：`Ran 119 tests in 3.264s` / `OK`（`test_path_fence` 73 + `test_mapping_interruption` 46）。
- **分母口径**：本文与三份证据 JSON 统一使用本轮唯一分母 **1085**。该数字含
  `tests/test_offline_guard.py`（守卫内部重跑 1084 项）；此前阶段的历史分母已全部作废，
  不再作为本轮分母引用。
- 整套用例零连接尝试：`test_offline_guard` 断言整轮没有任何 socket 连接。

### 2. 跨进程 reservation 实测（本轮新增）

`tests/test_mapping_interruption.py::CrossProcessReservationTests` 用
`tests/mapping_race_worker.py` 拉起**独立 Python 进程**，共享同一个 SQLite journal 与同一个
调用记录文件；两个进程用 `--start-at` 对齐到同一时刻，并对 `uuid4` 注入确定性序列，
使它们 preview 出**同一张**确认卡（相同 `confirmation_id` / `trace_id` /
`confirmation_binding`），而不是两张内容相近的卡。

| 场景 | 进程数 | gateway 调用 | submit 调用 | 实测结果 |
| --- | --- | --- | --- | --- |
| A 同卡同键并发 confirm | 2 | 1 | 1 | 两种组合都出现过：`submitted + submitted`（输家读到既有回执）或 `submitted + unknown`（输家落在网关窗口）；reservation 最终 `mapping_interruption_succeeded` |
| B 同键不同参数（指纹不同） | 2 | 1 | 1 | 一个 `submitted`，另一个 `rejected` / `IDEMPOTENCY_CONFLICT` |
| C 网关执行中进程崩溃后重放 2 次 | 3（1 崩溃 + 2 重放） | 1 | 0 | 两次重放均为 `unknown` / `MAPPING_INTERRUPTION_UNKNOWN`，reservation 停在 `mapping_interruption_unknown`；只有显式人工核对后才能继续 |

同一场景重复 20 轮：`submitted + submitted` 18 轮、`submitted + unknown` 2 轮，
**gateway 与 submit 每轮都恒为 1 次**（20/20）。结果组合的分布随调度变化，不变量是
"至多一次"；不把某一种分布当成稳定比例。

清理结果（A/B/C 三轮实测）：

- 线程：`1 → 1`，无新增线程；
- 监听端口：`lsof -nP -iTCP -sTCP:LISTEN` 过滤 python/java/node，前后均为空；
- 临时数据库：SQLite journal 随临时目录删除（`temp_dir_removed=true`），无残留文件；
- 进程：`subprocess.communicate()` 全部回收，无遗留子进程。

机制：`SqliteCommandJournal.reserve_mapping()` 用 `BEGIN IMMEDIATE` + `scope` 主键原子取得
reservation；`advance_mapping()` 用 CAS 先把 `pending → unknown`，**之后**才调用网关，
再 CAS `unknown → succeeded/failed`。跨进程不再是"网关先于账本"的窄窗口：输家要么读到
`existing` 直接返回，要么看到 `unknown` 并明确拒绝重试。租约（默认 30 秒）只允许
**尚未调用网关**的 `pending` 被恢复方接管一次；`unknown` 是终态，只能由
`resolve_mapping_interruption(..., authority_ref=...)` 显式收口。

### 3. 前端（`lingxi-flight-agent-ui`，本轮未复跑）

以下为同日早些时候的只读结果，**不是本轮实测**，仅作历史参考；本轮结论不依赖它。

```bash
npm test
```

```
# tests 171
# pass 171
# fail 0
```

### 4. Java（`ktd_backend-real-flight-assistant`，本轮未复跑）

以下为同日早些时候的只读结果，**不是本轮实测**，仅作历史参考；本轮未改 Java 代码。

```bash
JAVA_HOME=/Library/Java/JavaVirtualMachines/zulu-8.jdk/Contents/Home \
mvn -o -s /tmp/mvn-offline-settings.xml -Dmaven.repo.local="$HOME/.m2/repository" \
    -pl biz-inspection \
    -Dtest='com.fan.framework.inspection.platformsnapshot.*Test' test
```

```
[INFO] Tests run: 53, Failures: 0, Errors: 0, Skipped: 0
[INFO] BUILD SUCCESS
```

两个环境前提（不是代码问题，后续复跑需要）：
1. **必须用 JDK 8**。默认 `JAVA_HOME` 指向 Corretto 21 时，编译在
   `com.sun.tools.javac.code.TypeTag :: UNKNOWN` 处失败（旧 Lombok 与高版本 JDK 不兼容）。
2. 离线 settings 需声明 `osgeo`/`nexus-public` 两个 repository id 并禁用
   `maven-default-http-blocker`，否则 Maven 3.9 拒绝使用本地缓存中的 `gt-geojson`。

仅跑了 `platformsnapshot` 包；`biz-inspection` 其余模块含 MySQL/Redis 集成测试，本机离线环境不具备。

### 5. 跨端隔离联调（回环假 Java，仅本机 127.0.0.1，本轮复跑）

```bash
python3 tools/ci/cross_repo_isolation.py
```

```
[PASS] S0 权威快照可读且带显式不可用声明 — unavailable=['algorithm_lifecycle', 'mapping_transaction', 'route_status', 'target_binding']
[PASS] S1 点目标坐标进入飞控上下文 — action=point_flight
[PASS] S1b 交接信封与权威快照版本一致 — snapshot=16a0eeb0…
[PASS] S2 线/面/体/混合目标全部拒绝
[PASS] S3 多点串联保留用户顺序 — ['t-3', 't-1', 't-2']
[PASS] S4 未确认草稿不产出可下发任务 — HANDOFF_DRAFT_NOT_DISPATCHABLE
[PASS] S5 飞行控制交接不创建第二个任务 — creates_task=False
[PASS] S6 旧上下文按 409 拒绝 — STALE_CONTEXT/409
[PASS] S7 同键同体幂等、同键异体冲突 — new/duplicate/HANDOFF_IDEMPOTENCY_CONFLICT
[PASS] S8 hover 返回平台声明的 typed 缺口 — ROUTE_STATE_SOURCE_UNAVAILABLE
[PASS] S8b 指点动作未被能力清单放行 — ACTION_NOT_PERMITTED
[PASS] S8c 预览阶段零飞控下发 — writes=0
[PASS] S9 围栏结论经回环 HTTP 校验 — clear/
[PASS] S10 unknown 不显示为成功且都不是设备效果 — {"accepted": "accepted", "submitted": "submitted", "someday": "unknown"}
[PASS] S11 服务结束后端口已关闭 — port=56106
[PASS] S11b 无遗留监听线程 — threads=1/1
[PASS] S11c 全程未触碰真实平台写接口 — writes=0

全部通过：17/17（审计事件 3 条）
```

**并发注意**：`lingxi-agent` 工作树可能有其他会话在动（此前观察到 HEAD 在数分钟内
`860d9ed`→`dc34ad8`→`1bc9e16` 变化、`app/runtime/flight_handoff.py` 一度消失又出现）。
上面的 17/17 是**本轮**在该仓当前工作树上跑出的结果，下一次可能因对方改动而不同；
本轮未写入该仓任何文件。脚本全程 127.0.0.1 回环，假 Java 只返回合成数据，真实写接口 0 次。

### 6. 与 V1.3 证据的关系

`docs/ACCEPTANCE_FLIGHT-V13.md` 及其 `docs/evidence/product-v13-*` **未修改、未删除**。
V1.3 的 807/800/787 等历史分母、截图与 JSON 保持原样，仅作为历史记录存在。

## 五、证据分层

分层口径固定为六层：**单元测试 → 模拟器 → 假传输 → 回环隔离 → 真实平台未验证 → 真实设备未验证**。
低层证据不能升级为高层结论；`clear`、`submitted`、`accepted` 都只在本层成立。

| 层 | 覆盖内容 | 本轮证据（分母 1085） | 能证明 / 不能证明 |
| --- | --- | --- | --- |
| 单元测试 | 权威字段契约、目标几何、AI 白名单、交接信封、围栏契约、回执状态机、reservation 状态机 | `test_authority_contract` 19、`test_target_resolver` 15、`test_ai_lifecycle` 31、`test_handoff_contract` 41、`test_path_fence` 73、`test_receipt_contract` 15、`test_task_events` 15、`test_ktd_snapshot` 31 | 只证明确定性规则与状态机；不证明任何外部系统行为 |
| 模拟器 | 控制权无豁免、悬停≠急停、航线中微操拦截、参数三列、测绘六步、临时任务/低电量归约 | `test_product_v13` 29、`test_v13_hover_and_point` 13、`test_product_v13_actions` 10、`test_mapping_interruption` 46（含跨进程 5 项） | 只证明内存执行闭环；回执 `execution_mode=simulator`，不是设备执行 |
| 假传输 | 平台编译、确认卡、回执映射、零下发、`MAPPING_INTERRUPTION_*`、reservation CAS | `test_ktd_platform` 33、`test_platform_runtime` 9、`test_vendor_control` 16、`test_receipt_contract` 15 | 只证明协议形状与错误路径；假 transport 不产生真实网络等待 |
| 回环隔离 | 规划→飞控交接、快照读取、围栏回环、幂等/冲突、端口与线程回收 | `tools/ci/cross_repo_isolation.py` 本轮 17/17，127.0.0.1:56106，`writes=0` | 只证明本机假 Java 协议闭环；不是共享 test/uat/prod |
| 真实平台 | 平台是否返回 `route_status`/`task_batch_id`/`device_limits`、围栏折线/`altitude`/`converge`、测绘原子事务、事件投递、gateway 幂等键 | **未验证** | 不能从上面任何一层推出 |
| 真实设备 | 位移、悬停保持、载荷动作、避障、回执准确性 | **未验证** | `device_effect` 恒 `not_verified`，`device_completion_verified()` 恒 False |

**HTTP 2xx、健康检查、平台 `submitted`、模拟器 `accepted` 都不等于真实飞行成功。**
隔离测试通过、回环 17/17、`submitted` 回执、`simulator accepted` 都不能写成"设备已执行"。

## 六、未实现 / 未验证 / 需要外部提供

| 项 | 现状 | 需要谁提供 |
| --- | --- | --- |
| 平台 `route_status` | Java 快照恒 null，已显式声明不可用 | 平台团队：遥测原帧中的航线状态事实 |
| 静态标注解析接口 | 本仓只有 `TargetResolver` 协议，无默认实现 | 平台团队：标注查询接口与目标版本字段 |
| 算法粒度启停 | 平台只有任务级 `videoDetect` 总开关 | 平台团队：按算法启停 + 运行回执 |
| 测绘作废原子事务 | 六步是内存模拟；真实事务边界未验证 | 平台团队：拼图/断点/恢复的事务接口 |
| 测绘网关幂等 | 本地 reservation 已保证**同机多进程**至多一次；平台侧幂等未验证 | 平台团队：作废接口是否接受幂等键（见 [P5](ACCEPTANCE_FLIGHT-V14-P5-MAPPING-RESERVATION.md)） |
| 设备回执→设备效果 | 恒 `not_verified` | 平台团队：独立测量或到位确认 |
| 真实任务事件桥 | 默认 `TASK_EVENT_SOURCE_UNAVAILABLE` | 平台团队：`event_id`/序号/投递语义 |
| 规划 Agent 主链路接线 | `flight_handoff.py` 当前不在工作树 | 任务下发 Agent 负责人 |
| 前端真实数据接入 | `src/v14` 是展示层，数据由宿主注入 | 前端 + 后端联调 |
| 指点/环绕是否属 2.2.3 拦截集 | 按 4.1.1 保留临时任务语义 | 产品确认规范解释 |

**剩余限制（必须与结论一起读）**：

1. **真实平台测绘原子事务未验证**。六步作废只在内存模拟与假网关中执行；
   真实平台是否提供 `atomic + confirmed` 的作废接口、失败时是否部分落地，均未知。
2. **真实 gateway 是否支持 idempotency key 未验证**。本仓不假设支持：
   `supports_idempotency_key` 默认 `False`，跨进程至多一次只由本机 SQLite reservation 保证，
   不是平台侧幂等证明。
3. **DNS 解析不受 socket timeout 约束**。`getaddrinfo` 发生在 `open()` 之前；socket 超时
   只覆盖连接建立、TLS 握手与响应读取。围栏/平台请求的 DNS 阻塞仍是已知残余风险。
4. **不得把低层证据写成设备成功**：隔离测试通过、回环 17/17、HTTP 2xx、平台 `submitted`、
   模拟器 `accepted` 都**不是**设备已执行；`device_effect` 恒 `not_verified`，
   `device_completion_verified()` 恒 False。
5. **跨主机一致性未验证**：reservation 只在同一台机器的 SQLite 文件上验证；
   共享文件系统/多主机部署下的事务语义未验证。

## 七、Git 状态、改动文件与回滚

- 分支 `test`，HEAD `d6d284d04b38946e92ca5a10df244a2db487b3b0`（本轮未提交、未 push）。
- 本轮相对 P4 追加/修改的文件（全部为未提交改动）：
  - `src/lingxi_flight_control/authority_contract.py`（新增 `ROUTE_GATED_ACTIONS`，`route_status` 进 core）
  - `src/lingxi_flight_control/handoff_contract.py`（`CAPABILITY_ACTIONS['route_status']` 扩到 8 个微操）
  - `src/lingxi_flight_control/cockpit.py`（提示词：悬停不得替换为急停）
  - `src/lingxi_flight_control/flight_policy.py`（provider 契约三层分类；协作式不进平台路径）
  - `src/lingxi_flight_control/ktd_path.py`（`hard_bounded` 取决于传输层是否接收单次预算）
  - `src/lingxi_flight_control/ktd_platform.py`（`mapping_reservations` 表、`reserve_mapping`/`advance_mapping`、confirm 顺序重排、`resolve_mapping_interruption`）
  - `src/lingxi_flight_control/mapping_interruption.py`（reservation 状态常量、`supports_idempotency_key`、回执 `confirmed` 语义）
  - `tests/test_authority_contract.py`、`tests/test_ktd_snapshot.py`、`tests/test_product_v13.py`
  - `tests/test_path_fence.py`、`tests/fence_support.py`、`tests/test_ktd_path.py`
  - `tests/test_mapping_interruption.py`、`tests/mapping_race_worker.py`（跨进程 reservation）
  - `tests/test_platform_runtime.py`、`tests/test_vendor_control.py`（合成夹具 `route_status` 由 null 改为 `'idle'`）
- 工作区另有 P1–P5 的未提交新增模块、夹具、文档与证据（`git status --porcelain` 共 56 条），
  以及未跟踪的 `.workbuddy-ai/`，全部保留。
- 回滚：本轮改动为**新增模块/常量 + 追加校验 + 文案 + 测试**，可按文件正向 revert；
  回滚 reservation 会恢复「跨进程网关可能调用两次」的旧风险。已发出的平台指令无法用代码回滚撤销。
- 未合入共享 test/uat/prod，未部署，未重启共享服务。

## 八、资源清理

- 本轮所有命令均为前台执行并已返回：`offline_tests.sh`、`git diff --check`、`compileall`、
  定向 `unittest`、跨进程探针、回环隔离脚本（17/17 后自行关闭端口）。
- 无新增监听端口：`lsof -nP -iTCP -sTCP:LISTEN` 过滤 python/java/node 为空。
- 无后台进程/子进程残留：跨进程探针的 `subprocess.communicate()` 全部回收；
  `threading.active_count()` 前后均为 1。
- 未建立 MQTT 连接、未调用共享平台写接口（离线守卫断言零 socket 连接）。
- 未产生临时凭据、Cookie、私钥；未把构建产物加入 Git（`git status` 无 dist/、无 wheel、无日志）。
- 本轮临时文件只有 `/tmp/v14_offline*.log`、`/tmp/v14_targeted*.log`、`/tmp/v14_full_verbose.log`、
  `/tmp/v14_reservation_probe.py`、`/tmp/v14_race_20.py` 与 `/tmp/wait_tree_stable.sh`，不含凭据，可随时删除。

## 九、收口自检

| 检查项 | 结果 |
| --- | --- |
| 文档数字统一 | 四份文档与三份证据 JSON 均使用 **1085**（定向 **119**）；无旧分母残留 |
| V1.4 DOCX SHA256 | `e4e42b712e3249d1fc09d0968445525c0702808e894290d49df2b70301108c78`（本轮重新计算） |
| 分支 / HEAD | `test` / `d6d284d04b38946e92ca5a10df244a2db487b3b0`，三份 JSON 与文档一致 |
| 敏感信息 | 未写入 token、密码、Cookie、私钥；证据只有计数、状态码与合成数据 |
| 后台进程 / 监听端口 | 无 |
| Git status 与证据一致 | 未提交、未 push；工作区保留全部 P1–P5 改动 |
