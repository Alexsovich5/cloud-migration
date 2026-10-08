import json
import os
import re

import pytest

import state
from state import MigrationState, StateError

HAPPY_PATH = [
    state.ASSESSED,
    state.PROVISIONING,
    state.PROVISIONED,
    state.CONTAINERISED,
    state.DATA_MIGRATED,
    state.VALIDATED,
    state.COMPLETED,
]

ISO_RE = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?$')


@pytest.fixture
def state_path(tmpdir):
    return os.path.join(str(tmpdir), 'state', 'migration_state.json')


def test_load_missing_file_gives_empty_state(state_path):
    st = MigrationState.load(state_path)
    assert st.data == {'version': 1, 'workloads': {}}
    assert not os.path.exists(state_path)


def test_workload_is_created_pending(state_path):
    st = MigrationState.load(state_path)
    entry = st.workload('web-portal')
    assert entry['status'] == state.PENDING
    assert entry['history'] == []
    assert entry['ledger'] == []
    assert entry['error'] is None
    assert st.workload('web-portal') is entry


def test_full_happy_path_is_accepted(state_path):
    st = MigrationState.load(state_path)
    for status in HAPPY_PATH:
        st.transition('web-portal', status)
    entry = st.workload('web-portal')
    assert entry['status'] == state.COMPLETED
    assert [h['status'] for h in entry['history']] == HAPPY_PATH


def test_pending_to_completed_raises(state_path):
    st = MigrationState.load(state_path)
    with pytest.raises(StateError):
        st.transition('web-portal', state.COMPLETED)
    assert st.workload('web-portal')['status'] == state.PENDING
    assert st.workload('web-portal')['history'] == []


def test_completed_to_provisioning_raises(state_path):
    st = MigrationState.load(state_path)
    for status in HAPPY_PATH:
        st.transition('web-portal', status)
    with pytest.raises(StateError):
        st.transition('web-portal', state.PROVISIONING)


def test_failure_then_rollback_then_retry(state_path):
    st = MigrationState.load(state_path)
    st.transition('db', state.PROVISIONING)
    st.transition('db', state.FAILED, error='boom')
    assert st.workload('db')['error'] == 'boom'
    st.transition('db', state.ROLLED_BACK)
    st.transition('db', state.PROVISIONING)
    assert st.workload('db')['status'] == state.PROVISIONING


def test_unknown_status_raises(state_path):
    st = MigrationState.load(state_path)
    with pytest.raises(StateError):
        st.transition('web-portal', 'exploded')


def test_ledger_keeps_insertion_order(state_path):
    st = MigrationState.load(state_path)
    st.record('web-portal', 'security_group', id='sg-1', name='migration-web-portal-abc')
    st.record('web-portal', 'instance', id='i-1')
    st.record('web-portal', 'volume', id='vol-1', instance_id='i-1', device='/dev/xvdf')
    ledger = st.ledger('web-portal')
    assert [e['kind'] for e in ledger] == ['security_group', 'instance', 'volume']
    assert ledger[2] == {'kind': 'volume', 'id': 'vol-1', 'instance_id': 'i-1',
                         'device': '/dev/xvdf'}


def test_clear_ledger_empties_only_that_workload(state_path):
    st = MigrationState.load(state_path)
    st.record('a', 'instance', id='i-a')
    st.record('b', 'instance', id='i-b')
    st.clear_ledger('a')
    assert st.ledger('a') == []
    assert st.ledger('b') == [{'kind': 'instance', 'id': 'i-b'}]


def test_set_stores_arbitrary_key(state_path):
    st = MigrationState.load(state_path)
    st.set('web-portal', 'image', 'registry.local:5000/web-portal:1.0')
    assert st.workload('web-portal')['image'] == 'registry.local:5000/web-portal:1.0'


def test_save_then_load_round_trips(state_path):
    st = MigrationState.load(state_path)
    st.transition('web-portal', state.PROVISIONING)
    st.record('web-portal', 'instance', id='i-1')
    st.set('web-portal', 'data', {'files': 3, 'bytes': 2048})
    st.save()
    reloaded = MigrationState.load(state_path)
    assert reloaded.data == st.data
    assert reloaded.workload('web-portal')['status'] == state.PROVISIONING
    with open(state_path) as handle:
        assert json.load(handle)['version'] == 1


def test_save_leaves_no_tmp_file(state_path):
    st = MigrationState.load(state_path)
    st.workload('web-portal')
    st.save()
    st.save()
    directory = os.path.dirname(state_path)
    assert os.listdir(directory) == ['migration_state.json']
    assert not os.path.exists(state_path + '.tmp')


def test_history_timestamps_are_iso_strings(state_path):
    st = MigrationState.load(state_path)
    st.transition('web-portal', state.ASSESSED)
    st.transition('web-portal', state.PROVISIONING)
    for item in st.workload('web-portal')['history']:
        assert set(item) == {'status', 'at'}
        assert ISO_RE.match(item['at']), item['at']


def test_save_creates_parent_directory(tmpdir):
    path = os.path.join(str(tmpdir), 'state', 'migration_state.json')
    assert not os.path.isdir(os.path.dirname(path))
    st = MigrationState.load(path)
    st.workload('web-portal')
    st.save()
    assert os.path.isfile(path)
