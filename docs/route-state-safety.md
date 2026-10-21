# 航线启动、暂停与恢复的模拟器状态校验

本切片为模拟器增加确定性的航线前置条件，并使航线变化后的旧确认卡失效。它不证明模型理解率提高，也不代表 Java 平台或真实无人机已验证。

任务范围与基线：2026-10-16，仓库 `lingxi-flight-control-agent`，分支 `feature/natural-language-cockpit`，开始代码 `eec1631992a27f51ee42eb50f5965a3036b486cb`。运行身份为本机测试进程，测试操作者及资源均为合成值。允许副作用限于本地源码、测试、本文及进程内模拟状态；未调用模型、Java、网络或设备，未部署。并行任务文件保持原样；此子切片不提交或推送，由主任务统一复核。

## 已复现的问题

旧模拟器在获取控制权后，即使 `route_id=null`、`route_status=idle`，`route_resume` 和 `execute_route` 都能获得确认卡，并在确认后返回 `accepted`。恢复产生 `route_status=running, task_status=idle`；启动产生 `route_status=running, task_status=route_executing`。缺陷来自执行器仅校验参数、控制权及确认绑定，没有检查航线是否存在、当前状态是否支持动作。

交叉审查还复现了同一个 `CockpitService` 内两个会话的交错：A 在暂停状态生成恢复卡，B 急停并确认后，A 的旧卡仍能恢复航线，得到 `route_status=running, task_status=emergency_stopped`。释放控制权后重新获取，也能复用先前的航线卡。这些路径虽没有同时执行 Python 指令，仍会跨会话恢复过期意图；服务锁不能代替确认内容的有效性检查。

旧冻结集 `fixtures/intent_scenarios.json` 的 `route_06` 是另一层问题：文本为“执行已经上传的航线”，标签为 `execute_route`，但继承了全局 `route_status=paused`、`route_id=synthetic-route`、`breakpoint_index=2`。`route_01` 的暂停请求也继承已暂停状态。这些状态使“首次启动”与“恢复暂停”的场景含义不够清晰；暂停的航线也可能此前已上传，不能仅凭该样本确定模型误选的唯一原因。

原冻结集、标签及 `docs/evidence/cockpit-intents-20261016.json` 均保留。原报告中 `route_06` 的 `execute_route → route_resume` 不匹配仍然是不匹配。后续模型对照应另建版本，分别提供与意图一致的 `uploaded/running/paused` 状态，并保留同一句输入在冲突状态下的澄清场景；不能改旧标签或把执行器拒绝算作模型识别正确。

## 软件前置条件

三项动作都要求非空航线 ID 和当前操作者仍持有控制权。满足条件后仍须逐项确认。

| 动作 | 必需的模拟器航线状态 | 允许的迁移 |
| --- | --- | --- |
| `execute_route` | `uploaded` | 已上传 → 运行 |
| `route_pause` | `running` | 运行 → 暂停 |
| `route_resume` | `paused` | 暂停 → 运行 |

预览不满足条件时返回 `rejected`，不生成确认卡，不改变模拟状态。没有航线时使用 `ROUTE_NOT_LOADED`；状态不匹配时使用 `ROUTE_STATE_MISMATCH`。启动、暂停和恢复不会互相改写。

确认卡在原有资源、请求、上下文版本绑定外，额外绑定航线 ID、航线状态、断点和进程内航线确认代次。确认时重新核对控制权及航线条件；航线或相关飞行意图变化返回 `ROUTE_STATE_CHANGED` 并作废旧卡，仍未持有控制权时返回 `CONTROL_NOT_OWNED` 并作废旧卡。直接调用模拟器再次提交失效卡得到 `CONFIRMATION_NOT_FOUND`；通过 Cockpit 重放同一次确认时保留第一次拒绝回执。

航线确认代次跟随上传、启动、暂停、恢复、断点继续、指点飞行及盘旋变化。同 ID 重新上传，或暂停后恢复到相同状态，也不能复用旧卡。急停、起飞、降落、备降、返航、取消返航会改变飞行意图，因此也使旧航线卡失效；释放控制权使旧卡失效，重新获取不会使其复活。相机动作不改变代次，不会无故使航线卡失效。已确认卡的幂等行为保持不变。

这是内存模拟器的确认约束，不增加公开工具名、参数 schema、解析器行为或 API 参数。`continue_from_breakpoint` 在本切片中只使已有航线卡失效；其自身前置条件、航点长度与断点合法性仍需单独建立完整契约。真实 Java 的状态值和回执来源也未在本切片核验，不能直接照搬这些模拟器字符串。

本切片只作废上述三类已生成的航线卡，没有定义急停、降落后新航线请求的完整飞行状态机。另有已复现的并发边界：绕过 `CockpitService` 的服务锁，直接在多个线程调用同一个模拟器时，确认检查与应用动作并非原子操作，检查后替换航线可能让旧卡作用于新航线。同一个 Cockpit 服务内的预览、确认和取消由其锁串行化；这不能证明多个服务共享一个模拟器或直接执行器并发安全，本轮未扩展修复该旧边界。

## TDD 与验证

先通过公开的 `preview/confirm` 接口新增 13 项测试，原实现得到 15 个失败断言（含参数化子用例）；合法完整流程、重复确认及相机共存正例已通过。再添加状态校验，13 项全部通过。测试覆盖缺航线、状态不符、合法上传 → 启动 → 暂停 → 恢复、旧卡重放、状态往返、同 ID 重传、替换航线、指点飞行、控制权变化和幂等。

交叉审查后再新增 3 项公开 `CockpitService.handle` 测试：先观察到 22 个失败断言，再补充使确认代次变化的相关动作。覆盖单独的跨会话急停复现、三类航线卡分别遇到六种飞行意图变化，以及三类航线卡在释放并重新获取控制权后的确认与重放。修复后航线测试合计 16 项通过，旧卡拒绝前后模拟状态完全一致。

原 `tests/test_full_control.py` 从 `idle` 直接暂停的用例先实际失败，再调整为先上传、启动，保留“执行成功”的原断言。全动作注册测试也先构造合法航线生命周期，并增加这四项回执必须 `accepted` 的断言。没有将原非法执行的成功期望改成拒绝来隐藏缺陷。

已执行的离线检查：

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B -m unittest discover -s tests -p 'test_route_safety.py' -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B -m unittest discover -s tests -p 'test_full_control*.py' -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B -m unittest discover -s tests -p 'test_executor.py'
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B -m unittest discover -s tests -p 'test_observation_projection.py'
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests python3 -B -m unittest test_full_control test_full_control_registry test_executor test_observation_projection test_cockpit -v
git diff --check
```

结果分别为 16、21、18、10 项通过。交叉审查修复后，最后一条组合命令重新验证受影响模块，共 73 项通过，其中包含 Cockpit 的 24 项测试；`git diff --check` 通过。主任务需统一执行全量离线回归。这里的测试分母是软件契约用例数；没有新的模型评测、人工标签或飞行真值，业务准确率仍为 `not_evaluable`。

回退时仅回退本切片的 `full_control.py`、新增航线测试及受影响测试构造；不覆盖并行任务改动，不改动原冻结数据和失败报告。对接 Java、扩大动作状态覆盖或宣称真实飞行效果前，仍需权威状态、设备回执及相应授权和验证。
