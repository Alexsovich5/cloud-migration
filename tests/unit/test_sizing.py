import os
from unittest import mock

import pytest

import sizing

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PRICING_FILE = os.path.join(REPO_ROOT, 'config', 'pricing.yml')

EXPECTED_INSTANCES = ['t2.micro', 't2.small', 't2.medium', 't2.large',
                      'm4.large', 'm4.xlarge', 'm4.2xlarge']
EXPECTED_DB_CLASSES = ['db.t2.micro', 'db.t2.small', 'db.t2.medium',
                       'db.m4.large', 'db.m4.xlarge']


@pytest.fixture
def catalog():
    return sizing.Catalog.from_file(PRICING_FILE)


def test_pricing_file_lists_expected_instances_and_classes(catalog):
    assert catalog.hours_per_month == 730
    assert sorted(catalog.instances) == sorted(EXPECTED_INSTANCES)
    assert sorted(catalog.db_classes) == sorted(EXPECTED_DB_CLASSES)
    for spec in catalog.instances.values():
        assert spec['vcpu'] > 0 and spec['memory_gib'] > 0 and spec['hourly_usd'] > 0


def test_pricing_file_header_says_it_is_a_static_snapshot():
    with open(PRICING_FILE) as f:
        header = f.readline()
    assert header.startswith('#')
    assert 'static hand-maintained snapshot' in header


def test_declared_capacity_used_without_utilization(catalog):
    workload = {'name': 'w', 'cpu': 2, 'memory': 4}
    assert sizing.required_capacity(workload) == (2, 4)
    assert sizing.recommend_instance(workload, catalog) == 't2.medium'


def test_utilization_applies_headroom(catalog):
    workload = {'name': 'w', 'cpu': 2, 'memory': 4,
                'utilization': {'peak_cpu_pct': 40, 'peak_mem_pct': 55}}
    vcpu, mem = sizing.required_capacity(workload)
    assert abs(vcpu - 1.0) < 1e-9
    assert abs(mem - 2.75) < 1e-9
    assert sizing.recommend_instance(workload, catalog) == 't2.medium'


def test_small_workload_gets_micro(catalog):
    assert sizing.recommend_instance({'name': 'w', 'cpu': 1, 'memory': 1}, catalog) == 't2.micro'


def test_cheapest_fitting_type_wins_over_catalog_order():
    catalog = sizing.Catalog({
        'hours_per_month': 730,
        'instances': {
            'big.cheap': {'vcpu': 8, 'memory_gib': 32, 'hourly_usd': 0.01},
            'small.pricey': {'vcpu': 1, 'memory_gib': 1, 'hourly_usd': 0.50},
        },
        'ebs_gp2_gb_month': 0.10,
        'rds': {'classes': {}, 'storage_gb_month': 0.1},
    })
    assert sizing.recommend_instance({'name': 'w', 'cpu': 1, 'memory': 1}, catalog) == 'big.cheap'


def test_oversized_workload_raises(catalog):
    with pytest.raises(sizing.SizingError):
        sizing.recommend_instance({'name': 'w', 'cpu': 16, 'memory': 4}, catalog)


def test_db_class_defaults_and_override(catalog):
    assert sizing.recommend_db_class({'engine': 'mysql'}, catalog) == 'db.m4.large'
    assert sizing.recommend_db_class({'instance_class': 'db.t2.small'}, catalog) == 'db.t2.small'


def test_cost_for_spec_example(catalog):
    workload = {'name': 'web-portal', 'cpu': 2, 'memory': 4, 'storage': 50,
                'database': {'engine': 'mysql', 'storage': 100}}
    assert sizing.recommend_instance(workload, catalog) == 't2.medium'
    assert sizing.estimate_cost(workload, catalog) == {
        'instance': 37.96, 'storage': 5.0, 'database': 139.25, 'total': 182.21}


def test_cost_without_database(catalog):
    cost = sizing.estimate_cost({'name': 'w', 'cpu': 1, 'memory': 1}, catalog)
    assert cost['database'] == 0.0
    assert cost['storage'] == 0.0
    assert cost['total'] == cost['instance'] == 9.49


def test_aws_connector_delegates_instance_type(catalog):
    import aws_connector
    with mock.patch('boto3.session.Session'):
        connector = aws_connector.AWSConnector({}, catalog)
    workload = {'name': 'w', 'cpu': 2, 'memory': 4,
                'utilization': {'peak_cpu_pct': 10, 'peak_mem_pct': 10}}
    assert connector._get_instance_type(workload) == 't2.micro'


def test_engine_delegates_to_sizing(tmpdir):
    import migration_engine
    config = tmpdir.join('migration.yml')
    config.write('aws: {{region: us-east-1}}\n'
                 'pricing_file: {0}\n'
                 'workloads:\n'
                 '  - {{name: tiny, cpu: 1, memory: 1}}\n'
                 '  - {{name: web-portal, cpu: 2, memory: 4, storage: 50,\n'
                 '     database: {{engine: mysql, storage: 100}}}}\n'.format(PRICING_FILE))
    with mock.patch('boto3.session.Session'):
        engine = migration_engine.MigrationEngine(str(config))
    tiny, portal = engine.assess_workloads(str(tmpdir.join('out.json')))
    assert tiny['resource_profile']['recommended_instance'] == 't2.micro'
    assert portal['estimated_cost']['total'] == 182.21
    assert engine.aws.catalog is engine.catalog
