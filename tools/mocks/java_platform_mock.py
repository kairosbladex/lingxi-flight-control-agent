"""Java 后端平台路由仿真服务 (JavaPlatformMock)。

依据 PROMPT_GROK_BUILD.md Task 1 要求：
- POST /screen/uav/route/control (航线控制：暂停 120/恢复 122/返航 105/急停 113)
- POST /screen/uav/operation/control (微操移动 201/转向 202/镜头云台)
- POST /screen/uav/camera/control (拍照 100/录像 101/变焦)
- GET  /screen/uav/status/snapshot (实时快照接口)
- 动态故障注入：route_status: null / control_owned: false
"""

from __future__ import annotations

import http.server
import json
import threading
from typing import Any


class JavaPlatformMockHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass  # 抑制默认打印，保持测试纯净

    def do_GET(self) -> None:
        if self.path.startswith("/screen/uav/status/snapshot"):
            # 返回平台快照
            snapshot = {
                "code": 200,
                "msg": "success",
                "data": {
                    "drone_id": "sim-uav-01",
                    "route_status": getattr(self.server, "inject_route_status", "idle"),
                    "task_status": "flying",
                    "longitude": 113.882100,
                    "latitude": 22.511200,
                    "altitude_m": 80.0,
                    "height_m": 30.0,
                    "heading_deg": 90.0,
                    "yaw_deg": 90.0,
                    "battery": 88,
                    "control_owned": getattr(self.server, "inject_control_owned", True),
                }
            }
            body = json.dumps(snapshot).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b"{}"
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            payload = {}

        if self.path.startswith("/screen/uav/route/control"):
            resp = {"code": 200, "msg": "route control accepted", "data": {"type": payload.get("type")}}
            self._send_json(200, resp)
            return

        if self.path.startswith("/screen/uav/operation/control"):
            resp = {"code": 200, "msg": "operation accepted", "data": payload}
            self._send_json(200, resp)
            return

        if self.path.startswith("/screen/uav/camera/control"):
            resp = {"code": 200, "msg": "camera control accepted", "data": payload}
            self._send_json(200, resp)
            return

        self.send_response(404)
        self.end_headers()

    def _send_json(self, status_code: int, data: dict[str, Any]) -> None:
        raw = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class JavaPlatformMockServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 18080):
        self.host = host
        self.port = port
        self.server = http.server.HTTPServer((self.host, self.port), JavaPlatformMockHandler)
        self.server.inject_route_status = "idle"
        self.server.inject_control_owned = True
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
