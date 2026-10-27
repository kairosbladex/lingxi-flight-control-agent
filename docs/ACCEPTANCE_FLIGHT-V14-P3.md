# FLIGHT-V14 第三阶段：AI 生命周期与测绘中断安全契约

2026-10-22。依据《灵犀平台自然语言智能控制功能设计规范 V1.4》2.1、3.3、4.1 与本次要求，
补齐**算法粒度 AI 生命周期**与**测绘中断原子契约**，并把 AI 冲突追问接进自然语言入口。
默认执行器仍是模拟器，`KtdPlatformExecutor` 仍默认关闭；未连接 MQTT、真实无人机或共享
test/uat/prod 服务，未执行任何平台写操作，未接入任何真实算法、图像识别或地图标注接口。

## 基线

- 仓库 `lingxi-flight-control-agent`，分支 `test`，HEAD `d6d284d`（`feat: 补齐悬停动作与指点/环绕平台映射`）。
- 工作区保留第一、二阶段全部未提交改动（`authority_contract.py`、`target_resolver.py`、
  `task_events.py`、`path_geometry.py`、`flight_policy.py` 等）与未跟踪的 `.workbuddy-ai/`，
  未做任何回退，未使用 `reset --hard`、`checkout --`、`clean`、`stash`。
- 测试分母统一按本轮最终实测 **1085**（见主审计 `ACCEPTANCE_FLIGHT-V14.md`）；此前阶段的
  旧分母已作废，不再作为分母引用。

## 改前审计

| 面 | 改前状态 | 缺口 |
| --- | --- | --- |
| AI 生命周期 | 模拟器只有 `active_algorithms` 一个集合，由「全局 ∪ 活动标注」直接算出 | 没有阶段区分：配置、下发、启动请求、启动回执塌缩成同一个集合，无法证明算法「真的在运行」 |
| 算法启停边界 | 无 | 没有可注入网关，也没有 typed unavailable；平台 `ai_algorithm_set` 只返回泛化 `ACTION_UNSUPPORTED` |
| AI 冲突追问 | `v13_extension.parse_command` 有 `同时开启/仅替换` 的语法 | 飞控 Agent 的自然语言入口完全没有冲突判定，冲突时直接生成确认卡 |
| 测绘中断 | `mapping_interruption()` 判定后，模拟器在 `_apply` 里顺手改几个字段；平台在 `_compile` 直接拒绝 | 没有原子业务操作、没有步骤审计、没有「失败即不下发」的显式契约；平台 preview 直接拒绝，用户看不到成果失效提示 |
| 临时任务 | `task_events` 有替换/完成归约 | 没有「恢复主线 / 结束全部」的显式选择契约，也没有禁止系统猜默认值 |
| 标注事件 | 有 `event_id` 幂等与序号单调 | 没有 `annotation_version`；重复 entered/left 仍推进 revision |

## 一、AI 生命周期 typed 契约

新增 `src/lingxi_flight_control/ai_lifecycle.py`（`lingxi-ai-lifecycle/v1`），**不含任何模型识别逻辑**，
只描述「哪个算法应当在哪个标注上运行」以及「平台是否确认它在运行」。

六个阶段严格分开记录，互不冒充：

| 阶段 | 含义 | 数据来源 |
| --- | --- | --- |
| `expected` | 标注配置的期望值 | `AnnotationAlgorithmConfig` / `AnnotationAlgorithmGateway` |
| `dispatched` | 任务下发时写入任务 AI 配置的集合 | 全局 ∪ 活动标注，去重后限定项目配置集合 |
| `start_requested` | 到达标注后发出的启动请求 | `AlgorithmLifecycleGateway.start()` 调用 |
| `started` | 平台确认已启动 | 回执 `confirmed=True` 且 `stage=started` |
| `stop_requested` | 离开标注后发出的停止请求 | `AlgorithmLifecycleGateway.stop()` 调用 |
| `stopped` | 平台确认已停止 | 回执 `confirmed=True` 且 `stage=stopped` |

