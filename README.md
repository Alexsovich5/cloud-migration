# Cloud Migration Framework

Enterprise cloud migration framework for systematically moving on-premises workloads to AWS with automated assessment, migration planning, and Docker containerization.

Personal project, built to explore automating assessment and containerisation of on-prem workloads. It is not production software — see **Status** below for exactly what is and isn't implemented.

## Status

**Implemented**

- Migration engine orchestrating assessment and planning
- AWS connector
- Docker builder that containerises a discovered workload
- Terraform for target infrastructure

**Not implemented / known limitations**

- Assessment is inventory-only — no dependency mapping or cost modelling
- No rollback path
- Pinned to boto3 1.1.4 (2015-era); needs updating to run
- No tests

## Built with

- **Python** — boto3, botocore, PyYAML, requests, paramiko

## Running it

```bash
pip install -r requirements.txt
python src/migration_engine.py
```

## Layout

```
Dockerfile
config/
  migration.yml
requirements.txt
src/
  aws_connector.py
  docker_builder.py
  migration_engine.py
terraform/
  main.tf
  variables.tf
```

