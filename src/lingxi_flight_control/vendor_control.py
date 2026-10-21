"""Separate vendor service attempts: request success never establishes flight authority."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
import math
import re
from typing import ClassVar

from .ktd_platform import KtdHttpResponse, KtdPlatformError, _finite, _identifier


VENDOR_METHODS = ('flight_authority_grab', 'drc_mode_enter', 'drc_mode_exit')
VENDOR_ACTION = 'request_vendor_control'
VENDOR_KIND = 'vendor_control_request'
VENDOR_RESULT_SCHEMA = 'lingxi-vendor-control-result/v1'
VENDOR_STATUSES = frozenset({'dispatching', 'submitted', 'request_succeeded', 'failed', 'unknown', 'expired', 'conflict'})
_WAITING = frozenset({'dispatching', 'submitted'})
_TERMINAL = frozenset({'request_succeeded', 'failed', 'conflict'})
_FIELDS = frozenset({'schema_version', 'kind', 'command_id', 'method', 'trace_id', 'idempotency_key',
    'actor_employee_id', 'project_id', 'site_id', 'hangar_id', 'drone_id', 'task_id', 'status', 'result_code',
    'version', 'submitted_at', 'updated_at', 'expires_at', 'reported_at', 'received_at', 'error_code',
    'vendor_authority', 'drc_link', 'device_effect', 'dispatch_ids'})
_UUID = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')


@dataclass(frozen=True)
class VendorControlRequest:
    command_id: str
    idempotency_key: str
    method: str
    context_version: int
    kind: ClassVar[str] = VENDOR_KIND

    @property
    def action(self):
        return VENDOR_ACTION

    @property
    def parameters(self):
        return {'method': self.method}

    def validate(self):
        if (not _identifier(self.command_id) or not _identifier(self.idempotency_key)
                or not isinstance(self.method, str) or self.method not in VENDOR_METHODS
                or type(self.context_version) is not int or not 0 <= self.context_version < 2**53):
            raise ValueError('invalid vendor control request')

    def to_dict(self):
        self.validate()
        return {'kind': self.kind, 'command_id': self.command_id, 'idempotency_key': self.idempotency_key,
                'method': self.method, 'context_version': self.context_version}


def _reject(code='INVALID_VENDOR_RECEIPT'):
    raise KtdPlatformError(code) from None


def decode_vendor_receipt(response, *, binding, site_id, task_id, request, trace_id, now):
    """Decode exactly the Java VO, bound to a locally persisted attempt; internal only."""
    if not isinstance(request, VendorControlRequest):
        _reject('VENDOR_RECEIPT_BINDING_MISMATCH')
    request.validate()
    if (not isinstance(response, KtdHttpResponse) or type(response.status_code) is not int or response.status_code != 200
            or not isinstance(response.payload, Mapping) or type(response.payload.get('code')) is not int
            or response.payload['code'] != 200 or ('success' in response.payload and response.payload['success'] is not True)):
        _reject('VENDOR_RECEIPT_UPSTREAM_REJECTED')
    data = response.payload.get('data')
    if (not isinstance(data, Mapping) or set(data) != _FIELDS
            or data['schema_version'] != VENDOR_RESULT_SCHEMA or data['kind'] != 'vendor_service_result'):
        _reject()
    expected = {'command_id': request.command_id, 'idempotency_key': request.idempotency_key,
        'trace_id': trace_id, 'actor_employee_id': binding.actor_id, 'project_id': binding.project_id,
        'site_id': site_id, 'hangar_id': binding.hangar_id, 'drone_id': binding.drone_id,
        'task_id': task_id, 'method': request.method}
    if any(data[key] != value for key, value in expected.items()):
        _reject('VENDOR_RECEIPT_BINDING_MISMATCH')
    status, result, error = data['status'], data['result_code'], data['error_code']
    if (not isinstance(status, str) or status not in VENDOR_STATUSES
            or type(data['version']) is not int or not 0 <= data['version'] < 2**53
            or data['vendor_authority'] != 'unknown' or data['drc_link'] != 'unknown'
            or data['device_effect'] != 'not_verified'):
        _reject()
    ids = data['dispatch_ids']
    if (not isinstance(ids, Mapping) or set(ids) != {'bid', 'tid'}
            or any(not isinstance(value, str) or _UUID.fullmatch(value) is None for value in ids.values())):
        _reject()
    submitted, updated, expires = (data[key] for key in ('submitted_at', 'updated_at', 'expires_at'))
    if (not _finite(now, 0, float('inf')) or not _finite(submitted, 0, now)
            or not _finite(updated, submitted, now) or not _finite(expires, submitted, float('inf'))
            or not math.isclose(expires - submitted, 120, abs_tol=1e-6)
            or (status == 'expired' and now < expires) or (status in _WAITING and now >= expires)):
        _reject()
    reported, received = data['reported_at'], data['received_at']
    if ((received is not None and (not _finite(received, submitted, updated) or received >= expires))
            or (reported is not None and (received is None or not _finite(reported, submitted, received)))
            or (received is not None and reported is None)):
        _reject()
    if status in {'request_succeeded', 'failed'}:
        if type(result) is not int or not -(2**31) <= result < 2**31 or reported is None or received is None:
            _reject()
        if status == 'request_succeeded' and (result != 0 or error is not None):
            _reject()
        if status == 'failed' and (result == 0 or error != 'DEVICE_RESULT_' + str(result)):
            _reject()
    elif result is not None:
        _reject()
    if error is not None and (not isinstance(error, str) or re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}|DEVICE_RESULT_-[0-9]{1,10}', error) is None):
        _reject()
    if status in _WAITING | {'expired'} and (reported is not None or received is not None or error is not None):
        _reject()
    if status == 'conflict' and (error != 'DEVICE_TERMINAL_CONFLICT' or reported is None or received is None):
        _reject()
    return deepcopy(dict(data))


def validate_vendor_report_update(previous, current, now):
    """Run under the existing journal transaction; late POST cannot replace newer GET."""
    if previous is None:
        return
    if not isinstance(previous, Mapping) or set(previous) != _FIELDS or type(previous.get('version')) is not int:
        _reject('INVALID_STORED_VENDOR_REPORT')
    if current['version'] < previous['version']:
        _reject('STALE_VENDOR_RECEIPT')
    if current['updated_at'] < previous['updated_at']:
        _reject('STALE_VENDOR_RECEIPT')
    mutable = {'status', 'result_code', 'version', 'updated_at', 'reported_at', 'received_at', 'error_code'}
    if any(current[key] != previous[key] for key in _FIELDS - mutable):
        _reject('VENDOR_RECEIPT_CONFLICT')
    if current['version'] == previous['version'] and current != previous:
        if not (previous['status'] in _WAITING and current['status'] == 'expired' and now >= current['expires_at']
                and all(current[key] == previous[key] for key in _FIELDS - {'status', 'error_code'})):
            _reject('VENDOR_RECEIPT_CONFLICT')
    if previous['status'] in _TERMINAL and current['status'] not in {previous['status'], 'conflict'}:
        _reject('VENDOR_RECEIPT_CONFLICT')
    if previous['status'] in _TERMINAL and current['status'] == previous['status']:
        if any(current[key] != previous[key] for key in
               ('result_code', 'reported_at', 'received_at', 'error_code')):
            _reject('VENDOR_RECEIPT_CONFLICT')
    if previous['status'] == 'unknown' and current['status'] == 'expired':
        _reject('VENDOR_RECEIPT_CONFLICT')
