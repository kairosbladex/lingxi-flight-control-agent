import unittest
from lingxi_flight_control.cockpit import grounded_parameters
from lingxi_flight_control.full_control import FlightControlActionRequest, RuntimeBinding, MemoryFlightControlSimulator


class PreciseGimbalIntentTests(unittest.TestCase):
    def test_simulator_preserves_angle_or_explicitly_previews_v13_adjustment(self):
        sim=MemoryFlightControlSimulator(RuntimeBinding('actor','project','hangar','drone','batch',1))
        sim.state.control_owner='actor'
        sim.state.gimbal_pitch_deg=-20
        request=FlightControlActionRequest('command','key','gimbal_down',{'angle_deg':10},1)
        card=sim.preview(request)
        receipt=sim.confirm(card.confirmation_id, command_id='command',idempotency_key='key',
            confirmation_binding=card.confirmation_binding,context_version=1)
        self.assertEqual(receipt.status.value,'accepted')
        self.assertEqual(sim.state.gimbal_pitch_deg,-30)
        sim.state.gimbal_pitch_deg=-85
        bad=sim.preview(FlightControlActionRequest('next','next','gimbal_down',{'angle_deg':10},1))
        self.assertEqual(bad.status.value,'preview_required')
        self.assertEqual(bad.parameters, {'angle_deg': 5})
        self.assertEqual(bad.adjustments[0]['requested'], 10)
        self.assertEqual(sim.state.gimbal_pitch_deg,-85)

    def test_relative_direction_and_number_must_match(self):
        for phrase in ('云台向下调10度', '云台调低10度', '云台往下调整十度'):
            with self.subTest(phrase=phrase):
                self.assertTrue(grounded_parameters('gimbal_down', {'angle_deg':10}, phrase))
                self.assertFalse(grounded_parameters('gimbal_down', {'angle_deg':5}, phrase))
                self.assertFalse(grounded_parameters('gimbal_up', {'angle_deg':10}, phrase))
                self.assertFalse(grounded_parameters('gimbal_down', {}, phrase))

    def test_absolute_and_ambiguous_requests_do_not_become_relative(self):
        for phrase in ('云台调到负10度', '云台设为俯角10度', '云台向下调低一些',
                       '云台向下调5到10度', '云台向下调约10度'):
            self.assertFalse(grounded_parameters('gimbal_down', {'angle_deg':10}, phrase), phrase)
        phrase='飞机左转10度，云台向下调5度'
        self.assertFalse(grounded_parameters('gimbal_down', {'angle_deg':10}, phrase))
        self.assertTrue(grounded_parameters('gimbal_down', {'angle_deg':5}, phrase))

    def test_numeric_contract_preserves_value_and_rejects_invalid_values(self):
        for angle in (0.1, 10, 90):
            request=FlightControlActionRequest('command','key','gimbal_down',{'angle_deg':angle},1)
            request.validate()
            self.assertEqual(request.parameters['angle_deg'],angle)
        for angle in (0, -10, 91, True, float('nan'), float('inf')):
            with self.subTest(angle=angle), self.assertRaises(ValueError):
                FlightControlActionRequest('command','key','gimbal_down',{'angle_deg':angle},1).validate()
