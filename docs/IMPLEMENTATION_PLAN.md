# cloud-migration — Implementation Plan

This plan implements `docs/SPEC.md` in 17 tasks, T1 to T17. Each task is exactly one commit and leaves `make test` green. The tasks are ordered by dependency.

## Conventions for every task

- **What `make test` runs.** The Makefile exports `DOCKER_DEFAULT_PLATFORM=linux/amd64` and sets `COMPOSE ?= docker compose`. `make test` runs these steps:
  1. `$(COMPOSE) build`
  2. `$(COMPOSE) up -d` for the simulator services (once they exist, from T7)
  3. `$(COMPOSE) run --rm app make ci`
  4. `$(COMPOSE) down -v`, which always runs, even after a failure

  Inside the container, `make ci` runs `flake8` (from T2), `py.test -v tests`, and `terraform graph` (from T14). From T14, `make test` also runs the offline `tf-plan` service before `down`. `make test-unit` runs `py.test tests/unit` with no simulators.
- **Python 3.4 syntax only.** Use `str.format` and `subprocess.Popen`/`check_call`. Do not use f-strings, `subprocess.run`, type hints or `async`. `unittest.mock` comes from the stdlib. Keep the flat `src/` modules (`PYTHONPATH=src`), as the original code did.
- **Write the tests first.** Each task lists its red tests. The acceptance command must exit 0 before you commit.
- **Commit messages** have no dates, no attribution and no employer names.

---

## T1 — Scaffold Docker image, Makefile and pytest harness

**Goal:** From this commit on, `make test` builds the period image (Python 3.4 + Terraform 0.6.3) and runs pytest in it.

**Files:**
- modify `Dockerfile`:
  - `FROM python:3.4`, `ENV PYTHONPATH=/app/src PYTHONDONTWRITEBYTECODE=1`, `WORKDIR /app`.
  - Download `https://releases.hashicorp.com/terraform/0.6.3/terraform_0.6.3_linux_amd64.zip`.
  - Verify it with `echo "0160fcdb7f0d00948d52912df0626a2e49db958b6df2c6108cbd8b3527ce1144  /tmp/tf.zip" | sha256sum -c -`, then unzip it into `/usr/local/bin`.
  - Copy `requirements*.txt`. Run `pip install --no-deps ply==3.4` in its own `RUN` step first, then `pip install --no-deps -r requirements.txt -r requirements-dev.txt`. The order matters: pyhcl 0.1.11 uses distutils (so `setup_requires` is ignored) and its custom install command runs `import hcl` → `from ply import lex` after installing. With `--no-deps`, pip installs in file order, so if ply is not already present the pyhcl install fails with `ImportError`.
  - `COPY . .` and `ENTRYPOINT ["python", "src/migration_engine.py"]`.
- modify `requirements.txt`: set exactly these pins:
  - boto3==1.1.4
  - botocore==1.2.6
  - jmespath==0.8.0
  - python-dateutil==2.4.2
  - docutils==0.12
  - six==1.9.0
  - PyYAML==3.11
  - paramiko==1.15.2
  - pycrypto==2.6.1
  - ecdsa==0.13
- create `requirements-dev.txt`: pytest==2.8.0, py==1.4.30, ply==3.4, pyhcl==0.1.11 (ply listed before pyhcl, see above), flake8==2.4.1, pep8==1.5.7, pyflakes==0.8.1, mccabe==0.3.1.
- create `docker-compose.yml` in format `version: "2"`:
  - The `app` service builds from `.` with `platform: linux/amd64` and `entrypoint: []`, mounts `.:/app`, and sets environment `AWS_ACCESS_KEY_ID=testing`, `AWS_SECRET_ACCESS_KEY=testing` and `AWS_DEFAULT_REGION=us-east-1`.
  - Top-level `name: cloud-migration`; the `app` service image is tagged `cloud-migration:app`.
  - The default network uses the fixed subnet `172.49.0.0/24`. Any host port published by later tasks comes from the range 20900–20999.
- create `Makefile` with targets `build test test-unit ci lint tf-check demo shell down`:
  - `test` depends on `build`.
  - `ci` = `py.test -v tests`. The `demo` target is an `echo` placeholder until T16.
- create `setup.cfg`:
  - `[pytest]` with `testpaths = tests` and `markers = integration`
  - `[flake8]` with `max-line-length = 100` and `exclude = .git,__pycache__`
- create `.dockerignore` (`.git`, `state/`, `*.pyc`, `assessment_results.json`, `.cache/`) and `.gitignore` (`__pycache__/`, `*.pyc`, `state/`, `assessment_results.json`, `.terraform/`, `terraform/*.tfstate*`, `.cache/`). pytest 2.8 writes `.cache/` into the bind-mounted repo.
- create `tests/__init__.py`, `tests/unit/__init__.py`, `tests/integration/__init__.py` and `tests/conftest.py`. The conftest inserts `src` on `sys.path`.
- create `tests/unit/test_environment.py`.

**Tests to write first:**
- `test_environment.py` checks these three things:
  - `sys.version_info[:2] == (3, 4)`.
  - For every line of `requirements.txt` and `requirements-dev.txt`, `pkg_resources.get_distribution(name).version` equals the pin.
  - `subprocess.check_output(['terraform', 'version'])` contains `Terraform v0.6.3`.

**Acceptance command:** `make test`

**Commit message:**
```
Add Docker test harness with pinned Python 3.4 toolchain

Build a python:3.4 image with exact dependency pins and Terraform 0.6.3,
and run pytest inside it via make test.
```

---

## T2 — Port existing modules to Python 3.4 syntax

**Goal:** The three existing modules import and pass flake8 under 3.4, and their behaviour does not change. `make ci` now runs lint.

**Files:**
- modify `src/aws_connector.py`:
  - Convert f-strings to `.format`.
  - Replace `TagSpecifications` in `run_instances` with a follow-up `self.ec2.create_tags(Resources=[id], Tags=[...])`.
  - Return `volume_id` from `provision_infrastructure` (as part of a dict `{'instance_id', 'volume_id', 'security_group_id'}`).
- modify `src/docker_builder.py`:
  - Convert f-strings.
  - Add a `CommandRunner` class with `run(argv, timeout=None) -> (returncode, stdout, stderr)`, built on `subprocess.Popen(..., universal_newlines=True)` and `communicate(timeout=)`.
  - Route `build_image`, `push_to_ecr` and `analyze_image` through it (`DockerBuilder(config, runner=None)`).
