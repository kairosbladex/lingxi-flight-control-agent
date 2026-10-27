# FLIGHT-V14 第二阶段：电子围栏有界调用与环绕曲线校验

2026-10-22。依据《灵犀平台自然语言智能控制功能设计规范 V1.4》2.2.2、4.3.1 与本次要求，
补齐围栏调用的**有界性**、六向微操/指点飞行/环绕的**路径几何**，以及 confirm 前的重复检查。
默认执行器仍是模拟器，`KtdPlatformExecutor` 仍默认关闭；未连接 MQTT、真实无人机或共享
test/uat/prod 服务，未执行任何平台写操作，未接入任何真实 GIS / 电子围栏接口。

## 基线

- 仓库 `lingxi-flight-control-agent`，分支 `test`，HEAD `d6d284d`（`feat: 补齐悬停动作与指点/环绕平台映射`）。
- 工作区包含第一阶段未提交改动（`authority_contract.py`、`target_resolver.py`、`task_events.py`、
  `command_receipts.py` 等），本次全部保留，未做任何回退。
- 测试分母统一按本轮最终实测 **1085**（见主审计 `ACCEPTANCE_FLIGHT-V14.md`）；阶段内的
  旧分母已作废，不再作为分母引用。

## 改前审计

| 面 | 改前状态 | 缺口 |
| --- | --- | --- |
| 围栏调用点 | preview/confirm 都调用 `check_path`（模拟器 685/809、平台 1437/1509、Zhuoyi 224/258） | 没有单次 deadline；超时只能靠 socket 默认超时，且迟到的 `clear` 仍会被当成通过 |
| provider 契约 | 返回 `PathCheck(status)`，只有 clear/blocked/unavailable 三态 | timeout、malformed、几何非法无法区分，全部塌缩成「不可用」 |
| 六向微操 | `KtdPathChecker` 用 `height_m` + `yaw_deg` 生成两点折线 | 方向相对机头这件事只写在代码里，没有契约和测试；高度基准没有显式选择 |
| 指点飞行 | `action not in MOVES` 直接返回 `unavailable` | 目标点从未参与围栏检查，「目标点 blocked 拒绝」实际不可达 |
| 环绕 | 同样返回 `unavailable`，注释写着「orbit needs a curve contract」 | 没有 36 段闭合曲线，没有起始段，没有分段定位 |
| HTTP 传输 | `check_path` 硬性要求 `len(points) == 2` | 多点折线会被直接判为 scope 不匹配 |

## 一、围栏调用有界

`flight_policy.py` 新增 `evaluate_path()` 与 `PathOutcome`，`check_path()` 改为它的薄封装：

- 六态：`clear` / `blocked` / `unavailable` / `timeout` / `malformed` / `invalid`。
  只有 `clear` 放行；`blocked` 抛 `PATH_BLOCKED` 拒绝；`invalid`（几何非法）拒绝；
  其余三态按 V1.4 2.2.2 降级并返回 warning。
- provider 缺失、返回 `None`、抛异常、返回非 `PathCheck`、返回未知状态分别映射到
  `PATH_CHECKER_UNAVAILABLE` / `PATH_CHECK_UNAVAILABLE` / `PATH_CHECK_ERROR` /
  `PATH_CHECK_MALFORMED`，不再出现「说不清就当成不可用但话术含糊」的情况。
- **迟到的 clear 不算通过**：`evaluate_path` 用 `time.monotonic()` 计量 provider 调用，
  超过 provider 声明的 `deadline_sec` 即降级为 `timeout`；`KtdPathChecker` 内部同样在
  调用结束后复核耗时，超预算一律改判超时。
- 降级话术按状态区分（`电子围栏接口不可用` / `电子围栏检查超时` / `电子围栏响应无法解析`），
  统一以「这不代表路径安全或设备避障已验证」结尾。
- 降级只跳过围栏这一项：控制权、任务状态、设备能力、确认机制全部在围栏之前或独立校验，
  没有被绕过（见 `SimulatorFenceTests.test_degraded_fence_still_requires_control_and_confirmation`）。

> 后续修正（2026-10-22）：本节把「provider 声明 `deadline_sec`」当作有界，属于过度承诺。
> 普通同步 callable 的 `deadline_sec` 只是协作式承诺，本仓无法强制中断；只有底层
> transport 的 socket 超时才是硬超时。契约分层与平台装配规则见
> [ACCEPTANCE_FLIGHT-V14-FENCE-BOUNDED.md](ACCEPTANCE_FLIGHT-V14-FENCE-BOUNDED.md)。

单次 deadline 落在两处：

- `KtdPathChecker(transport, deadline_sec=3.0)`：0.1–10 秒，非法值构造即拒绝。
- `KtdHttpTransport.check_path(..., timeout_sec=...)`：0.1–30 秒，并与 transport 自身的
  `timeout_sec` 取 `min`，最终作为 `opener.open(request, timeout=...)` 的 socket 超时。
  `_read_response` 增加 `timeout` 参数，仍是同一条同步调用，**没有**为了超时新建线程或进程。

