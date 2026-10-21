import unittest
from io import StringIO
from lingxi_flight_control.v13_extension import Binding, V13Client, V13Facade, parse_command
from tools.v13_event_bridge import events


class V13ExtensionTests(unittest.TestCase):
    def test_grounded_targets_and_no_implicit_mapping(self):
        self.assertEqual(parse_command('去水塔，高50米'), {'action': 'point_flight', 'target': '水塔', 'altitudeM': 50})
        self.assertEqual(parse_command('去水塔'), {'action': 'point_flight', 'target': '水塔'})
        self.assertEqual(parse_command('围绕水塔绕一圈，半径30米，高50米')['radiusM'], 30)
        self.assertEqual(parse_command('围绕水塔绕一圈，半径30米'), {
            'action': 'point_orbit', 'target': '水塔', 'radiusM': 30})
        self.assertEqual(parse_command('去东经118，北纬34，高50米'), {
            'action': 'point_flight', 'target': '坐标目标', 'longitude': 118.0,
            'latitude': 34.0, 'altitudeM': 50.0})
        for text in ('不要去水塔', '网格巡检', '如果安全就飞', '去A再去B', '恢复航线然后拍照',
                     '围绕A再去B绕一圈，半径30米'):
            self.assertIsNone(parse_command(text))

    def test_binding_server_owned_and_confirm_parameters_cannot_be_replaced(self):
        calls = []
        client = V13Client('http://127.0.0.1:1', 'synthetic', 'project', 'actor',
            Binding('site', 'hangar', 'drone', 'task'), transport=lambda method, body: (calls.append((method, body)) or (200, {'status': 'observed'})))
        facade = V13Facade(client)
        facade.handle('messages', {'text': '去水塔，高50米', 'expectedVersion': 1})
        self.assertEqual(calls[0][1]['binding']['droneId'], 'drone')
        self.assertEqual(facade.handle('confirm', {'action': 'end_all'})[0], 400)
        self.assertEqual(len(calls), 1)
        with self.assertRaises(ValueError):
            client.call('../events')

    def test_algorithm_conflict_choice_and_empty_global(self):
        self.assertEqual(parse_command('同时开启AI:smoke,car')['algorithms'], ['smoke', 'car'])
        self.assertEqual(parse_command('仅替换为AI:smoke')['aiMode'], 'replace')
        self.assertEqual(parse_command('关闭全部AI')['algorithms'], [])
        self.assertIsNone(parse_command('打开AI'))

    def test_selected_target_id_is_forwarded_and_annotation_routes_are_explicit(self):
        calls = []
        client = V13Client('http://127.0.0.1:1', 'synthetic', 'project', 'actor',
            Binding('site', 'hangar', 'drone', 'task'),
            transport=lambda method, body: (calls.append((method, body)) or (200, {'status': 'observed'})))
        facade = V13Facade(client)
        facade.handle('messages', {'text': '去水塔，高50米', 'expectedVersion': 3,
                                   'targetId': 'target-1'})
        self.assertEqual(calls[-1][1]['targetId'], 'target-1')
        facade.handle('targets', {'name': '水塔'})
        self.assertEqual(calls[-1][0], 'targets')
        self.assertEqual(calls[-1][1]['name'], '水塔')
        facade.handle('annotation-ai', {'annotationId': 'target-1', 'update': True,
                                        'algorithms': ['smoke']})
        self.assertEqual(calls[-1][0], 'annotation-ai')
        self.assertEqual(calls[-1][1]['binding']['taskId'], 'task')
        facade.handle('orbit-check', {'longitude': 118, 'latitude': 34,
                                      'radiusM': 30, 'altitudeM': 50})
        self.assertEqual(calls[-1][0], 'orbit-check')

    def test_client_rejects_unknown_path_and_invalid_origin(self):
        with self.assertRaises(ValueError):
            V13Client('http://example.com', 'synthetic', 'project', 'actor',
                      Binding('site', 'hangar', 'drone', 'task'))
        client = V13Client('http://127.0.0.1:1', 'synthetic', 'project', 'actor',
                           Binding('site', 'hangar', 'drone', 'task'),
                           transport=lambda method, body: (200, {}))
        with self.assertRaises(ValueError):
            client.call('../../etc/passwd')

    def test_annotation_configuration_can_run_without_task_binding(self):
        calls = []
        client = V13Client('http://127.0.0.1:1', 'synthetic', 'project', 'actor',
                           Binding('site', 'hangar', 'drone'),
                           transport=lambda method, body: (
                               calls.append((method, body)) or (200, {'status': 'observed'})))
        facade = V13Facade(client)
        status, _ = facade.handle('annotation-ai', {
            'annotationId': 'target-1', 'update': True, 'algorithms': []})
        self.assertEqual(status, 200)
        self.assertEqual(calls[-1][1]['binding']['taskId'], '')

    def test_event_bridge_accepts_only_caller_supplied_events(self):
        stream = StringIO(
            '{"type":"position","observedAt":1}\n'
            '{"type":"low_battery_return","eventId":"low-1"}\n')
        self.assertEqual(list(events(stream)), [
            {'type': 'position', 'observedAt': 1},
            {'type': 'low_battery_return', 'eventId': 'low-1'},
        ])
        self.assertEqual(list(events(StringIO(''))), [])
