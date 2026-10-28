#!/usr/bin/env bash
# 本地与 CI 共用的全量离线测试入口（唯一真源）。
#
# 等价于验证清单第 1 条：
#   PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'
#
# 测试必须完全离线：真实模型联调在 tools/flight_agent_demo.py 与 tools/bench/*，
# 不属于 discover 范围；tests/test_offline_guard.py 会在跑完整套用例时断言
# 没有任何 socket 连接尝试。
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

echo "+ PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'"
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'
