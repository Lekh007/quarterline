"""Unit tests for observability: C12 events (JSONL + DB persistence),
sanitation, metrics aggregation, and traces (SPEC §24)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from quarterline.observability.events import emit_run_event, hash_text, new_run_id
from quarterline.observability.metrics import latest_eval_summary, metrics_summary
from quarterline.observability.tracing import recent_runs, run_trace
from quarterline.store.db import get_engine
from quarterline.store.models import Base, Run, RunEvent


@pytest.fixture
def db_session(offline_env: Path):
    Base.metadata.create_all(get_engine())
    factory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    session = factory()
    yield session
    session.close()


def test_jsonl_mirror_signature_kept(offline_env: Path) -> None:
    """The W0 stub test's contract: same signature, same JSONL behavior."""
    run_id = new_run_id()
    emit_run_event(run_id, {"event": "test_start", "endpoint": "/health"})
    lines = (offline_env / "logs" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[-1])
    assert record["run_id"] == run_id
    assert record["endpoint"] == "/health"
    assert "timestamp" in record


def test_event_persists_runs_and_run_events(db_session) -> None:
    run_id = new_run_id()
    emit_run_event(run_id, {"endpoint": "search", "answer_status": "ok"})
    emit_run_event(
        run_id, {"endpoint": "search", "answer_status": "error", "error_type": "provider_down"}
    )

    run = db_session.query(Run).filter(Run.run_id == run_id).one()
    assert run.endpoint == "search"
    assert run.status == "error"  # error status escalates and sticks
    assert run.error_type == "provider_down"
    assert run.started_at is not None and run.finished_at is not None

    events = (
        db_session.query(RunEvent)
        .filter(RunEvent.run_id == run_id)
        .order_by(RunEvent.id.asc())
        .all()
    )
    assert len(events) == 2
    payload = json.loads(events[0].event_json)
    assert payload["endpoint"] == "search"


def test_run_row_upserted_once_per_run(db_session) -> None:
    run_id = new_run_id()
    for index in range(3):
        emit_run_event(run_id, {"endpoint": "search", "sequence": index})
    count = db_session.query(Run).filter(Run.run_id == run_id).count()
    assert count == 1
    assert db_session.query(RunEvent).filter(RunEvent.run_id == run_id).count() == 3


def test_question_text_never_stored_hash_and_length_only(db_session, offline_env) -> None:
    secret = "What was Apple's secret internal revenue projection for June 2026?"
    run_id = new_run_id()
    emit_run_event(run_id, {"endpoint": "ask", "question": secret})

    # Database side.
    event = db_session.query(RunEvent).filter(RunEvent.run_id == run_id).one()
    assert secret not in (event.event_json or "")
    payload = json.loads(event.event_json)
    assert payload["question_hash"] == hash_text(secret)
    assert payload["question_length"] == len(secret)

    # JSONL mirror side.
    lines = (offline_env / "logs" / "events.jsonl").read_text(encoding="utf-8")
    assert secret not in lines
    assert hash_text(secret) in lines


def test_secret_looking_keys_are_redacted(db_session) -> None:
    run_id = new_run_id()
    emit_run_event(
        run_id,
        {"endpoint": "ask", "api_key": "sk-super-secret", "prompt": "do things"},
    )
    event = db_session.query(RunEvent).filter(RunEvent.run_id == run_id).one()
    payload = json.loads(event.event_json)
    assert payload["api_key"] == "[redacted]"
    assert payload["prompt"] == "[redacted]"


def test_emit_survives_missing_schema(tmp_path: Path, monkeypatch) -> None:
    """Telemetry must never break the caller — an un-migrated DB stays quiet."""
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'fresh.db').as_posix()}")
    from quarterline.store.db import reset_db_caches

    reset_db_caches()
    engine = create_engine(f"sqlite:///{(tmp_path / 'fresh.db').as_posix()}")
    # No create_all: tables do not exist.
    engine.connect()  # ensures the file DB exists but stays schema-less
    emit_run_event(new_run_id(), {"endpoint": "search"})  # must not raise
    assert (tmp_path / "storage" / "logs" / "events.jsonl").is_file()
    engine.dispose()


