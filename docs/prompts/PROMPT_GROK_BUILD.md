# PROMPT_GROK_BUILD: 高保真网络仿真桩与自动化集成 Harness 搭建

## 1. 角色定位与目标
- **执行角色**：仿真打桩与集成构建专家 (Mock Server & Integration Specialist)
- **执行工具**：Grok Build
- **核心目标**：针对外部 Java 平台接口、高精电子围栏服务和 MQTT 遥测通道，搭建独立、轻量级、高保真的仿真 Mock 服务，并编写一键端到端自动化冒烟测试脚本，解耦对外部真实环境的等待。
- **基线约束**：Mock 服务基于 Python 标准库或 aiohttp，启动迅速，端口独立，支持一键启动与停止，不污染系统环境。

---

## 2. 专属责任区与文件边界
请严格在以下文件路径中进行构建：
- `tools/mocks/java_platform_mock.py` (Java 后端平台路由仿真)
- `tools/mocks/fence_service_mock.py` (37点电子围栏接口仿真)
- `tools/mocks/mqtt_telemetry_simulator.py` (MQTT 遥测与回执仿真)
- `tools/harness/e2e_smoke_runner.py` (一键端到端集成测试 Harness)

---

## 3. 具体构建任务说明

### Task 1: Java 平台路由高保真仿真桩 (java_platform_mock.py)
- **仿真路由清单**：
  1. `POST /screen/uav/route/control` (航线控制：暂停 120/恢复 122/返航 105/急停 113)；
  2. `POST /screen/uav/operation/control` (微操移动 201/转向 202/镜头云台)；
  3. `POST /screen/uav/camera/control` (拍照 100/录像 101/变焦)；
  4. `GET /screen/uav/status/snapshot` (实时快照接口)。
- **动态故障注入功能**：
  - 支持通过环境变量或请求 Header 注入异常场景：
    - 正常返回 200；
    - 注入 `route_status: null`（模拟后端未上报航线状态）；
    - 注入 HTTP 504 Gateway Timeout（模拟平台挂起）；
    - 注入 `control_owned: false`（模拟无控制权）。

### Task 2: 37 点高精电子围栏仿真服务 (fence_service_mock.py)
- **仿真路由**：
  - `POST /electronicFence/path-route/with-clean`
- **响应逻辑**：
  1. 校验请求体中是否包含完整的 37 个采样点经纬度与高度数据；
  2. 内置一个虚拟禁飞区几何体（如多边形区域）：
     - 若 37 个采样点全部位于禁飞区外，返回 `status: "clear"`；
     - 若任一点穿透禁飞区，返回 `status: "blocked"`，并指明碰撞点索引；
     - 支持模拟网络超时（响应延迟 > 3000ms），验证飞控端的降级处理逻辑。

### Task 3: 本地 MQTT 遥测发生器与回执捕获器 (mqtt_telemetry_simulator.py)
- **仿真功能**：
  1. 基于轻量级 MQTT Broker 或本地 TCP 模拟：
     - 以 1Hz 频率定时推送无人机遥测报文：包含当前经度、纬度、海拔高、机头朝向航向角、电池剩余百分比、当前执行航点 ID；
  2. 模拟电量递减：当电量降至 19% 时自动触发低电量报警事件；
  3. 监听控制指令下发 Topic，并在 50ms 内模拟设备端下发 `submitted` → `executing` → `completed` 状态回执报文。

### Task 4: 一键端到端冒烟测试 Harness (e2e_smoke_runner.py)
- **功能要求**：
  1. 自动化流水线脚本：一键在后台拉起上述 3 个 Mock 服务（分配独立空闲端口，如 18080, 18081, 18883）；
  2. 驱动飞控 Agent 发起典型测试指令（地面下发、空中微操、载荷调整、测绘中断、低电量返航）；
  3. 断言全链路 HTTP 响应码为 200，并抓取日志验证状态机转换无误；
  4. 无论测试成功或失败，脚本退出前**必须自动清理并关闭所有后台 Mock 进程**，释放系统端口。

---

## 4. 验收与交付标准
1. **服务独立性**：Mock 服务可单文件脱机运行，无复杂外部依赖。
2. **冒烟通过率**：执行 `python3 tools/harness/e2e_smoke_runner.py`，全套集成测试 100% 通过。
3. **交付回传**：输出脚本文件与测试运行日志，汇报给总控大脑。
