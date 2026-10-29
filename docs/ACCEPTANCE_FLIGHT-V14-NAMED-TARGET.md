# V1.4 飞行中按静态标注名称指定目标验收记录

日期：2026-10-29
分支：`feature/v14-flight-named-target`
基线：A 分支返工已正向 merge（`0b76a316713644fcfec7b03ee1dc6d6f4a66710f`）
证据级别：离线单测、合成 resolver 与模拟器；未连接真实地图服务、模型服务或设备。

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
Ran 1412 tests in 14.716s
OK
git diff --check
（本轮无输出）
PYTHONPATH=src:. python3 tools/bench/cockpit_intents.py --surface service --fixture fixtures/v14-named-target-intents-v1.json --target-fixture fixtures/v14-named-target-candidates-v1.json --output /tmp/lingxi-named-v14-rework.json
```

冻结集离线覆盖校验成功加载 13 条 `named_target` 样本，resolver fixture 注入成功；因不带 `--live` 不调用模型，报告为 `accuracy_status=not_evaluable`、`denominator=13`、`missing=13`，fixture SHA-256 为 `17bb298c591c92fae81cfd5bb33fc2d30602a0eb87acff426ded3d09166b5425`。临时报告已删除。

变异检查（临时副本，对照组 7 项全通过；只统计新增失败）：撤掉 `TARGET_NOT_FOUND` 回退新增失败 1 项；撤掉绕过/绕开/绕行排除新增失败 1 项；恢复计划模式多目标拦截新增失败 1 项；把确认时服务不可用重新压成 `TARGET_VERSION_CHANGED` 新增失败 2 项。临时副本和报告已删除。

真实模型评测（新冻结集、v1.3 后 102 条与 31 条全量重跑）待验收方执行；真实静态标注服务、Ktd/卓翼和设备闭环也未连接。本轮不宣称真实模型或设备验证完成。

## Git 与清理

- 既有 `.workbuddy-ai/`、评测目录、`lingxi_agent_src_*` 未跟踪文件未暂存、未删除。
- 本轮临时离线报告、变异检查副本和脚本均已删除，没有启动常驻进程、容器或远端模型。
- 本分支包含 A 的正向 merge 与本轮 B 代码、测试、文档；推送后用 `git ls-remote origin feature/v14-flight-named-target` 核对最终远端 SHA。
