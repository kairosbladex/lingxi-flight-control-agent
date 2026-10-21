# Java能力工具化交付（2026-10-21）

## 交付范围

Python功能版本 `18c9c3649ba8e98b1eebeb2d39023a436bcbf2c0`，UI版本 `9960ba142293c7ee4af1681c22ed335f70ba07a2`。新增24个具名、强类型Java工具，覆盖相机画质/存储/夜景、红外测温/调色、屏幕对焦/框选变焦/指向、云台模式、机身灯光、停止指点飞行/环绕。模型只能选择固定工具及校验参数；资源由服务端绑定，复用原确认、幂等和审计链。

模拟器仍为59项；平台工具定义为83项，适配名称54项（含读状态），不代表每项在任意设备/上下文可执行。起飞、环绕启动等特殊适配以及维护、格式化、喊话/文件播放未自动开放。Java仓库未修改，核验HEAD为 `6721f11c35b3510ab66722e4f05481bac82e00a5`，工作区干净。

精确云台下调10度仍拒绝为 `GIMBAL_EXACT_ANGLE_UNAVAILABLE`：原Java相对云台控制发送角速度而非目标角度；绝对401/402模式无对应执行分支，不能把R.ok当执行完成。前进10米使用原Java mode=2计算目标点，真实位移尚待设备验收。

## 验证

- `bash tools/ci/offline_tests.sh`：737/737；日志 `/tmp/java-tools-final-tests.log`。
- UI `npm test`：150/150；`npm run build`通过。
- `PYTHONPATH=src python3 tools/bench/dji_browser_contract.py --java-tools --base-url http://192.168.130.238:11434`：真实模型+合成Java 6/6，确认前0POST、真实设备请求0，结果均保留 `device_effect=not_verified`。
- 10086真实页面只读检查：没有执行中任务，显示“当前没有正在执行的飞行任务”，输入和发送禁用；未取权、未确认、未发真机指令。
- 桌面文档《大疆飞控能力与Agent工具输入输出说明_20261021.docx》15页，逐页渲染检查通过，列明输入→模型工具参数→Java请求→动作及未支持边界。

## 独立10086部署和回滚

仅更新个人10086及其回环18974网关，不改共享8088/Java。系统服务 `lingxi-flight-browser-10086.service` 工作目录 `/home/zhuoyi/ktd/flight-browser-20261021/source-18c9c36`；静态入口 `/home/zhuoyi/ktd/dsm-frontend-10086/site` 指向 `releases/java-tools-18c9c36`。服务限制384MiB、禁止swap、CPU50%、Tasks32，未设置开机启用，保留用于用户下午验收。

停止命令：`ssh test-front 'systemctl stop lingxi-flight-browser-10086.service'`。回滚时停止该系统服务，恢复 `/home/zhuoyi/ktd/flight-browser-20261021/service-before-18c9c36` 至原unit，daemon-reload后启动；静态site恢复 `releases/python-flight-visible`，保留命令审计。旧user服务保持停止。

临时合成HTTP服务已退出，浏览器自有检查页已关闭；文档渲染与部署暂存 `/tmp/dji-capabilities.sQeuhr` 保留用于证据及回滚。定时监控继续暂停。健康检查及合成测试不是实际设备动作验收。
