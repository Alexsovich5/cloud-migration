import os

import pytest

import migration_engine

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures('simulators')]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEMO_CONFIG = os.path.join('config', 'demo.yml')
DEMO_WORKLOADS = ('web-portal', 'reporting-engine', 'file-processor')


def test_demo_flow_assess_migrate_report(tmpdir, monkeypatch, capsys):
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv('PORTAL_DB_PASSWORD', 'demo')
    monkeypatch.setenv('REPORTS_DB_PASSWORD', 'demo')
    monkeypatch.setenv('FAKE_DOCKER_LOG', str(tmpdir.join('docker.log')))
    args = ['--config', DEMO_CONFIG,
            '--state', str(tmpdir.join('state', 'demo.json')),
            '--output', str(tmpdir.join('assessment.json'))]

    assert migration_engine.main(args + ['--assess']) == 0
    assert migration_engine.main(args + ['--migrate']) in (0, 1)
    capsys.readouterr()
    assert migration_engine.main(args + ['--report']) == 0

    out = capsys.readouterr()[0]
    rows = [line.split()[0] for line in out.splitlines()[1:]
            if line and not line.startswith(('TOTAL', '-'))]
    assert sorted(rows) == sorted(DEMO_WORKLOADS)
    assert 'TOTAL 3 workloads' in out
