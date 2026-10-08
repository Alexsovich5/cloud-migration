# Cloud Migration Framework

A command-line tool that moves a list of on-premises workloads, described in one YAML file, to AWS. For each workload it maps dependencies over SSH, picks a migration strategy (rehost, replatform or refactor), scores risk, right-sizes an EC2 instance type from a static price catalog and estimates monthly cost. It then provisions a security group, instance, EBS volume and optional RDS database with boto3, builds and pushes a Docker image, copies the workload's data to S3 with MD5 checks, validates the result, and rolls back every created resource in reverse order if any step fails. Progress is kept in a JSON state file and printed as a text or JSON report. A Terraform 0.6 configuration describes the shared landing zone (VPC, subnets, security group, DB subnet group, artifacts bucket). All AWS calls in this repository go to local moto servers.

Personal project built on the 2015-era stack (Python 3.4, boto3 1.1.4, paramiko 1.15.2, Terraform 0.6.3).

## Status

**Implemented**

- Configuration loading and validation with path-qualified errors such as `workloads[1].cpu: expected int` — `src/config.py`, tested in `tests/unit/test_config.py`.
- Workload assessment: declared dependencies, resource profile, rehost/replatform/refactor strategy, 0–100 risk score, blockers and a `ready` flag, written to a JSON file — `src/assessment.py`, tested in `tests/unit/test_assessment.py`.
- Dependency mapping over SSH during `--assess`: runs `netstat -tan` (falling back to `ss -tan`) with paramiko and reports listening ports and undeclared outbound peers. The host key is verified before any password is sent (known_hosts, a pinned SHA256 fingerprint, or opt-in trust-on-first-use), and each command is capped at 1 MiB of output and 60 s by default. A workload whose discovery failed is not marked ready — `src/discovery.py`, tested in `tests/unit/test_discovery.py` and `tests/integration/test_ssh_discovery.py`.
- Right-sizing and monthly cost estimation from the static catalog in `config/pricing.yml` — `src/sizing.py`, tested in `tests/unit/test_sizing.py`.
- Terraform 0.6.3 landing zone: VPC, two public and two private subnets, internet gateway, route table, app security group, DB subnet group and an artifacts bucket whose policy denies unencrypted uploads — `terraform/`, tested in `tests/unit/test_terraform_config.py` and by `scripts/tf_plan_check.sh`.
- Per-workload provisioning of a security group, EC2 instance with tags, gp2 EBS volume and RDS instance (`StorageEncrypted=True`), each recorded in a resource ledger — `src/aws_connector.py`, tested in `tests/unit/test_aws_connector.py`, `tests/unit/test_aws_connector_endpoints.py` and `tests/integration/test_provisioning.py`.
- Data migration to S3 with `AES256` server-side encryption, local MD5 compared with the `put_object` and `head_object` ETags, and a JSON manifest. The artifacts bucket must be configured (there is no default name). It is created only when S3 says it does not exist, and nothing is uploaded unless its ACL owner is this account's canonical ID — `src/data_migration.py`, tested in `tests/unit/test_data_migration.py` and `tests/integration/test_data_migration_s3.py`.
- Docker pipeline: Dockerfile generation for python, node, java and php runtimes, exec-form `CMD`, build/tag/push to a configured private registry through an injectable command runner, and a `docker inspect` summary — `src/docker_builder.py`, tested in `tests/unit/test_docker_builder.py` and `tests/unit/test_command_runner.py`.
- Post-migration validation: instance state, a TCP connect to the workload's validation endpoint, and re-verification of the data manifest against S3 — `src/validation.py`, tested in `tests/unit/test_validation.py`.
- Rollback of a failed workload (or `--rollback NAME`) in reverse creation order, treating missing resources as already deleted — `src/rollback.py`, tested in `tests/unit/test_rollback.py` and `tests/integration/test_rollback_moto.py`.
- Progress tracking in an atomically written JSON state file with allowed status transitions, timestamped history and the resource ledger — `src/state.py`, tested in `tests/unit/test_state.py`.
- Progress report with status, strategy, instance type, estimated cost, resources, files, bytes and elapsed time per workload, plus totals, as text or JSON — `src/report.py`, tested in `tests/unit/test_report.py` and `tests/integration/test_demo.py`.
- CLI with `--assess`, `--migrate [--workload NAME]`, `--rollback NAME` and `--report [--format text|json]`; exit code 0 on success, 1 when a workload failed, 2 on bad config — `src/migration_engine.py`, tested in `tests/unit/test_cli.py`, `tests/unit/test_engine.py` and `tests/integration/test_end_to_end.py`.

