"""Route lifecycle safety through public simulator preview/confirm APIs only."""

import json
import unittest

from lingxi_flight_control import (
    FlightControlActionRequest,
    FlightControlStatus,
    DecisionKind,
    MemoryFlightControlSimulator,
    ModelDecision,
    RuntimeBinding,
    ToolCall,
)
from lingxi_flight_control.cockpit import CockpitService


class RouteStateSafetyTests(unittest.TestCase):
    def setUp(self):
        self.binding = RuntimeBinding("actor-1", "project-1", "hangar-1", "drone-1", "batch-1", 1)
        self.simulator = MemoryFlightControlSimulator(self.binding, clock=lambda: 100.0)
        self.sequence = 0
        self.execute("control_acquire")

    def preview(self, action, parameters=None):
        self.sequence += 1
        request = FlightControlActionRequest(
            f"route-command-{self.sequence}", f"route-key-{self.sequence}",
            action, parameters or {}, self.binding.context_version,
        )
        return self.simulator.preview(request)

    def confirm(self, card):
        return self.simulator.confirm(
            card.confirmation_id, command_id=card.command_id, idempotency_key=card.idempotency_key,
            confirmation_binding=card.confirmation_binding, context_version=card.context_version,
        )

    def execute(self, action, parameters=None):
        card = self.preview(action, parameters)
        self.assertEqual(card.status, FlightControlStatus.PREVIEW_REQUIRED, card.to_dict())
        result = self.confirm(card)
        self.assertEqual(result.status, FlightControlStatus.ACCEPTED, result.to_dict())
        return result

    def upload(self, route_id="route-1", longitude=113.2):
        return self.execute("upload_route", {
            "route_id": route_id, "waypoints": [{"longitude": longitude, "latitude": 23.1}],
        })

    def prepare(self, state):
        self.upload()
        if state in {"running", "paused"}:
            self.execute("execute_route")
        if state == "paused":
            self.execute("route_pause")

    def assert_preview_rejected_without_change(self, action, code):
        before = self.simulator.snapshot()
        result = self.preview(action)
        self.assertEqual(result.status, FlightControlStatus.REJECTED)
        self.assertEqual(result.error_code, code)
        self.assertIsNone(result.confirmation_id)
        self.assertIsNone(result.confirmation_binding)
        self.assertEqual(self.simulator.snapshot(), before)

    def assert_old_card_rejected_without_change(self, card, code="ROUTE_STATE_CHANGED"):
        before = self.simulator.snapshot()
        result = self.confirm(card)
        self.assertEqual(result.status, FlightControlStatus.REJECTED)
        self.assertEqual(result.error_code, code)
        self.assertEqual(self.simulator.snapshot(), before)
        # A stale card is consumed; restoring a later valid route cannot revive it.
        self.assertEqual(self.confirm(card).error_code, "CONFIRMATION_NOT_FOUND")
        self.assertEqual(self.simulator.snapshot(), before)

    def test_idle_without_a_route_cannot_pause_resume_or_start(self):
        self.assertIsNone(self.simulator.snapshot()["state"]["route_id"])
        for action in ("route_resume", "execute_route", "route_pause"):
            with self.subTest(action=action):
                self.assert_preview_rejected_without_change(action, "ROUTE_NOT_LOADED")

    def test_uploaded_route_cannot_be_paused_or_resumed(self):
        self.prepare("uploaded")
        for action in ("route_pause", "route_resume"):
            with self.subTest(action=action):
                self.assert_preview_rejected_without_change(action, "ROUTE_STATE_MISMATCH")

    def test_running_route_cannot_be_started_or_resumed_again(self):
        self.prepare("running")
        for action in ("execute_route", "route_resume"):
            with self.subTest(action=action):
                self.assert_preview_rejected_without_change(action, "ROUTE_STATE_MISMATCH")

    def test_paused_route_requires_resume_instead_of_start_or_pause(self):
        self.prepare("paused")
        for action in ("execute_route", "route_pause"):
            with self.subTest(action=action):
                self.assert_preview_rejected_without_change(action, "ROUTE_STATE_MISMATCH")

    def test_legal_upload_start_pause_resume_lifecycle_remains_available(self):
        result = self.upload()
        self.assertEqual(result.state["route_status"], "uploaded")
        for action, expected in (("execute_route", "running"), ("route_pause", "paused"),
                                 ("route_resume", "running")):
            with self.subTest(action=action):
                result = self.execute(action)
                self.assertEqual(result.state["route_status"], expected)
                self.assertEqual(result.state["route_id"], "route-1")
                self.assertEqual(result.state["task_status"], "route_executing")
                self.assertEqual(result.state["last_action"], action)

    def test_another_resume_invalidates_an_existing_resume_card(self):
        self.prepare("paused")
        card = self.preview("route_resume")
        self.execute("route_resume")
        self.assert_old_card_rejected_without_change(card)

    def test_route_state_cycle_cannot_restore_an_old_pause_card(self):
        self.prepare("running")
        card = self.preview("route_pause")
        self.execute("route_pause")
        self.execute("route_resume")
        self.assert_old_card_rejected_without_change(card)
        self.execute("route_pause")

    def test_reuploading_same_route_id_invalidates_old_start_card(self):
        self.prepare("uploaded")
        card = self.preview("execute_route")
        self.upload(longitude=113.3)
        self.assert_old_card_rejected_without_change(card)
        self.execute("execute_route")

    def test_replacing_route_invalidates_old_start_card(self):
        self.prepare("uploaded")
        card = self.preview("execute_route")
        self.upload(route_id="route-2")
        self.assert_old_card_rejected_without_change(card)
        result = self.execute("execute_route")
        self.assertEqual(result.state["route_id"], "route-2")

    def test_point_flight_invalidates_pending_route_confirmation(self):
        self.prepare("running")
        card = self.preview("route_pause")
        self.execute("point_flight", {"longitude": 113.2, "latitude": 23.1, "altitude_m": 50})
        self.assert_old_card_rejected_without_change(card)
        for action in ("route_pause", "route_resume", "execute_route"):
            with self.subTest(action=action):
                self.assert_preview_rejected_without_change(action, "ROUTE_STATE_MISMATCH")

    def test_unrelated_camera_action_does_not_invalidate_route_card(self):
        self.prepare("uploaded")
        card = self.preview("execute_route")
        self.execute("camera_photo")
        result = self.confirm(card)
        self.assertEqual(result.status, FlightControlStatus.ACCEPTED)
        self.assertEqual(result.state["route_status"], "running")
        self.assertEqual(result.state["camera"]["photo_count"], 1)

    def test_route_confirmation_rechecks_control_owner(self):
        self.prepare("paused")
        card = self.preview("route_resume")
        self.execute("control_release")
        self.assert_old_card_rejected_without_change(card, "CONTROL_NOT_OWNED")
        self.execute("control_acquire")
        self.execute("route_resume")

    def test_confirmed_route_card_is_idempotent(self):
        self.prepare("uploaded")
        card = self.preview("execute_route")
        self.assertEqual(self.confirm(card).status, FlightControlStatus.ACCEPTED)
        before = self.simulator.snapshot()
        self.assertEqual(self.confirm(card).status, FlightControlStatus.DUPLICATE)
        self.assertEqual(self.simulator.snapshot(), before)


