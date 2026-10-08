import uuid

import pytest

import aws_connector
import sizing
from tests.support import aws

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures('simulators')]


def unique(prefix):
    return '{0}-{1}'.format(prefix, uuid.uuid4().hex[:8])


def test_ec2_describe_instances_returns_reservations():
    response = aws.client('ec2').describe_instances()
    assert 'Reservations' in response


def test_s3_create_bucket_path_style():
    s3 = aws.client('s3')
    s3.create_bucket(Bucket='probe')
    names = [b['Name'] for b in s3.list_buckets()['Buckets']]
    assert 'probe' in names


def test_s3_bucket_acl_reports_the_list_buckets_owner():
    s3 = aws.client('s3')
    bucket = unique('acl')
    s3.create_bucket(Bucket=bucket)
    own = s3.list_buckets()['Owner']['ID']
    assert own
    assert s3.get_bucket_acl(Bucket=bucket)['Owner']['ID'] == own


def test_rds_describe_db_instances_returns_list():
    response = aws.client('rds').describe_db_instances()
    assert isinstance(response['DBInstances'], list)


def test_connector_clients_reach_simulators():
    connector = aws_connector.AWSConnector(aws.sim_config(), sizing.Catalog({}))
    bucket = unique('connector')
    connector.s3.create_bucket(Bucket=bucket)
    names = [b['Name'] for b in aws.client('s3').list_buckets()['Buckets']]
    assert bucket in names
    assert 'Reservations' in connector.ec2.describe_instances()
