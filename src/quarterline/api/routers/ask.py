"""POST /api/ask — grounded question answering (SPEC §19).

Questions travel in a JSON POST body ``{"question": str, "ticker": str?,
"mode": "general"|"brief"|"risk"}`` — never URL query strings (SPEC §19).
Advice-seeking questions receive the research-only refusal (status
``refused``) without any provider call. The generation module is imported
lazily so the deterministic routes keep working with Ollama stopped.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

router = APIRouter()

API_DIR = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(API_DIR / "templates"))


class AskRequest(BaseModel):
    """Question payload (extra fields rejected)."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)
    ticker: str | None = None
    mode: Literal["general", "brief", "risk"] = "general"

    @field_validator("ticker")
    @classmethod
    def _upper_ticker(cls, value: str | None) -> str | None:
        return value.upper() if value else value


def _wants_html(request: Request) -> bool:
    if request.headers.get("HX-Request", "").lower() == "true":
        return True
    accept = request.headers.get("accept", "")
    return "text/html" in accept and "application/json" not in accept.split(",")[0]


async def _parse_body(request: Request) -> tuple[dict, str | None]:
    content_type = request.headers.get("content-type", "")
    raw = await request.body()
    if not raw:
        return {}, 'empty request body; send JSON {"question": ...}'
    if "application/json" in content_type:
        try:
            payload = json.loads(raw)
        except ValueError:
            return {}, "request body is not valid JSON"
        if not isinstance(payload, dict):
            return {}, "request body must be a JSON object"
        return payload, None
    return {}, f"unsupported content-type {content_type!r}; questions use JSON POST bodies"


@router.post("/api/ask")
async def ask(request: Request):
    from quarterline.llm.generation import answer_question  # lazy: deterministic
    # routes never import the LLM stack (SPEC §25 graceful degradation).

    payload, parse_error = await _parse_body(request)
    if parse_error:
        return _error(request, 400, parse_error)
    try:
        validated = AskRequest.model_validate(payload)
    except ValidationError as exc:  # schema violations -> 400 with detail
        return _error(request, 400, f"invalid question payload: {exc}")

    outcome = answer_question(validated.question, validated.ticker, mode=validated.mode)

    if _wants_html(request):
        return templates.TemplateResponse(
            request=request,
            name="partials/ask.html",
            context={
                "question": validated.question,
                "outcome": outcome,
                "disclaimer": "Research and education only. Not investment advice.",
            },
        )
    return JSONResponse(content=outcome.model_dump(mode="json"))


def _error(request: Request, status: int, detail: str):
    if _wants_html(request):
        from quarterline.api.routers.companies import render_error

        return render_error(request, status, detail)
    return JSONResponse(status_code=status, content={"error": detail})


__all__ = ["AskRequest", "router"]
