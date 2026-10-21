"""Additive V1.3 facade; existing CockpitService and action contracts stay untouched."""
from dataclasses import dataclass
import json
import re
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from urllib.error import HTTPError
from urllib.parse import urlsplit
from uuid import uuid4


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_):
        return None


@dataclass(frozen=True)
class Binding:
    siteId: str
    hangarId: str
    droneId: str
    taskId: str = ''

    def value(self):
        values = vars(self)
        if any(not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', values[key])
               for key in ('siteId', 'hangarId', 'droneId')):
            raise ValueError('invalid server binding')
        if self.taskId and not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', self.taskId):
            raise ValueError('invalid server binding')
        return dict(values)


class V13Client:
    def __init__(self, base_url, token, project, actor, binding, transport=None):
        url = urlsplit(base_url)
        if url.scheme != 'https' and not (url.scheme == 'http' and url.hostname in {'127.0.0.1', 'localhost'}):
            raise ValueError('https or loopback required')
        if (url.username or url.password or url.query or url.fragment
                or url.path not in {'', '/', '/api', '/api/'}):
            raise ValueError('fixed origin required')
        base_path = '' if url.path in {'', '/'} else '/api'
        if not token or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', project) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', actor):
            raise ValueError('invalid trusted context')
        self.base = f'{url.scheme}://{url.netloc}{base_path}/flight-v13/'
        self.headers = {'Authorization': 'Bearer ' + token, 'X-Project-Id': project,
                        'X-Actor-Id': actor, 'Content-Type': 'application/json'}
        self.binding = binding.value()
        self.transport = transport or self._http
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def _http(self, method, body):
        request = Request(self.base + method, data=json.dumps(body).encode(), headers=self.headers)
        try:
            response = self.opener.open(request, timeout=15)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError('response exceeds budget')
            return response.code, json.loads(raw)

    def call(self, method, values=None):
        if method not in {'state', 'preview', 'confirm', 'targets', 'annotation-ai',
                          'events', 'orbit-check'}:
            raise ValueError('method not allowed')
        body = {'binding': self.binding, **(values or {})}
        return self.transport(method, body)


_NUMBER = r'(\d+(?:\.\d+)?)'
_TARGET = r'([^\s，,。；;]{1,64})'


def parse_command(text):
    """Only exact grounded forms; arbitrary or compound commands require clarification."""
    if not isinstance(text, str) or not 1 <= len(text) <= 1000:
        return None
    text = text.strip()
    simple = {'恢复航线': 'route_resume', '结束当前任务': 'end_current', '结束全部任务': 'end_all'}
    if text in simple:
        return {'action': simple[text]}
    match = re.fullmatch(r'去东经' + _NUMBER + r'[，, ]+北纬' + _NUMBER + r'[，, ]+(?:相对)?高(?:度)?' + _NUMBER + r'米', text)
    if match:
        longitude, latitude, altitude = map(float, match.groups())
        if not (-180 <= longitude <= 180 and -90 <= latitude <= 90 and 0 <= altitude <= 10000):
            return None
        return {'action': 'point_flight', 'target': '坐标目标', 'longitude': longitude,
                'latitude': latitude, 'altitudeM': altitude}
    match = re.fullmatch(
        r'围绕' + _TARGET + r'绕一圈[，, ]+半径' + _NUMBER + r'米'
        r'(?:[，, ]+(?:相对)?高(?:度)?' + _NUMBER + r'米)?', text)
    if match and not re.search(r'[再然后并]', match[1]):
        result = {'action': 'point_orbit', 'target': match[1],
                  'radiusM': float(match[2])}
        if match[3] is not None:
            result['altitudeM'] = float(match[3])
        return result
    match = re.fullmatch(
        r'去' + _TARGET + r'(?:[，, ]+(?:相对)?高(?:度)?' + _NUMBER + r'米)?', text)
    if match and not re.search(r'[再然后并]', match[1]):
        result = {'action': 'point_flight', 'target': match[1]}
        if match[2] is not None:
            result['altitudeM'] = float(match[2])
        return result
    match = re.fullmatch(r'(同时开启|仅替换为|关闭全部)AI(?:[：:]([A-Za-z0-9_.:-]+(?:[,，][A-Za-z0-9_.:-]+)*))?', text)
    if match:
        if match[1] != '关闭全部' and not match[2]:
            return None
        return {'action': 'ai_set', 'algorithms': [] if match[1] == '关闭全部' else re.split('[,，]', match[2]),
                'aiMode': 'add' if match[1] == '同时开启' else 'replace'}
    return None


class V13Facade:
    def __init__(self, client):
        self.client = client

    def handle(self, path, body):
        if not isinstance(body, dict):
            return 400, {'code': 'INVALID_REQUEST', 'message': '请求体必须是对象。'}
        if path == 'state':
            if set(body) - {'binding'}:
                return 400, {'code': 'INVALID_REQUEST'}
            return self.client.call('state')
        if path == 'targets':
            if set(body) != {'name'} or not isinstance(body['name'], str) or len(body['name']) > 128:
                return 400, {'code': 'INVALID_QUERY'}
            return self.client.call('targets', body)
        if path == 'annotation-ai':
            allowed = {'binding', 'annotationId', 'update', 'algorithms'}
            if not set(body) <= allowed or not isinstance(body.get('annotationId'), str):
                return 400, {'code': 'INVALID_ANNOTATION'}
            if body.get('update') not in {None, True, False}:
                return 400, {'code': 'INVALID_ANNOTATION'}
            return self.client.call('annotation-ai', body)
        if path == 'orbit-check':
            if not {'longitude', 'latitude', 'radiusM', 'altitudeM'} <= set(body):
                return 400, {'code': 'INVALID_ORBIT'}
            return self.client.call('orbit-check', body)
        if path == 'messages':
            if set(body) - {'text', 'expectedVersion', 'targetId'}:
                return 400, {'code': 'INVALID_REQUEST'}
            call = parse_command(body['text'])
            if call is None:
                return 200, {'status': 'clarifying', 'message': '请明确单目标和相对高度；环绕还需半径。多目标请分次下达，AI冲突请选择同时开启或仅替换。'}
            if 'target' in call and isinstance(body.get('targetId'), str):
                call['targetId'] = body['targetId']
            return self.client.call('preview', {**call, 'expectedVersion': body['expectedVersion']})
        if path == 'confirm':
            if set(body) != {'confirmationId', 'bindingHash', 'requestId'}:
                return 400, {'code': 'INVALID_CONFIRMATION'}
            return self.client.call('confirm', body)
        return 404, {'code': 'NOT_FOUND'}
