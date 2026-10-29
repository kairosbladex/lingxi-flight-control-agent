# V1.4 飞行中按静态标注名称指定目标验收记录

日期：2026-10-29  
分支：`feature/v14-flight-named-target`  
基线：`f4c87c851c3cd5db9d1b7d45d35090eba604362b`  
证据级别：离线单测、合成 resolver 与模拟器；未连接真实地图服务、模型服务或设备。

## 设计说明

`CockpitService` 增加可选 `target_resolver=None`，默认值保持旧行为。启用 resolver 时，消息处理按以下顺序执行：

1. 既有否定、条件、能力询问和坐标句门禁先结束；坐标句不查询标注。
2. 对明确的“去/飞到/飞往/前往 X”和“围绕/环绕 X 绕一圈”句式，先检查控制权，再复用 `check_flight_policy` 检查航线状态；非测绘 `running` 直接返回 `ROUTE_PAUSE_REQUIRED`，测绘 `running` 保留确认流程。
3. 通过门禁后构造 typed `TargetQuery`，按名称精确查询。resolver 只读、无模糊匹配；同名、线/面/体、找不到和服务异常都在模型前结束。
4. 唯一点目标才把 `resolved_target`（target_id、name、坐标、高度、version、source）放入本轮模型输入。模型仍只能选择 `point_flight`/`point_orbit` 和用户说出的数值；服务端强制坐标与解析结果逐字一致。
5. 确认卡公开 `target_id/name/version/source` 与 `target_binding`。确认时按 target_id 重新解析，版本或坐标改变返回 `TARGET_VERSION_CHANGED`，不调用执行器。

`point_flight` 的飞行高度仍要求用户明确提供；标注高度只作为标注事实，不作为默认飞行高度。`point_orbit` 仍要求用户明确半径。默认入口 `tools/cockpit_server.py` 只有传入 `--target-fixture` 才启用只读合成 resolver；没有真实地图适配器。

## 解析结果与待产品裁决

| 情况 | 当前结果 |
| --- | --- |
| 唯一点目标 | 继续模型；模型坐标必须等于 resolver 坐标；确认卡带版本。 |
| 同名目标 | `clarification/TARGET_AMBIGUOUS`，列候选名称和经纬度，不自动挑选。 |
| 体标注 | `rejected/UNSUPPORTED_TARGET_GEOMETRY`，沿用 `BODY_TARGET_MESSAGE`。 |
| 线/面标注 | `rejected/UNSUPPORTED_TARGET_GEOMETRY`，提示飞行中只支持单点指点飞行或环绕。 |
| 找不到/解析服务不可用 | clarification，不让模型猜坐标。 |
| 缺标注高度/版本 | clarification，禁止补默认高度或使用无版本目标。 |
| 多目标复合句 | `rejected/MULTI_TARGET_UNSUPPORTED`，不拆成多个动作。 |

待产品确认：标注高度是否在未来可直接作为飞行高度；线/面拒绝话术；歧义候选的正式追问话术；坐标飞行拦截话术中的“禁飞区/超限高”斜杠写法（第一部分记录）。

## 改动与评测入口

- `target_resolver.py` 增加只读 `FixtureTargetResolver`，真实适配器仍未实现。
- `cockpit.py` 增加 resolver 注入、目标句式识别、控制权/航线前门禁、目标 grounding、确认时版本/坐标复核和卡片 target 字段；`PROMPT_VERSION` 升为 `cockpit-prompt-v1.3`。resolver 为 `None` 时模型输入不增加 `resolved_target`，系统提示词正文保持基线文本。
- `tools/cockpit_server.py` 增加显式 `--target-fixture` 开关；默认不启用。
- `tools/bench/cockpit_intents.py` / `cockpit_service_intents.py` 支持 service surface 注入 fixture resolver、记录 resolver 来源和夹具 SHA，并允许 `task_type` 出现在新服务评测初始状态；原有 102 条夹具离线校验仍通过。
- 新冻结集合：`fixtures/v14-named-target-intents-v1.json`、`.provenance.json`；合成候选：`fixtures/v14-named-target-candidates-v1.json`。标签在运行候选前冻结，未修改既有 `fixtures/*-v1.json`/`*-v2.json`。

## 验证证据

实际执行：

```text
bash tools/ci/offline_tests.sh
Ran 1402 tests ... OK
git diff --check
PYTHONPATH=src:. python3 tools/bench/cockpit_intents.py --surface service --fixture fixtures/intent-scenarios-v2.json --output /tmp/lingxi-service-report.json
PYTHONPATH=src:. python3 tools/bench/cockpit_intents.py --surface service --fixture fixtures/v14-named-target-intents-v1.json --target-fixture fixtures/v14-named-target-candidates-v1.json --output /tmp/lingxi-named-report.json
```

新测试 `tests/test_v14_named_target.py` 覆盖唯一目标、同名、线/面/体、找不到、解析失败、坐标 grounding、缺高度/半径、确认时版本变化、否定/条件、控制权/航线顺序、测绘提示和 resolver 关闭兼容。模型调用计数与执行器状态均为合成断言。

未验证：真实 `qwen3.5:9b` 新集合、PROMPT v1.3 后的 102 条与 31 条全量重跑、变异检查、真实静态标注服务、Ktd/卓翼/设备闭环。原因是本轮没有启用共享推理服务或真实平台；报告只能证明代码和离线边界。

## Git 与清理

- 既有 `.workbuddy-ai/`、评测目录、`lingxi_agent_src_*` 未跟踪文件未暂存、未删除。
- 本轮只写入仓库源码、测试、冻结夹具和验收文档；`/tmp/lingxi-service-report.json`、`/tmp/lingxi-named-report.json` 是临时离线报告，未入库。
- 没有启动常驻进程、容器或远端模型；没有真实设备副作用。
- 代码提交 SHA 在提交完成后回填；远端推送后用 `git ls-remote origin feature/v14-flight-named-target` 核对。
