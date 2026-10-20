"""Route A：用 ktd 现有只读接口拼装测试授权，再用旧控制接口发命令。

全部使用合成 HTTP 边界和临时日志；不访问 ktd、MQTT 或真实设备。
"""
from io import BytesIO
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from lingxi_flight_control.full_control import FlightControlActionRequest, FlightControlStatus, RuntimeBinding
from lingxi_flight_control.ktd_legacy import LegacyKtdSnapshotProvider
from lingxi_flight_control.ktd_platform import (
    KtdHttpResponse, KtdHttpTransport, KtdPlatformError, KtdPlatformExecutor, KtdSnapshot,
    SqliteCommandJournal,
)
from lingxi_flight_control.platform_runtime import PlatformRuntimeConfig, build_platform_cockpit


def ok(data):
    return KtdHttpResponse(200, {"success": True, "code": 200, "data": data})


class FakeLegacyTransport:
    actor_id = "actor-1"

    def __init__(self):
        self.calls = []
        self.submitted = []
        self.responses = {
            "user_info": ok({"id": "user-1", "empId": "actor-1",
                             "permission": ["taskManage:Issued"]}),
            "sites": ok([{"id": "site-1", "uavId": "drone-1", "hangarId": "hangar-1"}]),
            "owners": ok([{"employeeId": "actor-1"}]),
            "permissions": ok({"isHangar": True,
                               "hangarInfo": {"brand": "207601", "model": "DJI3"},
                               "uav": {"UAV_MOVE_DISTANCE": False, "UAV_MOVE_SPEED": True}}),
            "hangar_state": ok({"uavId": "drone-1", "uavState": "5",
                                "inFlight": True, "landing": False}),
        }

    def read_legacy(self, resource, *, project_id, drone_id=None, query=None):
        self.calls.append((resource, project_id, drone_id, dict(query or {})))
        value = self.responses[resource]
        if isinstance(value, Exception):
            raise value
        return value

    def submit(self, command, context):
        self.submitted.append((command, context))
        return KtdHttpResponse(200, {"code": 200})


