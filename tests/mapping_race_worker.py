"""跨进程测绘 reservation 竞争的测试工作进程。

由 ``test_mapping_interruption.CrossProcessReservationTests`` 以独立 Python 进程
拉起。两个进程共享同一个 SQLite journal 和同一组「调用记录文件」，用来证明：

- 同一 command_id/idempotency_key/context/binding 的测绘网关在跨进程并发下至多
  被调用一次；
- 平台 submit 至多一次；
- 网关成功或结果未知后进程崩溃，恢复时都不会再次调用网关。

本文件不是测试用例，不被 ``unittest discover -p 'test_*.py'`` 收集。

关键点：``uuid4`` 被替换成确定性序列，让两个进程各自 ``preview`` 出**同一张**
确认卡（相同 confirmation_id / trace_id / confirmation_binding），这样才是真正的
「两个进程同时 confirm 同一个确认卡」，而不是两张内容相近的卡。
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import lingxi_flight_control.ktd_platform as ktd  # noqa: E402
from lingxi_flight_control import FlightControlActionRequest, RuntimeBinding  # noqa: E402
from lingxi_flight_control.flight_policy import PathCheck  # noqa: E402
from lingxi_flight_control.ktd_platform import (  # noqa: E402
    KtdHttpResponse, KtdPlatformExecutor, KtdSnapshot, SqliteCommandJournal,
)
from lingxi_flight_control.mapping_interruption import (  # noqa: E402
    INTERRUPTION_STEPS, MappingInterruptionReceipt,
)

NOW = 1000.0
BINDING = RuntimeBinding('actor-1', 'project-1', 'hangar-1', 'drone-1', 'batch-1', 1)


def snapshot() -> KtdSnapshot:
    return KtdSnapshot(
        actor_id='actor-1', project_id='project-1', hangar_id='hangar-1', drone_id='drone-1',
        site_id='site-1', task_id='java-task-1', context_version=1, version=7,
        observed_at=NOW, control_owner='actor-1', profile='DJI_HANGAR',
        control_lease_expires_at=NOW + 100000,
        permissions=frozenset({'move_forward', 'read_state'}),
        capabilities={'move_forward': True, 'UAV_MOVE_DISTANCE': True},
        state={'online': True, 'airborne': True, 'longitude': 113.0, 'latitude': 23.0,
               'altitude_m': 100.0, 'height_m': 50.0, 'yaw_deg': 90.0,
               'task_type': 'mapping', 'route_status': 'running', 'route_id': 'route-main',
               'auto_stitching': True, 'breakpoint_index': 7},
    )


class HardBoundedProvider:
    """硬超时形状的围栏 provider；只回 clear，不访问任何外部服务。"""

    deadline_sec = 1.0

    def check_path(self, binding, action, parameters, state, *, timeout_sec=None):
        return PathCheck('clear')


class Calls:
    """调用记录：O_APPEND 单行写入，跨进程天然互不覆盖。"""

    def __init__(self, path: str):
        self.path = path

    def add(self, name: str) -> None:
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, (name + "\n").encode())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class RecordingGateway:
    execution_mode = 'file-recording'
    supports_idempotency_key = True

    def __init__(self, calls: Calls, mode: str):
        self.calls = calls
        self.mode = mode
        self.requests = []

    def interrupt(self, request):
        self.calls.add('gateway')
        self.requests.append(request)
        if self.mode == 'crash_in_gateway':
            # 网关已经开始执行但结果不可知：进程直接消失，不回任何回执。
            os._exit(9)
        if self.mode == 'raise':
            raise RuntimeError('gateway transport reset')
        return MappingInterruptionReceipt(
            atomic=True, execution_mode=self.execution_mode, confirmed=True,
            completed_steps=INTERRUPTION_STEPS, audit_id='worker-1')


class RecordingTransport:
    def __init__(self, calls: Calls):
        self.calls = calls

    def submit(self, command, context):
        self.calls.add('submit')
        return KtdHttpResponse(200, {'code': 200})


def deterministic_uuid4():
    counter = itertools.count(1)
    ktd.uuid4 = lambda: uuid.UUID(int=next(counter))


def wait_until(start_at: float) -> None:
    """起跑栅栏：只同步开始时刻，不做任何业务重试。"""
    while time.time() < start_at:
        time.sleep(0.001)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--journal', required=True)
    parser.add_argument('--calls', required=True)
    parser.add_argument('--mode', default='normal',
                        choices=('normal', 'crash_in_gateway', 'crash_after_success', 'raise'))
    parser.add_argument('--key', default='key-1')
    parser.add_argument('--command', default='command-1')
    parser.add_argument('--distance', type=float, default=10.0)
    parser.add_argument('--start-at', type=float, default=0.0)
    parser.add_argument('--result', required=True)
    options = parser.parse_args()

    deterministic_uuid4()
    calls = Calls(options.calls)
    gateway = RecordingGateway(calls, options.mode)
    journal = SqliteCommandJournal(options.journal)
    if options.mode == 'crash_after_success':
        # 网关已成功、succeeded 已落库，但命令账本还没写：模拟此时进程消失。
        journal.claim = lambda *args, **kwargs: os._exit(9)
    executor = KtdPlatformExecutor(
        BINDING, site_id='site-1', task_id='java-task-1',
        transport=RecordingTransport(calls), snapshot_provider=snapshot,
        journal=journal, enabled=True, clock=lambda: NOW,
        path_checker=HardBoundedProvider(), mapping_gateway=gateway)

    request = FlightControlActionRequest(options.command, options.key, 'move_forward',
                                         {'distance_m': options.distance}, 1)
    card = executor.preview(request)
    result = {'stage': 'preview', 'status': card.to_dict()['status'],
              'error_code': card.error_code, 'confirmation_id': card.confirmation_id,
              'confirmation_binding': card.confirmation_binding}
    if card.status.value != 'preview_required':
        Path(options.result).write_text(json.dumps(result), encoding='utf-8')
        return 0

    wait_until(options.start_at)
    receipt = executor.confirm(card.confirmation_id, command_id=options.command,
                               idempotency_key=options.key,
                               confirmation_binding=card.confirmation_binding,
                               context_version=1)
    reservation = journal.read_mapping(executor._scope(options.key))
    recorded = (Path(options.calls).read_text().splitlines()
                if Path(options.calls).exists() else [])
    result.update({
        'stage': 'confirm',
        'status': receipt.to_dict()['status'],
        'error_code': receipt.error_code,
        'message': receipt.message,
        'reservation_state': reservation[1].get('state') if reservation else None,
        'gateway_calls': recorded.count('gateway'),
        'submit_calls': recorded.count('submit'),
    })
    Path(options.result).write_text(json.dumps(result), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
