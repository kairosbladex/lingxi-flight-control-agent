# FLIGHT-V14 第四阶段：跨仓契约对接与隔离联调

2026-10-22。依据《灵犀平台自然语言智能控制功能设计规范 V1.4》2.2、3.1、3.2、4.1、4.3 与本次要求，
把前三阶段在飞控仓内建立的 typed 契约接到**地面规划 Agent、Java 平台、前端**三侧，
并用本机回环的假 Java/HTTP 跑通隔离联调。

默认执行器仍是模拟器，`KtdPlatformExecutor` 仍默认关闭；未连接 MQTT、真实无人机或共享
test/uat/prod 服务，未执行任何平台写操作，未部署、未重启共享服务、未 push。

## 基线

| 仓库 | 分支 | HEAD | 开工时工作区 |
| --- | --- | --- | --- |
| `lingxi-flight-control-agent` | `test` | `d6d284d` | 含 P1–P3 未提交改动 + 未跟踪 `.workbuddy-ai/` |
| `lingxi-agent` | `bugfix/algorithm-selection-integrity` | `860d9ed` | 含既有未提交改动（`control_intent.py`、`control_permissions.py` 等） |
| `ktd_backend-real-flight-assistant`（`ktd_backend-b3-java` 的 worktree） | `test` | `ed13701f55` | 干净 |
| `lingxi-flight-agent-ui` | `test` | `f72b81a` | 干净，仅 `dist-v13/` 未跟踪 |

全程未使用 `reset --hard`、`checkout --`、`clean`、`stash`，未覆盖任何仓库已有改动。

## 一、职责边界（本次落地的实际边界）

| 侧 | 负责 | 本次没有做 |
| --- | --- | --- |
| `lingxi-agent` | 任务草稿、静态标注目的地、机库匹配、航线生成、下发交接 | 不解析飞行中微操，不执行飞控动作，不写标注库 |
| `lingxi-flight-control-agent` | 已绑定上下文内的 preview/confirm/execute、控制权、幂等、回执 | 不复制地图标注库、不复制任务规划状态机、不保存 Java 业务真值 |
| Java（`platformsnapshot`） | 权威快照、控制权、能力与动作可用性、显式能力缺口 | 不返回它没有的航线状态/算法生命周期/测绘事务 |
| 前端（`src/v14`） | 确认卡、参数三列、围栏降级、测绘失效、逐项回执 | 不自行判定授权，不自动重试，不把 unknown 显示成成功 |

关键约束：**交接规则的唯一真源是飞控仓的 `handoff_contract.py`**。规划 Agent 的
`app/runtime/flight_handoff.py` 在返回信封前调用同一份 `validate_envelope`，
不存在两套白名单。

## 二、跨仓契约变更表

信封：`lingxi-flight-handoff/v1`，两种 `kind`：

- `task_dispatch`：地面规划 → 任务下发。唯一会建立任务的类型。
- `flight_control`：飞行中控制。只消费已下发任务的绑定，**不创建第二个任务**。

