"""Strict reader for Java's authoritative v1 snapshot, never a write authority."""
from __future__ import annotations

import copy
import math
import re
import time
from collections.abc import Mapping

from .full_control import ACTION_SPECS, RuntimeBinding
from .ktd_platform import (
    KtdHttpResponse, KtdPlatformError, KtdSnapshot, _finite, _identifier,
    _valid_snapshot_version,
)

SCHEMA_VERSION = 'lingxi-platform-snapshot/v1'
_ACTIONS = frozenset(action.value for action in ACTION_SPECS)
_NUMERIC_STATE = {'longitude': (-180, 180), 'latitude': (-90, 90),
    'altitude_m': (-500, 10000), 'height_m': (-500, 10000),
    'yaw_deg': (-360, 360), 'battery_percent': (0, 100)}
_STATE_FIELDS = frozenset(_NUMERIC_STATE) | {'online', 'airborne', 'flight_status', 'route_status'}


def _reject(code='INVALID_SNAPSHOT_RESPONSE'):
    raise KtdPlatformError(code) from None


def _fact_version(value):
    return isinstance(value, str) and _valid_snapshot_version(value)


class KtdSnapshotProvider:
    """Read one fixed server resource; source times and explicit deny decisions survive."""

    def __init__(self, binding: RuntimeBinding, *, site_id: str, task_id: str | None,
                 transport, clock=time.time):
        if not isinstance(binding, RuntimeBinding):
            _reject('INVALID_SNAPSHOT_BINDING')
        binding.validate()
        if (not all(_identifier(value) for key, value in binding.to_dict().items()
                    if key != 'context_version') or not _identifier(site_id)
                or (task_id is not None and not _identifier(task_id))
                or not callable(clock) or not callable(getattr(transport, 'read_snapshot', None))
                or getattr(transport, 'actor_id', None) != binding.actor_id):
            _reject('INVALID_SNAPSHOT_BINDING')
        self.binding, self.site_id, self.task_id = binding, site_id, task_id
        self._transport, self._clock = transport, clock

    def __call__(self) -> KtdSnapshot:
        try:
            response = self._transport.read_snapshot(self.binding, site_id=self.site_id, task_id=self.task_id)
        except Exception:
            _reject('SNAPSHOT_TRANSPORT_ERROR')
        if (not isinstance(response, KtdHttpResponse) or type(response.status_code) is not int
                or response.status_code != 200 or not isinstance(response.payload, Mapping)
                or type(response.payload.get('code')) is not int or response.payload['code'] != 200
                or ('success' in response.payload and response.payload['success'] is not True)):
            _reject('SNAPSHOT_UPSTREAM_REJECTED')
        data = response.payload.get('data')
        if not isinstance(data, Mapping) or data.get('schema_version') != SCHEMA_VERSION:
            _reject()
        expected = {'actor_employee_id': self.binding.actor_id, 'project_id': self.binding.project_id,
            'site_id': self.site_id, 'hangar_id': self.binding.hangar_id,
            'drone_id': self.binding.drone_id, 'task_id': self.task_id}
        if any(key not in data or data[key] != value for key, value in expected.items()):
            _reject('RESOURCE_BINDING_MISMATCH')
        if not _fact_version(data.get('snapshot_version')):
            _reject()
        now = self._clock()
        if not _finite(now, 0, float('inf')):
            _reject('INVALID_SNAPSHOT_CLOCK')
        observed = self._observation_time(data, now)
        owner, lease = self._control(data, now)
        profile = data.get('profile')
        if profile not in (None, 'DJI_HANGAR', 'PZ_HANGAR'):
            _reject()
        state = self._state(data.get('state'))
        frame_complete = self._frame_complete(data['telemetry'], profile, state)
        permissions, capabilities, availability = self._authorization(
            data, state, owner, lease, profile, now, frame_complete)
        return KtdSnapshot(actor_id=self.binding.actor_id, project_id=self.binding.project_id,
            hangar_id=self.binding.hangar_id, drone_id=self.binding.drone_id, site_id=self.site_id,
            task_id=self.task_id, context_version=self.binding.context_version,
            version=data['snapshot_version'], observed_at=observed, control_owner=owner, profile=profile,
            permissions=permissions, capabilities=capabilities, state=state,
            control_lease_expires_at=lease, action_availability=availability)

    @staticmethod
    def _observation_time(data, now):
        telemetry = data.get('telemetry')
        if (not isinstance(telemetry, Mapping)
                or telemetry.get('source') not in ('java-udp-cache', 'java-raw-frame')
                or 'sequence' not in telemetry or telemetry['sequence'] is not None
                or not _fact_version(telemetry.get('version'))):
            _reject()
        sampled, received = telemetry.get('sampled_at'), telemetry.get('received_at')
        generated, expires = data.get('generated_at'), data.get('expires_at')
        if sampled is None or received is None or expires is None:
            _reject('AUTHORITATIVE_STATE_UNAVAILABLE')
        if (not all(_finite(value, 0, now) for value in (sampled, received, generated))
                or not _finite(expires, 0, float('inf'))):
            _reject('AUTHORITATIVE_STATE_STALE')
        observed = min(sampled, received)
        if (generated < max(sampled, received) or expires <= now
                or not math.isclose(expires, observed + 5, rel_tol=0, abs_tol=1e-6)):
            _reject('AUTHORITATIVE_STATE_STALE')
        return float(observed)

    @staticmethod
    def _frame_complete(telemetry, profile, state):
        if telemetry['source'] == 'java-udp-cache':
            if 'frame' in telemetry:
                _reject('INVALID_TELEMETRY_FRAME')
            return None
        frame = telemetry.get('frame')
        if (not isinstance(frame, Mapping)
                or set(frame) != {'schema_version', 'profile', 'payload_sha256', 'complete'}
                or frame.get('schema_version') != 'lingxi-telemetry-frame/v1'
                or profile not in ('DJI_HANGAR', 'PZ_HANGAR')
                or frame.get('profile') != profile
                or not _fact_version(frame.get('payload_sha256'))
                or type(frame.get('complete')) is not bool):
            _reject('INVALID_TELEMETRY_FRAME')
        if frame['complete']:
            # 完整只表示同一原报文中有这些字段，不证明厂商航权或设备动作完成。
            height = 'altitude_m' if profile == 'DJI_HANGAR' else 'height_m'
            required = ('longitude', 'latitude', 'yaw_deg', height, 'flight_status', 'airborne')
            if state['online'] is not True or any(state[key] is None for key in required):
                _reject('INVALID_TELEMETRY_FRAME')
        return frame['complete']

    @staticmethod
    def _control(data, now):
        fields = ('control_owner_employee_id', 'control_owner_updated_at',
                  'control_lease_expires_at', 'control_owner_version')
        if any(key not in data for key in fields):
            _reject()
        owner, updated, lease, version = (data[key] for key in fields)
        if ((owner is not None and not _identifier(owner))
                or (updated is not None and not _finite(updated, 0, now))
                or (version is not None and not _fact_version(version))):
            _reject('INVALID_CONTROL_SNAPSHOT')
        if lease is not None:
            if (owner is None or updated is None or version is None
                    or not _finite(lease, 0, float('inf'))
                    or not math.isclose(lease, updated + 300, rel_tol=0, abs_tol=1e-6)):
                _reject('INVALID_CONTROL_SNAPSHOT')
        return owner, lease

    @staticmethod
    def _state(value):
        if not isinstance(value, Mapping) or set(value) != _STATE_FIELDS:
            _reject()
        for key, item in value.items():
            if item is None:
                continue
            if key in _NUMERIC_STATE:
                if not _finite(item, *_NUMERIC_STATE[key]):
                    _reject()
            elif key in {'online', 'airborne'}:
                if type(item) is not bool:
                    _reject()
            elif not isinstance(item, str) or not 0 < len(item) <= 128:
                _reject()
        return copy.deepcopy(dict(value))

    def _authorization(self, data, state, owner, lease, profile, now, frame_complete):
        permissions, capabilities = data.get('permissions'), data.get('capabilities')
        availability = data.get('action_availability')
        if (not isinstance(permissions, list) or any(not isinstance(item, str) for item in permissions)
                or len(set(permissions)) != len(permissions) or set(permissions) - _ACTIONS
                or not isinstance(capabilities, Mapping) or set(capabilities) - (_ACTIONS | {'UAV_MOVE_DISTANCE'})
                or any(type(value) is not bool for value in capabilities.values())
                or not isinstance(availability, Mapping) or set(availability) - _ACTIONS):
            _reject()
        available = set()
        for action, entry in availability.items():
            if (not isinstance(entry, Mapping) or set(entry) != {'available', 'reasons'}
                    or type(entry.get('available')) is not bool or not isinstance(entry.get('reasons'), list)
                    or any(not isinstance(reason, str) or re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', reason) is None
                           for reason in entry['reasons'])
                    or (entry['available'] and entry['reasons'])
                    or (not entry['available'] and not entry['reasons'])):
                _reject()
            if entry['available']:
                if capabilities.get(action) is not True:
                    _reject('SNAPSHOT_AUTHORIZATION_CONFLICT')
                available.add(action)
        if set(permissions) != available:
            _reject('SNAPSHOT_AUTHORIZATION_CONFLICT')
        if available - {'read_state'}:
            if (owner != self.binding.actor_id or lease is None or lease <= now
                    or profile is None or state.get('online') is not True
                    or frame_complete is False):
                _reject('SNAPSHOT_AUTHORIZATION_CONFLICT')
        return frozenset(permissions), copy.deepcopy(dict(capabilities)), copy.deepcopy(dict(availability))