- modify `src/migration_engine.py`: convert f-strings.
- Fix all flake8 2.4.1 findings in the three existing modules: the unused `os` import in `migration_engine.py`, the unused `volume_id` (F841), and the misaligned continuation lines (E128). Remove the unused `iam` client from `AWSConnector`.
- modify `Makefile`: `ci` = `flake8 src tests && py.test -v tests`.
- create `tests/unit/test_py34_compat.py` and `tests/unit/test_aws_connector_tags.py`.

**Tests to write first:**
- `test_py34_compat.py` checks two things:
  - `compileall.compile_dir('src', quiet=1)` returns true.
  - `aws_connector`, `docker_builder` and `migration_engine` import, with boto3 clients created lazily or `boto3.client` patched through `mock.patch`.
- `test_aws_connector_tags.py`: with a `MagicMock` EC2 client, `_launch_instance` calls `run_instances` without `TagSpecifications`, then calls `create_tags` with Name, Project and ManagedBy.

**Acceptance command:** `make test`

**Commit message:**
```
Port existing modules to Python 3.4 syntax

Replace f-strings and subprocess.run with 3.4-compatible code and swap
the post-2015 TagSpecifications call for create_tags. Lint in make ci.
```

---

## T3 — Add instance catalog, right-sizing and cost estimation

**Goal:** A single `sizing` module replaces the two duplicated instance-type ladders, using a static price catalog.

**Files:**
- create `config/pricing.yml`, with a header comment: "static hand-maintained snapshot of us-east-1 Linux on-demand prices; edit to match current pricing". Contents:
  - `hours_per_month: 730`
  - instances t2.micro, t2.small, t2.medium, t2.large, m4.large, m4.xlarge, m4.2xlarge (each with vcpu, memory_gib and hourly_usd)
  - `ebs_gp2_gb_month: 0.10`
  - `rds.classes` for db.t2.micro, db.t2.small, db.t2.medium, db.m4.large and db.m4.xlarge
  - `rds.storage_gb_month: 0.115`
- create `src/sizing.py`:
  - `Catalog.from_file(path)` and `Catalog(dict)`
  - `required_capacity(workload)`, which applies `utilization` × 1.25 headroom
  - `recommend_instance(workload, catalog)`, which picks the smallest by hourly price that fits both vcpu and memory, and raises `SizingError` if nothing fits
  - `recommend_db_class(db, catalog)`, which uses `db.instance_class` if set, otherwise `db.m4.large`
  - `estimate_cost(workload, catalog)`, which returns `{'instance','storage','database','total'}` rounded to 2 dp
- modify `src/aws_connector.py`: `_get_instance_type` delegates to `sizing.recommend_instance`, and the constructor takes `catalog`.
- modify `src/migration_engine.py`: `_recommend_instance_type` and `_estimate_cost` delegate to `sizing`.
- create `tests/unit/test_sizing.py`.

**Tests to write first:**
- `test_sizing.py` covers these cases:
  - cpu 2 / mem 4 with no utilization → `t2.medium`
  - with `peak_cpu_pct 40` / `peak_mem_pct 55` → required 1.0 vCPU / 2.75 GiB → `t2.medium`
  - cpu 1 / mem 1 → `t2.micro`
  - cpu 16 → `SizingError`
  - cost for the SPEC example (t2.medium, storage 50, mysql with storage 100) = `{'instance': 37.96, 'storage': 5.0, 'database': 139.25, 'total': 182.21}`
  - a workload with no database has `database == 0.0`

**Acceptance command:** `make test`

**Commit message:**
```
Add instance catalog with right-sizing and cost estimation

Replace the duplicated instance-type ladders with one sizing module that
applies peak utilization headroom and prices from config/pricing.yml.
```

---

## T4 — Extract config validation and workload assessment

**Goal:** Config loading raises clear errors, and the assessment logic lives in its own tested module that writes to a configurable output path.

**Files:**
- create `src/config.py`:
  - `ConfigError`
  - `load(path)` → dict with defaults applied (`aws.region=us-east-1`, `aws.allowed_cidr=10.0.0.0/8`, `aws.endpoints={}`, `docker.command=docker`, `pricing_file=config/pricing.yml`, per workload `storage=0`, `ports=[]`, `containerizable=False`, `version=latest`)
  - `validate(cfg)`, which checks:
    - names are unique and match `^[a-z0-9-]+$`
    - `cpu` is an int ≥ 1
    - `memory` is a number > 0
    - `runtime` is in {python, node, java, php}
    - `type` is in {stateless, stateful, legacy}
    - `ports` is a list of ints between 1 and 65535
    - a `database` block has `engine` in {mysql, postgres}
    - `utilization` percentages are between 1 and 100
- create `src/assessment.py`, moving these functions from the engine unchanged in behaviour:
  - `discover_declared_dependencies(w)`
  - `recommend_strategy(w)`
  - `risk_score(w)`
  - `blockers(w)`

  Also add:
  - `assess(workloads, catalog, probe_factory=None)`, which uses `sizing` for the profile and cost
  - `write_results(results, path)`

  Service endpoints are normalised to `host:port`, stripping any URL scheme.
- modify `src/migration_engine.py`:
  - `_load_config` uses `config.load`, with no `sys.exit`.
  - `assess_workloads(output_path)` delegates to `assessment`.
- create `tests/fixtures/config/valid.yml`, `tests/fixtures/config/bad_cpu.yml` and `tests/fixtures/config/dup_names.yml`.
- create `tests/unit/test_config.py` and `tests/unit/test_assessment.py`.

**Tests to write first:**
- `test_config.py`:
  - `valid.yml` loads, and its defaults are present
  - `bad_cpu.yml` raises `ConfigError` whose message contains `workloads[0].cpu`
  - duplicate names raise
  - an invalid port (70000) raises
- `test_assessment.py`:
  - strategy is `replatform` when containerizable, `rehost` for legacy, and `refactor` for stateless non-containerizable
  - risk for legacy, non-containerizable, 4 services and storage 600 is 20+30+20+15+15=100, capped at 100
  - risk for the web-portal sample is 20
  - `licensed_software` gives `ready is False` plus a blocker message
  - `redis://cache.internal:6379` becomes `cache.internal:6379`
  - `write_results` writes valid JSON to a `tmpdir` path

**Acceptance command:** `make test`

**Commit message:**
```
Extract config validation and workload assessment modules

Validate migration.yml with path-specific errors and move strategy, risk,
blocker and dependency logic out of the engine into assessment.py.
```

---

## T5 — Map dependencies over SSH with paramiko

**Goal:** Live TCP listeners and peers are collected from a source host and merged into the assessment dependencies.

