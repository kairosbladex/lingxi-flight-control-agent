# 拍照与移动耗时隔离验证（2026-10-21）

## 结论

14:49至14:52 Asia/Singapore，线上相同后端309bb03、相同qwen3.5:9b配置，在独立回环HTTP网关+合成Java上验证6条输入：6/6正确生成确认卡，无超时，无确认、无控制写尝试、无真实设备请求。不能据此保证共享模型后续不超时，也不是设备执行验收。

| 输入 | 耗时秒 | 模型调用 | 结果 |
| --- | ---: | ---: | --- |
| 拍一张照片（首次） | 28.845 | 1 | camera_photo确认卡 |
| 向前飞2米 | 28.041 | 1 | move_forward，distance_m=2 |
| 向后飞2米 | 34.006 | 1 | move_backward，distance_m=2 |
| 拍一张照片（第二次） | 34.439 | 1 | camera_photo确认卡 |
| 向右平移2米 | 30.628 | 1 | move_right，distance_m=2 |
| 向左平移2米 | 28.216 | 1 | move_left，distance_m=2 |

## 方法与证据

- 复用BrowserGateway、CockpitService、原OldJava夹具和真实OllamaDecider；原平台HTTP全部由内存假接口处理，假Java额外拒绝任何非GET请求。探针从未调用confirm。
- 使用同一合成会话顺序查询，包含前面预览历史；每条请求幂等重放均不再调用模型。模型45秒超时、单轮65秒/3步、32768上下文、2048输出上限、不重试，与线上启动配置一致。
- 测试进程与账本隔离，模型仍是现有共享Ollama服务，不宣称独占GPU或无其他用户负载。只发送合成身份/状态/输入，不读取真实登录凭据或任务。
- 每次83工具、输入7148至7373 tokens；服务报告load_duration为12.84至17.80秒，prompt_eval_duration为9.95至10.81秒，eval_duration为1.86至8.83秒。加载阶段耗时偏高原因未核验；不能由这些数字断言显存换入或排队。
- 原始指标（不含模型原文/思考内容）见 [live.json](evidence/action-latency-20261021/live.json)，模型摘要、配置与源码哈希见 [provenance.json](evidence/action-latency-20261021/provenance.json)。失败/缺失没有剔除，本轮分母6。

## 命令与回归

- 本机探针自检：`PYTHONPATH=src python3 tools/bench/action_latency_smoke.py --offline-scripted --base-url http://model.invalid`，6/6。
- 远端在已部署source-309bb03目录，以`PYTHONPATH=src:tests`运行同一探针，`--base-url http://192.168.130.238:11434`；独立临时systemd服务限192MiB、Swap0、CPU50%、Tasks32、420秒，总调用预算12、探针预算360秒。
- 增加拍照/2米移动的模型请求超时与迟到返回回归，共4组合；均无确认卡、无状态变化、不自动重试，幂等重放返回原错误。
- `bash tools/ci/offline_tests.sh`：747/747；定向模型失败测试16/16；Python编译和`git diff --check`通过。

## 清理与范围

- 远端临时服务agent-action-latency-20261021已退出回收，回环39751无监听，上传探针目录和临时账本已删除；本机合成HTTP实例在finally关闭。
- 10086保持原PID78080、source-309bb03与原静态资源；没有发布、重启、改模型配置或延长超时，没有修改Java/前端/8088。
- 本次仅新增验证脚本、回归测试及证据文档，无产品逻辑改动、无新工作树；不需要线上回滚。
