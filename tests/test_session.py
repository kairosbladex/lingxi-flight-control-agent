"""会话检查点持久化与会话运行器的离线测试。

本模块**不得**打开任何 socket：全部断言都直接调纯函数/纯方法，
HTTP 适配层由 ``tools/bench/service_e2e.py`` 覆盖（不在 discover 范围内）。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from lingxi_flight_control import (
    BoundedAgentLoop,
    DecisionKind,
    LoopStatus,
    MemoryFlightControlSimulator,
    ModelDecision,
    RuntimeBinding,
    ToolCall,
    build_full_control_tool_registry,
)
from lingxi_flight_control.session import (
    DEFAULT_TTL_SEC,
    MAX_REQUEST_LOG,
    RESPONSE_KEYS,
    SESSION_SCHEMA_VERSION,
    FileCheckpointStore,
    MemoryCheckpointStore,
    SessionError,
    SessionExpired,
    SessionNotFound,
    SessionRecord,
    SessionRunner,
    SessionTampered,
    validate_session_id,
)

ACQUIRE = "control_acquire"
MOVE_FORWARD = "move_forward"
CAMERA_PHOTO = "camera_photo"
COMMAND = "接管控制权，然后向前飞10米，再拍张照片"
TWO_STEP_COMMAND = "接管控制权并向前飞10米"


def binding(version: int = 1, drone: str = "drone-1") -> RuntimeBinding:
    return RuntimeBinding("actor-1", "project-1", "hangar-1", drone, "batch-1", version)


def call(name: str, **arguments) -> ModelDecision:
    return ModelDecision(DecisionKind.TOOL_CALL, tool_call=ToolCall(name, arguments))


def final(message: str = "完成") -> ModelDecision:
    return ModelDecision(DecisionKind.FINAL, message=message)


class ScriptedDecider:
    """与 test_full_control_registry 同构的脚本决策器（待确认调用会被重发）。"""

    def __init__(self, decisions):
        self._decisions = list(decisions)
        self._pending = None
        self._pending_at = None

    def decide(self, _user_message, checkpoint, _tools):
        observed = len(checkpoint.observations)
        if self._pending is not None and observed == self._pending_at:
            return self._pending
        self._pending = None
        if not self._decisions:
            return final()
        decision = self._decisions.pop(0)
        if decision.tool_call is not None:
            self._pending = decision
            self._pending_at = observed
        return decision


def drive(loop, command: str, *, max_rounds: int = 12):
    """在同一个进程里连续放行确认，直到循环不再要求澄清。"""
    checkpoint = None
    result = None
    for _ in range(max_rounds):
        result = loop.run(command, checkpoint=checkpoint, confirmed=checkpoint is not None)
        checkpoint = result.checkpoint
        if result.status is not LoopStatus.CLARIFICATION:
            return result
    return result


def cockpit_script():
    return [call(ACQUIRE), call(MOVE_FORWARD, distance_m=10), call(CAMERA_PHOTO), final("已完成")]


def armed_checkpoint(*, drone: str = "drone-1"):
    """返回一个「已执行 acquire、正等待确认 move_forward」的检查点。"""
    simulator = MemoryFlightControlSimulator(binding(drone=drone))
    loop = BoundedAgentLoop(
        ScriptedDecider([call(ACQUIRE), call(MOVE_FORWARD, distance_m=10)]),
        build_full_control_tool_registry(simulator))
    first = loop.run(TWO_STEP_COMMAND)
    assert first.status is LoopStatus.CLARIFICATION
    second = loop.run(TWO_STEP_COMMAND, checkpoint=first.checkpoint, confirmed=True)
    assert second.status is LoopStatus.CLARIFICATION
    return second.checkpoint


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def store_factories(tmpdir: str, *, clock=None, ttl_sec: float = DEFAULT_TTL_SEC):
    """内存与文件两个实现跑同一组语义断言。"""
    return (
        ("memory", MemoryCheckpointStore(clock=clock, ttl_sec=ttl_sec)),
        ("file", FileCheckpointStore(tmpdir, clock=clock, ttl_sec=ttl_sec)),
    )


class SessionRecordTests(unittest.TestCase):
    def test_json_round_trip_preserves_everything(self):
        checkpoint = armed_checkpoint()
        record = SessionRecord(
            session_id="s-1", checkpoint=checkpoint, created_at=1.0, updated_at=2.0,
            expires_at=3.0, revision=7,
            request_log=({"request_id": "r1", "response": {"status": "clarification"}},))
        restored = SessionRecord.from_dict(json.loads(json.dumps(record.to_dict())))
        self.assertEqual(restored, record)
        self.assertEqual(restored.checkpoint.to_dict(), checkpoint.to_dict())
        self.assertEqual(restored.checkpoint.confirmation_binding,
                         checkpoint.confirmation_binding)
        self.assertEqual(restored.checkpoint.runtime_context, checkpoint.runtime_context)

    def test_wrong_or_missing_schema_version_is_tampered(self):
        payload = SessionRecord(
            session_id="s-1", checkpoint=armed_checkpoint(), created_at=1.0,
            updated_at=2.0, expires_at=3.0, revision=1).to_dict()
        for bad in (SESSION_SCHEMA_VERSION + 1, None, "1", 0):
            with self.subTest(schema_version=bad):
                payload["schema_version"] = bad
                with self.assertRaises(SessionTampered):
                    SessionRecord.from_dict(payload)

    def test_missing_schema_version_key_is_tampered(self):
        payload = SessionRecord(
            session_id="s-1", checkpoint=armed_checkpoint(), created_at=1.0,
            updated_at=2.0, expires_at=3.0, revision=1).to_dict()
        payload.pop("schema_version")
        with self.assertRaises(SessionTampered):
            SessionRecord.from_dict(payload)

    def test_invalid_runtime_context_is_tampered(self):
        base = SessionRecord(
            session_id="s-1", checkpoint=armed_checkpoint(), created_at=1.0,
            updated_at=2.0, expires_at=3.0, revision=1).to_dict()
        cases = [
            {"drone_id": "drone-1"},                        # 缺字段
            "not-an-object",                                # 不是映射
            {"execution_id": "e", "drone_id": "d", "context_version": 1, "confirmed": False},
            {"execution_id": "e", "drone_id": "", "context_version": 1, "confirmed": True},
            {"execution_id": "e", "drone_id": "d", "context_version": -1, "confirmed": True},
        ]
        for runtime_context in cases:
            with self.subTest(runtime_context=runtime_context):
                payload = json.loads(json.dumps(base))
                payload["checkpoint"]["runtime_context"] = runtime_context
                with self.assertRaises(SessionTampered):
                    SessionRecord.from_dict(payload)

    def test_missing_checkpoint_fields_are_tampered(self):
        base = SessionRecord(
            session_id="s-1", checkpoint=armed_checkpoint(), created_at=1.0,
            updated_at=2.0, expires_at=3.0, revision=1).to_dict()
        for mutate in ("drop_user_input", "drop_status", "bad_revision", "bad_log"):
            with self.subTest(mutate=mutate):
                payload = json.loads(json.dumps(base))
                if mutate == "drop_user_input":
                    payload["checkpoint"].pop("user_input")
                elif mutate == "drop_status":
                    payload["checkpoint"]["status"] = "no-such-status"
                elif mutate == "bad_revision":
                    payload["revision"] = -1
                else:
                    payload["request_log"] = [{"request_id": "r1", "response": {}, "extra": 1}]
                with self.assertRaises(SessionTampered):
                    SessionRecord.from_dict(payload)

    def test_response_for_returns_latest_response(self):
        record = SessionRecord(
            session_id="s-1", checkpoint=armed_checkpoint(), created_at=1.0, updated_at=2.0,
            expires_at=3.0, revision=2,
            request_log=({"request_id": "r1", "response": {"status": "clarification"}},
                         {"request_id": "r1", "response": {"status": "final"}},
                         {"request_id": "r2", "response": None}))
        self.assertEqual(record.response_for("r1"), {"status": "final"})
        self.assertIsNone(record.response_for("r2"))
        self.assertIsNone(record.response_for("nope"))
        self.assertIsNone(record.response_for(None))


class StoreSemanticsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = self._tmp.name

    def test_save_load_round_trip_preserves_checkpoint(self):
        checkpoint = armed_checkpoint()
        for name, store in store_factories(self.tmpdir):
            with self.subTest(store=name):
                record = store.save("s-1", checkpoint, request={"request_id": "r1"},
                                    response={"status": "clarification"})
                self.assertEqual(record.revision, 1)
                self.assertEqual(record.schema_version, SESSION_SCHEMA_VERSION)
                self.assertEqual(record.expires_at, record.updated_at + DEFAULT_TTL_SEC)
                loaded = store.load("s-1")
                self.assertEqual(loaded.checkpoint.to_dict(), checkpoint.to_dict())
                # 关键字段逐项对照：运行时绑定、确认绑定、已执行调用、观察结果。
                self.assertEqual(loaded.checkpoint.runtime_context["drone_id"], "drone-1")
                self.assertEqual(loaded.checkpoint.confirmation_binding,
                                 checkpoint.confirmation_binding)
                self.assertIsNotNone(loaded.checkpoint.confirmation_binding)
                self.assertEqual([c["name"] for c in loaded.checkpoint.executed_tool_calls],
                                 [ACQUIRE])
                self.assertEqual(len(loaded.checkpoint.observations), 1)
                self.assertEqual(loaded.request_log,
                                 ({"request_id": "r1",
                                   "response": {"status": "clarification"}},))
                self.assertEqual(store.list_sessions(), ("s-1",))

    def test_store_does_not_alias_the_caller_checkpoint(self):
        for name, store in store_factories(self.tmpdir):
            with self.subTest(store=name):
                checkpoint = armed_checkpoint()
                original_step_count = checkpoint.step_count
                store.save("s-alias", checkpoint)
                checkpoint.step_count += 5          # 循环继续推进同一个对象
                self.assertEqual(store.load("s-alias").checkpoint.step_count,
                                 original_step_count)
                loaded = store.load("s-alias")
                loaded.checkpoint.step_count = 99   # 调用方改读出来的副本
                self.assertEqual(store.load("s-alias").checkpoint.step_count,
                                 original_step_count)

    def test_save_is_upsert_and_revision_increments(self):
        clock = FakeClock()
        checkpoint = armed_checkpoint()
        for name, store in store_factories(self.tmpdir, clock=clock):
            with self.subTest(store=name):
                first = store.save("s-1", checkpoint, request={"request_id": "r1"},
                                   response={"n": 1})
                clock.advance(5)
                second = store.save("s-1", checkpoint, request={"request_id": "r2"},
                                    response={"n": 2})
                self.assertEqual(first.revision, 1)
                self.assertEqual(second.revision, 2)
                self.assertEqual(second.created_at, first.created_at)   # created_at 保持
                self.assertGreater(second.updated_at, first.updated_at)
                self.assertEqual([e["request_id"] for e in second.request_log], ["r1", "r2"])
                self.assertEqual(store.list_sessions(), ("s-1",))

    def test_request_log_is_bounded_and_keeps_the_last_entries(self):
        checkpoint = armed_checkpoint()
        for name, store in store_factories(self.tmpdir):
            with self.subTest(store=name):
                for index in range(MAX_REQUEST_LOG + 10):
                    record = store.save("s-bound", checkpoint,
                                        request={"request_id": f"r{index}"},
                                        response={"n": index})
                self.assertEqual(len(record.request_log), MAX_REQUEST_LOG)
                self.assertEqual(record.request_log[0]["request_id"], "r10")
                self.assertEqual(record.request_log[-1]["request_id"],
                                 f"r{MAX_REQUEST_LOG + 9}")

    def test_ttl_expiry_uses_the_injected_clock(self):
        clock = FakeClock()
        checkpoint = armed_checkpoint()
        for name, store in store_factories(self.tmpdir, clock=clock, ttl_sec=10.0):
            with self.subTest(store=name):
                store.save("s-ttl", checkpoint)
                clock.advance(9.999)
                self.assertIsNotNone(store.load("s-ttl"))
                clock.advance(0.001)                     # 恰好到 expires_at
                with self.assertRaises(SessionExpired):
                    store.load("s-ttl")
                # 过期不自动删除：list_sessions 仍看得到，删除由 runner/服务层显式做。
                self.assertEqual(store.list_sessions(), ("s-ttl",))
                store.delete("s-ttl")
                self.assertEqual(store.list_sessions(), ())

    def test_ttl_override_on_save(self):
        clock = FakeClock()
        checkpoint = armed_checkpoint()
        for name, store in store_factories(self.tmpdir, clock=clock, ttl_sec=1800.0):
            with self.subTest(store=name):
                record = store.save("s-ttl-override", checkpoint, ttl_sec=5.0)
                self.assertEqual(record.expires_at, clock.now + 5.0)
                clock.advance(6)
                with self.assertRaises(SessionExpired):
                    store.load("s-ttl-override")

    def test_missing_session_raises_not_found_and_delete_is_idempotent(self):
        for name, store in store_factories(self.tmpdir):
            with self.subTest(store=name):
                with self.assertRaises(SessionNotFound):
                    store.load("nope")
                store.delete("nope")                     # 不存在时静默
                self.assertEqual(store.list_sessions(), ())

    def test_delete_removes_the_session(self):
        checkpoint = armed_checkpoint()
        for name, store in store_factories(self.tmpdir):
            with self.subTest(store=name):
                store.save("s-del", checkpoint)
                store.delete("s-del")
                with self.assertRaises(SessionNotFound):
                    store.load("s-del")
                self.assertEqual(store.list_sessions(), ())

    def test_invalid_session_ids_are_rejected(self):
        checkpoint = armed_checkpoint()
        bad_ids = ["../../etc/passwd", "a/b", "/etc/passwd", "with space", "x" * 65,
                   "", None, 5, "a\\b", "s/../1"]
        for name, store in store_factories(self.tmpdir):
            for session_id in bad_ids:
                with self.subTest(store=name, session_id=session_id):
                    with self.assertRaises(SessionError):
                        store.save(session_id, checkpoint)
                    with self.assertRaises(SessionError):
                        store.load(session_id)
                    with self.assertRaises(SessionError):
                        store.delete(session_id)

    def test_validate_session_id_accepts_strict_tokens(self):
        for session_id in ("s1", "S-1.a_b", "x" * 64):
            self.assertEqual(validate_session_id(session_id), session_id)
        for session_id in ("../../etc/passwd", "a/b", "x" * 65, ""):
            with self.assertRaises(SessionError):
                validate_session_id(session_id)

    def test_file_store_writes_exactly_one_file_per_session(self):
        checkpoint = armed_checkpoint()
        store = FileCheckpointStore(self.tmpdir)
        store.save("s-file", checkpoint, request={"request_id": "r1"},
                   response={"status": "clarification"})
        store.save("s-file", checkpoint)
        names = sorted(os.listdir(self.tmpdir))
        self.assertEqual(names, ["s-file.json"])
        with open(os.path.join(self.tmpdir, "s-file.json"), encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["schema_version"], SESSION_SCHEMA_VERSION)
        self.assertEqual(payload["revision"], 2)
        self.assertEqual(payload["checkpoint"]["runtime_context"]["drone_id"], "drone-1")

    def test_file_store_rejects_path_traversal_without_touching_the_outside(self):
        store = FileCheckpointStore(self.tmpdir)
        escaped = os.path.abspath(os.path.join(self.tmpdir, "..", "etc", "passwd.json"))
        with self.assertRaises(SessionError):
            store.path_for("../../etc/passwd")
        with self.assertRaises(SessionError):
            store.path_for("a/b")
        self.assertFalse(os.path.exists(escaped))
        self.assertEqual(os.listdir(self.tmpdir), [])

    def test_unparseable_file_is_tampered(self):
        store = FileCheckpointStore(self.tmpdir)
        with open(os.path.join(self.tmpdir, "s-broken.json"), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        with self.assertRaises(SessionTampered):
            store.load("s-broken")
        with self.assertRaises(SessionTampered):
            store.save("s-broken", armed_checkpoint())   # 坏文件不被静默覆盖

    def test_file_with_wrong_schema_version_is_tampered(self):
        store = FileCheckpointStore(self.tmpdir)
        store.save("s-version", armed_checkpoint())
        path = os.path.join(self.tmpdir, "s-version.json")
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        payload["schema_version"] = 2
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        with self.assertRaises(SessionTampered):
            store.load("s-version")

    def test_file_with_mismatched_session_id_is_tampered(self):
        store = FileCheckpointStore(self.tmpdir)
        store.save("s-real", armed_checkpoint())
        os.rename(os.path.join(self.tmpdir, "s-real.json"),
                  os.path.join(self.tmpdir, "s-other.json"))
        with self.assertRaises(SessionTampered):
            store.load("s-other")

    def test_bad_request_ids_and_responses_are_rejected(self):
        store = MemoryCheckpointStore()
        checkpoint = armed_checkpoint()
        with self.assertRaises(SessionError):
            store.save("s-1", checkpoint, request={})
        with self.assertRaises(SessionError):
            store.save("s-1", checkpoint, request={"request_id": ""})
        with self.assertRaises(SessionError):
            store.save("s-1", checkpoint, request={"request_id": "r1"}, response="nope")
        with self.assertRaises(SessionError):
            store.save("s-1", "not-a-checkpoint")


class SessionRunnerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = self._tmp.name

    @staticmethod
    def _runner(store, decisions=None, *, max_rounds=12, drone="drone-1"):
        simulator = MemoryFlightControlSimulator(binding(drone=drone))
        loop = BoundedAgentLoop(
            ScriptedDecider(cockpit_script() if decisions is None else decisions),
            build_full_control_tool_registry(simulator))
        return SessionRunner(loop, store, max_rounds=max_rounds), simulator

    def test_response_keys_are_stable(self):
        runner, _ = self._runner(MemoryCheckpointStore())
        response = runner.handle("s-1", text=COMMAND, request_id="r1")
        self.assertEqual(tuple(response), RESPONSE_KEYS)
        self.assertEqual(response["session_id"], "s-1")
        self.assertEqual(response["status"], "clarification")
        self.assertEqual(response["executed"], [])
        self.assertEqual(response["tool_call"],
                         {"name": ACQUIRE, "arguments": {}})
        self.assertIsNone(response["error"])
        self.assertEqual(response["checkpoint_revision"], 1)
        self.assertEqual(response["request_id"], "r1")
        self.assertEqual(response["max_steps"], 8)

    def test_cockpit_baseline_completes_across_two_requests(self):
        for name, store in store_factories(self.tmpdir):
            with self.subTest(store=name):
                runner, simulator = self._runner(store)
                first = runner.handle("s-base", text=COMMAND, request_id="req-1")
                self.assertEqual(first["status"], "clarification")
                self.assertEqual(first["executed"], [])
                self.assertEqual(simulator.state.revision, 0)
                saved = store.load("s-base")
                self.assertIsNotNone(saved.checkpoint.confirmation_binding)

                second = runner.handle("s-base", text=COMMAND, confirmed=True,
                                       request_id="req-2")
                self.assertEqual(second["status"], "final")
                self.assertEqual(second["executed"], [ACQUIRE, MOVE_FORWARD, CAMERA_PHOTO])
                self.assertEqual(simulator.state.control_owner, "actor-1")
                self.assertAlmostEqual(simulator.state.latitude, 0.0000899266130030023)
                self.assertEqual(simulator.state.camera["photo_count"], 1)

    def test_same_request_id_is_replayed_without_running_the_loop(self):
        for name, store in store_factories(self.tmpdir):
            with self.subTest(store=name):
                runner, simulator = self._runner(store)
                first = runner.handle("s-dup", text=COMMAND, confirmed=True,
                                      request_id="req-1")
                self.assertEqual(first["status"], "final")
                revision = simulator.state.revision
                photos = simulator.state.camera["photo_count"]
                second = runner.handle("s-dup", text=COMMAND, confirmed=True,
                                       request_id="req-1")
                self.assertEqual(second, first)
                # 幂等键命中时循环根本没跑：状态版本与拍照数都不动。
                self.assertEqual(simulator.state.revision, revision)
                self.assertEqual(simulator.state.camera["photo_count"], photos)
                self.assertEqual(store.load("s-dup").revision, first["checkpoint_revision"])

    def test_replayed_response_is_a_copy_and_second_save_does_not_grow_the_log(self):
        store = MemoryCheckpointStore()
        runner, _ = self._runner(store)
        first = runner.handle("s-copy", text=COMMAND, confirmed=True, request_id="req-1")
        second = runner.handle("s-copy", text=COMMAND, confirmed=True, request_id="req-1")
        second["executed"].append("tampered")     # 改返回的副本
        third = runner.handle("s-copy", text=COMMAND, confirmed=True, request_id="req-1")
        self.assertEqual(third, first)
        self.assertEqual([e["request_id"] for e in store.load("s-copy").request_log],
                         ["req-1"])

    def test_different_text_starts_a_fresh_task_and_drops_the_old_log(self):
        store = MemoryCheckpointStore()
        runner, _ = self._runner(store)
        runner.handle("s-fresh", text=TWO_STEP_COMMAND, request_id="req-1")
        self.assertEqual(store.load("s-fresh").revision, 1)
        response = runner.handle("s-fresh", text="接管控制权", request_id="req-2")
        record = store.load("s-fresh")
        self.assertEqual(response["checkpoint_revision"], 1)
        self.assertEqual(record.revision, 1)
        self.assertEqual(record.checkpoint.user_input, "接管控制权")
        self.assertEqual([e["request_id"] for e in record.request_log], ["req-2"])

    def test_expired_session_is_dropped_instead_of_resurrected(self):
        clock = FakeClock()
        store = MemoryCheckpointStore(clock=clock, ttl_sec=10.0)
        runner, _ = self._runner(store)
        runner.handle("s-exp", text=COMMAND, request_id="req-1")
        clock.advance(10.0)
        response = runner.handle("s-exp", text=COMMAND, request_id="req-2")
        record = store.load("s-exp")
        self.assertEqual(response["checkpoint_revision"], 1)     # 新会话，不是续版本
        self.assertEqual(record.revision, 1)
        self.assertEqual([e["request_id"] for e in record.request_log], ["req-2"])
        self.assertEqual(record.checkpoint.user_input, COMMAND)

    def test_max_rounds_bounds_one_call_and_next_request_resumes(self):
        store = MemoryCheckpointStore()
        runner, simulator = self._runner(store, max_rounds=1)
        responses = []
        for index in range(1, 5):
            responses.append(runner.handle("s-bounded", text=COMMAND, confirmed=True,
                                           request_id=f"req-{index}"))
        # 每次调用最多推进一轮：前三次都停在澄清上，动作逐个落地。
        self.assertEqual([item["status"] for item in responses],
                         ["clarification", "clarification", "clarification", "final"])
        self.assertEqual([item["executed"] for item in responses],
                         [[], [ACQUIRE], [ACQUIRE, MOVE_FORWARD],
                          [ACQUIRE, MOVE_FORWARD, CAMERA_PHOTO]])
        self.assertEqual(simulator.state.revision, 3)
        with self.assertRaises(ValueError):
            SessionRunner(object(), store, max_rounds=-1)

    def test_tampered_runtime_binding_fails_closed_and_clears_the_binding(self):
        store = FileCheckpointStore(self.tmpdir)
        runner, _ = self._runner(store)
        first = runner.handle("s-tamper", text=COMMAND, request_id="req-1")
        self.assertEqual(first["status"], "clarification")

        path = os.path.join(self.tmpdir, "s-tamper.json")
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        payload["checkpoint"]["runtime_context"]["drone_id"] = "drone-evil"
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)

        # 全新实例、绑定到**原来**的无人机：改写过的运行时绑定必须被判为过期。
        fresh_store = FileCheckpointStore(self.tmpdir)
        fresh_runner, fresh_simulator = self._runner(fresh_store, drone="drone-1")
        result = fresh_runner.handle("s-tamper", text=COMMAND, confirmed=True,
                                     request_id="req-2")
        self.assertEqual(result["status"], "tool_error")
        self.assertEqual(result["error"]["code"], "stale_context")
        self.assertEqual(result["executed"], [])
        self.assertEqual(fresh_simulator.state.revision, 0)
        # 失败也写回：被撤销的确认绑定不能留在存储里“悬着”。
        saved = fresh_store.load("s-tamper")
        self.assertIsNone(saved.checkpoint.confirmation_binding)
        self.assertFalse(saved.checkpoint.confirmed)

    def test_tampered_runtime_binding_is_not_masked_by_confirmed(self):
        store = FileCheckpointStore(self.tmpdir)
        runner, _ = self._runner(store)
        runner.handle("s-tamper2", text=COMMAND, request_id="req-1")
        path = os.path.join(self.tmpdir, "s-tamper2.json")
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        payload["checkpoint"]["runtime_context"]["context_version"] = 99
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        fresh_runner, fresh_simulator = self._runner(FileCheckpointStore(self.tmpdir))
        result = fresh_runner.handle("s-tamper2", text=COMMAND, confirmed=False,
                                     request_id="req-2")
        self.assertEqual(result["status"], "tool_error")
        self.assertEqual(result["error"]["code"], "stale_context")
        self.assertEqual(result["executed"], [])
        self.assertEqual(fresh_simulator.state.revision, 0)

    def test_resume_from_file_store_matches_in_process_run(self):
        decisions = cockpit_script()

        simulator_a = MemoryFlightControlSimulator(binding())
        loop_a = BoundedAgentLoop(ScriptedDecider(decisions),
                                  build_full_control_tool_registry(simulator_a))
        armed = loop_a.run(COMMAND)
        self.assertIs(armed.status, LoopStatus.CLARIFICATION)
        result_a = drive(loop_a, COMMAND)
        self.assertIs(result_a.status, LoopStatus.FINAL)

        store_1 = FileCheckpointStore(self.tmpdir)
        runner_1, _ = self._runner(store_1, decisions)
        first = runner_1.handle("s-resume", text=COMMAND, request_id="req-1")
        self.assertEqual(first["status"], "clarification")

        # 「新进程」：新 store 实例、新 runner、新 decider，只有磁盘上的文件是共享的。
        store_2 = FileCheckpointStore(self.tmpdir)
        runner_2, simulator_b = self._runner(store_2, decisions)
        second = runner_2.handle("s-resume", text=COMMAND, confirmed=True,
                                 request_id="req-2")

        self.assertEqual(second["status"], result_a.status.value)
        self.assertEqual(second["message"], result_a.message)
        self.assertEqual(second["executed"],
                         [c["name"] for c in result_a.checkpoint.executed_tool_calls])
        self.assertEqual(second["executed"], [ACQUIRE, MOVE_FORWARD, CAMERA_PHOTO])
        self.assertEqual(simulator_b.state.latitude, simulator_a.state.latitude)
        self.assertEqual(simulator_b.state.camera["photo_count"],
                         simulator_a.state.camera["photo_count"])
        self.assertEqual(simulator_b.state.revision, simulator_a.state.revision)

    def test_store_is_used_for_every_exit_path(self):
        store = MemoryCheckpointStore()
        runner, _ = self._runner(store, decisions=[call("no_such_tool")])
        response = runner.handle("s-error", text="随便", request_id="req-1")
        self.assertEqual(response["status"], "tool_error")
        self.assertEqual(response["error"]["code"], "tool_error")
        record = store.load("s-error")           # 失败路径同样落盘
        self.assertEqual(record.checkpoint.status, LoopStatus.TOOL_ERROR)
        self.assertEqual(record.revision, response["checkpoint_revision"])

    def test_runner_rejects_bad_construction_arguments(self):
        store = MemoryCheckpointStore()
        with self.assertRaises(ValueError):
            SessionRunner(object(), store, max_rounds=0)
        with self.assertRaises(ValueError):
            SessionRunner(object(), object())
        runner, _ = self._runner(store, max_rounds=3)
        self.assertEqual(runner.max_rounds, 3)
        self.assertIs(runner.store, store)


if __name__ == "__main__":
    unittest.main()