class RouteCockpitSafetyTests(unittest.TestCase):
    """Two browser conversations share one real cockpit service and simulator."""

    REQUESTS = {
        'execute_route': ('执行已经上传的航线', {}),
        'route_pause': ('暂停航线', {}),
        'route_resume': ('恢复航线', {}),
        'emergency_stop': ('急停', {}),
        'land': ('降落', {}),
        'land_alternate': ('备降', {}),
        'return_home': ('返航', {}),
        'cancel_return_home': ('取消返航', {}),
        'takeoff': ('起飞到20米', {'altitude_m': 20}),
        'control_release': ('释放控制权', {}),
        'control_acquire': ('获取控制权', {}),
    }

    def make_service(self, route_state):
        self.sequence = 0
        binding = RuntimeBinding('actor-1', 'project-1', 'hangar-1', 'drone-1', 'batch-1', 1)
        self.simulator = MemoryFlightControlSimulator(binding, clock=lambda: 100.0)
        def seed(action, parameters=None):
            self.sequence += 1
            card = self.simulator.preview(FlightControlActionRequest(
                f'seed-{self.sequence}', f'seed-{self.sequence}', action, parameters or {}, 1))
            self.assertEqual(card.status, FlightControlStatus.PREVIEW_REQUIRED)
            result = self.simulator.confirm(card.confirmation_id, command_id=card.command_id,
                idempotency_key=card.idempotency_key, confirmation_binding=card.confirmation_binding,
                context_version=card.context_version)
            self.assertEqual(result.status, FlightControlStatus.ACCEPTED)
        seed('control_acquire')
        seed('upload_route', {'route_id': 'route-1',
                             'waypoints': [{'longitude': 113.2, 'latitude': 23.1}]})
        if route_state in {'running', 'paused'}:
            seed('execute_route')
        if route_state == 'paused':
            seed('route_pause')
        decisions = {text: (action, parameters)
                     for action, (text, parameters) in self.REQUESTS.items()}
        class ScriptedModel:
            def decide(self, text, checkpoint, tools):
                action, parameters = decisions[json.loads(text)['current_request']]
                return ModelDecision(DecisionKind.TOOL_CALL, tool_call=ToolCall(action, parameters))
        self.service = CockpitService(binding, self.simulator, ScriptedModel, clock=lambda: 100.0)

    def preview(self, session, action):
        self.sequence += 1
        response = self.service.handle('POST', '/api/flight-control/agent/messages', json.dumps({
            'text': self.REQUESTS[action][0], 'request_id': f'cockpit-{self.sequence}',
            'context_version': 1}), {'X-Agent-Session': session})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.payload['status'], 'preview_required', response.payload)
        return response.payload['card']

    def confirm(self, session, card):
        response = self.service.handle('POST', '/api/flight-control/agent/confirm', json.dumps({
            key: card[key] for key in ('confirmation_id', 'confirmation_binding',
                'command_id', 'idempotency_key', 'context_version')}), {'X-Agent-Session': session})
        self.assertEqual(response.status_code, 200)
        return response.payload

    def execute(self, session, action):
        result = self.confirm(session, self.preview(session, action))
        self.assertEqual(result['status'], 'accepted', result)
        return result

    def assert_old_card_rejected(self, card):
        before = self.simulator.snapshot()
        result = self.confirm('route-tab', card)
        self.assertEqual(result['status'], 'rejected', result)
        self.assertEqual(result['error_code'], 'ROUTE_STATE_CHANGED')
        self.assertFalse(result['dispatched'])
        self.assertEqual(self.simulator.snapshot(), before)
        self.assertEqual(self.confirm('route-tab', card), result)
        self.assertEqual(self.simulator.snapshot(), before)

    def test_emergency_stop_in_another_session_invalidates_old_resume_card(self):
        self.make_service('paused')
        old = self.preview('route-tab', 'route_resume')
        stopped = self.execute('other-tab', 'emergency_stop')
        self.assertEqual(stopped['state']['task_status'], 'emergency_stopped')
        self.assert_old_card_rejected(old)
        self.assertEqual(self.simulator.state.route_status, 'paused')

    def test_flight_intent_changes_invalidate_each_kind_of_old_route_card(self):
        for state, action in (('uploaded', 'execute_route'), ('running', 'route_pause'),
                              ('paused', 'route_resume')):
            for override in ('emergency_stop', 'land', 'land_alternate', 'return_home',
                             'cancel_return_home', 'takeoff'):
                with self.subTest(route_state=state, route_action=action, override=override):
                    self.make_service(state)
                    old = self.preview('route-tab', action)
                    self.execute('other-tab', override)
                    self.assert_old_card_rejected(old)

    def test_release_and_reacquire_do_not_restore_old_route_cards(self):
        for state, action in (('uploaded', 'execute_route'), ('running', 'route_pause'),
                              ('paused', 'route_resume')):
            with self.subTest(route_state=state, route_action=action):
                self.make_service(state)
                old = self.preview('route-tab', action)
                released = self.execute('other-tab', 'control_release')
                self.assertFalse(released['state']['control_owned'])
                acquired = self.execute('other-tab', 'control_acquire')
                self.assertTrue(acquired['state']['control_owned'])
                self.assertEqual(acquired['state']['route_status'], state)
                self.assert_old_card_rejected(old)


if __name__ == "__main__":
    unittest.main()
