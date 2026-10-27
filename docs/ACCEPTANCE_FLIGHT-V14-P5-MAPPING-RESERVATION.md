# FLIGHT-V14 P5：测绘中断网关的持久化 reservation（跨进程至多一次）

2026-10-22。修复 V1.4 验收遗留风险：`KtdPlatformExecutor.confirm()` 原先在**调用测绘
中断网关之后**才写 SQLite 幂等账本，进程 A 与进程 B 可以同时通过前置检查、各自调用一次
不可逆网关，之后才由账本阻止重复 submit。

现在「同一 command_id / idempotency_key / context / binding」在跨进程并发下**最多调用一次**
测绘网关；无法确认网关结果时**绝不自动重试**。

仅用注入快照、假传输、假网关与本地 SQLite 验证。**未连接真实平台、未执行任何平台写操作；
真实测绘事务、真实原子接口与真实幂等语义均未验证。**

## 一、reservation 状态机

状态存于 SQLite command journal 的 `mapping_reservations` 表，**先于**外部网关调用落库。

| 状态 | 含义 | 允许的外部调用 |
| --- | --- | --- |
| `mapping_reservation_pending` | 已取得 reservation，网关**尚未调用** | 仅本持有者可在租约内推进到 unknown |
| `mapping_interruption_unknown` | 调用即将开始/已经开始，结果未知 | **无**。不重试网关，不 submit |
| `mapping_interruption_succeeded` | 网关返回 `atomic=true` 且 `confirmed=true` | 允许写命令账本并 submit（至多一次） |
| `mapping_interruption_failed` | 网关给出明确失败（unavailable / partial / not confirmed） | 无。同一幂等键到此为止 |

转移（全部是单行 CAS，`WHERE scope=? AND fingerprint=? AND state IN (...)`）：

```
(无)
 └─ reserve_mapping INSERT ──────────────► mapping_reservation_pending
mapping_reservation_pending
 ├─ advance_mapping（调用网关前）────────► mapping_interruption_unknown
 └─ 租约过期后 advance_mapping（恢复方接管）► mapping_interruption_unknown
mapping_interruption_unknown
 ├─ 网关 effective（atomic+confirmed+无错误码）► mapping_interruption_succeeded
 ├─ 网关明确失败 ─────────────────────────► mapping_interruption_failed
 ├─ resolve_mapping_interruption('voided') ─► mapping_interruption_succeeded
 └─ resolve_mapping_interruption('not_voided') ► mapping_interruption_failed
```

关键点：**网关调用永远发生在 `unknown` 之后、`succeeded` 之前**。所以

- 崩在 `pending`（且租约已过期）⇒ 按定义「还没调用」，恢复方可以接管并调用一次；
- 崩在 `unknown` 及其之后 ⇒ 结果不可知，永久停在 `unknown`，不重试。

`unknown` 既不会被改写成 `unavailable`，也不会被改写成 `success`：回执状态是 `unknown`，
错误码是 `MAPPING_INTERRUPTION_UNKNOWN`，message 明说「不会自动重试」。

## 二、SQLite 唯一约束与事务位置

`src/lingxi_flight_control/ktd_platform.py`：

| 位置 | 语句 | 作用 |
| --- | --- | --- |
| `SqliteCommandJournal.__init__` | `CREATE TABLE IF NOT EXISTS mapping_reservations (scope TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, state TEXT NOT NULL, record TEXT NOT NULL)` | `scope` 主键 = 唯一约束；`fingerprint` 区分同键不同命令 |
| `SqliteCommandJournal.reserve_mapping` | `BEGIN IMMEDIATE` → `SELECT` → `INSERT` | 写事务在文件级串行化；返回 `acquired` / `existing` / `conflict` |
| `SqliteCommandJournal.advance_mapping` | `BEGIN IMMEDIATE` → `UPDATE ... WHERE scope=? AND fingerprint=? AND state IN (...)`，`rowcount == 1` | 状态机 CAS，只有一个进程能推进 |
| `SqliteCommandJournal.read_mapping` | `SELECT fingerprint, record` | 只读，供恢复与审计 |

- 没有任何进程内锁参与去重；模块级 `_MAPPING_GATE_LOCK` 已删除。`KtdPlatformExecutor._lock`
  只用于同一实例内的确认卡字典，不承担跨进程保证。
- `scope = sha256(actor, project, drone, idempotency_key)`，与命令账本同一作用域。
- `fingerprint = sha256(binding, site, task, request)`；同一幂等键但 fingerprint 不同
  ⇒ `IDEMPOTENCY_CONFLICT`（reservation 层直接拒绝，不会复用作废结果）。
