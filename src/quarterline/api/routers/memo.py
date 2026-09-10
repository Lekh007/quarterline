"""Agent memo API (SPEC §19/§20):

- ``POST /api/memos`` — start one synchronous bounded memo run; the response
  carries the run result (status ``awaiting_approval`` + approval_request_id
  when a validated memo survived the gate).
- ``GET /api/memos/{run_id}`` — the run result from its checkpoint (JSON, or
  the memo_review.html page for browsers).
- ``POST /api/memos/{run_id}/approve-export`` — approve (hash + expiry
  validated) and export; HTTP 409 for unapproved/invalid/expired/tampered
  requests.

The workflow module is imported LAZILY so the deterministic routes keep
working without the LLM stack (SPEC §25); tests inject scripted providers at
the graph layer.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from quarterline.api.routers.companies import render_error

router = APIRouter(prefix="/api", tags=["memos"])

API_DIR = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(API_DIR / "templates"))

_CITATION_PREFIX = "cite-"  # context keys for pre-split citation parts


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


@router.post("/memos")
async def create_memo_run(request: Request) -> JSONResponse:
    from quarterline.agent.graph import run_memo_workflow  # lazy: LLM stack

    payload, parse_error = await _parse_body(request)
    if parse_error:
        return _error(request, 400, parse_error)
    unknown = sorted(
        set(payload) - {"ticker", "memo_type", "question", "topic", "period_end", "market_context"}
    )
    if unknown:
        return _error(request, 400, f"unknown field(s): {', '.join(unknown)}")
    if not payload.get("ticker"):
        return _error(request, 400, "ticker is required")
    if payload.get("memo_type") not in ("quarter_review", "risk_review"):
        return _error(request, 400, "memo_type must be quarter_review or risk_review")
    if payload.get("period_end"):
        from quarterline.api.routers.companies import _parse_period_end

        parsed_period = _parse_period_end(payload.get("period_end"))
        if parsed_period is None:
            return _error(
                request, 400, f"invalid period_end {payload.get('period_end')!r} (use YYYY-MM-DD)"
            )
        payload["period_end"] = parsed_period.isoformat()
    if payload.get("market_context") not in (None, "true", "false", True, False):
        return _error(request, 400, "market_context must be a boolean")

    result = run_memo_workflow(payload)
    if _wants_html(request):
        return _render_review(request, result)
    return JSONResponse(status_code=200, content=result.model_dump(mode="json"))


@router.get("/memos/{run_id}")
async def get_memo_run(run_id: str, request: Request):
    from quarterline.agent.checkpoints import load_result  # lazy
    from quarterline.store.db import session_scope

    with session_scope() as session:
        result = load_result(session, run_id)
    if result is None:
        return _error(request, 404, f"no agent run {run_id!r}")
    if _wants_html(request):
        return _render_review(request, result)
    return JSONResponse(status_code=200, content=result.model_dump(mode="json"))


@router.post("/memos/{run_id}/approve-export")
async def approve_and_export(run_id: str, request: Request):
    from quarterline.agent.checkpoints import load_state  # lazy
    from quarterline.agent.graph import export_approved_memo  # lazy
    from quarterline.store.db import session_scope

    payload, parse_error = await _parse_body(request)
    if parse_error:
        return _error(request, 400, parse_error)
    unknown = sorted(set(payload) - {"approval_request_id", "export_type", "memo_content"})
    if unknown:
        return _error(request, 400, f"unknown field(s): {', '.join(unknown)}")
    export_type = payload.get("export_type") or "md"
    if export_type not in ("md", "json"):
        return _error(request, 400, "export_type must be 'md' or 'json'")
    raw_request_id = payload.get("approval_request_id")
    try:
        request_id = int(raw_request_id)
    except (TypeError, ValueError):
        return _error(request, 400, "approval_request_id must be an integer")

    with session_scope() as session:
        state = load_state(session, run_id)
        if state is None:
            return _error(request, 404, f"no agent run {run_id!r}")
        if state.get("status") != "awaiting_approval":
            return _error(
                request,
                409,
                f"run status is {state.get('status')!r}; export requires "
                "awaiting_approval with a validated memo",
            )
        result, decision = export_approved_memo(
            run_id,
            request_id,
            payload.get("memo_content") or state.get("memo_content") or "",
            export_type,
            session=session,
        )
    if not decision.approved:
        return _error(request, 409, f"export not approved: {decision.reason}")
    assert result is not None
    if _wants_html(request):
        return _render_review(request, result, exported=True)
    return JSONResponse(
        status_code=200,
        content={
            "status": "exported",
            "exported": result.exported,
            "run": result.model_dump(mode="json"),
        },
    )


# ---------------------------------------------------------------------------
# Rendering (HTML-escaped: Jinja autoescape + pre-split citation links)
# ---------------------------------------------------------------------------


def _citation_parts(text: str) -> list[dict]:
    """Split memo text into plain segments and citation references.

    Plain text is rendered by Jinja with autoescape ON; only the fixed-form
    ``[ev-xxxxxxxxxxxx]`` tokens become links (the id grammar is enforced by
    the regex, so nothing from the memo text can become markup).
    """
    import re

    parts: list[dict] = []
    pattern = re.compile(r"\[(ev-[0-9a-f]{12})\]")
    position = 0
    for match in pattern.finditer(text or ""):
        if match.start() > position:
            parts.append({"text": text[position : match.start()], "evidence_id": None})
        parts.append({"text": match.group(1), "evidence_id": match.group(1)})
        position = match.end()
    if position < len(text or ""):
        parts.append({"text": text[position:], "evidence_id": None})
    return parts


def _review_context(result, *, exported: bool = False) -> dict:
    sections = []
    if result.memo is not None:
        for number, section in enumerate(result.memo.sections):
            sections.append(
                {
                    "heading": section.heading,
                    "parts": _citation_parts(section.text),
                    "evidence_ids": section.evidence_ids,
                    "key": f"{_CITATION_PREFIX}{number}",
                }
            )
    return {
        "result": result,
        "sections": sections,
        "exported": exported,
        "disclaimer": "Research and education only. Not investment advice.",
    }


def _render_review(request: Request, result, *, exported: bool = False):
    return templates.TemplateResponse(
        request=request,
        name="memo_review.html",
        context=_review_context(result, exported=exported),
    )


def _error(request: Request, status: int, detail: str):
    if _wants_html(request):
        return render_error(request, status, detail)
    return JSONResponse(status_code=status, content={"error": detail})


__all__ = ["router"]
