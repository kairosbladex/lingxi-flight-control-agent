#!/usr/bin/env python3
"""Local simulator UI service; enabling a physical backend is an integration task."""
import argparse

from lingxi_flight_control import MemoryFlightControlSimulator, OllamaDecider, RuntimeBinding
from lingxi_flight_control.cockpit import COCKPIT_SYSTEM_PROMPT, CockpitService
from lingxi_flight_control.ollama_decider import DEFAULT_BASE_URL, DEFAULT_MODEL
from lingxi_flight_control.standalone_preview import build_standalone_server


def main():
    parser = argparse.ArgumentParser(description='灵犀飞控交互服务（仅本机模拟器）')
    parser.add_argument('--port', type=int, default=8794)
    parser.add_argument('--model', default=DEFAULT_MODEL)
    parser.add_argument('--base-url', default=DEFAULT_BASE_URL)
    args = parser.parse_args()
    binding = RuntimeBinding('local-operator', 'demo-project', 'demo-hangar', 'demo-drone', 'demo-batch', 1)
    simulator = MemoryFlightControlSimulator(binding)
    simulator.state.longitude, simulator.state.latitude = 113, 23
    simulator.state.heading_deg = 90
    simulator.state.altitude_m, simulator.state.task_status = 50, 'flying'
    service = CockpitService(binding, simulator, lambda: OllamaDecider(
        model=args.model, base_url=args.base_url, system_prompt=COCKPIT_SYSTEM_PROMPT,
        timeout=20, max_attempts=1, max_output_tokens=2048, keep_alive='30m'), timeout_sec=65)
    server = build_standalone_server(service, '127.0.0.1', args.port, allowed_origin='http://127.0.0.1:5173')
    print(f'飞控服务 http://127.0.0.1:{server.server_port} · simulator · 每个动作需确认', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