**只有 `started` 才进入 `state.active_algorithms`。** 请求被受理、配置被写入都不算运行中
（`test_start_request_alone_never_claims_running` 用一个「只受理不确认」的网关证明这一点）。

边界与默认实现：

- `AnnotationAlgorithmGateway`：读标注配置期望值。默认 `UnavailableAnnotationAlgorithmGateway`
  抛 `ANNOTATION_ALGORITHM_SOURCE_UNAVAILABLE`，**不返回空配置**（空配置会被误读成「这个标注没有 AI」）。
- `AlgorithmLifecycleGateway`：算法粒度启停。默认 `UnavailableAlgorithmLifecycleGateway`，
  任何启停都返回 `confirmed=False` + `ALGORITHM_LIFECYCLE_UNAVAILABLE`。
- `SimulatorAlgorithmLifecycleGateway`：完整测试 seam，维护进程内「哪些标注上哪些算法在运行」，
  每条回执都带 `execution_mode=simulator`。

确定性集合规则（`annotation_active_set` / `algorithms_to_stop`）：

- 算法只能来自当前项目 `configured_algorithms`；未配置算法整批拒绝，不静默忽略、不替换成近似算法。
- 全局算法与标注算法去重。
- 离开标注只移除**该标注来源**：若全局勾选、或其他仍在活动的标注引用同一算法，则**不发停止请求**。
- 体标注（`AnnotationGeometry.BODY`）在契约层直接 `BODY_ANNOTATION_AI_UNSUPPORTED`，
  且不能靠「配置为空」绕过；点/线/面允许配置，配置可以为空。

模拟器侧（`full_control.MemoryFlightControlSimulator`）：

- 新增 `algorithm_gateway=` 注入点，默认 `SimulatorAlgorithmLifecycleGateway()`。
- `_refresh_algorithms()` 改为「先算期望集合 → 按来源关闭 → 按来源启动 → 用已确认回执重建
  `active_algorithms`」。启动/停止失败时保留「可能在运行」的判断，绝不假装已关闭。
- `algorithm_lifecycle()` 只读审计：六阶段、执行模式、抑制集合、错误码。**不含坐标、半径或响应原文。**
- 换绑（`bind`）时清空运行集合：新绑定上不存在任何已确认的启动回执。

## 二、AI 冲突追问

`plan_selection()` 把「开启某算法」变成确定性结论：

- `requested` 含未配置算法 → 整批 `ALGORITHM_NOT_CONFIGURED`，**不存在部分绑定后返回成功**。
- 与**当前活动标注**的算法冲突且未给出模式 → 返回追问 `是同时开启还是仅替换？`，
  调用方不得生成确认卡。
- `mode='add'`（同时开启）→ 全局并集，标注算法继续有效。
- `mode='replace'`（仅替换）→ 本会话抑制冲突的标注算法（`suppressed`），只保留请求集合。
  该能力由 `select_algorithms(mode='replace')` 提供，**没有**注册成模型工具，也没有改动现有动作契约。

自然语言入口（`cockpit.CockpitService`）：

- 快路径解析出 `ai_algorithm_set` 后先做冲突判定；冲突时返回
  `status=clarification` + `ALGORITHM_CONFLICT_CLARIFICATION_REQUIRED`，**不产生卡片、不改状态**。
- 追问后的显式回答只认两句固定表达（`同时开启` / `仅替换`），不做模糊匹配：
  - `同时开启` → 复用被追问的原指令生成卡片；待答词不参与 grounding。
  - `仅替换` → 返回 `ALGORITHM_REPLACE_UNSUPPORTED`，**不生成卡片**，因为改写标注自身的 AI 配置
    属于地图标注库写操作，飞控助手不越权。
- 冲突判定必须早于并发守卫，否则「同时开启」会被误判成并发执行请求。
- 缺少标注状态时 `conflict_clarification()` 返回 `None`：不编造冲突，也不把「没有数据」当成放行依据，
  真正执行仍由执行器的算法白名单把关。

## 三、测绘中断原子契约

新增 `src/lingxi_flight_control/mapping_interruption.py`（`lingxi-mapping-interruption/v1`）。

