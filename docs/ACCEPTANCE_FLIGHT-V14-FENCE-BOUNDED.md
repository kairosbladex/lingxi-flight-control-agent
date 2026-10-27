# FLIGHT-V14 验收修复：围栏 provider 的硬超时边界（第二批）

2026-10-22。第一批把「有界」从「事后计时」升级为「调用前先验证契约」，但当时把
**只声明 `deadline_sec` 的普通同步 callable** 也算作有界。那是一个虚假的硬超时保证：
`deadline_sec` 只是 provider 的协作式承诺，本仓没有任何机制能停止一个违约的阻塞调用。
本次把 provider 契约分成三层，并把「协作式」彻底挡在平台执行路径之外。

默认执行器仍是模拟器，`KtdPlatformExecutor` 仍默认关闭；未连接 MQTT、真实无人机或
共享 test/uat/prod 服务，未执行任何平台写操作，未接入任何真实 GIS / 电子围栏接口。
**本文所有 clear/blocked 结论都来自假 provider 与假传输，不代表真实路径安全或设备避障已验证。**

## 结论先说

- 本仓**不强制中断任意同步 Python callable**。没有线程、进程、`ThreadPoolExecutor`、
  daemon thread、子进程或 asyncio 取消。
- 真正能停止等待的只有**底层 transport 的 socket 超时**：`KtdHttpTransport` →
  `opener.open(request, timeout=...)`。
- 「返回得晚」只说明这次结论超出预算、不可采信，**不等于调用已经被停止**。
- 只声明 `deadline_sec` 的普通 callable 默认按 `PATH_CHECKER_UNAVAILABLE` 降级，
  且不能装配到平台执行器；它只允许模拟器与离线测试使用。

## 一、契约分层（`flight_policy.py`）

`classify_path_provider(provider)` 在**调用前**返回三种契约之一：

| 契约类 | 判定条件 | 能给出的保证 | 允许的调用方 |
| --- | --- | --- | --- |
| `HardBoundedPathProvider` | `check_path(binding, action, parameters, state, *, timeout_sec=/deadline=)`，或标记 `hard_bounded = True` 且声明 `deadline_sec` | 预算交给底层 transport，由 socket 超时结束等待 | 平台执行器 + 模拟器 |
| `CooperativePathProvider` | 4 参数普通 callable，只声明 `deadline_sec`（0.1–10 秒） | 只是 provider 自己的承诺；违约时本仓无法停止等待 | 仅模拟器 / 离线测试（`allow_cooperative=True`） |
| `UnboundedPathProvider` | `None`、签名不匹配、预算缺失/非法、显式 `hard_bounded = False` | 无 | **任何情况下都不调用** |

- `evaluate_path(..., allow_cooperative=False)` 是默认值。协作式 provider 在平台默认下
  直接返回 `unavailable / PATH_CHECKER_UNAVAILABLE`，不调用。
- `UnboundedPathProvider` 不调用，直接降级；`None` 的 reason 也是
  `PATH_CHECKER_UNAVAILABLE`。
- `check_path(..., allow_cooperative=False)` 只是薄封装，默认值不允许协作式。
- 声明了 `deadline_sec` 但取值不可用（`0`、负数、`nan`、`inf`、超出 0.1–10、非数值）
  一律 `PATH_CHECKER_UNBOUNDED`，不调用。
- 形状校验用 `inspect.signature().bind(...)`：只接受 2 个参数的传输层不会被误当成
  4 参数 provider 调用。
- `clear` 仍是唯一放行状态；`blocked` 仍抛 `PATH_BLOCKED`；`invalid`（几何非法）仍拒绝；
  `timeout` / `unavailable` / `malformed` 仍按 V1.4 2.2.2 降级，warning 文案不变。

**迟到结论**：调用结束后复核耗时，`clear` 与 `blocked` 超预算一律改判
`timeout / PATH_CHECK_DEADLINE_EXCEEDED`。这条规则的含义是「本次响应不可采信」，
**不是**「调用已被停止」。`invalid` 是本仓本地几何结论、不来自 provider 响应，因此
**不参与**迟到改判（否则「几何非法」会被迟到改判成可降级而放行）。

## 二、本仓不强制中断任意同步 Python callable

这是本次修复的核心边界，必须写清楚：

- 没有 daemon thread、`ThreadPoolExecutor`、`asyncio`、`subprocess` 或 fork；
- 不会把仍在运行的调用当作已停止；
- 不会因为 `elapsed > deadline_sec` 就宣称「已取消」；
- 注入一个永久阻塞的普通 callable，平台路径**根本不会调用它**；如果它被绕过装配
  直接塞进 `_path_checker`，执行期也只会立即降级，不会等它返回。

