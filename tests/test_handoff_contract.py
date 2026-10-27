"""V1.4 跨仓交接契约：白名单、版本、绑定、几何与交付阶段。

本文件不访问网络、不连接地图标注服务，也不产生任何真实飞控指令。
"""
import copy
import unittest

from lingxi_flight_control.full_control import FlightControlAction, RuntimeBinding
from lingxi_flight_control.handoff_contract import (
    ACTION_UNSUPPORTED, ALGORITHM_NOT_CONFIGURED, BINDING_INCOMPLETE, BINDING_MISMATCH,
    CONTRACT_VERSION, CROSS_TYPE_FORBIDDEN, DRAFT_NOT_DISPATCHABLE, DeliveryStage,
    FIELD_NOT_ALLOWED, IDEMPOTENCY_CONFLICT, MAPPING_NOT_RESUMABLE,
    MULTI_GEOMETRY_UNSUPPORTED, MIXED_TARGET_UNSUPPORTED, SCHEMA_UNSUPPORTED,
    SNAPSHOT_STALE, STALE_CONTEXT, TARGET_GEOMETRY_UNSUPPORTED, TARGET_ORDER_INVALID,
    TARGET_VERSION_UNAVAILABLE, HandoffError, HandoffLedger, delivery_stage,
    device_effect_verified, parse_platform_unavailable, to_action_request,
    to_runtime_binding, unavailable_code_for, validate_envelope, verify_snapshot,
)

BINDING = RuntimeBinding(actor_id="emp-1", project_id="proj-1", hangar_id="hangar-1",
                         drone_id="drone-1", task_batch_id="batch-1", context_version=7)


def point(target_id="t-1", name="1号水塔", order=1, **extra):
    value = {"target_id": target_id, "name": name, "geometry_type": "point",
             "longitude": 118.1, "latitude": 34.2, "altitude_m": 50.0,
             "version": "v1", "source": "static-annotation", "order": order,
             "project_id": "proj-1"}
    value.update(extra)
    return value


def line(target_id="t-line", order=1, **extra):
    value = {"target_id": target_id, "name": "B河道", "geometry_type": "line",
             "version": "v1", "source": "static-annotation", "order": order,
             "project_id": "proj-1"}
    value.update(extra)
    return value


def base(**overrides):
    payload = {
        "schema_version": CONTRACT_VERSION,
        "kind": "flight_control",
        "trace_id": "trace-1",
        "command_id": "cmd-1",
        "idempotency_key": "idem-1",
        "binding": {"actor_id": "emp-1", "project_id": "proj-1", "site_id": "site-1",
                    "hangar_id": "hangar-1", "drone_id": "drone-1", "task_id": "task-1",
                    "task_batch_id": "batch-1", "context_version": 7,
                    "snapshot_version": "snap-1"},
        "intent": "point_flight",
        "task": {"task_type": "inspection"},
        "targets": [point()],
        "action": {"name": "point_flight",
                   "parameters": {"longitude": 118.1, "latitude": 34.2, "altitude_m": 50.0}},
    }
    payload.update(copy.deepcopy(overrides))
    return payload


def dispatch(**overrides):
    payload = base(kind="task_dispatch", intent="ground_plan",
                   targets=[point(order=1), point("t-2", "2号水塔", order=2)],
                   task={"task_type": "inspection"},
                   draft={"confirmed": True, "dispatch_id": "dispatch-1", "revision": 3})
    payload.pop("action")
    payload.update(copy.deepcopy(overrides))
    return payload


def check(payload, **kwargs):
    options = {"expected_binding": BINDING, "site_id": "site-1", "task_id": "task-1"}
    options.update(kwargs)
    return validate_envelope(payload, **options)


