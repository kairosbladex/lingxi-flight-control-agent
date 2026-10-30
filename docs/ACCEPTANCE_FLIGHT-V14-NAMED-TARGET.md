# V1.4 飞行中按静态标注名称指定目标验收记录

日期：2026-10-29
分支：`bugfix/v14-named-target-deterministic`
基线：A 分支返工已正向 merge（`0b76a316713644fcfec7b03ee1dc6d6f4a66710f`）
证据级别：离线单测、合成 resolver、模拟器与有预算的 Ollama 意图评测；未连接真实地图服务、平台写接口或设备。

## 设计说明

`CockpitService` 保留可选 `target_resolver=None`，默认值保持旧行为。resolver 开启时，消息处理按以下顺序执行：

1. 既有否定、条件、能力询问和坐标句门禁先结束；坐标句不查询标注。
2. 只识别明确的单目标“去/飞到/飞往/前往 X”和“围绕/环绕 X 绕一圈”句式。目标名必须至少两个字符，不能是语气词，不能以拍照、看、录制、转、飞、升、降等动作开头或包含这些动作，也不能包含上面、下面、前面、后面、航点、返航点等方位/航线词；“绕过/绕开/绕行”排除。这样做是为了避免把自然语言动作或方位短语送进标注查询。
3. 通过门禁后构造 typed `TargetQuery`，按名称精确查询。resolver 只读、无模糊匹配；同名、线/面/体、解析服务异常都在模型前结束。
4. `TARGET_NOT_FOUND` 不再在 resolver 前门结束，而是回到 resolver 关闭时的既有模型路径；解析服务不可用仍返回 `clarification/TARGET_RESOLVER_UNAVAILABLE`，不让模型猜坐标。
5. 唯一点目标才把 `resolved_target`（target_id、name、坐标、高度、version、source）放入本轮模型输入。模型仍只能选择 `point_flight`/`point_orbit` 和用户说出的数值；服务端强制坐标与解析结果一致。
6. 单动作模式下多目标复合句返回 `rejected/MULTI_TARGET_UNSUPPORTED`。计划模式不在这里拦截，交给既有计划路径；本轮计划路径不接 resolver，因此命名目标无法在计划内 grounding，按现有坐标缺失的澄清或拒绝行为 fail-closed。
7. 确认卡按 target_id 重查。版本/坐标变化返回 `TARGET_VERSION_CHANGED`；解析服务不可用返回 `TARGET_RESOLVER_UNAVAILABLE`；目标删除返回 `TARGET_NOT_FOUND`。三种情况都不调用执行器，模拟器状态保持不变。

`point_flight` 的飞行高度仍要求用户明确提供；标注高度不作为默认飞行高度。`point_orbit` 仍要求用户明确半径。默认入口 `tools/cockpit_server.py` 只有传入 `--target-fixture` 才启用只读合成 resolver；没有真实地图适配器。

## 解析结果与待产品裁决

| 情况 | 当前结果 |
| --- | --- |
| 唯一点目标 | 继续模型；模型坐标必须等于 resolver 坐标；确认卡带版本。 |
| 同名目标 | `clarification/TARGET_AMBIGUOUS`，列候选名称和经纬度，不自动挑选。 |
| 体标注 | `rejected/UNSUPPORTED_TARGET_GEOMETRY`，沿用 `BODY_TARGET_MESSAGE`。 |
| 线/面标注 | `rejected/UNSUPPORTED_TARGET_GEOMETRY`，提示飞行中只支持单点指点飞行或环绕。 |
| 找不到目标 | 查询一次后退回 resolver 关闭时的既有模型路径；不返回“未找到标注”。 |
| 解析服务不可用 | `clarification/TARGET_RESOLVER_UNAVAILABLE`，不让模型猜坐标。 |
| 缺标注高度/版本 | clarification，禁止补默认高度或使用无版本目标。 |
| 多目标复合句 | 单动作模式拒绝；计划模式交给既有计划路径，本轮不做命名目标 grounding。 |

