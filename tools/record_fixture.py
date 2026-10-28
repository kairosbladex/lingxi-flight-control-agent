#!/usr/bin/env python3
"""把驾驶舱动作序列导出成报文夹具（供无真机环境验证执行器适配层）。

用法：
    PYTHONPATH=src python3 tools/record_fixture.py fixtures/cockpit_basic.json

**诚实声明**：导出的报文来自 `MemoryFlightControlSimulator` 的回执，形状与驾驶舱
对外契约一致，但**不是**真机/MQTT 现场录制。夹具里的 ``source`` 字段会如实写成
``simulator-export``。接入真机后，用真实平台报文替换 ``recordings``，并把 ``source``
改成 ``platform-recording``；在那之前，任何「已接真实平台」的说法都不成立。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from lingxi_flight_control import (
    FlightControlAction,
    RuntimeBinding,
    export_fixture,
)

#: 与 tools/flight_agent_demo.py --cockpit 的基线口令同一条动作序列。
DEFAULT_SEQUENCE = (
    (FlightControlAction.CONTROL_ACQUIRE, {}),
    (FlightControlAction.MOVE_FORWARD, {"distance_m": 10}),
    (FlightControlAction.CAMERA_PHOTO, {}),
    (FlightControlAction.READ_STATE, {}),
)


def main() -> int:
    parser = argparse.ArgumentParser(description="导出驾驶舱报文夹具")
    parser.add_argument("path", nargs="?", default="fixtures/cockpit_basic.json")
    parser.add_argument("--execution-mode", default="recorded",
                        help="夹具声明的执行模式（不能含安全边界禁用词）")
    parser.add_argument("--drone-id", default="drone-demo")
    args = parser.parse_args()

    binding = RuntimeBinding("actor-demo", "project-demo", "hangar-01",
                             args.drone_id, "batch-01", 1)
    fixture = export_fixture(binding, DEFAULT_SEQUENCE, execution_mode=args.execution_mode)

    folder = os.path.dirname(os.path.abspath(args.path))
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(args.path, "w", encoding="utf-8") as handle:
        json.dump(fixture, handle, ensure_ascii=False, indent=2, sort_keys=False)
        handle.write("\n")
    print(f"已写出 {args.path}：source={fixture['source']} "
          f"execution_mode={fixture['execution_mode']} recordings={len(fixture['recordings'])}")
    print("注意：这是 simulator-export，不是真机录制。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