class EnvelopeBoundaryTest(unittest.TestCase):
    def test_valid_flight_control_handoff_maps_to_existing_request(self):
        envelope = check(base())
        self.assertFalse(envelope.creates_task())
        request = to_action_request(envelope)
        self.assertEqual(request.action, FlightControlAction.POINT_FLIGHT)
        self.assertEqual(request.parameters["altitude_m"], 50.0)
        self.assertEqual(to_runtime_binding(envelope), BINDING)

    def test_task_dispatch_is_the_only_kind_that_creates_a_task(self):
        envelope = check(dispatch())
        self.assertTrue(envelope.creates_task())
        with self.assertRaises(HandoffError) as caught:
            to_action_request(envelope)
        self.assertEqual(caught.exception.code, ACTION_UNSUPPORTED)

    def test_unknown_envelope_field_is_rejected(self):
        payload = base()
        payload["route"] = {"waypoints": []}
        with self.assertRaises(HandoffError) as caught:
            check(payload)
        self.assertEqual(caught.exception.code, FIELD_NOT_ALLOWED)
        self.assertEqual(caught.exception.http_status, 400)

    def test_unknown_binding_field_is_rejected(self):
        payload = base()
        payload["binding"]["tenant"] = "x"
        with self.assertRaises(HandoffError) as caught:
            check(payload)
        self.assertEqual(caught.exception.code, FIELD_NOT_ALLOWED)

    def test_schema_version_is_exact(self):
        with self.assertRaises(HandoffError) as caught:
            check(base(schema_version="lingxi-flight-handoff/v0"))
        self.assertEqual(caught.exception.code, SCHEMA_UNSUPPORTED)

    def test_missing_identity_is_incomplete_not_defaulted(self):
        payload = base()
        payload["binding"].pop("drone_id")
        with self.assertRaises(HandoffError) as caught:
            check(payload)
        self.assertEqual(caught.exception.code, BINDING_INCOMPLETE)
        self.assertIn("drone_id", caught.exception.fields)

    def test_trace_id_is_required(self):
        with self.assertRaises(HandoffError) as caught:
            check(base(trace_id=""))
        self.assertEqual(caught.exception.code, BINDING_INCOMPLETE)

    def test_other_drone_binding_is_conflict(self):
        payload = base()
        payload["binding"]["drone_id"] = "drone-2"
        with self.assertRaises(HandoffError) as caught:
            check(payload)
        self.assertEqual(caught.exception.code, BINDING_MISMATCH)
        self.assertEqual(caught.exception.http_status, 409)

    def test_other_task_batch_is_conflict(self):
        payload = base()
        payload["binding"]["task_batch_id"] = "batch-2"
        with self.assertRaises(HandoffError) as caught:
            check(payload)
        self.assertEqual(caught.exception.code, BINDING_MISMATCH)

    def test_stale_context_version_is_business_409(self):
        payload = base()
        payload["binding"]["context_version"] = 6
        with self.assertRaises(HandoffError) as caught:
            check(payload)
        self.assertEqual(caught.exception.code, STALE_CONTEXT)
        self.assertEqual(caught.exception.http_status, 409)

    def test_snapshot_version_must_match_authoritative_snapshot(self):
        envelope = check(base())

        class Snapshot:
            version = "snap-2"
            context_version = 7

        with self.assertRaises(HandoffError) as caught:
            verify_snapshot(envelope, Snapshot())
        self.assertEqual(caught.exception.code, SNAPSHOT_STALE)

        class Current:
            version = "snap-1"
            context_version = 7

        verify_snapshot(envelope, Current())


class DraftAndIdempotencyTest(unittest.TestCase):
    def test_unconfirmed_draft_is_never_a_dispatched_task(self):
        payload = dispatch(draft={"confirmed": False, "revision": 3})
        with self.assertRaises(HandoffError) as caught:
            check(payload)
        self.assertEqual(caught.exception.code, DRAFT_NOT_DISPATCHABLE)

    def test_dispatch_without_dispatch_id_is_rejected(self):
        payload = dispatch(draft={"confirmed": True, "revision": 3})
        with self.assertRaises(HandoffError) as caught:
            check(payload)
        self.assertEqual(caught.exception.code, DRAFT_NOT_DISPATCHABLE)

    def test_same_key_same_body_is_duplicate_and_different_body_conflicts(self):
        ledger = HandoffLedger()
        first = check(base())
        self.assertEqual(ledger.admit(first), "new")
        self.assertEqual(ledger.admit(check(base(trace_id="trace-2"))), "duplicate")
        with self.assertRaises(HandoffError) as caught:
            ledger.admit(check(base(targets=[point(name="2号水塔")])))
        self.assertEqual(caught.exception.code, IDEMPOTENCY_CONFLICT)


