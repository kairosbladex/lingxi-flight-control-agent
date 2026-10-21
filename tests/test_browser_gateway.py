from io import BytesIO
from pathlib import Path
import json
import tempfile
import unittest
from urllib.parse import urlsplit
from lingxi_flight_control import OllamaDecider, RuntimeBinding
from lingxi_flight_control.browser_gateway import BrowserGateway
from lingxi_flight_control.platform_runtime import PlatformRuntimeConfig
from test_ktd_legacy import FakeLegacyTransport, ok


class OldJava:
    def __init__(self):
        self.source = FakeLegacyTransport()
        self.source.responses['task'] = ok({'taskId': 'task-1', 'terminalId': 'drone-1', 'siteId': 'site-1', 'taskFlag': '2'})
        self.source.responses['permissions'].payload['data'].update(
            loadInfoCode={'1001': True}, load={'optoelecPodParamList': [{'podType': 'wide'}]})
        self.requests = []
        self.posts = []
    def open(self, request, timeout):
        path = urlsplit(request.full_url).path.removeprefix('/gis_server_a')
        self.requests.append((request.get_method(), path))
        if request.get_method() == 'POST':
            self.posts.append((path, json.loads(request.data)))
            packet = {'code': 200}
        else:
            key = {'/user/info':'user_info', '/uavSite/list':'sites', '/uavControlPower/list':'owners',
                   '/portal/task/hangar-view/state':'hangar_state', '/test/getUdpData':'pose',
                   '/screen/control/permissions/3/drone-1':'permissions',
                   '/inspectionResult/task/terminal/detail':'task'}[path]
            packet = self.source.responses[key].payload
        response = BytesIO(json.dumps(packet).encode()); response.code = 200
        return response


class BrowserGatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.java = OldJava()
        self.action = 'camera_photo'
        self.parameters = None
        def model(*args):
            return {'done_reason': 'stop', 'message': {'tool_calls': [{'function': {
                'name': self.action, 'arguments': self.parameters if self.parameters is not None else
                ({'distance_m': 10} if self.action == 'move_forward' else {})}}]}}
        config = PlatformRuntimeConfig('http://127.0.0.1:10086/gis_server_a',
            RuntimeBinding('unbound', 'unbound', 'unbound', 'unbound', 'unbound', 1), 'unbound', None,
            Path(self.tmp.name)/'journal.sqlite', 'unbound', 'synthetic-private-gateway', True)
        self.gateway = BrowserGateway(config, lambda: OllamaDecider(model='fixture', base_url='http://model.invalid',
            transport=model, max_attempts=1), enable_control=True, opener=self.java, clock=lambda:1000,
            allowed_drone_sns=['uav-sn-1'])
        self.headers = {'Authorization':'Bearer synthetic', 'projectId':'project-1',
            'X-Flight-Site':'site-1', 'X-Flight-Drone':'drone-1', 'X-Flight-Task':'task-1',
            'X-Agent-Session':'session-1', 'X-Agent-Actor':'forged', 'X-Agent-Gateway':'forged'}
    def call(self, path='/context', body=None):
        return self.gateway.handle('GET' if body is None else 'POST', '/api/flight-control'+path,
                                   b'' if body is None else json.dumps(body).encode(), self.headers)
    def proposal(self):
        text={'move_forward':'向前飞10米', 'camera_zoom':'光学变焦设置为5倍',
              'gimbal_down':'云台向下调10度',
              'infrared_set_limits':'红外温度下限10摄氏度，上限40摄氏度'}.get(self.action, '拍张照')
        return self.call('/agent/messages', {'text':text, 'request_id':'r-1', 'context_version':1})
    def confirmation(self, card):
        return {k:card[k] for k in ('confirmation_id','confirmation_binding','command_id','idempotency_key','context_version')}
    def test_identity_from_old_java_not_forged_headers(self):
        response = self.call()
        self.assertEqual(response.status_code, 200, response.payload)
        self.assertEqual(response.payload['context']['drone_id'], 'drone-1')
        self.assertEqual(response.payload['execution_mode'], 'platform')
        self.assertFalse(any(method=='POST' for method,path in self.java.requests))
    def test_original_platform_raw_token_and_case_insensitive_bearer(self):
        for token in ('synthetic', 'bearer synthetic'):
            self.headers['Authorization']=token
            self.assertEqual(self.call().status_code,200)
    def test_camera_requires_confirmation_and_exact_replay_does_not_resend(self):
        proposal = self.proposal()
        self.assertEqual(proposal.payload['status'],'preview_required',proposal.payload)
        self.assertFalse(any(m=='POST' for m,p in self.java.requests))
        body = self.confirmation(proposal.payload['card'])
        first = self.call('/agent/confirm',body)
        self.assertEqual(first.payload['status'],'submitted',first.payload)
        self.assertEqual(first.payload['device_effect'],'not_verified')
        self.call('/agent/confirm',body)
        self.assertEqual([p for m,p in self.java.requests if m=='POST'], ['/screen/uav/camera/control'])
    def test_task_end_after_preview_blocks_confirmation(self):
        card = self.proposal().payload['card']
        self.java.source.responses['task'].payload['data']['taskFlag']='3'
        self.assertEqual(self.call('/agent/confirm',self.confirmation(card)).status_code,403)
        self.assertFalse(any(m=='POST' for m,p in self.java.requests))
    def test_owner_change_after_preview_blocks_confirmation(self):
        card=self.proposal().payload['card']
        self.java.source.responses['owners']=ok([{'employeeId':'other'}])
        response=self.call('/agent/confirm',self.confirmation(card))
        self.assertEqual(response.payload['status'],'rejected')
        self.assertFalse(any(m=='POST' for m,p in self.java.requests))
    def test_missing_login_and_foreign_device_fail_before_control(self):
        self.headers['Authorization']=''
        self.assertEqual(self.call().status_code,401)
        self.assertEqual(self.java.requests,[])
        self.headers['Authorization']='Bearer synthetic'; self.headers['X-Flight-Drone']='other'
        self.assertEqual(self.call().status_code,403)
    def test_device_allowlist_is_enforced(self):
        self.java.source.responses['sites'].payload['data'][0]['uavSn']='other-sn'
        self.assertEqual(self.call().payload['error_code'],'DEVICE_OUT_OF_SCOPE')
    def test_actual_site_projection_uses_uav_code_for_serial(self):
        row=self.java.source.responses['sites'].payload['data'][0]
        row['uavCode']=row.pop('uavSn')
        self.assertEqual(self.call().status_code,200)
    def test_no_task_authenticates_then_returns_explicit_waiting_state(self):
        self.headers['X-Flight-Task']=''
        response=self.call()
        self.assertEqual(response.payload['error_code'],'TASK_REQUIRED')
        self.assertTrue(self.java.requests)
    def test_dji_implementation_supports_distance_despite_ui_mode_false(self):
        self.action='move_forward'
        response=self.proposal()
        self.assertEqual(response.payload['status'],'preview_required',response.payload)
        self.assertFalse(any(m=='POST' for m,p in self.java.requests))
        result=self.call('/agent/confirm',self.confirmation(response.payload['card']))
        self.assertEqual(result.payload['status'],'submitted',result.payload)
        path, body=self.java.posts[0]
        self.assertEqual(path,'/screen/uav/operation/control')
        self.assertEqual((body['type'],body['mode'],body['param']),(202,2,10))
    def test_java_camera_and_route_contracts_through_confirmation(self):
        cases = [
            ('camera_start_recording', {}, 'camera', {'type':101}),
            ('camera_stop_recording', {}, 'camera', {'type':102}),
            ('camera_wide', {}, 'camera', {'type':105,'cameraMode':5}),
            ('camera_infrared', {}, 'camera', {'type':105,'cameraMode':2}),
            ('camera_electro_optical', {}, 'camera', {'type':105,'cameraMode':6}),
            ('camera_zoom', {'value':5}, 'camera', {'type':103,'cameraMode':6,'param':5}),
            ('camera_panorama', {}, 'camera', {'type':125}),
            ('infrared_set_limits', {'min_c':10,'max_c':40}, 'camera', {'type':127,
                'jsonParams':'{"lowerLimit":10,"openIsotherm":true,"upperLimit":40}'}),
            ('route_pause', {}, 'route', {'type':110}),
            ('route_resume', {}, 'route', {'type':112}),
            ('emergency_stop', {}, 'route', {'type':113}),
            ('return_home', {}, 'route', {'type':114,'interrupt':False}),
            ('cancel_return_home', {}, 'route', {'type':-114}),
        ]
        for index,(action,parameters,endpoint,expected) in enumerate(cases):
            with self.subTest(action=action):
                self.headers['X-Agent-Session']=f'case-{index}'
                self.action,self.parameters=action,parameters
                before=len(self.java.posts)
                proposal=self.proposal()
                self.assertEqual(proposal.payload['status'],'preview_required',proposal.payload)
                self.assertEqual(len(self.java.posts),before)
                confirmation=self.confirmation(proposal.payload['card'])
                result=self.call('/agent/confirm',confirmation)
                self.assertEqual(result.payload['status'],'submitted',result.payload)
                self.assertEqual(result.payload['device_effect'],'not_verified')
                self.assertEqual(self.java.posts[-1],(f'/screen/uav/{endpoint}/control',
                    {'terminalId':'drone-1','siteId':'site-1','taskId':'task-1',**expected}))
                self.call('/agent/confirm',confirmation)
                self.assertEqual(len(self.java.posts),before+1)
    def test_verified_distance_does_not_need_unverified_pose_switch(self):
        self.action='move_forward'
        self.java.source.responses['permissions'].payload['data']['uav']['UAV_MOVE_DISTANCE']=True
        response=self.proposal()
        self.assertEqual(response.payload['status'],'preview_required',response.payload)
    def test_stale_pose_prevents_movement(self):
        self.action='move_forward'
        self.java.source.responses['permissions'].payload['data']['uav']['UAV_MOVE_DISTANCE']=True
        self.java.source.responses['pose'].payload['data']['timeStamp']=900
        response=self.proposal()
        self.assertEqual(response.payload['status'],'rejected')
        self.assertFalse(any(m=='POST' for m,p in self.java.requests))
    def test_pose_expiry_after_preview_blocks_distance_confirmation(self):
        self.action='move_forward'
        card=self.proposal().payload['card']
        self.java.source.responses['pose'].payload['data']['timeStamp']=900
        result=self.call('/agent/confirm',self.confirmation(card))
        self.assertEqual(result.payload['status'],'rejected')
        self.assertEqual(self.java.posts,[])
    def test_unknown_profile_does_not_inherit_dji_distance(self):
        self.action='move_forward'
        self.java.source.responses['permissions'].payload['data']['hangarInfo']['brand']='unknown'
        response=self.proposal()
        self.assertNotEqual(response.payload.get('status'),'preview_required')
        self.assertEqual(self.java.posts,[])
    def test_strawberry_brand_uses_verified_dji_distance_implementation(self):
        self.action='move_forward'
        self.java.source.responses['permissions'].payload['data']['hangarInfo']['brand']='207616'
        response=self.proposal()
        self.assertEqual(response.payload['status'],'preview_required',response.payload)
        self.assertEqual(self.java.posts,[])
    def test_missing_camera_blocks_new_camera_actions(self):
        self.action='camera_start_recording'
        self.java.source.responses['permissions'].payload['data']['loadInfoCode']={}
        response=self.proposal()
        self.assertEqual(response.payload['status'],'rejected')
        self.assertEqual(self.java.posts,[])
    def test_temperature_grounding_preserves_roles(self):
        from lingxi_flight_control.cockpit import grounded_parameters
        phrase='红外温度下限10摄氏度，上限40摄氏度'
        self.assertTrue(grounded_parameters('infrared_set_limits',{'min_c':10,'max_c':40},phrase))
        self.assertFalse(grounded_parameters('infrared_set_limits',{'min_c':40,'max_c':10},phrase))
        self.assertFalse(grounded_parameters('infrared_set_limits',{'min_c':10,'max_c':40},'温度10度和40度'))
    def test_numeric_gimbal_does_not_silently_execute_a_speed_or_default_step(self):
        self.action='gimbal_down'
        self.parameters={'angle_deg':10}
        result=self.proposal()
        self.assertEqual(result.payload['status'],'rejected',result.payload)
        self.assertEqual(result.payload['error_code'],'GIMBAL_EXACT_ANGLE_UNAVAILABLE')
        self.assertEqual(self.java.posts,[])
    def test_model_cannot_drop_explicit_gimbal_angle(self):
        self.action='gimbal_down'
        self.parameters={}
        result=self.proposal()
        self.assertEqual(result.payload['status'],'clarification',result.payload)
        self.assertEqual(self.java.posts,[])
    def test_unexpected_confirmation_failure_does_not_claim_unsent(self):
        def failed(*args): raise RuntimeError('private error')
        self.gateway.runtime.handle=failed
        response=self.call('/agent/confirm',{})
        self.assertEqual(response.status_code,503)
        self.assertEqual(response.payload['error_code'],'PLATFORM_OUTCOME_UNKNOWN')
        self.assertNotIn('dispatched',response.payload)
        self.assertNotIn('private error',str(response.payload))