固定六步，顺序不可颠倒（`INTERRUPTION_STEPS`）：

1. `stop_auto_stitching` 停止自动拼图
2. `void_rebuild_type` 作废 `rebuildType`
3. `void_break_info` 作废 `breakInfo`
4. `invalidate_task_breakpoint` 作废原测绘任务断点
5. `disable_route_resume` 关闭恢复航线能力
6. `block_task_resume` 永久禁止恢复原测绘任务

`MappingInterruptionGateway` 是唯一入口：

- `UnavailableMappingInterruptionGateway`（默认）：`atomic=False` + `MAPPING_INTERRUPTION_UNAVAILABLE`，
  不产生任何副作用，文案明确「本次不下发任何飞控动作」。
- `SimulatorMappingInterruptionGateway`：`confirmed=False` 时返回
  `MAPPING_INTERRUPTION_NOT_CONFIRMED` 且不动状态；任一步异常返回 `MAPPING_INTERRUPTION_PARTIAL`
  并给出 `failed_step` 与已完成步骤。

接入点：

- **模拟器 `confirm`**：在 `_apply` 之前调用网关。未确认时**根本不进入**；原子失败时撤销待确认卡、
  返回 `MAPPING_INTERRUPTION_UNAVAILABLE`，动作不执行（位姿不变）。回执新增
  `mapping_interruption` 字段，记录 `atomic`、`completed_steps`、`execution_mode`、`audit_id`。
- **`KtdPlatformExecutor`**：新增 `mapping_gateway=` 注入点，默认不可用。
  `preview` 改为「返回带成果失效警告的确认卡」而不是静默拒绝（V1.4 4.1.1 要求先展示提示）；
  `confirm` 在**任何 HTTP 提交之前**调用网关，失败即 `MAPPING_INTERRUPTION_UNAVAILABLE`。
  `available_actions()` 仍用 `interruption_pending=False`，能力探测不会触发中断。
- **`ZhuoyiPlatformExecutor`**：保持原有更严格的 fail-closed（preview 即拒绝），未改为「先警告后中断」。
  这是与 Ktd 执行器的已知差异，见下方限制。

载荷动作不是测绘中断：`NON_INTERRUPTING_PAYLOAD_ACTIONS` 显式列出拍照、录像、全景、变焦、
镜头切换、云台、喊话、音量、灯光、载荷开关、红外、AI 开关、恢复类与只读动作；
`interruption_required()` 先查这张表，并有测试断言它不触发中断、不作废测绘。

作废后的稳定性：

- `route_resume` / `continue_from_breakpoint` 在 `check_flight_policy` 与模拟器
  `_route_precondition_error` 两处都检查 `mapping_invalidated` 与新增的 `route_resume_disabled`，
  固定返回 `MAPPING_NOT_RESUMABLE`。
- `route_resume_disabled` 进入 `_policy_context` 与 `KtdSnapshot` 确认哈希链，
  中断前生成的旧确认卡在中断后必然失效（测试用「先预览另一张卡、确认中断后再确认旧卡」证明）。

## 四、临时任务与低电量

与第一阶段事件契约对接，新增 `post_temporary_decision()` / `validate_post_temporary_choice()`：

- `route_status == 'temporary_completed'` 时必须返回
  `{required: True, options: ['resume_main', 'end_all'], default: None}`。
- 未选择（`None`）→ `TEMPORARY_DECISION_REQUIRED`；非法取值 → `INVALID_TEMPORARY_DECISION`；
  断点已失效时选 `resume_main` → `TEMPORARY_RESUME_UNAVAILABLE`。**系统不猜默认分支。**
- 模拟器 `resolve_temporary_outcome(choice)`：`resume_main` 恢复有效主线断点，`end_all` 清空断点。

低电量与充电：

- `low_battery_return` 仍是最高优先级：保留可续飞断点、丢弃未完成临时任务、进入返航；
  返航期间临时任务替换/完成一律 `LOW_BATTERY_RETURN_PRIORITY`。
