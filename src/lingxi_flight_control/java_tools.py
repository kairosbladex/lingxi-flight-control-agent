"""原Java已有的具名能力。每个工具使用固定端点/操作码，不接受模型自报资源或原始JSON。"""
from dataclasses import dataclass, field
import json
import math
import re
from .full_control import FlightControlActionRequest
from .models import ToolDefinition, ToolParameter, ToolSchema


@dataclass(frozen=True)
class JavaTool:
    title: str
    code: int
    parameters: dict
    example: str
    arguments: dict
    effect: str
    endpoint: str = '/screen/uav/camera/control'
    mapping: dict = field(default_factory=dict)
    choices: dict = field(default_factory=dict)
    json_fields: bool = False
    fixed: dict = field(default_factory=dict)
    domain: str = 'camera'

    def definition(self, name):
        return ToolDefinition(name, self.title+'。'+self.effect+'。需要用户确认，受理不代表设备完成。',
            ToolSchema(self.parameters, tuple(self.parameters)), side_effect=True, requires_confirmation=True)


def choice(*values):
    return ToolParameter('string', enum=values)


def number(low, high, description=''):
    return ToolParameter('number', minimum=low, maximum=high, description=description)


BOOL=ToolParameter('boolean')
UNIT=number(0,1)
LENS=choice('wide','zoom','ir')
TOOLS={
    'camera_live_quality': JavaTool('设置直播清晰度',106,{'quality':choice('auto','smooth','sd','hd','uhd')},
        '把直播清晰度设置为高清',{'quality':'hd'},'先切换录像模式，再设置直播码流清晰度',
        mapping={'quality':'param'},choices={'quality':{'auto':0,'smooth':1,'sd':2,'hd':3,'uhd':4}}),
    'camera_photo_storage': JavaTool('设置照片存储镜头',109,{'lens':choice('current','wide','zoom','ir','all')},
        '照片保存全部镜头',{'lens':'all'},'设置之后拍照保存的镜头类型，不立即拍照',
        mapping={'lens':'storageSettings'},choices={'lens':{'current':'current','wide':'wide','zoom':'zoom','ir':'ir','all':'wide,zoom,ir'}}),
    'camera_video_storage': JavaTool('设置录像存储镜头',110,{'lens':choice('current','wide','zoom','ir','all')},
        '录像保存广角镜头',{'lens':'wide'},'设置之后录像保存的镜头类型，不立即开始录像',
        mapping={'lens':'storageSettings'},choices={'lens':{'current':'current','wide':'wide','zoom':'zoom','ir':'ir','all':'wide,zoom,ir'}}),
    'camera_night_mode': JavaTool('设置相机夜景模式',111,{'mode':choice('off','on','auto')},
        '开启相机夜景模式',{'mode':'on'},'发送夜景模式设置；具体镜头及固件是否支持需设备回执',
        mapping={'mode':'param'},choices={'mode':{'off':0,'on':1,'auto':2}}),
    'camera_denoise': JavaTool('设置夜景降噪等级',112,{'level':choice('enhanced','strong')},
        '设置增强降噪',{'level':'enhanced'},'增强为15fps，超强为5fps；仅手动夜景开启后生效，不自动开启夜景',
        mapping={'level':'param'},choices={'level':{'enhanced':2,'strong':3}}),
    'camera_night_vision': JavaTool('设置黑白夜视开关',113,{'enabled':BOOL},
        '开启黑白夜视',{'enabled':True},'仅变焦7倍以上生效，不自动改变变焦倍数',mapping={'enabled':'param'}),
    'camera_infrared_light': JavaTool('设置近红外补光开关',114,{'enabled':BOOL},
        '关闭近红外补光',{'enabled':False},'仅变焦7倍以上生效，不自动改变变焦倍数',mapping={'enabled':'param'}),
    'thermal_metering_stop': JavaTool('关闭红外测温',120,{},'关闭红外测温',{},'将红外测温模式设置为关闭'),
    'thermal_metering_point': JavaTool('设置红外单点测温',121,{'x':UNIT,'y':UNIT},
        '红外单点测温位置x=0.5，y=0.4',{'x':0.5,'y':0.4},'切换到单点测温模式并指定归一化图像坐标',json_fields=True),
    'thermal_metering_area': JavaTool('设置红外区域测温',122,{'x':UNIT,'y':UNIT,'width':UNIT,'height':UNIT},
        '红外区域测温x=0.1，y=0.2，width=0.3，height=0.4',{'x':0.1,'y':0.2,'width':0.3,'height':0.4},
        '切换到区域测温模式并指定矩形范围',json_fields=True),
    'thermal_palette': JavaTool('设置红外调色盘',123,{'style':choice('white_hot','black_hot','red_hot','medical','rainbow1','iron','arctic','lava','hot_iron','rainbow2')},
        '红外调色盘切换为白热',{'style':'white_hot'},'先切换红外镜头，再设置调色盘',mapping={'style':'param'},
        choices={'style':dict(zip(('white_hot','black_hot','red_hot','medical','rainbow1','iron','arctic','lava','hot_iron','rainbow2'),(0,1,2,3,5,6,8,11,12,13)))}),
    'thermal_gain': JavaTool('设置红外增益模式',126,{'mode':choice('auto','low','high')},
        '红外增益设为低增益',{'mode':'low'},'自动0，低增益1，高增益2；设置量程模式',mapping={'mode':'param'},
        choices={'mode':{'auto':0,'low':1,'high':2}}),
    'thermal_split': JavaTool('设置红外分屏',128,{'enabled':BOOL},
        '开启红外分屏',{'enabled':True},'先切红外镜头，再设置分屏开关',mapping={'enabled':'param'}),
    'camera_point_focus': JavaTool('指定图像坐标单点对焦',108,{'lens':LENS,'x':UNIT,'y':UNIT},
        '变焦镜头单点对焦x=0.5，y=0.4',{'lens':'zoom','x':0.5,'y':0.4},'对指定镜头归一化坐标进行对焦',
        mapping={'lens':'cameraType'},json_fields=True),
    'camera_frame_zoom': JavaTool('指定图像矩形框选变焦',107,{'lens':LENS,'x':UNIT,'y':UNIT,'width':UNIT,'height':UNIT,'locked':BOOL},
        '变焦镜头框选x=0.1，y=0.2，width=0.3，height=0.4，不锁定机头',
        {'lens':'zoom','x':0.1,'y':0.2,'width':0.3,'height':0.4,'locked':False},
        '按图像矩形框选变焦；锁定时可能带动机身转动',mapping={'lens':'cameraType','locked':'lock'},json_fields=True),
    'camera_aim': JavaTool('指定图像坐标云台瞄准',124,{'lens':LENS,'x':UNIT,'y':UNIT,'locked':BOOL},
        '广角镜头瞄准x=0.5，y=0.4，不锁定机头',{'lens':'wide','x':0.5,'y':0.4,'locked':False},
        'AIM瞄准指定图像点；锁定时可能带动机身转动',mapping={'lens':'cameraType','locked':'lock'},json_fields=True),
    'camera_look_at': JavaTool('让相机看向明确的地理目标',129,
        {'longitude':number(-180,180),'latitude':number(-90,90),'altitude_m':number(2,10000,'目标点WGS84椭球高米，0.1米步长，不是相对高度或云台角度'),'locked':BOOL},
        '看向经度113.1纬度34.8，WGS84椭球高100米，不锁定机头',
        {'longitude':113.1,'latitude':34.8,'altitude_m':100,'locked':False},
        '指向地理目标；不是指定云台角度，飞行过程中指向角度可变化',
        mapping={'altitude_m':'height','locked':'lock'},json_fields=True),
    'gimbal_yaw_center': JavaTool('云台偏航回中',405,{},'云台偏航回中',{},'调用云台reset模式2，偏航回中',endpoint='/screen/uav/gimbal/control'),
    'gimbal_free_mode': JavaTool('云台自由模式',420,{},'切换云台自由模式',{},'调用camera_aim，zoom镜头中心点，locked=false',endpoint='/screen/uav/gimbal/control'),
    'gimbal_follow_mode': JavaTool('云台跟随模式',422,{},'切换云台跟随模式',{},'调用camera_aim，zoom镜头中心点，locked=true，可能带动机身',endpoint='/screen/uav/gimbal/control'),
    'uav_night_light': JavaTool('设置无人机夜航灯',208,{'enabled':BOOL},'开启无人机夜航灯',{'enabled':True},
        '发送机体夜航灯开关',endpoint='/screen/uav/operation/control',mapping={'enabled':'param'},domain='flight'),
    'uav_stealth': JavaTool('设置无人机隐蔽模式',209,{'enabled':BOOL},'关闭无人机隐蔽模式',{'enabled':False},
        '发送机体隐蔽模式开关',endpoint='/screen/uav/operation/control',mapping={'enabled':'param'},domain='flight'),
    'point_flight_stop': JavaTool('停止指点飞行',120,{},'停止指点飞行',{},'调用fly_to_point_stop，不能等同已悬停',endpoint='/screen/uav/fly/control',domain='route'),
    'point_orbit_stop': JavaTool('退出指点环绕',122,{},'退出指点环绕',{},'调用poi_mode_exit，退出环绕模式',endpoint='/screen/uav/fly/control',domain='route'),
}


