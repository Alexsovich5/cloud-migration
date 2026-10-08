"""
Workload assessment: declared dependencies, strategy, risk, blockers,
resource profile and cost estimate, written out as JSON.
"""

import json
import logging
import os
from datetime import datetime

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


def assess(workloads, catalog, probe_factory=None):
    """Assess each workload and return a list of result dicts."""
    results = []
    for w in workloads:
        found = blockers(w)
        result = {
            'name': w['name'],
            'type': w.get('type', 'unknown'),
            'timestamp': datetime.utcnow().isoformat(),
            'dependencies': discover_declared_dependencies(w),
            'resource_profile': resource_profile(w, catalog),
            'migration_strategy': recommend_strategy(w),
            'risk_score': risk_score(w),
            'estimated_cost': sizing.estimate_cost(w, catalog),
            'ready': not found,
            'blockers': found,
        }
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
