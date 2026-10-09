# cloud-migration — Specification

Target period: May 2015 to September 2015. Every Python dependency and every tool listed here was released on or before 2015-09-30. The two exceptions are forced by what still runs today, and both are named in "Docker images" and "Stack & pinned versions".

## Problem

Moving a handful of on-premises workloads to AWS by hand is slow and easy to get wrong. The operator has to:

- work out what each workload depends on
- decide how to move it (rehost, replatform or refactor)
- size and price the target
- build the network
- containerise what can be containerised
- copy the data and prove it arrived intact
- check the result, and undo everything if something fails

This project is a command-line migration framework that does those steps from one YAML file. It has three parts:

- A Python 3.4 engine uses boto3 to assess workloads, provision per-workload EC2/EBS/RDS resources, copy data to S3 with checksums, build and push Docker images, validate the result, roll back on failure, and report progress.
- Terraform 0.6 builds the shared landing zone: VPC, subnets, security group, DB subnet group and artifacts bucket.
- Every call that would touch AWS goes to local moto servers instead. No real AWS account is ever used.

The rebuild keeps the technical concept of the original README and trims it to a core that one person can build and that actually works.

## In scope

1. **Configuration loading and validation.** The engine reads `config/migration.yml`, which covers AWS settings, the Docker registry and the workload list. It validates required keys and types and raises `ConfigError` with the offending path, such as `workloads[1].cpu: expected int`. (Original: "YAML: Configuration management".)
2. **Workload assessment.** For each workload the engine produces:
   - its declared dependencies (database, services)
   - a resource profile
   - a migration strategy from a subset of the "6 Rs": `rehost`, `replatform` or `refactor`
   - a 0–100 risk score
   - blockers (licensed software, compliance review)
   - a `ready` flag

   The results are written to a JSON file. (Original: "Automated workload assessment".)
3. **Dependency mapping over SSH.** When a workload has a `source` host block, the engine connects with paramiko and runs `netstat -tan` (falling back to `ss -tan`). It parses the output into LISTEN ports and ESTABLISHED outbound peers, and merges these with the dependencies declared in config. Peers are matched to known endpoints (`host:port`). Anything unmatched is reported as `undeclared`. (Original: "dependency mapping".)
   - The host key is checked before any credentials are sent: unknown keys are rejected (as with paramiko `RejectPolicy`). A key is accepted when it is listed in `~/.ssh/known_hosts` or the source's `known_hosts` file, or matches a pinned `host_key_fingerprint` (`SHA256:<base64>`). A pinned fingerprint is always enforced, and a changed key is always rejected. `trust_on_first_use: true` (it requires `known_hosts`) records an unknown key in that file and logs its fingerprint.
   - Each command's stdout and stderr are read together, capped at `max_output_bytes` (default 1 MiB) in total and limited to `command_timeout` seconds (default 60) overall. Exceeding either limit closes the channel and raises a `DiscoveryError` that names the host and the limit.
   - A workload whose discovery failed gets the blocker "Dependency discovery failed; dependencies are unverified", so it is not `ready`.
4. **Right-sizing and cost estimation.** The engine picks the smallest instance type from a static catalog (`config/pricing.yml`) that fits the workload:
   - required vCPU = `cpu × peak_cpu_pct/100 × 1.25`
   - required memory = `memory × peak_mem_pct/100 × 1.25`
   - when there is no `utilization` block, the declared cpu and memory are used as they are

   Monthly cost = instance hourly × 730, plus gp2 GB-month × storage, plus an RDS class and storage estimate when there is a database. The catalog is a hand-maintained snapshot and is labelled as such. (Original: "Cost estimation and right-sizing recommendations".)
5. **Terraform landing zone.** Terraform 0.6.3 HCL builds:
   - a VPC
   - two public and two private subnets (CIDRs from variables)
   - an internet gateway and a public route table
   - an app security group
   - a DB subnet group
   - an artifacts S3 bucket whose bucket policy denies unencrypted `PutObject`

   The config is checked in three ways: `terraform graph` (parse and interpolation references), an offline `terraform plan -input=false -refresh=false` in a container with no network (the only 0.6.3 command that runs the provider schema validation, see Risks), and pyhcl plus raw-text structure tests. (Original: "Terraform-based infrastructure provisioning", "S3 buckets configured with server-side encryption", "VPC security groups restrict access to known CIDR ranges".)
6. **Terraform outputs import (integration aid added in the rebuild).** This feature is not in the original README. It is small glue that the rebuild adds so the Terraform landing zone and the boto3 engine share network IDs, and the final README lists it separately as a rebuild addition. `--tfstate PATH` reads the `outputs` of the root module from a Terraform 0.6 state file (version 1 JSON). It fills in `aws.vpc_id`, `aws.subnet_ids`, `aws.security_group_id` and `aws.artifacts_bucket` wherever the config leaves them empty. (This links features 5 and 7.)
7. **Per-workload provisioning (boto3).** For each workload the engine:
   - creates a security group with one ingress rule per declared port, restricted to `aws.allowed_cidr`. The group name is unique per provisioning attempt (`migration-<name>-<8 hex chars of uuid4>`) and the group is tagged `Name=migration-<name>`, because EC2 (and moto) reject a duplicate group name in the same VPC, and a re-run or a second workload attempt must not collide with a group left by an earlier one
   - runs one EC2 instance of the recommended type and tags it with `create_tags`
   - creates and attaches a gp2 EBS volume at `/dev/xvdf` when `storage > 0`, in the instance's placement AZ, falling back to the subnet AZ or `<region>a` when moto reports no zone
   - calls `create_db_instance` with `StorageEncrypted=True` when a `database` block is present; the master password is read from the environment variable named by `database.password_env`

   Every created resource ID is recorded in the state ledger. (Original: "AWS: EC2, S3, RDS, VPC".)