@dataclass(frozen=True)
class JavaActionRequest(FlightControlActionRequest):
    def to_dict(self):
        self.validate()
        return {'command_id':self.command_id,'idempotency_key':self.idempotency_key,
            'action':self.action,'parameters':dict(self.parameters),'context_version':self.context_version}

    def validate(self):
        if self.action not in TOOLS or not isinstance(self.parameters,dict):
            raise ValueError('unknown Java tool')
        if any(not isinstance(v,str) or re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',v) is None
               for v in (self.command_id,self.idempotency_key)) or type(self.context_version) is not int:
            raise ValueError('invalid identifiers')
        spec=TOOLS[self.action]
        if set(self.parameters)!=set(spec.parameters):
            raise ValueError('explicit parameters required')
        for key,p in spec.parameters.items():
            value=self.parameters[key]
            if p.type=='boolean' and type(value) is not bool:
                raise ValueError('boolean required')
            if p.type=='string' and (not isinstance(value,str) or value not in p.enum):
                raise ValueError('enum required')
            if p.type=='number' and (type(value) not in (int,float) or not math.isfinite(value)
                    or not p.minimum<=value<=p.maximum):
                raise ValueError('number out of range')
        if 'width' in self.parameters:
            p=self.parameters
            if p['width']<=0 or p['height']<=0 or p['x']+p['width']>1 or p['y']+p['height']>1:
                raise ValueError('image rectangle out of bounds')
        if self.action=='camera_look_at':
            p=self.parameters
            if any(round(p[key],6)!=p[key] for key in ('longitude','latitude')) or not math.isclose(
                    p['altitude_m']*10,round(p['altitude_m']*10),abs_tol=1e-8):
                raise ValueError('target coordinate precision unsupported')


