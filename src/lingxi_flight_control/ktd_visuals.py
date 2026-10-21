"""Display-only media descriptions; URLs never enter flight state or model tools."""
from __future__ import annotations

from collections.abc import Mapping
import ipaddress
import re
import time
from urllib.parse import urlsplit

from .full_control import RuntimeBinding
from .ktd_platform import KtdHttpResponse, KtdPlatformError, _finite, _identifier


SCHEMA_VERSION = 'lingxi-platform-visuals/v1'
STREAM_KINDS = ('site', 'hangar', 'drone')
MEDIA_FORMATS = frozenset({'hls', 'flv', 'webrtc', 'mp4'})
SOURCE_REASONS = frozenset({'NOT_CONFIGURED', 'FORMAT_UNKNOWN', 'INVALID_DESCRIPTOR', 'RESOURCE_NOT_AVAILABLE'})
_DATA_FIELDS = frozenset({'schema_version', 'actor_employee_id', 'project_id', 'site_id',
                        'hangar_id', 'drone_id', 'task_id', 'generated_at', 'streams'})


def _reject(code='INVALID_VISUALS_RESPONSE'):
    raise KtdPlatformError(code) from None


def _url_parts(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 8192
            or any(ord(char) <= 32 or ord(char) >= 127 or char in '\\<>"' for char in value)
            or re.search(r'%(?:0[0-9a-f]|1[0-9a-f]|7f|5c)', value, re.I)):
        raise ValueError('Invalid media URL')
    parts = urlsplit(value)
    if (parts.scheme not in {'http', 'https'} or not parts.hostname
            or parts.username is not None or parts.password is not None or '#' in value):
        raise ValueError('Invalid media URL')
    host = parts.hostname.lower()
    if '%' in host:
        raise ValueError('Invalid media URL')
    if ':' in host:
        host = '[' + ipaddress.IPv6Address(host).compressed + ']'
    elif re.fullmatch(r'(?:[0-9]+|0x[0-9a-f]+)', host.rsplit('.', 1)[-1]):
        # Browsers also parse hex/short numeric hosts as IPv4. Accept only the
        # canonical dotted form so Python and browser origin checks agree.
        host = str(ipaddress.IPv4Address(host))
    elif (len(host) > 253 or any(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) is None
                               for label in host.split('.'))):
        raise ValueError('Invalid media URL')
    port = parts.port
    if port is not None and not 1 <= port <= 65535:
        raise ValueError('Invalid media URL')
    default_port = 443 if parts.scheme == 'https' else 80
    origin = parts.scheme + '://' + host + (':' + str(port) if port not in (None, default_port) else '')
    return parts, origin


def validate_media_origins(values, *, allow_insecure_http=False):
    """Normalize explicit server policy; no host suffixes, wildcards or URL paths."""
    if type(allow_insecure_http) is not bool:
        raise ValueError('FLIGHT_AGENT_MEDIA_ALLOW_INSECURE_HTTP 无效')
    if not isinstance(values, (list, tuple)) or len(values) > 32:
        raise ValueError('FLIGHT_AGENT_MEDIA_ALLOWED_ORIGINS 必须是最多32项的JSON数组')
    normalized = []
    for value in values:
        try:
            parts, origin = _url_parts(value)
            if (parts.path not in ('', '/') or '?' in value
                    or (parts.scheme == 'http' and not allow_insecure_http) or origin in normalized):
                raise ValueError('Invalid media origin')
        except (ValueError, TypeError):
            raise ValueError('FLIGHT_AGENT_MEDIA_ALLOWED_ORIGINS 无效') from None
        normalized.append(origin)
    return tuple(normalized)


