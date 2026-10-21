#!/usr/bin/env python3
"""独立 HTTP 编解码烟测：仅本脚本创建的 127.0.0.1 假服务，无外部地址参数。"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

from lingxi_flight_control.ktd_platform import KtdCommand, KtdHttpTransport, KtdRequestContext


def main():
    requests = []
    response_status = [200]

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            requests.append((self.path, json.loads(self.rfile.read(length))))
            self.send_response(response_status[0])
            self.send_header("Content-Type", "application/json")
            if response_status[0] == 302:
                self.send_header("Location", "/must-not-follow")
            self.end_headers()
            self.wfile.write(b'{"code":200}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        transport = KtdHttpTransport(
            f"http://127.0.0.1:{server.server_port}/api", token="local-fake-token",
            actor_id="local-test-actor", allow_insecure_http=True,
        )
        context = KtdRequestContext("local-test-actor", "local-test-project", "local-trace", "local-key")
        command = KtdCommand("/screen/uav/route/control", {"terminalId": "local-fake-drone", "type": 113})
        response = transport.submit(command, context)
        assert response.status_code == 200 and response.payload == {"code": 200}
        assert requests == [("/api/screen/uav/route/control", dict(command.body))]
        response_status[0] = 302
        response = transport.submit(command, context)
        assert response.status_code == 302
        assert len(requests) == 2 and all(path == "/api/screen/uav/route/control" for path, _ in requests)
        print("PASS: fixed request encoding and no redirect; 2 local fake requests, 0 platform/device requests")
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


if __name__ == "__main__":
    main()
