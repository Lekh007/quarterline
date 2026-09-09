"""Observability aggregations for the dashboard and runs API (SPEC §24).

Everything here is computed *in Python* from the persisted ``run_events``
payloads (and the ``runs`` rows for sample counts) — no SQL aggregation
extensions are assumed, so the SQLite and PostgreSQL profiles behave
identically.

Every rate/percentile is reported together with its sample size ``n``: low-N
numbers are noisy and the dashboard must show that (SPEC §24 "sample sizes").

Metrics (SPEC §24):
- request counts, per-endpoint counts
- P50/P95 per latency kind, computed from stored ``*_latency_ms`` durations
- provider/model breakdown
- cache-hit rate
- JSON repair rate (invalid before repair but valid after)
- numeric-rejection rate
- bullet retention (retained / (retained + dropped))
- insufficient-evidence rate, refusal rate, provider-failure rate
- latest evaluation summary pointer (from ``eval_runs``; see report.py)
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from quarterline.store.models import EvalRun, Run, RunEvent

#: Latency fields aggregated as P50/P95 (milliseconds; stored durations).
LATENCY_FIELDS = (
    "retrieval_latency_ms",
    "rerank_latency_ms",
    "generation_latency_ms",
    "validation_latency_ms",
    "total_latency_ms",
)


def percentile(sorted_values: list[float], p: float) -> float | None:
    """Nearest-rank percentile of an already-sorted list; None when empty."""
    if not sorted_values:
        return None
    rank = max(1, math.ceil((p / 100.0) * len(sorted_values)))
    return sorted_values[min(rank, len(sorted_values)) - 1]


def _event_field(event: dict, key: str) -> Any:
    value = event.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return None


def _load_events(session: Session) -> list[dict]:
    rows = session.execute(select(RunEvent.id, RunEvent.run_id, RunEvent.event_json)).all()
    events: list[dict] = []
    for row_id, run_id, payload in rows:
        try:
            parsed = json.loads(payload) if payload else {}
        except ValueError:
            parsed = {}
        parsed.setdefault("run_id", run_id)
        events.append({"_id": row_id, **parsed})
    return events


def _rate(numerator: int, denominator: int) -> dict:
    return {
        "n": denominator,
        "count": numerator,
        "rate": round(numerator / denominator, 4) if denominator else None,
    }


def metrics_summary(session: Session) -> dict:
    """Aggregate the persisted events into a dashboard-ready dict.

    Never raises on an empty store: every block carries its sample size and
    an empty store yields counts of zero with ``null`` rates.
    """
    run_count = session.execute(select(func.count(distinct(Run.run_id)))).scalar_one()
    events = _load_events(session)

    endpoints: dict[str, int] = {}
    for event in events:
        endpoint = event.get("endpoint") or "unknown"
        endpoints[str(endpoint)] = endpoints.get(str(endpoint), 0) + 1

    latencies: dict[str, dict] = {}
    for field in LATENCY_FIELDS:
        values = sorted(
            float(v) for event in events if (v := _event_field(event, field)) is not None and v >= 0
        )
        latencies[field] = {
            "n": len(values),
            "p50_ms": percentile(values, 50),
            "p95_ms": percentile(values, 95),
        }

    providers: dict[str, int] = {}
    for event in events:
        provider = event.get("provider")
        model = event.get("model")
        if provider is None and model is None:
            continue
        key = f"{provider or 'unknown'}/{model or 'unknown'}"
        providers[key] = providers.get(key, 0) + 1

    cache_events = [event for event in events if isinstance(event.get("cache_hit"), bool)]
    cache_hits = sum(1 for event in cache_events if event["cache_hit"])

    # JSON validity / repair: an event carries json_valid_* flags when the
    # pipeline validated generated JSON.
    json_checked = [event for event in events if isinstance(event.get("json_valid"), bool)]
    json_valid_count = sum(1 for event in json_checked if event["json_valid"])
    repaired_events = [event for event in events if isinstance(event.get("json_repaired"), bool)]
    repaired_count = sum(1 for event in repaired_events if event["json_repaired"])

    numeric_checked = [event for event in events if isinstance(event.get("numeric_error"), bool)]
    numeric_rejected = sum(1 for event in numeric_checked if event["numeric_error"])

    retained = sum(
        int(v) for event in events if isinstance((v := event.get("retained_bullet_count")), int)
    )
    dropped = sum(
        int(v) for event in events if isinstance((v := event.get("dropped_bullet_count")), int)
    )

    answer_events = [event for event in events if isinstance(event.get("answer_status"), str)]
    insufficient = sum(
        1 for event in answer_events if event["answer_status"] == "insufficient_evidence"
    )
    refused = sum(1 for event in answer_events if event["answer_status"] == "refused")
    provider_failure = sum(
        1 for event in answer_events if event["answer_status"] == "provider_unavailable"
    ) + sum(
        1 for event in events if isinstance(event.get("error_type"), str) and event["error_type"]
    )

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "total_runs": int(run_count),
        "total_events": len(events),
        "endpoints": endpoints,
        "latency_ms": latencies,
        "provider_model_breakdown": providers,
        "cache_hit": _rate(cache_hits, len(cache_events)),
        "json": {
            "valid": _rate(json_valid_count, len(json_checked)),
            "repaired": _rate(repaired_count, len(repaired_events)),
        },
        "numeric_rejection": _rate(numeric_rejected, len(numeric_checked)),
        "bullet_retention": {
            "retained": retained,
            "dropped": dropped,
            "rate": round(retained / (retained + dropped), 4) if retained + dropped else None,
        },
        "answer_status": {
            "n": len(answer_events),
            "insufficient_evidence": _rate(insufficient, len(answer_events)),
            "refusal": _rate(refused, len(answer_events)),
            "provider_failure": _rate(provider_failure, len(events)),
        },
        "note": (
            "All rates are shown with their sample size n. Low-n numbers are "
            "noisy and must not be read as quality guarantees (SPEC §24)."
        ),
    }


def latest_eval_summary(session: Session) -> dict | None:
    """Most recent eval run (any kind) for the dashboard's eval block."""
    row = session.execute(select(EvalRun).order_by(EvalRun.id.desc()).limit(1)).scalar_one_or_none()
    if row is None:
        return None
    return {
        "eval_run_id": row.id,
        "dataset_version": row.dataset_version,
        "chunking_strategy": row.chunking_strategy,
        "retrieval_config": row.retrieval_config,
        "status": row.status,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "notes": row.notes,
    }


__all__ = [
    "LATENCY_FIELDS",
    "latest_eval_summary",
    "metrics_summary",
    "percentile",
]