**Files:**
- create `src/discovery.py`:
  - `parse_netstat(text)` handles `netstat -tan` output: rows `tcp` / `tcp6` with states `LISTEN` and `ESTABLISHED`, and IPv4 plus `:::port` forms.
  - `parse_ss(text)` handles `ss -tan` output: `LISTEN` / `ESTAB` columns.
  - Both return `{'listening': sorted unique ints, 'established': sorted unique (ip, port) tuples}`. For `established`, the remote end is kept only when the local port is not a listening port (outbound).
  - `SSHProbe(host, port=22, username=None, key_file=None, password=None, timeout=10)`: `collect()` uses `paramiko.SSHClient` with `AutoAddPolicy`, runs `netstat -tan`, and falls back to `ss -tan` on a non-zero exit.
  - `merge_dependencies(declared, observed, known_endpoints)` marks each observed peer as `matched` (it updates `observed: True` on the declared dependency) or adds `{'type': 'undeclared', 'endpoint': 'ip:port', 'source': 'ssh'}`.
- modify `src/assessment.py`: when a workload has `source` and `probe_factory` is given, call it. On an SSH error, record `discovery_error` and do not fail the assessment. Add `listening_ports`.
- create `tests/fixtures/netstat_tan.txt` and `tests/fixtures/ss_tan.txt`, with realistic Debian 8 output that includes a MySQL peer on 3306 and an unknown peer on 11211.
- create `tests/support/__init__.py` and `tests/support/fake_ssh.py`, a threaded paramiko `ServerInterface` server:
  - it listens on 127.0.0.1 on a free port, with a generated RSA host key and password auth
  - it answers `exec_request` for `netstat -tan` with the fixture text and exit status 0
  - an option makes it exit 127 for `netstat`, to test the fallback
- create `tests/unit/test_discovery.py` and `tests/integration/test_ssh_discovery.py`.

**Tests to write first:**
- unit:
  - the parsers return `listening == [22, 8080]` and include `('10.0.0.30', 3306)` and `('10.0.0.40', 11211)`
  - merging against the declared `db.internal:3306`, with `known_endpoints={'10.0.0.30:3306': 'db.internal:3306'}`, marks it observed and adds one `undeclared` entry
- integration:
  - `SSHProbe('127.0.0.1', port, 'migrate', password='pw').collect()` against the fake server returns the same result
  - the fallback path uses `ss -tan` when netstat exits 127
  - a wrong password raises `paramiko.AuthenticationException`, and `assess` records `discovery_error`

**Acceptance command:** `make test`

**Commit message:**
```
Add SSH dependency discovery with paramiko

Parse netstat/ss output from source hosts into listening ports and
outbound peers, and merge them with dependencies declared in config.
```

---

## T6 — Track migration progress in an atomic JSON state file

**Goal:** Persistent per-workload status, history and resource ledger, which rollback and reporting depend on.

**Files:**
- create `src/state.py`:
  - `StateError` and the status constants.
  - `TRANSITIONS`:
    - pending→assessed|provisioning
    - assessed→provisioning
    - provisioning→provisioned|failed
    - provisioned→containerised|data_migrated|validated|failed
    - containerised→data_migrated|validated|failed
    - data_migrated→validated|failed
    - validated→completed|failed
    - failed→rolled_back
    - completed→rolled_back
    - rolled_back→provisioning
  - `MigrationState.load(path)`, which gives an empty `{'version': 1, 'workloads': {}}` if the file is missing.
  - `workload(name)`, which creates the entry with status `pending`.
  - `transition(name, status, error=None)`, which appends `{'status', 'at'}` using `datetime.utcnow().isoformat()` and raises `StateError` on an illegal move.
  - `record(workload_name, kind, **ids)`, which appends to the ledger (the workload argument is not called `name`, because ledger entries such as security groups carry a `name` field).
  - `ledger(name)`, `clear_ledger(name)` and `set(name, key, value)`.
  - `save()`, which does `json.dump` to `path + '.tmp'`, then `os.replace`, and creates parent directories.
- create `tests/unit/test_state.py`.

**Tests to write first:**
- the full happy path pending→…→completed is accepted
- pending→completed raises `StateError`
- the ledger keeps insertion order
- save then load round-trips
- `save` leaves no `.tmp` file behind
- the history timestamps are ISO strings
- the file created in `tmpdir/state/` has its parent directory created

**Acceptance command:** `make test`

**Commit message:**
```
Add migration state file with status transitions and ledger

Persist per-workload status history and created resource IDs atomically
so rollback and reporting can work from a single JSON file.
```

---

## T7 — Add moto 0.4 AWS simulators and endpoint-aware connector

**Goal:** `make test` starts moto EC2, S3 and RDS servers, and `AWSConnector` can target them through `aws.endpoints`.

**Files:**
- create `docker/moto/requirements.txt`: boto==2.38.0, Flask==0.10.1, Werkzeug==0.10.4, Jinja2==2.8, MarkupSafe==0.23, itsdangerous==0.24, httpretty==0.8.10, xmltodict==0.9.2, six==1.9.0, requests==2.7.0.
- create `docker/moto/Dockerfile`:
  - `FROM python:3.4`
  - `pip install --no-deps -r requirements.txt`
  - `ENV PYTHONHASHSEED=0`: moto builds its Flask url map from a dict, and under some hash seeds `PUT /<bucket>` is routed to the trailing-slash rule and fails with HTTP 500 (`FormDataRoutingRedirect`). Seed 0 gives the working rule order.
  - download `https://github.com/getmoto/moto/archive/967c778390a8d5991a475fa92856e627d3116b1b.tar.gz` (the commit the 0.4.14 tag points at, so a moved tag cannot change the build), check it with `sha256sum -c` against `ddf4f4c72dc614c29475f811bdb25e30b2039baa56a628af63bc0284ad433445` (computed from that tarball; GitHub archive bytes are not formally guaranteed stable, so if the check ever fails, re-verify the commit and update the hash rather than dropping the check), then `pip install --no-deps` the tarball
  - `ENTRYPOINT ["moto_server"]`
- modify `docker-compose.yml`: add these services, all built from `docker/moto`:
  - `moto-ec2` (`command: ["ec2", "-H", "0.0.0.0", "-p", "5000"]`)
  - `moto-s3` (`["s3bucket_path", "-H", "0.0.0.0", "-p", "5001"]`)
  - `moto-rds` (`["rds", "-H", "0.0.0.0", "-p", "5002"]`)

  Then add these to `app`:
  - `depends_on` all three simulators
  - env `MOTO_EC2=http://moto-ec2:5000`, `MOTO_S3=http://moto-s3:5001` and `MOTO_RDS=http://moto-rds:5002`
