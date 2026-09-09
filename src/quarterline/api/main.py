"""FastAPI application factory (SPEC §19).

Wave 0 shipped the shell; wave 2 registers the deterministic routers
(companies/watchlist, screener, filings/evidence). The placeholder index
route moved to ``routers.companies`` (real watchlist table). LLM routes
(brief, ask, memo, runs, dashboard) arrive in later waves.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from quarterline.api.routers import (
    ask,
    brief,
    companies,
    dashboard,
    filings,
    health,
    runs,
    screener,
)

API_DIR = Path(__file__).resolve().parent

#: research-only disclaimer shown in the base template footer of every page.
#: Exported for the generation wave (memo exports embed the same text).
DISCLAIMER = "Research and education only. Not investment advice."

#: CSP allows self + inline for now (SPEC §2.3.6); wave F4 tightens this.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' "
    "'unsafe-inline'; img-src 'self' data:"
)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Quarterline",
        description="Local-first public-company research desk.",
        version="0.1.0",
    )
    # Per-app provider-health cache used by the /health router (30 s TTL).
    app.state.provider_health_cache: dict | None = None

    app.mount("/static", StaticFiles(directory=str(API_DIR / "static")), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        return response

    app.include_router(health.router)
    app.include_router(companies.router)
    app.include_router(screener.router)
    app.include_router(filings.router)

    # --- wave-3 router registration (F5 replaces this line) ---
    app.include_router(brief.router)
    app.include_router(ask.router)
    app.include_router(runs.router)
    # --- wave-3 router registration (F6 replaces this line) ---
    app.include_router(dashboard.router)

    return app
