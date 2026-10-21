"""媒体描述只通过合成 HTTP 边界读取；不请求任何媒体或设备。"""
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import traceback
import unittest
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from lingxi_flight_control import RuntimeBinding
from lingxi_flight_control.ktd_platform import KtdHttpTransport, KtdPlatformError


SIGNED_URL = 'https://media.example/live/drone.m3u8?signature=SYNTHETIC_MEDIA_SECRET'


def visuals_packet():
    return {'code': 200, 'success': True, 'data': {
        'schema_version': 'lingxi-platform-visuals/v1',
        'actor_employee_id': 'actor-1', 'project_id': 'project-1', 'site_id': 'site-1',
        'hangar_id': 'hangar-1', 'drone_id': 'drone-1', 'task_id': None, 'generated_at': 1000,
        'streams': [
            {'kind': 'site', 'sources': [], 'reason_codes': ['NOT_CONFIGURED']},
            {'kind': 'hangar', 'sources': [], 'reason_codes': ['FORMAT_UNKNOWN']},
            {'kind': 'drone', 'sources': [{'format': 'hls', 'url': SIGNED_URL, 'expires_at': None}],
             'reason_codes': []},
        ],
    }}


class VisualsHttpBoundary:
    def __init__(self, packet=None):
        self.packet = deepcopy(visuals_packet() if packet is None else packet)
        self.calls = []
        self.raw = None
        self.status = 200
        self.failure = None

    def open(self, request, *, timeout):
        self.calls.append((request, timeout))
        if self.failure is not None:
            raise self.failure
        response = BytesIO(self.raw if self.raw is not None else json.dumps(self.packet).encode())
        response.code = self.status
        return response