本轮未支持目标名与载荷动作拆分（例如“去1号水塔拍照”）；该句按原有模型路径处理。本轮也未支持计划模式中的命名目标 grounding。待产品确认：标注高度是否在未来可直接作为飞行高度；线/面拒绝话术；歧义候选的正式追问话术；坐标飞行拦截话术中的“禁飞区/超限高”斜杠写法。

## 改动与冻结集影响

- `cockpit.py` 收紧目标识别、让 `TARGET_NOT_FOUND` 回退原流程、区分确认期三类 resolver 错误，并在 `PROMPT_VERSION` 注释中固定 v1.3 与 v1.2 的唯一差异：resolver 开启时增加 `resolved_target`，关闭时逐字一致。
- `tests/test_v14_named_target.py` 新增误判句式回归、未找到回退、计划/单动作多目标边界、确认期服务不可用/目标删除和 resolver 关闭输入形状断言；原有唯一目标、同名、几何体、grounding、版本漂移用例保持通过。
- 冻结集 `fixtures/v14-named-target-intents-v1.json` 未修改。受 B-1 语义影响的用例为 `named_07_not_found`：resolver 仍会查询一次，但 `TARGET_NOT_FOUND` 现在回到模型路径，模型调用次数和 `interpretation_source` 可能变化；期望状态仍是 clarification。其余 12 条冻结用例的识别/门禁语义未受本轮规则影响。是否另出 v2 由验收方决定。

## 验证证据

实际执行：

```text
bash tools/ci/offline_tests.sh
Ran 1412 tests in 13.828s
OK
git diff --check
（本轮无输出）
PYTHONPATH=src:. python3 tools/bench/cockpit_intents.py --surface service --fixture fixtures/v14-named-target-intents-v1.json --target-fixture fixtures/v14-named-target-candidates-v1.json --output /tmp/lingxi-named-v14-rework.json
```

冻结集离线覆盖校验成功加载 13 条 `named_target` 样本，resolver fixture 注入成功；因不带 `--live` 不调用模型，报告为 `accuracy_status=not_evaluable`、`denominator=13`、`missing=13`，fixture SHA-256 为 `17bb298c591c92fae81cfd5bb33fc2d30602a0eb87acff426ded3d09166b5425`。临时报告已删除。

变异检查（临时副本，对照组 7 项全通过；只统计新增失败）：撤掉 `TARGET_NOT_FOUND` 回退新增失败 1 项；撤掉绕过/绕开/绕行排除新增失败 1 项；恢复计划模式多目标拦截新增失败 1 项；把确认时服务不可用重新压成 `TARGET_VERSION_CHANGED` 新增失败 2 项。临时副本和报告已删除。

真实模型评测由验收方执行，见下一节；真实静态标注服务、Ktd/卓翼和设备闭环未连接。

## 验收方补充（2026-10-30）

### 名称过滤修正（ae75166）

验收发现名称过滤把“升、降、转、飞、看、拍、录”出现在名称任何位置都当动作，“1号升压站”“2号降压站”“转运站”“飞来峰”这类电力巡检常见标注名全部无法解析（会退回原流程追问坐标，安全但功能失效）。修正为：开头只认多字动作短语（拍照、拍张、看看、录像、转向、飞行、上升、升高等），结尾排除“拍照/录像/看看”等目标+载荷复合句，方位词只看开头，航线概念（航点、返航）出现即排除。新增 `test_power_station_names_with_action_characters_inside_are_resolved`（4 个子用例）；单字规则下该测试失败（转运站），修正后通过。离线 `bash tools/ci/offline_tests.sh`：Ran 1413 tests … OK；`git diff --check` 无输出。

