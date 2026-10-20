# Route A：不改 ktd_backend 的测试控制链路

日期：2026-10-20。状态：本地实现与离线验证完成；**未连接测试环境、未发送任何真实命令**，默认关闭。
范围：Agent 直连 ktd 现有 `/screen/uav/...` 控制接口；授权由 Agent 用 ktd 现有只读接口拼装，标注
`authorization_quality=legacy-read-api`。不改 ktd_backend，不新增 MQTT 连接，不直连 ktd 数据库。

## 边界

- 只支持 `DJI_HANGAR`。
- 开放 18 个写动作：6 向位移、拍照、开始/停止录像、7 个云台动作、左右转向；`read_state` 始终可读。
- 转向和云台相对步进使用 Agent 侧闭环/停止逻辑；不实现航线、起飞/降落、速度模式、键盘模式、厂商取权/DRC。
- 摇杆持续姿态由 `KtdPlatformExecutor.stick()` 提供，**不是模型工具**，只供测试环境脚本/操作者直接调用；
  旧接口一次只能携带一个轴。
- 不提供设备完成回执；成功只表示 Java 旧接口受理，固定 `device_effect=not_verified`。
- 位移仍没有 Agent 侧姿态校验；转向依赖旧调试接口回读航向，属于测试授权，不是权威快照，不能用于生产授权。

## 授权数据来源

| 事实 | 旧只读接口 | 限制 |
| --- | --- | --- |
| 操作者身份/权限 | `GET /user/info` | 必须与配置的 actor `empId` 一致；写动作要求 `taskManage:Issued` |
| 项目/站点/机库/无人机绑定 | `GET /uavSite/list?projectId=` | 只核对固定机场的 site/uav/hangar 关联；不校验任务类型 |
| 控制权 owner | `GET /uavControlPower/list?uavId=` | 不返回租约/更新时间；多 owner 直接视为无 owner |
| 机型/机库 profile | `GET /screen/control/permissions/3/<droneId>` | 只接受大疆机库；不采用其中的 `UAV_MOVE_DISTANCE=false` |
| 在线/飞行状态 | `GET /portal/task/hangar-view/state?uavId=` | 没有经纬度/高度/航向和时间戳；`uavState=-1/14` 视为离线 |
| 实时姿态 | `GET /test/getUdpData?sn=<uavSn>` | 调试接口，生产可能不开放；返回经纬度、绝对/相对高度、yaw、姿态时间和 `uavState`。读不到时转向权限被移除，位移仍可按下面的姿态未核验开关执行 |

`observed_at` 优先取姿态接口的 `timeStamp`（毫秒或秒），没有时退回本次读取时间；控制权租约是
`读取时间 + 300 秒` 的合成值，不进入确认绑定哈希。确认阶段会重新读取；身份、资源绑定、owner、
在线/飞行状态或姿态变化都会使旧确认卡失效。这些旧接口不发送控制指令、不主动写业务表，但
`hangar-view/state` 沿用的旧缓存读取可能触发其自身的过期清理。

## 启动方式

沿用 `tools/platform_cockpit_server.py`，新增两个开关：

```bash
# 只读：旧接口可读，但不能写
PYTHONPATH=src python3 tools/platform_cockpit_server.py --port 8794 --enable-legacy-control

# 写：两个开关都必须显式打开
PYTHONPATH=src python3 tools/platform_cockpit_server.py --port 8794 \
  --enable-platform-control --enable-legacy-control

# 位移：再加姿态未核验开关（旧接口没有姿态，目标点只能由 Java 服务端计算）
PYTHONPATH=src python3 tools/platform_cockpit_server.py --port 8794 \
  --enable-platform-control --enable-legacy-control --allow-legacy-pose-unverified
```

也可以用环境变量替代 CLI：

- `FLIGHT_AGENT_LEGACY_READ_API=true`
- `FLIGHT_AGENT_LEGACY_ALLOW_POSE_UNVERIFIED=true`（必须同时设置上一项）

原有服务端配置不变（`KTD_BASE_URL`、`KTD_SERVER_TOKEN`、`KTD_ACTOR_EMPLOYEE_ID`、
`KTD_PROJECT_ID`、`KTD_SITE_ID`、`KTD_HANGAR_ID`、`KTD_DRONE_ID`、`FLIGHT_AGENT_GATEWAY_TOKEN`、
`FLIGHT_AGENT_JOURNAL`）。`KTD_SERVER_TOKEN` 必须是**当前持有控制权的操作者**登录凭据，不能是服务账号。

## 测试环境前置

1. ktd 测试环境已部署且旧接口可用；不需要改 ktd 代码，也不需要新增快照接口。
2. 测试账号在 ktd 驾驶舱取得该无人机的控制权；`/uavControlPower/list` 必须只返回该员工。
3. 账号具备 `taskManage:Issued`；设备已领用、项目/站点/机库/无人机绑定一致。
4. 无人机在线；位移和转向还要求 `inFlight=true`。
5. 转向要求 `/test/getUdpData?sn=<uavSn>` 可读且返回有限 `yaw`；该调试接口不可用时只能做位移/云台/相机。
6. 相机/云台可在未起飞状态验证；位移和转向只在隔离空域、操作员在场、急停可用的条件下验证。