def test_metrics_summary_from_seeded_events(db_session) -> None:
    # Three runs with distinct outcomes.
    emit_run_event(
        new_run_id(),
        {
            "endpoint": "search",
            "provider": "fake",
            "model": "fake-embed",
            "retrieval_latency_ms": 10.0,
            "cache_hit": False,
            "answer_status": "ok",
        },
    )
    emit_run_event(
        new_run_id(),
        {
            "endpoint": "search",
            "provider": "fake",
            "model": "fake-embed",
            "retrieval_latency_ms": 30.0,
            "cache_hit": True,
            "answer_status": "insufficient_evidence",
        },
    )
    emit_run_event(
        new_run_id(),
        {
            "endpoint": "eval:generation",
            "provider": "scripted-fake",
            "model": "scripted-fake",
            "generation_latency_ms": 5.0,
            "json_valid": True,
            "json_repaired": True,
            "numeric_error": False,
            "retained_bullet_count": 3,
            "dropped_bullet_count": 1,
            "answer_status": "refused",
            "cache_hit": False,
        },
    )

    summary = metrics_summary(db_session)
    assert summary["total_runs"] == 3
    assert summary["total_events"] == 3
    assert summary["endpoints"] == {"search": 2, "eval:generation": 1}
    retrieval = summary["latency_ms"]["retrieval_latency_ms"]
    assert retrieval["n"] == 2 and retrieval["p50_ms"] == 10.0 and retrieval["p95_ms"] == 30.0
    assert summary["provider_model_breakdown"]["fake/fake-embed"] == 2
    assert summary["cache_hit"]["n"] == 3 and summary["cache_hit"]["count"] == 1
    assert summary["json"]["repaired"]["count"] == 1
    assert summary["bullet_retention"]["retained"] == 3
    assert summary["bullet_retention"]["rate"] == pytest.approx(0.75)
    statuses = summary["answer_status"]
    assert statuses["n"] == 3
    assert statuses["insufficient_evidence"]["count"] == 1
    assert statuses["refusal"]["count"] == 1
    assert "sample size" in summary["note"].lower() or "noisy" in summary["note"]


def test_metrics_summary_empty_store_is_graceful(db_session) -> None:
    summary = metrics_summary(db_session)
    assert summary["total_runs"] == 0
    assert summary["cache_hit"]["rate"] is None
    assert summary["latency_ms"]["total_latency_ms"]["p50_ms"] is None


def test_tracing_returns_ordered_timeline(db_session) -> None:
    run_id = new_run_id()
    emit_run_event(run_id, {"endpoint": "ask", "step": "retrieve"})
    emit_run_event(run_id, {"endpoint": "ask", "step": "generate"})
    emit_run_event(run_id, {"endpoint": "ask", "step": "validate"})

    trace = run_trace(db_session, run_id)
    assert trace is not None
    assert trace["run"]["endpoint"] == "ask"
    steps = [event["step"] for event in trace["events"]]
    assert steps == ["retrieve", "generate", "validate"]

    runs = recent_runs(db_session, limit=5)
    assert runs[0]["run_id"] == run_id


def test_run_trace_unknown_id_returns_none(db_session) -> None:
    assert run_trace(db_session, "nonexistent") is None


def test_latest_eval_summary(db_session) -> None:
    from datetime import UTC, datetime

    from quarterline.store.models import EvalRun

    assert latest_eval_summary(db_session) is None
    db_session.add(
        EvalRun(
            dataset_version="abc123",
            chunking_strategy="section",
            retrieval_config="hybrid",
            status="complete",
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
    )
    db_session.commit()
    summary = latest_eval_summary(db_session)
    assert summary is not None
    assert summary["retrieval_config"] == "hybrid"