- 记录字段：`command_id`、`idempotency_key`、`trace_id`、`context_version`、
  `snapshot_version`、`task_batch_id`、`fingerprint`，另含 `attempts`、`lease_expires_at`、
  `gateway_execution_mode`、`gateway_idempotency_supported`、`gateway_idempotency_key`、
  `receipt`、`error_code`、`resolution`、`authority_ref`。

## 三、confirm 顺序（改后）

| # | 步骤 | 外部副作用 |
| --- | --- | --- |
| 1 | 确认卡、`_matches`、上下文版本、有效期、账本回放 | 否 |
| 2 | 最新权威快照 + 资源绑定/控制权/能力/租约 | 只读 |
| 3 | 确定性编译、任务结束预检 | 只读 |
| 4 | confirm 阶段围栏复检 | 只读 |
| 5 | `_snapshot_hash` 与编译结果复比；漂移即撤销确认卡 | 否 |
| 6 | **原子 reservation**（`reserve_mapping`，跨进程唯一） | 本地 SQLite 写 |
| 7 | CAS `pending → unknown` | 本地 SQLite 写 |
| 8 | **测绘网关**（唯一不可逆步骤），必须 `atomic=true` 且 `confirmed=true` | 是 |
| 9 | CAS `unknown → succeeded` | 本地 SQLite 写 |
| 10 | 命令账本 `claim`（先落 `unknown`/prepared） | 本地 SQLite 写 |
| 11 | `transport.submit` | 是（平台写） |
| 12 | `finish` 更新回执；submit 结果未知则保留 `unknown`，不重试 | 本地 SQLite 写 |

非测绘任务不创建 reservation（第 6–9 步整体跳过），行为与改前一致。

## 四、网关幂等键

- `MappingInterruptionRequest` 新增 `idempotency_key`、`trace_id`、`context_version`、
  `snapshot_version`、`fingerprint`，由执行器填入同一组 reservation 标识；幂等键优先取
  `idempotency_key`。
- 网关是否**真的**按幂等键收敛由网关自己声明 `supports_idempotency_key`；默认 `False`。
  执行器把结果写进 reservation 的 `gateway_idempotency_supported`，审计输出
  `mapping_interruption_audit()['idempotency_supported']`，**不会**因为本地说有键就宣称
  平台侧幂等。
- 即使网关声明支持幂等键，`unknown` 也**不**自动重试——平台侧幂等未经验证，本地
  reservation 才是唯一保证。

## 五、崩溃与恢复语义

| 崩溃点 | 落库状态 | 恢复行为 |
| --- | --- | --- |
| `reserve_mapping` 之后、`pending → unknown` 之前 | `pending` | 租约（默认 30s）未过期：返回 `MAPPING_INTERRUPTION_UNKNOWN`，不调用网关；租约过期：CAS 接管后调用**一次** |
| `pending → unknown` 之后、网关返回之前（含网关抛错/进程被杀） | `unknown` | 永远返回 `MAPPING_INTERRUPTION_UNKNOWN`；不调网关、不 submit |
| 网关成功、`succeeded` 之后、命令账本 `claim` 之前 | `succeeded` | 不再调网关；重新 preview → confirm 只补写账本并 submit **一次** |
| 命令账本 `claim` 之后、submit 之前/之中 | 账本 `unknown` | 返回既有 `unknown` 回执，不重复 submit |
| submit 结果未知 | 账本 `unknown` | 保留 `unknown`，不重试 |

显式恢复接口（都不调用网关、不下发飞控）：

- `mapping_reservation_status(command_id, idempotency_key, context_version)`：只读查询；
- `resolve_mapping_interruption(..., resolution='voided'|'not_voided', authority_ref=...)`：
  人工核对或权威回执的显式恢复，把 `unknown` CAS 成 `succeeded`/`failed`，并记录
  `authority_ref`。恢复本身不重试网关；需要继续下发时由调用方重新 preview → confirm，
  由命令账本保证 submit 至多一次。

`pending` 租约只用于避免抢走**活着的**进程；它不承担「是否已调用」的判断——那个判断由
「调用前先写 `unknown`」保证。

## 六、跨进程测试与实测调用次数

`tests/mapping_race_worker.py` 是可执行工作进程（不被 `unittest discover` 收集）：两个
独立 Python 进程共享同一个 SQLite journal 与同一组调用记录文件，`uuid4` 被替换成确定性
序列，因此两个进程各自 `preview` 出**同一张**确认卡（相同 `confirmation_id` /
`confirmation_binding` / `trace_id`），再在同一时刻 `confirm`。