class LegacySnapshotProviderTests(unittest.TestCase):
    def setUp(self):
        self.transport = FakeLegacyTransport()
        self.now = 1000.0
        self.binding = RuntimeBinding("actor-1", "project-1", "hangar-1", "drone-1", "batch-1", 1)
        self.provider = LegacyKtdSnapshotProvider(
            self.binding, site_id="site-1", task_id=None,
            transport=self.transport, clock=lambda: self.now)

    def test_valid_legacy_snapshot_is_explicitly_marked_and_readable(self):
        snapshot = self.provider()
        self.assertEqual(snapshot.authorization_quality, "legacy-read-api")
        self.assertEqual(snapshot.control_owner, "actor-1")
        self.assertEqual(snapshot.control_lease_expires_at, self.now + 300)
        self.assertEqual(snapshot.observed_at, self.now)
        self.assertEqual(snapshot.profile, "DJI_HANGAR")
        self.assertIn("read_state", snapshot.permissions)
        self.assertIn("move_forward", snapshot.permissions)
        self.assertIn("camera_photo", snapshot.permissions)
        self.assertNotIn("route_pause", snapshot.permissions)
        self.assertIs(snapshot.capabilities["UAV_MOVE_DISTANCE"], True)
        self.assertIs(snapshot.state["online"], True)
        self.assertIs(snapshot.state["airborne"], True)
        self.assertEqual(snapshot.state["flight_status"], "5")
        self.assertRegex(snapshot.version, r"^[0-9a-f]{64}$")
        self.assertEqual([call[0] for call in self.transport.calls],
                         ["user_info", "sites", "owners", "permissions", "hangar_state"])

    def test_operator_must_match_login_identity(self):
        self.transport.responses["user_info"] = ok({"empId": "actor-2", "permission": []})
        with self.assertRaises(KtdPlatformError) as raised:
            self.provider()
        self.assertEqual(str(raised.exception), "AUTHENTICATION_REQUIRED")

    def test_site_hangar_and_drone_binding_must_match_exactly(self):
        for row in (
            {"id": "site-1", "uavId": "drone-2", "hangarId": "hangar-1"},
            {"id": "site-1", "uavId": "drone-1", "hangarId": "hangar-2"},
            {"id": "site-2", "uavId": "drone-1", "hangarId": "hangar-1"},
        ):
            with self.subTest(row=row):
                self.transport.responses["sites"] = ok([row])
                with self.assertRaises(KtdPlatformError) as raised:
                    self.provider()
                self.assertEqual(str(raised.exception), "RESOURCE_BINDING_MISMATCH")

    def test_multiple_or_missing_owners_do_not_select_a_winner(self):
        for owners in ([], [{"employeeId": "actor-1"}, {"employeeId": "actor-2"}]):
            with self.subTest(owners=owners):
                self.transport.responses["owners"] = ok(owners)
                self.assertIsNone(self.provider().control_owner)

    def test_missing_write_permission_only_exposes_read_state(self):
        self.transport.responses["user_info"] = ok({"empId": "actor-1", "permission": []})
        snapshot = self.provider()
        self.assertEqual(snapshot.permissions, frozenset({"read_state"}))
        self.assertEqual(snapshot.capabilities, {})
        self.assertEqual(set(snapshot.action_availability), {"read_state"})

    def test_only_dji_hangar_profile_is_supported(self):
        for hangar in ({"brand": "207606"}, {"brand": "207608"}, None):
            with self.subTest(hangar=hangar):
                self.transport.responses["permissions"] = ok({"isHangar": True, "hangarInfo": hangar})
                with self.assertRaises(KtdPlatformError) as raised:
                    self.provider()
                self.assertEqual(str(raised.exception), "PROFILE_UNSUPPORTED")
        self.transport.responses["permissions"] = ok({"isHangar": False, "hangarInfo": {"brand": "207601"}})
        with self.assertRaises(KtdPlatformError):
            self.provider()

    def test_offline_or_unknown_airborne_is_preserved_and_blocks_writes_later(self):
        for state, airborne, online in (("-1", False, False), ("14", None, False), ("5", None, True)):
            with self.subTest(state=state):
                self.transport.responses["hangar_state"] = ok(
                    {"uavId": "drone-1", "uavState": state, "inFlight": airborne})
                snapshot = self.provider()
                self.assertIs(snapshot.state["online"], online)
                self.assertIs(snapshot.state["airborne"], airborne)

    def test_upstream_errors_are_reduced_to_stable_codes(self):
        self.transport.responses["sites"] = KtdHttpResponse(500, {"code": 500})
        with self.assertRaises(KtdPlatformError) as raised:
            self.provider()
        self.assertEqual(str(raised.exception), "LEGACY_STATE_UNAVAILABLE")
        self.transport.responses["sites"] = KtdHttpResponse(401, {"code": 401})
        with self.assertRaises(KtdPlatformError) as raised:
            self.provider()
        self.assertEqual(str(raised.exception), "AUTHENTICATION_REQUIRED")
        self.transport.responses["sites"] = RuntimeError("socket down")
        with self.assertRaises(KtdPlatformError) as raised:
            self.provider()
        self.assertEqual(str(raised.exception), "LEGACY_STATE_UNAVAILABLE")

    def test_invalid_binding_and_transport_actor_fail_before_any_read(self):
        bad = RuntimeBinding("actor-2", "project-1", "hangar-1", "drone-1", "batch-1", 1)
        with self.assertRaises(KtdPlatformError):
            LegacyKtdSnapshotProvider(bad, site_id="site-1", task_id=None,
                                       transport=self.transport, clock=lambda: self.now)
        with self.assertRaises(KtdPlatformError):
            LegacyKtdSnapshotProvider(self.binding, site_id="site-1", task_id=None,
                                       transport=object(), clock=lambda: self.now)
        self.assertEqual(self.transport.calls, [])


class LegacyExecutorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.transport = FakeLegacyTransport()
        self.now = 1000.0
        self.binding = RuntimeBinding("actor-1", "project-1", "hangar-1", "drone-1", "batch-1", 1)
        self.provider = LegacyKtdSnapshotProvider(
            self.binding, site_id="site-1", task_id=None,
            transport=self.transport, clock=lambda: self.now)
        self.journal_path = str(Path(self.directory.name) / "commands.sqlite3")

    def executor(self, *, enabled=True, pose=False, provider=None):
        return KtdPlatformExecutor(
            self.binding, site_id="site-1", task_id=None,
            transport=self.transport, snapshot_provider=provider or self.provider,
            journal=SqliteCommandJournal(self.journal_path), enabled=enabled,
            clock=lambda: self.now, legacy_read_api=True,
            allow_unverified_pose=pose, track_commands=False)

    def request(self, action="move_forward", parameters=None, key="request-1"):
        return FlightControlActionRequest(
            "command-1", key, action,
            {"distance_m": 10} if parameters is None else parameters, 1)

    def confirm(self, executor, card):
        return executor.confirm(
            card.confirmation_id, command_id=card.command_id,
            idempotency_key=card.idempotency_key,
            confirmation_binding=card.confirmation_binding, context_version=1)

    def test_disabled_legacy_executor_has_no_network_or_write_effect(self):
        executor = self.executor(enabled=False)
        self.assertEqual(executor.preview(self.request()).error_code, "PLATFORM_DISABLED")
        self.assertEqual(self.transport.submitted, [])

    def test_camera_and_gimbal_work_without_pose_but_never_claim_completion(self):
        executor = self.executor()
        for action, expected_type in (("camera_photo", 100),
                                      ("camera_start_recording", 101),
                                      ("camera_stop_recording", 102),
                                      ("gimbal_reset", 403)):
            with self.subTest(action=action):
                card = executor.preview(self.request(action, {}, key="key-" + action))
                self.assertIs(card.status, FlightControlStatus.PREVIEW_REQUIRED)
                receipt = self.confirm(executor, card)
                self.assertEqual(receipt.to_dict()["status"], "submitted")
                self.assertFalse(receipt.device_effect_verified)
                command, _context = self.transport.submitted[-1]
                expected_path = ("/screen/uav/gimbal/control" if action == "gimbal_reset"
                                 else "/screen/uav/camera/control")
                self.assertEqual(command.path, expected_path)
                self.assertEqual(command.body["type"], expected_type)
                self.assertNotIn("tracking", command.body)

    def test_movement_requires_explicit_pose_unverified_setting(self):
        executor = self.executor(pose=False)
        self.assertEqual(executor.preview(self.request()).error_code,
                         "LEGACY_POSE_UNVERIFIED_DISABLED")
        self.assertEqual(self.transport.submitted, [])
        self.assertEqual(executor.available_actions(), frozenset(
            {"read_state", "camera_photo", "camera_start_recording",
             "camera_stop_recording", "gimbal_reset"}))

    def test_movement_uses_existing_java_endpoint_with_pose_unverified(self):
        executor = self.executor(pose=True)
        card = executor.preview(self.request())
        self.assertIs(card.status, FlightControlStatus.PREVIEW_REQUIRED)
        receipt = self.confirm(executor, card)
        self.assertEqual(receipt.to_dict()["status"], "submitted")
        command, _context = self.transport.submitted[0]
        self.assertEqual(command.path, "/screen/uav/operation/control")
        self.assertEqual(command.body, {"terminalId": "drone-1", "siteId": "site-1",
                                        "type": 202, "mode": 2, "param": 10})

    def test_confirmation_binding_ignores_synthetic_lease_and_read_time(self):
        executor = self.executor(pose=True)
        card = executor.preview(self.request())
        self.now += 1.0
        receipt = self.confirm(executor, card)
        self.assertEqual(receipt.to_dict()["status"], "submitted")
        self.assertEqual(len(self.transport.submitted), 1)

    def test_owner_loss_after_preview_rejects_before_send(self):
        executor = self.executor(pose=True)
        card = executor.preview(self.request())
        self.transport.responses["owners"] = ok([{"employeeId": "actor-2"}])
        receipt = self.confirm(executor, card)
        self.assertEqual(receipt.error_code, "CONTROL_NOT_OWNED")
        self.assertEqual(self.transport.submitted, [])

    def test_snapshot_quality_mismatch_cannot_enter_legacy_executor(self):
        authoritative = KtdSnapshot(
            actor_id="actor-1", project_id="project-1", hangar_id="hangar-1",
            drone_id="drone-1", site_id="site-1", task_id=None, context_version=1,
            version="a" * 64, observed_at=self.now, control_owner="actor-1",
            profile="DJI_HANGAR", permissions=frozenset({"move_forward", "read_state"}),
            capabilities={"move_forward": True, "UAV_MOVE_DISTANCE": True},
            state={"online": True, "airborne": True}, control_lease_expires_at=self.now + 60)
        executor = self.executor(pose=True, provider=lambda: authoritative)
        self.assertEqual(executor.preview(self.request()).error_code,
                         "AUTHORIZATION_QUALITY_MISMATCH")
        self.assertEqual(self.transport.submitted, [])

    def test_read_state_exposes_legacy_quality_and_never_sends(self):
        snapshot = self.executor().read_state()
        self.assertTrue(snapshot["available"])
        self.assertEqual(snapshot["authorization_quality"], "legacy-read-api")
        self.assertEqual(snapshot["control_owner"], "actor-1")
        self.assertEqual(self.transport.submitted, [])


class LegacyTransportTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        outer = self

        class Opener:
            def open(self, request, *, timeout):
                outer.requests.append((request.full_url, dict(request.headers)))
                response = BytesIO(json.dumps(
                    {"success": True, "code": 200, "data": []}).encode())
                response.code = 200
                return response

        self.opener = Opener()
        self.transport = KtdHttpTransport(
            "https://platform.test/api", token="test-only-token",
            actor_id="actor-1", opener=self.opener)

    def test_fixed_legacy_resources_and_headers(self):
        response = self.transport.read_legacy(
            "sites", project_id="project-1", query={"projectId": "project-1"})
        self.assertEqual(response.status_code, 200)
        url, headers = self.requests[0]
        self.assertEqual(url, "https://platform.test/api/uavSite/list?projectId=project-1")
        self.assertEqual(headers["Authorization"], "Bearer test-only-token")
        self.assertEqual(headers["Projectid"], "project-1")
        self.transport.read_legacy("permissions", project_id="project-1", drone_id="drone-1")
        self.assertEqual(self.requests[1][0],
                         "https://platform.test/api/screen/control/permissions/3/drone-1")

    def test_unknown_resource_and_bad_identifier_never_reach_network(self):
        for call in (lambda: self.transport.read_legacy("arbitrary", project_id="project-1"),
                     lambda: self.transport.read_legacy("permissions", project_id="project-1",
                                                        drone_id="../evil"),
                     lambda: self.transport.read_legacy("sites", project_id="project\r\n1")):
            with self.assertRaises(KtdPlatformError):
                call()
        self.assertEqual(self.requests, [])


class LegacyRuntimeConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.environment = {
            "KTD_BASE_URL": "https://platform.invalid/api",
            "KTD_SERVER_TOKEN": "synthetic-server-credential",
            "KTD_ACTOR_EMPLOYEE_ID": "actor-1",
            "KTD_PROJECT_ID": "project-1",
            "KTD_SITE_ID": "site-1",
            "KTD_HANGAR_ID": "hangar-1",
            "KTD_DRONE_ID": "drone-1",
            "FLIGHT_AGENT_TASK_BATCH_ID": "batch-1",
            "FLIGHT_AGENT_GATEWAY_TOKEN": "synthetic-gateway-credential",
            "FLIGHT_AGENT_JOURNAL": str(Path(self.directory.name) / "commands.sqlite3"),
        }

    def test_legacy_flags_are_parsed_strictly(self):
        config = PlatformRuntimeConfig.from_environment({
            **self.environment,
            "FLIGHT_AGENT_LEGACY_READ_API": "true",
            "FLIGHT_AGENT_LEGACY_ALLOW_POSE_UNVERIFIED": "true"})
        self.assertTrue(config.legacy_read_api)
        self.assertTrue(config.allow_legacy_pose_unverified)
        for key, value in (("FLIGHT_AGENT_LEGACY_READ_API", "yes"),
                           ("FLIGHT_AGENT_LEGACY_ALLOW_POSE_UNVERIFIED", "1")):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    PlatformRuntimeConfig.from_environment({**self.environment, key: value})
        with self.assertRaises(ValueError):
            PlatformRuntimeConfig.from_environment({**self.environment, key: value})
        config = PlatformRuntimeConfig.from_environment({
            **self.environment,
            "FLIGHT_AGENT_LEGACY_ALLOW_POSE_UNVERIFIED": "true"})
        self.assertFalse(config.legacy_read_api)
        self.assertTrue(config.allow_legacy_pose_unverified)
        with self.assertRaises(ValueError):
            build_platform_cockpit(
                config, lambda: None, enable_control=False,
                enable_legacy_control=False, allow_legacy_pose_unverified=True)

    def test_builder_accepts_legacy_without_network(self):
        config = PlatformRuntimeConfig.from_environment({
            **self.environment, "FLIGHT_AGENT_LEGACY_READ_API": "true"})
        service = build_platform_cockpit(
            config, lambda: None, enable_control=False,
            enable_legacy_control=True, opener=None)
        self.assertEqual(service.binding.actor_id, "actor-1")
        self.assertTrue(Path(self.environment["FLIGHT_AGENT_JOURNAL"]).exists())