8. **Data migration with integrity verification.** `aws.artifacts_bucket` is required: there is no default bucket name, and the engine stops with a config error (exit 2) when it is empty after `--tfstate` is applied. Before the first upload the engine checks the bucket. `head_bucket` must succeed, or it must answer 404/`NoSuchBucket`, in which case the bucket is created; any other error stops the upload. The bucket's ACL owner (`get_bucket_acl`) must also equal the caller's canonical ID (`list_buckets()['Owner']['ID']`), and this is checked again after a create. A bucket owned by anyone else gets nothing. The engine then uploads every file under a workload's `data_path` to `s3://<artifacts_bucket>/<workload>/…`. A symlink that resolves outside `data_path` stops the upload, and a symlink that stays inside it is skipped. Files are opened without following links (`O_NOFOLLOW`), checked with `fstat` to be regular files, and hashed and uploaded from that one open handle, so a file swapped for a link after listing is refused. `verify` refuses a manifest whose bucket differs from the configured one and checks the bucket owner before any `head_object`:
   - it computes an MD5 for each file locally
   - it compares that MD5 with the ETag returned by `put_object` and with the one returned by a later `head_object`
   - it sends `ServerSideEncryption='AES256'`

   It writes a manifest JSON with key, size, md5 and status. Any mismatch fails the workload. (Original: "Data migration with integrity verification", "Zero Data Loss" is dropped as a claim and kept only as the behaviour "mismatch ⇒ fail".)
9. **Docker containerisation pipeline.** The engine:
   - generates a Dockerfile for `python`, `node`, `java` or `php` runtimes, using period base images
   - splits the entrypoint into an exec-form `CMD`
   - runs `docker build`, `docker tag` and `docker push` against the configured private registry through an injectable command runner
   - summarises `docker inspect` output (size, layers, os, arch)

   (Original: "Docker containerization pipeline".)
10. **Post-migration validation.** The engine checks three things:
    - the instance state is `running` (`describe_instances`)
    - a TCP connect to `validation.host:port` succeeds within a timeout, for any workload that declares a `validation` block
    - the data manifest re-verifies against S3

    If any check fails, the workload fails. (Original: "Validated migration".)
11. **Rollback.** When a workload fails, or on `--rollback NAME`, the engine deletes that workload's ledger resources in reverse creation order: S3 objects, RDS instance, then detach (with `VolumeId`, `InstanceId` and `Device` from the ledger) and delete the volume, terminate the instance, and delete the security group. It then marks the workload `rolled_back`. Missing resources count as already deleted, so running rollback twice is safe. For EC2 this means the `*.NotFound` error codes. For RDS the engine lists `describe_db_instances()` and treats an identifier that is absent from the list as deleted, because moto 0.4.14 RDS errors carry no parseable error code (see Risks). `--rollback` of a workload left in an in-progress status by a crashed run first moves it to `failed` with `error="interrupted run"`, then rolls back. (Original: "Rollback and disaster recovery procedures". Only rollback is in scope. DR is not, see below.)
12. **Progress tracking.** A JSON state file (`state/migration_state.json`) records each workload's status, its timestamped transitions and its resource ledger. Writes are atomic: a temp file followed by `os.replace`. The allowed statuses are `pending → assessed → provisioning → provisioned → containerised → data_migrated → validated → completed`, plus `failed` and `rolled_back`. `--assess` with a state file moves `pending` workloads to `assessed` and leaves others unchanged. Illegal transitions raise `StateError`. `--migrate` on a `completed` workload does not attempt the illegal `completed → provisioning` move: it prints `<name>: already completed, use --rollback first` to stderr, skips the workload and makes the exit code 1. (Original: "Migration progress tracking".)
13. **Reporting.** `--report [--format text|json]` prints one row per workload with:
    - name, status and strategy
    - instance type and estimated monthly cost
    - resources created and files/bytes migrated
    - elapsed time

    It ends with totals. (Original: "reporting".)
14. **CLI.** `python src/migration_engine.py` accepts:
    - `--config PATH`
    - `--state PATH`
    - `--tfstate PATH`
    - `--output PATH`
    - exactly one of `--assess`, `--migrate [--workload NAME]`, `--rollback NAME` or `--report [--format text|json]`

    Exit code is 0 on success, 1 when any workload failed, and 2 on bad config. The `--assess` and `--migrate` flags are the ones the original README used.

## Out of scope

