import os
import re
from unittest import mock

import pytest
from botocore.exceptions import ClientError

import aws_connector
import sizing
from state import MigrationState

CATALOG = sizing.Catalog({
    'instances': {
        't2.micro': {'vcpu': 1, 'memory_gib': 1, 'hourly_usd': 0.013},
        't2.medium': {'vcpu': 2, 'memory_gib': 4, 'hourly_usd': 0.052},
    },
})

WORKLOAD = {'name': 'portal', 'cpu': 2, 'memory': 4, 'storage': 20, 'ports': [80, 443]}


def make_connector(aws_cfg=None, ec2=None, rds=None):
    clients = {'ec2': mock.MagicMock() if ec2 is None else ec2,
               'rds': mock.MagicMock() if rds is None else rds,
               's3': mock.MagicMock()}
    session = mock.MagicMock()
    session.client.side_effect = lambda svc, **kwargs: clients[svc]
    cfg = {'region': 'us-east-1', 'allowed_cidr': '10.0.0.0/8',
           'default_ami': 'ami-1a2b3c4d'}
    cfg.update(aws_cfg or {})
    return aws_connector.AWSConnector(cfg, CATALOG, session=session)


def fake_ec2(placement_az='us-east-1c'):
    ec2 = mock.MagicMock()
    ec2.create_security_group.return_value = {'GroupId': 'sg-9'}
    ec2.run_instances.return_value = {'Instances': [{'InstanceId': 'i-9'}]}
    ec2.describe_instances.return_value = {'Reservations': [{'Instances': [
        {'InstanceId': 'i-9', 'Placement': {'AvailabilityZone': placement_az},
         'State': {'Name': 'running'}}]}]}
    ec2.create_volume.return_value = {'VolumeId': 'vol-9'}
    return ec2


@pytest.fixture
def st(tmpdir):
    return MigrationState(os.path.join(str(tmpdir), 'state.json'))


def not_found(code, operation='Op'):
    return ClientError({'Error': {'Code': code, 'Message': 'not found'}}, operation)


def test_provision_call_order(st):
    ec2 = fake_ec2()
    connector = make_connector(ec2=ec2)

    connector.provision(WORKLOAD, st)

    names = [c[0] for c in ec2.mock_calls if not c[0].startswith('describe_')]
    assert names == ['create_security_group', 'create_tags',
                     'authorize_security_group_ingress', 'authorize_security_group_ingress',
                     'run_instances', 'create_tags', 'create_volume', 'attach_volume']


def test_provision_returns_ids_and_records_ledger_in_order(st):
    connector = make_connector(ec2=fake_ec2())

    result = connector.provision(WORKLOAD, st)

    assert result == {'security_group_id': 'sg-9', 'instance_id': 'i-9', 'volume_id': 'vol-9'}
    ledger = st.ledger('portal')
    assert [item['kind'] for item in ledger] == ['security_group', 'instance', 'volume']
    assert ledger[0]['id'] == 'sg-9'
    assert ledger[1]['id'] == 'i-9'
    assert ledger[2] == {'kind': 'volume', 'id': 'vol-9', 'instance_id': 'i-9',
                         'device': '/dev/xvdf'}


def test_provision_saves_state_file(st):
    connector = make_connector(ec2=fake_ec2())

    connector.provision(WORKLOAD, st)

    reloaded = MigrationState.load(st.path)
    assert len(reloaded.ledger('portal')) == 3


def test_security_group_name_is_unique_and_tagged(st):
    ec2 = fake_ec2()
    connector = make_connector(ec2=ec2)

    connector.provision(WORKLOAD, st)
    connector.provision(WORKLOAD, st)

    names = [c[1]['GroupName'] for c in ec2.create_security_group.call_args_list]
    for name in names:
        assert re.match(r'^migration-portal-[0-9a-f]{8}$', name)
    assert names[0] != names[1]
    group_entries = [i for i in st.ledger('portal') if i['kind'] == 'security_group']
    assert [e['name'] for e in group_entries] == names
    first_tag = ec2.create_tags.call_args_list[0][1]
    assert first_tag == {'Resources': ['sg-9'],
                         'Tags': [{'Key': 'Name', 'Value': 'migration-portal'}]}


def test_security_group_vpc_only_when_set(st):
    ec2 = fake_ec2()
    make_connector(ec2=ec2).provision(WORKLOAD, st)
    assert 'VpcId' not in ec2.create_security_group.call_args[1]

    ec2 = fake_ec2()
    make_connector({'vpc_id': 'vpc-1'}, ec2=ec2).provision(WORKLOAD, st)
    assert ec2.create_security_group.call_args[1]['VpcId'] == 'vpc-1'


def test_ingress_per_port_on_allowed_cidr(st):
    ec2 = fake_ec2()
    make_connector({'allowed_cidr': '192.168.0.0/16'}, ec2=ec2).provision(WORKLOAD, st)

    rules = [c[1] for c in ec2.authorize_security_group_ingress.call_args_list]
    assert rules == [
        {'GroupId': 'sg-9', 'IpPermissions': [{'IpProtocol': 'tcp', 'FromPort': port,
                                               'ToPort': port,
                                               'IpRanges': [{'CidrIp': '192.168.0.0/16'}]}]}
        for port in (80, 443)]


