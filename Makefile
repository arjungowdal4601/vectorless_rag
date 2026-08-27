UV ?= uv

.PHONY: setup test lint run

setup:
	$(UV) sync --locked --all-groups

test:
	$(UV) run --locked pytest

lint:
	$(UV) run --locked ruff format --check .
	$(UV) run --locked ruff check .
	$(UV) run --locked mypy src tests

run:
	$(UV) run --locked --env-file .env document-processing