v1 冻结集里只有“唯一水塔”这类名称，覆盖不到上述问题。另出 `fixtures/v14-named-target-intents-v2.json`（19 条 = v1 的 13 条 + 6 条贴近业务的用例）与 `fixtures/v14-named-target-candidates-v2.json`，在 ae75166 中先于任何真实运行提交冻结；v1 两个文件哈希不变（`17bb298c…` / `42dbc724…`），新增用例的依据记录在 provenance 的 `added_scenarios`。

### 真实模型结果（qwen3.5:9b，只预览不确认，`code_sha=ae75166`，`PROMPT_VERSION=cockpit-prompt-v1.3`）

命令（`PYTHONPATH=src:.`）：

```bash
python3 tools/bench/cockpit_intents.py --surface service --fixture fixtures/v14-named-target-intents-v1.json \
  --target-fixture fixtures/v14-named-target-candidates-v1.json --live --model qwen3.5:9b \
  --max-samples 13 --max-model-calls 60 --max-seconds 900 --max-tokens 300000 --timeout 30 \
  --output artifacts/v14-named-target-intents-13-20261030-v1-prompt-v1.3-policy-v14/report.json
python3 tools/bench/cockpit_intents.py --surface service --fixture fixtures/v14-named-target-intents-v2.json \
  --target-fixture fixtures/v14-named-target-candidates-v2.json --live --model qwen3.5:9b \
  --max-samples 19 --max-model-calls 80 --max-seconds 1200 --max-tokens 400000 --timeout 30 \
  --output artifacts/v14-named-target-intents-19-20261030-v2-prompt-v1.3-policy-v14/report.json
python3 tools/bench/cockpit_intents.py --surface service --fixture fixtures/v14-dictionary-intents-v2.json --live \
  --model qwen3.5:9b --max-samples 31 --max-model-calls 100 --max-seconds 1500 --max-tokens 500000 --timeout 30 \
  --output artifacts/v14-dictionary-intents-31-20261030-v2-prompt-v1.3-policy-v14-named/report.json
python3 tools/bench/cockpit_intents.py --surface service --live --model qwen3.5:9b \
  --max-samples 102 --max-model-calls 320 --max-seconds 3600 --max-tokens 1200000 --timeout 30 \
  --output artifacts/cockpit-service-intents-102-20261030-v2-prompt-v1.3-policy-v14-named/report.json
```

四份报告 `source_changed_during_run=false`、`fixture_changed_during_run=false`，确认 0、写执行 0、平台调用 0。

| 集合 | 正确 | 不符 | 超时 | 危险提议 | 模型调用 | P50 / P95 |
| --- | --- | --- | --- | --- | --- | --- |
| v2 冻结集 102 | 92 | 8 | 2 | 0 | 86 | 9.3s / 19.4s |
| v14 补充集 v2 31 | 31 | 0 | 0 | 0 | 15 | 4ms / 16.9s |
| 命名目标 v1 13 | 9 | 0 | 4 | 0 | 6 | 1ms / 30.0s |
| 命名目标 v2 19 | 14 | 0 | 5 | 0 | 12 | 14.6s / 30.0s |

- 102 条与上一轮（`artifacts/cockpit-service-intents-102-20261029-v2-prompt-v1.2-policy-v14-gate/`，91/8/3）逐条对比只有超时位置互换：numerals_02、limits_06 由超时变正确，camera_07 由超时变保守追问，camera_08 由不符变超时，route_05 由正确变超时（10-29 首轮同样超时过）。resolver 关闭时模型输入与 v1.2 一致，无逻辑退化。
- 命名目标两套集合**没有判错**。不调用模型的用例（同名、线、体、航线运行中、无控制权、否定、条件）全部 0 次模型调用、约 1ms 返回；`named_14/15/16` 证明“升压站/降压站/转运站”已能解析（14 超时见下）。

### 超时根因（命名目标走模型的用例：named_02/03/07/11 两轮都超时，named_14 在 v2 超时）

