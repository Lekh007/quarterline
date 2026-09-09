"""Observability dashboard (SPEC §24): GET /dashboard.

Renders the metrics aggregates (with sample sizes — low-N numbers are noisy),
the latest evaluation summary, and the most recent runs with links into the
runs API. Graceful empty state when nothing has been recorded yet.

Note for the orchestrator: ``base.html`` is not owned by F6; its "Dashboard"
nav link still says "Arrives in a later release". F6 exposes the nav entry
inside dashboard.html only (in-page section navigation).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from quarterline.observability.metrics import latest_eval_summary, metrics_summary
from quarterline.observability.tracing import recent_runs, run_trace
from quarterline.store.db import session_scope

API_DIR = Path(__file__).resolve().parents[1]

router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory=str(API_DIR / "templates"))

RECENT_RUNS_LIMIT = 10


def dashboard_context(session: Session) -> dict:
    summary = metrics_summary(session)
    eval_summary = latest_eval_summary(session)
    runs = recent_runs(session, limit=RECENT_RUNS_LIMIT)
    latest_trace = run_trace(session, runs[0]["run_id"]) if runs else None
    empty = summary["total_runs"] == 0
    latest_report = None
    try:
        from quarterline.eval.report import latest_report as load_latest_report

        latest_report = load_latest_report()
    except Exception:  # noqa: BLE001 - report parse issues must not break the page
        latest_report = None
    return {
        "summary": summary,
        "eval_summary": eval_summary,
        "latest_report": latest_report,
        "runs": runs,
        "latest_trace": latest_trace,
        "empty": empty,
        "disclaimer": (
            "Sample sizes are shown next to every rate. Low-n numbers are noisy "
            "and are not quality guarantees. All data is local; no user question "
            "text is stored (hash + length only)."
        ),
    }


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    with session_scope() as session:
        context = dashboard_context(session)
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context=context,
    )
