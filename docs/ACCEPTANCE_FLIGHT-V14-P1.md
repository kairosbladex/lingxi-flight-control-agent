# FLIGHT-V14 第一阶段：真实平台契约边界

2026-10-22。依据《灵犀平台自然语言智能控制功能设计规范 V1.4》与本次步骤 0 的审计，补齐四类 typed 契约：权威平台快照、静态标注目标解析、任务事件、设备回执。
默认执行器仍是模拟器，`KtdPlatformExecutor` 仍默认关闭；未连接 MQTT、真实无人机或共享 test/uat/prod 服务，未执行任何平台写操作。

## 基线

- 仓库 `lingxi-flight-control-agent`，分支 `test`，HEAD `d6d284d`（`feat: 补齐悬停动作与指点/环绕平台映射`）。
- 工作区改动前只有未跟踪的 `.workbuddy-ai/`，本次完整保留。
- 测试分母统一按本轮最终实测 **1085**（见「验证」）；此前阶段的旧分母已作废，不再引用。

## 审计结论（改前）

| 契约面 | 改前状态 | 缺口 |
| --- | --- | --- |
| 权威快照 | `KtdSnapshotProvider` 严格校验旧 v1 的 8 个 state 键、绑定、版本、控制权、租约与逐动作可用性 | 不接受 `route_id`（出现即被判为未知字段），没有字段级审计，`task_batch_id` 只存在于 Agent 绑定；执行器侧对“字段在但为 null”没有统一拒绝点 |
| 目标解析 | `parse_command` 只抽取意图与原始目标名，坐标指令直接带经纬度 | 没有可注入的 typed 目标解析器；同名、体标注、混合类型、缺高度、缺版本都没有稳定错误码 |
| 任务事件 | 模拟器 `apply_task_event` 支持 6 种字符串事件 | 没有 `event_id`/序号/幂等，乱序无法拒绝，缺 `temporary_task_replaced` 与 `mapping_invalidated`，低电量优先级未覆盖临时任务替换 |
| 设备回执 | `command_receipts` 有 10 个线格式状态与跨进程版本/冲突校验 | 没有规范状态枚举与重试策略，`unknown` 只能靠字符串判断；查询前未显式复核绑定；`ZhuoyiPlatformExecutor` 把 `unknown` 映射成 `rejected` |

## 一、权威平台快照

新增 `src/lingxi_flight_control/authority_contract.py`：

- `FIELD_GROUPS` 把 V1.4 要求的字段显式分组：identity（含 `task_batch_id`）、version、control、flight、task、route、mapping、temporary、battery、pose、device、capability。
- `audit_snapshot()` 给出字段级状态：`present` / `unknown`（在但为 null）/ `absent`（旧 schema 没有）/ `unbound`（Agent 会话事实）/ `invalid`。
- `requirements_for(action)` 按动作给出 `core` 与 `extension` 两组要求：`core` 是旧 v1 已声称返回的字段，`extension` 是 V1.4 新字段。
- `require_authority()` 只对 `unknown`/`invalid` 报 `AUTHORITATIVE_STATE_INCOMPLETE`；`absent` 不在这里拒绝，由执行器现有的 `FLIGHT_STATE_UNAVAILABLE` / `ROUTE_STATE_UNAVAILABLE` / `FLIGHT_POSE_UNAVAILABLE` 决定，避免把“旧 schema 没这个字段”误判成“平台说不知道”。
- `read_state` 不要求任何字段：离线时也要能显示“状态未知”，但这条路径不授予写权限。

`ktd_snapshot.py`：

- 新增可选 `route_id`（空串按坏数据拒绝）、可选 `task_batch_id` 平台回显；出现时必须是合法标识，回显与当前绑定不一致按 `RESOURCE_BINDING_MISMATCH` 拒绝。
- `breakpoint_index` 存在但 `route_id` 缺失按 `INVALID_POLICY_SNAPSHOT` 拒绝：断点必须挂在具体航线上。
- 基础 state 键缺失改为 `AUTHORITATIVE_STATE_INCOMPLETE`（原来只是通用响应错误），不补默认值。
- 未放宽旧 v1 schema：`route_id` / `task_batch_id` 都是“出现才校验”的兼容扩展，缺失仍是合法旧快照。

`ktd_platform.py`：

- `KtdSnapshot` 追加默认 `task_batch_id=None`（不改变位置参数顺序），`_validate_snapshot` 校验回显绑定并调用 `require_authority`。
- `_state()` 透出 `route_id` 与 `task_batch_id`；新增只读 `authority_contract(action)` 返回字段审计与是否被阻断。
- 确认哈希使用 `asdict(snapshot)`，因此 `route_id`、`breakpoint_index`、`task_type`、`mapping_invalidated`、`auto_stitching`、`temporary_task_id`、`low_battery_return`、`device_limits` 变化都会让旧确认卡失效（已有 `AUTHORITATIVE_STATE_CHANGED` 路径，本次补测试）。

`zhuoyi_platform.py`：`read_state` 透出 `route_id`；状态映射区分 `unknown/conflict/dispatching` 等，不再把未知结果说成 `rejected`。

