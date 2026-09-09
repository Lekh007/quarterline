"""Run traces for the runs API (SPEC §24).

:func:`run_trace` assembles one run's record plus its ordered event timeline
from the ``runs``/``run_events`` tables. Event payloads are stored JSON (see
``observability.events``); raw user question text is never present because
``events.emit_run_event`` sanitizes before persisting.
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from quarterline.store.models import Run, RunEvent


def run_trace(session: Session, run_id: str) -> dict | None:
    """Return ``{"run": ..., "events": [...]}`` for ``run_id`` or None.

    Events are ordered by insertion (``RunEvent.id``), which is the true
    timeline order even when two events share a timestamp.
    """
    run = session.execute(select(Run).where(Run.run_id == run_id)).scalar_one_or_none()
    if run is None:
        return None

    events: list[dict] = []
    rows = session.execute(
        select(RunEvent.id, RunEvent.created_at, RunEvent.event_json)
        .where(RunEvent.run_id == run_id)
        .order_by(RunEvent.id.asc())
    ).all()
    for row_id, created_at, payload in rows:
        try:
            parsed = json.loads(payload) if payload else {}
        except ValueError:
            parsed = {"event_json_unparseable": True}
        parsed["event_row_id"] = row_id
        parsed["stored_at"] = created_at.isoformat() if created_at else None
        events.append(parsed)

    return {
        "run": {
            "run_id": run.run_id,
            "endpoint": run.endpoint,
            "status": run.status,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "error_type": run.error_type,
            "params": json.loads(run.params_json) if run.params_json else {},
        },
        "events": events,
    }


def recent_runs(session: Session, limit: int = 50) -> list[dict]:
    """Most recent runs first (newest ``started_at``/id), bounded by ``limit``."""
    rows = session.execute(select(Run).order_by(Run.id.desc()).limit(limit)).scalars().all()
    return [
        {
            "run_id": run.run_id,
            "endpoint": run.endpoint,
            "status": run.status,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "error_type": run.error_type,
        }
        for run in rows
    ]


__all__ = ["recent_runs", "run_trace"]
