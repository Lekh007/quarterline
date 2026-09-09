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

test-postgres:
	@echo "PostgreSQL + pgvector profile is not implemented until wave 5 (F8 integration)"

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