## 二、静态标注目标解析

新增 `src/lingxi_flight_control/target_resolver.py`，只定义边界，不实现地图标注库：

- `TargetQuery`（project/actor/binding/intent/name 或 target_id，二选一）、`TargetCandidate`、`ResolvedTarget`（记录 `source`、`version`、`binding`、`intent`）。
- `TargetResolver` 为可注入 Protocol；本仓不提供默认实现，`resolve_target()` 在适配器缺失或异常时返回 `TARGET_RESOLVER_UNAVAILABLE`，不按围栏“降级”。
- 稳定错误码：`TARGET_NOT_FOUND`、`TARGET_AMBIGUOUS`（附候选，不自动挑）、`UNSUPPORTED_TARGET_GEOMETRY`（体标注、线/面用于飞行中指点/环绕）、`MIXED_TARGET_UNSUPPORTED`、`TARGET_ALTITUDE_REQUIRED`（点目标无有效高度，不填默认高度）、`TARGET_VERSION_UNAVAILABLE`、`TARGET_BINDING_MISMATCH`、`INVALID_TARGET_QUERY`、`MULTI_TARGET_UNSUPPORTED`。
- `parse_command` 仍只抽意图与原始目标；`query_from_command()` 只搬运名称，坐标指令直接拒绝走解析（避免把占位名“坐标目标”当真实标注）。
- 线/面仅在 `ground_plan` 意图下可取；飞行中只接受单点目标；多目标请求显式拒绝，不在本仓拆成多个真实动作。
- `parse_command` 的坐标形式按 V1.4 4.3.1 原文允许省略“米”（`去东经118，北纬34，高50`）。

边界说明：本仓的飞行执行链仍由 Java 预览接口解析目标，本次没有把目标解析器接入真实下发路径——接入需要平台侧目标接口契约，尚无可核验来源。

## 三、任务事件

新增 `src/lingxi_flight_control/task_events.py`：

- `TaskEventKind` 覆盖 8 类事件：`temporary_task_replaced`、`temporary_task_completed`、`low_battery_return`、`recharged_online`、`annotation_entered`、`annotation_left`、`all_tasks_ended`、`mapping_invalidated`。
- `TaskEvent` 强制绑定 `project/hangar/drone/task_batch`，必须有 `event_id`、单调 `sequence`，可选 `state_version`；临时任务事件必须带 `temporary_task_id`，标注事件必须带 `annotation_id`。
- `TaskEventLedger`：重复 `event_id` 幂等（不重复改状态、不推进 revision），`sequence <= last` 按 `STALE_TASK_EVENT` 拒绝。
- 归约规则：低电量返航最高优先级（丢弃临时任务、保留主线断点）；返航期间拒绝临时任务替换/完成（`LOW_BATTERY_RETURN_PRIORITY`）；旧临时任务完成事件按 `STALE_TEMPORARY_TASK_EVENT` 拒绝；`recharged_online` 只在返航后成立且不恢复已作废测绘；`mapping_invalidated` 清断点、关自动拼图、永久禁止恢复；`all_tasks_ended` 清断点与临时任务。
- `apply_task_event()` 为模拟器内部入口（非模型工具）：`full_control.MemoryFlightControlSimulator.apply_task_event` 改为走同一账本与归约，保留旧字符串事件名兼容。
- `UnavailableTaskEventBridge` 是真实事件桥的默认实现：读取时返回 `TASK_EVENT_SOURCE_UNAVAILABLE`，不返回空列表（空列表会被误读成“没有事件，所以状态没变”）。

## 四、设备回执

`command_receipts.py`：

- 新增规范状态 `DeviceReceiptState`（`dispatching/submitted/accepted/in_progress/succeeded/failed/cancelled/expired/unknown/conflict`）与线格式映射；`completed` → `succeeded`。未知线格式直接拒绝，不映射成 `unknown` 或 `accepted`。
- `retry_allowed()` 只对明确 `failed` 返回真；`unknown`/`expired`/`conflict`/进行中一律禁止自动重试。`device_completion_verified()` 恒为假——回执是平台状态，不是独立测量。
- 解码结果追加 `state`（规范状态），并新增 `validate_receipt_binding()` 在查询前复核命令/幂等键/trace/kind 与执行器当前绑定。
- 回执通道异常统一返回 `RECEIPT_UNAVAILABLE`（原 `COMMAND_RECEIPT_UNAVAILABLE`），不用 `accepted` 顶替。

`ktd_platform.query_receipt`：查询前调用绑定复核；`ZhuoyiPlatformExecutor` 增加 `FlightControlStatus.UNKNOWN`，`submitted/accepted/in_progress` 未取得终态返回 `UNKNOWN + RECEIPT_PENDING`，明确未知返回 `UNKNOWN + PLATFORM_OUTCOME_UNKNOWN`。