`tests/test_path_fence.py::ResourceHygieneTests` 断言：整轮超时/无界/迟到路径前后
`threading.enumerate()` 与 `multiprocessing.active_children()` 不变，且围栏期间不
`bind`/`listen` 任何端口；`test_path_modules_do_not_spawn_background_work` 扫描
`flight_policy` / `ktd_path` / `path_geometry` 源码，禁止出现 `threading`、
`multiprocessing`、`subprocess`、`ThreadPoolExecutor`、`asyncio`、`fork(`。

## 三、标准平台链路：唯一真正有界的地方

- `KtdPathChecker(transport, deadline_sec=3.0)` 的 `hard_bounded` 是**属性**：
  只有 `transport.check_path` 真的接收 `timeout_sec` / `deadline` 时才为 `True`。
  传输层不接受单次预算时，`hard_bounded` 为 `False`，分类为 `UnboundedPathProvider`。
- 预算向下传递：`KtdPathChecker._send()` → `transport.check_path(..., timeout_sec=deadline_sec)`
  → `KtdHttpTransport.check_path` 与自身 `timeout_sec` 取 `min` →
  `_read_response(..., timeout=)` → `opener.open(request, timeout=...)`。
- 超时后 `KtdPathChecker` 把 `TimeoutError` / `URLError(reason=TimeoutError)` 映射为
  `timeout / PATH_CHECK_TIMEOUT`；不会重试，不会起后台线程。
- 迟到 `clear`/`blocked` 在 checker 内同样改判 `timeout / PATH_CHECK_DEADLINE_EXCEEDED`。
- 环绕仍是 36 段 + 闭合边（37 环点）+ 起始段（共 37 段），`suggest` / `alternate` 仍被忽略。

**已知残余风险**：DNS 解析（`getaddrinfo`）发生在 `open()` 之前，不受 socket 超时约束；
连接建立、TLS 握手与响应读取才受 `timeout` 约束。这条风险继续保留，不声称已消除。

## 四、平台执行器装配规则

| 装配输入 | `KtdPlatformExecutor` / `ZhuoyiPlatformExecutor` 行为 |
| --- | --- |
| `HardBoundedPathProvider` | 正常装配，preview 与 confirm 都检查 |
| `CooperativePathProvider` | **构造期 `ValueError`**，消息含 `PATH_CHECKER_UNAVAILABLE: cooperative-only ...` |
| `UnboundedPathProvider` | 允许装配，但 preview/confirm 一律 `PATH_CHECKER_UNAVAILABLE`，不调用 |
| `path_checker=None` | 允许装配，preview/confirm 一律 `PATH_CHECKER_UNAVAILABLE`，保留 V1.4 降级语义 |

- 平台没有 hard bounded provider 时，装配期或第一次执行会明确给出
  `PATH_CHECKER_UNAVAILABLE`，不会静默把协作式 provider 当成有界。
- 绕过构造直接改写 `_path_checker`（外部误用）时，`evaluate_path` 仍按契约拒绝调用，
  因此不会出现「装配后才发现被挂死」。
- 降级只跳过围栏这一项：控制权、任务状态、设备能力、确认机制、测绘中断顺序都不变，
  V1.4「围栏不可用则降级继续」的业务语义保持不变。

## 五、模拟器 / fake transport / 真实平台的边界

| 场景 | provider | `allow_cooperative` | 回执 `execution_mode` | 能证明什么 |
| --- | --- | --- | --- | --- |
| 内存模拟器 | 协作式 fake（`StaticFenceProvider`） | `True` | `simulator` | 只验证状态机与话术 |
| 平台执行器 + 假传输 | `KtdPathChecker(FenceTransport())` | `False` | `platform` | 只验证协议形状、预算下发与降级 |
| 平台执行器 + 真实 `KtdHttpTransport` | `KtdPathChecker(KtdHttpTransport(...))` | `False` | `platform` | 才涉及真实 socket 超时；**仍未验证真实围栏结论** |

模拟器允许协作式 fake，但每条回执都带 `execution_mode=simulator`，不冒充真实平台
运行状态。`fake transport` 的 `timeout_sec` 只是记录参数，不产生真实网络等待；
只有真实 `KtdHttpTransport` 的 socket 超时才是有验证意义的硬超时。

## 六、调用点核查

| 调用点 | 位置 | 结论 |
| --- | --- | --- |
| `MemoryFlightControlSimulator.preview` | `full_control.py:722` | `allow_cooperative=True`，仅模拟器 |
| `MemoryFlightControlSimulator.confirm` | `full_control.py:1012` | 同上；preview 与 confirm 各查一次 |
| `KtdPlatformExecutor.preview` | `ktd_platform.py:1869` | 默认 `allow_cooperative=False` |
| `KtdPlatformExecutor.confirm` | `ktd_platform.py:1955` | 同上；在测绘中断之后、提交之前 |
| `ZhuoyiPlatformExecutor._policy` | `zhuoyi_platform.py:228` | 默认 `allow_cooperative=False`，preview/confirm 共用 |
| 平台运行时装配 | `platform_runtime.py:161` | `KtdPathChecker(KtdHttpTransport(...))`，`hard_bounded=True` |
| 跨仓联调脚本 | `tools/ci/cross_repo_isolation.py:339` | 同上，预算默认 3.0 秒 |

