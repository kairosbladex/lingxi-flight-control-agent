# 灵犀飞控 Agent

这是一个面向受限飞行控制场景的 Python Agent 核心实现：它把自然语言解析为结构化工具调用，在显式授权、参数校验和二阶段确认后，通过默认内存模拟器执行。

公开快照包含核心源码、离线测试、合成夹具、CI 配置和一个静态示例页面。内部验收报告、截图、部署清单、运行产物和协作规则未随公开仓库发布。

## 特性

- 受限工具白名单与结构化参数校验
- 控制权、确认、幂等键和上下文版本门禁
- 有界 Agent 循环与 fail-closed 错误处理
- 内存模拟器、回环执行器和夹具回放
- Ollama 协议适配器（默认连接 `http://127.0.0.1:11434`，可通过参数覆盖）
- 完全离线的 Python 3.11+ 单元测试

默认执行器只在内存中运行，不连接真实飞控、MQTT 或生产平台。

## 快速开始

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'
PYTHONPATH=src python3 tools/flight_agent_demo.py --fixture fixtures/cockpit_basic.json
```

需要接入本机 Ollama 时显式传入地址：

```bash
PYTHONPATH=src python3 tools/flight_agent_demo.py \
  --base-url http://127.0.0.1:11434 \
  "先向前飞10米，然后悬停30秒"
```

任何真实平台或设备接入都需要调用方另行实现鉴权、权限和回执闭环；本仓库的模拟测试不能证明真实设备执行成功。
