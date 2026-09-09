"""GET /health (SPEC §19).

- ``database``: SELECT 1 via the configured engine -> ok | fail.
- ``generation_provider`` / ``embedding_provider``: Ollama ``GET {base}/api/tags``
  with a <= 0.5 s timeout -> ok | unavailable, cached for 30 s per app.
- HTTP 503 only when the database fails; model outage alone yields 200 + degraded.
"""

from __future__ import annotations

import time

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from quarterline.config import get_settings
from quarterline.store.db import session_scope

router = APIRouter()

PROVIDER_CACHE_TTL_SECONDS = 30.0
PROVIDER_TIMEOUT_SECONDS = 0.5


def _check_ollama(base_url: str) -> str:
    """Return 'ok' when the Ollama tags endpoint answers, else 'unavailable'."""
    url = f"{base_url.rstrip('/')}/api/tags"
    try:
        response = httpx.get(url, timeout=PROVIDER_TIMEOUT_SECONDS)
    except httpx.HTTPError:
        return "unavailable"
    return "ok" if response.status_code == 200 else "unavailable"


def _provider_statuses(request: Request) -> dict[str, str]:
    cache: dict | None = getattr(request.app.state, "provider_health_cache", None)
    now = time.monotonic()
    if cache is not None and (now - cache["at"]) < PROVIDER_CACHE_TTL_SECONDS:
        return cache["statuses"]
    settings = get_settings()
    statuses = {
        "generation_provider": _check_ollama(settings.ollama_base_url),
        "embedding_provider": _check_ollama(settings.ollama_base_url),
    }
    request.app.state.provider_health_cache = {"at": now, "statuses": statuses}
    return statuses


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    database = "ok"
    try:
        with session_scope() as session:
            session.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - any DB failure must surface as 503, never crash the route
        database = "fail"

    providers = _provider_statuses(request)
    payload = {"status": "degraded", "database": database, **providers}
    if database == "ok" and all(value == "ok" for value in providers.values()):
        payload["status"] = "ok"

    if database == "fail":
        return JSONResponse(status_code=503, content=payload)
    return JSONResponse(status_code=200, content=payload)