| 契约项 | 字段 | 校验口径 | 失败码（HTTP） |
| --- | --- | --- | --- |
| actor/project/hangar/drone/task/task_batch 绑定 | `binding.*` | 六个标识非空；与飞控侧 `RuntimeBinding` 逐项相等；`site_id`/`task_id` 也须相等 | `HANDOFF_BINDING_INCOMPLETE`(400) / `HANDOFF_BINDING_MISMATCH`(409) |
| context_version | `binding.context_version` | 必须等于当前绑定版本 | `STALE_CONTEXT`(409) |
| snapshot_version | `binding.snapshot_version` | 非空，且与执行器读到的权威快照 `version` 相等 | `HANDOFF_SNAPSHOT_STALE`(409) |
| targetId / 同名候选 | `targets[].target_id` | 必须给出唯一 `target_id`；候选列表不得塞进信封 | `HANDOFF_TARGET_REQUIRED`(422) / `HANDOFF_FIELD_NOT_ALLOWED`(400) |
| target geometry_type | `targets[].geometry_type` | 点/线/面/体四值；体一律拒绝；混合一律拒绝 | `HANDOFF_TARGET_GEOMETRY_UNSUPPORTED`(422) / `HANDOFF_MIXED_TARGET_UNSUPPORTED`(422) |
| 目标版本 | `targets[].version` | 非空（否则确认卡无法失效） | `HANDOFF_TARGET_VERSION_UNAVAILABLE`(422) |
| 多点顺序 | `targets[].order` | 地面点目标必须 `1..N` 且与列表顺序一致，不静默排序 | `HANDOFF_TARGET_ORDER_INVALID`(422) |
| 线/面单条单个 | `targets` 长度 | 线/面 >1 拒绝；飞行控制只接受 1 个点 | `HANDOFF_MULTI_GEOMETRY_UNSUPPORTED`(422) |
| altitude/coordinate | `targets[].altitude_m`、`action.parameters` | 指点飞行必须有高度；不复用航线高度、不给默认值 | `HANDOFF_TARGET_GEOMETRY_UNSUPPORTED`(422) |
| route parameters | `action.parameters` | 复用 `_validate_parameters` 白名单与数值范围 | `HANDOFF_ACTION_UNSUPPORTED`(422) |
| configured/global/annotation algorithms | `algorithms.*` | 只允许项目已配置算法；标注必须属于本次目标 | `HANDOFF_ALGORITHM_NOT_CONFIGURED`(422) |
| task_type | `task.task_type` | 五值枚举；巡检执行中新起测绘类拒绝 | `HANDOFF_CROSS_TYPE_FORBIDDEN`(409) |
| route_status/breakpoint/temporary | `task.*` | 类型校验；断点必须挂 route_id（快照侧已有约束） | `HANDOFF_ACTION_UNSUPPORTED`(422) |
| mapping_invalidated | `task.mapping_invalidated` | 作废后 `route_resume`/`continue_from_breakpoint` 一律拒绝 | `MAPPING_NOT_RESUMABLE`(409) |
| 未确认草稿 | `draft.confirmed`/`draft.dispatch_id` | `task_dispatch` 必须 confirmed 且有 dispatch_id | `HANDOFF_DRAFT_NOT_DISPATCHABLE`(409) |
| command_id/idempotency_key/trace_id | 顶层三个字段 | 全部必填；同键同体幂等、同键异体冲突 | `HANDOFF_BINDING_INCOMPLETE`(400) / `HANDOFF_IDEMPOTENCY_CONFLICT`(409) |
| preview confirmation binding | 既有 `confirmation_binding` | 未改动：仍由 binding+request+route/policy context 哈希 | 既有 `STALE_CONTEXT`/`ROUTE_STATE_MISMATCH` |
| submitted/unknown/device receipt | `DeliveryStage` | `accepted`/`submitted`/`unknown` 都不是设备完成；`device_effect_verified()` 恒 False | 不产生成功态 |
| 平台能力缺失 | `contract.unavailable` | Java 顶层声明 → Python 转成该能力自己的错误码 | 如 `ROUTE_STATE_SOURCE_UNAVAILABLE` |

Java 侧新增（顶层，不塞进 `state`，避免触碰既有 state 白名单）：

```json
"contract": {
  "schema_version": "lingxi-platform-contract/v1",
  "unavailable": {
    "route_status": "ROUTE_STATE_SOURCE_UNAVAILABLE",
    "target_binding": "STATIC_ANNOTATION_SOURCE_UNAVAILABLE",
    "algorithm_lifecycle": "ALGORITHM_LIFECYCLE_UNAVAILABLE",
    "mapping_transaction": "MAPPING_TRANSACTION_UNAVAILABLE"
  }
}
```

`route_status` 只在 `state.route_status` 确实为 null 时声明，不是无条件写死；
`device_receipt` **没有**声明不可用（回执有独立端点，不能谎报缺口）。
该块参与 `snapshot_version` 指纹，契约变化会让旧确认卡失效。

## 三、各仓改动文件

### `lingxi-flight-control-agent`

| 文件 | 类型 | 内容 |
| --- | --- | --- |
| `src/lingxi_flight_control/handoff_contract.py` | 新增 | 信封白名单、绑定/版本/幂等/几何校验、`to_runtime_binding`、`to_action_request`、`verify_snapshot`、`HandoffLedger`、`DeliveryStage`、`parse_platform_unavailable` |
| `src/lingxi_flight_control/ktd_snapshot.py` | 修改 | 解析顶层 `contract` 块；格式非法即 `INVALID_PLATFORM_CONTRACT`；`KtdSnapshot.platform_unavailable` |
| `src/lingxi_flight_control/ktd_platform.py` | 修改 | `KtdSnapshot` 追加 `platform_unavailable`（尾部默认 None）；`_validate_snapshot` 先查平台声明的能力缺口，再走 `require_authority` |
| `tests/test_handoff_contract.py` | 新增 | 42 项 |
| `tests/test_ktd_snapshot.py` | 修改 | +4 项（契约块解析、typed 缺口优先、格式非法拒绝、无声明不推断） |
| `tools/ci/cross_repo_isolation.py` | 新增 | 回环假 Java + 17 个联调场景 |
| `docs/ACCEPTANCE_FLIGHT-V14-P4.md` | 新增 | 本文 |
| `docs/evidence/v14-cross-repo-20261022.json` | 新增 | 机器证据 |

