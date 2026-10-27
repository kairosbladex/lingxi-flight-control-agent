"""Decode one Java command journal record against the locally persisted send."""
from collections.abc import Mapping
from enum import Enum
import re

from .ktd_platform import KtdHttpResponse, KtdPlatformError, _finite, _hash


RECEIPT_SCHEMA = 'lingxi-command-receipt/v1'
REPORT_STATUSES = frozenset({'dispatching', 'submitted', 'accepted', 'in_progress',
                            'completed', 'failed', 'cancelled', 'expired', 'unknown', 'conflict'})
_WAITING = frozenset({'dispatching', 'submitted', 'accepted', 'in_progress'})
_UUID = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')

#: 设备回执通道不可用时的稳定错误码；绝不用 accepted 顶替。
RECEIPT_UNAVAILABLE = 'RECEIPT_UNAVAILABLE'


class DeviceReceiptState(str, Enum):
    """设备回执的规范状态；与平台线格式一一对应，不做语义外推。"""

    DISPATCHING = 'dispatching'
    SUBMITTED = 'submitted'
    ACCEPTED = 'accepted'
    IN_PROGRESS = 'in_progress'
    SUCCEEDED = 'succeeded'
    FAILED = 'failed'
    CANCELLED = 'cancelled'
    EXPIRED = 'expired'
    UNKNOWN = 'unknown'
    CONFLICT = 'conflict'


#: 平台线格式 → 规范状态。``completed`` 是平台既有字段，语义等同 ``succeeded``。
_WIRE_TO_STATE = {
    'dispatching': DeviceReceiptState.DISPATCHING,
    'submitted': DeviceReceiptState.SUBMITTED,
    'accepted': DeviceReceiptState.ACCEPTED,
    'in_progress': DeviceReceiptState.IN_PROGRESS,
    'completed': DeviceReceiptState.SUCCEEDED,
    'failed': DeviceReceiptState.FAILED,
    'cancelled': DeviceReceiptState.CANCELLED,
    'expired': DeviceReceiptState.EXPIRED,
    'unknown': DeviceReceiptState.UNKNOWN,
    'conflict': DeviceReceiptState.CONFLICT,
}

_TERMINAL = frozenset({DeviceReceiptState.SUCCEEDED, DeviceReceiptState.FAILED,
                       DeviceReceiptState.CANCELLED, DeviceReceiptState.CONFLICT})


def canonical_state(wire_status) -> DeviceReceiptState:
    """未知线格式直接拒绝，不映射成 unknown/accepted。"""
    try:
        return _WIRE_TO_STATE[wire_status]
    except (KeyError, TypeError):
        _reject()


def is_terminal(state: DeviceReceiptState) -> bool:
    return state in _TERMINAL


def retry_allowed(state: DeviceReceiptState) -> bool:
    """只有“明确失败”才允许人工发起新的独立动作。

    ``unknown`` / ``expired`` / ``conflict`` 可能已经发出，重试会重复执行；
    ``submitted`` / ``accepted`` / ``in_progress`` 仍在进行中。
    """
    return state is DeviceReceiptState.FAILED


def device_completion_verified(state: DeviceReceiptState) -> bool:
    """回执是平台自己的状态，不是独立测量；任何状态都不构成设备效果证明。"""
    return False


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
            'state': canonical_state(status).value,
            '_dispatch_fingerprint': _hash(dict(ids))}


def _field(record, key):
    if isinstance(record, Mapping):
        return record.get(key)
    return getattr(record, key, None)


def validate_receipt_binding(receipt, *, binding, site_id, task_id, request, trace_id) -> None:
    """查询回执前再次核对设备/任务绑定，避免拿旧资源的回执当本次结果。

    依赖的是本地已持久化的发送记录与当前执行器绑定，不新增平台字段。
    """
    if receipt is None:
        _reject('INVALID_STORED_COMMAND_REPORT')
    if (_field(receipt, 'command_id') != request.command_id
            or _field(receipt, 'idempotency_key') != request.idempotency_key
            or _field(receipt, 'trace_id') != trace_id
            or _field(receipt, 'kind') is not None):
        _reject('COMMAND_RECEIPT_BINDING_MISMATCH')
    # 执行器当前绑定必须与本地发送记录使用同一资源；site/task 由 fingerprint 再校验一次。
    if not all(isinstance(getattr(binding, name, None), str) and getattr(binding, name)
               for name in ('actor_id', 'project_id', 'hangar_id', 'drone_id')):
        _reject('COMMAND_RECEIPT_BINDING_MISMATCH')
    if not isinstance(site_id, str) or not site_id:
        _reject('COMMAND_RECEIPT_BINDING_MISMATCH')
    if task_id is not None and not isinstance(task_id, str):
        _reject('COMMAND_RECEIPT_BINDING_MISMATCH')


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