Added in the rebuild (not part of the original feature list):

- `--tfstate PATH` reads the root-module outputs of a Terraform 0.6 state file and fills the VPC, subnet, security group and bucket settings in the config, so the Terraform landing zone and the boto3 engine share IDs — `src/tfstate.py`, tested in `tests/unit/test_tfstate.py`.

**Not implemented / known limitations**

- AWS EC2, S3 and RDS are simulated with moto 0.4.14 servers; nothing has been run against a real AWS account.
- Source hosts are simulated by an in-process paramiko SSH server in tests; its host key is generated per test run and exported to a test known_hosts file.
- moto 0.4.14 has no bucket ACLs. `docker/moto/serve.py` adds `GET/PUT /<bucket>?acl` to the S3 simulator so the bucket ownership check can be tested, including a bucket staged as owned by another account.
- Host key types: the probe compares the key type paramiko negotiates (RSA first in paramiko 1.15). If known_hosts holds only another type for a host (for example ECDSA), the host is rejected as changed rather than matched. Pin the fingerprint or add the RSA key.
- A migration overwrites objects that already exist under `<workload>/` in the artifacts bucket, and rolling it back deletes them; use a bucket dedicated to migration artifacts.
- Docker build/push is exercised only through a recording fake docker command; no real registry is used.
- Terraform is checked with `terraform graph`, pyhcl/raw-text tests and an offline `terraform plan` that stops at configuration validation; it has never been planned against AWS or applied.
- moto 0.4.14 RDS ignores StorageEncrypted, so RDS encryption is shown only by unit tests.
- RDS against the simulator: boto3 1.1.4 parses moto-rds responses, so the RDS integration test runs as a normal test and the end-to-end and demo configs include `database` blocks. moto-rds errors come back as JSON `BadRequest` bodies rather than RDS XML, so deletion checks existence by listing instances.
- Moto fidelity is limited: there is no real networking, instances never boot, and AMI IDs and CIDR rules are only loosely validated.
- No IAM role or policy provisioning; moto 0.4 covers only a small part of IAM and Terraform 0.6.3 cannot plan IAM without real credentials.
- No NAT gateway in the landing zone (Terraform 0.6.3 has no resource for it); private subnets have no outbound route.
- No `terraform apply`, and no `plan` that reaches AWS: the 0.6.3 AWS provider validates credentials against real AWS and has no custom endpoints for EC2, S3, RDS or IAM.
- No AWS ECR; images go to a generic private registry host.
- No database dump and restore into RDS. The RDS instance is only created; database dump files placed in `data_path` travel through the S3 integrity path.
- No real `docker build` or `docker push` in the test suite; command construction and output parsing are tested with a fake runner.
- No disaster-recovery procedures beyond rolling back a failed migration.
- TLS for data in transit is not demonstrated: the moto servers are plain HTTP.
- No live AWS pricing; prices come from a hand-edited snapshot in `config/pricing.yml`.
- Right-sizing uses user-supplied peak-utilisation numbers; there is no CloudWatch or agent-based data collection.
- Dependency discovery sees only TCP connections open at probe time on hosts reachable over SSH; there is no long-running flow capture.
- No multipart S3 uploads (files above 5 GB are rejected) and no parallel migrations.
- No business-impact figures (speed-up, cost reduction, data-loss claims) are made; nothing like that is measured here.
- The `python:3.4` image ships 3.4.10, and the compose file uses format version 2, because current Docker cannot pull the older image manifests or read the version 1 format.

