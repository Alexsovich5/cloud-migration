"""
Loading, defaults and validation for migration.yml.

Errors name the offending path, for example ``workloads[1].cpu: expected int``.
"""

import numbers
import re

import yaml

NAME_RE = re.compile(r'^[a-z0-9-]+$')
RUNTIMES = ('python', 'node', 'java', 'php')
TYPES = ('stateless', 'stateful', 'legacy')
DB_ENGINES = ('mysql', 'postgres')
UTILIZATION_KEYS = ('peak_cpu_pct', 'peak_mem_pct')

AWS_DEFAULTS = {
    'region': 'us-east-1',
    'allowed_cidr': '10.0.0.0/8',
}
DOCKER_DEFAULTS = {
    'command': 'docker',
}
WORKLOAD_DEFAULTS = {
    'storage': 0,
    'containerizable': False,
    'version': 'latest',
}


class ConfigError(Exception):
    """Raised when migration.yml is missing, unreadable or invalid."""


def load(path):
    """Read a YAML config file, apply defaults, validate it and return it."""
    try:
        with open(path, 'r') as f:
            raw = yaml.safe_load(f)
    except (IOError, OSError) as e:
        raise ConfigError("{0}: cannot read config: {1}".format(path, e))
    except yaml.YAMLError as e:
        raise ConfigError("{0}: invalid YAML: {1}".format(path, e))
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("<root>: expected mapping")
    cfg = apply_defaults(raw)
    validate(cfg)
    return cfg


def apply_defaults(raw):
    """Return a copy of ``raw`` with missing optional settings filled in."""
    cfg = dict(raw)
    aws = dict(cfg.get('aws') or {})
    for key, value in AWS_DEFAULTS.items():
        aws.setdefault(key, value)
    aws['endpoints'] = dict(aws.get('endpoints') or {})
    cfg['aws'] = aws

    docker = dict(cfg.get('docker') or {})
    for key, value in DOCKER_DEFAULTS.items():
        docker.setdefault(key, value)
    cfg['docker'] = docker

    cfg.setdefault('pricing_file', 'config/pricing.yml')

    workloads = cfg.get('workloads')
    if workloads is None:
        workloads = []
    if isinstance(workloads, list):
        filled = []
        for w in workloads:
            if isinstance(w, dict):
                w = dict(w)
                for key, value in WORKLOAD_DEFAULTS.items():
                    w.setdefault(key, value)
                w.setdefault('ports', [])
            filled.append(w)
        workloads = filled
    cfg['workloads'] = workloads
    return cfg


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value):
    return isinstance(value, numbers.Real) and not isinstance(value, bool)


def _fail(path, message):
    raise ConfigError("{0}: {1}".format(path, message))


def validate(cfg):
    """Raise ConfigError for the first invalid setting found in ``cfg``."""
    if not isinstance(cfg, dict):
        _fail('<root>', 'expected mapping')
    for section in ('aws', 'docker'):
        if section in cfg and not isinstance(cfg[section], dict):
            _fail(section, 'expected mapping')
    workloads = cfg.get('workloads', [])
    if not isinstance(workloads, list):
        _fail('workloads', 'expected list')
    seen = set()
    for index, workload in enumerate(workloads):
        prefix = 'workloads[{0}]'.format(index)
        _validate_workload(prefix, workload)
        if workload['name'] in seen:
            _fail(prefix + '.name', "duplicate name '{0}'".format(workload['name']))
        seen.add(workload['name'])


def _validate_workload(prefix, w):
    if not isinstance(w, dict):
        _fail(prefix, 'expected mapping')

    name = w.get('name')
    if not isinstance(name, str) or not NAME_RE.match(name):
        _fail(prefix + '.name', 'expected [a-z0-9-]+')

    if 'cpu' not in w:
        _fail(prefix + '.cpu', 'required')
    if not _is_int(w['cpu']):
        _fail(prefix + '.cpu', 'expected int')
    if w['cpu'] < 1:
        _fail(prefix + '.cpu', 'expected int >= 1')

    if 'memory' not in w:
        _fail(prefix + '.memory', 'required')
    if not _is_number(w['memory']) or w['memory'] <= 0:
        _fail(prefix + '.memory', 'expected number > 0')

    if 'runtime' in w and w['runtime'] not in RUNTIMES:
        _fail(prefix + '.runtime', 'expected one of {0}'.format(', '.join(RUNTIMES)))
    if 'type' in w and w['type'] not in TYPES:
        _fail(prefix + '.type', 'expected one of {0}'.format(', '.join(TYPES)))

    if not _is_number(w.get('storage', 0)) or w.get('storage', 0) < 0:
        _fail(prefix + '.storage', 'expected number >= 0')

    ports = w.get('ports', [])
    if not isinstance(ports, list):
        _fail(prefix + '.ports', 'expected list of ints')
    for i, port in enumerate(ports):
        if not _is_int(port) or not 1 <= port <= 65535:
            _fail('{0}.ports[{1}]'.format(prefix, i), 'expected int between 1 and 65535')

    if 'database' in w:
        db = w['database']
        if not isinstance(db, dict):
            _fail(prefix + '.database', 'expected mapping')
        if db.get('engine') not in DB_ENGINES:
            _fail(prefix + '.database.engine',
                  'expected one of {0}'.format(', '.join(DB_ENGINES)))

    if 'utilization' in w:
        util = w['utilization']
        if not isinstance(util, dict):
            _fail(prefix + '.utilization', 'expected mapping')
        for key in UTILIZATION_KEYS:
            if key in util:
                value = util[key]
                if not _is_number(value) or not 1 <= value <= 100:
                    _fail('{0}.utilization.{1}'.format(prefix, key),
                          'expected percentage between 1 and 100')
