"""The bounded LangGraph memo workflow (SPEC §20 Graph + Budgets).

Graph (all nodes deterministic; the planner is RULE-BASED, never autonomous)::

    begin (entry router: fresh run vs. checkpoint resume vs. approved export)
      -> validate request -> classify intent -> create constrained plan
      -> execute allowed read tools (each at most once)
      -> write memo -> validate memo (F5's gate rules)
      -> await export approval -> export

Budgets (SPEC §20, enforced in :func:`_wrapped` + :func:`execute_tool` and
tested): each read tool at most once per run; at most 4 total tool calls
INCLUDING export; at most 12 graph transitions (counted separately from tool
calls); ONE memo schema-repair attempt (llm.repair semantics); an overall
request deadline checked BETWEEN nodes. On any limit breach the run fails in a
controlled way with the checkpoint persisted — there is no autonomous retry
loop and no infinite loop is possible (every node is visited at most once per
invocation and the entry router only ever moves forward).

Every node transition persists the full state snapshot to ``agent_runs``
(:mod:`quarterline.agent.checkpoints`); resume rebuilds from the last
checkpoint without re-calling tools. There is no cross-user or conversational
memory: each run is exactly its checkpoint row (SPEC §20 Memory/state).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError
from sqlalchemy.orm import Session

from quarterline.agent import approval
from quarterline.agent import checkpoints as ckpt
from quarterline.agent import tools as agent_tools
from quarterline.agent.schemas import (
    MemoDraft,
    MemoOutput,
    MemoRequest,
    MemoSection,
    memo_content_markdown,
    memo_json_schema,
)
from quarterline.agent.state import AgentState, initial_state, is_terminal
from quarterline.config import Settings, get_settings
from quarterline.core.advice_policy import detect_advice_content, detect_advice_request
from quarterline.core.citations import EvidenceRef, validate_statement_citations
from quarterline.core.factcheck import check_statement_numbers, expand_metric_mentions
from quarterline.core.models import FactCard
from quarterline.observability.events import emit_run_event, new_run_id
from quarterline.sources.prices.yfinance_provider import YfinancePriceProvider

#: Overall request deadline when no override is supplied (settings field
#: pending — see docs/failure_cases.md gaps note; SPEC §20 "Overall request
#: deadline").
DEFAULT_DEADLINE_SECONDS = 60.0

#: Deterministic retrieval queries per intent (mode presets scope sections).
QUARTER_REVIEW_QUERY = "quarterly results revenue operating margin cash flow management discussion"
RISK_REVIEW_QUERY = "risk factors supply chain regulation management discussion outlook"

#: Market-context language that may add the informational get_prices tool.
_MARKET_CONTEXT_RE = re.compile(
    r"\b(stock price|share price|market context|market data|prices|trading|"
    r"valuation|price action)\b",
    re.IGNORECASE,
)

#: Memo sections that make filing-derived assertions and therefore MUST cite
#: supplied evidence; ``evidence_gaps`` is open-question-like and may be uncited.
CITATION_REQUIRED_HEADINGS: frozenset[str] = frozenset(
    {"overview", "what_changed", "management_explanation", "risks_and_open_questions"}
)


class SimulatedCrash(RuntimeError):
    """Test hook: raised AFTER the checkpoint persist to simulate process death."""


def _metric_mention_reasons(mentions) -> list[str]:
    """Check 8 rejection reasons: allowlist + structural template (SPEC §18)."""
    from quarterline.core.factcheck import template_valid_for
    from quarterline.core.models import METRIC_IDS
    from quarterline.llm.schemas import MetricMention

    reasons: list[str] = []
    for mention in mentions:
        assert isinstance(mention, MetricMention)
        if mention.metric_id not in METRIC_IDS:  # defense in depth (pydantic gates too)
            reasons.append(f"metric_id {mention.metric_id!r} is not in the METRIC_IDS allowlist")
        elif not template_valid_for(mention.metric_id, mention.template):
            reasons.append(
                f"template {mention.template!r} is not structurally valid for "
                f"metric {mention.metric_id!r}"
            )
    return reasons


# ---------------------------------------------------------------------------
# Workflow configuration
# ---------------------------------------------------------------------------


@dataclass
class WorkflowConfig:
    """Injected collaborators + budget limits for one workflow build."""

    session: Session
    run_row_id: int
    settings: Settings
    provider: Any | None = None
    search_service: Any | None = None
    price_provider: Any | None = None
    max_tool_calls: int = 4
    max_transitions: int = 12
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS
    #: Injectable monotonic clock (functions are immutable, so a plain default).
    clock: Callable[[], float] = time.monotonic
    crash_after: str | None = None


def default_search_service(session: Session, settings: Settings):
    """SearchService from configuration (embedding provider per settings)."""
    from quarterline.retrieve.search import SearchService, provider_from_settings

    return SearchService(session, provider_from_settings(settings), settings=settings)


def default_price_provider(settings: Settings | None = None) -> YfinancePriceProvider:
    """The informational price provider (yfinance behind a lazy import)."""
    return YfinancePriceProvider()


def _resolve_provider(config: WorkflowConfig):
    """Provider resolution with the remote-fallback consent gate.

    With ``ALLOW_REMOTE_FALLBACK=false`` the OpenRouter provider is NEVER
    constructed — the consent error is raised before any constructor runs
    (SPEC §2.3.2/§25: never silently send data to a remote provider).
    """
    if config.provider is not None:
        return config.provider
    settings = config.settings
    if settings.llm_provider == "openrouter" and not settings.allow_remote_fallback:
        from quarterline.llm.base import RemoteFallbackNotConsented

        raise RemoteFallbackNotConsented(
            "llm_provider=openrouter without ALLOW_REMOTE_FALLBACK consent; "
            "no remote provider was constructed"
        )
    from quarterline.llm.generation import default_generation_provider

    return default_generation_provider(settings)


# ---------------------------------------------------------------------------
# Deterministic nodes (each returns partial state updates)
# ---------------------------------------------------------------------------


def _node_validate_request(config: WorkflowConfig):
    def node(state: AgentState) -> dict[str, Any]:
        try:
            request = MemoRequest.model_validate(state.get("request") or {})
        except ValidationError as exc:
            return {
                "status": "failed",
                "errors": [
                    f"invalid memo request: {exc.error_count()} error(s): {exc.errors()[:3]}"
                ],
            }
        payload = request.model_dump(mode="json")
        decision = detect_advice_request(request.focus_text)
        if decision.is_advice:
            return {
                "request": payload,
                "status": "refused",
                "errors": [
                    (
                        f"advice-policy {decision.policy_version} matched "
                        f"({', '.join(decision.matched)}); research-only refusal"
                    )
                ],
            }
        return {"request": payload}

    return node


def _node_classify_intent(config: WorkflowConfig):
    def node(state: AgentState) -> dict[str, Any]:
        request = MemoRequest.model_validate(state["request"])
        from quarterline.store.repositories.companies import CompaniesRepo

        company = CompaniesRepo(config.session).get_by_ticker(request.ticker)
        market = request.market_context or bool(_MARKET_CONTEXT_RE.search(request.focus_text))
        updates: dict[str, Any] = {
            "intent": request.memo_type,
            "market_context_requested": market,
            "company_id": company.id if company is not None else None,
        }
        if company is None:
            # Not fatal yet: the facts tool records a controlled not_found.
            updates["errors"] = list(state.get("errors") or []) + [
                f"unknown company {request.ticker!r}: no fact card can be built"
            ]
        return updates

    return node


def _node_create_plan(config: WorkflowConfig):
    def node(state: AgentState) -> dict[str, Any]:
        plan = ["get_company_facts", "search_filings"]
        if state.get("market_context_requested"):
            plan.append("get_prices")
        # The planner may ONLY select allowlist names (SPEC §20 Graph).
        unknown = [name for name in plan if name not in agent_tools.READ_TOOLS]
        if unknown:
            return {
                "status": "failed",
                "errors": list(state.get("errors") or [])
                + [f"planner selected non-allowlist tool(s): {unknown}; rejected"],
            }
        return {"plan": plan}

    return node


def _search_args_for(request: MemoRequest) -> dict[str, Any]:
    mode = "brief" if request.memo_type == "quarter_review" else "risk"
    query = request.focus_text.strip() or (
        QUARTER_REVIEW_QUERY if mode == "brief" else RISK_REVIEW_QUERY
    )
    return {
        "query": query,
        "ticker": request.ticker,
        "mode": mode,
        "period_end": request.period_end.isoformat() if request.period_end else None,
        "top_k": 6,
    }


def _node_execute_read_tools(config: WorkflowConfig):
    def node(state: AgentState) -> dict[str, Any]:
        request = MemoRequest.model_validate(state["request"])
        context = agent_tools.ToolContext(
            session=config.session,
            run_id=state["run_id"],
            run_row_id=config.run_row_id,
            max_tool_calls=config.max_tool_calls,
            tools_executed=list(state.get("tools_executed") or []),
            tool_call_count=int(state.get("tool_call_count", 0)),
            search_service=config.search_service,
            search_service_factory=(
                (lambda: default_search_service(config.session, config.settings))
                if config.search_service is None
                else None
            ),
            price_provider=(
                config.price_provider
                if config.price_provider is not None
                else default_price_provider(config.settings)
            ),
            storage_dir=str(config.settings.storage_dir),
        )
        tool_results = dict(state.get("tool_results") or {})
        errors = list(state.get("errors") or [])
        facts = state.get("facts")
        evidence = list(state.get("evidence") or [])

        period = request.period_end.isoformat() if request.period_end else None
        planned_args: dict[str, dict[str, Any]] = {
            "get_company_facts": {"ticker": request.ticker, "period_end": period},
            "search_filings": _search_args_for(request),
            "get_prices": {"ticker": request.ticker, "period_end": period},
        }

        for name in state.get("plan") or []:
            if name in context.tools_executed:
                continue  # resume: never re-call a tool (read-once rule)
            outcome = agent_tools.execute_tool(context, name, planned_args[name])
            tool_results[name] = outcome.json
            if outcome.executed:
                if outcome.ok:
                    if name == "get_company_facts":
                        if outcome.result.get("status") == "not_found":
                            errors.append(
                                f"get_company_facts failed: {outcome.result.get('error')}"
                            )
                            return _failed_tools(
                                state,
                                context,
                                tool_results,
                                errors,
                                "no canonical facts for this ticker; controlled failure",
                            )
                        facts = outcome.result.get("facts")
                    elif name == "search_filings":
                        evidence = list(outcome.result.get("items") or [])
                        if outcome.result.get("status") == "insufficient_evidence":
                            errors.append(
                                "search_filings returned insufficient evidence "
                                f"({outcome.result.get('insufficient_evidence_reason')})"
                            )
                    elif name == "get_prices":
                        if outcome.result.get("status") == "price_unavailable":
                            errors.append(
                                "get_prices unavailable (non-blocking): "
                                f"{outcome.result.get('error')}"
                            )
                        # Price failures never block a facts-only memo
                        # (SPEC §20/§25).
                else:
                    if name == "get_prices":
                        errors.append(f"get_prices unavailable (non-blocking): {outcome.error}")
                    else:
                        errors.append(f"{name} failed: {outcome.error}")
            else:
                errors.append(f"{name} blocked before execution: {outcome.error}")

        return {
            "tool_results": tool_results,
            "tools_executed": list(context.tools_executed),
            "tool_call_count": context.tool_call_count,
            "errors": errors,
            "facts": facts,
            "evidence": evidence,
        }

    return node


def _failed_tools(
    state: AgentState,
    context: agent_tools.ToolContext,
    tool_results: dict,
    errors: list[str],
    detail: str,
) -> dict[str, Any]:
    return {
        "tool_results": tool_results,
        "tools_executed": list(context.tools_executed),
        "tool_call_count": context.tool_call_count,
        "errors": errors + [detail],
        "status": "failed",
    }


def _node_write_memo(config: WorkflowConfig):
    def node(state: AgentState) -> dict[str, Any]:
        if state.get("draft") is not None or state.get("draft_output") is not None:
            return {}  # resume: never re-write (or re-call the provider)
        evidence = state.get("evidence") or []
        if not evidence:
            return {
                "status": "insufficient_evidence",
                "errors": list(state.get("errors") or [])
                + [
                    (
                        "no admissible evidence windows; memo abstained without a "
                        "generation call (evidence-policy-v1)"
                    )
                ],
            }
        facts = state.get("facts")
        if not facts:
            return {
                "status": "failed",
                "errors": list(state.get("errors") or []) + ["no fact card; cannot write a memo"],
            }
        card = FactCard.model_validate(facts)
        request = MemoRequest.model_validate(state["request"])

        from quarterline.llm.generation import label_display_of
        from quarterline.llm.prompts import load_prompt, render_prompt

        label_display = label_display_of(card)
        spec = load_prompt("memo", "v1")
        system = render_prompt(
            spec,
            ticker=card.ticker,
            period=card.period_end.isoformat(),
            label=label_display or "Insufficient data",
        )
        payload = {
            "task": "agent_memo",
            "memo_type": request.memo_type,
            "focus": request.focus_text,
            "ticker": card.ticker,
            "period_end": card.period_end.isoformat(),
            "required_label_echo": label_display or "Insufficient data",
            "facts": [
                {"metric_id": m.metric_id, "unit": m.unit, "availability": m.status.value}
                for m in card.metrics
            ],
            "evidence": [
                {
                    "evidence_id": item["evidence_id"],
                    "section": item.get("section"),
                    "text": item["text"],
                }
                for item in evidence
            ],
        }
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": _json_dumps(payload)},
        ]
        try:
            provider = _resolve_provider(config)
        except Exception as exc:  # noqa: BLE001 - consent/config failures are controlled
            return {
                "status": "failed",
                "errors": list(state.get("errors") or [])
                + [f"generation provider not available: {exc}"],
            }
        from quarterline.llm.base import (
            GenerationProviderUnavailable,
            RemoteFallbackNotConsented,
        )
        from quarterline.llm.repair import GenerationFailure, generate_and_parse

        try:
            repaired = generate_and_parse(
                provider, messages=messages, model_cls=MemoOutput, json_schema=memo_json_schema()
            )
        except (GenerationProviderUnavailable, RemoteFallbackNotConsented) as exc:
            # SPEC §25: generation unavailable -> facts and evidence survive,
            # no fake prose; the RUN fails in a controlled way.
            return {
                "status": "failed",
                "errors": list(state.get("errors") or [])
                + [f"generation provider unavailable: {exc}"],
            }
        except GenerationFailure as exc:
            return {
                "status": "failed",
                "errors": list(state.get("errors") or [])
                + [f"controlled generation failure after the single repair pass: {exc}"],
            }
        return {
            "draft_output": repaired.model.model_dump(mode="json"),
            "memo_repair_used": bool(repaired.repair_used),
        }

    return node


def _json_dumps(payload: dict) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False)


def _node_validate_memo(config: WorkflowConfig):
    def node(state: AgentState) -> dict[str, Any]:
        output = state.get("draft_output")
        if output is None or state.get("draft") is not None:
            return {}
        parsed = MemoOutput.model_validate(output)
        card = FactCard.model_validate(state["facts"])
        request = MemoRequest.model_validate(state["request"])
        from quarterline.llm.generation import label_display_of

        label_display = label_display_of(card)
        checks: list[dict[str, Any]] = []
        errors = list(state.get("errors") or [])

        def _check(
            check_id: str, name: str, passed: bool, reasons: list[str], dropped: int = 0
        ) -> None:
            checks.append(
                {
                    "check_id": check_id,
                    "name": name,
                    "passed": passed,
                    "dropped_count": dropped,
                    "reasons": reasons,
                }
            )

        # check 2: status validity / model abstention
        if parsed.status in ("insufficient_evidence", "refused"):
            _check(
                "2",
                "status_validity",
                True,
                ["model returned an abstention status; no content is presented"],
            )
            run_status = "refused" if parsed.status == "refused" else "insufficient_evidence"
            return {
                "validation_results": {"version": "validation-v1", "checks": checks},
                "status": run_status,
                "errors": errors + [f"model abstained: {parsed.status}"],
            }
        _check("2", "status_validity", True, [])

        # check 3: label echo EQUALITY
        echoed = (parsed.label_echo or "").strip()
        expected = (label_display or "").strip()
        label_ok = bool(expected) and echoed.lower() == expected.lower()
        if not label_ok:
            _check(
                "3",
                "label_echo_equality",
                False,
                [
                    (
                        f"label_echo {echoed!r} does not equal the code-generated "
                        f"label {expected!r}; all content dropped"
                    )
                ],
                dropped=len(parsed.sections),
            )
            return {
                "validation_results": {"version": "validation-v1", "checks": checks},
                "status": "insufficient_evidence",
                "errors": errors + ["label echo mismatch; all memo content dropped"],
            }
        _check("3", "label_echo_equality", True, [])

        # checks 4-7: citations per section (filing-derived sections must cite)
        evidence_map = {
            item["evidence_id"]: EvidenceRef(
                evidence_id=item["evidence_id"],
                document_id=int(item["document_id"]),
                ticker=card.ticker,
                period_end=(_parse_date(item["period_end"]) if item.get("period_end") else None),
                text=item["text"],
            )
            for item in state.get("evidence") or []
        }
        kept_sections: list[MemoSection] = []
        per_check: dict[str, list[str]] = {}
        checked = 0
        for section in parsed.sections:
            checked += 1
            reasons: list[str] = []
            if section.heading in CITATION_REQUIRED_HEADINGS and not section.evidence_ids:
                reasons.append(
                    "citation_existence: filing-derived section has no supplied citation"
                )
            reasons.extend(
                validate_statement_citations(
                    section.text,
                    section.evidence_ids,
                    evidence_map,
                    expected_ticker=card.ticker,
                    expected_period_end=card.period_end,
                )
            )
            if reasons:
                for reason in reasons:
                    bucket, _, detail = reason.partition(":")
                    per_check.setdefault(bucket, []).append(f"{section.heading}: {detail.strip()}")
            else:
                kept_sections.append(section)
        for bucket, check_id, name in (
            ("citation_existence", "4", "citation_existence"),
            ("citation_supplied_context", "5", "citation_supplied_context"),
            ("citation_company_period", "6", "citation_company_period"),
            ("citation_sentence_format", "7", "citation_sentence_format"),
        ):
            reasons = per_check.get(bucket, [])
            _check(
                check_id,
                name,
                not reasons,
                reasons,
                dropped=checked - len(kept_sections) if reasons else 0,
            )

        # check 8: metric-reference validity
        mention_reasons = _metric_mention_reasons(parsed.metric_mentions)
        _check(
            "8",
            "metric_reference_validity",
            not mention_reasons,
            mention_reasons,
            dropped=len(mention_reasons),
        )
        kept_mentions = parsed.metric_mentions if not mention_reasons else []

        # check 9: numeric consistency on surviving section text
        numeric_kept: list[MemoSection] = []
        numeric_reasons: list[str] = []
        for section in kept_sections:
            reasons = check_statement_numbers(section.text, card)
            if reasons:
                numeric_reasons.extend(f"{section.heading}: {r}" for r in reasons)
            else:
                numeric_kept.append(section)
        _check(
            "9",
            "numeric_consistency",
            not numeric_reasons,
            numeric_reasons,
            dropped=len(kept_sections) - len(numeric_kept),
        )

        # check 10: advice-policy compliance
        advice_kept: list[MemoSection] = []
        advice_reasons: list[str] = []
        advice_hit = False
        for section in numeric_kept:
            decision = detect_advice_content(section.text)
            if decision.is_advice:
                advice_hit = True
                advice_reasons.append(
                    f"{section.heading}: generated content issues investment advice "
                    f"({', '.join(decision.matched)}); section dropped"
                )
            else:
                advice_kept.append(section)
        _check(
            "10",
            "advice_policy_compliance",
            not advice_reasons,
            advice_reasons,
            dropped=len(numeric_kept) - len(advice_kept),
        )

        # metric expansion: Python renders the numbers (never the model)
        expansion = expand_metric_mentions(kept_mentions, card)
        metric_facts = [fact.text for fact in expansion.rendered]

        gate_failed = any(
            not check["passed"]
            for check in checks
            if check["check_id"] in {"3", "4", "5", "6", "7", "9", "10"}
        )
        if advice_kept or metric_facts:
            dropped_anything = gate_failed or bool(expansion.issues)
            draft_status = "partial" if dropped_anything else "ok"
            run_status = None  # continue to approval
        elif advice_hit:
            draft_status = "refused"
            run_status = "refused"
        else:
            draft_status = "insufficient_evidence"
            run_status = "insufficient_evidence"

        reasons_all = [f"check[{c['check_id']}]: {r}" for c in checks for r in c["reasons"]] + [
            f"metric_mention dropped: {issue}" for issue in expansion.issues
        ]
        draft = MemoDraft(
            status=draft_status,  # type: ignore[arg-type]
            title=f"{card.ticker} {request.memo_type.replace('_', ' ')} — "
            f"{card.period_end.isoformat()}",
            label_echo=label_display if label_ok else None,
            sections=[
                MemoSection(
                    heading=section.heading,
                    text=section.text,
                    evidence_ids=list(section.evidence_ids),
                )
                for section in advice_kept
            ],
            metric_mentions=kept_mentions if not mention_reasons else [],
        )
        memo_content = memo_content_markdown(draft)
        updates: dict[str, Any] = {
            "draft": draft.model_dump(mode="json"),
            "memo_content": memo_content,
            "memo_content_hash": approval.memo_content_hash(memo_content),
            "metric_facts": metric_facts,
            "validation_results": {
                "version": "validation-v1",
                "checks": checks,
                "reasons": reasons_all,
            },
            "errors": errors + reasons_all,
        }
        if run_status is not None:
            updates["status"] = run_status
        return updates

    return node


def _parse_date(value: str):
    from datetime import date

    return date.fromisoformat(value)


def _node_await_export_approval(config: WorkflowConfig):
    def node(state: AgentState) -> dict[str, Any]:
        draft = state.get("draft")
        if not draft or state.get("status") not in (None, "awaiting_approval"):
            return {}
        memo_content = state.get("memo_content") or ""
        repo = _runs_repo(config)
        existing = [
            row
            for row in repo.list_approvals(state["run_id"])
            if row.status == "pending"
            and row.memo_content_hash == (state.get("memo_content_hash") or "")
        ]
        by_type = {row.export_type: row for row in existing}
        if "md" not in by_type:
            by_type["md"] = approval.request_approval(
                config.session, state["run_id"], memo_content, "md"
            )
        if "json" not in by_type:
            by_type["json"] = approval.request_approval(
                config.session, state["run_id"], memo_content, "json"
            )
        return {
            "approval_status": "pending",
            "approval_request_id": by_type["md"].id,
            "status": "awaiting_approval",
        }

    return node


def _node_export(config: WorkflowConfig):
    def node(state: AgentState) -> dict[str, Any]:
        context = agent_tools.ToolContext(
            session=config.session,
            run_id=state["run_id"],
            run_row_id=config.run_row_id,
            max_tool_calls=config.max_tool_calls,
            tools_executed=list(state.get("tools_executed") or []),
            tool_call_count=int(state.get("tool_call_count", 0)),
            storage_dir=str(config.settings.storage_dir),
        )
        export_type = state.get("export_type_requested") or "md"
        outcome = agent_tools.execute_tool(
            context, "export_memo", {"run_id": state["run_id"], "export_type": export_type}
        )
        errors = list(state.get("errors") or [])
        tool_results = dict(state.get("tool_results") or {})
        tool_results["export_memo"] = outcome.json
        if outcome.ok:
            return {
                "tool_results": tool_results,
                "tools_executed": list(context.tools_executed),
                "tool_call_count": context.tool_call_count,
                "exported": outcome.result,
                "status": "completed",
                "approval_status": "granted",
            }
        return {
            "tool_results": tool_results,
            "tools_executed": list(context.tools_executed),
            "tool_call_count": context.tool_call_count,
            "errors": errors + [f"export_memo rejected: {outcome.error}"],
            "status": "failed",
        }

    return node


def _runs_repo(config: WorkflowConfig):
    from quarterline.store.repositories.runs import RunsRepo

    return RunsRepo(config.session)


# ---------------------------------------------------------------------------
# Graph assembly (budgets + deadline + checkpoints in the node wrapper)
# ---------------------------------------------------------------------------

_NODE_CHAIN = (
    ("validate_request", "classify_intent"),
    ("classify_intent", "create_plan"),
    ("create_plan", "execute_read_tools"),
    ("execute_read_tools", "write_memo"),
    ("write_memo", "validate_memo"),
    ("validate_memo", "await_export_approval"),
)


def _route_begin(state: AgentState) -> str:
    """Entry router: fresh run, checkpoint resume, or approved export."""
    if state.get("draft"):
        if state.get("approval_status") == "granted":
            return "export"
        return "await_export_approval"
    if state.get("draft_output"):
        return "validate_memo"
    if state.get("facts") is not None or state.get("evidence"):
        return "write_memo"
    return "validate_request"


def build_workflow(config: WorkflowConfig):
    """Compile the LangGraph workflow with wrapped (budgeted) nodes."""

    def wrapped(name: str, node_fn: Callable[[AgentState], dict]):
        def _node(state: AgentState) -> dict[str, Any]:
            transitions = int(state.get("transition_count", 0)) + 1
            updates: dict[str, Any] = {}
            now = config.clock()
            started = state.get("started_at_monotonic", now)
            elapsed = max(0.0, now - started)
            if elapsed > config.deadline_seconds:
                updates = {
                    "transition_count": transitions,
                    "status": "failed",
                    "errors": list(state.get("errors") or [])
                    + [
                        (
                            f"request deadline exceeded before node {name!r} "
                            f"({elapsed:.3f}s > {config.deadline_seconds:.3f}s); "
                            "controlled failure, no retry loop"
                        ),
                    ],
                }
            elif transitions > config.max_transitions:
                updates = {
                    "transition_count": transitions,
                    "status": "failed",
                    "errors": list(state.get("errors") or [])
                    + [
                        (
                            f"graph transition budget breached: {transitions} of "
                            f"{config.max_transitions} allowed; controlled failure"
                        ),
                    ],
                }
            else:
                updates = node_fn(state) or {}
                updates["transition_count"] = transitions
            merged = {**state, **updates}
            ckpt.persist_checkpoint(config.session, merged)
            # Commit NOW: a checkpoint is only a real crash boundary once it is
            # committed, and releasing the SQLite write lock lets best-effort
            # telemetry (separate connection) persist while the run continues.
            config.session.commit()
            if config.crash_after == name:
                raise SimulatedCrash(f"simulated crash after node {name!r} (checkpoint persisted)")
            return updates

        return _node

    graph = StateGraph(AgentState)
    nodes = {
        "begin": lambda state: {},
        "validate_request": _node_validate_request(config),
        "classify_intent": _node_classify_intent(config),
        "create_plan": _node_create_plan(config),
        "execute_read_tools": _node_execute_read_tools(config),
        "write_memo": _node_write_memo(config),
        "validate_memo": _node_validate_memo(config),
        "await_export_approval": _node_await_export_approval(config),
        "export": _node_export(config),
    }
    for name, fn in nodes.items():
        graph.add_node(name, wrapped(name, fn))

    graph.add_edge(START, "begin")
    graph.add_conditional_edges(
        "begin",
        _route_begin,
        {
            "validate_request": "validate_request",
            "write_memo": "write_memo",
            "validate_memo": "validate_memo",
            "await_export_approval": "await_export_approval",
            "export": "export",
        },
    )
    for node_name, next_name in _NODE_CHAIN:
        mapping: dict[Any, Any] = {next_name: next_name, "end": END}

        def _router(state: AgentState, _next: str = next_name) -> Any:
            return "end" if is_terminal(state) else _next

        graph.add_conditional_edges(node_name, _router, mapping)
    graph.add_conditional_edges(
        "await_export_approval",
        lambda state: "export" if state.get("approval_status") == "granted" else "end",
        {"export": "export", "end": END},
    )
    graph.add_edge("export", END)
    return graph.compile()


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def _emit(run_id: str, event: dict[str, Any]) -> None:
    emit_run_event(run_id, {"endpoint": "agent_memo", **event})


def run_memo_workflow(
    request: dict[str, Any],
    *,
    session: Session | None = None,
    provider: Any | None = None,
    search_service: Any | None = None,
    price_provider: Any | None = None,
    settings: Settings | None = None,
    max_tool_calls: int | None = None,
    max_transitions: int | None = None,
    deadline_seconds: float | None = None,
    clock: Callable[[], float] | None = None,
    crash_after: str | None = None,
) -> Any:
    """Run the full bounded workflow; returns the :class:`AgentRunResult`.

    Synchronous by design for the MVP (SPEC §20); the run is checkpointed at
    every transition, so a crashed invocation is resumable.
    """
    settings = settings if settings is not None else get_settings()
    if session is not None:
        return _run(
            session,
            request,
            settings,
            provider=provider,
            search_service=search_service,
            price_provider=price_provider,
            max_tool_calls=max_tool_calls or settings.agent_max_tool_calls,
            max_transitions=max_transitions or settings.agent_max_graph_transitions,
            deadline_seconds=deadline_seconds
            if deadline_seconds is not None
            else DEFAULT_DEADLINE_SECONDS,
            clock=clock,
            crash_after=crash_after,
        )
    from quarterline.store.db import session_scope

    with session_scope() as owned_session:
        return _run(
            owned_session,
            request,
            settings,
            provider=provider,
            search_service=search_service,
            price_provider=price_provider,
            max_tool_calls=max_tool_calls or settings.agent_max_tool_calls,
            max_transitions=max_transitions or settings.agent_max_graph_transitions,
            deadline_seconds=deadline_seconds
            if deadline_seconds is not None
            else DEFAULT_DEADLINE_SECONDS,
            clock=clock,
            crash_after=crash_after,
        )


def _run(
    session: Session,
    request: dict[str, Any],
    settings: Settings,
    *,
    provider,
    search_service,
    price_provider,
    max_tool_calls: int,
    max_transitions: int,
    deadline_seconds: float,
    clock,
    crash_after: str | None,
):
    run_id = new_run_id()
    state = initial_state(
        run_id,
        dict(request),
        tool_budget={
            "max_tool_calls": max_tool_calls,
            "max_graph_transitions": max_transitions,
        },
        deadline_seconds=deadline_seconds,
        started_at_monotonic=(clock or time.monotonic)(),
        crash_after=crash_after,
    )
    run_row_id = ckpt.ensure_run_row(session, state)
    session.commit()  # the run row is durable before any node runs
    _emit(run_id, {"event": "run_started", "status": "running"})
    config = WorkflowConfig(
        session=session,
        run_row_id=run_row_id,
        settings=settings,
        provider=provider,
        search_service=search_service,
        price_provider=price_provider,
        max_tool_calls=max_tool_calls,
        max_transitions=max_transitions,
        deadline_seconds=deadline_seconds,
        clock=clock or time.monotonic,
        crash_after=crash_after,
    )
    workflow = build_workflow(config)
    final: AgentState = workflow.invoke(state, config={"recursion_limit": max_transitions + 8})
    _emit(
        run_id,
        {
            "event": "run_finished",
            "status": final.get("status"),
            "intent": final.get("intent"),
            "transition_count": final.get("transition_count"),
            "tool_call_count": final.get("tool_call_count"),
            "error_type": (final.get("errors") or [None])[-1],
        },
    )
    return ckpt.build_result(
        session,
        final,
        tool_call_limit=max_tool_calls,
        transition_limit=max_transitions,
    )


def resume_run(
    run_id: str,
    *,
    session: Session | None = None,
    settings: Settings | None = None,
    provider: Any | None = None,
    search_service: Any | None = None,
    price_provider: Any | None = None,
) -> Any | None:
    """Resume a crashed run from its last checkpoint (SPEC §20 Memory/state).

    The entry router replays ONLY the remaining nodes: completed tool calls,
    fact cards, evidence and validated drafts already in the checkpoint are
    never recomputed and no tool is called twice.
    """
    settings = settings if settings is not None else get_settings()

    def _resume(session: Session) -> Any | None:
        state = ckpt.load_state(session, run_id)
        if state is None:
            return None
        config = WorkflowConfig(
            session=session,
            run_row_id=ckpt.ensure_run_row(session, state),
            settings=settings,
            provider=provider,
            search_service=search_service,
            price_provider=price_provider,
            max_tool_calls=int(
                state.get("tool_budget", {}).get("max_tool_calls", settings.agent_max_tool_calls)
            ),
            max_transitions=int(
                state.get("tool_budget", {}).get(
                    "max_graph_transitions", settings.agent_max_graph_transitions
                )
            ),
            deadline_seconds=float(state.get("deadline_seconds", DEFAULT_DEADLINE_SECONDS)),
        )
        workflow = build_workflow(config)
        final = workflow.invoke(state, config={"recursion_limit": config.max_transitions + 8})
        return ckpt.build_result(
            session,
            final,
            tool_call_limit=config.max_tool_calls,
            transition_limit=config.max_transitions,
        )

    if session is not None:
        return _resume(session)
    from quarterline.store.db import session_scope

    with session_scope() as owned_session:
        return _resume(owned_session)


def export_approved_memo(
    run_id: str,
    request_id: int,
    memo_content: str,
    export_type: str,
    *,
    session: Session | None = None,
    settings: Settings | None = None,
) -> tuple[Any | None, approval.ApprovalDecision]:
    """Approve (hash + expiry validated) then run the approval-gated export.

    Returns ``(result_or_None, decision)``; ``result is None`` with a refusal
    reason means the caller answers HTTP 409 (SPEC §20/§26).
    """
    settings = settings if settings is not None else get_settings()

    def _export(session: Session) -> tuple[Any | None, approval.ApprovalDecision]:
        state = ckpt.load_state(session, run_id)
        if state is None:
            return None, approval.ApprovalDecision(approved=False, reason="run not found")
        stored_content = state.get("memo_content") or ""
        content_for_check = memo_content if memo_content is not None else stored_content
        decision = approval.approve(session, run_id, request_id, content_for_check, export_type)
        if not decision.approved:
            return ckpt.load_result(session, run_id), decision
        config = WorkflowConfig(
            session=session,
            run_row_id=ckpt.ensure_run_row(session, state),
            settings=settings,
            max_tool_calls=int(
                state.get("tool_budget", {}).get("max_tool_calls", settings.agent_max_tool_calls)
            ),
            max_transitions=int(
                state.get("tool_budget", {}).get(
                    "max_graph_transitions", settings.agent_max_graph_transitions
                )
            ),
            deadline_seconds=float(state.get("deadline_seconds", DEFAULT_DEADLINE_SECONDS)),
        )
        granted = {
            "approval_status": "granted",
            "export_type_requested": export_type,
        }
        merged = {**state, **granted}
        ckpt.persist_checkpoint(session, merged)
        session.commit()  # durable grant boundary before the export node runs
        workflow = build_workflow(config)
        final = workflow.invoke(merged, config={"recursion_limit": config.max_transitions + 8})
        return (
            ckpt.build_result(
                session,
                final,
                tool_call_limit=config.max_tool_calls,
                transition_limit=config.max_transitions,
            ),
            decision,
        )

    if session is not None:
        return _export(session)
    from quarterline.store.db import session_scope

    with session_scope() as owned_session:
        return _export(owned_session)


__all__ = [
    "CITATION_REQUIRED_HEADINGS",
    "DEFAULT_DEADLINE_SECONDS",
    "QUARTER_REVIEW_QUERY",
    "RISK_REVIEW_QUERY",
    "SimulatedCrash",
    "WorkflowConfig",
    "build_workflow",
    "default_price_provider",
    "default_search_service",
    "export_approved_memo",
    "resume_run",
    "run_memo_workflow",
]
