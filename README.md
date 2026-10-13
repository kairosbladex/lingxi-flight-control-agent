# 灵犀飞行执行 Agent

独立维护飞行执行阶段的自然语言控制能力。该项目只处理已经绑定到执行上下文的飞行控制意图，不负责巡检地点解析、航线规划、算法选择或任务下发。

## 当前范围

- 将受限的控制文本解析为结构化意图；
- 校验距离、方向和单动作约束；
- 在内存模拟器中执行相对移动；
- 使用 `execution_id`、`context_version` 和 `idempotency_key` 拒绝过期或重复命令。

当前版本不连接 MQTT、真实飞控或生产任务平台，也不保存业务数据。

## 运行

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
PYTHONPATH=src python -m lingxi_flight_control.simulator
```

## 与 lingxi-agent 的边界

`lingxi-agent` 负责任务规划、任务草稿、确认和任务下发；本项目只接收结构化的执行上下文和控制意图。两边通过后续版本化协议对接，厂商指令和设备权限留在平台适配层。