## Built with

- **Python 3.4** — boto3 1.1.4, botocore 1.2.6, jmespath 0.8.0, python-dateutil 2.4.2, docutils 0.12, six 1.9.0, PyYAML 3.11, paramiko 1.15.2, pycrypto 2.6.1, ecdsa 0.13; tests with pytest 2.8.0, py 1.4.30, pyhcl 0.1.11, ply 3.4 and flake8 2.4.1 (pep8 1.5.7, pyflakes 0.8.1, mccabe 0.3.1)
- **Terraform 0.6.3** — landing zone in `terraform/`
- **moto 0.4.14** — EC2, S3 and RDS simulators built from `docker/moto/Dockerfile`

## Running it

Everything runs in Docker images from the project's era, so nothing needs installing locally beyond Docker.

```bash
docker compose up -d moto-ec2 moto-s3 moto-rds   # start the AWS simulators
make demo                                        # assess, migrate and report using config/demo.yml
```

`aws.artifacts_bucket` must name a bucket this account owns, or be left empty and filled from the Terraform output with `--tfstate`.

A workload `source` block turns on SSH discovery during `--assess`. The host key must be known before the probe will log in:

```yaml
source:
  host: 10.0.0.12
  username: migrate
  key_file: ~/.ssh/id_rsa
  known_hosts: config/known_hosts            # checked together with ~/.ssh/known_hosts
  # host_key_fingerprint: "SHA256:..."       # optional pin, as printed by ssh-keygen -lf
  # trust_on_first_use: true                 # record an unknown key in known_hosts (logged)
  # max_output_bytes: 1048576                # per command, stdout + stderr
  # command_timeout: 60                      # seconds per command
```

## Tests

```bash
make test                # runs the suite inside the period image
```

The suite has unit tests plus integration tests against the moto servers and an in-process fake SSH server; it does not touch real AWS, a real Docker daemon or a real registry.

## Layout

```
.dockerignore
.gitignore
Dockerfile
Makefile
README.md
config/
  demo.yml
  migration.yml
  pricing.yml
docker-compose.yml
docker/
  moto/
    Dockerfile
    requirements.txt
    serve.py
docs/
  IMPLEMENTATION_PLAN.md
  SPEC.md
requirements-dev.txt
requirements.txt
scripts/
  tf_plan_check.sh
setup.cfg
src/
  assessment.py
  aws_connector.py
  config.py
  data_migration.py
  discovery.py
  docker_builder.py
  migration_engine.py
  report.py
  rollback.py
  sizing.py
  state.py
  tfstate.py
  validation.py
terraform/
  main.tf
  outputs.tf
  variables.tf
tests/
  __init__.py
  conftest.py
  fixtures/
    config/
      bad_cpu.yml
      dup_names.yml
      valid.yml
      with_database.yml
    data/
      web-portal/
        db/
          portal_db.sql
        uploads/
          logo.txt
    dockerfiles/
      java.Dockerfile
      node.Dockerfile
      php.Dockerfile
      python.Dockerfile
    e2e/
      migration.yml
    netstat_tan.txt
    ss_tan.txt
    terraform.tfstate
  integration/
    __init__.py
    conftest.py
    test_data_migration_s3.py
    test_demo.py
    test_end_to_end.py
    test_provisioning.py
    test_rollback_moto.py
    test_simulators.py
    test_ssh_discovery.py
  support/
    __init__.py
    aws.py
    fake_docker.py
    fake_ssh.py
    logs.py
  unit/
    __init__.py
    test_assessment.py
    test_aws_connector.py
    test_aws_connector_endpoints.py
    test_cli.py
    test_command_runner.py
    test_config.py
    test_data_migration.py
    test_discovery.py
    test_docker_builder.py
    test_engine.py
    test_environment.py
    test_py34_compat.py
    test_readme.py
    test_report.py
    test_rollback.py
    test_sizing.py
    test_state.py
    test_terraform_config.py
    test_tfstate.py
    test_validation.py
```
