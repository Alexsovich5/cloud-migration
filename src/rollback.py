"""Undo a workload by deleting its ledger resources in reverse creation order."""

import logging
import time

from botocore.exceptions import ClientError

import state as state_mod

logger = logging.getLogger('rollback')

TERMINATED = 'terminated'
# S3 reports a missing bucket as an error; a missing key is not an error.
S3_NOT_FOUND = ('NoSuchBucket', 'NoSuchKey')
ROLLBACK_FROM = (state_mod.FAILED, state_mod.COMPLETED, state_mod.ROLLED_BACK)


class RollbackError(Exception):
    """Raised when some ledger resources could not be deleted."""


def _delete_s3_object(item, aws, s3):
    try:
        s3.delete_object(Bucket=item['bucket'], Key=item['key'])
    except ClientError as exc:
        if exc.response.get('Error', {}).get('Code') in S3_NOT_FOUND:
            return False
        raise
    return True


def _terminate(item, aws, timeout, interval):
    existed = aws.terminate_instance(item['id'])
    if not existed:
        return False
    deadline = time.time() + timeout
    while True:
        current = aws.instance_state(item['id'])
        if current in (TERMINATED, None):
            return True
        if time.time() >= deadline:
            raise RollbackError('instance {0} still {1} after {2}s'.format(
                item['id'], current, timeout))
        time.sleep(interval)


def _delete(item, aws, s3, timeout, interval):
    """Delete one ledger entry; return False if it was already gone."""
    kind = item['kind']
    if kind == 's3_object':
        return _delete_s3_object(item, aws, s3)
    if kind == 'db_instance':
        return aws.delete_db_instance(item['id'])
    if kind == 'volume':
        return aws.delete_volume(item['id'], item['instance_id'], item['device'])
    if kind == 'instance':
        return _terminate(item, aws, timeout, interval)
    if kind == 'security_group':
        return aws.delete_security_group(item['id'])
    raise RollbackError('unknown ledger kind {0!r}'.format(kind))


def _label(item):
    return item.get('key') if item['kind'] == 's3_object' else item.get('id')


def run(name, state, aws, s3, timeout=60, interval=2):
    """Delete every resource in ``name``'s ledger, newest first.

    Returns the IDs (S3 keys for objects) that were deleted. Resources that
    no longer exist are dropped from the ledger without being listed. If any
    other error occurs, the remaining entries stay in the ledger, the state
    is saved and ``RollbackError`` is raised after every entry was tried.
    """
    status = state.status(name)
    if status not in ROLLBACK_FROM:
        raise state_mod.StateError('{0}: cannot roll back from {1}'.format(name, status))

    ledger = state.ledger(name)
    deleted = []
    remaining = []
    errors = []
    for item in reversed(ledger):
        try:
            if _delete(item, aws, s3, timeout, interval):
                deleted.append(_label(item))
        except (ClientError, RollbackError) as exc:
            logger.error("%s: could not delete %s %s: %s", name, item['kind'],
                         _label(item), exc)
            errors.append('{0} {1}: {2}'.format(item['kind'], _label(item), exc))
            remaining.append(item)

    state.clear_ledger(name)
    for item in reversed(remaining):
        ids = dict(item)
        state.record(name, ids.pop('kind'), **ids)
    if not errors and status != state_mod.ROLLED_BACK:
        state.transition(name, state_mod.ROLLED_BACK)
    state.save()

    if errors:
        raise RollbackError('{0}: rollback incomplete: {1}'.format(name, '; '.join(errors)))
    return deleted