- modify `Makefile`: `test` runs `$(COMPOSE) up -d moto-ec2 moto-s3 moto-rds` before `run`.
- modify `src/aws_connector.py`:
  - `__init__(self, aws_cfg, catalog, session=None)` creates clients through `session.client(svc, region_name=..., endpoint_url=aws_cfg['endpoints'].get(svc))`, where `session` defaults to `boto3.session.Session()`.
  - Add an `s3` property. The S3 client is created with `config=botocore.client.Config(signature_version='s3v4')`. Without it, botocore 1.2.6 `fix_s3_host` rewrites every request for a DNS-compatible bucket (`probe`, `e2e-artifacts`, `migration-artifacts`) to `<bucket>.s3.amazonaws.com`, ignoring `endpoint_url`, and the tests would hit real AWS with fake credentials. `fix_s3_host` returns early for s3v4.
- create `tests/support/aws.py`:
  - `wait_for_port(host, port, timeout=30)`
  - `sim_config()`, which builds an `aws` config dict from the env vars
  - `client(svc)`, which builds boto3 clients for the tests, using the same `Config(signature_version='s3v4')` for S3
- create `tests/integration/conftest.py` with a session `simulators` fixture that skips, with a clear message, when `MOTO_EC2` is unset (so `make test-unit` still works outside compose) and otherwise waits for the three ports. Simulator tests opt in with `pytest.mark.usefixtures('simulators')`; the T6 SSH integration tests need no simulators and keep running.
- create `tests/integration/test_simulators.py`.

**Tests to write first:**
- `test_simulators.py` checks the simulators and the connector:
  - boto3 1.1.4 `ec2.describe_instances()` against `moto-ec2` returns a response containing `'Reservations'`. Do not assert that it is empty: moto 0.4.14 keeps state for the container's lifetime and has no reset endpoint, and other integration tests in the same session create instances. This rule applies to every integration test from here on: assert only on resources the test created (by ledger ID, tag, name or key prefix). Also, never rely on a globally unique resource name: any name a test picks (bucket, DB identifier, workload name that feeds one) must be unique per test (for example a uuid4 suffix), because a name used earlier in the session collides. Security group names are made unique by the engine itself (T8).
  - `s3.create_bucket(Bucket='probe')` then `list_buckets` contains `probe` (path-style).
  - `rds.describe_db_instances()` returns a list. If botocore cannot parse moto 0.4's RDS XML, mark this test `xfail(strict=False)` with the exact parser error as the reason, and record it in SPEC "Known limitations". Both outcomes are acceptable, but the result must be decided and written down in this task. The decision also fixes the e2e (T13) and demo (T16) configs: if RDS is `xfail`, those configs carry no `database` blocks, and database provisioning is covered only by unit tests and `tests/fixtures/config/with_database.yml`.
- unit `test_aws_connector_endpoints.py`:
  - a mock session receives `endpoint_url` per service, and `None` when there is no endpoint
  - a real `boto3.session.Session` S3 client built by `AWSConnector` has `meta.config.signature_version == 's3v4'`

**Acceptance command:** `make test`

**Commit message:**
```
Add moto 0.4 AWS simulators and endpoint-aware connector

Run moto EC2, S3 and RDS servers from a period python:3.4 image and let
AWSConnector point boto3 clients at custom endpoints.
```

---

## T8 — Provision per-workload EC2, EBS and RDS resources

**Goal:** Provisioning creates real (simulated) resources and records each one in the state ledger as it goes.

**Files:**
- modify `src/aws_connector.py`:
  - `provision(workload, state)`:
    1. Create a security group named `migration-<name>-<uuid.uuid4().hex[:8]>`, with `VpcId` only when `aws.vpc_id` is set, and tag it `Name=migration-<name>` with `create_tags`. The suffix makes each provisioning attempt's name unique: moto 0.4.14 `create_security_group` (`ec2/models.py`) raises `InvalidGroup.Duplicate` for a repeated name in the same VPC namespace, and moto state lasts the whole test session, so a fixed name would make T11, T13 and T16 (and a second `make demo`) fail after T8's test has provisioned the same workload. Record `{kind: 'security_group', id, name}` with the full generated name.
    2. Add ingress for each port on `aws.allowed_cidr`.
    3. Run the instance (`ImageId` = `workload.ami` or `aws.default_ami`, `SubnetId` only when subnets are set, type from `sizing`), then `create_tags`. Record `instance`.
    4. If `storage > 0`, create a gp2 volume and attach it at `/dev/xvdf`. The AZ is the instance's placement AZ from `describe_instances`. moto 0.4.14 reports it empty or as `'None'`, so fall back to the subnet's AZ (`describe_subnets`) when a subnet is set, else `region + 'a'`. Record the volume as `{kind: 'volume', id, instance_id, device: '/dev/xvdf'}`, because moto's `DetachVolume` returns HTTP 500 without `InstanceId` and `Device`.
    5. Return a dict of IDs.
  - `create_database(workload, state)`:
    - `DBInstanceIdentifier` = `migrated-<db name with _ replaced by ->`
    - class from `sizing.recommend_db_class`
    - `MasterUserPassword` from `os.environ[db['password_env']]`, raising `ConfigError`-style `ValueError` if it is missing
    - `StorageEncrypted=True` and `MultiAZ=db.get('multi_az', False)`
    - record `db_instance`
  - `instance_state(instance_id)` via `describe_instances`.
  - Delete helpers:
    - `terminate_instance`, `delete_volume(volume_id, instance_id, device)` (calls `detach_volume(VolumeId=, InstanceId=, Device=)` first) and `delete_security_group`
    - `delete_db_instance(identifier)`: first list `describe_db_instances()` (unfiltered) and filter client-side by `DBInstanceIdentifier`. If absent, return `False` (already deleted). Otherwise call `delete_db_instance(SkipFinalSnapshot=True)`. moto 0.4.14 RDS errors are JSON `BadRequest` bodies, so botocore cannot read `DBInstanceNotFound` from them, and a filtered describe of a missing ID fails the same way.
    - the EC2 helpers treat `InvalidInstanceID.NotFound`, `InvalidVolume.NotFound` and `InvalidGroup.NotFound` as success
  - Remove the old `check_*` stubs and `destroy_infrastructure`; T11 replaces them.
- create `tests/unit/test_aws_connector.py` and `tests/integration/test_provisioning.py`.

**Tests to write first:**
- unit, with a `MagicMock` session:
  - the call order is create_security_group → create_tags (group) → authorize ×N → run_instances → create_tags (instance) → create_volume → attach_volume
  - the security group name starts with `migration-<name>-`, ends in 8 hex chars, and differs between two `provision` calls for the same workload; the group is tagged `Name=migration-<name>`; the ledger entry carries the full `name`
  - the ledger kinds come out in that order
  - there is no volume when storage is 0
  - a missing `password_env` raises
  - a NotFound `ClientError` on delete returns `False` without raising
  - `delete_volume` calls `detach_volume` with all three of `VolumeId`, `InstanceId` and `Device`
  - the volume ledger entry carries `instance_id` and `device`
  - a placement AZ of `''` or `'None'` falls back to `us-east-1a` when there is no subnet
  - `delete_db_instance` for an identifier missing from `describe_db_instances` returns `False` and never calls `delete_db_instance`