审计：`flight_path_check`（动作、状态、原因码、分段数、分段序号、高度字段、耗时、预算、
drone 摘要）与 `flight_fence`（端点、分段数、超时预算、HTTP 状态、耗时）都是结构化字段，
不含经纬度、半径或响应原文。

## 二、六向微操路径

新增 `path_geometry.py`，把「动作 → 待校验折线」收敛成确定性函数：

- **方向相对机头**：`move_forward/backward/left/right` = 机头航向 +0/180/−90/+90 度，
  由 `geometry.relative_target` 落到球面坐标；机头航向缺失时返回
  `PATH_HEADING_UNAVAILABLE` 降级，不默认正北。
- **相对高度**：`move_up/down` 只在同一高度基准上加减 `distance_m`，经纬度不变；
  结果落到地面以下或超出字段区间即 `PATH_ALTITUDE_OUT_OF_RANGE` 拒绝。
- **高度字段显式化**：`resolve_vertical()` 返回 `(值, 字段名)`，六向微操优先 `height_m`
  （相对高度），指点/环绕优先 `altitude_m`（与 `/screen/uav/fly/control` 的 `height` 同基准，
  见 `_compile_fly`）。所选字段写进 `PathCheck.vertical_field` 并进审计，不再含糊。
- 距离非正、非有限、超过 1000 米、位姿越界都是 `PathGeometryError` → 拒绝执行。
- 模型只提供动作与参数，折线一律由本模块生成，**没有任何「模型建议路径」入口**。

## 三、指点飞行路径

- `point_flight_path()` 用当前位姿 + 目标 `longitude/latitude/altitude_m` 生成两点折线，
  目标高度按 `/screen/uav/fly/control` 的 2–10000 米区间校验，越界 `PATH_ALTITUDE_OUT_OF_RANGE`。
- **高度基准必须一致**：目标高度是飞控 `height` 基准；当前位姿只有 `height_m`（相对高度）时
  返回 `PATH_ALTITUDE_DATUM_MISMATCH` 降级，不把两个基准拼进同一条折线。
- 目标点围栏 `blocked` 在 preview 与 confirm 都拒绝（`PATH_BLOCKED`），确认卡不会生成/被撤销。
- confirm 用新的权威快照重算折线并比对快照哈希：目标点参数变化、快照版本变化、位姿变化
  都会使旧卡失效（`AUTHORITATIVE_STATE_CHANGED`）。
- 平台无对应能力时的既有 typed 边界保持不变：`action_availability` 不通过 → `ACTION_UNAVAILABLE`，
  能力位缺失 → `CAPABILITY_UNAVAILABLE`，非 `DJI_HANGAR` → `PROFILE_UNSUPPORTED`；
  三种都在编译阶段拒绝，**不发送猜测报文**。

## 四、环绕路径

`orbit_path()` 生成闭合环绕曲线：

- 以目标中心、`radius_m`、当前高度为基准，按 10 度步长采样 36 段，第 36 个点回到 0 度方位角
  形成**闭合边**（37 个环点）。
- 有当前位姿且与环起点不同时，额外在最前面加一个 `source=old` 的**进入段**，
  总分段 37（36 环段 + 1 起始段）；位姿缺失只跳过起始段，不否决请求。
- 半径非正/超 5000 米、经纬度越界、环点纬度越界、高度越界都是 `PathGeometryError` 拒绝。
- 响应解析只认 `projectId`/`operatorId`/`snapshot`/`invalid`/`segment[].status`/`converge`；
  `segment` 数量必须等于分段数，否则 `malformed`。`suggest`/`alternate` 等建议字段
  从不读取，也从不自动绕飞。
- `blocked` 时 `segment_index` 指出具体分段（起始段 blocked 即 0），环绕用
  「指令已拦截，环绕路径存在禁飞区。」。
- 平台不能下发精确半径：确认卡继续显示 `_platform_warnings` 的语义差异
  （「大疆环绕半径由进入时的水平距离决定，请求的 radius_m 不会下发」），不声称设备按请求半径执行。
- 环绕检查不可用/超时时按 V1.4 降级并警告，不阻断。

## 五、确认安全

- preview 与 confirm 各自独立调用 `check_path`；没有「首次通过就跳过」的路径。
- `blocked`/`invalid` 抛出后，模拟器与平台执行器都在同一处 `_pending.pop`，
  待确认卡被撤销，二次确认返回 `CONFIRMATION_NOT_FOUND`。
- 旧卡失效条件：目标/半径（请求指纹进入确认绑定）、当前位姿（模拟器新增
  `PATH_ACTIONS` 的位姿进 `policy_context`；平台权威快照哈希本就包含位姿）、
  快照版本（`AUTHORITATIVE_STATE_CHANGED`）。
- **围栏版本**：现有契约里没有围栏数据版本字段，本次没有编造；用「confirm 重新检查」代替，
  这一点在下方「未验证」里如实标注。