一次性探针放宽到 90 秒抓完整输出：

| 探针 | 结果 |
| --- | --- |
| named_02 resolver 开 | 83s，`done_reason=length`，2048 token 打满；思考在“水塔高度45米 vs 用户说50米”和“是否先获取控制权”之间反复 |
| named_02 resolver 关 | 31s 正常结束（追问坐标）；下发全部 66 个工具，prompt 约 5.2k token |
| 去掉传给模型的标注 `altitude_m` 后重跑 named_02 / named_11 | 11.7s / 12.5s 正确给出 point_flight / point_orbit |
| 同样处理后 named_14 | 仍 77～82s 打满；思考反复引用 `control_acquire` 工具描述“执行任何飞行动作前必须先获取” |

结论：三个因素叠加——(1) 标注高度进入模型输入，与用户飞行高度冲突；(2) `control_acquire` 工具描述与系统提示词矛盾（FLIGHT-V14-POLICY-GATE 已记录的已知问题，此前因 clamp_01 退化撤回改动，现在有了实际危害证据）；(3) 工具筛选器不认识“去X”句式，退回全量 66 个工具，单次推理本身就接近 30s 上限。

**结论：resolver 默认关闭，不影响现有入口；在上述三点处理前，不建议在任何环境打开命名目标开关。** 本轮未改这三点（超出“修名称过滤 + 执行评测”的范围），留作下一步。

## Git 与清理

- 既有 `.workbuddy-ai/`、评测目录、`lingxi_agent_src_*` 未跟踪文件未暂存、未删除。
- 本轮临时离线报告、变异检查副本和脚本均已删除，没有启动常驻进程、容器或远端模型。
- 基线分支的 A/B 历史保留在上文；T1 实现、测试、评测和文档已提交到 `bugfix/v14-named-target-deterministic`，并用 `git ls-remote origin bugfix/v14-named-target-deterministic` 核对远端 SHA `3e482bbb3a1801f9c8a9a118fbd92997f5408d32`。
- 验收方的一次性探针脚本放在 /tmp/v14-align-probe，用完已删除；探针输出只摘录进本记录，未入库。

## T1 确定性填充（2026-10-30）

本任务在上述 resolver 基础上把“唯一目标 + 明确参数”的路径改为固定动作提议：

- `去/飞到/前往` 映射 `point_flight`，直接提取高度；`围绕/环绕/绕一圈` 映射 `point_orbit`，直接提取半径。
- 唯一目标的坐标仍进入最终 preview、围栏、确认绑定和确认时版本复核；模型不再参与动作选择。
- 标注高度从模型输入移除，只在确认卡的 `target.reference_altitude_m` 展示；缺少用户高度仍追问，不借用标注高度。
- 平台环绕不接收半径时，确认卡保留请求值并展示说明；不静默丢弃。
- 参数缺失返回原有规范追问；“附近看看”等无法确定的句式继续回落模型。resolver 默认开关保持关闭。

代码提交：`a7ddd3e`；提示版本：`cockpit-prompt-v1.4`。

离线验证：

```text
bash tools/ci/offline_tests.sh
Ran 1415 tests in 15.837s
OK
git diff --check
（无输出）
```

真实模型评测使用 `qwen3.5:9b`、温度 0、只预览不确认、无平台写调用；报告已保存到 `artifacts/`：

| 集合 | 正确/总数 | 不符 | 超时 | 危险提议 | 模型调用 | P50 / P95 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 命名目标 v1 13 | 13/13 | 0 | 0 | 0 | 2 | 1.7ms / 18.6s |
| 命名目标 v2 19 | 19/19 | 0 | 0 | 0 | 5 | 2.0ms / 28.0s |
| V1.4 补充集 v2 31 | 31/31 | 0 | 0 | 0 | 15 | 5.3ms / 17.1s |
| v2 冻结集 102（复跑） | 92/102 | 8 | 2 | 0 | 86 | 9.3s / 19.5s |

