# 灵犀飞行执行 Agent

独立维护已绑定执行上下文内的受限控制文本解析和内存执行闭环；同时保留任务下发模型作为兼容层。

## 当前范围

- 将单个中文方向和距离解析为 `RelativeMove`；
- 在已确认的 `ExecutionContext` 中执行相对移动；
- 用 `idempotency_key` 拒绝重复命令，用上下文版本拒绝过期命令；
- MemoryFlightSimulator 工具动态绑定当前 `ExecutionContext`，检查点恢复时拒绝执行批次、无人机或版本已变化的旧上下文；
- 通过注入的意图解释器生成结构化 `ToolCall`，维持旧任务下发调用方兼容。
- `full_control.py` 提供显式的 `FlightControlAction`、`RuntimeBinding`、`FlightControlState` 和
  `MemoryFlightControlSimulator`，覆盖驾驶舱控制权、飞行、航线、相机/云台、红外、载荷和状态读取。
  这些动作只在内存中预览、确认和执行，回执固定标记 `execution_mode=simulator`。

当前版本不连接 MQTT、真实飞控或生产任务平台；执行器和任务工具均由调用方注入。

## 运行

```bash
PYTHONPATH=src python -m unittest discover -s tests -p 'test_*.py' -v
```

## 与 lingxi-agent 的边界

`lingxi-agent` 负责业务入口和任务数据；本项目负责意图到工具调用的最小流程。工具实现、权限、幂等和平台适配由调用方注入。
