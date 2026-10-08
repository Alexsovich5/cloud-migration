import os
import uuid

import pytest

import aws_connector
import config
import sizing
from state import MigrationState
from tests.support import aws

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures('simulators')]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def sample_workload(name):
    cfg = config.load(os.path.join(ROOT, 'config', 'migration.yml'))
    return [w for w in cfg['workloads'] if w['name'] == name][0]


def make_connector():
    cfg = aws.sim_config()
    cfg['allowed_cidr'] = '10.0.0.0/8'
    cfg['default_ami'] = 'ami-1a2b3c4d'
    catalog = sizing.Catalog.from_file(os.path.join(ROOT, 'config', 'pricing.yml'))
    return aws_connector.AWSConnector(cfg, catalog)


def rollback(connector, ledger):
    for item in reversed(ledger):
        if item['kind'] == 'volume':
            connector.delete_volume(item['id'], item['instance_id'], item['device'])
        elif item['kind'] == 'instance':
            connector.terminate_instance(item['id'])
        elif item['kind'] == 'security_group':
            connector.delete_security_group(item['id'])
        elif item['kind'] == 'db_instance':
            connector.delete_db_instance(item['id'])


@pytest.fixture
def provisioned(request, tmpdir):
    connector = make_connector()
    st = MigrationState(os.path.join(str(tmpdir), 'state.json'))
    request.addfinalizer(lambda: rollback(connector, st.ledger('file-processor')))
    return connector, st


def by_kind(ledger, kind):
    return [item for item in ledger if item['kind'] == kind]


def test_file_processor_instance_volume_and_group(provisioned):
    connector, st = provisioned
    ec2 = aws.client('ec2')

    result = connector.provision(sample_workload('file-processor'), st)

    ledger = st.ledger('file-processor')
    instance_id = by_kind(ledger, 'instance')[0]['id']
    volume_id = by_kind(ledger, 'volume')[0]['id']
    group = by_kind(ledger, 'security_group')[0]
    assert result == {'instance_id': instance_id, 'volume_id': volume_id,
                      'security_group_id': group['id']}

    reservations = ec2.describe_instances(InstanceIds=[instance_id])['Reservations']
    instances = [i for r in reservations for i in r['Instances']
                 if i['InstanceId'] == instance_id]
    assert len(instances) == 1
    assert instances[0]['InstanceType'] == 't2.medium'
    tags = dict((t['Key'], t['Value']) for t in instances[0].get('Tags', []))
    assert tags['Name'] == 'file-processor'

    volumes = [v for v in ec2.describe_volumes()['Volumes'] if v['VolumeId'] == volume_id]
    assert len(volumes) == 1
    assert volumes[0]['Size'] == 100
    assert [a['InstanceId'] for a in volumes[0]['Attachments']] == [instance_id]

    groups = [g for g in ec2.describe_security_groups()['SecurityGroups']
              if g['GroupId'] == group['id']]
    assert len(groups) == 1
    assert groups[0]['GroupName'] == group['name']
    ports = [p['FromPort'] for p in groups[0]['IpPermissions']]
    assert 9000 in ports


def test_second_provision_gets_a_new_group(provisioned):
    connector, st = provisioned
    workload = sample_workload('file-processor')

    connector.provision(workload, st)
    connector.provision(workload, st)

    groups = by_kind(st.ledger('file-processor'), 'security_group')
    assert len(groups) == 2
    assert groups[0]['id'] != groups[1]['id']
    assert groups[0]['name'] != groups[1]['name']


def test_create_database_returns_identifier(tmpdir, monkeypatch, request):
    monkeypatch.setenv('IT_DB_PASSWORD', 'demo-password')
    connector = make_connector()
    st = MigrationState(os.path.join(str(tmpdir), 'state.json'))
    db_name = 'it_db_{0}'.format(uuid.uuid4().hex[:8])
    workload = {'name': 'db-probe', 'cpu': 1, 'memory': 1,
                'database': {'engine': 'mysql', 'name': db_name, 'username': 'admin',
                             'password_env': 'IT_DB_PASSWORD', 'storage': 10}}
    request.addfinalizer(lambda: rollback(connector, st.ledger('db-probe')))

    db_id = connector.create_database(workload, st)

    expected = 'migrated-' + db_name.replace('_', '-')
    assert db_id == expected
    listed = [d['DBInstanceIdentifier']
              for d in aws.client('rds').describe_db_instances()['DBInstances']]
    assert expected in listed
    assert st.ledger('db-probe') == [{'kind': 'db_instance', 'id': expected}]
