"""GET /metrics — Prometheus scrape endpoint (SPEC §24 aggregates).

Exposes exactly what ``/dashboard`` renders, in Prometheus exposition format,
so the same honesty rules apply: rates ship with their sample sizes and an
unmeasured rate is absent rather than zero.

Local-only surface: this carries no authentication, in line with the rest of
the single-user local deployment (SPEC §28 — authn/authz is out of scope).
Bind it to loopback, or put it behind your ingress, before exposing it.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from quarterline.observability.metrics import metrics_summary
from quarterline.observability.prometheus import render_prometheus
from quarterline.store.db import session_scope

router = APIRouter(tags=["metrics"])

#: Prometheus text exposition format 0.0.4.
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics() -> PlainTextResponse:
    with session_scope() as session:
        summary = metrics_summary(session)
    return PlainTextResponse(render_prometheus(summary), media_type=CONTENT_TYPE)
