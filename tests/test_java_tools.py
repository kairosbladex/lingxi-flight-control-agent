import unittest
from lingxi_flight_control.java_tools import TOOLS, JavaActionRequest, encode, grounded
from lingxi_flight_control.cockpit import build_proposal_registry
from lingxi_flight_control.full_control import MemoryFlightControlSimulator, RuntimeBinding


class JavaToolTests(unittest.TestCase):
    def setUp(self):
        from test_browser_gateway import BrowserGatewayTests
        self.fixture=BrowserGatewayTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_all_tools_require_confirmation_and_replay_never_resends(self):
        for index,(name,spec) in enumerate(TOOLS.items()):
            with self.subTest(name=name):
                f=self.fixture
                f.headers['X-Agent-Session']=f'java-tool-{index}'
                f.action,f.parameters=name,dict(spec.arguments)
                before=len(f.java.posts)
                proposal=f.call('/agent/messages',{'text':spec.example,'request_id':'request1','context_version':1})
                self.assertEqual(proposal.payload['status'],'preview_required',proposal.payload)
                self.assertEqual(len(f.java.posts),before)
                confirmation=f.confirmation(proposal.payload['card'])
                response=f.call('/agent/confirm',confirmation)
                self.assertEqual(response.payload['status'],'submitted',response.payload)
                self.assertEqual(response.payload['device_effect'],'not_verified')
                self.assertEqual(len(f.java.posts),before+1)
                self.assertEqual(f.java.posts[-1][0],spec.endpoint)
                self.assertEqual(f.java.posts[-1][1]['terminalId'],'drone-1')
                f.call('/agent/confirm',confirmation)
                self.assertEqual(len(f.java.posts),before+1)

    def test_explicit_java_wire_contracts(self):
        cases=[
            ('camera_live_quality',{'quality':'hd'},{'type':106,'param':3}),
            ('camera_photo_storage',{'lens':'all'},{'type':109,'storageSettings':'wide,zoom,ir'}),
            ('camera_video_storage',{'lens':'wide'},{'type':110,'storageSettings':'wide'}),
            ('camera_night_mode',{'mode':'auto'},{'type':111,'param':2}),
            ('camera_denoise',{'level':'strong'},{'type':112,'param':3}),
            ('camera_night_vision',{'enabled':True},{'type':113,'param':1}),
            ('camera_infrared_light',{'enabled':False},{'type':114,'param':0}),
            ('thermal_gain',{'mode':'low'},{'type':126,'param':1}),
            ('thermal_palette',{'style':'lava'},{'type':123,'param':11}),
            ('thermal_split',{'enabled':False},{'type':128,'param':0}),
            ('camera_point_focus',{'lens':'zoom','x':0.2,'y':0.4},
                {'type':108,'jsonParams':'{"cameraType":"zoom","x":0.2,"y":0.4}'}),
            ('gimbal_yaw_center',{}, {'type':405}),
            ('gimbal_free_mode',{}, {'type':420}),
            ('gimbal_follow_mode',{}, {'type':422}),
            ('uav_night_light',{'enabled':True},{'type':208,'param':1}),
            ('uav_stealth',{'enabled':False},{'type':209,'param':0}),
            ('point_flight_stop',{}, {'type':120}),
            ('point_orbit_stop',{}, {'type':122}),
        ]
        for name,parameters,expected in cases:
            self.assertEqual(encode(JavaActionRequest('cmd','key',name,parameters,1))[1],expected,name)

    def test_resource_injection_bad_enum_and_geometry_are_rejected(self):
        for name,params in [
            ('camera_night_mode',{'mode':'on','terminalId':'other'}),
            ('camera_night_mode',{'mode':'http://untrusted'}),
            ('thermal_split',{'enabled':1}),
            ('camera_point_focus',{'lens':'wide','x':float('nan'),'y':0.2}),
            ('thermal_metering_area',{'x':0.9,'y':0.1,'width':0.2,'height':0.3}),
            ('thermal_metering_area',{'x':0.1,'y':0.1,'width':0,'height':0.3})]:
            with self.subTest(name=name),self.assertRaises(ValueError):
                JavaActionRequest('cmd','key',name,params,1).validate()

    def test_model_coordinate_roles_cannot_be_swapped(self):
        text='广角镜头单点对焦x=0.2，y=0.4'
        self.assertTrue(grounded('camera_point_focus',{'lens':'wide','x':0.2,'y':0.4},text))
        self.assertFalse(grounded('camera_point_focus',{'lens':'wide','x':0.4,'y':0.2},text))
        self.assertFalse(grounded('camera_night_mode',{'mode':'on'},'不要开启夜景'))
        self.assertFalse(grounded('camera_aim',{'lens':'wide','x':0.2,'y':0.4,'locked':True},text+'，不锁定机头'))

    def test_original_simulator_tool_surface_does_not_gain_platform_tools(self):
        simulator=MemoryFlightControlSimulator(RuntimeBinding('a','p','h','d','b',1))
        names={tool.name for tool in build_proposal_registry(simulator).definitions()}
        self.assertEqual(len(names),59)
        self.assertFalse(names.intersection(TOOLS))

    def test_no_camera_means_no_new_camera_write(self):
        f=self.fixture
        f.action='camera_night_mode'; f.parameters={'mode':'on'}
        f.java.source.responses['permissions'].payload['data']['loadInfoCode']={}
        response=f.call('/agent/messages',{'text':'开启夜景模式','request_id':'r','context_version':1})
        self.assertEqual(response.payload['status'],'rejected',response.payload)
        self.assertEqual(f.java.posts,[])

    def test_saved_receipt_reconstructs_the_same_typed_request(self):
        from lingxi_flight_control.ktd_platform import KtdPlatformExecutor, KtdReceipt
        request=JavaActionRequest('cmd','key','camera_night_mode',{'mode':'on'},1)
        receipt=KtdReceipt('submitted',request.action,'cmd','key',1,{},parameters=request.parameters)
        restored=KtdPlatformExecutor._request_from_receipt(receipt)
        self.assertIsInstance(restored,JavaActionRequest)
        self.assertEqual(restored.to_dict(),request.to_dict())

    def test_java_integer_zoom_is_not_silently_truncated(self):
        f=self.fixture
        f.action='camera_zoom';f.parameters={'value':5.5}
        result=f.call('/agent/messages',{'text':'光学变焦设置为5.5倍','request_id':'r','context_version':1})
        self.assertEqual(result.payload['error_code'],'INTEGER_ZOOM_REQUIRED',result.payload)
        self.assertEqual(f.java.posts,[])
