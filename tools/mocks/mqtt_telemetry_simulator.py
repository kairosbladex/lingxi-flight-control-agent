"""本地 MQTT 遥测发生器与设备回执捕获仿真器 (MqttTelemetrySimulator)。

依据 PROMPT_GROK_BUILD.md Task 3 要求：
- 周期性生成遥测报文（经纬度、高、朝向角、电量、当前航点）
- 电量递减模拟与低于 20% 时触发低电量报警事件
- 模拟指令受理后的状态流转回执：submitted → executing → completed
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable


class MqttTelemetrySimulator:
    """本地轻量级遥测与回执状态机仿真器。"""

    def __init__(self, drone_id: str = "sim-uav-01", initial_battery: int = 100):
        self.drone_id = drone_id
        self.battery = initial_battery
        self.longitude = 113.882100
        self.latitude = 22.511200
        self.altitude_m = 80.0
        self.height_m = 30.0
        self.heading_deg = 90.0
        self.waypoint_index = 0
        self.low_battery_triggered = False

        self._running = False
        self._thread: threading.Thread | None = None
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._receipt_log: list[dict[str, Any]] = []

    def add_telemetry_listener(self, listener: Callable[[dict[str, Any]], None]) -> None:
        self._listeners.append(listener)

    def add_listener(self, listener: Callable[[dict[str, Any]], None]) -> None:
        self.add_telemetry_listener(listener)

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def simulate_command_receipt_lifecycle(self, command_id: str) -> list[str]:
        """模拟指令下发到设备后的回执三态演进。"""
        stages = ["submitted", "executing", "completed"]
        for stage in stages:
            self._receipt_log.append({
                "command_id": command_id,
                "stage": stage,
                "timestamp": time.time(),
            })
        return stages

    def _loop(self) -> None:
        while self._running:
            self.battery = max(0, self.battery - 1)
            telemetry = {
                "drone_id": self.drone_id,
                "longitude": self.longitude,
                "latitude": self.latitude,
                "altitude_m": self.altitude_m,
                "height_m": self.height_m,
                "heading_deg": self.heading_deg,
                "battery": self.battery,
                "waypoint_index": self.waypoint_index,
                "timestamp": time.time(),
            }

            if self.battery < 20 and not self.low_battery_triggered:
                self.low_battery_triggered = True
                telemetry["alarm"] = "LOW_BATTERY_WARNING"

            for listener in self._listeners:
                try:
                    listener(telemetry)
                except Exception:
                    pass

            time.sleep(0.1)  # 快速采样循环供自动化冒烟使用
