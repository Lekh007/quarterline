"""The four typed agent tools (SPEC §20 Tools) and the allowlist registry.

The planner may ONLY select names from :data:`TOOL_REGISTRY` (this module's
single allowlist). Unknown names are rejected BEFORE invocation and repeated
calls are budget-checked BEFORE execution; every call — including rejected
attempts — is written to the ``agent_tool_calls`` audit trail
(:mod:`quarterline.store.repositories.runs`).

1. ``get_company_facts`` — SQL-backed via FactsRepo/build_fact_card; TYPED
   (ticker, period_end) arguments only; it can never receive or run a SQL
   string (SPEC §2.3.5: no raw SQL past the repo layer).
2. ``search_filings`` — the hybrid SearchService; returns bounded evidence.
3. ``get_prices`` — informational market context only; ANY failure becomes a
   graceful ``price_unavailable`` result that does not block a facts-only memo
   (SPEC §20/§25).
4. ``export_memo`` — writes approved Markdown/JSON under
   ``{STORAGE_DIR}/exports/`` with a SERVER-GENERATED filename; requires a
   valid approval (see approval.py) verified again inside the handler.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from quarterline.core.provenance import build_fact_card
from quarterline.store.repositories.runs import RunsRepo

#: Run ids are uuid4 hex (observability.events.new_run_id); anything carrying
#: path separators, dots or other characters is rejected before any filename
#: is ever built (path-traversal proof, SPEC §26).
RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")

#: Export base directory name, always under STORAGE_DIR.
EXPORTS_DIRNAME = "exports"


# ---------------------------------------------------------------------------
# Typed tool arguments (no strings that could carry SQL or paths)
# ---------------------------------------------------------------------------


class _StrictArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GetCompanyFactsArgs(_StrictArgs):
    """Typed company/period arguments — there is no SQL-shaped field at all."""

    ticker: str = Field(min_length=1, max_length=16)
    period_end: date | None = None


class SearchFilingsArgs(_StrictArgs):
    """Typed retrieval arguments (mode presets + optional period filter)."""

    query: str = Field(min_length=1, max_length=2000)
    ticker: str = Field(min_length=1, max_length=16)
    mode: Literal["general", "brief", "risk"] = "general"
    period_end: date | None = None
    top_k: int = Field(default=6, ge=1, le=12)


class GetPricesArgs(_StrictArgs):
    """Informational market-context request; ``period_end`` anchors the window."""

    ticker: str = Field(min_length=1, max_length=16)
    period_end: date | None = None


class ExportMemoArgs(_StrictArgs):
    """Export request; the FILENAME is never an argument — the server builds
    ``memo-{run_id}.{md|json}`` from a validated run id."""

    run_id: str = Field(min_length=32, max_length=32)
    export_type: Literal["md", "json"]


# ---------------------------------------------------------------------------
# Tool context + outcomes
# ---------------------------------------------------------------------------


@dataclass
class ToolContext:
    """Everything a tool handler may touch (nothing else is reachable)."""

    session: Session
    run_id: str
    run_row_id: int | None = None
    #: Budget limits (SPEC §20): each read tool once; N calls total incl. export.
    max_tool_calls: int = 4
    #: Executed tool names so far (the read-once rule).
    tools_executed: list[str] = field(default_factory=list)
    #: Tool calls consumed so far (including rejected attempts is NOT done:
    #: only executed or in-flight calls count toward the SPEC budget).
    tool_call_count: int = 0
    #: Injectable collaborators (tests pass fakes; production builds defaults).
    search_service: Any = None
    price_provider: Any = None
    search_service_factory: Callable[[], Any] | None = None
    storage_dir: str | None = None
    #: The memo content hash the stored approval must still match.
    memo_content_hash: str | None = None


@dataclass
class ToolOutcome:
    """The JSON-safe result of one tool attempt (executed or rejected)."""

    tool: str
    ok: bool
    #: Executed (ok or handler-error); rejected calls never ran.
    executed: bool
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    rejection: str | None = (
        None  # unknown_tool | invalid_arguments | budget_exceeded | repeated_tool
    )
    call_id: int | None = None
    duration_ms: float | None = None

    @property
    def json(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "ok": self.ok,
            "executed": self.executed,
            "result": self.result,
            "error": self.error,
            "rejection": self.rejection,
        }


class ToolRegistryEntry:
    """One allowlist entry: typed args model + handler + read/write kind."""

    def __init__(
        self,
        name: str,
        args_model: type[BaseModel],
        handler: Callable[[ToolContext, BaseModel], dict[str, Any]],
        *,
        kind: Literal["read", "write"],
    ) -> None:
        self.name = name
        self.args_model = args_model
        self.handler = handler
        self.kind = kind


TOOL_REGISTRY: dict[str, ToolRegistryEntry] = {}


def _register(entry: ToolRegistryEntry) -> None:
    TOOL_REGISTRY[entry.name] = entry


# ---------------------------------------------------------------------------
# Execution with allowlist + budget checks + audit trail
# ---------------------------------------------------------------------------


def execute_tool(context: ToolContext, name: str, raw_args: dict[str, Any]) -> ToolOutcome:
    """The ONLY way any tool runs (SPEC §20: allowlist, budgets, audit).

    Order: allowlist lookup -> typed-args parse -> budget checks -> log row
    BEFORE execution -> handler -> log row AFTER execution. Rejected attempts
    are logged with their rejection reason and never execute.
    """
    repo = RunsRepo(context.session) if context.run_row_id is not None else None

    def _log(
        status: str,
        arguments: dict,
        *,
        result: dict | None = None,
        error: str | None = None,
        duration: float | None = None,
        call_id: int | None = None,
    ) -> int | None:
        if repo is None:
            return None
        if call_id is not None:
            repo.finish_tool_call(
                call_id, status=status, result=result, duration_ms=duration or 0.0, error=error
            )
            return call_id
        return repo.start_tool_call(
            context.run_row_id or 0,
            context.tool_call_count,
            name,
            arguments,
            status=status,
        )

    # 1. Allowlist: unknown tool names are rejected BEFORE anything else.
    entry = TOOL_REGISTRY.get(name)
    if entry is None:
        call_id = _log("rejected_unknown_tool", dict(raw_args or {}))
        return ToolOutcome(
            tool=name,
            ok=False,
            executed=False,
            error=f"unknown tool {name!r}: the planner may only select from the "
            f"allowlist {sorted(TOOL_REGISTRY)}",
            rejection="unknown_tool",
            call_id=call_id,
        )

    # 2. Typed arguments only (a SQL string or path component cannot parse).
    try:
        args = entry.args_model.model_validate(raw_args or {})
    except ValidationError as exc:
        call_id = _log("rejected_invalid_arguments", dict(raw_args or {}))
        return ToolOutcome(
            tool=name,
            ok=False,
            executed=False,
            error=f"invalid arguments for {name}: {exc.error_count()} error(s)",
            rejection="invalid_arguments",
            call_id=call_id,
        )

    # 3. Budgets checked BEFORE execution (SPEC §20).
    if context.tool_call_count >= context.max_tool_calls:
        call_id = _log("rejected_budget_exceeded", args.model_dump(mode="json"))
        return ToolOutcome(
            tool=name,
            ok=False,
            executed=False,
            error=(
                f"tool budget exhausted: {context.tool_call_count} of "
                f"{context.max_tool_calls} allowed calls used; {name} blocked"
            ),
            rejection="budget_exceeded",
            call_id=call_id,
        )
    if entry.kind == "read" and name in context.tools_executed:
        call_id = _log("rejected_repeated_tool", args.model_dump(mode="json"))
        return ToolOutcome(
            tool=name,
            ok=False,
            executed=False,
            error=f"{name} was already executed this run (each read tool at most once)",
            rejection="repeated_tool",
            call_id=call_id,
        )

    # 4. Log BEFORE execution, run, log AFTER (audit trail; SPEC §20).
    call_id = _log("running", args.model_dump(mode="json"))
    started = time.perf_counter()
    context.tool_call_count += 1
    try:
        result = entry.handler(context, args)
    except Exception as exc:  # noqa: BLE001 - handler failures become outcomes
        duration = (time.perf_counter() - started) * 1000
        if call_id is not None:
            _log(
                "error",
                args.model_dump(mode="json"),
                result=None,
                error=f"{type(exc).__name__}: {exc}",
                duration=duration,
                call_id=call_id,
            )
        return ToolOutcome(
            tool=name,
            ok=False,
            executed=True,
            error=f"{type(exc).__name__}: {exc}",
            call_id=call_id,
            duration_ms=duration,
        )
    duration = (time.perf_counter() - started) * 1000
    if call_id is not None:
        _log(
            "ok",
            args.model_dump(mode="json"),
            result=result,
            duration=duration,
            call_id=call_id,
        )
    context.tools_executed.append(name)
    return ToolOutcome(
        tool=name, ok=True, executed=True, result=result, call_id=call_id, duration_ms=duration
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _handle_get_company_facts(context: ToolContext, args: BaseModel) -> dict[str, Any]:
    assert isinstance(args, GetCompanyFactsArgs)
    # SQL-backed via the typed repo path only: build_fact_card(session, ticker,
    # period_end). No query text exists anywhere in the argument surface.
    try:
        card = build_fact_card(context.session, args.ticker, args.period_end)
    except LookupError as exc:
        return {"status": "not_found", "error": str(exc)}
    return {
        "status": "ok",
        "ticker": card.ticker,
        "period_end": card.period_end.isoformat(),
        "fiscal_quarter": card.fiscal_quarter,
        "fiscal_year": card.fiscal_year,
        "currency": card.currency,
        "facts": card.model_dump(mode="json"),
        "metrics": [
            {"metric_id": m.metric_id, "status": m.status.value, "unit": m.unit}
            for m in card.metrics
        ],
    }


def _handle_search_filings(context: ToolContext, args: BaseModel) -> dict[str, Any]:
    assert isinstance(args, SearchFilingsArgs)
    service = context.search_service
    if service is None and context.search_service_factory is not None:
        service = context.search_service_factory()
    if service is None:
        return {"status": "unavailable", "error": "no search service configured", "items": []}
    from quarterline.retrieve.models import SearchQuery

    query = SearchQuery(
        query=args.query,
        ticker=args.ticker,
        strategy="section",
        retrieval="hybrid",
        mode=args.mode,
        period_end=args.period_end,
        top_k=args.top_k,
    )
    try:
        result = service.search(query)
    except Exception as exc:  # noqa: BLE001 - retrieval failure is data, not a crash
        from quarterline.retrieve.embeddings import EmbeddingModelMismatchError

        if not isinstance(exc, EmbeddingModelMismatchError):
            return {"status": "unavailable", "error": str(exc), "items": []}
        query.retrieval = "lexical"  # SPEC §25: lexical remains available
        result = service.search(query)
    from quarterline.store.repositories.documents import DocumentsRepo

    docs_repo = DocumentsRepo(context.session)
    items: list[dict[str, Any]] = []
    for item in result.items:
        document = docs_repo.get_document(item.document_id)
        items.append(
            {
                "evidence_id": item.evidence_id,
                "document_id": item.document_id,
                "section": item.section,
                "text": item.text,
                "period_end": (
                    document.period_end.isoformat()
                    if document is not None and document.period_end
                    else None
                ),
                "scores": {k: float(v) for k, v in item.scores.items()},
            }
        )
    return {
        "status": ("insufficient_evidence" if result.insufficient_evidence else "ok"),
        "insufficient_evidence_reason": result.insufficient_evidence_reason,
        "degraded": dict(result.degraded),
        "items": items,
    }


def _handle_get_prices(context: ToolContext, args: BaseModel) -> dict[str, Any]:
    assert isinstance(args, GetPricesArgs)
    if context.price_provider is None:
        return {"status": "price_unavailable", "error": "no price provider configured"}
    from quarterline.sources.prices.yfinance_provider import (
        PriceProviderUnavailable,
        default_window,
    )

    start, end = default_window(args.period_end)
    try:
        rows = context.price_provider.get_prices(args.ticker, start, end)
    except PriceProviderUnavailable as exc:
        # Graceful degradation (SPEC §20/§25): never blocks a facts-only memo.
        return {"status": "price_unavailable", "error": str(exc)}
    return {
        "status": "ok",
        "ticker": args.ticker,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "informational_only": True,
        "rows": rows,
    }


def _exports_dir(context: ToolContext):
    from pathlib import Path

    base = Path(context.storage_dir) if context.storage_dir else None
    if base is None:
        from quarterline.config import get_settings

        base = Path(get_settings().storage_dir)
    exports = base / EXPORTS_DIRNAME
    exports.mkdir(parents=True, exist_ok=True)
    return exports


def _handle_export_memo(context: ToolContext, args: BaseModel) -> dict[str, Any]:
    assert isinstance(args, ExportMemoArgs)
    run_id = args.run_id
    if not RUN_ID_RE.fullmatch(run_id):
        # Path-traversal proof: the run id MUST be bare uuid4 hex; the filename
        # is server-generated from it — a client can never name the file.
        return {
            "status": "rejected",
            "error": f"run_id {run_id!r} is not a valid run id; export filename is "
            "server-generated and path components are rejected",
        }
    from quarterline.agent.approval import verify_export_approval

    decision = verify_export_approval(context.session, run_id, args.export_type)
    if not decision.approved:
        return {"status": "rejected", "error": decision.reason}

    from quarterline.agent.checkpoints import load_state
    from quarterline.agent.schemas import MemoDraft

    state = load_state(context.session, run_id)
    if state is None or not state.get("memo_content"):
        return {"status": "rejected", "error": "no validated memo content stored for this run"}
    content = state["memo_content"]

    if args.export_type == "md":
        filename = f"memo-{run_id}.md"
        payload = content
    else:
        draft = MemoDraft.model_validate(state["draft"])
        filename = f"memo-{run_id}.json"
        payload = draft.model_dump_json(indent=2)

    exports = _exports_dir(context)
    target = exports / filename
    resolved = target.resolve()
    if resolved.parent != exports.resolve():
        # Defense in depth: can only trigger if the filesystem itself is odd.
        return {"status": "rejected", "error": "resolved export path left the exports directory"}
    resolved.write_text(payload, encoding="utf-8")
    from quarterline.observability.events import emit_run_event

    emit_run_event(
        run_id,
        {
            "endpoint": "agent_export",
            "ticker": state.get("request", {}).get("ticker"),
            "export_type": args.export_type,
            "filename": filename,
            "bytes": len(payload),
            "approval_request_id": decision.request_id,
        },
    )
    return {
        "status": "ok",
        "path": str(resolved),
        "filename": filename,
        "bytes": len(payload),
        "export_type": args.export_type,
        "memo_content_hash": decision.memo_content_hash,
    }


_register(
    ToolRegistryEntry(
        "get_company_facts", GetCompanyFactsArgs, _handle_get_company_facts, kind="read"
    )
)
_register(
    ToolRegistryEntry("search_filings", SearchFilingsArgs, _handle_search_filings, kind="read")
)
_register(ToolRegistryEntry("get_prices", GetPricesArgs, _handle_get_prices, kind="read"))
_register(ToolRegistryEntry("export_memo", ExportMemoArgs, _handle_export_memo, kind="write"))

#: The planner's read-tool universe (export is approval-gated, never planned).
READ_TOOLS: tuple[str, ...] = ("get_company_facts", "search_filings", "get_prices")

__all__ = [
    "EXPORTS_DIRNAME",
    "READ_TOOLS",
    "TOOL_REGISTRY",
    "ExportMemoArgs",
    "GetCompanyFactsArgs",
    "GetPricesArgs",
    "SearchFilingsArgs",
    "ToolContext",
    "ToolOutcome",
    "ToolRegistryEntry",
    "execute_tool",
]
