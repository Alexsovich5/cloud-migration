import json
import os
from unittest import mock

import pytest

import migration_engine
import state as state_mod
from migration_engine import MigrationEngine
from state import MigrationState

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PRICING = os.path.join(ROOT, 'config', 'pricing.yml')

CONFIG = """
pricing_file: {pricing}
aws:
  default_ami: ami-1a2b3c4d
docker:
  registry: registry.local:5000
workloads:
  - name: web-portal
    type: stateless
    runtime: python
    cpu: 2
    memory: 4
    ports: [80]
    containerizable: true
    data_path: {data}
    database:
      engine: mysql
      host: db.internal
      port: 3306
  - name: batch-job
    type: legacy
    runtime: java
    cpu: 1
    memory: 2
"""


@pytest.fixture
def paths(tmpdir):
    data = tmpdir.mkdir('data')
    data.join('a.txt').write('a')
    config_path = tmpdir.join('migration.yml')
    config_path.write(CONFIG.format(pricing=PRICING, data=str(data)))
    return str(config_path), str(tmpdir.join('state', 'migration_state.json'))


@pytest.fixture
def parts():
    """Collaborator mocks attached to one parent so call order is recorded."""
    parent = mock.MagicMock()
    parent.aws.provision.return_value = {'security_group_id': 'sg-1',
                                         'instance_id': 'i-1', 'volume_id': None}
    parent.aws.create_database.return_value = 'migrated-web-portal'
    parent.docker.build_image.return_value = 'web-portal:latest'
    parent.docker.push.return_value = 'registry.local:5000/web-portal:latest'
    parent.data.migrate.return_value = 'state/web-portal.manifest.json'
    parent.validator.validate.return_value = (True, [])
    return parent


def make_engine(paths, parts):
    config_path, state_path = paths
    return MigrationEngine(config_path, state_path, aws=parts.aws, docker=parts.docker,
                           data_migrator=parts.data, validator=parts.validator)


def statuses(state_path, name):
    with open(state_path) as handle:
        entry = json.load(handle)['workloads'][name]
    return entry['status'], [h['status'] for h in entry['history']], entry


def call_names(parts):
    return [c[0] for c in parts.method_calls]


def test_happy_path_runs_steps_in_order_and_completes(paths, parts):
    engine = make_engine(paths, parts)
    with mock.patch.object(migration_engine.rollback, 'run') as rb:
        ok = engine.execute_migration(only='web-portal')
    assert ok is True
    assert rb.call_count == 0
    assert call_names(parts) == ['aws.provision', 'aws.create_database',
                                 'docker.build_image', 'docker.push',
                                 'data.migrate', 'validator.validate']
    status, history, entry = statuses(paths[1], 'web-portal')
    assert status == state_mod.COMPLETED
    assert history == ['provisioning', 'provisioned', 'containerised',
                       'data_migrated', 'validated', 'completed']
    assert entry['image'] == 'registry.local:5000/web-portal:latest'


def test_plain_workload_goes_provisioned_to_validated(paths, parts):
    engine = make_engine(paths, parts)
    with mock.patch.object(migration_engine.rollback, 'run'):
        assert engine.execute_migration(only='batch-job') is True
    assert call_names(parts) == ['aws.provision', 'validator.validate']
    status, history, _ = statuses(paths[1], 'batch-job')
    assert status == state_mod.COMPLETED
    assert history == ['provisioning', 'provisioned', 'validated', 'completed']


def fake_rollback(name, st, aws, s3):
    st.transition(name, state_mod.ROLLED_BACK)
    st.save()
    return []


def test_provision_error_rolls_back_and_next_workload_runs(paths, parts):
    parts.aws.provision.side_effect = [RuntimeError('quota exceeded'),
                                       {'security_group_id': 'sg-2',
                                        'instance_id': 'i-2', 'volume_id': None}]
    engine = make_engine(paths, parts)
    with mock.patch.object(migration_engine.rollback, 'run',
                           side_effect=fake_rollback) as rb:
        ok = engine.execute_migration()
    assert ok is False
    assert rb.call_count == 1
    assert rb.call_args[0][0] == 'web-portal'
    status, history, entry = statuses(paths[1], 'web-portal')
    assert status == state_mod.ROLLED_BACK
    assert history == ['provisioning', 'failed', 'rolled_back']
    assert parts.docker.build_image.call_count == 0
    assert statuses(paths[1], 'batch-job')[0] == state_mod.COMPLETED