- integration against moto-ec2: provisioning the `file-processor` sample gives:
  - `describe_instances` shows one instance with tag `Name=file-processor` and type `t2.medium`
  - `describe_volumes`, filtered client-side by the volume ID from the ledger (moto ignores `VolumeIds`), shows a 100 GiB volume attached to it
  - every lookup is by the IDs in the ledger, never by global counts
  - the security group has an ingress rule for port 9000
  - provisioning `file-processor` a second time in the same session succeeds with a different group ID and name (no `InvalidGroup.Duplicate`)
  - a finalizer rolls back the test's own resources by ledger ID with the T8 delete helpers (terminate instance, detach/delete volume, delete group), so later tests are not affected
- integration against moto-rds: `create_database` returns its ID (or `xfail` per the T7 decision).

**Acceptance command:** `make test`

**Commit message:**
```
Provision per-workload EC2, EBS and RDS resources

Create security group, tagged instance, attached gp2 volume and encrypted
RDS instance per workload, recording every ID in the state ledger.
```

---

## T9 — Migrate workload data to S3 with checksum verification

**Goal:** Files under `data_path` land in the artifacts bucket, have their MD5 proven, and are listed in a manifest.

**Files:**
- create `src/data_migration.py`:
  - `IntegrityError`.
  - `md5_file(path, chunk=1 << 20)`.
  - `DataMigrator(s3_client, bucket)`:
    - `migrate(workload, state, manifest_dir='state')`:
      - walk `data_path` in sorted order
      - reject files larger than 5 GiB
      - `put_object(Bucket, Key='<name>/<relpath>', Body=f, ContentMD5=base64(md5), ServerSideEncryption='AES256')`
      - compare the ETag (stripping quotes) with the local md5
      - record `s3_object` in the ledger
      - write the manifest JSON (SPEC format)
      - set `state.data = {'files', 'bytes', 'manifest'}`
      - raise `IntegrityError` on any mismatch
    - `verify(manifest_path)`: `head_object` for each key, then compare `ContentLength` and ETag. Returns `(ok, [problems])`.
    - `delete_objects(keys)`.
  - Ensure the bucket exists: `head_bucket`, else `create_bucket`, when `aws.artifacts_bucket` is set, defaulting to `migration-artifacts`.
- create `tests/fixtures/data/web-portal/db/portal_db.sql` and `tests/fixtures/data/web-portal/uploads/logo.txt`.
- create `tests/unit/test_data_migration.py` and `tests/integration/test_data_migration_s3.py`.

**Tests to write first:**
- unit:
  - `md5_file` of a known string matches `hashlib`
  - with a fake client that returns a wrong ETag, `migrate` raises `IntegrityError` and the manifest marks that file `mismatch`
  - the keys are `web-portal/db/portal_db.sql` and so on, in sorted order
- integration against moto-s3:
  - migrating the fixture directory gives 2 files, the byte count equal to the sum of the file sizes, and `verify` → `(True, [])`
  - after overwriting one object with different bytes directly through boto3, `verify` returns `False` and names that key

**Acceptance command:** `make test`

**Commit message:**
```
Migrate workload data to S3 with MD5 integrity checks

Upload data_path files with server-side encryption, compare ETags to
local MD5s, write a manifest and re-verify it with head_object.
```

---

## T10 — Rework Docker pipeline for a private registry

**Goal:** A correct Dockerfile generator for period base images, plus build, tag and push to a configurable registry through a testable runner.

**Files:**
- modify `src/docker_builder.py`:
  - Base images: python `python:3.4-slim`, node `node:0.12-slim`, java `java:8u45-jre`, php `php:5.6-apache`, fallback `ubuntu:14.04`. `docker.base_images` overrides them.
  - The CMD is `json.dumps(shlex.split(entrypoint))`.
  - Use `MAINTAINER migration-framework` plus a `LABEL project=`.
  - Honour both `ports` and `port` in `EXPOSE`, de-duplicated and sorted.
  - `build_image(w)` writes the Dockerfile only when none exists and runs `[cmd, 'build', '-t', 'name:version', '-f', path, ctx]`.
  - `push(w)` computes `registry/name:version` and runs `tag`, then `push`. Rename `push_to_ecr` to `push`.
  - A non-zero exit raises `DockerError` carrying stderr.
  - `analyze_image` parses `inspect` JSON.
  - Record `image` in the state through the engine (T12).
- create `tests/fixtures/dockerfiles/python.Dockerfile`, `node.Dockerfile`, `java.Dockerfile` and `php.Dockerfile` (golden files).
- create `tests/support/fake_docker.py`, an executable Python script that appends `argv` as a JSON line to `$FAKE_DOCKER_LOG`. For `inspect` it prints a canned JSON array. It exits 1 when `argv` contains `FAIL`.
- create `tests/unit/test_docker_builder.py`.

**Tests to write first:**
- each runtime matches its golden file
- `gunicorn app:app` → `CMD ["gunicorn", "app:app"]`
- a recording runner sees the exact argv sequence build → tag → push, ending in `registry.local:5000/web-portal:1.0`
- an existing Dockerfile is not overwritten
- a runner returning code 1 raises `DockerError` with its stderr
- `analyze_image` on the canned inspect output gives `{'size_mb': 120.5, 'layers': 5, 'os': 'linux', 'arch': 'amd64'}`
- one test runs the real `CommandRunner` against `tests/support/fake_docker.py` and checks the log file

**Acceptance command:** `make test`

**Commit message:**
```
Rework Docker pipeline for period images and private registry

Fix exec-form CMD generation, use 2015 base image tags, drop ECR naming
and run build/tag/push through an injectable command runner.
```

---

## T11 — Add post-migration validation and ledger-driven rollback

**Goal:** Real health checks replace the stubs that always returned `True`, and a failed or explicitly rolled-back workload leaves nothing behind.

**Files:**
- create `src/validation.py`: `Validator(aws, data_migrator)`. `validate(workload, state)` → `(ok, failures)` and checks:
  - every ledger `instance` is `running` (moto instances report `running` or `pending`; poll `describe_instances` up to `timeout` seconds)
  - `tcp_check(host, port, timeout)` via `socket.create_connection`, when `validation` is set
  - `data_migrator.verify(manifest)`, when the workload has data