### `lingxi-agent`

| 文件 | 类型 | 内容 |
| --- | --- | --- |
| `app/runtime/flight_handoff.py` | 新增 | 交接产出方：`build_task_dispatch` / `build_flight_control` / `flight_action_for_target` / `validate` |
| `tests/test_flight_handoff.py` | 新增 | 17 项 |
| `tests/test_flight_control_full.py` | 修改 | 修跨仓漂移：航线类动作无航线/断点时断言 typed 拒绝（`ROUTE_NOT_LOADED`/`BREAKPOINT_UNAVAILABLE`），不再要求伪造确认卡 |
| `docs/ACCEPTANCE_FLIGHT-V14-CROSS-REPO-20261022.md` | 新增 | 本仓交付记录 |

**发现的跨仓漂移**：`test_all_action_domains_have_explicit_state_results` 原先要求列出的全部动作
都返回 `preview_required`，但飞控仓已对航线类动作加了前置条件。用 HEAD `d6d284d` 的源码
复跑同样失败（3 项），说明这不是本阶段引入，而是既有的两仓口径不一致，本次按新契约修正。

### Java（`ktd_backend-real-flight-assistant`，分支 `test`）

| 文件 | 类型 | 内容 |
| --- | --- | --- |
| `biz-inspection/.../platformsnapshot/PlatformSnapshotService.java` | 修改 | 新增 `contract(state)` 并写入快照；参与指纹 |
| `biz-inspection/.../platformsnapshot/PlatformSnapshotTest.java` | 修改 | +2 项（缺口声明只列真实缺口、契约块参与版本） |

### `lingxi-flight-agent-ui`

| 文件 | 类型 | 内容 |
| --- | --- | --- |
| `src/v14/presenter.ts` | 新增 | 展示模型：动作区分、参数三列、围栏降级、测绘失效、临时任务、AI 三态、逐项回执、候选不自动选中 |
| `src/v14/V14Panel.vue` | 新增 | 展示层组件（宿主注入数据与回调，不发请求） |
| `src/v14/main.ts` | 新增 | `window.__V14_BOOTSTRAP__` 入口，无数据渲染空态 |
| `v14.html` / `vite.v14.config.ts` | 新增 | 独立构建入口（`dist-v14`） |
| `tests/v14.test.mjs` | 新增 | 12 项 |

## 四、测试命令与真实输出

### Python 飞控仓

```bash
bash tools/ci/offline_tests.sh
git diff --check
```

```
Ran 1085 tests in 14.041s

OK
```

（本轮统一分母 **1085**，含 `tests/test_offline_guard.py` 的 socket 禁用守卫；此前阶段的
旧分母已作废，不再作为分母引用。本阶段新增的交接契约与快照契约块仍可按用例名追溯。）

### `lingxi-agent`

```bash
.venv/bin/python -m unittest tests.test_flight_handoff
.venv/bin/python -m unittest tests.test_flight_control_full
.venv/bin/python -m unittest discover -s tests -p 'test_control*.py'
.venv/bin/python -m compileall -q app config tests
git diff --check
```

```
Ran 17 tests in 0.001s
OK
```

```
Ran 3 tests in 0.023s
OK
```

```
Ran 54 tests in 2.637s
OK
```

`compileall` 无输出（通过）；`git diff --check` 通过。

**全量 `unittest discover` 在本机跑不完**：进程在
`test_agent_java_switch.AgentJavaSwitchTests.test_concurrent_jar_change_prevents_replacement`
处被 SIGKILL（退出码 137），单独运行该用例同样被杀，属本机对该用例的限制；
该模块与本次新增的 `flight_handoff.py` 无导入关系（本模块只依赖标准库 + 飞控仓契约）。
排除该模块后其余全量结果见下方“排除崩溃模块的全量结果”。

排除崩溃模块的全量结果：

```
RESULT FAILED run 1657 fail 0 error 2 skipped 4
NONOK test_eval_closure_report.ClosureReportTests.test_legacy_jsonl_report_files
NONOK test_thread_lease.ThreadLeaseTests.test_spawned_processes_have_one_winner
```