| Original README item | Reason |
|---|---|
| Real AWS account (EC2, S3, RDS, VPC, IAM) | This is a portfolio rebuild with no cloud account. All AWS calls go to moto 0.4.14 servers. |
| IAM role/policy provisioning ("IAM roles follow least-privilege") | moto 0.4 IAM covers only a small slice. Terraform 0.6.3 cannot plan without real credentials, so any IAM code would be untestable here. It is listed as a limitation. |
| NAT gateway in the landing zone | The AWS managed NAT gateway launched in December 2015, after the period, and Terraform 0.6.3 has no resource for it. Private subnets have no outbound route. |
| `terraform apply`, and a `plan` that reaches AWS | The AWS provider in 0.6.2 and later validates credentials against real AWS when it is configured, and it has no custom endpoint for EC2/S3/RDS/IAM (only DynamoDB, from 0.6.2). Tests run `plan` offline only as far as its configuration validation step, which happens before the provider is configured; the expected credential failure afterwards is ignored. |
| AWS ECR as the registry | ECR launched in December 2015, after the period. A generic private registry host (Docker Registry 2 style) is configured instead. |
| Actual database data copy (dump/restore into RDS) | moto provides only the control plane. The RDS instance is created, and database dump files placed in `data_path` travel through the S3 integrity path. |
| Real `docker build` / `push` in the test suite | A 2015 Docker CLI cannot talk to a modern daemon (the minimum API version is now higher). Docker-in-Docker would add a lot of machinery for little gain. Command construction and output parsing are tested with a fake runner. |
| Disaster-recovery procedures beyond rollback | "DR" in the original is not described beyond the word. Only rollback of a failed migration is concrete enough to build. |
| "All data in transit encrypted via TLS" | Moto servers are plain HTTP. Against real AWS, boto3 would use HTTPS by default, and nothing in this code turns that off. The claim cannot be demonstrated here. |
| Live AWS pricing (Price List API) | The Price List API launched in December 2015. Prices come from a static, hand-edited catalog. |
| Business-impact metrics (60% faster, 35% cost reduction, zero data loss, standardized process) | These are unmeasured claims and are not carried over. |
| Employer, role, dates and "Status: Complete" badges | These are fabricated and are not carried over. |

## Architecture

```
                      config/migration.yml   config/pricing.yml   terraform.tfstate (optional)
                                 │                   │                    │
                                 ▼                   ▼                    ▼
 ┌──────────────────────────────────────── migration_engine.py (CLI) ─────────────────────────────┐
 │  config.load() ─► tfstate.merge_outputs()                                                       │
 │                                                                                                 │
 │  --assess   assessment.assess() ──► sizing.recommend()/estimate_cost()                          │
 │                  └─► discovery.discover(ssh) ──paramiko──► source host (netstat -tan)           │
 │                  └─► assessment_results.json                                                     │
 │                                                                                                 │
 │  --migrate  per workload:                                                                        │
 │     state.transition() ─► AWSConnector.provision() ──boto3──► EC2 / RDS  (moto-ec2, moto-rds)    │
 │                        ─► DockerBuilder.build/push() ──runner──► docker CLI ► private registry    │
 │                        ─► DataMigrator.migrate() ──boto3──► S3 (moto-s3, s3bucket_path)           │
 │                        ─► Validator.validate()  (EC2 state, TCP connect, manifest re-verify)     │
 │     on error ─► Rollback.run(ledger) (reverse order)                                             │
 │                                                                                                 │
 │  --report   report.render(state, assessment) ─► stdout (text|json)                              │
 └──────────────────────────────── state/migration_state.json (ledger + history) ─────────────────┘

 terraform/ (0.6.3 HCL) ─► landing zone: VPC, 2×public, 2×private subnets, IGW, route table,
                          app SG, DB subnet group, artifacts bucket + SSE-enforcing policy
```

Components (all modules live in `src/` and are imported flat, with `PYTHONPATH=src`, as the original code did):

| Module | Responsibility |
|---|---|
| `migration_engine.py` | CLI parsing and orchestration. `MigrationEngine` wires the other modules together. |
| `config.py` | YAML load, defaults and validation (`ConfigError`). |
| `tfstate.py` | Reads Terraform 0.6 state v1 outputs and merges them into config. |
| `sizing.py` | Instance catalog, right-sizing and cost estimation. |
| `assessment.py` | Strategy, risk, blockers and the dependency merge. Writes assessment JSON. |
| `discovery.py` | `parse_netstat()` and `parse_ss()`, which are pure functions, plus `SSHProbe` (paramiko). |
| `state.py` | `MigrationState`: statuses, transitions, ledger and atomic persistence. |
| `aws_connector.py` | boto3 clients (with optional `endpoint_url` per service), provisioning, health and deletes. |
| `data_migration.py` | `DataMigrator`: upload, checksum, manifest, and `verify()`. |
| `docker_builder.py` | Dockerfile generation, build/tag/push through `CommandRunner`, and inspect summary. |
| `validation.py` | `Validator`: instance, TCP and data checks. |
| `rollback.py` | Reverse-order deletion of the ledger. |
| `report.py` | Text and JSON rendering. |

## Data model & interfaces

### `config/migration.yml`

