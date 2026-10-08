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
