export DOCKER_DEFAULT_PLATFORM := linux/amd64
COMPOSE ?= docker compose

.PHONY: build test test-unit ci lint tf-check demo shell down

build:
	$(COMPOSE) build

test: build
	$(COMPOSE) run --rm app make ci; status=$$?; $(COMPOSE) down -v; exit $$status

test-unit:
	py.test -v tests/unit

ci:
	flake8 src tests && py.test -v tests

lint:
	flake8 src tests

tf-check:
	cd terraform && terraform graph

demo:
	@echo "demo is not available yet"

shell:
	$(COMPOSE) run --rm app bash

down:
	$(COMPOSE) down
