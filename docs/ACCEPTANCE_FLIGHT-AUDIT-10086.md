# 10086更新与飞控审计

2026-10-21 11:29 Asia/Singapore验收。用户授权更新10086，下午由用户手动创建真实任务。

## 已更新

- Python运行版本`ca100239f609db789cf33f6783d67e0e64479ac3`，包含原Java定距映射、扩展动作、精确云台意图保护和审计。Java仓库/共享8088均未改。
- 前端静态版本沿用`python-flight-visible`（UI08fb3e5）；现有组件消费新Python接口，无需替换前端文件。
- 原用户级服务在SSH退出后被会话管理器停止，已停用该用户级实例，改用独立系统级`lingxi-flight-browser-10086.service`。没有开启全局linger或开机自启，没有重启其他服务。
- 工作目录`/home/zhuoyi/ktd/flight-browser-20261021/source-ca10023`；回环端口18974，经10086既有同源代理访问。内存上限384MiB、Swap0、CPU50%、Tasks32；实测占用约23MiB。为下午验收保留运行。
- 系统服务在后续独立SSH会话中保持active。实际10086页面选中天健湖草莓机库并打开助手，返回TASK_REQUIRED、禁止发送；审计读到409与当前代码SHA。未发送Java控制请求，未替用户取权/确认/起飞。

## 工具与证据边界

服务始终输出脱敏结构化审计：输入摘要、任务/设备/会话摘要、request/command/trace关联、模型建议动作及数值、用户确认/取消阶段、Java请求attempt/response/outcome_unknown、状态/错误与代码SHA。原始指令、登录凭据、坐标、Java响应正文不进入新增审计。

`tools/flight_audit.py`只读查询，默认最近10分钟、最多500条，最长180分钟/最多5000条；可按任务ID过滤并用after增量读取，截断明确标记。输出不把HTTP成功当设备完成。只使用原有系统日志，不开启数据库消费或任务调度。

远端执行示例（把任务ID换为本轮手动任务ID）：

```bash
ssh test-front 'cd /home/zhuoyi/ktd/flight-browser-20261021/source-ca10023 && PYTHONPATH=src /home/zhuoyi/ktd/flight-browser-20261021/runtime/python/bin/python3 tools/flight_audit.py --minutes 30 --task-id TASK_ID'
```

下午开始时提供任务ID，按该任务关联观察；无需经过任务下发Agent。既有automation-2仍暂停，此更新不创建后台轮询承诺。服务会被动记录用户操作，即使聊天尚未开始轮询也可回查；系统journal自身保留策略未修改，不承诺永久留存。

**未完成证据**：工具目前不自动采集厂商遥测/媒体，因此前飞10米的实际方向、位移、完成时间及拍照媒体仍需独立核对。精确云台10度仍因Java速度接口缺口被明确拒绝。复合“移动完再拍照”不能基于submitted自动推进。

## 验证

- `bash tools/ci/offline_tests.sh`：728/728通过，含不泄露凭据/原文、数值投影、任务过滤、确认与幂等回归。
- `PYTHONPATH=src python3 tools/bench/dji_browser_contract.py --offline-scripted`：7/7独立HTTP合成接口闭环，确认前0POST，真实设备0请求；临时HTTP与账本已清理。
- 编译检查、diff检查通过；远端真实只读审计运行通过，结果未截断。实际页面验证不是实飞完成证明。

## 停止、回滚和保留

- 停止：`ssh test-front 'systemctl stop lingxi-flight-browser-10086.service'`。
- 回滚：停止此系统服务后，用独立系统unit指向上一工作目录`source-direct-fix`，移除/修正CODE_SHA并daemon-reload后启动；不要重新启用会随SSH退出停止的用户级服务。旧unit备份`/home/zhuoyi/ktd/flight-browser-20261021/service-before-d2c2079`。
- 保留所有真实账本，不重发unknown命令；源目录、旧版本、私有Python和发布包用于回滚。新系统unit不启用开机自启。没有新建工作树。
- 本地临时打包目录`/tmp/flight-audit-release.bOLeZ4`保留发布包、unit备份和验收截图供回滚/复核，不包含用户凭据；截图不提交Git。