class TargetGeometryTest(unittest.TestCase):
    def test_multi_point_dispatch_keeps_user_order(self):
        envelope = check(dispatch())
        self.assertEqual([item.target_id for item in envelope.targets], ["t-1", "t-2"])
        self.assertEqual([item.order for item in envelope.targets], [1, 2])

    def test_reordered_multi_point_is_rejected_instead_of_silently_sorted(self):
        payload = dispatch(targets=[point("t-1", "1号水塔", order=2), point("t-2", "2号水塔", order=1)])
        with self.assertRaises(HandoffError) as caught:
            check(payload)
        self.assertEqual(caught.exception.code, TARGET_ORDER_INVALID)

    def test_multi_point_without_explicit_order_is_rejected(self):
        payload = dispatch(targets=[point(order=None), point("t-2", "2号水塔", order=2)])
        with self.assertRaises(HandoffError) as caught:
            check(payload)
        self.assertEqual(caught.exception.code, TARGET_ORDER_INVALID)

    def test_line_target_never_enters_the_flight_point_interface(self):
        payload = base(intent="point_orbit", targets=[line()],
                       action={"name": "point_orbit",
                               "parameters": {"longitude": 118.1, "latitude": 34.2, "radius_m": 30}})
        with self.assertRaises(HandoffError) as caught:
            check(payload)
        self.assertEqual(caught.exception.code, TARGET_GEOMETRY_UNSUPPORTED)
        self.assertEqual(caught.exception.http_status, 422)

    def test_polygon_target_is_rejected_for_flight_control(self):
        payload = base(targets=[point(geometry_type="polygon")])
        with self.assertRaises(HandoffError) as caught:
            check(payload)
        self.assertEqual(caught.exception.code, TARGET_GEOMETRY_UNSUPPORTED)

    def test_volume_target_is_rejected_everywhere(self):
        payload = dispatch(targets=[point(geometry_type="volume")])
        with self.assertRaises(HandoffError) as caught:
            check(payload)
        self.assertEqual(caught.exception.code, TARGET_GEOMETRY_UNSUPPORTED)
        self.assertEqual(caught.exception.detail, "volume")

    def test_mixed_targets_are_rejected(self):
        payload = dispatch(targets=[point(order=1), line(order=2)])
        with self.assertRaises(HandoffError) as caught:
            check(payload)
        self.assertEqual(caught.exception.code, MIXED_TARGET_UNSUPPORTED)

    def test_two_lines_are_rejected(self):
        payload = dispatch(targets=[line("t-a", 1), line("t-b", 2)])
        with self.assertRaises(HandoffError) as caught:
            check(payload)
        self.assertEqual(caught.exception.code, MULTI_GEOMETRY_UNSUPPORTED)

    def test_flight_control_accepts_one_point_only(self):
        payload = base(targets=[point(order=1), point("t-2", "2号水塔", order=2)])
        with self.assertRaises(HandoffError) as caught:
            check(payload)
        self.assertEqual(caught.exception.code, MULTI_GEOMETRY_UNSUPPORTED)

    def test_target_without_version_is_rejected(self):
        payload = base(targets=[point(version=None)])
        with self.assertRaises(HandoffError) as caught:
            check(payload)
        self.assertEqual(caught.exception.code, TARGET_VERSION_UNAVAILABLE)

    def test_candidate_list_cannot_be_smuggled_into_the_envelope(self):
        payload = base()
        payload["targets"][0]["candidates"] = [point("t-2", "1号水塔")]
        with self.assertRaises(HandoffError) as caught:
            check(payload)
        self.assertEqual(caught.exception.code, FIELD_NOT_ALLOWED)


class TaskAndActionTest(unittest.TestCase):
    def test_invalidated_mapping_cannot_be_resumed(self):
        payload = base(action={"name": "route_resume", "parameters": {}},
                       task={"task_type": "mapping", "mapping_invalidated": True})
        with self.assertRaises(HandoffError) as caught:
            check(payload)
        self.assertEqual(caught.exception.code, MAPPING_NOT_RESUMABLE)

    def test_route_resume_disabled_blocks_resume(self):
        payload = base(action={"name": "continue_from_breakpoint", "parameters": {"index": 3}},
                       task={"task_type": "inspection", "route_resume_disabled": True})
        with self.assertRaises(HandoffError) as caught:
            check(payload)
        self.assertEqual(caught.exception.code, MAPPING_NOT_RESUMABLE)

    def test_unknown_action_is_rejected(self):
        payload = base(action={"name": "spiral_fly", "parameters": {}})
        with self.assertRaises(HandoffError) as caught:
            check(payload)
        self.assertEqual(caught.exception.code, ACTION_UNSUPPORTED)

    def test_unknown_parameter_is_rejected_by_the_shared_whitelist(self):
        payload = base(action={"name": "move_forward",
                               "parameters": {"distance_m": 10, "speed_mps": 99}})
        with self.assertRaises(HandoffError) as caught:
            check(payload)
        self.assertEqual(caught.exception.code, ACTION_UNSUPPORTED)

    def test_dispatch_cannot_carry_a_flight_action(self):
        payload = dispatch()
        payload["action"] = {"name": "move_forward", "parameters": {"distance_m": 1}}
        with self.assertRaises(HandoffError) as caught:
            check(payload)
        self.assertEqual(caught.exception.code, FIELD_NOT_ALLOWED)

    def test_mapping_task_cannot_be_started_while_inspection_is_running(self):
        payload = dispatch(task={"task_type": "mapping"})
        with self.assertRaises(HandoffError) as caught:
            check(payload, current_task_type="inspection")
        self.assertEqual(caught.exception.code, CROSS_TYPE_FORBIDDEN)
        self.assertEqual(caught.exception.http_status, 409)

    def test_mapping_dispatch_is_allowed_when_no_inspection_is_running(self):
        envelope = check(dispatch(task={"task_type": "mapping"}), current_task_type=None)
        self.assertEqual(envelope.task.task_type, "mapping")

    def test_annotation_algorithm_must_be_in_project_configuration(self):
        payload = base(algorithms={"configured": ["fire"], "global": [],
                                   "annotation": {"t-1": ["drowning"]}})
        with self.assertRaises(HandoffError) as caught:
            check(payload)
        self.assertEqual(caught.exception.code, ALGORITHM_NOT_CONFIGURED)

    def test_annotation_algorithm_for_foreign_target_is_rejected(self):
        payload = base(algorithms={"configured": ["fire"], "global": [],
                                   "annotation": {"t-9": ["fire"]}})
        with self.assertRaises(HandoffError) as caught:
            check(payload)
        self.assertEqual(caught.exception.code, ALGORITHM_NOT_CONFIGURED)