```yaml
aws:
  region: us-east-1
  vpc_id: ""                 # may be filled from --tfstate output vpc_id
  subnet_ids: []             # from output private_subnet_ids (comma-separated in 0.6 outputs)
  security_group_id: ""      # optional; from output app_security_group_id
  artifacts_bucket: ""       # from output artifacts_bucket
  allowed_cidr: 10.0.0.0/8
  default_ami: ami-1a2b3c4d
  endpoints:                 # optional; only set for simulators
    ec2: http://moto-ec2:5000
    s3:  http://moto-s3:5001
    rds: http://moto-rds:5002
docker:
  registry: registry.local:5000
  command: docker            # path to docker binary (tests point this at a fake)
  base_images: {}            # optional overrides of the runtime → image map
pricing_file: config/pricing.yml
workloads:
  - name: web-portal                 # required, [a-z0-9-]+
    type: stateless | stateful | legacy
    runtime: python | node | java | php
    cpu: 2                            # vCPU, int, required
    memory: 4                         # GiB, number, required
    storage: 50                       # GiB, int, default 0
    port: 8080
    ports: [80, 443]
    containerizable: true
    entrypoint: "gunicorn app:app"
    version: "1.0"
    source_path: ./apps/web-portal    # Docker build context
    data_path: ./data/web-portal      # files to migrate to S3
    utilization: {peak_cpu_pct: 40, peak_mem_pct: 55}
    licensed_software: false
    compliance_requirements: false
    source: {host: 10.0.0.12, port: 22, username: migrate, key_file: ~/.ssh/id_rsa,
             known_hosts: config/known_hosts,           # or host_key_fingerprint: "SHA256:..."
             trust_on_first_use: false, max_output_bytes: 1048576, command_timeout: 60}
    services: [{name: auth-service, endpoint: "auth.internal:5000"}]
    database: {engine: mysql, host: db.internal, port: 3306, name: portal_db,
               username: portal_user, password_env: PORTAL_DB_PASSWORD, storage: 100}
    validation: {host: 10.0.1.20, port: 8080, timeout: 5}
```

### `config/pricing.yml`

This is a static snapshot, and its header comment says so.

```yaml
hours_per_month: 730
instances:            # name: {vcpu, memory_gib, hourly_usd}
  t2.micro:  {vcpu: 1, memory_gib: 1,  hourly_usd: 0.013}
  ...
  m4.2xlarge:{vcpu: 8, memory_gib: 32, hourly_usd: 0.504}
ebs_gp2_gb_month: 0.10
rds:
  classes: {db.t2.micro: 0.017, db.t2.small: 0.034, db.t2.medium: 0.068,
            db.m4.large: 0.175, db.m4.xlarge: 0.350}
  storage_gb_month: 0.115
```

### Assessment output (`assessment_results.json`, list)

```json
{"name": "web-portal", "type": "stateless", "timestamp": "2015-08-01T10:00:00",
 "dependencies": [{"type": "database", "engine": "mysql", "host": "db.internal", "port": 3306, "source": "config"},
                  {"type": "service", "name": "auth-service", "endpoint": "auth.internal:5000", "source": "config"},
                  {"type": "undeclared", "endpoint": "10.0.0.40:11211", "source": "ssh"}],
 "listening_ports": [8080],
 "resource_profile": {"cpu_cores": 2, "memory_gb": 4, "storage_gb": 50, "recommended_instance": "t2.medium"},
 "migration_strategy": "replatform", "risk_score": 20,
 "estimated_cost": {"instance": 37.96, "storage": 5.0, "database": 139.25, "total": 182.21},
 "ready": true, "blockers": []}
```

### State file (`state/migration_state.json`)

```json
{"version": 1,
 "workloads": {
   "web-portal": {
     "status": "completed",
     "history": [{"status": "provisioning", "at": "..."}],
     "ledger": [{"kind": "security_group", "id": "sg-…", "name": "migration-web-portal-3f9c2a1b"}, {"kind": "instance", "id": "i-…"},
                {"kind": "volume", "id": "vol-…", "instance_id": "i-…", "device": "/dev/xvdf"}, {"kind": "db_instance", "id": "migrated-portal-db"},
                {"kind": "s3_object", "bucket": "…", "key": "web-portal/…"}],
     "image": "registry.local:5000/web-portal:1.0",
     "data": {"files": 3, "bytes": 2048, "manifest": "state/web-portal.manifest.json"},
     "error": null}}}
```

### Data manifest (`state/<workload>.manifest.json`)

```json
{"workload": "web-portal", "bucket": "…", "files": [{"key": "web-portal/db/dump.sql", "size": 1024, "md5": "…", "status": "verified"}]}
```

### Python interfaces (signatures)

