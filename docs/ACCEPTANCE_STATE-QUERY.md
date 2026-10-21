# 状态直读与10086更新（2026-10-21）

## 改动

- 后端 `309bb03c4a607c8eb5b71d438e2b7176b0b881e1`，前端 `427dd86b0a6290325ffdcce3aebe165bb8d6e832`，均已推送并核验远端SHA。
- 明确完整的状态、电量、高度、控制权查询直接读取现有执行器快照，不创建模型，不生成确认卡，不发送控制指令。复合、否定、引用和条件句不命中直读路径。
- 保留鉴权、设备绑定、上下文版本、请求幂等、会话上限及状态不可用门禁。新查询清除旧意图与确认卡，重复请求返回原响应，新请求重新读状态。
- 明确区分 `RUN_TIMEOUT`、`MODEL_STEP_LIMIT`、`MODEL_CONTEXT_LIMIT`、`STATE_UNAVAILABLE`；旧预算码兼容文案不再要求用户缩短句子。未增加预算或放宽飞控权限。
- 状态缺失字段显示未知；平台读数不等于设备动作验收。模型动作路径仍采用原调用返回后的时限检查，本次没有重构模型传输取消机制。

## 验证

- 修复前 `PYTHONPATH=src python3 -m unittest discover -s tests -p test_direct_state_query.py` 复现原句失败；随后清除测试导入导致的重复发现。
- `bash tools/ci/offline_tests.sh`：746/746，包括全套socket禁止连接守护。
- `npm test`：154/154；`npm run build`通过；`npm run e2e:chat`：27项既有浏览器场景、0浏览器错误、0禁止请求。
- `PYTHONPATH=src python3 tools/bench/state_query_smoke.py`：3/3，模型调用0、合成Java控制POST 0、真实设备请求0。本机回环57777已关闭。
- 同一脚本在远端新源码目录、独立限额systemd临时实例运行通过；MemoryMax192M、Swap0、CPU50%、Tasks32、60秒上限，实例已退出并回收。
- `git diff --check`、Python编译检查通过。证据见 [verification.json](evidence/state-query-20261021/verification.json)。

## 部署与边界

- 用户明确授权更新10086；仅重启独立 `lingxi-flight-browser-10086.service`，新工作目录 `/home/zhuoyi/ktd/flight-browser-20261021/source-309bb03`。
- 独立静态链接 `/home/zhuoyi/ktd/dsm-frontend-10086/site` 指向 `releases/state-query-309bb03`，复制旧站点后仅替换 `flight-agent-ui/ktd/assistant.js`。原入口与其他静态文件保持不变。
- 线上 `/afmap/` 与健康接口可达；线上JS SHA256为 `226df20b559301604a8265d887cdc9dbdfb8ae9e0557d02dd7597beccb4b72f1`，与本机构建相同；运行源码哈希相同，脱敏审计记录新代码SHA。
- 未登录状态消息请求返回401，原鉴权未绕过。未使用用户登录态读取真实设备，本轮真实状态页面验收未完成；用户需刷新页面重试原句。
- 不修改8088/共享Java、不确认、不取权、不起飞、不发送真实飞控指令。最近审计窗口没有Java控制事件，不代表全历史无控制行为。

## 资源与回滚

- 保留用户验收实例：回环18974，经10086同源代理；MemoryMax384MiB、Swap0、CPU50%、Tasks32，实测约16.5MiB，未设开机自启。
- 停止：`ssh test-front 'systemctl stop lingxi-flight-browser-10086.service'`。
- 回滚：停止上述服务，恢复 `/home/zhuoyi/ktd/flight-browser-20261021/service-before-state-query-309bb03` 为 `/etc/systemd/system/lingxi-flight-browser-10086.service`，daemon-reload后启动；静态site链接恢复至 `releases/java-tools-18c9c36`。保留账本与审计，不重发未知命令。
- 旧源码/静态版本与unit备份保留用于回滚。本次上传暂存与本机发布包清理；本机浏览器合成证据保留于临时目录。没有新增工作树；两个既有语义分支分别提交。
