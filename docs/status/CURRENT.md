---
status: partial
as_of: 2026-10-30
code_sha: a7ddd3e
scope: lingxi-flight-control-agent Python 飞控 Agent 仓库
evidence_level: offline, mock
unknowns: 真实平台快照、MQTT、设备回执与设备效果
next_action: 先登记外部联调前置条件，再单独申请并验证真实平台只读链路
---

# 当前状态快照

本页回答“现在是什么状态”。历史过程、单次验收和对话线索分别见 [CONTINUE.md](../CONTINUE.md)、`ACCEPTANCE_*.md`、[对话复盘](../reviews/CONVERSATION_REVIEW_20261028.md)。

## 基线

- 分支：`bugfix/v14-named-target-deterministic`。
- HEAD：`a66bec8`（T1 验收记录）；实现提交为 `a7ddd3e`。另有未跟踪的 `.workbuddy-ai/`、既有评测目录、源码压缩包和文本快照，均未纳入本次提交。
- T1 已将唯一命名目标的明确指点/环绕参数改为确定性填充；默认 resolver 开关仍关闭。
- 默认执行器仍是模拟器；真实平台执行器、生产 MQTT、设备写入和共享环境发布保持关闭。

## 已有证据能支持的结论

- 本仓的离线测试脚本是 `bash tools/ci/offline_tests.sh`；仓库规则要求离线 discover 不发起 socket 连接。
- 最近的 V1.4 审计记录了 Agent 离线 1339 项通过、后端编译和契约测试通过、前端 177/177 通过；该记录的作用域是代码/隔离契约，不是设备验收，见 [V1.4 多端完成审计](../evidence/v14-completion-audit-20261027.md)。
- 当前未跟踪评测产物记录了 58 个样本、93 个动作和 62 个参数的合成离线契约匹配；其 `code_sha` 为 `f359b27`，工作树为 dirty，且明确未调用 Ollama、未连接平台或设备，因此不能当作当前 HEAD 的发布门禁。
- 2026-10-29 真实 `qwen3.5:9b` 服务链路评测：v2 冻结集 89/102 正确，V1.4 词典补充集原始 22/31，危险动作提议 0；航线中/无控制权微操会模型超时、拿不到规范拦截话术，见 [V1.4 真实模型意图评测](../evidence/v14-live-intent-eval-20261029.md)。
- 2026-10-29 FLIGHT-V14-POLICY-GATE（分支 `bugfix/v14-deterministic-policy-gate`）：前门禁保持不变；后续补齐模型误提 `control_acquire` 的无控制权兜底，并核验 Ktd/卓翼平台快照字段。离线全量 1389 项通过；真实补充集重跑 31/31、危险提议 0，三条真实探针均返回 `rejected/CONTROL_NOT_OWNED` 规范话术，见 [验收记录](../ACCEPTANCE_FLIGHT-V14-POLICY-GATE.md)。
- 2026-10-29 V1.4 产品裁决落地（分支 `bugfix/v14-policy-alignment`）：非测绘航线中坐标飞行/环绕按 P1 拦截，测绘场景保留 `MAPPING_MESSAGE` 确认卡，点飞行围栏话术和临时任务 120 秒按裁决更新；离线 1395 项通过；平台缺测绘原子作废契约时预览即拒；2026-10-30 真实 qwen3.5:9b 补充集 31/31、4 条航线中探针均符合裁决，危险提议 0；真实平台/设备未验证，见 [验收记录](../ACCEPTANCE_FLIGHT-V14-POLICY-ALIGNMENT.md)。
- 2026-10-29 飞行中命名目标（分支 `feature/v14-flight-named-target`）：可选静态标注 resolver、唯一/歧义/线面体拒绝、坐标 grounding 与确认时版本复核已接入；冻结集合与 service 评测入口已建立，离线 1413 项通过；2026-10-30 真实 qwen3.5:9b：v2 冻结集 92/102、补充集 31/31、命名目标 v2 集 14/19（0 判错、5 超时，根因为标注高度入模型、control_acquire 描述矛盾与全量工具下发），危险提议 0；resolver 默认关闭，三点处理前不建议开启；真实地图/设备未验证，见 [验收记录](../ACCEPTANCE_FLIGHT-V14-NAMED-TARGET.md)。
- 2026-10-30 T1（分支 `bugfix/v14-named-target-deterministic`）：离线 1415 项通过；命名目标 v1/v2 真实评测分别 13/13、19/19，模型调用降至 2、5，确定性样本毫秒级；补充集 31/31。102 条冻结集同预算复跑为 92/8/2，另一次运行 93/9/0，危险提议均为 0；模型服务波动仍存在。标注高度移出模型输入，确认卡仅展示参考高度；真实地图/设备未验证，见 [验收记录](../ACCEPTANCE_FLIGHT-V14-NAMED-TARGET.md)。
- 真实平台联调前置条件已整理为 Java 快照、控制权租约、逐动作权限、`route_status`、回执、围栏、设备和书面授权等外部条件，见 [平台联调前置条件](../evidence/platform-integration-prereq-20261027.md)。

## 未验证与阻塞

以下事项仍不能从现有代码、mock、HTTP 受理或模拟器回执推导出来：

- Java 权威快照的 `route_status`、控制权租约、逐动作能力和版本绑定；
- 真实 MQTT 发布、厂商回执与 `bid/tid` 级关联；
- 算法粒度启停、测绘停止事务和真实任务事件来源；
- 电子围栏/三维避障服务的真实返回；
- 实机、载荷、固件、空域批件和设备效果。

因此当前结论是 **代码和离线契约部分完成，真实平台与设备闭环 blocked/unknown**。不得把“已完成代码部分”改写成“已实飞完成”。

## 下一步顺序

1. 给每个外部前置条件补负责人、时间窗和可读接入地址；没有这些信息就保持只读和 mock。
2. 建立 evidence registry，把 `code_sha + command + artifact + evidence_level + timestamp` 绑定到每个结论。
3. 将 `CONTINUE.md` 压缩成当前快照，把历史段落迁到 `docs/history/`；迁移前先保留旧链接。
4. 真实联调只从只读快照开始，逐项验证租约、`route_status`、回执和设备效果；每一层分别记录，不能跨层升级。
