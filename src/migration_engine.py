#!/usr/bin/env python3
"""
Cloud Migration Engine

Core migration orchestration module for assessing, planning, and executing
workload migrations from on-premises infrastructure to AWS.
"""

import logging
import argparse
from datetime import datetime

import assessment
import config
import sizing
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
        self.catalog = sizing.Catalog.from_file(
            self.config['pricing_file'])
        self.aws = AWSConnector(self.config.get('aws', {}), self.catalog)
        self.docker = DockerBuilder(self.config.get('docker', {}))
        self.migration_log = []

    def _load_config(self, path):
        """Load and validate migration configuration (raises ConfigError)."""
        cfg = config.load(path)
        logger.info("Configuration loaded from %s", path)
        return cfg

    def assess_workloads(self, output_path='assessment_results.json'):
        """Assess workloads for migration readiness and write the results."""
        logger.info("Starting workload assessment...")
        results = assessment.assess(self.config['workloads'], self.catalog)
        assessment.write_results(results, output_path)
        return results

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
        strategy = assessment.recommend_strategy(workload)
        logger.info("Migrating %s using %s strategy", name, strategy)

        # Provision infrastructure
        self.aws.provision_infrastructure(workload)

        # Containerize if applicable
        if workload.get('containerizable', False):
            self.docker.build_image(workload)
            self.docker.push(workload)

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
        print("\nAssessment complete: {0}/{1} workloads ready".format(ready, len(results)))
    elif args.migrate:
        engine.execute_migration()
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
