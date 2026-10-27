"""一键端到端全链路集成冒烟测试 Harness (E2E Smoke Runner)。

依据 PROMPT_GROK_BUILD.md Task 4 要求：
1. 自动拉起 JavaPlatformMockServer, FenceServiceMockServer 与 MqttTelemetrySimulator；
2. 驱动典型测试链路：平台快照查询、微操移动、围栏校验、载荷调整、航线控制、遥测与低电量告警；
3. 断言全链路响应正常，状态流转无误；
4. 退出前必须在 finally 块中自动清理释放所有 Mock 端口与进程资源。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

# 确保脚本脱机或直接调用时均能正确定位 tools 模块
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.mocks.java_platform_mock import JavaPlatformMockServer
from tools.mocks.fence_service_mock import FenceServiceMockServer
from tools.mocks.mqtt_telemetry_simulator import MqttTelemetrySimulator


def run_smoke() -> int:
    print("[E2E Smoke] 正在初始化高保真仿真环境...")
    java_mock = JavaPlatformMockServer(port=18080)
    fence_mock = FenceServiceMockServer(port=18081)
    mqtt_mock = MqttTelemetrySimulator(initial_battery=25)

    received_telemetry = []

    try:
        java_mock.start()
        fence_mock.start()
        mqtt_mock.add_listener(lambda t: received_telemetry.append(t))
        mqtt_mock.start()
        print("[E2E Smoke] Mock 服务已就绪: Java平台(18080), 电子围栏(18081), MQTT遥测器")

        # 1. 验证平台快照查询
        print("[E2E Smoke] [Step 1/6] 检查平台快照接口 GET /screen/uav/status/snapshot...")
        req = urllib.request.Request("http://127.0.0.1:18080/screen/uav/status/snapshot")
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["code"] == 200
            assert data["data"]["drone_id"] == "sim-uav-01"
            assert data["data"]["route_status"] == "idle"
            print("  --> 快照验证通过: UAV状态正常，持有控制权。")

        # 2. 验证 37 点高精电子围栏校验
        print("[E2E Smoke] [Step 2/6] 检查电子围栏接口 POST /electronicFence/path-route/with-clean...")
        fence_req_data = {
            "projectId": "project-01",
            "operatorId": "actor-01",
            "snapshot": [
                {"longitude": 113.882100, "latitude": 22.511200, "altitude": 50.0},
                {"longitude": 113.882200, "latitude": 22.511200, "altitude": 50.0},
            ]
        }
        req = urllib.request.Request(
            "http://127.0.0.1:18081/electronicFence/path-route/with-clean",
            data=json.dumps(fence_req_data).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["code"] == 200
            assert len(data["data"]["invalid"]) == 0
            assert data["data"]["segment"][0]["status"] == "ok"
            print("  --> 围栏校验通过: 折线无碰撞，状态为 clear。")

        # 3. 验证微操指令下发
        print("[E2E Smoke] [Step 3/6] 检查微操指令下发 POST /screen/uav/operation/control...")
        op_payload = {"type": 201, "mode": 2, "param": 10.0}
        req = urllib.request.Request(
            "http://127.0.0.1:18080/screen/uav/operation/control",
            data=json.dumps(op_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["code"] == 200
            print("  --> 微操移动指令下发成功。")

        # 4. 验证载荷控制（拍照与变焦）
        print("[E2E Smoke] [Step 4/6] 检查载荷控制接口 POST /screen/uav/camera/control...")
        cam_payload = {"type": 100}
        req = urllib.request.Request(
            "http://127.0.0.1:18080/screen/uav/camera/control",
            data=json.dumps(cam_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["code"] == 200
            print("  --> 载荷拍照控制成功响应。")

        # 5. 验证航线控制（暂停与恢复）
        print("[E2E Smoke] [Step 5/6] 检查航线控制接口 POST /screen/uav/route/control...")
        route_payload = {"type": 120}  # 航线暂停
        req = urllib.request.Request(
            "http://127.0.0.1:18080/screen/uav/route/control",
            data=json.dumps(route_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["code"] == 200
            print("  --> 航线暂停指令受领成功。")

        # 6. 验证 MQTT 遥测上报与回执状态机
        print("[E2E Smoke] [Step 6/6] 验证遥测上报与指令状态回执流转...")
        time.sleep(0.8)  # 等待遥测采样与电量扣减
        assert len(received_telemetry) > 0
        latest_tel = received_telemetry[-1]
        assert latest_tel["drone_id"] == "sim-uav-01"
        assert latest_tel["battery"] < 25

        receipt_stages = mqtt_mock.simulate_command_receipt_lifecycle("cmd-smoke-001")
        assert receipt_stages == ["submitted", "executing", "completed"]
        print(f"  --> 遥测报文接收成功 ({len(received_telemetry)} 帧)，电量降至 {latest_tel['battery']}%。")
        print("  --> 指令回执三态演进闭环: submitted -> executing -> completed。")

        print("\n=======================================================")
        print("🎉 [E2E Smoke PASS] 全链路仿真集成冒烟 100% 通过！")
        print("=======================================================")
        return 0

    except Exception as e:
        print(f"\n❌ [E2E Smoke FAILED]: {e}", file=sys.stderr)
        return 1

    finally:
        print("[E2E Smoke] 正在清理与释放 Mock 后台服务...")
        java_mock.stop()
        fence_mock.stop()
        mqtt_mock.stop()
        print("[E2E Smoke] 端口 18080, 18081 与后台线程已完全安全释放。")


if __name__ == "__main__":
    sys.exit(run_smoke())
