from unittest import mock

import aws_connector
import sizing

CATALOG = sizing.Catalog({
    'instances': {'t2.micro': {'vcpu': 1, 'memory_gib': 1, 'hourly_usd': 0.013}},
})


def make_connector(ec2):
    with mock.patch('boto3.session.Session'):
        connector = aws_connector.AWSConnector({'subnet_ids': ['subnet-1']}, CATALOG)
    connector.ec2 = ec2
    return connector


def test_launch_instance_tags_with_create_tags():
    ec2 = mock.MagicMock()
    ec2.run_instances.return_value = {'Instances': [{'InstanceId': 'i-abc123'}]}
    connector = make_connector(ec2)

    instance_id = connector._launch_instance(
        name='portal', instance_type='t2.medium', security_group='sg-1', ami='ami-1')

    assert instance_id == 'i-abc123'
    run_kwargs = ec2.run_instances.call_args[1]
    assert 'TagSpecifications' not in run_kwargs
    assert run_kwargs['SubnetId'] == 'subnet-1'
    ec2.create_tags.assert_called_once_with(
        Resources=['i-abc123'],
        Tags=[
            {'Key': 'Name', 'Value': 'portal'},
            {'Key': 'Project', 'Value': 'cloud-migration'},
            {'Key': 'ManagedBy', 'Value': 'migration-framework'},
        ])


def test_provision_infrastructure_returns_all_resource_ids():
    ec2 = mock.MagicMock()
    ec2.create_security_group.return_value = {'GroupId': 'sg-9'}
    ec2.run_instances.return_value = {'Instances': [{'InstanceId': 'i-9'}]}
    ec2.create_volume.return_value = {'VolumeId': 'vol-9'}
    connector = make_connector(ec2)

    workload = {'name': 'portal', 'cpu': 1, 'memory': 1, 'storage': 20, 'ports': [80]}
    result = connector.provision_infrastructure(workload)

    assert result == {'instance_id': 'i-9', 'volume_id': 'vol-9', 'security_group_id': 'sg-9'}


def test_provision_infrastructure_without_storage_has_no_volume():
    ec2 = mock.MagicMock()
    ec2.create_security_group.return_value = {'GroupId': 'sg-9'}
    ec2.run_instances.return_value = {'Instances': [{'InstanceId': 'i-9'}]}
    connector = make_connector(ec2)

    workload = {'name': 'portal', 'cpu': 1, 'memory': 1, 'ports': []}
    result = connector.provision_infrastructure(workload)

    assert result['volume_id'] is None
    assert not ec2.create_volume.called