## 姿态控制

### 转向（`turn_left` / `turn_right`）

- 使用旧控制接口 `mode=3`（杆量）和 `type=206/-206`，按航向误差发短脉冲；
- 每个脉冲后回读 `/test/getUdpData` 的 `yaw`，误差进入默认 3° 容差即成功；
- 单次最多 30 秒、80 个脉冲；误差连续增大（方向约定不符）或航向连续 6 个脉冲几乎不变时立即停止；
- 无论成功、失败还是异常，结束时都会发送回中（`param=0`）和删除运动记录（`type=207`）；
- 回执 `accepted` 只表示闭环到达容差且已回中，仍固定 `device_effect=not_verified`；
- 读不到姿态时转向动作不会出现在权限里，预览直接返回 `ACTION_NOT_PERMITTED`。

### 云台

| 动作 | 旧接口参数 | 结束处理 |
| --- | --- | --- |
| `gimbal_up` / `gimbal_down` | `type=401, controlMode=2, param=1/2, stepPercentage=5/3` | 一次 5° 俯仰；再发 `param=0` 停止 |
| `gimbal_left` / `gimbal_right` | `type=402, controlMode=2, param=2/1, stepPercentage=1` | 旧接口会启动连续发送线程；Agent 在 300ms 后发 `param=0` 停止 |
| `gimbal_45` / `gimbal_90` | `type=404, param=-45/-90` | 旧接口内部处理 |
| `gimbal_reset` | `type=403` | 旧接口内部复位 |

相对云台停止失败会返回 `unknown/GIMBAL_STOP_FAILED`，不自动重试。

### 摇杆持续姿态（脚本原语）

`KtdPlatformExecutor.stick(axis, percent, duration_sec)` 支持单轴
`forward/backward/left/right/up/down/yaw_left/yaw_right`，`percent` 1–100、时长 0.2–5 秒；
发送间隔 ≤0.8 秒，结束时同样回中并删除运动记录。它不是模型工具，不改变 59 项动作契约。

## 行为与限制

- **相机/云台/转向**：不需要 `--allow-legacy-pose-unverified`；`--enable-legacy-control` +
  `--enable-platform-control` 即可预览和确认。转向额外依赖姿态回读成功。
- **位移**：必须额外打开 `--allow-legacy-pose-unverified`。Agent 只校验距离 0.01–20 米和
  `airborne=true`；目标点由 ktd 的 `DJHangarOperation` 使用服务端实时姿态计算。
  **Agent 不执行原有的 20 米相对高度/隐含爬升门禁**，这是 route A 的已知安全降级。
- **转向闭环**：确认请求会阻塞到转向结束或超时（最多约 30 秒）；期间同一会话不能提交新指令，
  也不能通过会话取消中止。需要立即中止时使用 ktd 驾驶舱的停止/急停，或等待 Agent 自动回中。
- **回执**：旧接口不返回命令 ID；Agent 不添加 `tracking`，不调用回执查询，结果固定为
  `submitted` 或 `unknown/rejected`，不合成完成状态。
- **幂等**：本地 SQLite journal 仍生效；`unknown` 不自动重发，需要人工核对。
- **控制权**：以 ktd 旧接口返回的 owner 为准；Agent 不获取、不续租、不释放控制权。

## 安全与回滚

- 默认关闭；只读启动不发送任何写请求。写动作必须同时打开 `--enable-platform-control` 和
  `--enable-legacy-control`；位移必须再单独打开姿态未核验开关。
- 合成租约不是厂商航权证明；本链路只适用于已明确授权的隔离测试环境，不能作为生产控制或安全验收依据。
- 回滚：去掉两个/三个开关即可停止写请求；保留 journal 和已提交记录，不自动重试、不删除审计数据。
- 该实现没有修改 ktd_backend，也没有新增对 ktd 数据库、Redis 或 MQTT 的直接访问。

## 验证

新增 `tests/test_ktd_legacy.py` **32 项**，覆盖身份/绑定/owner/权限/机型/在线状态门禁、18 个写动作映射、
位移姿态开关、转向闭环（到达容差、无姿态拒绝、方向相反时中止）、云台相对步进与停止、摇杆原语边界、
确认期间正常姿态漂移不误拒、租约合成值不参与确认绑定、owner 变化拒绝、quality 不匹配拒绝、
传输路径白名单、操作控制时间戳严格递增、姿态时间戳秒/毫秒解析和配置解析。

```bash
bash tools/ci/offline_tests.sh
# 669 tests, OK（2026-10-20）
```

全部为合成 HTTP 边界、临时 SQLite 与固定时钟；没有 socket、真实模型、MQTT、ktd 服务或设备请求。
