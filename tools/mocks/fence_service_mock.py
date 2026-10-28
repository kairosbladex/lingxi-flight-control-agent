"""37点高精电子围栏仿真服务 (FenceServiceMock)。

依据 PROMPT_GROK_BUILD.md Task 2 要求：
- POST /electronicFence/path-route/with-clean
- 校验采样点经纬度与高度数据
- 虚拟禁飞区几何体碰撞检测：
  - 全部在限制区外返回 status: "clear" (segment 列表且 converge: "ln_outside")
  - 穿透禁飞区返回 status: "blocked" 并提供碰撞点索引
  - 支持模拟网络超时注入
"""

from __future__ import annotations

import http.server
import json
import threading
import time
from typing import Any


class FenceServiceMockHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        if getattr(self.server, "inject_timeout", False):
            time.sleep(3.5)  # 注入超过 3000ms 超时

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b"{}"
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            payload = {}

        if self.path.startswith("/electronicFence/path-route/with-clean"):
            points = payload.get("snapshot") or payload.get("points") or []
            project_id = payload.get("projectId", "project")
            operator_id = payload.get("operatorId", "actor")

            # 模拟碰撞检测：经度大于 113.885000 判定为触碰禁飞区
            blocked_idx = None
            segments_data = []
            num_segments = max(len(points) - 1, 1)

            for idx in range(num_segments):
                p = points[min(idx + 1, len(points) - 1)] if points else {}
                lon = p.get("longitude", 0.0) if isinstance(p, dict) else 0.0
                if lon > 113.885000 and blocked_idx is None:
                    blocked_idx = idx

            if blocked_idx is not None:
                resp = {
                    "code": 200,
                    "msg": "path evaluated",
                    "data": {
                        "projectId": project_id,
                        "operatorId": operator_id,
                        "snapshot": points,
                        "invalid": [{"index": blocked_idx, "reason": "COLLISION_NO_FLY_ZONE"}],
                        "segment": [{"status": "ng", "converge": "ln_intersect"} for _ in range(num_segments)],
                    }
                }
            else:
                resp = {
                    "code": 200,
                    "msg": "path evaluated",
                    "data": {
                        "projectId": project_id,
                        "operatorId": operator_id,
                        "snapshot": points,
                        "invalid": [],
                        "segment": [{"status": "ok", "converge": "ln_outside"} for _ in range(num_segments)],
                    }
                }

            raw = json.dumps(resp).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        self.send_response(404)
        self.end_headers()


class FenceServiceMockServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 18081):
        self.host = host
        self.port = port
        self.server = http.server.HTTPServer((self.host, self.port), FenceServiceMockHandler)
        self.server.inject_timeout = False
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
