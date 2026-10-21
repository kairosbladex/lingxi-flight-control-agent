#!/usr/bin/env python3
"""启动不依赖 Java/旧前端的飞控 Agent 预览 API。"""

from __future__ import annotations

import argparse
import os
import sys

from lingxi_flight_control import (
    DEFAULT_BASE_URL,
    DEFAULT_CARD_TTL_SEC,
    DEFAULT_MODEL,
    DEFAULT_POSE_TTL_SEC,
    OllamaDecider,
    StandalonePreviewService,
    build_standalone_server,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="独立飞控 Agent 预览服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8793)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--pose-ttl-sec", type=float, default=DEFAULT_POSE_TTL_SEC)
    parser.add_argument("--card-ttl-sec", type=float, default=DEFAULT_CARD_TTL_SEC)
    parser.add_argument("--allowed-origin", default="*")
    parser.add_argument("--gateway-token-env", default=None,
                        help="持有该凭证的网关才能调用（凭证值从该环境变量读）；不设则保持本地开发行为")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    gateway_token = None
    if args.gateway_token_env:
        gateway_token = os.environ.get(args.gateway_token_env)
        if not gateway_token:
            print(f"环境变量 {args.gateway_token_env} 未设置，拒绝以无凭证方式启动", file=sys.stderr)
            return 2
    service = StandalonePreviewService(
        lambda: OllamaDecider(model=args.model, base_url=args.base_url, keep_alive="30m"),
        pose_ttl_sec=args.pose_ttl_sec, card_ttl_sec=args.card_ttl_sec,
        gateway_token=gateway_token,
    )
    server = build_standalone_server(service, args.host, args.port,
                                     allowed_origin=args.allowed_origin)
    host, port = server.server_address
    print(f"独立飞控 Agent 预览服务已启动: http://{host}:{port}", flush=True)
    print(f"模型={args.model}  端点={args.base_url}  位姿TTL={args.pose_ttl_sec:.0f}s  卡片TTL={args.card_ttl_sec:.0f}s", flush=True)
    print("健康检查: curl -s http://%s:%s/healthz" % (host, port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n收到中断，正在停止服务。", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