def test_failed_status_keeps_error_message(paths, parts):
    parts.aws.provision.side_effect = RuntimeError('quota exceeded')
    engine = make_engine(paths, parts)
    with mock.patch.object(migration_engine.rollback, 'run') as rb:
        engine.execute_migration(only='web-portal')
    st = rb.call_args[0][1]
    assert st.status('web-portal') == state_mod.FAILED
    assert st.workload('web-portal')['error'] == 'quota exceeded'


def test_validation_failure_rolls_back(paths, parts):
    parts.validator.validate.return_value = (False, ['instance i-1 state stopped'])
    engine = make_engine(paths, parts)
    with mock.patch.object(migration_engine.rollback, 'run',
                           side_effect=fake_rollback) as rb:
        ok = engine.execute_migration(only='batch-job')
    assert ok is False
    assert rb.call_count == 1
    status, history, _ = statuses(paths[1], 'batch-job')
    assert status == state_mod.ROLLED_BACK
    assert history == ['provisioning', 'provisioned', 'failed', 'rolled_back']


def test_migrate_on_completed_workload_is_refused(paths, parts, capsys):
    st = MigrationState(paths[1])
    for status in ('provisioning', 'provisioned', 'validated', 'completed'):
        st.transition('batch-job', status)
    st.save()
    with open(paths[1]) as handle:
        before = handle.read()

    engine = make_engine(paths, parts)
    with mock.patch.object(migration_engine.rollback, 'run') as rb:
        ok = engine.execute_migration(only='batch-job')

    assert ok is False
    assert 'batch-job: already completed, use --rollback first' in capsys.readouterr()[1]
    assert rb.call_count == 0
    assert parts.aws.provision.call_count == 0
    with open(paths[1]) as handle:
        assert handle.read() == before


def test_rollback_workload_recovers_interrupted_run(paths, parts):
    st = MigrationState(paths[1])
    st.transition('web-portal', state_mod.PROVISIONING)
    st.record('web-portal', 'security_group', id='sg-9', name='migration-web-portal-x')
    st.save()

    engine = make_engine(paths, parts)
    with mock.patch.object(migration_engine.rollback, 'run',
                           side_effect=fake_rollback) as rb:
        engine.rollback_workload('web-portal')

    assert rb.call_count == 1
    status, history, entry = statuses(paths[1], 'web-portal')
    assert status == state_mod.ROLLED_BACK
    assert history == ['provisioning', 'failed', 'rolled_back']


def test_rollback_workload_marks_interrupted_error(paths, parts):
    st = MigrationState(paths[1])
    st.transition('web-portal', state_mod.PROVISIONING)
    st.save()
    engine = make_engine(paths, parts)
    with mock.patch.object(migration_engine.rollback, 'run') as rb:
        engine.rollback_workload('web-portal')
    passed = rb.call_args[0][1]
    assert passed.status('web-portal') == state_mod.FAILED
    assert passed.workload('web-portal')['error'] == 'interrupted run'


def test_assess_moves_pending_to_assessed_only(paths, parts, tmpdir):
    st = MigrationState(paths[1])
    for status in ('provisioning', 'provisioned', 'validated', 'completed'):
        st.transition('batch-job', status)
    st.save()

    engine = make_engine(paths, parts)
    out = str(tmpdir.join('assessment.json'))
    results = engine.assess_workloads(out)

    assert [r['name'] for r in results] == ['web-portal', 'batch-job']
    assert os.path.exists(out)
    assert statuses(paths[1], 'web-portal')[0] == state_mod.ASSESSED
    status, history, _ = statuses(paths[1], 'batch-job')
    assert status == state_mod.COMPLETED
    assert history == ['provisioning', 'provisioned', 'validated', 'completed']
