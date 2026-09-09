"""FastAPI application factory (SPEC §19).

Wave 0 ships the shell: security headers, static mount, Jinja2 templates, the
health router, and a placeholder index page. Later waves add routers for
companies, filings, screener, brief, ask, memo, runs, and dashboard.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from quarterline.api.routers import health

API_DIR = Path(__file__).resolve().parent

#: research-only disclaimer shown in the base template footer of every page.
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

    templates = Jinja2Templates(directory=str(API_DIR / "templates"))
    app.mount("/static", StaticFiles(directory=str(API_DIR / "static")), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        return response

    app.include_router(health.router)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"title": "Quarterline", "disclaimer": DISCLAIMER},
        )

    return app