- create `src/rollback.py`: `run(name, state, aws, s3)` walks the ledger in reverse, as follows:
  - `s3_object` → delete
  - `db_instance` → `delete_db_instance`
  - `volume` → `delete_volume(id, instance_id, device)` (detach with all three parameters, then delete)
  - `instance` → terminate and wait for `terminated` (bounded poll)
  - `security_group` → delete

  It collects the deleted IDs, clears the ledger, transitions to `rolled_back` and returns the list. Individual delete errors other than NotFound are collected, and it raises `RollbackError` at the end, leaving the undeleted entries in the ledger.
- create `tests/unit/test_validation.py`, `tests/unit/test_rollback.py` and `tests/integration/test_rollback_moto.py`.

**Tests to write first:**
- unit:
  - `tcp_check` is `True` against a listening socket fixture and `False` on a closed port within 1 s
  - `validate` reports `instance i-x state stopped` as a failure
  - with a fake `aws`, rollback deletes in the order s3_object, db_instance, volume, instance, security_group
  - running it a second time is a no-op
  - a non-NotFound error leaves that entry in the ledger and raises `RollbackError`
- integration against moto:
  - provision `file-processor` and migrate a fixture file, then run rollback
  - afterwards, looking up each ID from the pre-rollback ledger: the instance state is `terminated`, the volume ID is absent from `describe_volumes` (filtered client-side), the security group ID is gone, and the bucket has 0 keys under `file-processor/`

**Acceptance command:** `make test`

**Commit message:**
```
Add post-migration validation and ledger-driven rollback

Check instance state, TCP reachability and data manifests, and undo a
workload by deleting its recorded resources in reverse order.
```

---

## T12 — Orchestrate workloads through the state machine

**Goal:** `MigrationEngine` drives each workload through the state transitions with automatic rollback, tested with mocked collaborators only.

**Files:**
- modify `src/migration_engine.py`:
  - `MigrationEngine(config_path, state_path, tfstate_path=None, session=None, runner=None)` builds the catalog, `AWSConnector`, `DockerBuilder`, `DataMigrator` and `Validator`. Each collaborator can be injected for tests.
  - `assess_workloads(output_path)` also moves every `pending` workload to `assessed` in the state file and leaves other statuses unchanged.
  - `execute_migration(only=None)` runs, per workload:
    1. if the status is `completed`, print `<name>: already completed, use --rollback first` to stderr, mark the run as failed for the exit code, and skip the workload without touching state
    2. transition to provisioning
    3. provision (plus `create_database` if there is a database)
    4. transition to provisioned
    5. if containerizable: build and push, record `image`, then transition to containerised
    6. if `data_path`: migrate data, then transition to data_migrated
    7. validate, then transition to validated
    8. transition to completed
  - On an exception or a validation failure, it transitions to `failed` with `error=str(e)`, runs `rollback.run`, and continues with the next workload. It saves state after every transition.
  - `rollback_workload(name)`: if the status is in progress (`provisioning`, `provisioned`, `containerised`, `data_migrated` or `validated`, left behind by a crashed run), first transition to `failed` with `error="interrupted run"`, then run `rollback.run`.
- create `tests/unit/test_engine.py`.

**Tests to write first** (all with `MagicMock` collaborators, no simulators):
- the happy path calls provision → build/push → migrate → validate in order, and the final status is `completed` with the history in order
- a non-containerizable workload with no `data_path` goes provisioned → validated
- a provision exception gives `failed` → `rolled_back`, calls `rollback.run` once, and the next workload still runs
- a validation failure `(False, [...])` rolls back the workload
- `--migrate` on a `completed` workload prints the "already completed, use --rollback first" message, leaves state unchanged and reports failure
- `rollback_workload` on a workload stuck in `provisioning` ends in `rolled_back` with `failed` in its history
- `assess_workloads` moves `pending` to `assessed` and leaves a `completed` workload alone

**Acceptance command:** `make test`

**Commit message:**
```
Orchestrate workloads through the migration state machine

Run provision, containerise, data copy and validation per workload,
roll back on failure and recover workloads left mid-run by a crash.
```

---

## T13 — Wire the CLI, sample config and end-to-end run

**Goal:** `--assess`, `--migrate [--workload]` and `--rollback NAME` work from the command line with the exit codes from the SPEC, and a full run passes against the simulators.

**Files:**
- modify `src/migration_engine.py`: `main(argv=None)` returns an exit code:
  - a mutually exclusive group of `--assess`, `--migrate` and `--rollback` (`--report` joins the group in T16, together with `report.py`)
  - 2 on `ConfigError`, 1 if any workload is failed or rolled_back after `--migrate` (or was skipped as already completed), and 0 otherwise
  - the `if __name__` block calls `sys.exit(main())`
- modify `config/migration.yml`:
  - Keep the three workloads.
  - The registry becomes `registry.local:5000`.
  - Remove the placeholder `vpc-xxxxxxxx` and subnet IDs, leaving them as empty values.
  - Service endpoints change to `host:port` form.
  - Add `utilization` to web-portal, `data_path: data/web-portal` and a `validation` block (commented example).
  - Add `password_env: PORTAL_DB_PASSWORD` to web-portal's database and `password_env: REPORTS_DB_PASSWORD` to reporting-engine's.
  - Add `pricing_file`.
  - Remove `entrypoint` from non-python workloads.
- modify `docker-compose.yml`: add `PORTAL_DB_PASSWORD=demo` and `REPORTS_DB_PASSWORD=demo` to the `app` environment.
- create `tests/fixtures/e2e/migration.yml`:
  - endpoints come from the moto services, `docker.command: tests/support/fake_docker.py`, and `artifacts_bucket: e2e-artifacts`
  - `web-portal` has `data_path` = the fixtures data dir and a `validation` block pointing at a port the test opens
  - `broken-app` has `validation` pointing at a closed port, to force a failure
  - `database` blocks only if T7 recorded that moto-rds works; otherwise none
- create `tests/fixtures/config/with_database.yml`, a small valid config whose workload has a `database` block with `password_env`, used by the unit tests for config validation and `create_database` whatever the T7 outcome
- create `tests/unit/test_cli.py` and `tests/integration/test_end_to_end.py`.

**Tests to write first:**
- unit:
  - a missing config gives exit code 2 with a message on stderr
  - `--assess` and `--migrate` together cause an argparse error
  - `--assess` on `valid.yml` writes the output file and prints `Assessment complete: N/M workloads ready`
  - `--migrate` with a state file where the workload is `completed` returns 1 and prints the "already completed" message
- integration: `main(['--config', e2e, '--state', tmp, '--migrate'])` returns 1, and then, looking up only the IDs in each workload's ledger:
  - `web-portal` is `completed`, with its image recorded, 2 files, and its instance running in moto
  - `broken-app` is `rolled_back`, has an empty ledger, and the instance ID recorded before rollback is `terminated`
  - `--rollback web-portal` then returns 0 and terminates that instance
  - the fake docker log shows the push to `registry.local:5000/web-portal:1.0`