两项 error 都是本机环境限制，不是断言失败：
`test_legacy_jsonl_report_files` 在 `mkdir(.tmp, exist_ok=True)` 上被本机路径代理拒绝
（`PermissionError: EEXIST`）；`test_spawned_processes_have_one_winner` 依赖多进程队列，
15 秒内拿不到子进程结果（`_queue.Empty`）。两者都不经过本次新增模块。

### Java

```bash
mvn -o -s /tmp/mvn-offline-settings.xml -Dmaven.repo.local="$HOME/.m2/repository" \
    -pl biz-inspection -Dtest='com.fan.framework.inspection.platformsnapshot.*Test' test
mvn -o -s /tmp/mvn-offline-settings.xml -Dmaven.repo.local="$HOME/.m2/repository" \
    -pl biz-inspection -Dtest='com.fan.framework.inspection.flightcommand.*Test,com.fan.framework.inspection.flightv13.*Test' test
```

```
[INFO] Tests run: 53, Failures: 0, Errors: 0, Skipped: 0
[INFO] BUILD SUCCESS
```

```
[INFO] Tests run: 170, Failures: 0, Errors: 0, Skipped: 0
[INFO] BUILD SUCCESS
```

离线编译前先 `mvn -o ... -pl sys-core -am -DskipTests install`（本地 `.m2` 的 `sys-core`
快照缺少 `NullPreservingJsonSerializer` 等新类）。未跑整个模块的测试套件：
`biz-inspection` 里还有依赖 MySQL/Redis 的集成测试，本机离线环境不具备。

### 前端

```bash
npm test
npx vue-tsc -b
npx vite build --config vite.v14.config.ts --outDir /tmp/v14-dist
```

```
# tests 171
# pass 171
# fail 0
```

`vue-tsc -b` 无输出（通过）；V1.4 面板构建 `✓ built in 193ms`。
`npm run build` 在清空既有 `dist/` 时被本机安全删除保护拦截（一次删除 112 个文件超过阈值），
属环境限制，不是类型或打包错误；因此改用 `vue-tsc` + 独立 outDir 验证。

### 跨端隔离联调

```bash
python3 tools/ci/cross_repo_isolation.py
```

```
全部通过：17/17（审计事件 3 条）
```

## 五、隔离联调场景与结果

全部在 `127.0.0.1` 的临时端口（本次 49929）上运行，假 Java 只返回合成数据。

| 场景 | 验证点 | 结果 |
| --- | --- | --- |
| S0 | 权威快照可读，且带 4 项显式不可用声明 | PASS |
| S1 | 点目标经纬度/高度进入飞控 `point_flight` 参数 | PASS |
| S1b | 交接信封 `snapshot_version` 与权威快照一致 | PASS |
| S2 | 线→飞行、面→飞行、体、混合 四种输入全部拒绝 | PASS |
| S3 | 三点串联按用户顺序 `t-3,t-1,t-2` 保序 | PASS |
| S4 | 未确认草稿不产出可下发任务 | PASS |
| S5 | 飞行控制交接 `creates_task=False`，不建第二个任务 | PASS |
| S6 | 旧 `context_version` 返回 `STALE_CONTEXT`/409 | PASS |
| S7 | 同键同体 `new→duplicate`，同键异体 `HANDOFF_IDEMPOTENCY_CONFLICT` | PASS |
| S8 | `hover` 返回平台声明的 `ROUTE_STATE_SOURCE_UNAVAILABLE`（而非笼统不完整） | PASS |
| S8b | `point_flight` 未被能力清单放行 | PASS |
| S8c | 预览阶段对 `/screen/uav/operation/control` 的写请求为 0 | PASS |
| S9 | 围栏结论经真实回环 HTTP 校验为 `clear`（含 `projectId`/`operatorId`/逐段 `converge` 比对） | PASS |
| S10 | `accepted`/`submitted`/未知状态都不是设备效果 | PASS |
| S11 | 服务停止后端口已关闭（`connect_ex != 0`） | PASS |
| S11b | 线程数回到基线（1/1），无遗留监听线程 | PASS |
| S11c | 全程真实写接口调用 0 次 | PASS |

## 六、仍缺失的真实能力