```python
config.load(path) -> dict                      # raises ConfigError
tfstate.read_outputs(path) -> dict             # {name: value}
tfstate.merge_outputs(config, outputs) -> dict
sizing.Catalog.from_file(path)
sizing.recommend_instance(workload, catalog) -> str
sizing.estimate_cost(workload, catalog) -> dict
assessment.assess(workloads, catalog, probe_factory=None) -> list
assessment.recommend_strategy(workload) -> str
assessment.risk_score(workload) -> int
discovery.parse_netstat(text) -> {"listening": [int], "established": [(ip, port)]}
discovery.parse_ss(text) -> same shape
discovery.SSHProbe(host, port, username, key_file=None, password=None, known_hosts=None,
                   host_key_fingerprint=None, trust_on_first_use=False,
                   max_output_bytes=1048576, command_timeout=60).collect() -> same shape
state.MigrationState.load(path); .transition(name, status); .record(workload_name, kind, **ids); .save()
AWSConnector(aws_cfg, catalog, session=None).provision(workload, state) -> {'security_group_id', 'instance_id', 'volume_id'}
AWSConnector.create_database(workload, state) -> db_id
AWSConnector.instance_state(instance_id) -> str
DataMigrator(s3_client, bucket).migrate(workload, state, manifest_dir='state') -> manifest_path
DataMigrator.verify(manifest_path) -> (ok, [problems])
DockerBuilder(docker_cfg, runner=CommandRunner()).generate_dockerfile(w) -> str; .build_image(w); .push(w); .analyze_image(name)
Validator(aws, data_migrator).validate(workload, state) -> (ok, [failures])
rollback.run(workload_name, state, aws, s3) -> list_of_deleted
report.render(state, assessment, fmt="text") -> str
```

### CLI

```
python src/migration_engine.py [--config config/migration.yml] [--state state/migration_state.json]
                               [--tfstate terraform/terraform.tfstate] [--output assessment_results.json]
                               (--assess | --migrate [--workload NAME] | --rollback NAME | --report [--format text|json])
```

## Stack & pinned versions

| Component | Version | Release date | Why it was the popular choice then |
|---|---|---|---|
| Python | 3.4 (image ships 3.4.10) | 3.4.0 2014-03-16; 3.4.3 2015-02-25 | This was the current Python 3 in mid-2015 (3.5 shipped 2015-09-13) and it is the stack named in the original README. The code uses only 3.4 syntax: no f-strings and no `subprocess.run`. |
| boto3 | 1.1.4 | 2015-09-24 | boto3 went GA in June 2015 as the new official AWS SDK. 1.1.x was current. |
| botocore | 1.2.6 | 2015-09-30 | Satisfies boto3 1.1.4's `>=1.2.0,<1.3.0`. |
| jmespath | 0.8.0 | 2015-09-23 | boto3/botocore dependency. |
| python-dateutil | 2.4.2 | 2015-03-31 | botocore dependency. |
| docutils | 0.12 | 2014-07-14 | botocore dependency. |
| six | 1.9.0 | 2015-01-02 | Shared dependency. |
| PyYAML | 3.11 | 2014-03-27 | The standard YAML library of the time. |
| paramiko | 1.15.2 | 2014-12-19 | The de facto Python SSH client in 2015 (Fabric/Ansible used it). |
| pycrypto | 2.6.1 | 2014-06-20 | paramiko 1.15 dependency. Needs gcc, so the full `python:3.4` image is used. |
| ecdsa | 0.13 | 2015-02-07 | paramiko 1.15 dependency. |
| pytest | 2.8.0 | 2015-09-18 | The dominant Python test runner. `unittest.mock` from the 3.4 stdlib is used for mocks. |
| py | 1.4.30 | 2015-06-26 | pytest 2.8 dependency. |
| pyhcl | 0.1.11 | 2015-04-24 | The only Python HCL parser of the time. Used to assert Terraform structure. |
| ply | 3.4 | 2012-04-18 | Pinned exactly by pyhcl 0.1.11's `requirements.txt`. |
| flake8 | 2.4.1 | 2015-05-18 | The standard linter. |
| pep8 | 1.5.7 | 2014-05-29 | flake8 2.4.1 excludes pep8 1.6.0–1.6.2. |
| pyflakes | 0.8.1 | 2014-03-30 | flake8 2.4.1 requires `<0.9`. |
| mccabe | 0.3.1 | 2015-06-14 | flake8 dependency. |
| Terraform | 0.6.3 (linux_amd64 zip, SHA256 `0160fcdb7f0d00948d52912df0626a2e49db958b6df2c6108cbd8b3527ce1144`) | 2015-08-11 (official CHANGELOG; 0.6.4 followed on 2015-10-15) | The latest Terraform in the period, and the version named in the original README. No registry. It is downloaded from releases.hashicorp.com in the Dockerfile and the checksum is verified. |
| moto (simulator only) | 0.4.14, pinned by commit `967c778390a8d5991a475fa92856e627d3116b1b` | 2015-09-17 (tag date) | This was the standard AWS mock of 2015. PyPI has no files for 0.4.14, so `check_period.py` cannot gate it. It is installed in `docker/moto/Dockerfile` from the commit tarball `https://github.com/getmoto/moto/archive/967c778390a8d5991a475fa92856e627d3116b1b.tar.gz`, not the movable tag, and the tarball's sha256 (`ddf4f4c72dc614c29475f811bdb25e30b2039baa56a628af63bc0284ad433445`) is checked before install. |
| moto server deps: boto 2.38.0, Flask 0.10.1, Werkzeug 0.10.4, Jinja2 2.8, MarkupSafe 0.23, itsdangerous 0.24, httpretty 0.8.10, xmltodict 0.9.2, six 1.9.0, requests 2.7.0 | as listed | 2013-06-14 … 2015-07-26 (latest: Jinja2 2.8, 2015-07-26) | These satisfy moto 0.4.14's `install_requires`. They are pinned in `docker/moto/requirements.txt` and installed with `--no-deps`. |
| Docker Compose file | format `version: "2"` | Compose 1.6, 2016-01 (post-period) | This is a deliberate deviation. Current `docker compose` no longer reads the 2015 v1 format (no `services:` key). |