悬停/指点/环绕/急停：现有 Java 契约没有逐条设备回执（tracking 只覆盖 DJI 六向距离动作）。`track_commands=True` 时这些动作在编译阶段即 `TRACKING_UNSUPPORTED`；非 tracking 模式下可下发但 `receipt_tracking=False`、`device_effect_verified=False`，查询回执返回 `RECEIPT_UNSUPPORTED`。不把“HTTP 已受理”说成设备完成。

## 验证

```bash
bash tools/ci/offline_tests.sh   # Ran 1085 tests in 14.041s / OK，含 socket 禁用守卫
git diff --check                 # 无输出（退出码 0）
python3 -m compileall -q src tests                       # 无输出（退出码 0）
PYTHONPATH=src:tests python3 -m unittest -v test_path_fence test_mapping_interruption
                                 # Ran 119 tests in 3.264s / OK
```

本轮唯一分母为 **1085**（含 `test_offline_guard`；守卫内部重跑 1084 项）；此前阶段的
旧分母已作废，不再引用。

新增 4 个测试文件共 62 项：`tests/test_authority_contract.py`、`tests/test_target_resolver.py`、`tests/test_task_events.py`、`tests/test_receipt_contract.py`。
新增合成夹具 4 份 + provenance：`fixtures/v14-*.json`（明确标注为合成、非平台抓包）。
机器证据：[v14-contracts-20261022.json](evidence/v14-contracts-20261022.json)。

### 证据分层

| 结论 | 等级 |
| --- | --- |
| 快照字段接受/拒绝、`AUTHORITATIVE_STATE_INCOMPLETE`、确认哈希随新字段失效 | 单元验证（注入快照，假传输） |
| 目标解析全部分支、`parse_command` 不伪造坐标 | 单元验证（假 resolver） |
| 事件幂等/乱序/旧事件/低电量优先级/测绘作废 | 仅模拟器验证 |
| 回执规范状态、重试策略、`RECEIPT_UNAVAILABLE`、绑定复核 | 仅假传输验证 |
| Java 是否真的返回 `route_id`/`task_batch_id`/`device_limits`/`task_type` | **未验证真实平台** |
| 平台事件流的 `event_id`/序号/投递语义 | **未验证真实平台** |
| 悬停/指点/环绕/急停的逐条设备回执 | **未验证真实平台** |
| 静态标注服务契约与目标版本 | **未验证真实平台** |
| 任何设备动作、回执准确性、位移精度 | **未验证真实设备** |

## 平台能力缺失与 typed unavailable 边界

本次没有伪造任何 Java 接口或字段：

1. `task_batch_id` 平台回显是**可选**字段；缺失时按 Agent 绑定记录，`audit_snapshot` 标为 `unbound`，不声称平台已证明。
2. `route_id` 是兼容扩展；缺失时仍按旧 `route_status` 检查，执行器给 `ROUTE_STATE_UNAVAILABLE`，不猜测航线。
3. 真实任务事件桥没有可核验契约，默认 `UnavailableTaskEventBridge` 返回 `TASK_EVENT_SOURCE_UNAVAILABLE`。
4. 目标解析器没有可核验地图接口，默认无实现，缺失即 `TARGET_RESOLVER_UNAVAILABLE`。
5. 悬停/指点/环绕/急停没有逐条回执契约，查询返回 `RECEIPT_UNSUPPORTED`。

## 行为变化与影响面

- 权威快照把已声明字段留空（`flight_status: null`、`route_status: null` 等）时，相关动作从“继续执行”改为 `AUTHORITATIVE_STATE_INCOMPLETE`。这是本次唯一的收紧点；`tests/test_platform_runtime.py` 的合成快照因此补了真实 `flight_status` 值。
- **已知平台侧依赖**：现有 Java 快照服务把 `route_status` 恒置为 `null`。按本契约，`route_pause`/`route_resume`/`hover` 在权威路径上会提前返回 `AUTHORITATIVE_STATE_INCOMPLETE`（此前 `route_pause`/`route_resume` 在 `route_status` 为 null 时仍会下发，`hover` 本来就会 `ROUTE_STATE_UNAVAILABLE`）。V1.4 4.3.3 要求“检查是否在航线执行中”，平台不返回该事实时无法检查，因此这里选择拒绝而不是猜。要恢复这些动作，需要 Java 快照真实返回 `route_status`（以及建议的 `route_id`）。本次没有改 Java。
- 旧只读接口（`legacy-read-api`）拼装的测试授权不提供 `flight_status`/`route_status`/`mapping_invalidated`，这些字段在审计中保持缺失记录，不冒充权威拒绝原因，因此旧路径行为不变。
- 未改动默认执行器、动作注册表、preview→confirm→execute 链路、控制权校验、幂等键与上下文版本机制。

## Git 与回滚

- 基于 `test@d6d284d` 在当前工作树修改，未 push、未合入共享 test、未部署、未重启共享服务。
- 保留 `.workbuddy-ai/` 及所有既有未提交改动，未使用 `reset --hard`、`checkout --`、`clean`、`stash`。
- 回滚：本次改动均为新增模块 + 兼容扩展，按提交正向 revert 即可；已提交的平台指令无法用代码回滚撤销，需保留 journal 与追踪记录。
