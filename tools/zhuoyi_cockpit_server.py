#!/usr/bin/env python3
"""Agent cockpit backed by the zhuoyi-ai-platfrom flight-control API.

The Java service is the only backend this entry point talks to. It is intended for an isolated
verification instance; the Java side still decides whether publishing is configured.
"""
import argparse
import os

from lingxi_flight_control import OllamaDecider, RuntimeBinding
from lingxi_flight_control.cockpit import COCKPIT_SYSTEM_PROMPT, CockpitService
from lingxi_flight_control.ollama_decider import DEFAULT_BASE_URL, DEFAULT_MODEL
from lingxi_flight_control.plan import plan_clarify_detector, plan_system_prompt
from lingxi_flight_control.standalone_preview import build_standalone_server
from lingxi_flight_control.zhuoyi_platform import ZhuoyiPlatformExecutor


def env(name, default=None):
    value = os.environ.get(name, default)
    if value is None or not str(value).strip():
        raise SystemExit(f"缺少环境变量 {name}")
    return value


def main():
    parser = argparse.ArgumentParser(description='灵犀飞控 Agent（zhuoyi-ai-platfrom 后端）')
    parser.add_argument('--port', type=int, default=8795)
    parser.add_argument('--model', default=DEFAULT_MODEL)
    parser.add_argument('--base-url', default=DEFAULT_BASE_URL)
    parser.add_argument('--max-plan-steps', type=int, default=5)
    args = parser.parse_args()

    binding = RuntimeBinding(
        env('ZHUOYI_ACTOR_ID', 'actor-1'),
        env('ZHUOYI_PROJECT_ID', 'project-1'),
        env('ZHUOYI_HANGAR_ID', 'hangar-1'),
        env('ZHUOYI_DRONE_ID', 'drone-1'),
        env('FLIGHT_AGENT_TASK_BATCH_ID', 'zhuoyi-batch-1'),
        int(env('FLIGHT_AGENT_CONTEXT_VERSION', '1')),
    )
    executor = ZhuoyiPlatformExecutor(
        binding,
        base_url=env('ZHUOYI_PLATFORM_BASE_URL', 'http://127.0.0.1:18091'),
        gateway_token=env('ZHUOYI_PLATFORM_GATEWAY_TOKEN'),
        site_id=env('ZHUOYI_SITE_ID', 'site-1'),
        task_id=os.environ.get('ZHUOYI_TASK_ID') or None,
        receipt_timeout=float(env('ZHUOYI_PLATFORM_RECEIPT_TIMEOUT', '10')),
        allow_insecure_http=True,
    )
    decider = lambda prompt, plan=False: (  # noqa: E731
        lambda: OllamaDecider(
            model=args.model, base_url=args.base_url, system_prompt=prompt,
            timeout=60, max_attempts=1, max_output_tokens=2048, keep_alive='30m',
            clarify_detector=plan_clarify_detector if plan else None)
    )
    service = CockpitService(
        binding, executor, decider(COCKPIT_SYSTEM_PROMPT),
        gateway_token=env('FLIGHT_AGENT_GATEWAY_TOKEN'),
        timeout_sec=180, enable_plan_execution=True,
        plan_decider_factory=decider(plan_system_prompt(args.max_plan_steps), plan=True),
        max_plan_steps=args.max_plan_steps)
    server = build_standalone_server(service, '127.0.0.1', args.port,
                                     allowed_origin='http://127.0.0.1:5173')
    print(f'飞控 Agent http://127.0.0.1:{server.server_port} · platform · '
          f'整份计划一次确认 · Java={executor.base_url}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