**Acceptance command:** `make test`

**Commit message:**
```
Wire migration CLI with exit codes and end-to-end test

Expose assess, migrate and rollback on the command line, update the
sample config for a private registry and run a full migration against
the simulators.
```

---

## T14 — Rewrite Terraform landing zone in 0.6 syntax

**Goal:** A Terraform config that Terraform 0.6.3 parses and that passes the 0.6.3 provider schema validation, with SSE enforced through a bucket policy and no post-period resources.

Why `graph` is not enough: in 0.6.3, `command/graph.go` builds the graph with `Validate: false`, and there is no `validate` command. The only offline way to run the provider schema check is `terraform plan`, which calls `ctx.Validate()` before Input, Refresh and provider configuration. So this task runs `plan` with fake credentials in a container with no network and checks only that no configuration error is reported. The credential failure that follows is expected.

**Files:**
- modify `terraform/main.tf`:
  - Write it in 0.6 HCL:
    - `provider "aws" { region = "${var.aws_region}" }`
    - `aws_vpc` with `tags { Name = "migration-vpc" Project = "cloud-migration" }`
    - `aws_subnet.public` / `aws_subnet.private` with `count = 2`, `cidr_block = "${element(split(",", var.public_subnet_cidrs), count.index)}"` and `availability_zone = "${element(split(",", var.availability_zones), count.index)}"`
    - `aws_internet_gateway`
    - `aws_route_table.public` with a `route { cidr_block = "0.0.0.0/0" gateway_id = "${aws_internet_gateway.main.id}" }`
    - `aws_route_table_association.public` with count 2
    - `aws_security_group.app` with `name = "migration-app"` (the original `name_prefix` is not in the 0.6.3 schema), keeping the three ingress rules (80 and 443 from `10.0.0.0/8`, 22 from `${var.admin_cidr}`) and the egress rule
    - `aws_db_subnet_group` with `name`, a `description` (Required in 0.6.3) and `subnet_ids = ["${aws_subnet.private.*.id}"]`, and no `tags` (not in the 0.6.3 schema)
    - `aws_s3_bucket.artifacts` with `acl = "private"` and a `policy` heredoc that denies `s3:PutObject` when `s3:x-amz-server-side-encryption` is not `AES256`
  - Remove `aws_nat_gateway`, `aws_eip`, `versioning`, `server_side_encryption_configuration` and `cidrsubnet`.
- create `terraform/outputs.tf`: `vpc_id`, `public_subnet_ids` / `private_subnet_ids` (`"${join(",", aws_subnet.private.*.id)}"`), `app_security_group_id`, `db_subnet_group_name` and `artifacts_bucket`.
- modify `terraform/variables.tf`: keep the four existing variables and add `public_subnet_cidrs` (`"10.100.0.0/24,10.100.1.0/24"`), `private_subnet_cidrs` (`"10.100.10.0/24,10.100.11.0/24"`) and `availability_zones` (`"us-east-1a,us-east-1b"`).
- modify `docker-compose.yml`: add a `tf-plan` service built from `.` with `platform: linux/amd64`, `network_mode: none`, `working_dir: /app/terraform`, env `AWS_ACCESS_KEY_ID=testing`, `AWS_SECRET_ACCESS_KEY=testing`, and `entrypoint: ["sh", "/app/scripts/tf_plan_check.sh"]`. The entrypoint must be overridden (not just `command`): T1's Dockerfile sets `ENTRYPOINT ["python", "src/migration_engine.py"]`, which from `/app/terraform` would exit 2 before the script ever runs.
- create `scripts/tf_plan_check.sh`: runs `terraform plan -input=false -refresh=false 2>&1 | tee /tmp/plan.out`, then fails if `/tmp/plan.out` contains `There are warnings and/or errors related to your configuration` or `Required variable not set`, and otherwise exits 0 whatever `plan` itself returned.
- modify `Makefile`: `tf-check` = `cd terraform && terraform graph . > /dev/null`, and `ci` also runs `tf-check`. `test` runs `$(COMPOSE) run --rm --no-deps tf-plan` after `make ci` and before `down`.
- create `tests/unit/test_terraform_config.py`.

**Tests to write first:**
- `test_terraform_config.py` parses each `.tf` file with `hcl.load`, then checks:
  - the resource types are exactly {aws_vpc, aws_subnet, aws_internet_gateway, aws_route_table, aws_route_table_association, aws_security_group, aws_db_subnet_group, aws_s3_bucket}
  - the bucket policy JSON (loaded with `json.loads`) has a Deny statement on `s3:PutObject` with `StringNotEquals` `s3:x-amz-server-side-encryption: AES256`
  - `aws_security_group.app` has `name` and no `name_prefix`
  - `aws_db_subnet_group` has a non-empty `description` and no `tags`
  - ingress rules: pyhcl 0.1.11 merges repeated blocks and keeps only the last `ingress`, so it cannot see all three. A small block scanner in the test (brace-matching over the raw `main.tf` text, inside `resource "aws_security_group" "app"`) extracts every `ingress { ... }` body and asserts exactly three: port 80 and 443 with `cidr_blocks = ["10.0.0.0/8"]`, and port 22 with `cidr_blocks = ["${var.admin_cidr}"]`
  - no file contains `[*]`, `cidrsubnet`, `nat_gateway` or `tags = {`
  - the outputs file defines all six outputs
  - `subprocess.check_call(['terraform', 'graph', '.'], cwd='terraform')` exits 0
- red check for the plan step: before fixing `name_prefix`, `description` and the DB subnet group `tags`, `$(COMPOSE) run --rm --no-deps tf-plan` fails with the configuration-error message; after the fixes it passes. In both runs the output must start with `terraform plan`'s own output (the `tee` of `/tmp/plan.out`), which shows the `sh /app/scripts/tf_plan_check.sh` entrypoint ran; a `python: can't open file 'src/migration_engine.py'` error means the entrypoint override is missing

**Acceptance command:** `make test`

**Commit message:**
```
Rewrite Terraform landing zone in 0.6 HCL