| 缺口 | 现状 | 影响 |
| --- | --- | --- |
| Java `route_status` | 遥测原帧没有该事实，快照恒 null 且已显式声明不可用 | 平台路径上 `route_pause`/`route_resume`/`hover` 一律 typed 拒绝 |
| 静态标注目标解析接口 | 飞控仓只有 `TargetResolver` 协议；Java 声明 `STATIC_ANNOTATION_SOURCE_UNAVAILABLE` | 平台路径无法把标注名解析成坐标，需任务下发链路传入 |
| 算法粒度启停 | 平台只有任务级 `videoDetect` 总开关 | `ai_algorithm_set` 固定 `ALGORITHM_LIFECYCLE_UNAVAILABLE` |
| 测绘作废原子事务 | 本服务没有拼图/断点/航线恢复的事务接口 | `MAPPING_TRANSACTION_UNAVAILABLE`，确认前即拒绝 |
| Java 动作能力清单 | `ACTIONS` 仍 59 项，不含 `hover`；`DJI_SUPPORTED` 不含指点/环绕 | 平台预览阶段即 `ACTION_UNAVAILABLE`，未伪造能力 |
| 设备回执到设备效果的映射 | `device_effect` 恒 `not_verified` | 任何回执都不能证明设备真的动了 |
| 前端与后端联调 | `src/v14` 是展示层，数据由宿主注入 | 尚无真实数据源接入，未验证真实交互 |
| 规划 Agent 真实草稿→交接 | 产出方已实现并单测，但未接现有 `task_draft` 主链路 | 端到端仍需在真实草稿流程里接线 |

## 七、证据分层

| 结论 | 等级 |
| --- | --- |
| 信封白名单/版本/绑定/幂等/几何/顺序/草稿规则 | **单元验证**（飞控仓 42 项 + 规划仓 17 项） |
| 契约块解析、typed 缺口优先于通用拒绝 | **单元验证**（快照 4 项） |
| Java 只声明真实缺口、契约块参与快照版本 | **单元验证**（Java 2 项，本包 53 项通过） |
| 规划→飞控交接、快照读取、围栏检查、端口回收 | **仅假 Java/HTTP 验证**（回环 17 项） |
| 模拟器内的 preview→confirm→execute | **仅模拟器验证**（前三阶段，本次未新增真实执行路径） |
| 平台指令是否被真实设备执行 | **未验证真实平台** |
| 真实设备动作、真实航线状态、真实测绘事务 | **未验证真实设备** |
| 前端在真实数据下的交互 | **未验证**（仅类型检查、构建与展示模型单测） |

**这些结果都不能证明真实飞行**：没有连接 MQTT、没有连接真实无人机、没有调用共享平台写接口；
`accepted`/`submitted` 只是链路状态，`device_effect` 恒为 `not_verified`。

## 八、已知限制

1. **`lingxi-agent` 主链路未接线**：`flight_handoff.py` 是独立产出方，尚未替换
   `task_draft_api` 的下发路径。真实草稿的字段形状与本契约的映射只做了显式字段搬运，
   没有改动既有下发逻辑。
2. **Java 只加契约块，未加接口**：没有新增 controller、没有改动作清单、没有新增数据库字段。
   `route_status` 等仍为空，只是从“静默 null”变成“显式不可用”。
3. **`ACTIONS` 仍 59 项**：飞控仓有 66 项动作，平台侧差额继续由 `ACTION_UNAVAILABLE` 拒绝，
   本次没有为了让数量对齐而伪造能力。
4. **前端是展示层组件**：`V14Panel.vue` 不发请求，`main.ts` 只读 `window.__V14_BOOTSTRAP__`；
   真实接入需要在宿主侧提供已校验数据。
5. **隔离脚本不在 CI 门禁内**：`tools/ci/offline_tests.sh` 仍只跑飞控仓单测，
   跨仓脚本依赖 `lingxi-agent` 源码路径，单独执行。
6. **Java 全模块测试未跑**：只跑了 `platformsnapshot`（53）与 `flightcommand`/`flightv13`（170）
   两个包；其余包含 MySQL/Redis 集成测试。

## 九、Git 与回滚

- 四个仓库均**未 push**、未合入共享 test、未部署、未重启共享服务。
- 飞控仓：在 `test@d6d284d` 工作区追加 P4 改动，保留 P1–P3 与 `.workbuddy-ai/`。
- 规划仓：在 `bugfix/algorithm-selection-integrity@860d9ed` 工作区新增 2 个文件，保留既有改动。
- Java：在 `ktd_backend-real-flight-assistant`（`test@ed13701f55`）改 2 个文件。
- 前端：在 `test@f72b81a` 新增 `src/v14/` 等 5 个路径。
- 回滚：全部为新增模块 + 追加字段，按文件删除/正向 revert 即可；已下发的平台指令无法用代码回滚撤销。
- 机器证据：[v14-cross-repo-20261022.json](evidence/v14-cross-repo-20261022.json)。