- `recharged_online` 只在返航后成立；恢复条件收紧为「有 route_id + 未作废 + 有断点 +
  未关闭恢复能力」，测绘已作废或结束全部任务后固定 `ended`。

事件幂等与顺序（`task_events` 增强）：

- 新增可选 `annotation_version`；比已见版本更旧的事件返回 `STALE_ANNOTATION_VERSION`。
- `reduce_event()` 返回「是否真的变化」；重复 `entered`/`left` 不再推进 `revision`，
  但不同 `event_id` 的重复投递仍计入 `applied`（与 `event_id` 去重区分开）。
- 序号倒退仍 `STALE_TASK_EVENT`，绑定不符仍 `TASK_EVENT_BINDING_MISMATCH`。

## 五、测试

新增 `tests/test_ai_lifecycle.py`（31 项）与 `tests/test_mapping_interruption.py`（23 项），合计 54 项：

| 要求 | 用例 |
| --- | --- |
| 全局/标注算法去重 | `test_global_and_annotation_algorithms_dedupe`、`test_six_stages_are_recorded_separately` |
| 两个标注共享算法时离开一个不关闭 | `test_two_annotations_sharing_algorithm_keep_it_after_one_leaves`、`test_shared_algorithm_between_two_annotations_is_not_stopped` |
| 全局算法防误关 | `test_global_algorithm_survives_leaving_the_annotation`、`test_globally_selected_algorithm_is_never_auto_closed` |
| 重复和乱序事件 | `test_duplicate_and_out_of_order_events_are_idempotent_or_rejected`（含旧 `annotation_version`） |
| 体标注拒绝 | `test_annotation_geometry_gate` |
| 未配置算法拒绝 | `test_unknown_algorithm_is_rejected_not_replaced`、`test_unconfigured_algorithm_never_runs` |
| 请求≠运行 | `test_start_request_alone_never_claims_running` |
| AI 冲突追问 | `test_conflict_requires_clarification_before_any_card`、`test_conflicting_ai_requires_clarification_without_a_card`、`test_add_answer_continues_with_a_card`、`test_replace_answer_produces_no_card` |
| 多算法原子 | `test_multi_algorithm_selection_is_atomic`、`test_clarification_plan_cannot_be_applied` |
| 测绘干预首次 preview 返回警告 | `test_first_preview_warns_and_changes_nothing`、平台同名用例 |
| 未确认不执行 | `test_unconfirmed_interruption_does_nothing` |
| 确认后原子中断成功 | `test_confirmed_interruption_runs_all_six_steps_in_order`、`test_interruption_happens_before_the_flight_action` |
| 原子能力不可用时不发送飞控动作 | `test_unavailable_gateway_blocks_the_flight_action`、平台 `test_platform_mapping_interruption_requires_real_atomic_contract` |
| 部分失败可见 | `test_partial_failure_is_reported_not_silently_ignored` |
| 作废后恢复拒绝 | `test_invalidated_mapping_can_never_be_resumed` |
| 载荷不误判 | `test_payload_actions_are_not_mapping_interruptions`、`test_payload_actions_do_not_invalidate_mapping` |
| 旧卡失效 | `test_old_card_dies_after_the_interruption` |
| 临时任务替换与旧事件拒绝 | `test_new_temporary_task_replaces_old_and_keeps_main_breakpoint`、`test_old_temporary_completion_is_rejected` |
| 低电量返航与充电恢复 | `test_low_battery_return_has_highest_priority`、`test_recharge_restores_only_the_valid_inspection_mainline` |
| 结束全部任务清空断点 | `test_end_all_choice_clears_the_breakpoint`、`test_resume_is_refused_after_mapping_invalidation` |
| 平台无算法级接口 | `test_platform_has_no_algorithm_level_lifecycle` |

```bash
bash tools/ci/offline_tests.sh   # Ran 1085 tests in 14.041s / OK，含 socket 禁用守卫
git diff --check                 # 无输出（退出码 0）
python3 -m compileall -q src tests                       # 无输出（退出码 0）
```

本轮唯一分母为 **1085**；此前阶段的旧分母已作废，不再引用。

### 证据分层

