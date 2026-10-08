import copy
import os

import pytest

import config

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURES = os.path.join(REPO_ROOT, 'tests', 'fixtures', 'config')


def fixture(name):
    return os.path.join(FIXTURES, name)


@pytest.fixture
def valid_cfg():
    return config.load(fixture('valid.yml'))


def test_valid_config_loads_with_defaults(valid_cfg):
    assert valid_cfg['aws']['region'] == 'us-east-1'
    assert valid_cfg['aws']['allowed_cidr'] == '10.0.0.0/8'
    assert valid_cfg['aws']['endpoints'] == {}
    assert valid_cfg['aws']['default_ami'] == 'ami-1a2b3c4d'
    assert valid_cfg['docker']['command'] == 'docker'
    assert valid_cfg['docker']['registry'] == 'registry.local:5000'
    assert valid_cfg['pricing_file'] == 'config/pricing.yml'

    portal, reporting = valid_cfg['workloads']
    assert portal['ports'] == [80, 443]
    assert portal['containerizable'] is True
    assert portal['storage'] == 0
    assert portal['version'] == 'latest'
    assert reporting['storage'] == 0
    assert reporting['ports'] == []
    assert reporting['containerizable'] is False
    assert reporting['version'] == 'latest'


def test_sample_config_loads():
    cfg = config.load(os.path.join(REPO_ROOT, 'config', 'migration.yml'))
    assert [w['name'] for w in cfg['workloads']] == [
        'web-portal', 'reporting-engine', 'file-processor']


def test_bad_cpu_names_the_offending_path():
    with pytest.raises(config.ConfigError) as exc:
        config.load(fixture('bad_cpu.yml'))
    assert 'workloads[0].cpu' in str(exc.value)


def test_duplicate_names_raise():
    with pytest.raises(config.ConfigError) as exc:
        config.load(fixture('dup_names.yml'))
    assert 'workloads[1].name' in str(exc.value)
    assert 'duplicate' in str(exc.value)


def test_missing_file_raises_config_error(tmpdir):
    with pytest.raises(config.ConfigError):
        config.load(str(tmpdir.join('missing.yml')))


def _with_workload_change(cfg, key, value):
    cfg = copy.deepcopy(cfg)
    cfg['workloads'][0][key] = value
    return cfg


@pytest.mark.parametrize('key,value,path', [
    ('ports', [80, 70000], 'workloads[0].ports[1]'),
    ('ports', 80, 'workloads[0].ports'),
    ('name', 'Web_Portal', 'workloads[0].name'),
    ('cpu', 0, 'workloads[0].cpu'),
    ('cpu', 2.5, 'workloads[0].cpu'),
    ('cpu', True, 'workloads[0].cpu'),
    ('memory', 0, 'workloads[0].memory'),
    ('memory', '4', 'workloads[0].memory'),
    ('runtime', 'ruby', 'workloads[0].runtime'),
    ('type', 'batch', 'workloads[0].type'),
    ('database', {'engine': 'oracle'}, 'workloads[0].database.engine'),
    ('utilization', {'peak_cpu_pct': 0}, 'workloads[0].utilization.peak_cpu_pct'),
    ('utilization', {'peak_mem_pct': 101}, 'workloads[0].utilization.peak_mem_pct'),
])
def test_invalid_workload_fields_raise(valid_cfg, key, value, path):
    with pytest.raises(config.ConfigError) as exc:
        config.validate(_with_workload_change(valid_cfg, key, value))
    assert path in str(exc.value)


def test_missing_required_cpu_raises(valid_cfg):
    cfg = copy.deepcopy(valid_cfg)
    del cfg['workloads'][1]['cpu']
    with pytest.raises(config.ConfigError) as exc:
        config.validate(cfg)
    assert 'workloads[1].cpu' in str(exc.value)


def test_invalid_port_in_file_raises(tmpdir):
    path = tmpdir.join('bad_port.yml')
    path.write('workloads:\n'
               '  - {name: api, type: stateless, runtime: node, cpu: 1, memory: 1,'
               ' ports: [70000]}\n')
    with pytest.raises(config.ConfigError) as exc:
        config.load(str(path))
    assert 'workloads[0].ports[0]' in str(exc.value)


def test_valid_config_passes_validation(valid_cfg):
    assert config.validate(valid_cfg) is None