def test_run_instances_uses_sizing_ami_and_tags(st):
    ec2 = fake_ec2()
    make_connector(ec2=ec2).provision(WORKLOAD, st)

    kwargs = ec2.run_instances.call_args[1]
    assert kwargs['ImageId'] == 'ami-1a2b3c4d'
    assert kwargs['InstanceType'] == 't2.medium'
    assert kwargs['SecurityGroupIds'] == ['sg-9']
    assert 'SubnetId' not in kwargs
    assert 'TagSpecifications' not in kwargs
    assert ec2.create_tags.call_args_list[1][1] == {
        'Resources': ['i-9'],
        'Tags': [{'Key': 'Name', 'Value': 'portal'},
                 {'Key': 'Project', 'Value': 'cloud-migration'},
                 {'Key': 'ManagedBy', 'Value': 'migration-framework'}]}


def test_workload_ami_and_subnet_override(st):
    ec2 = fake_ec2()
    workload = dict(WORKLOAD, ami='ami-feedbeef')
    make_connector({'subnet_ids': ['subnet-1', 'subnet-2']}, ec2=ec2).provision(workload, st)

    kwargs = ec2.run_instances.call_args[1]
    assert kwargs['ImageId'] == 'ami-feedbeef'
    assert kwargs['SubnetId'] == 'subnet-1'


def test_no_volume_when_storage_is_zero(st):
    ec2 = fake_ec2()
    result = make_connector(ec2=ec2).provision(dict(WORKLOAD, storage=0), st)

    assert result['volume_id'] is None
    assert not ec2.create_volume.called
    assert not ec2.attach_volume.called
    assert [i['kind'] for i in st.ledger('portal')] == ['security_group', 'instance']


def test_volume_uses_placement_az_and_attaches(st):
    ec2 = fake_ec2(placement_az='us-east-1c')
    make_connector(ec2=ec2).provision(WORKLOAD, st)

    ec2.create_volume.assert_called_once_with(Size=20, VolumeType='gp2',
                                              AvailabilityZone='us-east-1c')
    ec2.attach_volume.assert_called_once_with(VolumeId='vol-9', InstanceId='i-9',
                                              Device='/dev/xvdf')


@pytest.mark.parametrize('reported', ['', 'None', None])
def test_empty_placement_az_falls_back_to_region_a(st, reported):
    ec2 = fake_ec2(placement_az=reported)
    make_connector({'region': 'us-east-1'}, ec2=ec2).provision(WORKLOAD, st)

    assert ec2.create_volume.call_args[1]['AvailabilityZone'] == 'us-east-1a'


def test_empty_placement_az_falls_back_to_subnet_az(st):
    ec2 = fake_ec2(placement_az='None')
    ec2.describe_subnets.return_value = {'Subnets': [
        {'SubnetId': 'subnet-1', 'AvailabilityZone': 'us-east-1d'}]}
    make_connector({'subnet_ids': ['subnet-1']}, ec2=ec2).provision(WORKLOAD, st)

    ec2.describe_subnets.assert_called_once_with(SubnetIds=['subnet-1'])
    assert ec2.create_volume.call_args[1]['AvailabilityZone'] == 'us-east-1d'


DB_WORKLOAD = dict(WORKLOAD, database={
    'engine': 'mysql', 'name': 'portal_db', 'username': 'portal_user',
    'password_env': 'TEST_PORTAL_DB_PASSWORD', 'storage': 50})


def test_create_database(st, monkeypatch):
    monkeypatch.setenv('TEST_PORTAL_DB_PASSWORD', 's3cret')
    rds = mock.MagicMock()
    rds.create_db_instance.return_value = {
        'DBInstance': {'DBInstanceIdentifier': 'migrated-portal-db'}}

    db_id = make_connector(rds=rds).create_database(DB_WORKLOAD, st)

    assert db_id == 'migrated-portal-db'
    rds.create_db_instance.assert_called_once_with(
        DBInstanceIdentifier='migrated-portal-db', DBInstanceClass='db.m4.large',
        Engine='mysql', Port=3306, MasterUsername='portal_user', MasterUserPassword='s3cret',
        AllocatedStorage=50, StorageEncrypted=True, MultiAZ=False)
    assert st.ledger('portal') == [{'kind': 'db_instance', 'id': 'migrated-portal-db'}]


def test_create_database_honours_class_and_multi_az(st, monkeypatch):
    monkeypatch.setenv('TEST_PORTAL_DB_PASSWORD', 's3cret')
    rds = mock.MagicMock()
    rds.create_db_instance.return_value = {'DBInstance': {'DBInstanceIdentifier': 'x'}}
    db = dict(DB_WORKLOAD['database'], instance_class='db.t2.small', multi_az=True)

    make_connector(rds=rds).create_database(dict(WORKLOAD, database=db), st)

    kwargs = rds.create_db_instance.call_args[1]
    assert kwargs['DBInstanceClass'] == 'db.t2.small'
    assert kwargs['MultiAZ'] is True


