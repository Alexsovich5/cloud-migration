import json
import os
import re

import pytest

import migration_engine
import state as state_mod
from state import MigrationState

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VALID = os.path.join(REPO_ROOT, 'tests', 'fixtures', 'config', 'valid.yml')


def completed_state(path, name):
    st = MigrationState(path)
    for status in (state_mod.PROVISIONING, state_mod.PROVISIONED,
                   state_mod.VALIDATED, state_mod.COMPLETED):
        st.transition(name, status)
    st.save()
    return st


def test_missing_config_exits_2_with_message(tmpdir, capsys):
    missing = str(tmpdir.join('missing.yml'))

    code = migration_engine.main(['--config', missing, '--state',
                                  str(tmpdir.join('state.json')), '--assess'])

    assert code == 2
    err = capsys.readouterr()[1]
    assert missing in err
    assert 'cannot read config' in err


@pytest.mark.parametrize('action', [['--assess'], ['--migrate'], ['--rollback', 'web-portal']])
def test_missing_artifacts_bucket_exits_2_with_message(tmpdir, capsys, action):
    with open(VALID) as handle:
        text = handle.read()
    assert 'artifacts_bucket: unit-artifacts' in text
    config_path = str(tmpdir.join('no-bucket.yml'))
    with open(config_path, 'w') as handle:
        handle.write(text.replace('  artifacts_bucket: unit-artifacts\n', ''))

    code = migration_engine.main(['--config', config_path, '--state',
                                  str(tmpdir.join('state.json'))] + action)

    assert code == 2
    assert 'aws.artifacts_bucket: required' in capsys.readouterr()[1]


def test_assess_and_migrate_are_mutually_exclusive(capsys):
    with pytest.raises(SystemExit) as exc:
        migration_engine.main(['--config', VALID, '--assess', '--migrate'])
    assert exc.value.code == 2
    assert 'not allowed with argument' in capsys.readouterr()[1]


def test_one_action_is_required(capsys):
    with pytest.raises(SystemExit) as exc:
        migration_engine.main(['--config', VALID])
    assert exc.value.code == 2


def test_assess_writes_output_and_prints_summary(tmpdir, capsys):
    output = str(tmpdir.join('results.json'))
    state_path = str(tmpdir.join('state.json'))

    code = migration_engine.main(['--config', VALID, '--state', state_path,
                                  '--output', output, '--assess'])

    assert code == 0
    with open(output) as handle:
        results = json.load(handle)
    assert [r['name'] for r in results] == ['web-portal', 'reporting-engine']
    ready = sum(1 for r in results if r['ready'])
    out = capsys.readouterr()[0]
    assert 'Assessment complete: {0}/2 workloads ready'.format(ready) in out
    st = MigrationState.load(state_path)
    assert st.status('web-portal') == state_mod.ASSESSED


def test_migrate_completed_workload_returns_1(tmpdir, capsys):
    state_path = str(tmpdir.join('state.json'))
    completed_state(state_path, 'web-portal')
    with open(state_path) as handle:
        before = handle.read()

    code = migration_engine.main(['--config', VALID, '--state', state_path,
                                  '--migrate', '--workload', 'web-portal'])

    assert code == 1
    assert 'web-portal: already completed, use --rollback first' in capsys.readouterr()[1]
    with open(state_path) as handle:
        assert handle.read() == before


def test_unknown_workload_name_exits_2(tmpdir, capsys):
    code = migration_engine.main(['--config', VALID, '--state',
                                  str(tmpdir.join('state.json')),
                                  '--migrate', '--workload', 'no-such-app'])

    assert code == 2
    assert re.search(r'no-such-app.*not in config', capsys.readouterr()[1])


def test_rollback_unknown_workload_exits_2(tmpdir, capsys):
    code = migration_engine.main(['--config', VALID, '--state',
                                  str(tmpdir.join('state.json')),
                                  '--rollback', 'no-such-app'])

    assert code == 2
    assert 'no-such-app' in capsys.readouterr()[1]


def test_report_and_migrate_are_mutually_exclusive(capsys):
    with pytest.raises(SystemExit) as exc:
        migration_engine.main(['--config', VALID, '--report', '--migrate'])
    assert exc.value.code == 2
    assert 'not allowed with argument' in capsys.readouterr()[1]


def test_report_prints_state_and_assessment(tmpdir, capsys):
    state_path = str(tmpdir.join('state.json'))
    output = str(tmpdir.join('results.json'))
    completed_state(state_path, 'web-portal')
    assert migration_engine.main(['--config', VALID, '--state', state_path,
                                  '--output', output, '--assess']) == 0
    capsys.readouterr()

    code = migration_engine.main(['--config', VALID, '--state', state_path,
                                  '--output', output, '--report'])

    assert code == 0
    out = capsys.readouterr()[0]
    assert 'WORKLOAD' in out
    assert 'TOTAL 2 workloads (1 assessed, 1 completed)' in out


def test_report_json_lists_config_workloads_without_state(tmpdir, capsys):
    code = migration_engine.main(['--config', VALID, '--state',
                                  str(tmpdir.join('none.json')),
                                  '--output', str(tmpdir.join('none-results.json')),
                                  '--report', '--format', 'json'])

    assert code == 0
    data = json.loads(capsys.readouterr()[0])
    names = sorted(w['name'] for w in data['workloads'])
    assert names == ['reporting-engine', 'web-portal']
    assert data['totals']['by_status'] == {'pending': 2}
