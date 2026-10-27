# PROMPT_CODEX_CLI: 后端核心适配器与接口网关实现

## 1. 角色定位与目标
- **执行角色**：后端核心与接口契约工程师 (Core Backend & Contract Specialist)
- **执行工具**：Codex CLI
- **核心目标**：攻坚 V1.4 规范中未对接的外部 Java 平台快照容错、静态标注库真实数据读取网关，以及虚拟 AI 算法生命周期网关，确保代码实现具备工业级健壮性并新增完整单测。
- **基线约束**：绝不破坏现有 `bash tools/ci/offline_tests.sh`（1,115 项自动化单测 100% PASS）。

---

## 2. 专属责任区与文件边界
请严格在以下文件路径中进行开发，严禁修改与本任务无关的代码：
- `src/lingxi_flight_control/adapters/ktd_platform_adapter.py`（或增强 `src/lingxi_flight_control/ktd_platform.py` / `ktd_snapshot.py`）
- `src/lingxi_flight_control/adapters/static_annotation_gateway.py`
- `src/lingxi_flight_control/adapters/virtual_algorithm_gateway.py`
- `tests/test_adapters_platform.py`
- `tests/test_adapters_annotation.py`

---

## 3. 具体开发任务说明

### Task 1: 平台快照 route_status 健壮容错适配
- **背景**：根据 V1.4 2.2.3 规范，在自动航线中必须拦截微操指令。但目前部分后端环境返回的快照中 `route_status` 偶发为 `null`。
- **开发要求**：
  1. 增强快照解析逻辑：当 `route_status` 为 `null` 时，非只读微操路径严格执行 fail-closed，抛出精准的 typed 错误码 `ROUTE_STATE_SOURCE_UNAVAILABLE`，并保留平台能力缺失的诊断上下文；
  2. 对于载荷只读查询指令（如读取相机变焦、云台姿态），允许基于降级模式放行；
  3. 提供可插拔的快照解析策略，支持对接 Grok Build 搭建的 Mock 接口或真实 Java 生产接口。

### Task 2: 真实静态标注库数据读取网关 (StaticAnnotationGateway)
- **背景**：依据 V1.4 2.1 与 3.2.2 规范，废弃旧版后台巡检区域库，所有巡检目的地统一使用地图静态标注库。
- **开发要求**：
  1. 实现 `StaticAnnotationGateway`，支持从配置接口读取静态标注列表（包含点、线、面坐标几何、`target_id`、版本号、`configured_algorithms` 算法绑定列表）；
  2. 严格落实几何校验门禁：
     - **点目标**：多点按用户指定的 `order` 严格保留顺序，绝不静默按地理距离重排；
     - **线目标**：仅支持单线，强制端点起飞，多线输入立即返回 `MULTI_LINE_UNSUPPORTED`；
     - **面目标**：仅支持单面全覆盖，多面输入立即返回 `MULTI_POLYGON_UNSUPPORTED`；
     - **体目标**：输入立体目标严格拦截，返回 `BODY_ANNOTATION_AI_UNSUPPORTED` 或 `UNSUPPORTED_TARGET_GEOMETRY`；
     - **混合目标**：点+线、线+面等混合输入严格拦截，返回 `MIXED_TARGET_UNSUPPORTED`。

### Task 3: 虚拟 AI 算法生命周期网关 (VirtualAlgorithmGateway)
- **背景**：依据 V1.4 2.1.2 规范，算法绑定强制默认“实时+航点检测”（到达开启、离开关闭、去重防误关）。但外部平台当前仅暴露任务级 `videoDetect` 接口。
- **开发要求**：
  1. 实现 `VirtualAlgorithmGateway`：当平台缺少细粒度子算法启停时，在内存与推流元数据层维护 `active_algorithms_set`；
  2. 落实差集停用逻辑：离开某标注区域时，仅关闭该标注独有的算法；若某算法在全局配置中已被勾选，则保留其开启状态（绝不误关）。

---

## 4. 验收与交付标准
1. **测试基线**：执行 `bash tools/ci/offline_tests.sh`，原有 1,115 项用例必须全部 PASS。
2. **新增测试**：在 `tests/test_adapters_*.py` 中补充针对上述 3 项适配器的独立单测，新增用例不少于 30 项。
3. **代码规范**：所有新增函数和类必须包含完整类型注解（Type Hints）与详尽 Docstrings，通过 `python3 -m compileall -q src tests` 与 `git diff --check`。
4. **交付回传**：在完成本地提交后，将修改的文件清单与单测执行结果汇报给总控大脑。