class KtdVisualsProvider:
    """Validate the fixed Java resource read, then apply server-only URL policy.

    An available source is a display candidate, not evidence that a stream is
    live. No source URL is fetched, started, rewritten, cached or persisted here.
    """
    def __init__(self, binding: RuntimeBinding, *, site_id: str, task_id: str | None,
                 transport, allowed_origins=(), allow_insecure_http=False, clock=time.time):
        if not isinstance(binding, RuntimeBinding):
            _reject('INVALID_VISUALS_BINDING')
        binding.validate()
        if (not all(_identifier(value) for key, value in binding.to_dict().items() if key != 'context_version')
                or not _identifier(site_id) or (task_id is not None and not _identifier(task_id))
                or getattr(transport, 'actor_id', None) != binding.actor_id
                or not callable(getattr(transport, 'read_visuals', None)) or not callable(clock)):
            _reject('INVALID_VISUALS_BINDING')
        self.binding, self.site_id, self.task_id = binding, site_id, task_id
        self._transport, self._clock = transport, clock
        self._allow_http = allow_insecure_http
        self._origins = frozenset(validate_media_origins(allowed_origins, allow_insecure_http=allow_insecure_http))

    @property
    def resource_binding(self):
        return {'project_id': self.binding.project_id, 'site_id': self.site_id,
                'hangar_id': self.binding.hangar_id, 'drone_id': self.binding.drone_id, 'task_id': self.task_id}

    def __call__(self):
        try:
            response = self._transport.read_visuals(self.binding, site_id=self.site_id, task_id=self.task_id)
        except Exception:
            _reject('VISUALS_TRANSPORT_ERROR')
        if not isinstance(response, KtdHttpResponse) or type(response.status_code) is not int:
            _reject()
        payload = response.payload
        if (response.status_code in (401, 403) or (isinstance(payload, Mapping)
                and type(payload.get('code')) is int and payload['code'] in (401, 403))):
            _reject('VISUALS_FORBIDDEN')
        if (response.status_code != 200 or not isinstance(payload, Mapping)
                or type(payload.get('code')) is not int or payload['code'] != 200
                or ('success' in payload and payload['success'] is not True)):
            _reject('VISUALS_SOURCE_UNAVAILABLE')
        data = payload.get('data')
        if not isinstance(data, Mapping) or set(data) != _DATA_FIELDS or data.get('schema_version') != SCHEMA_VERSION:
            _reject()
        expected = {**self.resource_binding, 'actor_employee_id': self.binding.actor_id}
        if any(data[key] != value for key, value in expected.items()):
            _reject('VISUALS_BINDING_MISMATCH')
        now = self._clock()
        if (not _finite(now, 0, 8.64e12) or not _finite(data['generated_at'], 0, now)
                or now - data['generated_at'] > 30):
            _reject('VISUALS_DESCRIPTOR_STALE')
        streams = data['streams']
        if (not isinstance(streams, list) or len(streams) != len(STREAM_KINDS)
                or any(not isinstance(stream, Mapping) or set(stream) != {'kind', 'sources', 'reason_codes'}
                       for stream in streams)
                or [stream['kind'] for stream in streams] != list(STREAM_KINDS)):
            _reject()
        return {'resource_binding': self.resource_binding, 'generated_at': data['generated_at'],
                'streams': [self._stream(stream, now) for stream in streams]}

    def _stream(self, stream, now):
        sources, reasons = stream['sources'], stream['reason_codes']
        if (not isinstance(sources, list) or len(sources) > 8 or not isinstance(reasons, list)
                or any(not isinstance(reason, str) or reason not in SOURCE_REASONS for reason in reasons)
                or len(set(reasons)) != len(reasons) or (not sources and not reasons)):
            _reject()
        accepted, denied, seen = [], [], set()
        for source in sources:
            if (not isinstance(source, Mapping) or set(source) != {'format', 'url', 'expires_at'}
                    or not isinstance(source['format'], str) or source['format'] not in MEDIA_FORMATS
                    or not isinstance(source['url'], str)
                    or (source['expires_at'] is not None and not _finite(source['expires_at'], 0, 8.64e12))):
                _reject()
            identity = source['format'], source['url']
            if identity in seen:
                _reject()
            seen.add(identity)
            reason = self._url_reason(source, now)
            if reason:
                if reason not in denied:
                    denied.append(reason)
            else:
                accepted.append(dict(source))
        return {'kind': stream['kind'], 'available': bool(accepted), 'sources': accepted,
                'reason_codes': [] if accepted else list(dict.fromkeys([*reasons, *denied]))}

    def _url_reason(self, source, now):
        url = source['url']
        try:
            # Classify unsupported schemes without ever exposing the input text.
            if urlsplit(url).scheme not in ('http', 'https'):
                return 'MEDIA_PROTOCOL_UNSUPPORTED'
            parts, origin = _url_parts(url)
        except (ValueError, TypeError):
            return 'MEDIA_URL_INVALID'
        if parts.scheme == 'http' and not self._allow_http:
            return 'MEDIA_PROTOCOL_UNSUPPORTED'
        if origin not in self._origins:
            return 'MEDIA_ORIGIN_NOT_ALLOWED'
        if source['expires_at'] is not None and source['expires_at'] <= now:
            return 'MEDIA_URL_EXPIRED'
        return None
