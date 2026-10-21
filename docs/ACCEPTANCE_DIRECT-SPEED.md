# 拍照/移动响应优化及10086发布（2026-10-21）

## 根因与范围

通过只读docker inspect、限定时间日志、模型manifest和nvidia-smi核实：11434共享Ollama容器绑定GPU3（Tesla T4，15360MiB），qwen3.5:9b与qwen3-vl:8b-instruct在原测试时间窗交替启动runner。日志明确报告显存不足并驱逐模型；blob SHA与模型manifest对应。前轮12.8至17.8秒load_duration包含这一反复加载链，而非仅网络慢。已有keep_alive=30m不能阻止显存不足驱逐。

没有重启或修改共享Ollama、GPU配置、其他容器或端口。未声称解决共享推理调度；复杂句仍走共享模型，仍可能慢或超时。

## 实现

- 后端提交 `e25a15feca9fab7e4c8342057d7f94f0c2586126` 已推送并核验。
- 新增默认关闭的 `--enable-direct-commands`，仅浏览器网关显式启用；只完整匹配单句拍照与前后左右定距移动，距离仍为0<d<=20米，保守中文数词/数字，小数和半米可用。
- 不匹配复合、条件、否定、引用、疑问、速度约束、近似/区间数值、缺参、垂直移动或转向；沿用原策略/模型，不截取子串，不猜数字，不扩展59动作契约。
- 仅替换建议生成，仍通过BoundedAgentLoop、ToolRegistry、原参数/意图一致性校验、平台权限/遥测/任务核验、预览确认、幂等和审计。未引入另一执行器或状态机。
- 明确新命令清除旧澄清意图；审计记录interpretation_source=exact-command-v1。确认才可能调用原执行器；不自动取权、不自动确认、不修改45秒模型/65秒单轮上限。

## 隔离验证

原分母6条与候选/最终代码相同：拍照、前2米、后2米、拍照、右2米、左2米。

| 输入 | 原模型路径秒 | 最终隔离HTTP毫秒 |
| --- | ---: | ---: |
| 拍照1 | 28.845 | 45 |
| 向前飞2米 | 28.041 | 54 |
| 向后飞2米 | 34.006 | 7 |
| 拍照2 | 34.439 | 6 |
| 向右平移2米 | 30.628 | 6 |
| 向左平移2米 | 28.216 | 6 |

- 最终源代码远端隔离HTTP6/6正确确认卡，模型0调用、控制写尝试0、确认0、真实设备请求0，模型地址故意不可用仍通过。候选版本同一组6/6，6至97毫秒。
- 最终耗时不含真实Java遥测/鉴权的延迟，不能作为真实平台页面延迟承诺。未在真实登录态提交拍照或移动预览。
- 真实模型fallback两句：帮我拍摄一张照片、机头方向向前平移3米，2/2正确确认卡，29.907/31.563秒，说明未匹配表达仍保持模型路径。
- `bash tools/ci/offline_tests.sh`：756/756，包含新增9项测试、28组有效数值/方向组合、44组非法数值、23条非精确输入，以及缺控制权、旧上下文、合成确认幂等、任务变化拒绝。socket守护全量通过。
- 合成单元测试允许内存假Java确认一次以验证幂等；三个HTTP耗时探针均未调用confirm，不能混记成真机动作。
- 编译、diff检查通过。证据位于 [verification.json](evidence/direct-speed-20261021/verification.json)、[final.json](evidence/direct-speed-20261021/final.json)、[candidate.json](evidence/direct-speed-20261021/candidate.json)、[fallback.json](evidence/direct-speed-20261021/fallback.json)。

## 发布、清理与回滚

- 经用户授权，仅更新独立10086 Python服务，工作目录 `/home/zhuoyi/ktd/flight-browser-20261021/source-e25a15f`，运行进程命令行核实启用direct开关，PID78674；服务源码哈希与本机提交一致。
- 静态前端保持state-query-309bb03版本及原哈希；8088/Java/共享模型未修改。入口与健康接口可达，无登录的状态查询仍401；这些不是设备动作验收。
- 保留回环18974验收服务：384MiB、Swap0、CPU50%、Tasks32，实测约15.6MiB。停止：`ssh test-front 'systemctl stop lingxi-flight-browser-10086.service'`。
- 回滚：停止此服务，恢复 `/home/zhuoyi/ktd/flight-browser-20261021/service-before-direct-speed-e25a15f` 到 `/etc/systemd/system/lingxi-flight-browser-10086.service`，daemon-reload后启动。旧source-309bb03、账本和审计保留，静态文件无需回滚。
- 三个临时systemd实例均inactive且自动回收（192MiB/Swap0/CPU50%/Tasks32，90至420秒上限），回环HTTP和临时账本关闭清理；候选目录、上传归档、本机打包目录与临时原始诊断输出已清理。
- 本次复用原语义开发分支，无新工作树，无真实飞控指令。只保留脱敏指标和技术原因，不保存业务/模型原文日志。