The `requests` pin in the original `requirements.txt` is removed from the app, because nothing in the app uses it. It remains only as a moto server dependency.

The pins were verified with `tools/check_period.py` over `requirements.txt`, `requirements-dev.txt` and `docker/moto/requirements.txt`: 28 pins checked, 0 problems, exit 0.

## Docker images

| Image | Use | Hub check | Note |
|---|---|---|---|
| `python:3.4` | app, tests and Terraform (built from `Dockerfile`) | 200 | Multi-arch, last updated 2019-03-20, and ships 3.4.10. The period-exact `python:3.4.3` tag exists (200) but is a schema-1 manifest, and current Docker refuses to pull it ("Docker Image manifest version 2, schema 1 has been removed"). That was verified by an actual build attempt, so `python:3.4` is the nearest usable tag. The app service is forced to `platform: linux/amd64` because Terraform 0.6.3 ships no arm64 binary. |
| `python:3.4` | moto simulators (built from `docker/moto/Dockerfile`), three containers: `moto-ec2` (`moto_server ec2 -p 5000`), `moto-s3` (`moto_server s3bucket_path -p 5001`), `moto-rds` (`moto_server rds -p 5002`) | 200 | moto 0.4 serves one service per process, so there is one container per service. |
| `python:3.4-slim`, `python:2.7-slim`, `node:0.12-slim`, `php:5.6-apache`, `java:8u45-jre`, `ubuntu:14.04` | These strings appear only in generated Dockerfiles. They are never pulled by the test suite. | all 200 | `java:8-jre` (the usual 2015 name) now returns 404, so the dated `java:8u45-jre` tag (Java 8u45, April 2015) is used. |

## Simulated/mocked integrations

| Real system | Replacement | Where |
|---|---|---|
| AWS EC2 (security groups, instances, tags, EBS) | moto 0.4.14 `moto_server ec2` in container `moto-ec2` | Integration tests and `make demo` |
| AWS S3 | moto 0.4.14 `moto_server s3bucket_path` (path-style) in container `moto-s3`, started through `docker/moto/serve.py`. The wrapper adds `GET /<bucket>?acl`, which returns the bucket owner (by default the ListBuckets owner ID), and `PUT /<bucket>?acl`, which sets that owner so tests can stage a bucket that belongs to another account. moto 0.4.14 implements neither. | Integration tests and `make demo` |
| AWS RDS | moto 0.4.14 `moto_server rds` in container `moto-rds`. If its responses do not parse with boto3 1.1.4 (see Risks), RDS is covered only by `unittest.mock` fakes of the boto3 client, the integration test is marked `xfail` with the reason, and the e2e and demo configs carry no `database` blocks. Even when it parses, moto ignores `StorageEncrypted`, so encryption is shown only by unit mocks. | Integration tests |
| AWS credentials and DB passwords | Static fake credentials `AWS_ACCESS_KEY_ID=testing` / `AWS_SECRET_ACCESS_KEY=testing`, plus `PORTAL_DB_PASSWORD=demo` and `REPORTS_DB_PASSWORD=demo` (named by `password_env` in the sample config), in the compose env of `app` | Everywhere |
| On-prem source host reachable over SSH | `tests/support/fake_ssh.py`: an in-process paramiko `ServerInterface` server on 127.0.0.1 that answers `exec` of `netstat -tan` / `ss -tan` with fixture text. Its RSA host key is generated when the test process starts and written to a per-test known_hosts file, so discovery tests run with host key verification on. Handlers simulate endless, trickling and large-stderr output | Integration test for discovery |
| Docker daemon and private registry | `tests/support/fake_docker.py`, a recording executable that `docker.command` points at. Unit tests inject a fake `CommandRunner`. | Unit tests, e2e and `make demo` |
| Migrated application endpoint (validation TCP check) | A `socket` listener started by the test on 127.0.0.1 | Integration tests |
| Terraform against AWS | Not executed. `terraform graph`, an offline `terraform plan` in a no-network container that is checked only for configuration errors, and pyhcl/raw-text assertions run. | `make tf-check`, part of `make test` |

The final README must say plainly that AWS, the SSH source hosts and the Docker registry are simulated, and that nothing has been run against real AWS.

## Existing code inventory

