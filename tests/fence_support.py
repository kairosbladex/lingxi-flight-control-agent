"""测试与模拟器用的围栏 provider。

这些实现只做契约与状态机验证：不访问任何 GIS、电子围栏或设备接口，
返回的 ``clear`` 也不代表真实路径安全。它们同步返回、不重试、不起线程。

分层约定：

- :class:`StaticFenceProvider` / :class:`BlockingFenceProvider` 只声明 ``deadline_sec``，
  属于 **cooperative**（协作式），只能用于模拟器与离线测试；
- :class:`HardBoundedFenceProvider` / :class:`TimeoutKwargFenceProvider` 显式接收
  ``timeout_sec``，属于 **hard bounded** 形状，可用于平台执行器测试。
"""

import time

from lingxi_flight_control.flight_policy import PathCheck

#: 测试用默认预算，落在 flight_policy 允许的 0.1–10 秒内。
DEFAULT_DEADLINE_SEC = 1.0


class StaticFenceProvider:
    """协作式 provider：声明 ``deadline_sec``，同步返回预设结论。

    没有底层硬超时传输，因此**不能**装配到平台执行器；只允许模拟器与离线测试。
    ``result`` 可以是 ``PathCheck``，也可以是返回任意值的可调用对象
    （用来构造 malformed / 抛异常 / 超时用例）。
    """

    def __init__(self, result=None, *, deadline_sec=DEFAULT_DEADLINE_SEC):
        self.result = result
        self.deadline_sec = deadline_sec
        self.calls = []

    def __call__(self, binding, action, parameters, state):
        self.calls.append(action)
        return self.result() if callable(self.result) else self.result


class TimeoutKwargFenceProvider:
    """用 ``check_path(..., timeout_sec=)`` 声明硬超时契约的 provider 形状。"""

    def __init__(self, result=None):
        self.result = result if result is not None else PathCheck('clear')
        self.timeouts = []
        self.calls = []

    def check_path(self, binding, action, parameters, state, *, timeout_sec=None):
        self.calls.append(action)
        self.timeouts.append(timeout_sec)
        return self.result


class HardBoundedFenceProvider:
    """硬超时形状的测试 provider：记录收到的预算，只回预设结论。

    接受 ``timeout_sec`` 只是契约声明；真正结束等待仍由底层 transport 负责。
    """

    def __init__(self, result=None, *, deadline_sec=DEFAULT_DEADLINE_SEC):
        self.result = result if result is not None else PathCheck('clear')
        self.deadline_sec = deadline_sec
        self.timeouts = []
        self.calls = []

    def check_path(self, binding, action, parameters, state, *, timeout_sec=None):
        self.calls.append(action)
        self.timeouts.append(timeout_sec)
        return self.result() if callable(self.result) else self.result


class BlockingFenceProvider:
    """永不及时返回的普通同步 callable。

    用来证明协作式/无界 provider 不会进入平台执行路径：若被调用，``calls`` 会
    增加且耗时接近 ``block_sec``；平台路径必须既不调用它，也不等它返回。
    """

    deadline_sec = 1.0

    def __init__(self, block_sec=2.0):
        self.block_sec = float(block_sec)
        self.calls = 0

    def __call__(self, binding, action, parameters, state):
        self.calls += 1
        time.sleep(self.block_sec)
        return PathCheck('clear')