| 结论 | 等级 |
| --- | --- |
| 六阶段分离、去重/防误关、体标注拒绝、未知算法整批拒绝、冲突追问与原子计划 | 单元验证 |
| 模拟器到离生命周期、临时任务替换、低电量/充电归约、测绘六步原子中断 | **模拟器已验证** |
| 平台「先中断后下发」顺序、`ai_algorithm_set` → `ALGORITHM_LIFECYCLE_UNAVAILABLE`、零下发 | **仅假 Java/HTTP 验证** |
| 平台是否存在算法级启停接口 | **真实平台接口缺失**（只有任务级 `videoDetect` 总开关，不能冒充） |
| 真实算法运行/停止回执 | **真实 AI 运行回执缺失**（模拟器回执均标 `execution_mode=simulator`） |
| 六步是否真的在数据库事务内完成、失败能否回滚 | **真实测绘数据库事务未验证** |
| 标注 AI 配置读取接口 | **未接入**（默认网关抛 `ANNOTATION_ALGORITHM_SOURCE_UNAVAILABLE`） |
| 标注到离事件的平台投递语义 | **未验证真实平台** |
| 任何真实设备动作、算法准确率、测绘成果 | **未验证真实设备** |

## 已知限制

1. **`仅替换` 未落地为可执行动作**：`plan_selection(mode='replace')` 与
   `select_algorithms(mode='replace')` 已实现并有测试，但它需要「抑制标注算法」这一语义，
   现有 65 项动作契约里没有对应参数。自然语言入口对 `仅替换` 返回
   `ALGORITHM_REPLACE_UNSUPPORTED` 并明确说明需先在标注配置中调整，**不越权改写标注配置**。
2. **`ZhuoyiPlatformExecutor` 仍是 preview 即拒绝**：比 Ktd 更严格（符合「默认不可中断」），
   但没有「先展示成果失效再原子中断」的路径。两者差异已在此记录，未擅自放宽。
3. **测绘六步是内存模拟**：`rebuild_type` / `break_info` 是模拟器侧占位字段，用于表达
   「这两项必须被作废」；真实 Java 持久化字段的形状与事务边界未验证，没有据此编造接口。
4. **`annotation_version` 由事件方提供**：本仓只做单调校验，不推断版本来源；
   平台事件是否携带该字段未验证。
5. 全局算法（`ai_algorithm_set`）仍是任务级开关，属于阶段 2「已下发配置」；
   它不会被计入阶段 4「已启动回执」。

## 行为变化与影响面

- `KtdPlatformExecutor.preview` 在测绘任务上从「直接拒绝」改为「返回带成果失效警告的确认卡」，
  `confirm` 仍拒绝且零下发。`tests/test_product_v13.py` 的同名用例已同步更新，保留零下发断言。
- `FlightControlState` 新增 `rebuild_type`、`break_info`、`route_resume_disabled` 三个字段，
  进入 `to_dict()` 与 `_policy_context`；`FlightControlReceipt` 新增 `mapping_interruption`。
  这是本阶段唯一的状态/回执形状变化，均为追加字段，没有改动既有字段含义。
- `check_flight_policy` 的 `MAPPING_NOT_RESUMABLE` 条件增加 `route_resume_disabled`。
- `reduce_event` 返回布尔值；`apply_task_event` 在「合法但无变化」时不推进 `revision`。
- 未改动默认执行器、动作注册表（仍 65 项）、preview→confirm→execute 链路、控制权校验、
  幂等键与上下文版本机制。

## Git 与回滚

- 基于 `test@d6d284d` 在当前工作树修改，**未 push**、未合入共享 test、未部署、未重启共享服务。
- 保留 `.workbuddy-ai/` 与第一、二阶段全部未提交改动；未使用 `reset --hard`、`checkout --`、`clean`、`stash`。
- 回滚：本阶段为新增模块 + 追加字段，按提交正向 revert 即可。已提交的平台指令无法用代码回滚撤销。
- 机器证据：[v14-ai-mapping-20261022.json](evidence/v14-ai-mapping-20261022.json)。
