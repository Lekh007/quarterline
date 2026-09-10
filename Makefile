# Quarterline — local commands (SPEC §29).
# Windows note: use Git Bash / MSYS make, or run the underlying commands directly.

.PHONY: install db-init ingest ingest-facts ingest-docs embed serve test test-postgres \
        eval eval-retrieval eval-generation fmt lint

install:
	uv sync --extra dev

db-init:
	uv run quarterline db upgrade

ingest: ingest-facts ingest-docs

ingest-facts:
	@echo "ingest facts is not implemented until wave 1 (F1 financial normalization)"

ingest-docs:
	@echo "ingest documents is not implemented until wave 1 (F2 document pipeline)"

embed:
	@echo "index build / embedding is not implemented until wave 2 (F3 retrieval)"

serve:
	uv run quarterline serve

test:
	uv run pytest -q

# PostgreSQL 16 + pgvector profile (SPEC §15/§29).
# Boots the compose database (or reuses one already running), runs the pgvector
# contract tests, then stops the service. Safe to run twice: tests recreate the
# schema and the named volume persists across runs.
# If Docker is unavailable the pytest run still executes and the tests SKIP
# (fast TCP probe) instead of failing.
POSTGRES_TEST_URL ?= postgresql://quarterline:quarterline@127.0.0.1:5432/quarterline

test-postgres:
	@docker compose up -d quarterline-db 2>/dev/null \
		|| echo "docker compose unavailable/failed - continuing (tests will skip if PostgreSQL is unreachable)"
	@until docker compose exec -T quarterline-db pg_isready -U quarterline -d quarterline >/dev/null 2>&1; do \
		sleep 1; \
	done 2>/dev/null || true
	QUARTERLINE_TEST_POSTGRES_URL=$(POSTGRES_TEST_URL) uv run pytest -q tests/integration/test_postgres_profile.py
	@docker compose stop quarterline-db 2>/dev/null || true

eval: eval-retrieval eval-generation

eval-retrieval:
	@echo "eval retrieval is not implemented until wave 3 (F6 evaluation)"

eval-generation:
	@echo "eval generation is not implemented until wave 3 (F6 evaluation)"

fmt:
	uv run ruff format .
	uv run ruff check --fix .

lint:
	uv run ruff check .
	uv run ruff format --check .
