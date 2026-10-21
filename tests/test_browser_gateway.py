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
    def open(self, request, timeout):
        path = urlsplit(request.full_url).path.removeprefix('/gis_server_a')
        self.requests.append((request.get_method(), path))
        if request.get_method() == 'POST':
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
        def model(*args):
            return {'done_reason': 'stop', 'message': {'tool_calls': [{'function': {
                'name': self.action, 'arguments': {'distance_m': 10} if self.action == 'move_forward' else {}}}]}}
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
        return self.call('/agent/messages', {'text':'向前飞10米' if self.action == 'move_forward' else '拍张照', 'request_id':'r-1', 'context_version':1})
    def confirmation(self, card):
        return {k:card[k] for k in ('confirmation_id','confirmation_binding','command_id','idempotency_key','context_version')}
    def test_identity_from_old_java_not_forged_headers(self):
        response = self.call()
        self.assertEqual(response.status_code, 200, response.payload)
        self.assertEqual(response.payload['context']['drone_id'], 'drone-1')
        self.assertEqual(response.payload['execution_mode'], 'platform')
        self.assertFalse(any(method=='POST' for method,path in self.java.requests))
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
    def test_no_task_authenticates_then_returns_explicit_waiting_state(self):
        self.headers['X-Flight-Task']=''
        response=self.call()
        self.assertEqual(response.payload['error_code'],'TASK_REQUIRED')
        self.assertTrue(self.java.requests)
    def test_distance_false_is_not_overridden(self):
        self.action='move_forward'
        response=self.proposal()
        self.assertEqual(response.payload['status'],'rejected')
        self.assertFalse(any(m=='POST' for m,p in self.java.requests))
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
    def test_unexpected_confirmation_failure_does_not_claim_unsent(self):
        def failed(*args): raise RuntimeError('private error')
        self.gateway.runtime.handle=failed
        response=self.call('/agent/confirm',{})
        self.assertEqual(response.status_code,503)
        self.assertEqual(response.payload['error_code'],'PLATFORM_OUTCOME_UNKNOWN')
        self.assertNotIn('dispatched',response.payload)
        self.assertNotIn('private error',str(response.payload))
