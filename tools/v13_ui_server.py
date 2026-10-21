#!/usr/bin/env python3
"""Standalone V1.3 extension page and fixed-route gateway; old UI is never mounted."""
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
import signal
import threading
from lingxi_flight_control.v13_extension import Binding, V13Client, V13Facade


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dist', required=True)
    parser.add_argument('--java', required=True)
    parser.add_argument('--java-token-env', required=True)
    parser.add_argument('--project', required=True)
    parser.add_argument('--actor', required=True)
    parser.add_argument('--site', required=True)
    parser.add_argument('--hangar', required=True)
    parser.add_argument('--drone', required=True)
    parser.add_argument('--task', default='')
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    signal.alarm(300)
    client = V13Client(args.java, __import__('os').environ[args.java_token_env], args.project,
                       args.actor, Binding(args.site, args.hangar, args.drone, args.task))
    facade = V13Facade(client)
    # 浏览器只拿到一次性 CSRF；Java 服务凭证始终留在 Python 进程中。
    csrf = secrets.token_urlsafe(32)

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            if not self.path.startswith('/api/flight-v13/'):
                self.send_error(404)
                return
            if self.path == '/api/flight-v13/bootstrap':
                return self.do_POST_json({'csrf': csrf})
            if not secrets.compare_digest(self.headers.get('X-V13-CSRF', ''), csrf):
                self.send_error(403)
                return
            length = int(self.headers.get('Content-Length', 0))
            if not 0 <= length <= 65536:
                self.send_error(413)
                return
            try:
                body = json.loads(self.rfile.read(length) or b'{}')
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                self.send_error(400)
                return
            route = self.path.removeprefix('/api/flight-v13/')
            try:
                status, payload = facade.handle(route, body)
            except Exception:
                status, payload = 502, {'code': 'UPSTREAM_UNAVAILABLE', 'message': '状态未确认，请勿重复确认'}
            raw = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path == '/healthz':
                return self.do_POST_json({'status': 'isolated-ui'})
            if self.path == '/':
                self.path = '/v13.html'
            return super().do_GET()

        def do_POST_json(self, value):
            raw = json.dumps(value).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(('127.0.0.1', args.port), partial(Handler, directory=args.dist))
    print(json.dumps({'url': 'http://127.0.0.1:%d' % server.server_port, 'isolated': True}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
