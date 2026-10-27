# FLIGHT-V14 P1：平台 confirm 路径先作废测绘、后复检围栏

2026-10-22。修复 V1.4 验收发现的 P1 顺序缺陷：`KtdPlatformExecutor.confirm()` 在 confirm
阶段围栏复检之前就调用了测绘原子中断网关，导致「围栏已 blocked、飞控动作未下发，但测绘
成果已被不可逆作废」。

仅用模拟器、注入快照与假传输验证。**未连接真实平台、未执行任何平台写操作，真实事务与
真实原子接口均未验证。**

## 缺陷与修复

改前 `confirm()` 的顺序是：读取快照 → 编译命令 → **调用测绘中断网关** → 任务结束预检 →
围栏复检 → 快照/命令复比 → 账本 claim → 提交。围栏从 `clear` 变 `blocked` 时，第 3 步
已经完成不可逆作废，而第 5 步才拒绝，副作用无法回滚。

改后顺序（`src/lingxi_flight_control/ktd_platform.py`）：

| # | 步骤 | 是否产生外部副作用 |
| --- | --- | --- |
| 1 | 确认卡存在性、`_matches`（command_id / idempotency_key / confirmation_binding / context_version）、上下文版本、`CONFIRMATION_EXPIRED` | 否 |
| 2 | 账本回放：同幂等键已有记录则直接返回既有回执（含 `unknown`），不再作废、不再下发 | 否（只读账本） |
| 3 | `_snapshot()`：最新权威快照 + 资源绑定、授权质量、时效、权限、`require_authority`、控制权与租约、能力、在线 | 否（一次读快照） |
| 4 | `_compile(..., interruption_pending=True)`：纯编译，不写任何外部状态 | 否 |
| 5 | `_task_end_precheck()`：仅 `task_end_*` 需要的一次只读安全检查 | 只读 GET |
| 6 | `check_path()`：confirm 阶段围栏/路径复检；`blocked` → `PATH_BLOCKED`，`timeout`/`unavailable`/`malformed` 按 V1.4 2.2.2 降级并保留 warning | 只读围栏查询 |
| 7 | `_snapshot_hash` 与编译结果 `command` 复比；漂移 → `AUTHORITATIVE_STATE_CHANGED` 并撤销确认卡 | 否 |
| 8 | `_mapping_interruption_reserved()`：仅在需要测绘中断时执行。先 `reserve_mapping()` 原子取得持久化 reservation，再 CAS `pending → unknown`，**之后**才调用网关；进程内锁已删除（P5） | **是（唯一不可逆步骤）** |
| 9 | 回执必须 `atomic is True`、`confirmed is True` 且 `error_code is None`，否则抛错终止；结果未知则停在 `mapping_interruption_unknown` | — |
| 10 | 账本 `claim`（先落 `unknown`）→ `transport.submit` | 是（平台写） |

`_compile()` 增加了 `interruption_pending` 参数：preview 与 confirm 都显式声明「本路径允许
后续走原子中断」，能力探测（`available_actions`）仍按 `MAPPING_INTERRUPTION_UNAVAILABLE`
拒绝，不会因为放行 confirm 而让探测路径误判为可执行。

「必须 `atomic=true` 且 `confirmed=true`」的落地方式：`confirmed=True` 在请求侧
（`MappingInterruptionRequest`）显式传入并由网关校验，未确认时网关返回
`MAPPING_INTERRUPTION_NOT_CONFIRMED`；`atomic` 在回执侧强制为 `True` 且 `error_code`
必须为空。两者缺一即抛错，不会进入提交。

## 各状态的外部副作用

| confirm 阶段状态 | 测绘网关调用 | 平台 submit | 测绘状态 | 回执 |
| --- | --- | --- | --- | --- |
| 围栏 `clear` | 1（仅测绘任务） | 1 | 已按原子契约作废 | 正常回执 |
| 围栏 `blocked` | 0 | 0 | **未作废** | `PATH_BLOCKED`，确认卡失效 |
| 围栏 `timeout` | 1 | 1 | 已按原子契约作废 | 降级 warning 保留，不显示为 clear |
| 围栏 `unavailable` | 1 | 1 | 已按原子契约作废 | 降级 warning 保留 |
| 网关 unavailable | 1 | 0 | 未作废（回执 `atomic=false`、`completed_steps=[]`） | `MAPPING_INTERRUPTION_UNAVAILABLE` |
| 网关 partial | 1 | 0 | 部分步骤已落地，回执如实给出 `failed_step` / `completed_steps` | `MAPPING_INTERRUPTION_PARTIAL`，message 含「测绘中断未完成」 |
| 网关未确认 | 1 | 0 | 未作废 | `MAPPING_INTERRUPTION_NOT_CONFIRMED` |
| 网关结果未知（抛错/进程崩溃） | 1 | 0 | 状态停在 `mapping_interruption_unknown`，**不重试、不 submit** | `unknown` + `MAPPING_INTERRUPTION_UNKNOWN`，只能人工/权威回执显式恢复 |
| 提交结果 `unknown` | 1 | 1 | 已作废 | `unknown` 保留，不自动重试；重放只读账本 |
| 非测绘任务 | 0 | 1 | 不涉及 | 原有路径检查与执行行为不变 |