| File | Verdict | Reason |
|---|---|---|
| `Dockerfile` | refactor | `python:3.4-slim` cannot compile pycrypto, so switch to `python:3.4`. Add the Terraform 0.6.3 download with a SHA256 check, dev requirements and `PYTHONPATH=src`. |
| `README.md` | refactor | Regenerated in the last task from `tools/readme_template.md`. |
| `config/migration.yml` | refactor | Keep the three sample workloads. Replace the ECR registry (post-period) with a generic private registry. Drop the placeholder VPC IDs (they now come from tfstate or are empty). Add `data_path`, `utilization`, `validation`, `endpoints` and `pricing_file`. Change service endpoints to `host:port`. |
| `requirements.txt` | refactor | Keep the boto3 1.1.4, PyYAML 3.11 and paramiko 1.15.2 pins. Bump botocore 1.2.0 → 1.2.6 (both are in the period). Add exact pins for transitive dependencies. Drop the unused `requests`. |
| `src/aws_connector.py` | refactor | The overall structure is good: the provisioning steps, the security-group rules and the RDS call. Fix these: f-strings (3.6+); `TagSpecifications` (a 2017 API) → `create_tags`; `volume_id` computed but not returned or recorded; health/connectivity/integrity stubs that return `True`; `destroy_infrastructure` that does nothing. Add endpoints, a ledger and waits. Move instance sizing to `sizing.py`. Remove the unused `iam` client. Create the S3 client with `Config(signature_version='s3v4')` (see Risks). |
| `src/docker_builder.py` | refactor | Keep the Dockerfile generator and the inspect summary. Fix these: f-strings; `subprocess.run(capture_output=, text=)`, which is 3.5/3.7 only, → `CommandRunner` over `subprocess.Popen`; `CMD ["gunicorn app:app"]`, which puts the whole command in one argv element, → a `shlex.split` exec form; the ECR naming; the `openjdk:8-jre` base image → `java:8u45-jre` (the `openjdk` Hub repository dates from 2016). `node:4-slim` is itself in the period (Node 4.0.0 shipped 2015-09-08) but is swapped for `node:0.12-slim`, because 0.12 was the widely deployed Node line through mid-2015 and 4.0 appeared only three weeks before the period ends. Also fix the flake8 findings (unused imports, E128 continuation lines). |
| `src/migration_engine.py` | refactor | Keep the CLI flags, strategy rules, risk scoring, blocker checks and dependency extraction, and move them into `assessment.py` and `sizing.py`. Fix these: f-strings; `sys.exit` inside config loading → `ConfigError`; a hard-coded output path; rollback that is logged but does nothing; the unused `os` import and unused `volume_id` (F841). |
| `terraform/main.tf` | refactor | Keep the resource set (VPC, subnets, IGW, SG, DB subnet group, bucket) and the tags that the 0.6.3 schema allows. It is written in 0.12+ syntax (bare `var.x`, `[*]` splats, `tags = {}`), uses `cidrsubnet` (not in 0.6.3), `aws_nat_gateway` (Dec 2015), `versioning` (0.6.4) and `server_side_encryption_configuration` (2017). It also has schema errors against the 0.6.3 provider: `aws_security_group.name_prefix` (not in 0.6.3, use `name`), `tags` on `aws_db_subnet_group` (not in 0.6.3, dropped) and a missing Required `description` on `aws_db_subnet_group`. Rewrite it in 0.6.3 HCL, fix those, add the route table, and move outputs to `outputs.tf`. |
| `terraform/variables.tf` | refactor | The four variables are already valid 0.6 syntax and are kept. Add `public_subnet_cidrs`, `private_subnet_cidrs` (comma-separated strings for `split()`) and `availability_zones`. |

No existing file is deleted.

## Test strategy