class DeliveryStageTest(unittest.TestCase):
    def test_accepted_and_submitted_are_not_device_completion(self):
        for status in ("accepted", "submitted", "dispatching", "preview_required", "draft"):
            stage = delivery_stage(status)
            self.assertFalse(device_effect_verified(stage))

    def test_unknown_wire_status_stays_unknown(self):
        self.assertIs(delivery_stage("someday"), DeliveryStage.UNKNOWN)
        self.assertIs(delivery_stage(None), DeliveryStage.UNKNOWN)
        self.assertFalse(device_effect_verified(DeliveryStage.UNKNOWN))

    def test_device_receipt_is_still_not_a_flight_effect_proof(self):
        self.assertIs(delivery_stage("completed"), DeliveryStage.DEVICE_RECEIPT)
        self.assertFalse(device_effect_verified(DeliveryStage.DEVICE_RECEIPT))


class PlatformUnavailableTest(unittest.TestCase):
    def test_java_declared_unavailable_becomes_a_typed_action_code(self):
        data = {"contract": {"schema_version": "lingxi-platform-contract/v1",
                             "unavailable": {"route_status": "ROUTE_STATE_SOURCE_UNAVAILABLE",
                                             "target_binding": "STATIC_ANNOTATION_SOURCE_UNAVAILABLE"}}}
        unavailable = parse_platform_unavailable(data)
        self.assertEqual(unavailable_code_for("hover", unavailable), "ROUTE_STATE_SOURCE_UNAVAILABLE")
        self.assertEqual(unavailable_code_for("point_flight", unavailable),
                         "STATIC_ANNOTATION_SOURCE_UNAVAILABLE")
        self.assertIsNone(unavailable_code_for("camera_photo", unavailable))

    def test_unknown_capability_name_is_kept_but_not_mapped(self):
        # 新版本 Java 多声明一条本端还不认识的缺口，不应让整份快照失效；
        # 它只是不会被映射到具体动作，本端权威字段校验照常执行。
        data = {"contract": {"schema_version": "lingxi-platform-contract/v1",
                             "unavailable": {"telepathy": "TELEPATHY_UNAVAILABLE",
                                             "route_status": "ROUTE_STATE_SOURCE_UNAVAILABLE"}}}
        unavailable = parse_platform_unavailable(data)
        self.assertIn("telepathy", unavailable)
        self.assertIsNone(unavailable_code_for("camera_photo", unavailable))
        self.assertEqual(unavailable_code_for("hover", unavailable), "ROUTE_STATE_SOURCE_UNAVAILABLE")

    def test_malformed_capability_entry_is_rejected(self):
        for capability, code in (("", "X_UNAVAILABLE"), ("route_status", "unavailable"),
                                 ("route_status", ""), (1, "X_UNAVAILABLE")):
            with self.subTest(capability=capability, code=code):
                data = {"contract": {"schema_version": "lingxi-platform-contract/v1",
                                     "unavailable": {capability: code}}}
                with self.assertRaises(HandoffError) as caught:
                    parse_platform_unavailable(data)
                self.assertEqual(caught.exception.code, SCHEMA_UNSUPPORTED)

    def test_absent_contract_block_means_no_claim(self):
        self.assertEqual(dict(parse_platform_unavailable({"state": {}})), {})


if __name__ == "__main__":
    unittest.main()
