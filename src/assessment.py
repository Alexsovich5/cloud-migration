"""
Workload assessment: declared and observed dependencies, strategy, risk,
blockers, resource profile and cost estimate, written out as JSON.
"""

import json
import logging
import os
import socket
from datetime import datetime

import paramiko

import discovery
import sizing

logger = logging.getLogger('assessment')


def normalise_endpoint(endpoint):
    """Reduce 'scheme://host:port/path' to 'host:port'."""
    endpoint = (endpoint or '').strip()
    if '://' in endpoint:
        endpoint = endpoint.split('://', 1)[1]
    return endpoint.split('/', 1)[0]


def discover_declared_dependencies(w):
    """List the database and service dependencies declared in config."""
    deps = []
    if 'database' in w:
        db = w['database']
        deps.append({
            'type': 'database',
            'engine': db.get('engine', 'unknown'),
            'host': db.get('host', ''),
            'port': db.get('port', 0),
            'source': 'config',
        })
    for svc in w.get('services') or []:
        deps.append({
            'type': 'service',
            'name': svc.get('name', ''),
            'endpoint': normalise_endpoint(svc.get('endpoint', '')),
            'source': 'config',
        })
    return deps


def recommend_strategy(w):
    """Pick rehost, replatform or refactor."""
    if w.get('containerizable', False):
        return 'replatform'
    wtype = w.get('type', '')
    if wtype == 'legacy':
        return 'rehost'
    if wtype == 'stateless':
        return 'refactor'
    return 'rehost'


def risk_score(w):
    """Migration risk from 0 to 100."""
    risk = 20
    if w.get('type') == 'legacy':
        risk += 30
    if len(w.get('services') or []) > 3:
        risk += 20
    if w.get('storage', 0) > 500:
        risk += 15
    if not w.get('containerizable', False):
        risk += 15
    return min(risk, 100)


def blockers(w):
    """Reasons the workload cannot be migrated yet."""
    found = []
    if w.get('licensed_software'):
        found.append("Licensed software requires vendor approval")
    if w.get('compliance_requirements'):
        found.append("Compliance review required before migration")
    return found


def resource_profile(w, catalog):
    return {
        'cpu_cores': w['cpu'],
        'memory_gb': w['memory'],
        'storage_gb': w.get('storage', 0),
        'recommended_instance': sizing.recommend_instance(w, catalog),
    }


DISCOVERY_ERRORS = (paramiko.SSHException, discovery.DiscoveryError,
                    socket.error, OSError, EOFError)


def observe_dependencies(w, declared, probe_factory):
    """Probe ``w['source']`` and merge what it reports into ``declared``.

    Returns ``(dependencies, listening_ports, error)``. The SSH port the probe
    connected through is left out of ``listening_ports``. On an SSH or
    discovery failure the declared list is returned unchanged with the error
    text, so one unreachable host does not stop the assessment.
    """
    source = w.get('source')
    if not source or probe_factory is None:
        return declared, [], None
    try:
        observed = probe_factory(source).collect()
    except DISCOVERY_ERRORS as e:
        message = '{0}: {1}'.format(type(e).__name__, e)
        logger.warning("Discovery failed for %s: %s", w['name'], message)
        return declared, [], message
    merged = discovery.merge_dependencies(declared, observed,
                                          source.get('known_endpoints') or {})
    ssh_port = source.get('port', 22)
    listening = [p for p in observed['listening'] if p != ssh_port]
    return merged, listening, None


def assess(workloads, catalog, probe_factory=None):
    """Assess each workload and return a list of result dicts.

    ``probe_factory(source)`` must return an object whose ``collect()``
    yields ``{'listening': [...], 'established': [...]}``; it is only called
    for workloads with a ``source`` block.
    """
    results = []
    for w in workloads:
        found = blockers(w)
        deps, listening, error = observe_dependencies(
            w, discover_declared_dependencies(w), probe_factory)
        result = {
            'name': w['name'],
            'type': w.get('type', 'unknown'),
            'timestamp': datetime.utcnow().isoformat(),
            'dependencies': deps,
            'listening_ports': listening,
            'resource_profile': resource_profile(w, catalog),
            'migration_strategy': recommend_strategy(w),
            'risk_score': risk_score(w),
            'estimated_cost': sizing.estimate_cost(w, catalog),
            'ready': not found,
            'blockers': found,
        }
        if error is not None:
            result['discovery_error'] = error
        results.append(result)
        logger.info("Assessed workload: %s (ready=%s)", w['name'], result['ready'])
    return results


def write_results(results, path):
    """Write assessment results to ``path`` as indented JSON."""
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with open(path, 'w') as f:
        json.dump(results, f, indent=2, sort_keys=True)
    logger.info("Assessment saved to %s", path)
    return path
