# 精确距离与云台角度意图

2026-10-21，用户明确重点为人工不易精确操作的数值控制，继续保持Java不改、真机命令不代发。

## 实现和证据

- “往前飞10米”保留`move_forward/distance_m=10`，沿用原Java mode=2定距请求，确认后才提交；动作方向以机头为准。仍需新鲜完整位姿、空中状态、任务和权限，并保留20米隐含爬升门禁。
- 四方向云台工具增加`angle_deg`，范围0.1–90度。模型建议须与原文对应方向的精确角度一致；不能丢掉10度回退默认步进，不能借用同句飞机转向的10度，不能把“调到负10度”当“向下调10度”。模糊的“一点”、范围或约数要求澄清。
- 模拟器按显式角度执行并拒绝俯仰越界，不把请求静默截断成另一角度；这仅验证软件数值契约。
- 原Java `DJUtil.camera_screen_drag`明确使用pitch_speed/yaw_speed（度/秒）；`gimbalControl`的stepPercentage最终乘成速度。param=0仅停止Java重复发送标记，没有下发厂商零速度。绝对模式也没有通用目标角度执行分支。因此本轮不把该接口包装成精确转10度，返回`GIMBAL_EXACT_ANGLE_UNAVAILABLE`并明确未下发。
- 云台精确执行仍缺可靠的角度设定入口，或者可核验角度回读与停止机制。原接口“已受理”和软件模拟都不能补足这个缺口。没有据此修改Java或擅自用近似定时运动代替。

## 实际验证

`bash tools/ci/offline_tests.sh`：725/725通过，包含数值契约、方向/数值绑定、绝对与相对消歧、误用其他动作数字、平台零发送及模拟边界回归。`compileall`及`git diff --check`通过。

`PYTHONPATH=src python3 tools/bench/dji_browser_contract.py --precision-only --base-url <现有模型地址>`：qwen3.5:9b、实际入口同prompt/2048输出上限/45秒单次超时、隔离HTTP、合成Java。

| 输入 | 模型结果 | 合成接口结果 | 是否证明真机达标 |
| --- | --- | --- | --- |
| 往前飞10米 | move_forward，10米 | 确认前0POST，确认后submitted/not_verified | 否 |
| 云台向下调10度 | gimbal_down，10度 | 旧接口精确角度不可用，0POST | 否，明确阻塞 |

两条意图和门禁符合预期，不表示两种精确动作都实现。临时HTTP服务和账本均在finally中清理。没有新建工作树，Java不变，10086没有替换；发布与真机验收仍待完成。回滚使用本轮前Python提交3de1c08，保留真实审计账本。