`tests/test_mapping_interruption.py`：

- `MappingReservationStateMachineTests`（12 项，同进程两个执行器）：reservation 先于网关
  落库、网关幂等键与标识一致、unknown 不重试、failed 对同键终态、`atomic` 但未 `confirmed`
  不算成功、同键不同 fingerprint 冲突、活租约不被抢占、过期租约只被接管一次、网关成功后
  账本崩溃不再调网关、人工恢复是 unknown 的唯一出口、非测绘任务不建 reservation。
- `CrossProcessReservationTests`（5 项，双进程）：并发同一确认卡、同键不同 fingerprint
  冲突、网关成功后崩溃、网关 unknown 后不自动重试（含人工恢复后只 submit 一次）、不同
  command_id 各自调用。

20 轮双进程并发实测（`docs/evidence/v14-mapping-reservation-20261022.json`）：

| 指标 | 结果 |
| --- | --- |
| 每轮进程数 | 2 |
| 网关调用次数 | **恒为 1**（20/20） |
| 平台 submit 次数 | **恒为 1**（20/20） |
| 结果组合 | `submitted + submitted` 18 次（输家读到既有回执）；`submitted + unknown` 2 次（输家落在网关调用窗口，返回 `MAPPING_INTERRUPTION_UNKNOWN`） |

结果组合的分布随进程调度变化（另一次 20 轮实测为 10/10），**不变量是网关与 submit 恒为 1 次**；
这里记录的是本轮收口时的实测值，不把某一种分布当成稳定比例。
| 同键不同 fingerprint | 一方 `IDEMPOTENCY_CONFLICT`，网关仍 1 次、submit 1 次 |
| 网关成功后崩溃 | 网关 1 次、submit 0 次；恢复后网关仍 1 次、submit 1 次 |
| 网关 unknown | 网关 1 次、submit 0 次；连续两次恢复仍 1/0；人工恢复后 1/1 |

输家在网关调用窗口里拿到 `unknown` 而不是等待，是刻意的：不轮询、不 sleep 重试、不用
daemon thread，宁可让调用方重新查询，也不让第二个进程碰不可逆接口。

## 七、仍无法由平台/本仓保证的部分

1. **真实平台是否提供 `atomic + confirmed` 的测绘作废接口**：未验证。默认仍是
   `UnavailableMappingInterruptionGateway`，测绘任务因此被拒绝且不下发。
2. **真实平台是否按幂等键收敛**：未验证。本仓不假设；`supports_idempotency_key` 缺省
   `False`，`unknown` 一律不重试。
3. **网关内部是否原子**：`atomic=true` 只是网关自述；平台若先停拼图再失败，`partial` 已
   落地的步骤无法回滚，本仓只如实报告，不伪造回滚。
4. **跨主机一致性**：reservation 依赖 SQLite 文件的写事务，只对**同一台机器上共享同一
   文件的多进程**成立。多主机/多副本部署需要换成服务端唯一键，本仓不声称支持。
5. **`resolve_mapping_interruption` 的权威性**：`authority_ref` 只是字符串，本仓无法验证
   人工核对结论本身是否正确；它只保证「没有显式恢复就不会自动往下走」。
6. **设备动作与位移效果**：未验证，`device_effect` 始终是 `not_verified`。

## 八、验证

```bash
bash tools/ci/offline_tests.sh   # Ran 1085 tests in 14.041s / OK（含 socket 禁用守卫）
git diff --check                 # 无输出（退出码 0）
python3 -m compileall -q src tests                       # 无输出（退出码 0）
PYTHONPATH=src:tests python3 -m unittest -v test_path_fence test_mapping_interruption
                                 # Ran 119 tests in 3.264s / OK
```

本轮唯一分母为 **1085**（含 `test_offline_guard`；守卫内部重跑 1084 项）。

机器证据：[v14-mapping-reservation-20261022.json](evidence/v14-mapping-reservation-20261022.json)。

## 九、Git

- 基于 `test@d6d284d` 在当前工作树修改，未 commit、未 push、未部署。
- 工作树仍保留 V1.4 P1–P4 的其它未提交改动；本次只涉及
  `src/lingxi_flight_control/ktd_platform.py`、`src/lingxi_flight_control/mapping_interruption.py`、
  `src/lingxi_flight_control/full_control.py`、`tests/test_mapping_interruption.py`、
  `tests/test_path_fence.py`、新增 `tests/mapping_race_worker.py` 与本文档/证据。