def test_create_database_port_from_config_or_engine_default(st, monkeypatch):
    monkeypatch.setenv('TEST_PORTAL_DB_PASSWORD', 's3cret')
    rds = mock.MagicMock()
    rds.create_db_instance.return_value = {'DBInstance': {'DBInstanceIdentifier': 'x'}}
    connector = make_connector(rds=rds)

    db = dict(DB_WORKLOAD['database'], engine='postgres')
    connector.create_database(dict(WORKLOAD, database=db), st)
    assert rds.create_db_instance.call_args[1]['Port'] == 5432

    connector.create_database(dict(WORKLOAD, database=dict(db, port=6543)), st)
    assert rds.create_db_instance.call_args[1]['Port'] == 6543


def test_create_database_missing_password_env_raises(st, monkeypatch):
    monkeypatch.delenv('TEST_PORTAL_DB_PASSWORD', raising=False)
    rds = mock.MagicMock()
    connector = make_connector(rds=rds)

    with pytest.raises(ValueError) as err:
        connector.create_database(DB_WORKLOAD, st)
    assert 'TEST_PORTAL_DB_PASSWORD' in str(err.value)

    db = dict(DB_WORKLOAD['database'])
    del db['password_env']
    with pytest.raises(ValueError):
        connector.create_database(dict(WORKLOAD, database=db), st)
    assert not rds.create_db_instance.called
    assert st.ledger('portal') == []


def test_instance_state():
    ec2 = fake_ec2()
    assert make_connector(ec2=ec2).instance_state('i-9') == 'running'
    ec2.describe_instances.assert_called_once_with(InstanceIds=['i-9'])


@pytest.mark.parametrize('method,args,ec2_call,code', [
    ('terminate_instance', ('i-1',), 'terminate_instances', 'InvalidInstanceID.NotFound'),
    ('delete_volume', ('vol-1', 'i-1', '/dev/xvdf'), 'detach_volume',
     'InvalidVolume.NotFound'),
    ('delete_security_group', ('sg-1',), 'delete_security_group', 'InvalidGroup.NotFound'),
])
def test_delete_not_found_returns_false(method, args, ec2_call, code):
    ec2 = mock.MagicMock()
    getattr(ec2, ec2_call).side_effect = not_found(code)
    connector = make_connector(ec2=ec2)

    assert getattr(connector, method)(*args) is False


def test_delete_other_errors_propagate():
    ec2 = mock.MagicMock()
    ec2.terminate_instances.side_effect = not_found('UnauthorizedOperation')
    with pytest.raises(ClientError):
        make_connector(ec2=ec2).terminate_instance('i-1')


def test_delete_helpers_return_true_on_success():
    ec2 = mock.MagicMock()
    connector = make_connector(ec2=ec2)
    assert connector.terminate_instance('i-1') is True
    ec2.terminate_instances.assert_called_once_with(InstanceIds=['i-1'])
    assert connector.delete_security_group('sg-1') is True
    ec2.delete_security_group.assert_called_once_with(GroupId='sg-1')


def test_delete_volume_detaches_with_instance_and_device_first():
    ec2 = mock.MagicMock()
    connector = make_connector(ec2=ec2)

    assert connector.delete_volume('vol-1', 'i-1', '/dev/xvdf') is True

    names = [c[0] for c in ec2.mock_calls]
    assert names == ['detach_volume', 'delete_volume']
    ec2.detach_volume.assert_called_once_with(VolumeId='vol-1', InstanceId='i-1',
                                              Device='/dev/xvdf')
    ec2.delete_volume.assert_called_once_with(VolumeId='vol-1')


def test_delete_db_instance_missing_returns_false_without_delete():
    rds = mock.MagicMock()
    rds.describe_db_instances.return_value = {'DBInstances': [
        {'DBInstanceIdentifier': 'other-db'}]}
    connector = make_connector(rds=rds)

    assert connector.delete_db_instance('migrated-portal-db') is False
    rds.describe_db_instances.assert_called_once_with()
    assert not rds.delete_db_instance.called


def test_delete_db_instance_present_skips_final_snapshot():
    rds = mock.MagicMock()
    rds.describe_db_instances.return_value = {'DBInstances': [
        {'DBInstanceIdentifier': 'migrated-portal-db'}]}
    connector = make_connector(rds=rds)

    assert connector.delete_db_instance('migrated-portal-db') is True
    rds.delete_db_instance.assert_called_once_with(
        DBInstanceIdentifier='migrated-portal-db', SkipFinalSnapshot=True)


def test_old_stubs_are_gone():
    for name in ('check_instance_health', 'check_connectivity', 'check_data_integrity',
                 'destroy_infrastructure', 'provision_infrastructure'):
        assert not hasattr(aws_connector.AWSConnector, name)
