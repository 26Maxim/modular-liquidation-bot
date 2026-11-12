.PHONY: dev test test-contracts test-unit test-integration test-e2e lint typecheck verify docker-build run

PYTHON ?= python3
PYTEST ?= pytest
RUFF ?= ruff
MYPY ?= mypy
FORGE ?= forge

lint:
	$(RUFF) check backend/src/ tests/

typecheck:
	$(MYPY) backend/src/liquidator

test-contracts:
	cd contracts/evm && $(FORGE) test -vv

test-unit:
	$(PYTEST) tests/unit -v

test-integration:
	$(PYTEST) tests/integration -v

test-e2e:
	$(PYTEST) tests/e2e -v

test: test-contracts test-unit test-integration test-e2e

verify: lint typecheck test

run:
	uvicorn liquidator.app:create_app --factory --host $${UVICORN_HOST:-0.0.0.0} --port $${PORT:-8000}