「网关 partial」一行必须说清：原子契约失败时**已完成的步骤可能已经落地**，系统只能如实
报告，不能声称回滚；本仓不伪造「已作废」，也不把 partial 当成成功继续下发。

## 调用次数与幂等

- 同一幂等键的**顺序重放**：第 2 步账本命中即返回，网关与 submit 均不再调用（测试
  `test_unknown_submit_is_kept_and_never_retried` 覆盖）。
- **进程内并发**：由 SQLite `mapping_reservations` 的 `scope` 主键 + `BEGIN IMMEDIATE`
  收敛；原模块级 `_MAPPING_GATE_LOCK` 已删除（P5）。
- **跨进程并发（本轮实测）**：两个独立进程同时 confirm 同一张卡，实测
  **gateway 1 次 / submit 1 次**，结果为一个 `submitted` + 一个 `unknown`；同键不同参数
  实测 gateway 1 次 / submit 1 次，另一方 `IDEMPOTENCY_CONFLICT`；网关执行中进程崩溃后
  重放两次，实测 gateway 仍为 1 次、submit 0 次，两次都返回
  `MAPPING_INTERRUPTION_UNKNOWN`。详见 [P5](ACCEPTANCE_FLIGHT-V14-P5-MAPPING-RESERVATION.md)。
- 提交结果为 `unknown` 时账本已落 `unknown`，不重试；`finish` 失败时返回已持久化的
  `unknown`，不回退成 `rejected`。

> 残余风险：上述「至多一次」只在本机 SQLite 上验证。真实 gateway 是否按
> `idempotency_key` 收敛、真实平台事务是否具备同等原子性，**均未验证**；本仓不假设
> 网关支持幂等键（`supports_idempotency_key` 默认 `False`）。

## 回归测试

新增 10 项，另有 1 项既有用例按新顺序拆分：

- `tests/test_path_fence.py::PlatformMappingFenceTests`（4 项）：blocked 不触碰网关与平台、
  timeout 降级后仍按「围栏 → 作废 → 提交」顺序下发、unavailable 降级但控制权与确认绑定
  仍然生效。
- `tests/test_mapping_interruption.py::PlatformConfirmOrderTests`（6 项）：网关
  unavailable / partial / not confirmed 均不产生平台写调用、成功路径先作废后提交且回执带
  `mapping_interruption`、`unknown` 不重试且重放不重复作废、非测绘任务不调用网关。
- `tests/test_authority_contract.py`：`test_task_type_change_to_mapping_is_blocked_before_dispatch`
  改为断言 `AUTHORITATIVE_STATE_CHANGED`（任务类型在 preview/confirm 之间变化时快照哈希
  先失配），并新增 `test_mapping_task_without_atomic_contract_is_blocked_before_dispatch`
  保留「测绘任务 + 无原子契约 → 拒绝」的原覆盖。

## 验证

```bash
bash tools/ci/offline_tests.sh   # Ran 1085 tests in 14.041s / OK，含 socket 禁用守卫
git diff --check                 # 无输出（退出码 0）
python3 -m compileall -q src tests                       # 无输出（退出码 0）
PYTHONPATH=src:tests python3 -m unittest -v test_path_fence test_mapping_interruption
                                 # Ran 119 tests in 3.264s / OK
```

本轮唯一分母为 **1085**（含 `test_offline_guard`；守卫内部重跑 1084 项）。此前阶段的
旧分母已作废，不再引用。

机器证据：[v14-confirm-order-20261022.json](evidence/v14-confirm-order-20261022.json)。

### 证据分层

| 结论 | 等级 |
| --- | --- |
| confirm 阶段预检查顺序、blocked 不调用网关/submit、降级 warning 语义 | 单元验证（注入快照 + 假传输 + 假网关） |
| 网关 unavailable/partial/未确认时无平台写调用 | 单元验证（假网关） |
| `unknown` 不重试、幂等重放不重复作废 | 单元验证（临时 SQLite 账本 + 假传输） |
| 跨进程并发下 gateway / submit 至多一次、unknown 不重试 | 本机多进程验证（SQLite journal + 文件调用记录；2 进程与 3 进程场景） |
| 真实平台的原子作废接口是否存在、是否具备同等原子性 | **未验证真实平台** |
| 真实 gateway 是否按 `idempotency_key` 收敛 | **未验证真实平台**（本仓不假设支持） |
| 平台是否接受本次重排后的提交时序 | **未验证真实平台** |
| 任何设备动作与位移效果 | **未验证真实设备** |

## Git

- 基于 `test@d6d284d` 在当前工作树修改，未 commit、未 push、未部署。
- 工作树仍保留 V1.4 其它未提交改动（`flight_policy.py`、`full_control.py`、`ktd_snapshot.py`
  等）与未跟踪的验收文档，本次未清理、未暂存、未改写历史。
