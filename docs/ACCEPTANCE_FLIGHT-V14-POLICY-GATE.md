---
status: in_progress
as_of: 2026-10-30
branch: bugfix/v14-control-acquire-description
baseline: bugfix/v14-named-target-deterministic @ 5153eae
prompt_version: cockpit-prompt-v1.6
evidence_level: offline + bounded live_model（qwen3.5:9b），simulator_preview_only
---

# FLIGHT-V14-POLICY-GATE：control_acquire 描述修正

T2 以 T1b 已推送 tip `5153eae` 为基线，只移除 `control_acquire` 描述中承担前置条件的句子；控制权校验仍由确定性门禁、最终策略层和执行器负责。最终候选为：

> 申请或获取本无人机控制权；仅在用户明确要求申请或获取控制权时使用

模型可见描述变化使 `PROMPT_VERSION` 从 `cockpit-prompt-v1.5` 升为 `cockpit-prompt-v1.6`。

## 候选 A/B

| 候选 | 描述 | `v14_clamp_01` 两轮 | 备注 |
| --- | --- | --- | --- |
| A（采用） | 申请或获取本无人机控制权；仅在用户明确要求申请或获取控制权时使用 | 2/2 `camera_zoom(100)` | 语义最窄，明确“用户明确要求”边界 |
| B | 处理本无人机控制权申请或获取请求；仅在用户明确要求控制权操作时使用 | 2/2 `camera_zoom(100)` | 语义中性，但“控制权操作”范围更宽 |
| 旧描述 | 获取或申请无人机控制权；执行任何飞行动作前必须先获取 | — | 与系统提示词冲突，已移除 |

A/B 运行均为温度 0、只预览不确认；`control_01` 与 `multiple_03` 的两轮候选 A 仍按既有冻结标签暴露 `control_acquire` 的自由参数差异，未因描述改动新增危险提议。

## 验证

已完成：

```text
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_v14_policy_gate.py' -q  # 24 项，OK
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_product_v13.py' -q       # 30 项，OK
```

真实模型复跑结果和完整离线门禁在本节后续补录。验收标准：v14 补充集保持 31/31，`v14_clamp_01` 两轮均为 `camera_zoom(100)`，v2 冻结集正确数不下降，危险提议保持 0。

真实平台、MQTT、设备回执和设备效果不在本任务验证范围。
