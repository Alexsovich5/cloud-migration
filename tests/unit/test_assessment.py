import json
import os

import pytest

import assessment
import config
import sizing

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PRICING_FILE = os.path.join(REPO_ROOT, 'config', 'pricing.yml')
SAMPLE_CONFIG = os.path.join(REPO_ROOT, 'config', 'migration.yml')


@pytest.fixture
def catalog():
    return sizing.Catalog.from_file(PRICING_FILE)


@pytest.fixture
def sample_workloads():
    return config.load(SAMPLE_CONFIG)['workloads']


def by_name(workloads, name):
    return [w for w in workloads if w['name'] == name][0]


def test_strategy_rules():
    assert assessment.recommend_strategy(
        {'type': 'stateless', 'containerizable': True}) == 'replatform'
    assert assessment.recommend_strategy(
        {'type': 'legacy', 'containerizable': True}) == 'replatform'
    assert assessment.recommend_strategy(
        {'type': 'legacy', 'containerizable': False}) == 'rehost'
    assert assessment.recommend_strategy(
        {'type': 'stateless', 'containerizable': False}) == 'refactor'
    assert assessment.recommend_strategy(
        {'type': 'stateful', 'containerizable': False}) == 'rehost'


def test_risk_score_is_capped_at_100():
    workload = {
        'type': 'legacy',
        'containerizable': False,
        'services': [{'name': 's{0}'.format(i), 'endpoint': 'h:{0}'.format(i)}
                     for i in range(4)],
        'storage': 600,
    }
    assert assessment.risk_score(workload) == 100


def test_risk_score_of_web_portal_sample(sample_workloads):
    assert assessment.risk_score(by_name(sample_workloads, 'web-portal')) == 20


def test_risk_score_of_reporting_engine_sample(sample_workloads):
    # baseline 20 + legacy 30 + not containerizable 15
    assert assessment.risk_score(by_name(sample_workloads, 'reporting-engine')) == 65


def test_blockers():
    assert assessment.blockers({}) == []
    assert assessment.blockers({'licensed_software': True,
                                'compliance_requirements': True}) == [
        'Licensed software requires vendor approval',
        'Compliance review required before migration',
    ]


def test_licensed_software_makes_workload_not_ready(catalog):
    workload = {'name': 'erp', 'type': 'legacy', 'runtime': 'java', 'cpu': 2, 'memory': 4,
                'storage': 0, 'licensed_software': True}
    result = assessment.assess([workload], catalog)[0]
    assert result['ready'] is False
    assert result['blockers'] == ['Licensed software requires vendor approval']


def test_service_endpoint_scheme_is_stripped():
    deps = assessment.discover_declared_dependencies({
        'services': [{'name': 'cache', 'endpoint': 'redis://cache.internal:6379'},
                     {'name': 'auth', 'endpoint': 'http://auth.internal:5000/'},
                     {'name': 'plain', 'endpoint': 'queue.internal:5672'}],
    })
    assert [d['endpoint'] for d in deps] == [
        'cache.internal:6379', 'auth.internal:5000', 'queue.internal:5672']
    assert all(d['type'] == 'service' and d['source'] == 'config' for d in deps)


def test_database_dependency_is_declared():
    deps = assessment.discover_declared_dependencies({
        'database': {'engine': 'mysql', 'host': 'db.internal', 'port': 3306}})
    assert deps == [{'type': 'database', 'engine': 'mysql', 'host': 'db.internal',
                     'port': 3306, 'source': 'config'}]


def test_assess_web_portal_sample(sample_workloads, catalog):
    results = assessment.assess(sample_workloads, catalog)
    assert [r['name'] for r in results] == ['web-portal', 'reporting-engine', 'file-processor']
    portal = results[0]
    assert portal['type'] == 'stateless'
    assert portal['migration_strategy'] == 'replatform'
    assert portal['risk_score'] == 20
    assert portal['ready'] is True
    assert portal['blockers'] == []
    assert portal['resource_profile'] == {
        'cpu_cores': 2, 'memory_gb': 4, 'storage_gb': 50,
        'recommended_instance': sizing.recommend_instance(sample_workloads[0], catalog)}
    assert portal['estimated_cost'] == sizing.estimate_cost(sample_workloads[0], catalog)
    assert {'type': 'service', 'name': 'cache', 'endpoint': 'cache.internal:6379',
            'source': 'config'} in portal['dependencies']
    assert 'T' in portal['timestamp']


def test_write_results_writes_valid_json(tmpdir, sample_workloads, catalog):
    results = assessment.assess(sample_workloads, catalog)
    path = str(tmpdir.join('out', 'assessment.json'))
    assessment.write_results(results, path)
    with open(path) as f:
        loaded = json.load(f)
    assert loaded == results


def test_engine_writes_assessment_to_given_path(tmpdir, monkeypatch):
    from unittest import mock
    import migration_engine
    monkeypatch.chdir(REPO_ROOT)
    # The sample config leaves aws.artifacts_bucket empty for --tfstate to fill.
    tfstate = os.path.join(REPO_ROOT, 'tests', 'fixtures', 'terraform.tfstate')
    with mock.patch('boto3.session.Session'):
        engine = migration_engine.MigrationEngine(
            SAMPLE_CONFIG, str(tmpdir.join('state.json')), tfstate_path=tfstate)
    assert engine.data_migrator.bucket == 'migration-artifacts'
    path = str(tmpdir.join('results.json'))
    results = engine.assess_workloads(path)
    with open(path) as f:
        assert json.load(f) == results
    assert not os.path.exists(os.path.join(str(tmpdir), 'assessment_results.json'))


def test_engine_raises_config_error_instead_of_exiting(tmpdir):
    import migration_engine
    with pytest.raises(config.ConfigError):
        migration_engine.MigrationEngine(str(tmpdir.join('missing.yml')))