def encode(request):
    request.validate()
    spec=TOOLS[request.action]
    values={}
    for key,value in request.parameters.items():
        if key in spec.choices:
            value=spec.choices[key][value]
        elif type(value) is bool and not spec.json_fields:
            value=int(value)
        values[spec.mapping.get(key,key)]=value
    return spec.endpoint, {'type':spec.code, **spec.fixed, **(
        {'jsonParams':json.dumps(values,ensure_ascii=False,sort_keys=True,separators=(',',':'))}
        if spec.json_fields else values)}


LABELS={
    'auto':('自动','自适应'),'smooth':('流畅',),'sd':('标清',),'hd':('高清',),'uhd':('超清',),
    'current':('当前',),'wide':('广角',),'zoom':('变焦',),'ir':('红外',),'all':('全部','所有'),
    'off':('关闭','关掉'),'on':('开启','打开'),'enhanced':('增强',),'strong':('超强',),
    'white_hot':('白热',),'black_hot':('黑热',),'red_hot':('描红',),'medical':('医疗',),
    'rainbow1':('彩虹1','彩虹一'),'iron':('铁红',),'arctic':('北极',),'lava':('熔岩',),
    'hot_iron':('热铁',),'rainbow2':('彩虹2','彩虹二'),'low':('低增益',),'high':('高增益',),
}


def grounded(name, parameters, text):
    try:
        JavaActionRequest('check','check',name,dict(parameters),1).validate()
    except (ValueError,TypeError):
        return False
    if re.search(r'不要|不必|禁止|别开启|别打开|do not|don.t', text, re.I):
        return False
    labels={'x':r'(?:(?<![A-Za-z])x|横坐标)','y':r'(?:(?<![A-Za-z])y|纵坐标)', 'width':r'(?:width|宽度)',
        'height':r'(?:height|高度)', 'longitude':r'(?:经度|longitude)',
        'latitude':r'(?:纬度|latitude)', 'altitude_m':r'(?:椭球高|WGS84高度)'}
    for key,value in parameters.items():
        if isinstance(value,str):
            if not any(label in text for label in LABELS.get(value,(value,))):
                return False
            # “超强”不能被误认成“增强”，高清也不应覆盖超清，词本身互不相同。
        elif key=='locked':
            unlocked=bool(re.search(r'不锁定|仅云台|只转云台',text))
            if value and (unlocked or '锁定机头' not in text):
                return False
            if not value and not unlocked:
                return False
        elif type(value) is bool:
            if not any(word in text for word in (('开启','打开') if value else ('关闭','关掉'))):
                return False
        elif key in labels:
            matches=re.findall(labels[key]+r'\s*(?:=|为|是|[:：])?\s*(-?\d+(?:\.\d+)?)',text,re.I)
            if len(matches)!=1 or not math.isclose(float(matches[0]),value,abs_tol=1e-8):
                return False
    return True
