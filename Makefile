export DOCKER_DEFAULT_PLATFORM := linux/amd64
COMPOSE ?= docker compose

.PHONY: build test test-unit ci lint tf-check demo shell down

build:
	$(COMPOSE) build

test: build
	$(COMPOSE) up -d moto-ec2 moto-s3 moto-rds && $(COMPOSE) run --rm app make ci && $(COMPOSE) run --rm --no-deps tf-plan; status=$$?; $(COMPOSE) down -v; exit $$status

test-unit:
	py.test -v tests/unit

ci:
	flake8 src tests && py.test -v tests && $(MAKE) tf-check

lint:
	flake8 src tests

tf-check:
	cd terraform && terraform graph . > /dev/null

STATE := state/demo-$(shell date +%s).json
DEMO = $(COMPOSE) run --rm app python src/migration_engine.py --config config/demo.yml --state $(STATE)

demo:
	$(COMPOSE) up -d moto-ec2 moto-s3 moto-rds
	$(DEMO) --assess
	-$(DEMO) --migrate
	$(DEMO) --report

shell:
	$(COMPOSE) run --rm app bash

down:
	$(COMPOSE) down
