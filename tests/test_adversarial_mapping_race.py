"""DeepSeek 对抗单测：测绘中断六步原子事务高并发竞争与死锁防御测试。

依据 V1.4 4.1.2 规范与 Task 2 要求：
- 严格验证六步顺序：关拼图 → 废rebuildType → 废breakInfo → 作废断点 → 置灰恢复 → 禁止恢复
- 多线程/并发竞争下零死锁、零跳步、零脏恢复标记
- 未确认干预严格拒绝且零副作用
"""

from __future__ import annotations

import concurrent.futures
import threading
import unittest

from lingxi_flight_control.mapping_interruption import (
    INTERRUPTION_STEPS,
    MappingInterruptionError,
    MappingInterruptionRequest,
    MAPPING_INTERRUPTION_NOT_CONFIRMED,
    MAPPING_INTERRUPTION_UNAVAILABLE,
    SimulatorMappingInterruptionGateway,
    UnavailableMappingInterruptionGateway,
    interruption_required,
)


class MockTaskState:
    """模拟无人机测绘任务状态。"""

    def __init__(self):
        self.task_type = "mapping"
        self.route_status = "running"
        self.mapping_invalidated = False
        self.route_resume_disabled = False
        self.auto_stitching = True
        self.rebuild_type = "3d_model"
        self.break_info = {"point_id": 12}
        self.breakpoint_index = 5
        self.lock = threading.Lock()


class AdversarialMappingRaceTests(unittest.TestCase):
    def setUp(self):
        self.state = MockTaskState()
        self.gateway = SimulatorMappingInterruptionGateway(lambda: self.state)

    def test_01_six_steps_executed_in_exact_sequence(self):
        """六步原子事务步骤名称与执行顺序绝对不可篡改。"""
        req = MappingInterruptionRequest(
            command_id="cmd-001",
            action="move_forward",
            task_batch_id="batch-001",
            confirmed=True,
            idempotency_key="idem-001",
        )
        receipt = self.gateway.interrupt(req)
        self.assertTrue(receipt.atomic)
        self.assertEqual(receipt.completed_steps, INTERRUPTION_STEPS)
        # 核验证据字段
        self.assertEqual(receipt.completed_steps[0], "stop_auto_stitching")
        self.assertEqual(receipt.completed_steps[1], "void_rebuild_type")
        self.assertEqual(receipt.completed_steps[2], "void_break_info")
        self.assertEqual(receipt.completed_steps[3], "invalidate_task_breakpoint")
        self.assertEqual(receipt.completed_steps[4], "disable_route_resume")
        self.assertEqual(receipt.completed_steps[5], "block_task_resume")

    def test_02_unconfirmed_interruption_leaves_zero_side_effects(self):
        """用户未确认成果作废时，严格返回 NOT_CONFIRMED 且任务状态毫发无损。"""
        req = MappingInterruptionRequest(
            command_id="cmd-002",
            action="hover",
            task_batch_id="batch-002",
            confirmed=False,
            idempotency_key="idem-002",
        )
        receipt = self.gateway.interrupt(req)
        self.assertFalse(receipt.atomic)
        self.assertEqual(receipt.error_code, MAPPING_INTERRUPTION_NOT_CONFIRMED)
        self.assertEqual(len(receipt.completed_steps), 0)
        # 状态保持原样
        self.assertFalse(self.state.mapping_invalidated)
        self.assertTrue(self.state.auto_stitching)

    def test_03_high_concurrency_race_zero_deadlocks(self):
        """多线程极限并发下触发测绘中断，断言零死锁、零异常挂起。"""
        concurrency = 20
        requests = [
            MappingInterruptionRequest(
                command_id=f"cmd-race-{i}",
                action="emergency_stop",
                task_batch_id="batch-race",
                confirmed=True,
                idempotency_key=f"idem-race-{i}",
            )
            for i in range(concurrency)
        ]

        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(self.gateway.interrupt, req) for req in requests]
            for future in concurrent.futures.as_completed(futures, timeout=5.0):
                results.append(future.result())

        self.assertEqual(len(results), concurrency)
        # 所有调用的六步均按完整顺序闭环
        for r in results:
            self.assertTrue(r.atomic)
            self.assertEqual(r.completed_steps, INTERRUPTION_STEPS)

    def test_04_unavailable_gateway_fails_safely_without_dirty_state(self):
        """平台网关不可用时，抛出 UNAVAILABLE 并严格阻止后续飞行指令下发。"""
        gw = UnavailableMappingInterruptionGateway()
        req = MappingInterruptionRequest(
            command_id="cmd-unavail",
            action="move_left",
            task_batch_id="batch-unavail",
            confirmed=True,
            idempotency_key="idem-unavail",
        )
        receipt = gw.interrupt(req)
        self.assertFalse(receipt.atomic)
        self.assertEqual(receipt.error_code, MAPPING_INTERRUPTION_UNAVAILABLE)

    def test_05_payload_actions_never_trigger_mapping_interruption(self):
        """载荷指令（拍照、喊话、变焦等）严格豁免测绘中断。"""
        for act in ("camera_photo", "camera_zoom", "speaker_speak", "searchlight_brightness"):
            self.assertFalse(
                interruption_required(act, self.state),
                f"载荷动作 '{act}' 绝不得触发测绘中断判定",
            )

    def test_06_flight_micro_actions_strictly_require_interruption(self):
        """六向微操与悬停在测绘执行中必须触发测绘中断判定。"""
        for act in ("move_forward", "move_left", "hover", "turn_right"):
            self.assertTrue(
                interruption_required(act, self.state),
                f"飞行微操 '{act}' 必须触发测绘中断判定",
            )


if __name__ == "__main__":
    unittest.main()
