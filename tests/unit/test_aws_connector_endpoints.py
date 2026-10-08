from unittest import mock

import boto3

import aws_connector
import sizing

CATALOG = sizing.Catalog({})


def test_session_clients_receive_endpoint_per_service():
    session = mock.MagicMock()
    cfg = {'region': 'eu-west-1',
           'endpoints': {'ec2': 'http://moto-ec2:5000', 's3': 'http://moto-s3:5001'}}

    aws_connector.AWSConnector(cfg, CATALOG, session=session)

    calls = {}
    for call in session.client.call_args_list:
        calls[call[0][0]] = call[1]
    assert sorted(calls) == ['ec2', 'rds', 's3']
    assert calls['ec2']['endpoint_url'] == 'http://moto-ec2:5000'
    assert calls['s3']['endpoint_url'] == 'http://moto-s3:5001'
    assert calls['rds']['endpoint_url'] is None
    for kwargs in calls.values():
        assert kwargs['region_name'] == 'eu-west-1'


def test_no_endpoints_means_default_aws_endpoints():
    session = mock.MagicMock()

    aws_connector.AWSConnector({}, CATALOG, session=session)

    for call in session.client.call_args_list:
        assert call[1]['endpoint_url'] is None


def test_real_session_s3_client_uses_sigv4():
    session = boto3.session.Session(aws_access_key_id='testing',
                                    aws_secret_access_key='testing')
    connector = aws_connector.AWSConnector(
        {'region': 'us-east-1', 'endpoints': {'s3': 'http://moto-s3:5001'}},
        CATALOG, session=session)

    assert connector.s3.meta.config.signature_version == 's3v4'
    assert connector.s3.meta.endpoint_url == 'http://moto-s3:5001'
