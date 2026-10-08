import json

import report
import state as state_mod
from state import MigrationState


def history(*pairs):
    return [{'status': status, 'at': at} for status, at in pairs]


def build_state(tmpdir):
    st = MigrationState(str(tmpdir.join('state.json')))
    st.data['workloads'] = {
        'web-portal': {
            'status': state_mod.COMPLETED,
            'history': history(
                (state_mod.PROVISIONING, '2015-09-01T10:00:00.250000'),
                (state_mod.PROVISIONED, '2015-09-01T10:00:30'),
                (state_mod.COMPLETED, '2015-09-01T10:01:05.250000')),
            'ledger': [{'kind': 'security_group', 'id': 'sg-1'},
                       {'kind': 'instance', 'id': 'i-1'},
                       {'kind': 'volume', 'id': 'vol-1'}],
            'error': None,
            'data': {'files': 2, 'bytes': 2048, 'manifest': 'm.json'},
        },
        'broken-app': {
            'status': state_mod.ROLLED_BACK,
            'history': history(
                (state_mod.PROVISIONING, '2015-09-01T11:00:00'),
                (state_mod.FAILED, '2015-09-01T11:00:10'),
                (state_mod.ROLLED_BACK, '2015-09-01T12:00:12')),
            'ledger': [],
            'error': 'validation failed',
        },
    }
    return st


ASSESSMENT = [
    {'name': 'web-portal', 'migration_strategy': 'containerize',
     'resource_profile': {'recommended_instance': 't2.medium'},
     'estimated_cost': {'instance': 34.31, 'storage': 5.0, 'database': 0.0,
                        'total': 39.31}},
    {'name': 'broken-app', 'migration_strategy': 'rehost',
     'resource_profile': {'recommended_instance': 't2.micro'},
     'estimated_cost': {'instance': 9.49, 'storage': 0.0, 'database': 0.0,
                        'total': 9.49}},
]


def row_for(text, name):
    return [line for line in text.splitlines() if line.startswith(name + ' ')][0]


def test_text_has_header_rows_and_totals(tmpdir):
    text = report.render(build_state(tmpdir), ASSESSMENT)

    header = text.splitlines()[0].split()
    assert header == ['WORKLOAD', 'STATUS', 'STRATEGY', 'INSTANCE', 'EST', '$/MO',
                      'RESOURCES', 'FILES', 'BYTES', 'ELAPSED']
    portal = row_for(text, 'web-portal').split()
    assert portal == ['web-portal', 'completed', 'containerize', 't2.medium',
                      '39.31', '3', '2', '2048', '0:01:05']
    assert 'broken-app' in text
    assert 'TOTAL 2 workloads (1 completed, 1 rolled_back)' in text
    totals = text.splitlines()[-1]
    assert totals.startswith('TOTAL 2 workloads (1 completed, 1 rolled_back)')
    assert '48.80' in totals
    assert '2048' in totals


def test_elapsed_spans_first_to_last_history_entry(tmpdir):
    text = report.render(build_state(tmpdir), ASSESSMENT)

    assert row_for(text, 'web-portal').split()[-1] == '0:01:05'
    assert row_for(text, 'broken-app').split()[-1] == '1:00:12'


def test_columns_are_fixed_width(tmpdir):
    lines = report.render(build_state(tmpdir), ASSESSMENT).splitlines()

    table = lines[:3]
    start = lines[0].index('STATUS')
    for line in table[1:]:
        assert line[start - 1] == ' '
        assert line[start] != ' '


def test_json_totals(tmpdir):
    data = json.loads(report.render(build_state(tmpdir), ASSESSMENT, fmt='json'))

    totals = data['totals']
    assert totals['bytes'] == 2048
    assert totals['monthly_cost'] == round(39.31 + 9.49, 2)
    assert totals['workloads'] == 2
    assert totals['by_status'] == {'completed': 1, 'rolled_back': 1}
    names = sorted(w['name'] for w in data['workloads'])
    assert names == ['broken-app', 'web-portal']
    portal = [w for w in data['workloads'] if w['name'] == 'web-portal'][0]
    assert portal['elapsed'] == '0:01:05'
    assert portal['resources'] == 3


def test_without_assessment_strategy_and_cost_are_dashes(tmpdir):
    text = report.render(build_state(tmpdir))

    portal = row_for(text, 'web-portal').split()
    assert portal[2] == '-'
    assert portal[4] == '-'
    data = json.loads(report.render(build_state(tmpdir), fmt='json'))
    portal = [w for w in data['workloads'] if w['name'] == 'web-portal'][0]
    assert portal['strategy'] is None
    assert portal['monthly_cost'] is None


def test_workload_without_history_shows_zero_elapsed(tmpdir):
    st = MigrationState(str(tmpdir.join('s.json')))
    st.workload('file-processor')

    row = row_for(report.render(st), 'file-processor').split()
    assert row[1] == 'pending'
    assert row[-1] == '0:00:00'
