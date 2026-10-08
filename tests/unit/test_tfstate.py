import json
import os

import pytest

import migration_engine
import tfstate

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURE = os.path.join(REPO_ROOT, 'tests', 'fixtures', 'terraform.tfstate')
VALID = os.path.join(REPO_ROOT, 'tests', 'fixtures', 'config', 'valid.yml')


def write_state(tmpdir, data):
    path = tmpdir.join('terraform.tfstate')
    path.write(json.dumps(data))
    return str(path)


def test_fixture_outputs_parse():
    outputs = tfstate.read_outputs(FIXTURE)

    assert outputs['vpc_id'] == 'vpc-6f7e8d9c'
    assert outputs['app_security_group_id'] == 'sg-5d1a2c3b'
    assert outputs['artifacts_bucket'] == 'migration-artifacts'
    assert set(outputs) == {'vpc_id', 'public_subnet_ids', 'private_subnet_ids',
                            'app_security_group_id', 'db_subnet_group_name',
                            'artifacts_bucket'}


def test_private_subnet_ids_become_a_list():
    cfg = {'aws': {'vpc_id': '', 'subnet_ids': []}}

    merged = tfstate.merge_outputs(cfg, tfstate.read_outputs(FIXTURE))

    assert merged['aws']['subnet_ids'] == ['subnet-0a1b2c3d', 'subnet-4e5f6a7b']
    assert merged['aws']['vpc_id'] == 'vpc-6f7e8d9c'
    assert merged['aws']['security_group_id'] == 'sg-5d1a2c3b'
    assert merged['aws']['artifacts_bucket'] == 'migration-artifacts'


def test_preset_values_are_not_overwritten():
    cfg = {'aws': {'vpc_id': 'vpc-preset', 'subnet_ids': ['subnet-preset'],
                   'artifacts_bucket': 'my-bucket'}}

    merged = tfstate.merge_outputs(cfg, tfstate.read_outputs(FIXTURE))

    assert merged['aws']['vpc_id'] == 'vpc-preset'
    assert merged['aws']['subnet_ids'] == ['subnet-preset']
    assert merged['aws']['artifacts_bucket'] == 'my-bucket'
    assert merged['aws']['security_group_id'] == 'sg-5d1a2c3b'


def test_missing_outputs_leave_config_alone():
    cfg = {'aws': {'vpc_id': ''}}

    merged = tfstate.merge_outputs(cfg, {'vpc_id': 'vpc-1'})

    assert merged['aws'] == {'vpc_id': 'vpc-1'}


def test_version_3_raises(tmpdir):
    path = write_state(tmpdir, {'version': 3, 'modules': [
        {'path': ['root'], 'outputs': {'vpc_id': {'value': 'vpc-1'}}}]})

    with pytest.raises(ValueError) as exc:
        tfstate.read_outputs(path)
    assert 'version' in str(exc.value)


def test_missing_root_module_raises(tmpdir):
    path = write_state(tmpdir, {'version': 1, 'modules': [
        {'path': ['root', 'network'], 'outputs': {'vpc_id': 'vpc-1'}}]})

    with pytest.raises(ValueError) as exc:
        tfstate.read_outputs(path)
    assert 'root' in str(exc.value)


def test_cli_assess_with_tfstate(tmpdir, capsys):
    output = str(tmpdir.join('results.json'))

    code = migration_engine.main(['--config', VALID, '--state',
                                  str(tmpdir.join('state.json')), '--output', output,
                                  '--tfstate', FIXTURE, '--assess'])

    assert code == 0
    assert 'Assessment complete' in capsys.readouterr()[0]


def test_engine_applies_tfstate_to_config(tmpdir):
    engine = migration_engine.MigrationEngine(
        VALID, str(tmpdir.join('state.json')), tfstate_path=FIXTURE,
        aws=object(), docker=object(), data_migrator=object(), validator=object())

    assert engine.config['aws']['vpc_id'] == 'vpc-6f7e8d9c'
    assert engine.config['aws']['subnet_ids'] == ['subnet-0a1b2c3d', 'subnet-4e5f6a7b']


def test_cli_bad_tfstate_exits_2(tmpdir, capsys):
    path = write_state(tmpdir, {'version': 3, 'modules': []})

    code = migration_engine.main(['--config', VALID, '--state',
                                  str(tmpdir.join('state.json')),
                                  '--tfstate', path, '--assess'])

    assert code == 2
    assert 'version' in capsys.readouterr()[1]