## 六、测试

新增 `tests/test_path_fence.py`（48 项）与改造 `tests/test_ktd_path.py`（净增 1 项），合计新增 49 项：

| 场景 | 用例 |
| --- | --- |
| provider clear / blocked / unavailable / timeout / 抛异常 / malformed | `FenceProviderTests` 全覆盖，含迟到 clear 改判超时 |
| 六向微操路径 | 相对机头方向、机头朝东时「向右」朝南、升降只改高度、非法距离/越界高度 |
| 坐标目标路径 | 目标高度用飞控基准、基准不一致降级、目标越界拒绝 |
| 环绕 36 段与闭合边 | 环点都在半径 30±0.5 米、相邻弧长 = 2R·sin(π/36)、首尾闭合 |
| 起始段 blocked | `segment_index == 0`、分段数 37 |
| confirm 前路径变 blocked | 模拟器与平台两条链都拒绝并撤销待确认卡 |
| 围栏降级仍要控制权与确认 | 无控制权 `CONTROL_NOT_OWNED`；有控制权仍 `preview_required` 后 `accepted` |
| 无后台残留 | 超时前后线程集合一致、无子进程、`bind`/`listen` 全程未被调用、路径模块无线程/进程/异步导入 |
| 审计不泄露 | `flight_path_check` 不含经纬度与半径 |

```bash
bash tools/ci/offline_tests.sh   # Ran 1085 tests in 14.041s / OK，含 socket 禁用守卫
git diff --check                 # 无输出（退出码 0）
python3 -m compileall -q src tests                       # 无输出（退出码 0）
```

本轮唯一分母为 **1085**；此前阶段的旧分母已作废，不再引用。

### 证据分层

| 结论 | 等级 |
| --- | --- |
| 六态围栏契约、超时改判、降级话术、几何生成、环绕 36 段闭合 | 单元验证 |
| 折线结构、单次 deadline 下发、`segment` 数量与回显校验 | 仅假传输验证（协议与状态机） |
| blocked 撤销待确认卡、降级不绕过控制权与确认、位姿变化失效 | 仅模拟器验证 |
| 平台执行器 preview/confirm 双检查、环绕半径语义差异 | 仅假传输验证（`_NoSubmit` 断言零下发） |
| 平台 `/electronicFence/path-route/with-clean` 是否接受 38 点折线 | **未验证真实平台** |
| 该接口 `altitude` 字段的基准（相对高度还是椭球高） | **未验证真实平台** |
| `converge=ln_outside` 的真实语义与 `invalid` 元素结构 | **未验证真实平台** |
| 围栏数据版本字段 | **不存在于现有契约，未实现** |
| 静态标注解析结果与目标点的绑定 | **未接入飞行链路**（第一阶段结论不变） |
| 任何真实避障、限高、禁飞区判定与设备动作 | **未验证真实设备** |

## 已知限制

1. **DNS 解析不受 socket 超时约束**：`opener.open(timeout=...)` 约束连接与读写，
   但 `getaddrinfo` 在部分平台仍可能阻塞。本仓不改系统解析器，也不为此起线程；
   平台地址是固定服务端 origin，属于已接受残余风险。
2. 若底层 transport 的 `check_path` 无法取消，本层只能「超时后丢弃结果」而不是抢占式中断；
   迟到结果一律改判 `timeout`，不会变成 `clear`。
3. 环绕折线的环点全部标 `source=new`、进入点标 `source=old`，这是本仓对 DTO 的用法，
   平台是否要求别的取值未验证。

## 行为变化与影响面

- `point_orbit` 缺少 `radius_m` 时，`KtdPathChecker` 从 `unavailable` 改为 `invalid`
  （`tests/test_ktd_path.py` 同步更新）。正常入口由 `_validate_parameters` 先拒绝，不受影响。
- 围栏降级话术从 V1.3 措辞改为 V1.4 措辞，`电子围栏接口不可用` 前缀保留，现有断言不变。
- 模拟器 `PATH_ACTIONS` 的 `policy_context` 新增位姿四项：预览后位姿变了，旧确认卡失效。
  这是本次唯一收紧点，没有改动默认执行器、动作注册表与 preview→confirm→execute 链路。
- `KtdHttpTransport.check_path` 从「只接受 2 点」放宽到 2–129 点，并逐点校验字段集合；
  单点或超长折线仍是 `TRANSPORT_SCOPE_MISMATCH`，不会发出请求。

## Git 与回滚

- 基于 `test@d6d284d` 在当前工作树修改，**未 push**、未合入共享 test、未部署、未重启共享服务。
- 保留 `.workbuddy-ai/` 与第一阶段全部未提交改动；未使用 `reset --hard`、`checkout --`、`clean`、`stash`。
- 回滚：本次为新增模块 + 兼容扩展，按提交正向 revert 即可。已提交的平台指令无法用代码回滚撤销。
