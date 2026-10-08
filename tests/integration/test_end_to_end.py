import json
import os
import socket
import uuid

import pytest
import yaml

import migration_engine
import rollback
import state as state_mod
from state import MigrationState
from tests.support import aws

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures('simulators')]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
E2E_CONFIG = os.path.join(ROOT, 'tests', 'fixtures', 'e2e', 'migration.yml')


def closed_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def ids_of(ledger, kind):
    return [item['id'] for item in ledger if item['kind'] == kind]


def instance_state(instance_id):
    response = aws.client('ec2').describe_instances(InstanceIds=[instance_id])
    for reservation in response['Reservations']:
        for instance in reservation['Instances']:
            if instance['InstanceId'] == instance_id:
                return instance['State']['Name']
    return None


@pytest.fixture
def e2e(tmpdir, monkeypatch, request):
    """Write a runnable copy of the e2e config and capture ledgers before rollback."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(('127.0.0.1', 0))
    server.listen(5)
    request.addfinalizer(server.close)

    with open(E2E_CONFIG) as handle:
        cfg = yaml.safe_load(handle)
    cfg['pricing_file'] = os.path.join(ROOT, cfg['pricing_file'])
    cfg['docker']['command'] = os.path.join(ROOT, cfg['docker']['command'])
    workloads = dict((w['name'], w) for w in cfg['workloads'])
    portal = workloads['web-portal']
    portal['data_path'] = os.path.join(ROOT, portal['data_path'])
    portal['source_path'] = str(tmpdir.mkdir('web-portal-src'))
    # moto keeps RDS instances for the container's lifetime, so the
    # identifier derived from the database name must be unique per run.
    portal['database']['name'] = 'portal_db_{0}'.format(uuid.uuid4().hex[:8])
    portal['validation']['port'] = server.getsockname()[1]
    workloads['broken-app']['validation']['port'] = closed_port()

    config_path = str(tmpdir.join('migration.yml'))
    with open(config_path, 'w') as handle:
        yaml.safe_dump(cfg, handle, default_flow_style=False)

    docker_log = str(tmpdir.join('docker.log'))
    monkeypatch.setenv('FAKE_DOCKER_LOG', docker_log)
    monkeypatch.setenv('PORTAL_DB_PASSWORD', 'demo')

    captured = {}
    real_run = rollback.run

    def spy(name, st, aws_connector, s3, *args, **kwargs):
        captured[name] = list(st.ledger(name))
        return real_run(name, st, aws_connector, s3, *args, **kwargs)
    monkeypatch.setattr(rollback, 'run', spy)

    state_path = str(tmpdir.join('state', 'migration_state.json'))
    return {'config': config_path, 'state': state_path, 'docker_log': docker_log,
            'captured': captured}


def test_full_migration_then_rollback(e2e):
    args = ['--config', e2e['config'], '--state', e2e['state']]

    assert migration_engine.main(args + ['--migrate']) == 1

    st = MigrationState.load(e2e['state'])

    portal = st.workload('web-portal')
    assert st.status('web-portal') == state_mod.COMPLETED
    assert portal['image'] == 'registry.local:5000/web-portal:1.0'
    assert portal['data']['files'] == 2
    ledger = st.ledger('web-portal')
    assert [item['kind'] for item in ledger] == [
        'security_group', 'instance', 'volume', 'db_instance', 's3_object', 's3_object']
    portal_instance = ids_of(ledger, 'instance')[0]
    assert instance_state(portal_instance) == 'running'
    db_id = ids_of(ledger, 'db_instance')[0]
    listed = [d['DBInstanceIdentifier']
              for d in aws.client('rds').describe_db_instances()['DBInstances']]
    assert db_id in listed

    assert st.status('broken-app') == state_mod.ROLLED_BACK
    history = [h['status'] for h in st.workload('broken-app')['history']]
    assert history[-2:] == [state_mod.FAILED, state_mod.ROLLED_BACK]
    assert st.ledger('broken-app') == []
    broken_instance = ids_of(e2e['captured']['broken-app'], 'instance')[0]
    assert instance_state(broken_instance) == 'terminated'

    with open(e2e['docker_log']) as handle:
        calls = [json.loads(line) for line in handle if line.strip()]
    assert ['push', 'registry.local:5000/web-portal:1.0'] in calls

    assert migration_engine.main(args + ['--rollback', 'web-portal']) == 0

    st = MigrationState.load(e2e['state'])
    assert st.status('web-portal') == state_mod.ROLLED_BACK
    assert st.ledger('web-portal') == []
    assert instance_state(portal_instance) == 'terminated'
    listed = [d['DBInstanceIdentifier']
              for d in aws.client('rds').describe_db_instances()['DBInstances']]
    assert db_id not in listed
