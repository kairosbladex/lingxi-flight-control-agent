"""挂载能力与算法启停事件 mock。

两部分：

1. :class:`MountedCapabilityMock` 提供设备已挂载清单
   （``mounted_lenses`` / ``mounted_payloads``）和项目已配置算法；
   缺失清单保持 ``None``（“未核验”），不能默认成“未挂载”或“已挂载”。
2. :class:`AlgorithmLifecycleEventMock` 实现 ``AlgorithmLifecycleGateway``
   协议，记录 ``start`` / ``stop`` 事件并返回带阶段与确认标志的回执。
   它只证明“模拟器收到了启停事件”，不证明真实平台算法在运行。

挂载清单的消费方是 ``flight_policy.payload_mount_message``；算法事件的消费方是
``MemoryFlightControlSimulator.apply_task_event`` 的标注到达/离开归约。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from lingxi_flight_control.ai_lifecycle import (
    AlgorithmLifecycleReceipt, AlgorithmStage,
)

EXECUTION_MODE = 'integration-mock'


class MountedCapabilityMock:
    """可注入模拟器状态的挂载/算法配置夹具。"""

    def __init__(self, *, lenses: Sequence[str] | None = None,
                 payloads: Sequence[str] | None = None,
                 configured_algorithms: Sequence[str] = ()):
        self.lenses = None if lenses is None else list(lenses)
        self.payloads = None if payloads is None else list(payloads)
        self.configured_algorithms = list(configured_algorithms)

    def state_fields(self) -> dict[str, Any]:
        return {
            'mounted_lenses': None if self.lenses is None else list(self.lenses),
            'mounted_payloads': None if self.payloads is None else list(self.payloads),
            'configured_algorithms': list(self.configured_algorithms),
        }

    def apply(self, simulator, *, annotation_algorithms: Mapping[str, Sequence[str]] | None = None,
              active_annotations: Sequence[str] = ()) -> None:
        """把夹具写进模拟器状态；只改只读事实，不触发算法启停。"""
        state = simulator.state
        state.mounted_lenses = None if self.lenses is None else list(self.lenses)
        state.mounted_payloads = None if self.payloads is None else list(self.payloads)
        state.configured_algorithms = list(self.configured_algorithms)
        state.annotation_algorithms = {
            key: list(value) for key, value in (annotation_algorithms or {}).items()
        }
        state.active_annotations = list(active_annotations)


class AlgorithmLifecycleEventMock:
    """算法启停网关 mock；回执 ``execution_mode=integration-mock``。"""

    execution_mode = EXECUTION_MODE
    supports_real_platform = False

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self._counter = 0

    def start(self, binding, annotation_id: str, algorithms: Sequence[str], *,
              request_id: str = '') -> AlgorithmLifecycleReceipt:
        return self._receipt(AlgorithmStage.STARTED, annotation_id, algorithms, request_id)

    def stop(self, binding, annotation_id: str, algorithms: Sequence[str], *,
             request_id: str = '') -> AlgorithmLifecycleReceipt:
        return self._receipt(AlgorithmStage.STOPPED, annotation_id, algorithms, request_id)

    def _receipt(self, stage: AlgorithmStage, annotation_id: str,
                 algorithms: Sequence[str], request_id: str) -> AlgorithmLifecycleReceipt:
        ordered = tuple(dict.fromkeys(str(item) for item in algorithms))
        self._counter += 1
        receipt_id = f'mock-{stage.value}-{self._counter}'
        event = {
            'stage': stage.value,
            'annotation_id': annotation_id,
            'algorithms': list(ordered),
            'request_id': request_id,
            'execution_mode': self.execution_mode,
            'confirmed': True,
            'platform_receipt_id': receipt_id,
        }
        self.events.append(event)
        return AlgorithmLifecycleReceipt(
            stage=stage, annotation_id=annotation_id, algorithms=ordered,
            execution_mode=self.execution_mode, confirmed=True, request_id=request_id,
            platform_receipt_id=receipt_id,
            message='mock 已记录算法事件；不代表真实平台运行状态。')

    def snapshot(self) -> dict[str, Any]:
        return {
            'execution_mode': self.execution_mode,
            'events': list(self.events),
            'starts': [event for event in self.events if event['stage'] == 'started'],
            'stops': [event for event in self.events if event['stage'] == 'stopped'],
        }