class FullLegacyFlowTests(unittest.TestCase):
    """真实 KtdHttpTransport 编解码 + legacy provider + 旧控制接口。"""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.requests = []
        outer = self

        class Opener:
            def open(self, request, *, timeout):
                path = urlsplit(request.full_url).path
                body = json.loads(request.data) if request.data else None
                outer.requests.append((request.get_method(), path, body))
                if path.endswith("/user/info"):
                    data = {"empId": "actor-1", "permission": ["taskManage:Issued"]}
                elif path.endswith("/uavSite/list"):
                    data = [{"id": "site-1", "uavId": "drone-1", "hangarId": "hangar-1"}]
                elif path.endswith("/uavControlPower/list"):
                    data = [{"employeeId": "actor-1"}]
                elif "/screen/control/permissions/3/" in path:
                    data = {"isHangar": True,
                            "hangarInfo": {"brand": "207601", "model": "DJI3"},
                            "uav": {"UAV_MOVE_DISTANCE": False, "UAV_MOVE_SPEED": True}}
                elif path.endswith("/portal/task/hangar-view/state"):
                    data = {"uavId": "drone-1", "uavState": "5",
                            "inFlight": True, "landing": False}
                elif path.endswith("/screen/uav/camera/control"):
                    data = None
                else:
                    raise AssertionError("unexpected legacy URL " + path)
                response = BytesIO(json.dumps(
                    {"success": True, "code": 200, "data": data}).encode())
                response.code = 200
                return response

        self.opener = Opener()
        self.binding = RuntimeBinding("actor-1", "project-1", "hangar-1", "drone-1", "batch-1", 1)
        self.transport = KtdHttpTransport(
            "https://platform.test/api", token="test-only-token",
            actor_id="actor-1", opener=self.opener)
        self.provider = LegacyKtdSnapshotProvider(
            self.binding, site_id="site-1", task_id=None,
            transport=self.transport, clock=lambda: 1000.0)
        self.executor = KtdPlatformExecutor(
            self.binding, site_id="site-1", task_id=None,
            transport=self.transport, snapshot_provider=self.provider,
            journal=SqliteCommandJournal(str(Path(self.directory.name) / "commands.sqlite3")),
            enabled=True, clock=lambda: 1000.0, legacy_read_api=True,
            allow_unverified_pose=True, track_commands=False)

    def test_whole_legacy_flow_reads_five_get_endpoints_and_posts_old_control(self):
        request = FlightControlActionRequest(
            "command-1", "key-1", "camera_photo", {}, 1)
        card = self.executor.preview(request)
        self.assertIs(card.status, FlightControlStatus.PREVIEW_REQUIRED)
        receipt = self.executor.confirm(
            card.confirmation_id, command_id=card.command_id,
            idempotency_key=card.idempotency_key,
            confirmation_binding=card.confirmation_binding, context_version=1)
        self.assertEqual(receipt.to_dict()["status"], "submitted")
        self.assertFalse(receipt.device_effect_verified)
        paths = [item[1] for item in self.requests]
        self.assertEqual(sum(path.endswith("/user/info") for path in paths), 2)
        self.assertIn("/api/screen/uav/camera/control", paths)
        posts = [item for item in self.requests if item[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2], {"terminalId": "drone-1", "siteId": "site-1", "type": 100})


if __name__ == "__main__":
    unittest.main()
