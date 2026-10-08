"""
Instance catalog, right-sizing and monthly cost estimation.

Prices come from a static YAML catalog (config/pricing.yml).
"""

import yaml

HEADROOM = 1.25
DEFAULT_DB_CLASS = 'db.m4.large'
DEFAULT_DB_STORAGE = 100


class SizingError(Exception):
    """Raised when no catalog entry can satisfy a workload."""


class Catalog(object):
    """Instance types, RDS classes and storage prices."""

    def __init__(self, data):
        self.hours_per_month = data.get('hours_per_month', 730)
        self.instances = dict(data.get('instances') or {})
        self.ebs_gp2_gb_month = float(data.get('ebs_gp2_gb_month', 0.0))
        rds = data.get('rds') or {}
        self.db_classes = dict(rds.get('classes') or {})
        self.rds_storage_gb_month = float(rds.get('storage_gb_month', 0.0))

    @classmethod
    def from_file(cls, path):
        with open(path, 'r') as f:
            return cls(yaml.safe_load(f) or {})

    def instance_hourly(self, name):
        try:
            return float(self.instances[name]['hourly_usd'])
        except KeyError:
            raise SizingError("Unknown instance type: {0}".format(name))

    def db_hourly(self, name):
        try:
            return float(self.db_classes[name])
        except KeyError:
            raise SizingError("Unknown RDS class: {0}".format(name))


def required_capacity(workload):
    """Return (vcpu, memory_gib) needed, with headroom over peak utilization."""
    cpu = workload['cpu']
    memory = workload['memory']
    utilization = workload.get('utilization')
    if not utilization:
        return cpu, memory
    cpu_pct = utilization.get('peak_cpu_pct', 100)
    mem_pct = utilization.get('peak_mem_pct', 100)
    return (cpu * cpu_pct / 100.0 * HEADROOM,
            memory * mem_pct / 100.0 * HEADROOM)


def recommend_instance(workload, catalog):
    """Pick the cheapest catalog instance type that fits vCPU and memory."""
    vcpu, memory = required_capacity(workload)
    fitting = [
        (float(spec['hourly_usd']), name)
        for name, spec in catalog.instances.items()
        if spec['vcpu'] >= vcpu and spec['memory_gib'] >= memory
    ]
    if not fitting:
        raise SizingError(
            "No instance type fits {0} (needs {1} vCPU, {2} GiB)".format(
                workload.get('name', '?'), vcpu, memory))
    return min(fitting)[1]


def recommend_db_class(db, catalog):
    """Use the configured RDS class, or the default class."""
    return db.get('instance_class') or DEFAULT_DB_CLASS


def estimate_cost(workload, catalog):
    """Estimate monthly USD cost of instance, EBS storage and database."""
    hours = catalog.hours_per_month
    instance = catalog.instance_hourly(recommend_instance(workload, catalog)) * hours
    storage = workload.get('storage', 0) * catalog.ebs_gp2_gb_month
    database = 0.0
    db = workload.get('database')
    if db:
        db_class = recommend_db_class(db, catalog)
        database = (catalog.db_hourly(db_class) * hours +
                    db.get('storage', DEFAULT_DB_STORAGE) * catalog.rds_storage_gb_month)
    return {
        'instance': round(instance, 2),
        'storage': round(storage, 2),
        'database': round(database, 2),
        'total': round(instance + storage + database, 2),
    }
