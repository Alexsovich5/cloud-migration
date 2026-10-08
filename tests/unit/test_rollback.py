import os
from unittest import mock

import pytest
from botocore.exceptions import ClientError

import rollback
import state as state_mod
from state import MigrationState


@pytest.fixture
def st(tmpdir):
    st = MigrationState(os.path.join(str(tmpdir), 'state.json'))
    st.record('app', 'security_group', id='sg-1', name='migration-app-1')
    st.record('app', 'instance', id='i-1')
    st.record('app', 'volume', id='vol-1', instance_id='i-1', device='/dev/xvdf')
    st.record('app', 'db_instance', id='migrated-app')
    st.record('app', 's3_object', bucket='bkt', key='app/a.txt')
    st.transition('app', state_mod.PROVISIONING)
    st.transition('app', state_mod.FAILED, error='boom')
    return st


def fake_aws(calls):
    aws = mock.MagicMock()
    aws.delete_db_instance.side_effect = lambda i: calls.append(('db_instance', i)) or True
    aws.delete_volume.side_effect = (
        lambda v, i, d: calls.append(('volume', v, i, d)) or True)
    aws.terminate_instance.side_effect = lambda i: calls.append(('instance', i)) or True
    aws.instance_state.return_value = 'terminated'
    aws.delete_security_group.side_effect = (
        lambda g: calls.append(('security_group', g)) or True)
    return aws


def fake_s3(calls):
    s3 = mock.MagicMock()
    s3.delete_object.side_effect = (
        lambda Bucket, Key: calls.append(('s3_object', Bucket, Key)) or {})
    return s3


def test_rollback_deletes_in_reverse_ledger_order(st):
    calls = []

    deleted = rollback.run('app', st, fake_aws(calls), fake_s3(calls), interval=0)

    assert calls == [('s3_object', 'bkt', 'app/a.txt'),
                     ('db_instance', 'migrated-app'),
                     ('volume', 'vol-1', 'i-1', '/dev/xvdf'),
                     ('instance', 'i-1'),
                     ('security_group', 'sg-1')]
    assert deleted == ['app/a.txt', 'migrated-app', 'vol-1', 'i-1', 'sg-1']
    assert st.ledger('app') == []
    assert st.status('app') == state_mod.ROLLED_BACK
    assert MigrationState.load(st.path).ledger('app') == []


def test_rollback_waits_for_instance_terminated(st):
    calls = []
    aws = fake_aws(calls)
    aws.instance_state.side_effect = ['shutting-down', 'shutting-down', 'terminated']

    rollback.run('app', st, aws, fake_s3(calls), interval=0)

    assert aws.instance_state.call_count == 3


def test_rollback_times_out_waiting_for_termination(st):
    calls = []
    aws = fake_aws(calls)
    aws.instance_state.return_value = 'shutting-down'

    with pytest.raises(rollback.RollbackError):
        rollback.run('app', st, aws, fake_s3(calls), timeout=0, interval=0)

    assert st.ledger('app') == [{'kind': 'instance', 'id': 'i-1'}]
    assert st.status('app') == state_mod.FAILED


def test_second_rollback_is_a_noop(st):
    calls = []
    aws, s3 = fake_aws(calls), fake_s3(calls)
    rollback.run('app', st, aws, s3, interval=0)
    del calls[:]

    assert rollback.run('app', st, aws, s3, interval=0) == []
    assert calls == []
    assert st.status('app') == state_mod.ROLLED_BACK


def test_not_found_counts_as_already_deleted(st):
    calls = []
    aws = fake_aws(calls)
    aws.delete_volume.side_effect = lambda v, i, d: False
    aws.delete_db_instance.side_effect = lambda i: False

    deleted = rollback.run('app', st, aws, fake_s3(calls), interval=0)

    assert deleted == ['app/a.txt', 'i-1', 'sg-1']
    assert st.ledger('app') == []
    assert st.status('app') == state_mod.ROLLED_BACK


def test_other_error_leaves_entry_and_raises(st):
    calls = []
    aws = fake_aws(calls)
    aws.delete_db_instance.side_effect = ClientError(
        {'Error': {'Code': 'InvalidDBInstanceState', 'Message': 'busy'}}, 'DeleteDBInstance')

    with pytest.raises(rollback.RollbackError) as excinfo:
        rollback.run('app', st, aws, fake_s3(calls), interval=0)

    assert 'migrated-app' in str(excinfo.value)
    assert ('security_group', 'sg-1') in calls
    assert st.ledger('app') == [{'kind': 'db_instance', 'id': 'migrated-app'}]
    assert st.status('app') == state_mod.FAILED
    assert MigrationState.load(st.path).ledger('app') == st.ledger('app')


def test_missing_bucket_counts_as_deleted(st):
    calls = []
    s3 = mock.MagicMock()
    s3.delete_object.side_effect = ClientError(
        {'Error': {'Code': 'NoSuchBucket', 'Message': 'gone'}}, 'DeleteObject')

    deleted = rollback.run('app', st, fake_aws(calls), s3, interval=0)

    assert 'app/a.txt' not in deleted
    assert st.ledger('app') == []


def test_rollback_refuses_in_progress_workload(tmpdir):
    st = MigrationState(os.path.join(str(tmpdir), 'state.json'))
    st.record('app', 'instance', id='i-1')
    st.transition('app', state_mod.PROVISIONING)
    calls = []

    with pytest.raises(state_mod.StateError):
        rollback.run('app', st, fake_aws(calls), fake_s3(calls), interval=0)

    assert calls == []
