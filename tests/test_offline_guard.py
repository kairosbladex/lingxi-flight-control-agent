"""守护测试：整套离线单元测试不得访问网络。

真实模型联调（``tools/flight_agent_demo.py``、``tools/bench/*``）刻意不在
``unittest discover`` 的范围内；一旦某个单元测试偷偷发起真实连接，CI 就会依赖
内网模型服务器是否可达，变成「看运气」的失败。

这里把 socket 的两个连接入口换成抛错，跑一遍完整的 discover，断言整轮里
没有任何连接尝试。它同时挡住两类回归：新测试引入真实网络调用，以及被测代码
在默认构造路径上偷偷连接服务端。
"""

from __future__ import annotations

import io
import os
import socket
import unittest


_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SELF_PREFIX = os.path.basename(__file__)[: -len(".py")] + "."


def _flatten(suite: unittest.TestSuite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


def _suite_without_self() -> unittest.TestSuite:
    """除本模块外的全部离线测试；排除自己以免无限递归。"""
    discovered = unittest.TestLoader().discover(_TESTS_DIR, pattern="test_*.py")
    suite = unittest.TestSuite()
    for test in _flatten(discovered):
        if not test.id().startswith(_SELF_PREFIX):
            suite.addTest(test)
    return suite


class OfflineSuiteGuardTest(unittest.TestCase):
    def test_discovered_suite_makes_no_network_connection(self) -> None:
        attempts: list[str] = []

        def blocked_connect(self, address, *args, **kwargs):  # noqa: ANN001 - 与被替换的签名一致
            attempts.append(repr(address))
            raise AssertionError(f"offline unit tests must not connect to {address!r}")

        def blocked_create_connection(address, *args, **kwargs):  # noqa: ANN001
            attempts.append(repr(address))
            raise AssertionError(f"offline unit tests must not connect to {address!r}")

        original_connect = socket.socket.connect
        original_create = socket.create_connection
        socket.socket.connect = blocked_connect
        socket.create_connection = blocked_create_connection
        stream = io.StringIO()
        try:
            result = unittest.TextTestRunner(stream=stream, verbosity=0).run(_suite_without_self())
        finally:
            socket.socket.connect = original_connect
            socket.create_connection = original_create

        self.assertEqual(attempts, [], f"offline tests attempted network access: {attempts}")
        self.assertTrue(result.wasSuccessful(), stream.getvalue()[-4000:])


if __name__ == "__main__":  # pragma: no cover - 手工运行单个守护
    unittest.main()
