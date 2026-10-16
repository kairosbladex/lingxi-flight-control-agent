#!/usr/bin/env python3
"""独立平台入口：只监听回环，默认只读，配置和凭据仅从服务端环境读取。"""
import argparse
import os
import sys

from lingxi_flight_control import OllamaDecider
from lingxi_flight_control.cockpit import COCKPIT_SYSTEM_PROMPT
from lingxi_flight_control.ollama_decider import DEFAULT_BASE_URL, DEFAULT_MODEL
from lingxi_flight_control.platform_runtime import PlatformRuntimeConfig, build_platform_cockpit
from lingxi_flight_control.standalone_preview import build_standalone_server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8794)
    parser.add_argument('--model', default=DEFAULT_MODEL)
    parser.add_argument('--base-url', default=DEFAULT_BASE_URL, help='模型服务地址')
    parser.add_argument('--enable-platform-control', action='store_true',
                        help='显式开放已获权威许可的动作提议与逐次确认；需单独的设备测试授权')
    parser.add_argument('--enable-vendor-requests', action='store_true',
                        help='显式允许配置白名单内的厂商请求提议与逐次确认；不授予持续航权或启用飞行动作')
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error('port必须在1至65535之间')
    try:
        config = PlatformRuntimeConfig.from_environment(os.environ)
        service = build_platform_cockpit(config, lambda: OllamaDecider(
            model=args.model, base_url=args.base_url, system_prompt=COCKPIT_SYSTEM_PROMPT,
            timeout=20, max_attempts=1, max_output_tokens=2048, keep_alive='30m'),
            enable_control=args.enable_platform_control, enable_vendor_requests=args.enable_vendor_requests)
        server = build_standalone_server(service, '127.0.0.1', args.port,
                                          allowed_origin='http://127.0.0.1:5173')
    except (ValueError, OSError):
        # 启动异常不回显配置、凭据、文件路径或上游响应。
        print('平台入口启动失败，请核对服务端配置、私有日志目录和监听端口。', file=sys.stderr)
        return 2
    mode = '逐次确认控制已启用' if args.enable_platform_control else '只读，控制未启用'
    if args.enable_vendor_requests:
        mode += '；厂商请求仅开放配置白名单，仍需逐项确认'
    print(f'平台飞控服务 http://127.0.0.1:{server.server_port} · {mode}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
