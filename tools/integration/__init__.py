"""契约 mock 套件（离线联调前置）。

这些模块只复刻“平台应该返回什么形状”，不连接真实 Java/围栏/MQTT/设备，
也不授予任何真实控制权。权威快照、围栏、控制权、挂载与算法生命周期四组 mock
被 ``platform_conformance_smoke.py`` 串起来，逐项断言现有运行时读路径的
fail-closed 行为。
"""