结论：**没有**绕过 `evaluate_path` 的围栏调用点；`evaluate_path` / `check_path` 之外
不存在直接调用 provider 的代码。

## 七、测试

`tests/test_path_fence.py` 73 项、`tests/test_ktd_path.py` 6 项。关键用例：

| 要求 | 用例 |
| --- | --- |
| hard bounded provider 收到 `timeout_sec` | `ProviderContractTests.test_hard_bounded_provider_is_called_and_receives_the_budget`、`FenceProviderTests.test_timeout_kwarg_provider_receives_the_budget` |
| 只声明 `deadline_sec` 的 callable 是协作式、平台默认不调用 | `test_plain_deadline_callable_is_cooperative_not_hard_bounded` |
| 协作式 provider 被平台装配拒绝 | `test_cooperative_provider_is_rejected_at_platform_assembly`、`test_zhuoyi_platform_assembly_also_rejects_cooperative_provider` |
| 无界 provider 不被调用 | `test_provider_without_a_budget_keyword_is_unbounded_and_not_called`、`PlatformFenceTests.test_unbounded_provider_degrades_on_the_card_without_being_called` |
| 永久阻塞 callable 不进入平台执行路径 | `test_permanently_blocking_callable_never_enters_the_platform_path` |
| `KtdHttpTransport` 超时真正返回 | `test_ktd_http_transport_timeout_really_returns`（断言预算到达 `opener.open`） |
| 迟到 clear/blocked 降级为 timeout | `test_late_clear_beyond_declared_budget_becomes_timeout`、`test_late_blocked_is_reclassified_as_timeout`、`CheckerContractTests.test_late_blocked_conclusion_is_reclassified_as_timeout` |
| 迟到不改判本地几何非法 | `test_late_local_geometry_rejection_is_never_reclassified_as_pass` |
| 传输层无预算 → 不调用 | `CheckerContractTests.test_transport_without_a_single_call_budget_is_never_called`、`test_ktd_path.KtdPathTests.test_transport_without_a_single_call_budget_is_not_called` |
| 无 provider → `PATH_CHECKER_UNAVAILABLE` 降级 | `test_platform_without_a_provider_degrades_to_unavailable`、`FenceProviderTests.test_missing_provider_degrades_but_still_reports` |
| simulator 与 platform 边界 | `test_simulator_accepts_cooperative_fake_but_keeps_simulator_mode`、`test_cooperative_provider_is_rejected_at_platform_assembly` |
| 无遗留线程/进程/端口 | `ResourceHygieneTests` 三项 |

实际结果：

```
bash tools/ci/offline_tests.sh
Ran 1085 tests in 14.041s
OK
```

`git diff --check` 无输出（通过）；`python3 -m compileall -q src tests` 无输出（退出码 0）。
本轮唯一分母为 **1085**（含 `test_offline_guard`；守卫内部重跑 1084 项）；定向
`test_path_fence` + `test_mapping_interruption` 为 119 项 / 3.264 秒。

## 八、行为变化

1. **只声明 `deadline_sec` 的普通 callable 从「有界」降为「协作式」**：平台默认
   `PATH_CHECKER_UNAVAILABLE` 且不调用；模拟器仍可用（`allow_cooperative=True`）。
2. 协作式 provider 在 `KtdPlatformExecutor` / `ZhuoyiPlatformExecutor` 构造期被拒绝
   （`ValueError`，消息含 `PATH_CHECKER_UNAVAILABLE`），不再等到第一次 preview。
3. `KtdPathChecker` 的 `hard_bounded` 取决于传输层是否接收单次预算；无预算传输层
   不再被视为有界。
4. 迟到 `blocked` 仍按第一批口径改判 `timeout` 降级：迟到结论不作为本次快照的有效
   拦截证据；平台链路上 socket 超时 ≤ checker 预算，实际很难触发。
5. 仓外若注入了「只声明 `deadline_sec` 的 provider」，需要改成接受 `timeout_sec`
   的硬超时形状，或只在模拟器里使用。

## 九、未验证 / 未做

- 未连接真实 GIS / 电子围栏服务，`/electronicFence/path-route/with-clean` 的真实时延、
  折线长度上限、`altitude` 基准、`converge` 语义仍未验证。
- 未做真实设备或平台写操作；`clear` 只表示「假传输回显的协议结论」，不代表路径安全。
- 未验证真实网络下的 socket 超时行为（只用注入 opener 验证预算传递与异常映射）。
- 未引入线程/进程级强制中断，也未验证「违约 provider 会被杀死」——这是有意不做的。
- DNS 解析阻塞仍是已知残余风险。
