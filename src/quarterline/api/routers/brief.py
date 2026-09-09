"""POST /api/c/{ticker}/brief — grounded brief generation (SPEC §19).

The route accepts a JSON body ``{"period_end": "YYYY-MM-DD"?}`` (an
urlencoded HTMX form with the same field also works) and returns either the
rendered :template:`partials/brief.html` partial (HTMX / browser requests) or
JSON. Questions use POST, never query strings (SPEC §19).

Degraded modes are explicit (SPEC §25): when the generation provider is
unavailable the response shows the fact card and the retrieved evidence with
a banner — never generated prose. Unknown tickers/periods return 404; the
generation module is imported lazily so the deterministic routes keep working
with Ollama stopped (and the F4 LLM-blocked tests keep passing).
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from quarterline.api.routers.companies import _parse_period_end, render_error

router = APIRouter()

API_DIR = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(API_DIR / "templates"))

_BANNER_DEGRADED = "generation unavailable — showing facts and evidence, not generated prose"


def _wants_html(request: Request) -> bool:
    if request.headers.get("HX-Request", "").lower() == "true":
        return True
    accept = request.headers.get("accept", "")
    return "text/html" in accept and "application/json" not in accept.split(",")[0]


async def _parse_body(request: Request) -> tuple[dict, str | None]:
    """JSON body preferred; urlencoded form accepted (HTMX default)."""
    content_type = request.headers.get("content-type", "")
    raw = await request.body()
    if not raw:
        return {}, None
    if "application/json" in content_type:
        try:
            payload = json.loads(raw)
        except ValueError:
            return {}, "request body is not valid JSON"
        if not isinstance(payload, dict):
            return {}, "request body must be a JSON object"
        return payload, None
    if "application/x-www-form-urlencoded" in content_type or "=" in raw.decode("utf-8", "ignore"):
        from urllib.parse import parse_qs

        parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
        flat = {key: values[0] for key, values in parsed.items() if values}
        return flat, None
    return {}, f"unsupported content-type {content_type!r}"


@router.post("/api/c/{ticker}/brief")
async def generate_company_brief(request: Request, ticker: str):
    from quarterline.llm.generation import generate_brief  # lazy: deterministic
    # routes never import the LLM stack (SPEC §25 graceful degradation).

    payload, parse_error = await _parse_body(request)
    if parse_error:
        return _error(request, 400, parse_error)
    unknown = sorted(set(payload) - {"period_end"})
    if unknown:
        return _error(request, 400, f"unknown field(s): {', '.join(unknown)}")

    raw_period = payload.get("period_end")
    wanted = _parse_period_end(raw_period)
    if raw_period and wanted is None:
        return _error(request, 400, f"invalid period_end {raw_period!r} (use YYYY-MM-DD)")

    try:
        outcome = generate_brief(ticker, wanted)
    except LookupError as exc:
        return _error(request, 404, str(exc))

    if _wants_html(request):
        return templates.TemplateResponse(
            request=request,
            name="partials/brief.html",
            context={
                "outcome": outcome,
                "degraded_banner": _BANNER_DEGRADED
                if outcome.status == "provider_unavailable"
                else None,
                "disclaimer": "Research and education only. Not investment advice.",
            },
        )
    return JSONResponse(content=outcome.model_dump(mode="json"))


def _error(request: Request, status: int, detail: str):
    if _wants_html(request):
        return render_error(request, status, detail)
    return JSONResponse(status_code=status, content={"error": detail})


__all__ = ["router"]
