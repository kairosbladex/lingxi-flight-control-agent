"""Decode one Java command journal record against the locally persisted send."""
from collections.abc import Mapping
import re

from .ktd_platform import KtdHttpResponse, KtdPlatformError, _finite, _hash


RECEIPT_SCHEMA = 'lingxi-command-receipt/v1'
REPORT_STATUSES = frozenset({'dispatching', 'submitted', 'accepted', 'in_progress',
                            'completed', 'failed', 'cancelled', 'expired', 'unknown', 'conflict'})
_WAITING = frozenset({'dispatching', 'submitted', 'accepted', 'in_progress'})
_UUID = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')


def _reject(code='INVALID_COMMAND_RECEIPT'):
    raise KtdPlatformError(code) from None


def decode_command_receipt(response, *, binding, site_id, task_id, request, trace_id, now):
    """A device report is evidence of its own status, never a new authorization."""
    if (not isinstance(response, KtdHttpResponse) or type(response.status_code) is not int
            or response.status_code != 200 or not isinstance(response.payload, Mapping)
            or type(response.payload.get('code')) is not int or response.payload['code'] != 200
            or ('success' in response.payload and response.payload['success'] is not True)):
        _reject('COMMAND_RECEIPT_UPSTREAM_REJECTED')
    data = response.payload.get('data')
    if not isinstance(data, Mapping) or data.get('schema_version') != RECEIPT_SCHEMA:
        _reject()
    expected = {'command_id': request.command_id, 'idempotency_key': request.idempotency_key,
        'trace_id': trace_id, 'actor_employee_id': binding.actor_id, 'project_id': binding.project_id,
        'site_id': site_id, 'hangar_id': binding.hangar_id, 'drone_id': binding.drone_id,
        'task_id': task_id, 'action': request.action}
    if any(key not in data or data[key] != value for key, value in expected.items()):
        _reject('COMMAND_RECEIPT_BINDING_MISMATCH')
    parameters = data.get('parameters')
    if not isinstance(parameters, Mapping) or set(parameters) != set(request.parameters):
        _reject('COMMAND_RECEIPT_BINDING_MISMATCH')
    for key, value in parameters.items():
        wanted = request.parameters[key]
        if (not _finite(value, -float('inf'), float('inf'))
                or type(wanted) not in (int, float) or value != wanted):
            _reject('COMMAND_RECEIPT_BINDING_MISMATCH')
    status = data.get('status')
    if (not isinstance(status, str) or status not in REPORT_STATUSES
            or type(data.get('version')) is not int or not 0 <= data['version'] < 2**53
            or data.get('device_effect') != 'not_verified'):
        _reject()
    ids = data.get('dispatch_ids')
    if (not isinstance(ids, Mapping) or set(ids) != {'bid', 'tid', 'fly_to_id'}
            or any(not isinstance(value, str) or _UUID.fullmatch(value) is None for value in ids.values())):
        _reject()
    if not _finite(now, 0, float('inf')):
        _reject()
    submitted, updated, expires = (data.get(key) for key in ('submitted_at', 'updated_at', 'expires_at'))
    if (not _finite(submitted, 0, now) or not _finite(updated, submitted, now)
            or not _finite(expires, submitted, float('inf')) or expires <= submitted):
        _reject()
    if (status == 'expired' and now < expires) or (status in _WAITING and now >= expires):
        _reject()
    if any(key not in data for key in ('reported_at', 'received_at', 'error_code')):
        _reject()
    reported, received, error = (data[key] for key in ('reported_at', 'received_at', 'error_code'))
    if (error is not None and (not isinstance(error, str) or re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', error) is None)):
        _reject()
    if received is not None and not _finite(received, submitted, updated):
        _reject()
    if reported is not None and (received is None or not _finite(reported, submitted, received)):
        _reject()
    if status == 'completed' and (reported is None or received is None or received > expires or error is not None):
        _reject()
    # Browser/model projections never contain device serials, vendor IDs or actor identity.
    return {**{key: data[key] for key in ('status', 'version', 'submitted_at', 'updated_at',
            'expires_at', 'reported_at', 'received_at', 'error_code', 'trace_id', 'device_effect')},
            '_dispatch_fingerprint': _hash(dict(ids))}


def validate_report_update(previous, current, now):
    """Checked inside the local journal transaction, including across executors."""
    if previous is None:
        return
    if not isinstance(previous, Mapping) or type(previous.get('version')) is not int:
        _reject('INVALID_STORED_COMMAND_REPORT')
    if current['version'] < previous['version']:
        _reject('STALE_COMMAND_RECEIPT')
    if any(current.get(key) != previous.get(key) for key in
           ('submitted_at', 'expires_at', 'trace_id', 'device_effect', '_dispatch_fingerprint')):
        _reject('COMMAND_RECEIPT_CONFLICT')
    if current['version'] == previous['version'] and current != previous:
        # Java's GET can project an unresolved record as expired without writing a row.
        if not (previous['status'] in _WAITING | {'unknown'} and current['status'] == 'expired'
                and now >= current['expires_at'] and all(current[key] == previous.get(key)
                for key in current if key not in {'status', 'error_code'})):
            _reject('COMMAND_RECEIPT_CONFLICT')
    if (previous['status'] in {'completed', 'failed', 'cancelled', 'conflict'}
            and current['status'] not in {previous['status'], 'conflict'}):
        _reject('COMMAND_RECEIPT_CONFLICT')