- **Runner:** pytest 2.8.0 inside the `app` image (`python:3.4`). `make test` does the following:
  1. `docker compose build`
  2. `docker compose up -d moto-ec2 moto-s3 moto-rds`
  3. `docker compose run --rm app make ci`, where `ci` = `flake8 src tests` + `py.test -v tests` + `terraform graph terraform > /dev/null`
  4. `docker compose run --rm --no-deps tf-plan`, a service built from the same image with `network_mode: none` and `entrypoint: ["sh", "/app/scripts/tf_plan_check.sh"]` (overriding the image's `python src/migration_engine.py` entrypoint), whose script runs `terraform plan -input=false -refresh=false` with fake credentials and fails if the output contains `There are warnings and/or errors related to your configuration` (the later credential error is expected and ignored)
  5. `docker compose down -v`, which always runs

  `make test-unit` runs `py.test tests/unit` without simulators.
- **Unit tests (`tests/unit/`):** pure logic, with no network.
  - config validation (good and bad fixtures)
  - sizing and cost arithmetic, with hand-computed expected values
  - strategy, risk and blockers
  - netstat/ss parsing from fixture files
  - state transitions and atomic save
  - Dockerfile generation against golden files in `tests/fixtures/dockerfiles/`
  - docker command vectors through a fake runner
  - AWS connector calls through `unittest.mock` boto3 clients
  - checksum and manifest logic with a fake S3 client
  - rollback ordering
  - report rendering (text and JSON)
  - tfstate v1 parsing
  - Terraform structure through pyhcl: resource names, `tags`, bucket policy denying unencrypted puts, `name` (not `name_prefix`) on the security group, `description` and no `tags` on the DB subnet group, no `nat_gateway`, no 0.12 syntax markers. pyhcl 0.1.11 keeps only the last of repeated blocks, so the three `ingress` rules are asserted by a block scanner over the raw `main.tf` text.
  - the S3 client's `meta.config.signature_version == 's3v4'`
- **Integration tests (`tests/integration/`):** these run against the compose stack. Moto 0.4.14 keeps all state for the life of its container and has no reset endpoint, so state is shared by every test in the session. Every integration assertion is scoped to the resources the test itself created (by ID from the ledger, tag, name or key prefix). No test asserts that a list is globally empty. Moto's `describe_volumes` ignores `VolumeIds`, so results are filtered client-side. Tests must also not rely on globally unique resource names: a name another test (or an earlier `make demo`) may already have used in moto can collide. Security groups get a per-attempt unique name from the engine (see feature 7), and any other name a test chooses (buckets, DB identifiers, workload names used for DB identifiers) is made unique per test, for example with a uuid4 suffix.
  - boto3 → moto-ec2: security group, instance, tags, volume attach, terminate
  - boto3 → moto-s3: upload, head, ETag = MD5, manifest verify, delete
  - boto3 → moto-rds: create/delete, or `xfail` with the recorded reason
  - paramiko → in-process fake SSH server
  - the TCP validation check against a local socket
  - a full `--assess` → `--migrate` → `--report` run of the CLI with a test config, including a forced failure that must leave zero ledger resources in moto after rollback
  - `terraform graph` via subprocess
- **Environment guard:** `tests/unit/test_environment.py` asserts `sys.version_info[:2] == (3, 4)`, that each pinned distribution has the exact version (via `pkg_resources`), and that `terraform version` prints `v0.6.3`.

## Risks

- **S3 requests escaping to real AWS.** botocore 1.2.6 `utils.fix_s3_host` rewrites any request for a DNS-compatible bucket name to `http://<bucket>.s3.amazonaws.com`, even when `endpoint_url` is set, unless the client signs with `s3v4`. The default in 1.2.6 is SigV2 (`s3`), and every bucket name used here is DNS-compatible. `AWSConnector` and `tests/support/aws.py` therefore create the S3 client with `botocore.client.Config(signature_version='s3v4')`, which makes `fix_s3_host` return early and keeps requests path-style on the moto endpoint. A unit test asserts the setting.
- **moto RDS parsing.** botocore 1.2.6 may not parse moto 0.4.14's RDS responses. T7 decides this and records the outcome; the fallback is described under "Simulated/mocked integrations". Outcome (T7): boto3 1.1.4 `describe_db_instances()` parses moto-rds responses, so the RDS integration test is a normal passing test (no `xfail`), and the e2e and demo configs may carry `database` blocks.
- **moto S3 route order.** moto 0.4.14 builds its Flask url map from a dict, so rule order follows the Python string hash seed. Under some seeds the `s3bucket_path` rule `/<bucket>/` is tried before `/<bucket>`, and `PUT /<bucket>` (CreateBucket) fails with a Flask `FormDataRoutingRedirect` (HTTP 500). `docker/moto/Dockerfile` sets `PYTHONHASHSEED=0`, which gives the working order on every start.
- **Shared moto state.** See "Test strategy": tests must scope assertions to their own resources and must not rely on globally unique resource names. moto 0.4.14 `create_security_group` raises `InvalidGroup.Duplicate` for a repeated name in the same VPC namespace (`ec2/models.py`, checked in the pinned commit tarball), so the engine suffixes each group name with 8 hex chars of a uuid4.
- **moto EC2 gaps.** `DetachVolume` without `InstanceId` and `Device` returns HTTP 500, so the ledger stores both for each volume. Instances report no placement zone, so the volume AZ falls back to the subnet AZ or `<region>a`.

## Known limitations that will remain

- Nothing is ever run against real AWS. Moto 0.4 fidelity is limited: no real networking, instances never boot, and AMI IDs and CIDR rules are only loosely validated.
- The RDS database is only created, never populated. Database contents move only as dump files through S3.
- Right-sizing uses static, user-supplied peak-utilisation numbers and a hand-maintained price snapshot. There is no CloudWatch or agent-based data collection and no live pricing.
- Dependency discovery sees only TCP connections that are open at probe time on hosts reachable by SSH. There is no long-running flow capture.
- Terraform is validated only offline: `graph`, pyhcl/raw-text checks, and `plan` up to its configuration validation step. A full `plan` and `apply` need real AWS credentials in 0.6.x. Private subnets have no NAT.
- moto 0.4.14 RDS ignores `StorageEncrypted`, and its errors are JSON `BadRequest` bodies rather than RDS XML, so botocore cannot read an error code from them. Encryption is shown only by unit mocks, and RDS deletion checks existence by listing instances.
- Docker build and push are not exercised against a real daemon or registry in tests.
- No IAM provisioning, no DR procedures, no multi-part S3 uploads (files above 5 GB are rejected), and no parallel migrations.
- `python:3.4` ships 3.4.10 rather than a 2015 point release, and the compose file uses format 2 (2016). Both are forced by current Docker.