class KtdVisualsTests(unittest.TestCase):
    def setUp(self):
        self.binding = RuntimeBinding('actor-1', 'project-1', 'hangar-1', 'drone-1', 'batch-1', 7)
        self.now = 1000.0
        self.boundary = VisualsHttpBoundary()
        self.transport = KtdHttpTransport('https://platform.invalid/api', token='synthetic-platform-key',
            actor_id='actor-1', opener=self.boundary)

    def provider(self, **options):
        from lingxi_flight_control.ktd_visuals import KtdVisualsProvider
        return KtdVisualsProvider(self.binding, site_id='site-1', task_id=None,
            transport=self.transport, clock=lambda: self.now, **options)

    def test_default_origin_policy_returns_three_unavailable_views_without_playback_urls(self):
        result = self.provider()()
        self.assertEqual([stream['kind'] for stream in result['streams']], ['site', 'hangar', 'drone'])
        self.assertTrue(all(stream['available'] is False and stream['sources'] == [] for stream in result['streams']))
        self.assertEqual(result['streams'][2]['reason_codes'], ['MEDIA_ORIGIN_NOT_ALLOWED'])
        self.assertNotIn('SYNTHETIC_MEDIA_SECRET', json.dumps(result))
        self.assertEqual(len(self.boundary.calls), 1)

    def test_allowed_source_preserves_signature_and_uses_one_fixed_server_bound_read(self):
        result = self.provider(allowed_origins=('https://media.example',))()
        self.assertEqual(result['streams'][2], {'kind': 'drone', 'available': True,
            'sources': [{'format': 'hls', 'url': SIGNED_URL, 'expires_at': None}], 'reason_codes': []})
        self.assertEqual(result['resource_binding'], {'project_id': 'project-1', 'site_id': 'site-1',
            'hangar_id': 'hangar-1', 'drone_id': 'drone-1', 'task_id': None})
        request, timeout = self.boundary.calls[0]
        self.assertEqual(request.get_method(), 'GET')
        self.assertIsNone(request.data)
        target = urlsplit(request.full_url)
        self.assertEqual(target.path, '/api/lingxi/agent/platform/visuals')
        self.assertEqual(parse_qs(target.query), {'siteId': ['site-1'], 'hangarId': ['hangar-1'], 'droneId': ['drone-1']})
        self.assertEqual(request.get_header('Authorization'), 'Bearer synthetic-platform-key')
        self.assertEqual(request.get_header('Projectid'), 'project-1')
        self.assertEqual(timeout, 5)
        self.assertEqual(len(self.boundary.calls), 1)

    def test_all_resource_fields_and_actor_must_match_the_server_binding(self):
        for field in ('actor_employee_id', 'project_id', 'site_id', 'hangar_id', 'drone_id', 'task_id'):
            with self.subTest(field=field):
                self.boundary.packet = visuals_packet()
                self.boundary.packet['data'][field] = 'other'
                with self.assertRaisesRegex(KtdPlatformError, 'VISUALS_BINDING_MISMATCH'):
                    self.provider()()

    def test_url_filter_rejects_untrusted_origins_protocols_and_ambiguous_urls(self):
        cases = [
            ('https://media.example.evil/a.m3u8', 'MEDIA_ORIGIN_NOT_ALLOWED'),
            ('https://other.media.example/a.m3u8', 'MEDIA_ORIGIN_NOT_ALLOWED'),
            ('https://media.example:8443/a.m3u8', 'MEDIA_ORIGIN_NOT_ALLOWED'),
            ('https://127.0.0.1/a.mp4', 'MEDIA_ORIGIN_NOT_ALLOWED'),
            ('http://media.example/a.flv', 'MEDIA_PROTOCOL_UNSUPPORTED'),
            ('webrtc://media.example/live', 'MEDIA_PROTOCOL_UNSUPPORTED'),
            ('data:video/mp4,abcd', 'MEDIA_PROTOCOL_UNSUPPORTED'),
            ('javascript:alert(1)', 'MEDIA_PROTOCOL_UNSUPPORTED'),
            ('//media.example/a.mp4', 'MEDIA_PROTOCOL_UNSUPPORTED'),
            ('https://user:secret@media.example/a', 'MEDIA_URL_INVALID'),
            ('https://media.example/a#secret', 'MEDIA_URL_INVALID'),
            ('https://media.example\\evil/a', 'MEDIA_URL_INVALID'),
            ('https://media.example/a\r\nHost:evil', 'MEDIA_URL_INVALID'),
            ('https://media.example/a%0aevil', 'MEDIA_URL_INVALID'),
            ('https://media.example:0/a', 'MEDIA_URL_INVALID'),
            ('https://media.example:invalid/a', 'MEDIA_URL_INVALID'),
        ]
        for url, reason in cases:
            with self.subTest(url=url):
                self.boundary.packet['data']['streams'][2]['sources'][0]['url'] = url
                stream = self.provider(allowed_origins=('https://media.example',))()['streams'][2]
                self.assertEqual(stream, {'kind': 'drone', 'available': False, 'sources': [], 'reason_codes': [reason]})
        self.assertTrue(all(urlsplit(call[0].full_url).hostname == 'platform.invalid' for call in self.boundary.calls))

    def test_expiry_is_explicit_and_the_signature_query_is_never_guessed_or_rewritten(self):
        source = self.boundary.packet['data']['streams'][2]['sources'][0]
        source['url'] = 'https://MEDIA.EXAMPLE:443/live.m3u8?expires=1&signature=SYNTHETIC_MEDIA_SECRET'
        provider = self.provider(allowed_origins=('https://media.example',))
        self.assertEqual(provider()['streams'][2]['sources'][0]['url'], source['url'])
        self.assertIsNone(provider()['streams'][2]['sources'][0]['expires_at'])
        source['expires_at'] = self.now
        self.assertEqual(provider()['streams'][2]['reason_codes'], ['MEDIA_URL_EXPIRED'])
        source['expires_at'] = self.now + 1
        self.assertTrue(provider()['streams'][2]['available'])

    def test_explicit_formats_are_preserved_and_insecure_http_has_a_separate_opt_in(self):
        sources = [{'format': name, 'url': f'https://media.example/{name}/source', 'expires_at': None}
                   for name in ('hls', 'flv', 'webrtc', 'mp4')]
        self.boundary.packet['data']['streams'][2]['sources'] = sources
        result = self.provider(allowed_origins=('https://media.example',))()
        self.assertEqual(result['streams'][2]['sources'], sources)
        sources[0]['url'] = 'http://media.example/live.m3u8'
        self.boundary.packet['data']['streams'][2]['sources'] = [sources[0]]
        result = self.provider(allowed_origins=('http://media.example',), allow_insecure_http=True)()
        self.assertTrue(result['streams'][2]['available'])
        self.assertEqual(len(self.boundary.calls), 2)

    def test_optional_task_is_server_bound_and_invalid_transport_actor_makes_no_request(self):
        from dataclasses import replace
        from lingxi_flight_control.ktd_visuals import KtdVisualsProvider
        self.boundary.packet['data']['task_id'] = 'task-1'
        provider = KtdVisualsProvider(self.binding, site_id='site-1', task_id='task-1',
            transport=self.transport, clock=lambda: self.now)
        self.assertEqual(provider()['resource_binding']['task_id'], 'task-1')
        self.assertEqual(parse_qs(urlsplit(self.boundary.calls[0][0].full_url).query)['taskId'], ['task-1'])
        before = len(self.boundary.calls)
        with self.assertRaises(KtdPlatformError):
            self.transport.read_visuals(replace(self.binding, actor_id='other'), site_id='site-1', task_id=None)
        self.assertEqual(len(self.boundary.calls), before)

    def test_invalid_schema_duplicate_kinds_and_unknown_reason_do_not_become_descriptions(self):
        packets = []
        for field, value in (('schema_version', 'unknown'), ('generated_at', True),
                             ('generated_at', 1001), ('generated_at', 900), ('streams', [])):
            packet = visuals_packet()
            packet['data'][field] = value
            packets.append(packet)
        for change in ('duplicate', 'unknown_reason', 'rtmp', 'bad_expiry', 'secret_field'):
            packet = visuals_packet()
            stream = packet['data']['streams'][2]
            if change == 'duplicate': stream['kind'] = 'site'
            elif change == 'unknown_reason': stream['reason_codes'] = ['SYNTHETIC_MEDIA_SECRET']
            elif change == 'rtmp': stream['sources'][0]['format'] = 'rtmp'
            elif change == 'bad_expiry': stream['sources'][0]['expires_at'] = '1234'
            else: stream['sources'][0]['pushUrl'] = 'SYNTHETIC_MEDIA_SECRET'
            packets.append(packet)
        for packet in packets:
            with self.subTest(packet=packet):
                self.boundary.packet = packet
                with self.assertRaises(KtdPlatformError) as caught:
                    self.provider()()
                self.assertNotIn('SYNTHETIC_MEDIA_SECRET', ''.join(traceback.format_exception(caught.exception)))

    def test_upstream_errors_are_fixed_redacted_and_never_retried(self):
        for status in (401, 403, 404, 503):
            self.boundary.failure = HTTPError('https://platform.invalid/api?signature=SYNTHETIC_MEDIA_SECRET',
                status, 'SYNTHETIC_MEDIA_SECRET', {}, BytesIO(b'{"code":503}'))
            before = len(self.boundary.calls)
            with self.subTest(status=status), self.assertRaises(KtdPlatformError) as caught:
                self.provider()()
            self.assertEqual(str(caught.exception), 'VISUALS_FORBIDDEN' if status in (401, 403) else 'VISUALS_SOURCE_UNAVAILABLE')
            self.assertNotIn('SYNTHETIC_MEDIA_SECRET', ''.join(traceback.format_exception(caught.exception)))
            self.assertEqual(len(self.boundary.calls), before + 1)
        self.boundary.failure = TimeoutError('SYNTHETIC_MEDIA_SECRET')
        with self.assertRaisesRegex(KtdPlatformError, 'VISUALS_TRANSPORT_ERROR') as caught:
            self.provider()()
        self.assertNotIn('SYNTHETIC_MEDIA_SECRET', ''.join(traceback.format_exception(caught.exception)))
        self.boundary.failure = None
        for raw in (b'{"code":200,"code":403}', b'{"code":NaN}', b'not-json'):
            self.boundary.raw = raw
            with self.assertRaises(KtdPlatformError): self.provider()()


