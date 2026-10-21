#!/usr/bin/env python3
"""独立平台入口：只监听回环，默认只读，配置和凭据仅从服务端环境读取。"""
import argparse
import os
import sys
import secrets
import json

from lingxi_flight_control import OllamaDecider
from lingxi_flight_control.cockpit import COCKPIT_SYSTEM_PROMPT
from lingxi_flight_control.ollama_decider import DEFAULT_BASE_URL, DEFAULT_MODEL
from lingxi_flight_control.platform_runtime import PlatformRuntimeConfig, build_platform_cockpit
from lingxi_flight_control.standalone_preview import build_standalone_server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8794)
    parser.add_argument('--model', default=DEFAULT_MODEL)
    parser.add_argument('--model-timeout', type=int, default=45,
                        help='单次模型理解超时秒数，范围1至60，不重试')
    parser.add_argument('--base-url', default=DEFAULT_BASE_URL, help='模型服务地址')
    parser.add_argument('--enable-platform-control', action='store_true',
                        help='显式开放已获权威许可的动作提议与逐次确认；需单独的设备测试授权')
    parser.add_argument('--java-gateway', action='store_true',
                        help='由已认证Java网关注入每次请求的登录及设备上下文')
    parser.add_argument('--browser-gateway', action='store_true',
                        help='浏览器同源入口；Python调用原Java接口核验登录，不需要修改Java')
    parser.add_argument('--enable-vendor-requests', action='store_true',
                        help='显式允许配置白名单内的厂商请求提议与逐次确认；不授予持续航权或启用飞行动作')
    parser.add_argument('--enable-legacy-control', action='store_true',
                        help='route A：仅用 ktd 现有只读接口做测试授权，仍逐次确认；需单独的设备测试授权')
    parser.add_argument('--allow-legacy-pose-unverified', action='store_true',
                        help='允许 route A 位移：旧接口无姿态，目标点只能由 Java 服务端实时姿态计算')
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error('port必须在1至65535之间')
    if not 1 <= args.model_timeout <= 60:
        parser.error('model-timeout必须在1至60之间')
    if args.allow_legacy_pose_unverified and not args.enable_legacy_control:
        parser.error('--allow-legacy-pose-unverified 必须与 --enable-legacy-control 同时使用')
    try:
        environment = dict(os.environ)
        if args.java_gateway and args.browser_gateway:
            raise ValueError('网关模式互斥')
        if args.java_gateway or args.browser_gateway:
            if args.enable_legacy_control or args.allow_legacy_pose_unverified or args.enable_vendor_requests:
                raise ValueError('Java网关不能降级授权或自动取权')
            for key in ('KTD_ACTOR_EMPLOYEE_ID', 'KTD_PROJECT_ID', 'KTD_HANGAR_ID',
                        'KTD_DRONE_ID', 'KTD_SITE_ID', 'FLIGHT_AGENT_TASK_BATCH_ID', 'KTD_SERVER_TOKEN'):
                environment[key] = 'gateway-unbound'
            environment.pop('KTD_TASK_ID', None)
            if args.browser_gateway:
                environment['FLIGHT_AGENT_GATEWAY_TOKEN'] = secrets.token_urlsafe(32)
        config = PlatformRuntimeConfig.from_environment(environment)
        decider = lambda: OllamaDecider(
            model=args.model, base_url=args.base_url, system_prompt=COCKPIT_SYSTEM_PROMPT,
            timeout=args.model_timeout, max_attempts=1, max_output_tokens=2048, keep_alive='30m')
        if args.browser_gateway:
            from lingxi_flight_control.browser_gateway import BrowserGateway
            service = BrowserGateway(config, decider, enable_control=args.enable_platform_control,
                allowed_drone_sns=json.loads(environment.get('FLIGHT_AGENT_ALLOWED_DRONE_SNS', '[]')))
        elif args.java_gateway:
            from lingxi_flight_control.platform_gateway import PlatformGateway
            service = PlatformGateway(config, decider, enable_control=args.enable_platform_control)
        else:
            service = build_platform_cockpit(config, decider,
            enable_control=args.enable_platform_control, enable_vendor_requests=args.enable_vendor_requests,
            enable_legacy_control=True if args.enable_legacy_control else None,
            allow_legacy_pose_unverified=True if args.allow_legacy_pose_unverified else None)
        server = build_standalone_server(service, '127.0.0.1', args.port,
                                          allowed_origin='http://127.0.0.1:5173')
    except (ValueError, OSError):
        # 启动异常不回显配置、凭据、文件路径或上游响应。
        print('平台入口启动失败，请核对服务端配置、私有日志目录和监听端口。', file=sys.stderr)
        return 2
    mode = '逐次确认控制已启用' if args.enable_platform_control else '只读，控制未启用'
    if args.enable_legacy_control:
        mode += '；route A 旧只读接口授权（测试用，非权威快照）'
        if args.allow_legacy_pose_unverified:
            mode += '；位移姿态未核验'
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