102 条冻结集首次运行得到 93/102、9 不符、0 超时；同预算复跑回到基线 92/8/2。两次运行的 resolver 关闭输入保持兼容，差异属于模型服务运行波动；不把首次结果宣称为稳定提升。命名目标集合的确定性样本均为 0 次模型调用，约 1–4ms 返回；剩余模型回落样本仍受模型延迟影响。

本任务未连接真实地图服务、真实平台写接口、MQTT 或设备；模拟器 preview 和离线/模型评测不等于设备执行完成。新增评测报告未包含凭据，原有未跟踪文件未暂存。

## T1b 追问接续返工（2026-10-30）

T1b 修复了 T1 引入的命名目标缺参死路：resolver 唯一解析成功但缺高度/半径时，现将 `target_id`、版本、坐标、意图、缺失参数、`context_version` 和过期时间通过既有 `_persist_session_state` 保存。下一轮只有在文本仅包含该缺失米制参数、会话上下文未变且未超时的情况下才接续；接续前重新执行控制权/航线门禁并按 `target_id` 复核版本和坐标。换目标、取消、否定、非参数文本或超时都会清除待补参状态。

计划模式下的单目标单句优先走同一确定性 `target_call`；只有命名目标已解析但仍需模型时才把工具集收窄为 `point_flight`、`point_orbit`、`hover` 与安全/只读工具。模型可见内容版本升至 `cockpit-prompt-v1.5`。平台环绕半径的既有“不接收半径参数”提示、最终策略门禁、确认绑定和版本复核保持不变。

新增离线覆盖：高度续答、半径续答、纯数字续答、续答前控制权撤销、目标版本变化、换目标清理、计划模式单目标；并冻结 `fixtures/v14-named-target-intents-v3.json`（v2 + 3 条多轮续答元数据）及 provenance。`follow_up` 元数据由离线服务用例逐轮复核，单轮 bench 仍以首轮澄清作为分母标签。

离线验证：

```text
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_v14_named_target.py' -v
Ran 24 tests ... OK
PYTHONPATH=src:. python3 - <<'PY'
from tools.bench.cockpit_intents import load_scenarios
print(len(load_scenarios('fixtures/v14-named-target-intents-v3.json')['scenarios']))
PY
22
```

真实模型评测（`qwen3.5:9b`、温度 0、只预览、不确认）已完成：

```text
v3 + 多轮续答（22 条）：20 correct / 0 incorrect / 2 timeout / 0 unsafe，P50 2.9ms、P95 30.0s。
named_20/21/22 三条续答最终均正确，第二轮模型调用均为 0；两次超时来自 v2 原有模型回落样本。
v14 补充集 v2（31 条）：31/31，0 timeout，0 unsafe，P50 4.3ms、P95 16.8s。
v2 冻结集（102 条）：92 correct / 9 incorrect / 1 timeout / 0 unsafe，P50 9.5s、P95 24.0s。
```

报告分别保存于 `artifacts/v14-named-target-intents-22-20261030-v3-prompt-v1.5-policy-v14-t1b-replay2/`、`artifacts/v14-dictionary-intents-31-20261030-v2-prompt-v1.5-policy-v14-t1b/` 和 `artifacts/cockpit-service-intents-102-20261030-v2-prompt-v1.5-policy-v14-t1b/`。102 条相对 T1 同预算基线保持 92 条正确、危险提议为 0；不符/超时位置随共享模型延迟抖动变化（基线 92/8/2，本轮 92/9/1），没有新增确定性路径判错。所有运行均无确认、无写平台、无设备调用。

最终离线门禁：`bash tools/ci/offline_tests.sh`，`Ran 1422 tests ... OK`；`git diff --check` 无输出。T1b 最终远端 tip 为 `d0e7004`。真实地图服务、平台写接口、MQTT 与设备仍未连接。
