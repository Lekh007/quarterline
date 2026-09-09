"""Integration test: alembic migrations against a temporary SQLite database."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import create_engine, inspect

from quarterline.cli import run_db_upgrade

EXPECTED_TABLES = {
    "companies",
    "source_artifacts",
    "fact_observations",
    "normalized_facts",
    "fact_lineage",
    "derived_metrics",
    "documents",
    "sections",
    "chunks",
    "embeddings",
    "prices",
    "runs",
    "run_events",
    "brief_cache",
    "agent_runs",
    "agent_tool_calls",
    "approval_requests",
    "eval_runs",
    "eval_results",
}


def test_upgrade_head_creates_all_tables(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "migrations-test.db"
    assert not db_path.exists()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")

    run_db_upgrade(f"sqlite:///{db_path.as_posix()}")

    assert db_path.exists()
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    tables = {row[0] for row in rows}
    assert EXPECTED_TABLES <= tables
    assert "alembic_version" in tables

    # Idempotent: second upgrade is a no-op at head.
    run_db_upgrade(f"sqlite:///{db_path.as_posix()}")


def test_migrated_schema_enforces_foreign_keys(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "fk-test.db"
    url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    run_db_upgrade(url)

    inspector = inspect(create_engine(url))
    fk_columns = {
        column
        for fk in inspector.get_foreign_keys("fact_observations")
        for column in fk["constrained_columns"]
    }
    assert {"company_id"} <= fk_columns
