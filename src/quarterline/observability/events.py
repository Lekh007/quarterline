"""Structured telemetry events (contract C12; SPEC §24).

Public surface (unchanged from the W0 stub):

- :func:`new_run_id` — a fresh run id (uuid4 hex).
- :func:`emit_run_event(run_id, event)` — record one structured event.

Wave F6 backend: every event is (1) mirrored to
``{STORAGE_DIR}/logs/events.jsonl`` exactly as before, and (2) persisted to
the ``runs``/``run_events`` tables (SPEC §9.9): the first event for a run
upserts the ``runs`` row, every event appends a ``run_events`` row with the
full JSON payload. The database write is best-effort — telemetry must never
break the request it observes; the JSONL mirror is always written.

Privacy (SPEC §24): raw user question text never reaches logs or the
database. ``question``/``query`` values are replaced by a SHA-256 hash prefix
plus the text length (see :func:`hash_text`); secret-looking keys are
redacted. API keys must never be passed to this module in the first place.

Cost honesty (SPEC §24): ``estimated_api_cost`` stays ``null`` unless a
credible estimator produced it; local inference is never labelled ``free``
(its compute cost is simply not estimated here). Cost context fields:
``estimated_api_cost``, ``cost_currency``, ``cost_basis``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from quarterline.config import get_settings

_LOGGER = logging.getLogger(__name__)

_WRITE_LOCK = threading.Lock()

#: Keys whose string values are replaced by a hash + length (never stored raw).
HASHED_KEYS = frozenset({"question", "query"})

#: Keys whose values are redacted outright (defence in depth; keys should
#: never be passed in, but if a caller does, the value does not reach storage).
REDACTED_KEYS = frozenset(
    {"prompt", "messages", "api_key", "openrouter_api_key", "authorization", "edgar_identity"}
)


def new_run_id() -> str:
    """Return a fresh run id (uuid4 hex)."""
    return uuid.uuid4().hex


def hash_text(text: str) -> str:
    """First 16 hex chars of the SHA-256 of ``text`` (question-text privacy).

    Hashes allow grouping identical questions without storing the text itself
    (SPEC §24: exclude unnecessary user content from logs).
    """
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def sanitize_event(event: dict) -> dict:
    """Return a copy of ``event`` safe for storage.

    ``question``/``query`` -> ``question_hash`` + ``question_length``;
    secret-looking keys -> ``"[redacted]"``. Everything else passes through
    unchanged (including an explicitly supplied ``question_hash``).
    """
    out: dict = {}
    for key, value in event.items():
        if key in HASHED_KEYS:
            text = value if isinstance(value, str) else str(value)
            out.setdefault("question_hash", hash_text(text))
            out["question_length"] = len(text)
        elif key in REDACTED_KEYS:
            out[key] = "[redacted]"
        else:
            out[key] = value
    return out


def _jsonl_mirror(record: dict) -> None:
    settings = get_settings()
    log_dir = Path(settings.storage_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, default=str, ensure_ascii=False)
    with _WRITE_LOCK, (log_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _derive_status(event: dict) -> str:
    status = event.get("status")
    if isinstance(status, str) and status:
        return status
    return "error" if event.get("error_type") else "ok"


#: Status escalation order for multi-event runs.
STATUS_RANK = {"ok": 0, "degraded": 1, "error": 2}


def _persist_to_db(run_id: str, record: dict, event: dict) -> None:
    """Upsert the ``runs`` row and append a ``run_events`` row (SPEC §9.9).

    Best-effort: any failure (schema missing, DB unavailable) is swallowed —
    the JSONL mirror has already been written. First event for a run inserts
    the runs row (``started_at``); later events update ``finished_at`` and
    roll the status forward (``ok`` < ``degraded`` < ``error``).
    """
    from sqlalchemy import select

    from quarterline.store.db import get_engine
    from quarterline.store.models import Run, RunEvent

    now = datetime.now(UTC)
    payload_json = json.dumps(record, default=str, ensure_ascii=False)
    endpoint = event.get("endpoint")
    error_type = event.get("error_type")
    params = {
        key: value
        for key, value in event.items()
        if key not in ("timestamp", "run_id") and isinstance(value, (str, int, float, bool))
    }

    with get_engine().begin() as conn:
        row = conn.execute(select(Run).where(Run.run_id == run_id)).fetchone()
        if row is None:
            conn.execute(
                Run.__table__.insert().values(
                    run_id=run_id,
                    endpoint=endpoint if isinstance(endpoint, str) else None,
                    status=_derive_status(event),
                    started_at=now,
                    finished_at=now,
                    params_json=json.dumps(params, default=str, ensure_ascii=False),
                    error_type=error_type if isinstance(error_type, str) else None,
                )
            )
        else:
            new_status = _derive_status(event)
            if row.status == "error":
                new_status = "error"
            elif STATUS_RANK.get(row.status, 0) > STATUS_RANK.get(new_status, 0):
                new_status = row.status
            conn.execute(
                Run.__table__.update()
                .where(Run.run_id == run_id)
                .values(
                    finished_at=now,
                    status=new_status,
                    endpoint=endpoint if isinstance(endpoint, str) else row.endpoint,
                    error_type=error_type if isinstance(error_type, str) else row.error_type,
                )
            )
        conn.execute(
            RunEvent.__table__.insert().values(
                run_id=run_id,
                created_at=now,
                event_json=payload_json,
            )
        )


def emit_run_event(run_id: str, event: dict) -> None:
    """Record one structured telemetry event for ``run_id``.

    Writes the JSONL mirror (timestamp + run_id + sanitized event) and, when
    the database is reachable, persists to the ``runs``/``run_events`` tables.
    Never raises: observability must not break the observed request.
    """
    safe = sanitize_event(event)
    record: dict[str, object] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "run_id": run_id,
    }
    record.update(safe)
    try:
        _jsonl_mirror(record)
    except Exception as exc:  # noqa: BLE001 - telemetry must never raise
        _LOGGER.debug("event JSONL mirror failed for run %s: %s", run_id, exc)
    try:
        _persist_to_db(run_id, record, safe)
    except Exception as exc:  # noqa: BLE001 - best-effort DB write
        _LOGGER.debug("event DB persistence failed for run %s: %s", run_id, exc)


__all__ = [
    "HASHED_KEYS",
    "REDACTED_KEYS",
    "emit_run_event",
    "hash_text",
    "new_run_id",
    "sanitize_event",
]
