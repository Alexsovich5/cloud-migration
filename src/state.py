"""Persistent per-workload migration status, history and resource ledger."""

import json
import os
from datetime import datetime


class StateError(Exception):
    """Raised on an illegal status transition or an unknown status."""


PENDING = 'pending'
ASSESSED = 'assessed'
PROVISIONING = 'provisioning'
PROVISIONED = 'provisioned'
CONTAINERISED = 'containerised'
DATA_MIGRATED = 'data_migrated'
VALIDATED = 'validated'
COMPLETED = 'completed'
FAILED = 'failed'
ROLLED_BACK = 'rolled_back'

STATUSES = (PENDING, ASSESSED, PROVISIONING, PROVISIONED, CONTAINERISED,
            DATA_MIGRATED, VALIDATED, COMPLETED, FAILED, ROLLED_BACK)

TRANSITIONS = {
    PENDING: (ASSESSED, PROVISIONING),
    ASSESSED: (PROVISIONING,),
    PROVISIONING: (PROVISIONED, FAILED),
    PROVISIONED: (CONTAINERISED, DATA_MIGRATED, VALIDATED, FAILED),
    CONTAINERISED: (DATA_MIGRATED, VALIDATED, FAILED),
    DATA_MIGRATED: (VALIDATED, FAILED),
    VALIDATED: (COMPLETED, FAILED),
    FAILED: (ROLLED_BACK,),
    COMPLETED: (ROLLED_BACK,),
    ROLLED_BACK: (PROVISIONING,),
}

STATE_VERSION = 1


class MigrationState(object):
    """JSON-backed migration state; ``save`` writes atomically."""

    def __init__(self, path, data=None):
        self.path = path
        if data is None:
            data = {'version': STATE_VERSION, 'workloads': {}}
        data.setdefault('version', STATE_VERSION)
        data.setdefault('workloads', {})
        self.data = data

    @classmethod
    def load(cls, path):
        if not os.path.exists(path):
            return cls(path)
        with open(path) as handle:
            return cls(path, json.load(handle))

    def workload(self, name):
        workloads = self.data['workloads']
        if name not in workloads:
            workloads[name] = {
                'status': PENDING,
                'history': [],
                'ledger': [],
                'error': None,
            }
        return workloads[name]

    def status(self, name):
        return self.workload(name)['status']

    def transition(self, name, status, error=None):
        if status not in STATUSES:
            raise StateError('unknown status {0!r}'.format(status))
        entry = self.workload(name)
        current = entry['status']
        if status not in TRANSITIONS.get(current, ()):
            raise StateError('{0}: illegal transition {1} -> {2}'.format(
                name, current, status))
        entry['status'] = status
        entry['history'].append({'status': status,
                                 'at': datetime.utcnow().isoformat()})
        entry['error'] = error
        return entry

    def record(self, workload_name, kind, **ids):
        # The workload is positional-only in practice so that ledger fields
        # such as ``name`` (a security group name) can be passed as keywords.
        item = {'kind': kind}
        item.update(ids)
        self.workload(workload_name)['ledger'].append(item)
        return item

    def ledger(self, name):
        return list(self.workload(name)['ledger'])

    def clear_ledger(self, name):
        self.workload(name)['ledger'] = []

    def set(self, name, key, value):
        self.workload(name)[key] = value

    def save(self):
        directory = os.path.dirname(os.path.abspath(self.path))
        if not os.path.isdir(directory):
            os.makedirs(directory)
        tmp_path = self.path + '.tmp'
        with open(tmp_path, 'w') as handle:
            json.dump(self.data, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self.path)
