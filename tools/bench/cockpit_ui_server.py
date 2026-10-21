#!/usr/bin/env python3
"""Explicit synthetic model-boundary fixture for local browser/API acceptance.

Never use this fixture to score model understanding. It accepts only these few
fixed UI-test phrases and cannot construct any platform executor.
"""
import argparse
import json

from lingxi_flight_control import (
    DecisionKind, MemoryFlightControlSimulator, ModelDecision, RuntimeBinding, ToolCall,
)
from lingxi_flight_control.cockpit import CockpitService
from lingxi_flight_control.standalone_preview import build_standalone_server


class FixtureBrain:
    def decide(self, text, checkpoint, tools):
        incoming = json.loads(text)
        phrase = incoming['current_request']
        commands = {
            '申请控制权': ('control_acquire', {}), '获取控制权': ('control_acquire', {}),
            '接管控制权': ('control_acquire', {}),
            '向前飞10米': ('move_forward', {'distance_m': 10}),
            '向左飞5米': ('move_left', {'distance_m': 5}),
            '10米': ('move_forward', {'distance_m': 10}), '十米': ('move_forward', {'distance_m': 10}),
            '拍照': ('camera_photo', {}), '拍一张照片': ('camera_photo', {}),
            '右转90度': ('turn_right', {'angle_deg': 90}),
            '先向前飞10米再拍照': ('move_forward', {'distance_m': 10}),
            '继续': ('camera_photo', {}), '急停': ('emergency_stop', {}),
        }
        if phrase in commands:
            name, arguments = commands[phrase]
            return ModelDecision(DecisionKind.TOOL_CALL, tool_call=ToolCall(name, arguments))
        return ModelDecision(DecisionKind.CLARIFY, message='请补充飞行距离，例如10米。')


def main():
    parser = argparse.ArgumentParser(description='离线浏览器专用：脚本模型 + 真实服务/模拟器')
    parser.add_argument('--port', type=int, default=8794)
    args = parser.parse_args()
    binding = RuntimeBinding('fixture-operator', 'fixture-project', 'fixture-hangar', 'fixture-drone', 'fixture-batch', 1)
    simulator = MemoryFlightControlSimulator(binding)
    simulator.state.longitude, simulator.state.latitude = 113, 23
    simulator.state.altitude_m, simulator.state.heading_deg = 50, 90
    service = CockpitService(binding, simulator, FixtureBrain)
    server = build_standalone_server(service, '127.0.0.1', args.port)
    print(f'fixture model only · simulator · http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
