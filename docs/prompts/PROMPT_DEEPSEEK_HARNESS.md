# PROMPT_DEEPSEEK_HARNESS: 几何解算推演、并发原子安全与对抗测试

## 1. 角色定位与目标
- **执行角色**：算法推演与安全攻防专家 (Algorithms & Safety Assurance Specialist)
- **执行工具**：DeepSeek Harness
- **核心目标**：运用高阶链式思考与数学几何推演能力，深度验证 V1.4 规范中的 37 点电子围栏空间几何插值算法、测绘中断六步原子事务的多进程死锁防御，并生成全套边界对抗测试集。
- **基线约束**：纯数学与算法推演，零外部网络依赖，测试完全可离线执行。

---

## 2. 专属责任区与文件边界
请严格在以下文件路径中进行分析与测试集编写：
- `src/lingxi_flight_control/path_geometry.py`（算法审查与加固）
- `src/lingxi_flight_control/mapping_interruption.py`（并发锁分析与加固）
- `tests/test_adversarial_geometry.py`（新增对抗几何单测）
- `tests/test_adversarial_mapping_race.py`（新增并发竞争对抗单测）
- `tests/test_adversarial_inputs.py`（新增极限输入 Fuzzing 单测）

---

## 3. 具体攻坚任务说明

### Task 1: 37 点高精电子围栏空间几何采样算法推演
- **背景**：根据 V1.4 2.2.2 规范，飞行微操下发前必须调用电子围栏校验。本仓需生成包含 37 个离散采样点的空间折线坐标。
- **推演要求**：
  1. 审查 `src/lingxi_flight_control/path_geometry.py` 中的轨迹插值算法，推演用户指令（前后左右上下微操、转向、指点航线、半径 R 环绕轨迹）在球坐标系与高斯投影下的坐标转换精度；
  2. 验证高度基准换算（WGS84 椭球高 vs 相对起飞点高度）：当无人机低空飞行遇到限高柱时，算法是否能精确识别采样点的空间穿越；
  3. 在 `tests/test_adversarial_geometry.py` 中编写 15+ 项边界几何用例（包含掠过围栏边界 1 厘米的极限贴边飞行、逆时针超大半径环绕等）。

### Task 2: 测绘中断六步原子事务并发竞争与死锁证明
- **背景**：依据 V1.4 4.1.2 规范，测绘中断必须按严格顺序执行六步原子事务：关拼图 → 提交作废 → 关原图实时推流 → 清空主线断点 → 置灰航线恢复 → 使旧确认卡失效。
- **推演要求**：
  1. 深度分析 `src/lingxi_flight_control/mapping_interruption.py` 中基于 SQLite reservation 与内存锁的事务设计；
  2. 模拟极限多进程并发场景：
     - 用户几乎在同一毫秒下达微操中断指令；
     - 外部平台网关发生超时重试重入；
  3. 在 `tests/test_adversarial_mapping_race.py` 中编写对抗性多进程并发测试，断言系统在任何异常挂起或重入下，绝不产生死锁、绝不跳步执行、绝不残留脏恢复标记。

### Task 3: 极限边界输入（Corner-Cases）对抗测试库生成
- **背景**：大模型意图提取可能传入极其异常的极端参数。
- **生成要求**：
  1. 在 `tests/test_adversarial_inputs.py` 中编写针对参数极值智能换算（V1.4 2.2.5）的对抗测试：
     - 变焦倍数 10000 倍、负变焦、非数（NaN/Inf）；
     - 相对位移 1000 米超限飞行；
     - 混合经纬度、畸形凹多边形、自相交线段；
  2. 证明确定性层能 100% 拦截或智能取极值并给出固定话术，绝不发生系统抛未捕获异常。

---

## 4. 验收与交付标准
1. **数学与逻辑严密性**：推演结论有清晰的边界证明，无死锁隐患。
2. **测试集有效性**：新增测试文件全部纳入 `unittest discover` 自动化回归套件，执行命令全部 PASS。
3. **交付成果**：输出 `tests/test_adversarial_*.py` 代码，并将分析结论记录回传给总控大脑。
