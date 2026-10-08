import compileall
import importlib
import os
import sys
from unittest import mock

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_DIR = os.path.join(REPO_ROOT, 'src')


def test_src_compiles_under_running_interpreter():
    assert compileall.compile_dir(SRC_DIR, quiet=1, force=True)


@pytest.mark.parametrize('module_name',
                         ['sizing', 'aws_connector', 'docker_builder', 'migration_engine'])
def test_module_imports(module_name):
    with mock.patch('boto3.client') as fake_client:
        sys.modules.pop(module_name, None)
        module = importlib.import_module(module_name)
    assert module.__name__ == module_name
    assert not fake_client.called


def test_aws_connector_creates_only_needed_clients():
    import aws_connector
    import sizing
    with mock.patch('boto3.client') as fake_client:
        aws_connector.AWSConnector({'region': 'us-west-2'}, sizing.Catalog({}))
    services = sorted(call[0][0] for call in fake_client.call_args_list)
    assert services == ['ec2', 'rds', 's3']
