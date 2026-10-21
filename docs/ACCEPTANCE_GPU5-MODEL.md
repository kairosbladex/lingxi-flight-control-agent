# 飞控GPU5专用模型与10086切换（2026-10-21）

## 归属与实例

11437容器`lingxi-t4-ollama-9b`创建于2026-10-08，标签purpose=lingxi-rewrite-5008-9b，近期仍有调用，归任务下发Agent重写用途；不能复用为独占飞控。11437和11434未停止、重启或修改。

新实例`flight-control-ollama-gpu5`，`http://192.168.130.238:11438`，固定GPU5 UUID `GPU-867d3056-9e2e-2b5e-90e2-b7f0d866aa74`，Tesla T4 15GB。同卡还有其他业务进程，专用的是模型实例，不是硬件独占。验证时显存总占用约10.7GiB，余量约4GB，不能保证其他服务未来增长不会影响它。

仅qwen3.5:9b独立模型目录；manifest复制、blob硬链接后只读挂载，不修改共享模型目录。镜像固定为sha256:013cbb5ffadc4b6672bf6c86f81a63c4ddf83c1c25168cccee877b5ae7942895（Ollama0.21.0），模型digest保持6488c96fa5faab64bb65cbd30d4289e20e6130ef535a93ef9a49f42eda893ea7。

限制16GiB内存、同值memory-swap（禁swap）、4CPU、256任务，单模型、并发1、排队4、32768上下文；永久驻留、关闭cloud，模型目录只读，独立CUDA缓存约62MiB。没有下载模型或升级共享运行时。

## 启动与失败保留

- 初始裸容器第一次冷请求超过45秒，探针在后续请求期间被停止，未尝试项不算成功。见initial-cold.json。
- 启动预热与业务请求分开，预热最长150秒，不扩大业务45秒/单轮65秒预算。全新容器CUDA初始化实测约77秒；持久化自有CUDA缓存后重建加预热约6秒。
- -1曾以字符串发送被Ollama400拒绝，六条全部失败；已改成数值-1并补测试，保留rejected-string-keepalive.json，不掩盖失败。
- 主机无免密sudo，使用已有user systemd和已开启的Linger=yes，不修改全局linger。服务`flight-control-model-gpu5.service`启用用户启动，ExecStartPost预热并核验GPU驻留后才变active；失败有限重启（10秒间隔/600秒最多3次），停止自动删除自有容器。
- 自有unit通过systemd-analyze --user verify；读取到其他已有unit的文件权限警告，未修改它们。

## 最终验证

验证入口复用`tools/bench/action_latency_smoke.py`；所有Java接口为合成，只允许GET，探针不调用confirm。首次连续测试关闭快捷解析，强制走模型，83工具、32768上下文、45秒、不重试，与线上一致。

- 最终数值驻留配置6/6：拍照12.411秒、前移2米10.189秒、后移2米9.681秒、拍照11.407秒、右移2米8.727秒、左移2米8.620秒。每条模型1次，正确确认卡，幂等重放不调用模型。
- 空闲60秒后，两个客户端同时各发2条模型fallback输入（帮我拍摄一张照片、机头方向向前平移3米）：4/4正确，8.269/16.174/16.267/16.281秒，0超时。
- 07:18:44至07:23:26 UTC读回相同runner PID2038944，常驻GPU约9.32GiB、上下文32768、长期有效；最终容器日志仅一次runner启动，0驱逐、0OOM。
- 预热后首轮6/6数据另存warm-first.json，不与最终10条混淆。失败、初步通过与最终通过分文件保存。
- 离线唯一入口`bash tools/ci/offline_tests.sh`：759/759；编译、diff检查通过。新增驻留线协议/缺驻留拒绝启动测试。
- 本次真实飞控指令0、控制写尝试0、确认0；仅合成数据进入模型。不宣称真机飞行或业务效果验收。

证据：[verification.json](evidence/gpu5-model-20261021/verification.json)、[final-sequential.json](evidence/gpu5-model-20261021/final-sequential.json)、[idle-concurrency.json](evidence/gpu5-model-20261021/idle-concurrency.json)、[residency-final.json](evidence/gpu5-model-20261021/residency-final.json)。这是分钟级驻留及有限并发验证，不是长期SLA或任意并发保证。

## 切换与回滚

- 用户授权后，10086独立Python切换至source-ef5ec1d、模型11438，启用`--model-keep-alive=-1`，保留direct命令开关，PID79256。原45秒业务模型时限不变。代码ef5ec1db0b06e805c5110f21457e6e2f15db592e已推送。
- 运行进程参数核验新URL/驻留/快捷开关，页面健康可达，无登录状态查询401。静态前端哈希不变，8088/共享Java未修改。未用真实登录态提交拍照/移动预览。
- 回滚：停止`lingxi-flight-browser-10086.service`，把`/home/zhuoyi/ktd/flight-browser-20261021/service-before-gpu5-ef5ec1d`恢复到`/etc/systemd/system/lingxi-flight-browser-10086.service`，daemon-reload并启动，恢复旧source-e25a15f及11434；不删账本。
- 新模型停止：`ssh ai-lingxi 'systemctl --user disable --now flight-control-model-gpu5.service'`。确认已回滚10086再停；停止后容器自动删除、GPU释放，模型目录与CUDA缓存保留供恢复。服务文件位于`~/.config/systemd/user/flight-control-model-gpu5.service`，自有资源目录`/data/workspace/flight-control-model-20261021/`。
- 保留10086回环18974（384MiB/Swap0/CPU50%，实测约15.6MiB）及11438专用模型，供用户验收。临时验证systemd服务均inactive，回环端口关闭，合成账本、候选7c48c7d目录及上传归档已清理。
- 无新工作树、无其他服务重启；只创建飞控自有模型服务并切换10086。默认分支提交包含部署脚本、测试、证据。