class MediaConfigTests(unittest.TestCase):
    def setUp(self):
        from test_platform_runtime import PlatformRuntimeConfigTests
        PlatformRuntimeConfigTests.setUp(self)

    def config(self, **changes):
        from lingxi_flight_control.platform_runtime import PlatformRuntimeConfig
        return PlatformRuntimeConfig.from_environment({**self.environment, **changes})

    def test_default_is_empty_and_backend_http_permission_does_not_allow_media_http(self):
        config = self.config(KTD_BASE_URL='http://platform.invalid/api', KTD_ALLOW_INSECURE_HTTP='true')
        self.assertEqual(config.media_allowed_origins, ())
        self.assertFalse(config.media_allow_insecure_http)
        with self.assertRaises(ValueError):
            self.config(FLIGHT_AGENT_MEDIA_ALLOWED_ORIGINS='["http://media.example"]')
        config = self.config(FLIGHT_AGENT_MEDIA_ALLOWED_ORIGINS='["http://media.example:8080"]',
            FLIGHT_AGENT_MEDIA_ALLOW_INSECURE_HTTP='true')
        self.assertEqual(config.media_allowed_origins, ('http://media.example:8080',))
        self.assertTrue(config.media_allow_insecure_http)
        self.assertFalse(config.journal_path.exists())

    def test_origin_config_is_a_bounded_exact_list_not_a_url_proxy_or_pattern(self):
        for value in ('null', '{}', '"https://media.example"', 'not-json', '[true]',
                      '["https://*.example"]', '["https://media.example/path"]',
                      '["https://user:SYNTHETIC_MEDIA_SECRET@media.example"]',
                      '["https://media.example?token=SYNTHETIC_MEDIA_SECRET"]',
                      '["https://media.example#fragment"]',
                      '["https://0x7f000001"]', '["https://0x7f.0.0.1"]',
                      '["https://[fe80::1%25en0]"]',
                      '["https://media.example", "https://MEDIA.EXAMPLE:443"]'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    self.config(FLIGHT_AGENT_MEDIA_ALLOWED_ORIGINS=value)
                self.assertNotIn('SYNTHETIC_MEDIA_SECRET', str(caught.exception))
        with self.assertRaises(ValueError):
            self.config(FLIGHT_AGENT_MEDIA_ALLOW_INSECURE_HTTP='yes')


class JavaVisualsFixtureTests(unittest.TestCase):
    def test_original_converter_fixture_crosses_real_transport_without_filling_nulls_or_playing(self):
        from lingxi_flight_control.ktd_visuals import KtdVisualsProvider
        fixture = Path(__file__).resolve().parents[1] / 'fixtures/ktd-platform-visuals-v1.json'
        raw = fixture.read_bytes()
        packet = json.loads(raw)
        data = packet['data']
        binding = RuntimeBinding(data['actor_employee_id'], data['project_id'], data['hangar_id'],
                                 data['drone_id'], 'java-converter-fixture', 1)
        boundary = VisualsHttpBoundary(packet)
        transport = KtdHttpTransport('https://synthetic-java.invalid/api', token='synthetic-platform-key',
                                    actor_id=binding.actor_id, opener=boundary)
        provider = KtdVisualsProvider(binding, site_id=data['site_id'], task_id=data['task_id'],
            transport=transport, allowed_origins=('https://media.example.test',), clock=lambda: data['generated_at'])
        result = provider()
        self.assertEqual(result['resource_binding']['task_id'], 'task-1')
        self.assertEqual([stream['kind'] for stream in result['streams']], ['site', 'hangar', 'drone'])
        self.assertEqual([[source['format'] for source in stream['sources']] for stream in result['streams']],
                         [['hls'], ['mp4'], ['hls', 'flv']])
        self.assertTrue(all(source['expires_at'] is None for stream in result['streams'] for source in stream['sources']))
        self.assertEqual(result['streams'][2]['sources'][0]['url'], data['streams'][2]['sources'][0]['url'])
        self.assertEqual(fixture.read_bytes(), raw)
        self.assertEqual(boundary.packet, packet)
        self.assertEqual(len(boundary.calls), 1)
        self.assertEqual(urlsplit(boundary.calls[0][0].full_url).path, '/api/lingxi/agent/platform/visuals')


if __name__ == '__main__':
    unittest.main()
