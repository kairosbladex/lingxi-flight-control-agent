"""Java 平台候选适配器：仅使用假 HTTP 边界与临时持久日志。"""

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from lingxi_flight_control.full_control import FlightControlActionRequest, FlightControlStatus, RuntimeBinding
from lingxi_flight_control.ktd_platform import (
    KtdCommand, KtdHttpResponse, KtdHttpTransport, KtdPlatformExecutor,
    KtdPlatformError, KtdRequestContext, KtdSnapshot, SqliteCommandJournal,
)


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.response = KtdHttpResponse(200, {"code": 200})
        self.failure = None

    def submit(self, command, context):
        self.calls.append((command, context))
        if self.failure:
            raise self.failure
        return self.response

    def read_task_events(self, binding, **kwargs):
        return KtdHttpResponse(200, {"code": 200, "data": []})


class PlatformExecutorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.binding = RuntimeBinding("actor-1", "project-1", "hangar-1", "drone-1", "batch-1", 1)
        self.now = 1000.0
        self.transport = FakeTransport()
        self.snapshot = KtdSnapshot(
            actor_id="actor-1", project_id="project-1", hangar_id="hangar-1",
            drone_id="drone-1", site_id="site-1", task_id="java-task-1",
            context_version=1, version=7, observed_at=self.now,
            control_owner="actor-1", profile="DJI_HANGAR",
            control_lease_expires_at=self.now + 60,
            permissions=frozenset({"move_forward", "read_state"}),
            capabilities={"move_forward": True, "UAV_MOVE_DISTANCE": True},
            state={"online": True, "airborne": True, "longitude": 113.0,
                   "latitude": 23.0, "altitude_m": 100.0, "height_m": 50.0, "yaw_deg": 90.0,
                   "route_status": "hover"},
        )
        self.journal_path = self.directory.name + "/commands.sqlite3"

    def executor(self, *, enabled=True):
        return KtdPlatformExecutor(
            self.binding, site_id="site-1", task_id="java-task-1",
            transport=self.transport, snapshot_provider=lambda: self.snapshot,
            journal=SqliteCommandJournal(self.journal_path), enabled=enabled,
            clock=lambda: self.now,
        )

    def request(self, action="move_forward", parameters=None, key="request-1"):
        return FlightControlActionRequest("command-1", key, action,
                                          {"distance_m": 10} if parameters is None else parameters, 1)

    def confirm(self, executor, card, **changes):
        arguments = dict(command_id=card.command_id, idempotency_key=card.idempotency_key,
                         confirmation_binding=card.confirmation_binding, context_version=1)
        arguments.update(changes)
        return executor.confirm(card.confirmation_id, **arguments)

    def test_default_off_and_preview_have_zero_network_effects(self):
        executor = KtdPlatformExecutor(
            self.binding, site_id="site-1", task_id="java-task-1", transport=self.transport,
            snapshot_provider=lambda: self.snapshot, journal=SqliteCommandJournal(self.journal_path),
            clock=lambda: self.now,
        )
        self.assertEqual(executor.preview(self.request()).error_code, "PLATFORM_DISABLED")
        card = self.executor().preview(self.request())
        self.assertIs(card.status, FlightControlStatus.PREVIEW_REQUIRED)
        self.assertEqual(self.transport.calls, [])

    def test_platform_executor_reads_typed_event_deltas(self):
        executor = self.executor()
        self.assertEqual(executor.read_task_events(after_sequence=3), ())

    def test_confirm_compiles_distance_and_returns_submission_not_completion(self):
        executor = self.executor()
        receipt = self.confirm(executor, executor.preview(self.request()))
        self.assertEqual(receipt.to_dict()["status"], "submitted")
        self.assertFalse(receipt.to_dict()["device_effect_verified"])
        command, context = self.transport.calls[0]
        self.assertEqual(command.path, "/screen/uav/operation/control")
        self.assertEqual(command.body, {"terminalId": "drone-1", "siteId": "site-1",
                                        "taskId": "java-task-1", "type": 202, "mode": 2, "param": 10})
        self.assertNotEqual(command.body["taskId"], self.binding.task_batch_id)
        self.assertEqual(context.actor_id, "actor-1")
        self.assertEqual(receipt.state["altitude_m"], 100.0)

    def test_timeout_is_unknown_and_survives_restart_without_resend(self):
        self.transport.failure = TimeoutError("sensitive upstream detail")
        executor = self.executor()
        card = executor.preview(self.request())
        first = self.confirm(executor, card)
        self.assertEqual(first.to_dict()["status"], "unknown")
        self.assertNotIn("sensitive", str(first.to_dict()))
        restarted = self.executor()
        self.assertEqual(self.confirm(restarted, card).to_dict()["status"], "unknown")
        self.assertEqual(restarted.preview(self.request()).to_dict()["status"], "unknown")
        self.assertEqual(len(self.transport.calls), 1)

    def test_confirm_rechecks_all_authoritative_bindings_and_permissions(self):
        for field, value in (("actor_id", "other"), ("project_id", "other"),
                             ("hangar_id", "other"), ("drone_id", "other"),
                             ("site_id", "other"), ("task_id", "other"),
                             ("context_version", 2), ("version", 8),
                             ("control_owner", "other"), ("permissions", frozenset()),
                             ("capabilities", {})):
            with self.subTest(field=field):
                original = self.snapshot
                executor = self.executor()
                card = executor.preview(self.request(key=field))
                self.snapshot = replace(original, **{field: value})
                result = self.confirm(executor, card)
                self.assertEqual(result.to_dict()["status"], "rejected")
                self.snapshot = original
        self.assertEqual(self.transport.calls, [])

    def test_distance_bounds_capabilities_and_unsafe_fields_are_rejected(self):
        for params in ({"distance_m": 20.01}, {"distance_m": 0}, {"distance_m": True},
                       {"distance_m": float("nan")}, {"distance_m": 10, "url": "http://evil"},
                       {"distance_m": 10, "actor_id": "other"}, {}):
            with self.subTest(parameters=params):
                self.assertEqual(self.executor().preview(self.request(parameters=params)).to_dict()["status"], "rejected")
        for changes in ({"profile": "D10"}, {"capabilities": {"move_forward": True}},
                        {"capabilities": {"move_forward": True, "UAV_MOVE_DISTANCE": "true"}},
                        {"state": {"online": True}}, {"observed_at": 990.0}):
            with self.subTest(changes=changes):
                original = self.snapshot
                self.snapshot = replace(original, **changes)
                self.assertEqual(self.executor().preview(self.request()).to_dict()["status"], "rejected")
                self.snapshot = original
        self.assertEqual(self.transport.calls, [])

    def test_cancel_only_discards_preview_and_does_not_send_stop(self):
        executor = self.executor()
        card = executor.preview(self.request())
        result = executor.cancel(card.confirmation_id, command_id=card.command_id,
                                 idempotency_key=card.idempotency_key,
                                 confirmation_binding=card.confirmation_binding, context_version=1)
        self.assertEqual(result.to_dict()["status"], "cancelled")
        self.assertEqual(self.confirm(executor, card).to_dict()["status"], "rejected")
        self.assertEqual(self.transport.calls, [])

    def test_all_six_distances_use_metres_and_existing_operation_codes(self):
        for action, expected in (("move_forward", 202), ("move_backward", 203),
                                 ("move_left", 204), ("move_right", 205),
                                 ("move_up", 201), ("move_down", -201)):
            with self.subTest(action=action):
                self.snapshot = replace(self.snapshot, permissions=frozenset({action}),
                                        capabilities={action: True, "UAV_MOVE_DISTANCE": True},
                                        state={**self.snapshot.state, "height_m": 50})
                executor = self.executor()
                result = self.confirm(executor, executor.preview(self.request(action, {"distance_m": 20}, action)))
                self.assertEqual(result.to_dict()["status"], "submitted")
                command = self.transport.calls[-1][0]
                self.assertEqual(command.body["type"], expected)
                self.assertEqual(command.body["mode"], 2)
                self.assertEqual(command.body["param"], 20)

    def test_route_camera_and_gimbal_codes_match_java_contract(self):
        for action, path, code in (
            ("route_pause", "route", 110), ("route_resume", "route", 112),
            ("emergency_stop", "route", 113), ("return_home", "route", 114),
            ("cancel_return_home", "route", -114), ("camera_photo", "camera", 100),
            ("camera_start_recording", "camera", 101), ("camera_stop_recording", "camera", 102),
            ("gimbal_reset", "gimbal", 403),
        ):
            with self.subTest(action=action):
                route_status = ("running" if action == "route_pause"
                                else "paused" if action == "route_resume"
                                else self.snapshot.state.get("route_status"))
                self.snapshot = replace(self.snapshot, permissions=frozenset({action}), capabilities={action: True},
                                        state={**self.snapshot.state, "route_status": route_status})
                executor = self.executor()
                result = self.confirm(executor, executor.preview(self.request(action, {}, action)))
                self.assertEqual(result.to_dict()["status"], "submitted")
                command = self.transport.calls[-1][0]
                self.assertEqual(command.path, f"/screen/uav/{path}/control")
                self.assertEqual(command.body["type"], code)

    def test_route_controls_require_the_authoritative_route_state(self):
        for action, route_status in (("route_pause", "paused"), ("route_resume", "running"),
                                     ("route_pause", None), ("route_resume", None)):
            with self.subTest(action=action, route_status=route_status):
                self.snapshot = replace(self.snapshot, permissions=frozenset({action}),
                                        capabilities={action: True},
                                        state={**self.snapshot.state, "route_status": route_status})
                self.assertIn(self.executor().preview(self.request(action, {}, f"bad-{action}-{route_status}"))
                              .error_code, {"ROUTE_STATE_MISMATCH", "AUTHORITATIVE_STATE_INCOMPLETE"})

    def test_unimplemented_actions_are_explicitly_unsupported(self):
        for action, parameters in (("land", {}),
                                   ("takeoff", {"altitude_m": 20}),
                                   ("set_move_speed", {"value": 2})):
            with self.subTest(action=action):
                self.assertEqual(self.executor().preview(self.request(action, parameters)).error_code, "ACTION_UNSUPPORTED")
        # 转向和云台已实现，但必须由权威快照逐动作授权；本测试的快照没有授予。
        self.assertEqual(
            self.executor().preview(self.request("turn_left", {"angle_deg": 90})).error_code,
            "ACTION_NOT_PERMITTED")
        self.assertEqual(self.transport.calls, [])

    def test_micro_control_requires_authoritative_hover_state(self):
        self.snapshot = replace(self.snapshot, state={**self.snapshot.state, "route_status": "idle"})
        result = self.executor().preview(self.request())
        self.assertEqual(result.error_code, "HOVER_REQUIRED")
        self.assertIn("请先说‘悬停’", result.message)
        self.assertEqual(self.transport.calls, [])

    def test_manual_hover_requires_stable_mode_and_zero_speeds(self):
        self.snapshot = replace(self.snapshot, state={**self.snapshot.state,
            "route_status": "idle", "flight_mode": "QHOVER",
            "ground_speed_mps": 0.02, "vertical_speed_mps": -0.01})
        self.assertIsNotNone(self.executor().preview(self.request()).confirmation_id)
        self.snapshot = replace(self.snapshot, state={**self.snapshot.state, "ground_speed_mps": 1.0})
        self.assertEqual(self.executor().preview(self.request(key="moving")).error_code, "HOVER_REQUIRED")

    def test_micro_control_rejects_missing_authoritative_route_status(self):
        self.snapshot = replace(self.snapshot, state={**self.snapshot.state,
            "route_status": None, "flight_mode": "QHOVER",
            "ground_speed_mps": 0.01, "vertical_speed_mps": 0.01})
        self.assertEqual(self.executor().preview(self.request(key="route-unknown")).error_code,
                         "AUTHORITATIVE_STATE_INCOMPLETE")

    def test_control_acquire_requires_success_and_fresh_owner_snapshot(self):
        self.snapshot = replace(self.snapshot, permissions=frozenset({"control_acquire", "read_state"}),
                                capabilities={"control_acquire": True}, control_owner=None,
                                control_lease_expires_at=None)
        self.transport.control_calls = []

        def acquire(binding, *, site_id, session_id, force=False):
            self.transport.control_calls.append((binding, site_id, session_id, force))
            self.snapshot = replace(self.snapshot, control_owner=self.binding.actor_id,
                                    control_lease_expires_at=self.now + 60)
            return KtdHttpResponse(200, {"code": 200, "data": "1"})

        self.transport.acquire_control = acquire
        executor = self.executor()
        request = self.request("control_acquire", {}, "control-acquire")
        card = executor.preview(request)
        self.assertIs(card.status, FlightControlStatus.PREVIEW_REQUIRED)
        result = self.confirm(executor, card)
        self.assertIs(result.status, FlightControlStatus.ACCEPTED)
        self.assertEqual(result.state["control_owner"], self.binding.actor_id)
        self.assertEqual(len(self.transport.control_calls), 1)
        self.assertEqual(self.transport.calls, [])

    def test_control_acquire_data_zero_is_rejected_without_claiming_authority(self):
        self.snapshot = replace(self.snapshot, permissions=frozenset({"control_acquire", "read_state"}),
                                capabilities={"control_acquire": True}, control_owner=None,
                                control_lease_expires_at=None)
        self.transport.acquire_control = lambda *args, **kwargs: KtdHttpResponse(
            200, {"code": 200, "data": "0"})
        executor = self.executor()
        result = self.confirm(executor, executor.preview(
            self.request("control_acquire", {}, "control-acquire-fail")))
        self.assertEqual(result.error_code, "CONTROL_ACQUIRE_FAILED")
        self.assertIs(result.status, FlightControlStatus.REJECTED)
        self.assertIsNone(result.state["control_owner"])

    def test_control_apply_returns_pending_without_claiming_owner(self):
        self.snapshot = replace(self.snapshot, permissions=frozenset({"control_acquire", "read_state"}),
                                capabilities={"control_acquire": True}, control_owner="other")
        self.transport.apply_control = lambda *args, **kwargs: KtdHttpResponse(
            200, {"code": 200, "data": "0"})
        executor = self.executor()
        result = self.confirm(executor, executor.preview(self.request(
            "control_acquire", {"mode": "apply", "reason": "现场操作申请"}, "control-apply")))
        self.assertEqual(result.status, "pending")
        self.assertEqual(result.error_code, "CONTROL_REQUEST_PENDING")
        self.assertEqual(result.state["control_owner"], "other")

    def test_control_force_is_rejected_without_http_side_effect(self):
        self.snapshot = replace(self.snapshot, permissions=frozenset({"control_acquire", "read_state"}),
                                capabilities={"control_acquire": True}, control_owner="other")
        self.transport.acquire_control = lambda *args, **kwargs: KtdHttpResponse(
            200, {"code": 200, "data": "0"})
        executor = self.executor()
        result = self.confirm(executor, executor.preview(self.request(
            "control_acquire", {"mode": "force"}, "control-force")))
        self.assertEqual(result.error_code, "CONTROL_ACQUIRE_FAILED")
        self.assertEqual(result.status, FlightControlStatus.REJECTED)
        self.assertEqual(self.transport.calls, [])

    def test_control_release_requires_owner_and_verifies_release_snapshot(self):
        self.snapshot = replace(self.snapshot, permissions=frozenset({"control_release", "read_state"}),
                                capabilities={"control_release": True})

        def release(binding, *, site_id):
            self.snapshot = replace(self.snapshot, control_owner=None,
                                    control_lease_expires_at=None)
            return KtdHttpResponse(200, {"code": 200})

        self.transport.release_control = release
        executor = self.executor()
        result = self.confirm(executor, executor.preview(
            self.request("control_release", {}, "control-release")))
        self.assertIs(result.status, FlightControlStatus.ACCEPTED)
        self.assertIsNone(result.state["control_owner"])

    def test_confirmation_binding_strictly_rejects_wrong_and_boolean_versions(self):
        executor = self.executor()
        card = executor.preview(self.request())
        for changes in ({"command_id": "other"}, {"idempotency_key": "other"},
                        {"confirmation_binding": "wrong"}, {"context_version": True}):
            with self.subTest(changes=changes):
                self.assertEqual(self.confirm(executor, card, **changes).to_dict()["status"], "rejected")
        self.assertEqual(self.transport.calls, [])

    def test_state_change_and_card_expiry_invalidate_confirmation(self):
        for change in ("position", "expiry"):
            with self.subTest(change=change):
                executor = self.executor()
                card = executor.preview(self.request(key=change))
                if change == "position":
                    self.snapshot = replace(self.snapshot, state={**self.snapshot.state, "longitude": 113.001})
                else:
                    self.now += 31
                    self.snapshot = replace(self.snapshot, observed_at=self.now)
                self.assertEqual(self.confirm(executor, card).to_dict()["status"], "rejected")
        self.assertEqual(self.transport.calls, [])

    def test_idempotency_conflict_never_reuses_an_old_confirmation(self):
        executor = self.executor()
        card = executor.preview(self.request())
        self.assertEqual(executor.preview(self.request(parameters={"distance_m": 11})).error_code, "IDEMPOTENCY_CONFLICT")
        self.confirm(executor, card)
        self.assertEqual(self.executor().preview(self.request(parameters={"distance_m": 11})).error_code, "IDEMPOTENCY_CONFLICT")
        self.assertEqual(len(self.transport.calls), 1)

    def test_concurrent_executors_submit_once_with_shared_durable_journal(self):
        first, second = self.executor(), self.executor()
        cards = first.preview(self.request()), second.preview(self.request())
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda pair: self.confirm(*pair), ((first, cards[0]), (second, cards[1]))))
        self.assertEqual(len(self.transport.calls), 1)
        self.assertTrue(all(result.to_dict()["status"] in ("submitted", "unknown", "rejected") for result in results))

    def test_response_classification_never_upgrades_ambiguous_200_or_server_error(self):
        for index, (response, expected) in enumerate((
            (KtdHttpResponse(200, {}), "unknown"), (KtdHttpResponse(200, {"code": "200"}), "unknown"),
            (KtdHttpResponse(200, {"code": 403}), "rejected"), (KtdHttpResponse(503, {"code": 200}), "unknown"),
            (KtdHttpResponse(302, {"code": 200}), "unknown"),
        )):
            with self.subTest(response=response):
                self.transport.response = response
                executor = self.executor()
                result = self.confirm(executor, executor.preview(self.request(key=f"result-{index}")))
                self.assertEqual(result.to_dict()["status"], expected)
        self.assertEqual(len(self.transport.calls), 5)

    def test_no_state_is_invented_when_authoritative_provider_is_unavailable(self):
        self.snapshot = None
        executor = self.executor()
        result = executor.read_state()
        self.assertFalse(result["available"])
        self.assertNotIn("airborne", result)
        self.assertEqual(executor.preview(self.request()).error_code, "AUTHORITATIVE_STATE_UNAVAILABLE")
        self.assertEqual(self.transport.calls, [])

    def test_read_state_does_not_require_control_or_enable_writes(self):
        self.snapshot = replace(self.snapshot, control_owner="someone-else")
        executor = self.executor(enabled=False)
        result = executor.preview(self.request("read_state", {}))
        self.assertEqual(result.to_dict()["status"], "observed")
        self.assertEqual(result.state["control_owner"], "someone-else")
        self.assertEqual(self.transport.calls, [])

    def test_pz_distance_requires_relative_height_used_by_its_java_path(self):
        self.snapshot = replace(self.snapshot, profile="PZ_HANGAR",
                                state={**self.snapshot.state, "height_m": None})
        self.assertEqual(self.executor().preview(self.request()).error_code, "FLIGHT_POSE_UNAVAILABLE")
        self.snapshot = replace(self.snapshot, state={**self.snapshot.state, "height_m": 20})
        executor = self.executor()
        self.assertEqual(self.confirm(executor, executor.preview(self.request())).to_dict()["status"], "submitted")

    def test_dji_unknown_or_low_relative_height_cannot_trigger_an_implicit_climb(self):
        for value, code in ((None, 'RELATIVE_HEIGHT_UNAVAILABLE'), (True, 'RELATIVE_HEIGHT_UNAVAILABLE'),
                            ('20', 'RELATIVE_HEIGHT_UNAVAILABLE'), (float('nan'), 'RELATIVE_HEIGHT_UNAVAILABLE'),
                            (-1, 'DJI_FLYTO_IMPLICIT_CLIMB'), (0, 'DJI_FLYTO_IMPLICIT_CLIMB'),
                            (19.99, 'DJI_FLYTO_IMPLICIT_CLIMB')):
            with self.subTest(height=value):
                self.snapshot = replace(self.snapshot, state={**self.snapshot.state, 'height_m': value})
                result = self.executor().preview(self.request())
                self.assertEqual(result.error_code, code)
                self.assertEqual(result.to_dict()['status'], 'rejected')
        self.assertEqual(self.transport.calls, [])

    def test_dji_low_target_is_unverified_and_absolute_target_obeys_vendor_range(self):
        self.snapshot = replace(self.snapshot, permissions=frozenset({'move_down', 'move_up'}),
                                capabilities={'move_down': True, 'move_up': True, 'UAV_MOVE_DISTANCE': True})
        for action, altitude, height, distance, code in (
            ('move_down', 100, 25, 5.01, 'DJI_LOW_TARGET_UNVERIFIED'),
            ('move_down', 10, 50, 9, 'DJI_TARGET_ALTITUDE_OUT_OF_RANGE'),
            ('move_up', 9999, 50, 2, 'DJI_TARGET_ALTITUDE_OUT_OF_RANGE'),
        ):
            with self.subTest(action=action, code=code):
                self.snapshot = replace(self.snapshot, state={**self.snapshot.state,
                                        'altitude_m': altitude, 'height_m': height})
                self.assertEqual(self.executor().preview(self.request(action, {'distance_m': distance})).error_code, code)
        self.assertEqual(self.transport.calls, [])

    def test_dji_relative_height_boundaries_and_pz_low_height_are_not_conflated(self):
        for profile, action, height, distance in (
            ('DJI_HANGAR', 'move_forward', 20, 10),
            ('DJI_HANGAR', 'move_down', 25, 5),
            ('PZ_HANGAR', 'move_forward', 5, 10),
        ):
            with self.subTest(profile=profile, action=action):
                self.snapshot = replace(self.snapshot, profile=profile,
                    permissions=frozenset({action}), capabilities={action: True, 'UAV_MOVE_DISTANCE': True},
                    state={**self.snapshot.state, 'height_m': height})
                card = self.executor().preview(self.request(action, {'distance_m': distance}, profile + action))
                self.assertEqual(card.to_dict()['status'], 'preview_required')
        self.assertEqual(self.transport.calls, [])

    def test_corrupt_journal_fails_closed_without_sending(self):
        executor = self.executor()
        card = executor.preview(self.request())
        connection = sqlite3.connect(self.journal_path)
        try:
            with connection:
                connection.execute("DROP TABLE commands")
        finally:
            connection.close()
        self.assertEqual(self.confirm(executor, card).error_code, "JOURNAL_OR_STATE_UNAVAILABLE")
        self.assertEqual(self.transport.calls, [])

    def test_journal_commit_before_uncatchable_process_exit_blocks_retry(self):
        self.transport.failure = SystemExit("simulated process death after send")
        executor = self.executor()
        card = executor.preview(self.request())
        with self.assertRaises(SystemExit):
            self.confirm(executor, card)
        restarted = self.executor()
        self.assertEqual(self.confirm(restarted, card).to_dict()["status"], "unknown")
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(Path(self.journal_path).stat().st_mode & 0o777, 0o600)

    def test_changed_request_cannot_mutate_existing_preview(self):
        parameters = {"distance_m": 10}
        executor = self.executor()
        card = executor.preview(self.request(parameters=parameters))
        parameters["distance_m"] = 19
        result = self.confirm(executor, card)
        self.assertEqual(result.to_dict()["status"], "submitted")
        self.assertEqual(self.transport.calls[0][0].body["param"], 10)

    def test_pending_cards_have_a_bounded_capacity(self):
        executor = self.executor()
        for index in range(128):
            self.assertIs(executor.preview(self.request(key=f"card-{index}")).status,
                          FlightControlStatus.PREVIEW_REQUIRED)
        self.assertEqual(executor.preview(self.request(key="overflow")).error_code, "PENDING_LIMIT_REACHED")
        self.assertEqual(self.transport.calls, [])

    def test_available_actions_are_filtered_by_authoritative_state_and_enabled(self):
        self.assertEqual(self.executor().available_actions(), frozenset({"move_forward", "read_state"}))
        self.assertEqual(self.executor(enabled=False).available_actions(), frozenset({"read_state"}))
        self.snapshot = replace(self.snapshot, capabilities={"move_forward": True})
        self.assertEqual(self.executor().available_actions(), frozenset({"read_state"}))
        self.snapshot = replace(self.snapshot, observed_at=990)
        self.assertEqual(self.executor().available_actions(), frozenset())
        self.assertEqual(self.transport.calls, [])

    def test_missing_or_invalid_control_lease_cannot_authorize_writes(self):
        for lease in (None, True, "1060", float("inf"), float("nan")):
            with self.subTest(lease=lease):
                self.snapshot = replace(self.snapshot, control_lease_expires_at=lease)
                executor = self.executor()
                self.assertEqual(executor.preview(self.request()).error_code, "CONTROL_LEASE_UNAVAILABLE")
                self.assertEqual(executor.available_actions(), frozenset({"read_state"}))
        self.assertEqual(self.transport.calls, [])

    def test_expired_or_exact_deadline_control_lease_rejects_preview(self):
        for lease in (self.now - 1, self.now):
            with self.subTest(lease=lease):
                self.snapshot = replace(self.snapshot, control_lease_expires_at=lease)
                self.assertEqual(self.executor().preview(self.request()).error_code, "CONTROL_LEASE_EXPIRED")
        self.assertEqual(self.transport.calls, [])

    def test_control_lease_expiring_between_preview_and_confirm_never_sends(self):
        self.snapshot = replace(self.snapshot, control_lease_expires_at=self.now + 1)
        executor = self.executor()
        card = executor.preview(self.request())
        self.assertIs(card.status, FlightControlStatus.PREVIEW_REQUIRED)
        self.now += 1
        # 新 OSD 不能把已经过期的控制权续租；只有权威租约事实可以。
        self.snapshot = replace(self.snapshot, observed_at=self.now)
        self.assertEqual(self.confirm(executor, card).error_code, "CONTROL_LEASE_EXPIRED")
        self.assertEqual(self.transport.calls, [])

    def test_missing_control_lease_keeps_read_state_available_without_write_authority(self):
        self.snapshot = replace(self.snapshot, control_lease_expires_at=None)
        executor = self.executor()
        self.assertTrue(executor.read_state()["available"])
        self.assertEqual(executor.preview(self.request("read_state", {})).to_dict()["status"], "observed")
        self.assertEqual(executor.preview(self.request()).error_code, "CONTROL_LEASE_UNAVAILABLE")
        self.assertEqual(self.transport.calls, [])


class HttpTransportTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.response_status = 200
        self.response_payload = {"code": 200}
        outer = self

        class Opener:
            def open(self, request, *, timeout):
                outer.requests.append((request.full_url, request.get_method(),
                                       json.loads(request.data or b'{}'), dict(request.headers)))
                response = BytesIO(json.dumps(outer.response_payload).encode())
                response.code = outer.response_status
                return response

        self.opener = Opener()
        self.base_url = "https://platform.test/api"
        self.context = KtdRequestContext("actor-1", "project-1", "trace-1", "key-1")
        self.command = KtdCommand("/screen/uav/route/control", {"terminalId": "drone-1", "type": 113})

    def transport(self):
        return KtdHttpTransport(self.base_url, token="test-only-token", actor_id="actor-1", opener=self.opener)

    def test_fixed_http_endpoint_and_authentication_context(self):
        response = self.transport().submit(self.command, self.context)
        self.assertEqual(response, KtdHttpResponse(200, {"code": 200}))
        path, method, body, headers = self.requests[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "https://platform.test/api/screen/uav/route/control")
        self.assertEqual(body, self.command.body)
        self.assertEqual(headers["Authorization"], "Bearer test-only-token")
        self.assertEqual(headers["Projectid"], "project-1")
        self.assertEqual(headers["X-trace-id"], "trace-1")

    def test_control_power_uses_fixed_get_endpoint_and_strict_response_payload(self):
        self.response_payload = {"code": 200, "data": "1"}
        binding = RuntimeBinding("actor-1", "project-1", "hangar-1", "drone-1", "batch-1", 1)
        response = self.transport().acquire_control(binding, site_id="site-1", session_id="trace-1")
        self.assertEqual(response, KtdHttpResponse(200, {"code": 200, "data": "1"}))
        path, method, body, headers = self.requests[0]
        self.assertEqual(method, "GET")
        self.assertEqual(body, {})
        self.assertIn("/uavControlPower/obtain?", path)
        self.assertIn("uavId=drone-1", path)
        self.assertIn("sessionId=trace-1", path)
        self.assertIn("isForce=false", path)
        self.assertEqual(headers["Projectid"], "project-1")
        self.response_payload = {"code": 200, "data": "0"}
        self.assertEqual(self.transport().acquire_control(binding, site_id="site-1", session_id="trace-2").payload["data"], "0")
        self.assertIn("isForce=false", self.requests[-1][0])
        self.transport().acquire_control(binding, site_id="site-1", session_id="trace-3", force=True)
        self.assertIn("isForce=true", self.requests[-1][0])

    def test_control_apply_uses_fixed_post_endpoint_and_reason(self):
        binding = RuntimeBinding("actor-1", "project-1", "hangar-1", "drone-1", "batch-1", 1)
        response = self.transport().apply_control(binding, site_id="site-1", reason="现场操作申请")
        self.assertEqual(response, KtdHttpResponse(200, {"code": 200}))
        path, method, body, headers = self.requests[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "https://platform.test/api/uavControlPower/apply")
        self.assertEqual(body, {"uavId": "drone-1", "reason": "现场操作申请"})
        self.assertEqual(headers["Projectid"], "project-1")

    def test_redirect_response_is_returned_without_retry(self):
        self.response_status = 302
        self.assertEqual(self.transport().submit(self.command, self.context).status_code, 302)
        self.assertEqual(len(self.requests), 1)

    def test_path_or_identity_cannot_be_overridden_by_a_tool(self):
        for command, context in ((KtdCommand("https://evil/control", {}), self.context),
                                 (KtdCommand("/screen/uav/route/control?x=1", {}), self.context),
                                 (self.command, replace(self.context, actor_id="other"))):
            with self.subTest(path=command.path):
                with self.assertRaises(KtdPlatformError):
                    self.transport().submit(command, context)
        self.assertEqual(self.requests, [])

    def test_base_url_and_header_configuration_reject_injection(self):
        for url in ("http://example.test/api", "https://user:pass@example.test/api",
                    "https://example.test/api?x=1", "https://example.test/other", "file:///api"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    KtdHttpTransport(url, token="test", actor_id="actor")
        with self.assertRaises(ValueError):
            KtdHttpTransport("https://example.test/api", token="bad\r\nheader", actor_id="actor")
        # 常见平台 JWT 会超过 256 字符，服务端凭据不能误用业务 ID 长度限制。
        KtdHttpTransport("https://example.test/api", token="x" * 1000, actor_id="actor")


if __name__ == "__main__":
    unittest.main()
