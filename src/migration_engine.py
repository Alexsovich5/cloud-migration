#!/usr/bin/env python3
"""
Cloud Migration Engine

Core migration orchestration module for assessing, planning, and executing
workload migrations from on-premises infrastructure to AWS.
"""

import os
import sys
import json
import yaml
import logging
import argparse
from datetime import datetime

from aws_connector import AWSConnector
from docker_builder import DockerBuilder

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('migration_engine')


class MigrationEngine:
    """Orchestrates the end-to-end cloud migration process."""

    def __init__(self, config_path='config/migration.yml'):
        self.config = self._load_config(config_path)
        self.aws = AWSConnector(self.config.get('aws', {}))
        self.docker = DockerBuilder(self.config.get('docker', {}))
        self.migration_log = []

    def _load_config(self, path):
        """Load migration configuration from YAML file."""
        try:
            with open(path, 'r') as f:
                config = yaml.safe_load(f)
            logger.info("Configuration loaded from %s", path)
            return config
        except FileNotFoundError:
            logger.error("Configuration file not found: %s", path)
            sys.exit(1)

    def assess_workloads(self):
        """Assess on-premises workloads for migration readiness."""
        logger.info("Starting workload assessment...")
        workloads = self.config.get('workloads', [])
        assessment_results = []

        for workload in workloads:
            result = {
                'name': workload['name'],
                'type': workload.get('type', 'unknown'),
                'timestamp': datetime.utcnow().isoformat(),
                'dependencies': self._discover_dependencies(workload),
                'resource_profile': self._profile_resources(workload),
                'migration_strategy': self._recommend_strategy(workload),
                'estimated_cost': self._estimate_cost(workload),
                'risk_score': self._calculate_risk(workload),
                'ready': True
            }

            # Check for blockers
            blockers = self._check_blockers(workload)
            if blockers:
                result['ready'] = False
                result['blockers'] = blockers

            assessment_results.append(result)
            logger.info("Assessed workload: %s (ready=%s)",
                       workload['name'], result['ready'])

        self._save_assessment(assessment_results)
        return assessment_results

    def _discover_dependencies(self, workload):
        """Map workload dependencies including network, storage, and services."""
        deps = []
        if 'database' in workload:
            deps.append({
                'type': 'database',
                'engine': workload['database'].get('engine', 'unknown'),
                'host': workload['database'].get('host', ''),
                'port': workload['database'].get('port', 0)
            })
        if 'services' in workload:
            for svc in workload['services']:
                deps.append({
                    'type': 'service',
                    'name': svc.get('name', ''),
                    'endpoint': svc.get('endpoint', '')
                })
        return deps

    def _profile_resources(self, workload):
        """Profile resource utilization for right-sizing."""
        return {
            'cpu_cores': workload.get('cpu', 2),
            'memory_gb': workload.get('memory', 4),
            'storage_gb': workload.get('storage', 50),
            'iops': workload.get('iops', 1000),
            'network_mbps': workload.get('network', 100),
            'recommended_instance': self._recommend_instance_type(workload)
        }

    def _recommend_instance_type(self, workload):
        """Recommend AWS instance type based on resource profile."""
        cpu = workload.get('cpu', 2)
        memory = workload.get('memory', 4)

        if cpu <= 1 and memory <= 2:
            return 't2.small'
        elif cpu <= 2 and memory <= 4:
            return 't2.medium'
        elif cpu <= 4 and memory <= 8:
            return 'm4.large'
        elif cpu <= 8 and memory <= 16:
            return 'm4.xlarge'
        else:
            return 'm4.2xlarge'

    def _recommend_strategy(self, workload):
        """Determine migration strategy (6 Rs)."""
        wtype = workload.get('type', '')
        containerizable = workload.get('containerizable', False)

        if containerizable:
            return 'replatform'
        elif wtype == 'legacy':
            return 'rehost'
        elif wtype == 'stateless':
            return 'refactor'
        else:
            return 'rehost'

    def _estimate_cost(self, workload):
        """Estimate monthly AWS cost for the workload."""
        instance_costs = {
            't2.small': 16.79,
            't2.medium': 33.58,
            'm4.large': 73.00,
            'm4.xlarge': 146.00,
            'm4.2xlarge': 292.00
        }
        instance = self._recommend_instance_type(workload)
        base_cost = instance_costs.get(instance, 100.00)
        storage_cost = workload.get('storage', 50) * 0.10
        return round(base_cost + storage_cost, 2)

    def _calculate_risk(self, workload):
        """Calculate migration risk score (0-100)."""
        risk = 20  # baseline
        if workload.get('type') == 'legacy':
            risk += 30
        if len(workload.get('services', [])) > 3:
            risk += 20
        if workload.get('storage', 0) > 500:
            risk += 15
        if not workload.get('containerizable', False):
            risk += 15
        return min(risk, 100)

    def _check_blockers(self, workload):
        """Identify migration blockers."""
        blockers = []
        if workload.get('licensed_software'):
            blockers.append("Licensed software requires vendor approval")
        if workload.get('compliance_requirements'):
            blockers.append("Compliance review required before migration")
        return blockers

    def _save_assessment(self, results):
        """Save assessment results to JSON file."""
        output_path = 'assessment_results.json'
        with open(output_path, 'w') as f:
            json.dump(results, f, indent=2)
        logger.info("Assessment saved to %s", output_path)

    def execute_migration(self):
        """Execute the migration plan."""
        logger.info("Starting migration execution...")
        workloads = self.config.get('workloads', [])

        for workload in workloads:
            try:
                self._migrate_workload(workload)
            except Exception as e:
                logger.error("Migration failed for %s: %s",
                           workload['name'], str(e))
                self._rollback(workload)

    def _migrate_workload(self, workload):
        """Migrate a single workload to AWS."""
        name = workload['name']
        strategy = self._recommend_strategy(workload)
        logger.info("Migrating %s using %s strategy", name, strategy)

        # Provision infrastructure
        self.aws.provision_infrastructure(workload)

        # Containerize if applicable
        if workload.get('containerizable', False):
            self.docker.build_image(workload)
            self.docker.push_to_ecr(workload, self.aws)

        # Migrate data
        if 'database' in workload:
            self.aws.migrate_database(workload['database'])

        # Validate
        if self._validate_migration(workload):
            logger.info("Migration validated for %s", name)
            self.migration_log.append({
                'workload': name,
                'status': 'success',
                'timestamp': datetime.utcnow().isoformat()
            })
        else:
            raise Exception("Validation failed")

    def _validate_migration(self, workload):
        """Validate migrated workload health."""
        checks = [
            self.aws.check_instance_health(workload),
            self.aws.check_connectivity(workload),
            self.aws.check_data_integrity(workload)
        ]
        return all(checks)

    def _rollback(self, workload):
        """Rollback a failed migration."""
        logger.warning("Rolling back migration for %s", workload['name'])
        self.aws.destroy_infrastructure(workload)
        self.migration_log.append({
            'workload': workload['name'],
            'status': 'rolled_back',
            'timestamp': datetime.utcnow().isoformat()
        })


def main():
    parser = argparse.ArgumentParser(description='Cloud Migration Framework')
    parser.add_argument('--config', default='config/migration.yml',
                       help='Path to migration config')
    parser.add_argument('--assess', action='store_true',
                       help='Run workload assessment')
    parser.add_argument('--migrate', action='store_true',
                       help='Execute migration plan')
    args = parser.parse_args()

    engine = MigrationEngine(args.config)

    if args.assess:
        results = engine.assess_workloads()
        ready = sum(1 for r in results if r['ready'])
        print(f"\nAssessment complete: {ready}/{len(results)} workloads ready")
    elif args.migrate:
        engine.execute_migration()
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
