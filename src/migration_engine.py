#!/usr/bin/env python3
"""
Cloud Migration Engine

Core migration orchestration module for assessing, planning, and executing
workload migrations from on-premises infrastructure to AWS.
"""

import logging
import argparse
import os
import sys

import assessment
import config
import rollback
import sizing
import state as state_mod
from aws_connector import AWSConnector
from data_migration import DataMigrator, artifacts_bucket
from docker_builder import DockerBuilder
from validation import Validator

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('migration_engine')

DEFAULT_STATE_PATH = 'state/migration_state.json'
# Statuses a crashed run can leave behind; rollback first marks them failed.
IN_PROGRESS = (state_mod.PROVISIONING, state_mod.PROVISIONED,
               state_mod.CONTAINERISED, state_mod.DATA_MIGRATED,
               state_mod.VALIDATED)


class ValidationFailed(Exception):
    """Raised when post-migration validation reports failures."""


class MigrationEngine:
    """Drives each workload through the migration state machine."""

    def __init__(self, config_path='config/migration.yml', state_path=DEFAULT_STATE_PATH,
                 tfstate_path=None, session=None, runner=None, aws=None, docker=None,
                 data_migrator=None, validator=None):
        self.config = self._load_config(config_path)
        self.state_path = state_path
        self.tfstate_path = tfstate_path
        self.catalog = sizing.Catalog.from_file(self.config['pricing_file'])
        aws_cfg = self.config.get('aws', {})
        self.aws = aws if aws is not None else AWSConnector(aws_cfg, self.catalog,
                                                            session=session)
        self.docker = docker if docker is not None else DockerBuilder(
            self.config.get('docker', {}), runner=runner)
        self.data_migrator = data_migrator if data_migrator is not None else DataMigrator(
            self.aws.s3, artifacts_bucket(aws_cfg))
        self.validator = validator if validator is not None else Validator(
            self.aws, self.data_migrator)
        self.manifest_dir = os.path.dirname(os.path.abspath(state_path))

    def _load_config(self, path):
        """Load and validate migration configuration (raises ConfigError)."""
        cfg = config.load(path)
        logger.info("Configuration loaded from %s", path)
        return cfg

    def _load_state(self):
        return state_mod.MigrationState.load(self.state_path)

    def assess_workloads(self, output_path='assessment_results.json'):
        """Assess workloads, write the results and mark pending ones assessed."""
        logger.info("Starting workload assessment...")
        results = assessment.assess(self.config['workloads'], self.catalog)
        assessment.write_results(results, output_path)

        st = self._load_state()
        for w in self.config['workloads']:
            if st.status(w['name']) == state_mod.PENDING:
                st.transition(w['name'], state_mod.ASSESSED)
        st.save()
        return results

    def execute_migration(self, only=None):
        """Migrate every workload (or only the one named); return True if all succeeded."""
        logger.info("Starting migration execution...")
        st = self._load_state()
        ok = True
        for workload in self.config.get('workloads', []):
            name = workload['name']
            if only is not None and name != only:
                continue
            status = st.status(name)
            if status == state_mod.COMPLETED:
                sys.stderr.write('{0}: already completed, use --rollback first\n'.format(name))
                ok = False
                continue
            if status not in state_mod.TRANSITIONS or \
                    state_mod.PROVISIONING not in state_mod.TRANSITIONS[status]:
                sys.stderr.write('{0}: status {1}, use --rollback first\n'.format(name, status))
                ok = False
                continue
            try:
                self._migrate_workload(workload, st)
            except Exception as e:
                ok = False
                logger.error("Migration failed for %s: %s", name, e)
                self._fail_and_rollback(name, st, str(e))
        return ok

    def _step(self, st, name, status):
        st.transition(name, status)
        st.save()

    def _migrate_workload(self, workload, st):
        name = workload['name']
        logger.info("Migrating %s using %s strategy", name,
                    assessment.recommend_strategy(workload))

        self._step(st, name, state_mod.PROVISIONING)
        self.aws.provision(workload, st)
        if workload.get('database'):
            self.aws.create_database(workload, st)
            st.save()
        self._step(st, name, state_mod.PROVISIONED)

        if workload.get('containerizable', False):
            self.docker.build_image(workload)
            st.set(name, 'image', self.docker.push(workload))
            self._step(st, name, state_mod.CONTAINERISED)

        if workload.get('data_path'):
            self.data_migrator.migrate(workload, st, manifest_dir=self.manifest_dir)
            self._step(st, name, state_mod.DATA_MIGRATED)

        valid, failures = self.validator.validate(workload, st)
        if not valid:
            raise ValidationFailed('validation failed: {0}'.format('; '.join(failures)))
        self._step(st, name, state_mod.VALIDATED)
        self._step(st, name, state_mod.COMPLETED)
        logger.info("Migration completed for %s", name)

    def _fail_and_rollback(self, name, st, error):
        if st.status(name) in IN_PROGRESS:
            st.transition(name, state_mod.FAILED, error=error)
            st.save()
        if st.status(name) != state_mod.FAILED:
            return []
        logger.warning("Rolling back migration for %s", name)
        try:
            return rollback.run(name, st, self.aws, self.aws.s3)
        except Exception as e:
            logger.error("Rollback incomplete for %s: %s", name, e)
            return []

    def rollback_workload(self, name):
        """Roll back one workload, recovering it first if a run was interrupted."""
        st = self._load_state()
        if st.status(name) in IN_PROGRESS:
            st.transition(name, state_mod.FAILED, error='interrupted run')
            st.save()
        return rollback.run(name, st, self.aws, self.aws.s3)


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
