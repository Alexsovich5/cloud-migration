import os
import socket
import uuid

import pytest

import rollback
import state as state_mod
from data_migration import DataMigrator
from state import MigrationState
from tests.integration.test_provisioning import make_connector, sample_workload
from tests.support import aws
from validation import Validator

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures('simulators')]


@pytest.fixture
def migrated(request, tmpdir):
    connector = make_connector()
    s3 = aws.client('s3')
    migrator = DataMigrator(s3, 'rollback-it-{0}'.format(uuid.uuid4().hex[:12]))
    st = MigrationState(os.path.join(str(tmpdir), 'state.json'))
    data_dir = tmpdir.mkdir('data')
    data_dir.join('batch-001.csv').write('id,size\n1,512\n')

    def cleanup():
        if st.ledger('file-processor'):
            if st.status('file-processor') != state_mod.FAILED:
                st.transition('file-processor', state_mod.FAILED)
            rollback.run('file-processor', st, connector, s3)
    request.addfinalizer(cleanup)

    workload = dict(sample_workload('file-processor'), data_path=str(data_dir))
    st.transition('file-processor', state_mod.PROVISIONING)
    connector.provision(workload, st)
    st.transition('file-processor', state_mod.PROVISIONED)
    migrator.migrate(workload, st, manifest_dir=str(tmpdir))
    st.transition('file-processor', state_mod.DATA_MIGRATED)
    return connector, s3, migrator, st, workload


def ids_of(ledger, kind):
    return [item['id'] for item in ledger if item['kind'] == kind]


def test_validate_passes_for_migrated_workload(migrated, request):
    connector, s3, migrator, st, workload = migrated
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(('127.0.0.1', 0))
    server.listen(1)
    request.addfinalizer(server.close)
    host, port = server.getsockname()
    workload = dict(workload, validation={'host': host, 'port': port, 'timeout': 2})

    assert Validator(connector, migrator, timeout=10).validate(workload, st) == (True, [])


def test_rollback_removes_every_ledger_resource(migrated):
    connector, s3, migrator, st, workload = migrated
    ec2 = aws.client('ec2')
    ledger = st.ledger('file-processor')
    assert [item['kind'] for item in ledger] == [
        'security_group', 'instance', 'volume', 's3_object']
    instance_id = ids_of(ledger, 'instance')[0]
    volume_id = ids_of(ledger, 'volume')[0]
    group_id = ids_of(ledger, 'security_group')[0]
    st.transition('file-processor', state_mod.FAILED, error='forced')

    deleted = rollback.run('file-processor', st, connector, s3)

    assert deleted == ['file-processor/batch-001.csv', volume_id, instance_id, group_id]
    assert st.ledger('file-processor') == []
    assert st.status('file-processor') == state_mod.ROLLED_BACK

    assert connector.instance_state(instance_id) == 'terminated'
    volumes = [v['VolumeId'] for v in ec2.describe_volumes()['Volumes']]
    assert volume_id not in volumes
    groups = [g['GroupId'] for g in ec2.describe_security_groups()['SecurityGroups']]
    assert group_id not in groups
    listed = s3.list_objects(Bucket=migrator.bucket, Prefix='file-processor/')
    assert listed.get('Contents', []) == []

    assert rollback.run('file-processor', st, connector, s3) == []
