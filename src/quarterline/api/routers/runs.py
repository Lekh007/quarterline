"""Runs API (SPEC §24): recent runs and per-run event timelines.

- ``GET /api/runs`` — the most recent runs (newest first, bounded).
- ``GET /api/runs/{run_id}`` — one run plus its ordered event timeline.

Event payloads never contain raw question text or secrets (see
``observability.events``); they carry the question hash + length instead.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from quarterline.observability.tracing import recent_runs, run_trace
from quarterline.store.db import session_scope

router = APIRouter(prefix="/api", tags=["runs"])

DEFAULT_RUN_LIMIT = 50


@router.get("/runs")
async def list_runs(limit: int = DEFAULT_RUN_LIMIT) -> JSONResponse:
    bounded = max(1, min(limit, 200))
    with session_scope() as session:
        runs = recent_runs(session, limit=bounded)
    return JSONResponse(status_code=200, content={"runs": runs, "count": len(runs)})


@router.get("/runs/{run_id}")
async def get_run(run_id: str) -> JSONResponse:
    with session_scope() as session:
        trace = run_trace(session, run_id)
    if trace is None:
        return JSONResponse(status_code=404, content={"detail": "run not found"})
    return JSONResponse(status_code=200, content=trace)