Replace 0.12-era syntax and post-2015 resources with 0.6.3-compatible
config, fix provider schema errors, add a public route table, enforce
SSE via bucket policy and check the schema with an offline plan.
```

---

## T15 — Import Terraform outputs into migration config

**Goal:** `--tfstate` fills the empty network settings from Terraform's state outputs.

**Files:**
- create `src/tfstate.py`:
  - `read_outputs(path)` handles state `version: 1`: it finds the module whose `path == ["root"]` and returns its `outputs` dict of strings. It raises `ValueError` for any other version or when the root module is missing.
  - `merge_outputs(cfg, outputs)` fills these only when they are empty in the config:
    - `aws.vpc_id` ← `vpc_id`
    - `aws.subnet_ids` ← `private_subnet_ids.split(',')`
    - `aws.security_group_id` ← `app_security_group_id`
    - `aws.artifacts_bucket` ← `artifacts_bucket`
- modify `src/migration_engine.py`: apply `merge_outputs` after `config.load` when `--tfstate` is given.
- modify `src/aws_connector.py`: when `aws.security_group_id` is set, add it to `SecurityGroupIds` next to the per-workload group.
- create `tests/fixtures/terraform.tfstate`, a hand-written Terraform 0.6 v1 state with root outputs matching `outputs.tf`.
- create `tests/unit/test_tfstate.py`.

**Tests to write first:**
- the fixture outputs parse
- `private_subnet_ids` becomes a 2-element list
- a pre-set `vpc_id` in config is not overwritten
- `version: 3` raises `ValueError`
- the CLI `--assess --tfstate fixture` succeeds
- a mock EC2 client sees both security group IDs in `run_instances`

**Acceptance command:** `make test`

**Commit message:**
```
Import Terraform state outputs into migration config

Read root module outputs from a Terraform 0.6 state file and use them to
fill empty VPC, subnet, security group and bucket settings.
```

---

## T16 — Add progress report and demo target

**Goal:** `--report` summarises the state file, and `make demo` shows the whole flow against the simulators.

**Files:**
- create `src/report.py`: `render(state, assessment=None, fmt='text')`.
  - Text output is a fixed-width table with the columns WORKLOAD, STATUS, STRATEGY, INSTANCE, EST $/MO, RESOURCES, FILES, BYTES and ELAPSED. ELAPSED is the time from the first to the last history timestamp, as `H:MM:SS`.
  - It ends with a totals line: workload count by status, summed monthly cost and summed bytes.
  - `json` output is `{'workloads': [...], 'totals': {...}}`.
  - `assessment` is optional. When it is missing, the strategy and cost columns show `-`.
- modify `src/migration_engine.py`: add `--report` to the mutually exclusive group, with `--format text|json`. It reads the state and, if present, `--output` (the assessment JSON), and prints the result.
- create `config/demo.yml`, a copy of the sample config with simulator endpoints, `docker.command: tests/support/fake_docker.py` and `data_path: tests/fixtures/data/web-portal`. The `database` blocks (with `password_env`, matched by the compose env from T13) stay only if T7 recorded that moto-rds works; otherwise they are removed and a comment points to `tests/fixtures/config/with_database.yml`.
- modify `Makefile`: `demo` runs these in the app container against the running simulators, with `STATE := state/demo-$(shell date +%s).json` (a simply expanded variable, so all three steps share one path) so each run starts fresh:
  1. `python src/migration_engine.py --config config/demo.yml --state $(STATE) --assess`
  2. `-python src/migration_engine.py ... --state $(STATE) --migrate` (the `-` prefix lets make continue if a workload fails and the exit code is 1)
  3. `python src/migration_engine.py ... --state $(STATE) --report`
- create `tests/unit/test_report.py`.

**Tests to write first:**
- with a hand-built state (one completed and one rolled_back workload) plus an assessment:
  - the text output contains the header, both names and `TOTAL 2 workloads (1 completed, 1 rolled_back)`
  - the elapsed time for a 65-second history is `0:01:05`
  - `json.loads` of the JSON output gives totals bytes = 2048 and monthly cost = the sum of the two `estimated_cost.total` values
  - without an assessment, the strategy shows `-`
- unit `test_cli.py`: `--report` and `--migrate` together cause an argparse error
- an integration smoke test runs `make demo`-equivalent CLI calls through `main()` with `config/demo.yml` and a fresh `tmpdir` state path. `--migrate` returns 0 or 1, and `--report` returns 0 and lists every workload.

**Acceptance command:** `make test && make demo`

**Commit message:**
```
Add migration progress report and simulator demo

Render per-workload status, cost, resources and data totals as text or
JSON, and add make demo to run assess, migrate and report end to end.
```

---

## T17 — Regenerate README from template

**Goal:** An honest README built from `tools/readme_template.md`.

**Files:**
- modify `README.md`: fill in the template.
  - **Title:** "Cloud Migration Framework".
  - **Description:** one concrete paragraph.
  - **Stack line:** "Personal project built on the 2015-era stack (Python 3.4, boto3 1.1.4, paramiko 1.15.2, Terraform 0.6.3)".
  - **Implemented:** one bullet per SPEC in-scope feature. Each bullet names its module and test file. The `--tfstate` import is listed under a separate "Added in the rebuild" note, since it is not in the original README.
  - **Not implemented / known limitations:** every row of SPEC "Out of scope" and "Known limitations", plus the T7 RDS decision. It also carries these explicit mock statements:
    - "AWS EC2, S3 and RDS are simulated with moto 0.4.14 servers; nothing has been run against a real AWS account."
    - "Source hosts are simulated by an in-process paramiko SSH server in tests."
    - "Docker build/push is exercised only through a recording fake docker command; no real registry is used."
    - "Terraform is checked with `terraform graph`, pyhcl/raw-text tests and an offline `terraform plan` that stops at configuration validation; it has never been planned against AWS or applied."
    - "moto 0.4.14 RDS ignores StorageEncrypted, so RDS encryption is shown only by unit tests."
  - **Built with:** Python 3.4 with the pinned library list from `requirements.txt`, and Terraform 0.6.3.
  - **Running it:** `docker compose up -d moto-ec2 moto-s3 moto-rds`, with `make demo` as the smoke command.
  - **Tests:** `make test`, plus one sentence on coverage: unit tests plus integration against moto and a fake SSH server; not real AWS or Docker.
  - **Layout:** generated with `git ls-files | python3 -c` (a tree printer) after staging, so every listed file exists.
  - Delete the template's rules comment. No badges, no employer, no dates, no metrics.
- create `tests/unit/test_readme.py`.

**Tests to write first:**
- `test_readme.py` checks these things:
  - every path in the README's Layout block exists on disk
  - the README contains `moto` and `simulated`
  - the README contains none of `ACORIA`, `Status-Complete`, `99.9`, `60%`, `35%` or `Developed during`
  - the README contains none of the template's `{{` placeholders

**Acceptance command:** `make test`

**Commit message:**
```
Regenerate README with honest status and generated layout

Describe implemented features, simulated AWS/SSH/Docker integrations and
known limitations, with the file tree taken from git ls-files.
```
