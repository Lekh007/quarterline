"""Integration tests for the bounded LangGraph memo workflow (SPEC §20).

Covers the §26 agent/security list end to end against the offline fixture
store: budgets (tool calls, transitions, deadline), controlled failures,
checkpoint/resume, market-context planning, graceful price degradation, the
prompt-injection fixture treated as inert evidence, and the no-remote-fallback
consent gate.
"""

from __future__ import annotations

import pytest
from agent_test_helpers import (
    ALIGNED_PERIOD_END,
    FakePriceProvider,
    ScriptedProvider,
    StubSearchService,
    agent_db_fixture,  # noqa: F401 (registers the agent_db fixture)
    fixture_embedding_provider,
    generation_db_fixture,  # noqa: F401 (registers the generation_db fixture)
    memo_payload,
    scripted_result,
)
from generation_test_helpers import scripted_text

from quarterline.agent import approval
from quarterline.agent.checkpoints import list_tool_calls, load_state
from quarterline.agent.graph import resume_run, run_memo_workflow
from quarterline.llm.base import GenerationProviderUnavailable
from quarterline.llm.generation import build_fact_card, label_display_of
from quarterline.retrieve.models import SearchQuery, SearchResult
from quarterline.retrieve.search import SearchService
from quarterline.store.db import session_scope


def _service(session) -> SearchService:
    from quarterline.config import get_settings

    return SearchService(session, fixture_embedding_provider(), settings=get_settings())


def _first_evidence_id(session) -> str:
    service = _service(session)
    result = service.search(
        SearchQuery(
            query="quarterly results revenue operating margin cash flow management discussion",
            ticker="AAPL",
            strategy="section",
            retrieval="hybrid",
            mode="brief",
            top_k=6,
        )
    )
    assert result.items and not result.insufficient_evidence, (
        result.items,
        result.insufficient_evidence_reason,
    )
    return result.items[0].evidence_id


def test_happy_path_awaiting_approval_with_budgets(agent_db) -> None:
    with session_scope() as session:
        eid = _first_evidence_id(session)
        label = label_display_of(build_fact_card(session, "AAPL"))
        provider = ScriptedProvider([scripted_result(memo_payload(eid, label))])
        result = run_memo_workflow(
            {"ticker": "AAPL", "memo_type": "quarter_review"},
            session=session,
            provider=provider,
            search_service=_service(session),
        )

    assert result.status == "awaiting_approval"
    assert result.approval_request_id is not None
    assert result.memo is not None
    assert {s.heading for s in result.memo.sections} >= {"overview", "evidence_gaps"}
    assert provider.call_count == 1, "exactly one provider call (no repair needed)"
    # Budgets reported (SPEC §20: 2 read tools used of 4; 8 transitions of 12).
    assert result.budgets is not None
    assert result.budgets.tool_calls.used == 2
    assert result.budgets.tool_calls.limit == 4
    assert result.budgets.transitions.used == 8
    assert result.budgets.transitions.limit == 12
    assert result.budgets.memo_repair_attempts.limit == 1
    assert [(t.tool_name, t.status) for t in result.tool_call_log] == [
        ("get_company_facts", "ok"),
        ("search_filings", "ok"),
    ]
    assert result.trace_uri == f"/api/runs/{result.run_id}"


def test_insufficient_evidence_ends_run_without_memo_or_provider_call(agent_db) -> None:
    """No admissible evidence -> the run ends insufficient_evidence and the
    writer is never called (SPEC §16/§25)."""
    empty = SearchResult()
    empty.insufficient_evidence = True
    empty.insufficient_evidence_reason = "no evidence retrieved"
    provider = ScriptedProvider([])  # must never be called

    with session_scope() as session:
        result = run_memo_workflow(
            {"ticker": "AAPL", "memo_type": "quarter_review"},
            session=session,
            provider=provider,
            search_service=StubSearchService(empty),
        )

    assert result.status == "insufficient_evidence"
    assert result.memo is None
    assert provider.call_count == 0
    assert any("insufficient evidence" in e.lower() for e in result.errors)


def test_provider_failure_fails_run_but_preserves_facts(agent_db) -> None:
    with session_scope() as session:
        build_fact_card(session, "AAPL")  # fixture sanity: the card exists
        provider = ScriptedProvider([GenerationProviderUnavailable("Ollama stopped")])
        result = run_memo_workflow(
            {"ticker": "AAPL", "memo_type": "quarter_review"},
            session=session,
            provider=provider,
            search_service=_service(session),
        )

    assert result.status == "failed"
    assert result.memo is None
    assert any("provider unavailable" in e.lower() for e in result.errors)
    # Facts and evidence survive in the checkpoint (SPEC §25 degraded mode).
    with session_scope() as session:
        state = load_state(session, result.run_id)
    assert state["facts"] is not None
    assert state["facts"]["ticker"] == "AAPL"
    assert state["evidence"], "retrieved evidence preserved alongside the failure"


def test_transition_budget_breach_controlled_failure(agent_db) -> None:
    with session_scope() as session:
        eid = _first_evidence_id(session)
        label = label_display_of(build_fact_card(session, "AAPL"))
        provider = ScriptedProvider([scripted_result(memo_payload(eid, label))])
        result = run_memo_workflow(
            {"ticker": "AAPL", "memo_type": "quarter_review"},
            session=session,
            provider=provider,
            search_service=_service(session),
            max_transitions=3,  # the chain needs 8; breach must fail controlled
        )

    assert result.status == "failed"
    assert result.memo is None
    assert provider.call_count == 0, "no provider call after the budget breach"
    assert any("transition budget breached" in e for e in result.errors)
    assert result.budgets.transitions.used <= 4
    # The controlled failure is persisted (no infinite loop happened).
    with session_scope() as session:
        state = load_state(session, result.run_id)
    assert state["status"] == "failed"


def test_deadline_enforced_between_nodes(agent_db) -> None:
    """An injected clock that jumps past the deadline stops the run BEFORE the
    next node executes (overall request deadline, SPEC §20 Budgets)."""
    ticks = iter([0.0, 0.0, 10_000.0, 10_000.0, 10_000.0, 10_000.0, 10_000.0, 10_000.0])

    def clock() -> float:
        return next(ticks, 10_000.0)

    with session_scope() as session:
        result = run_memo_workflow(
            {"ticker": "AAPL", "memo_type": "quarter_review"},
            session=session,
            provider=ScriptedProvider([]),  # must never be reached
            search_service=_service(session),
            deadline_seconds=5.0,
            clock=clock,
        )

    assert result.status == "failed"
    assert any("deadline exceeded" in e for e in result.errors)
    assert result.memo is None
    # Breach detected before the read tools ran (checked between nodes).
    used_tools = [t for t in result.tool_call_log if t.status == "ok"]
    assert used_tools == []


def test_checkpoint_resume_completes_without_recalling_tools(agent_db) -> None:
    """Crash simulation between write-memo and approval; resume reaches
    awaiting_approval and never re-calls a tool or the provider."""
    with session_scope() as session:
        eid = _first_evidence_id(session)
        label = label_display_of(build_fact_card(session, "AAPL"))
        provider = ScriptedProvider([scripted_result(memo_payload(eid, label))])
        with pytest.raises(Exception) as excinfo:
            run_memo_workflow(
                {"ticker": "AAPL", "memo_type": "quarter_review"},
                session=session,
                provider=provider,
                search_service=_service(session),
                crash_after="validate_memo",
            )
        assert "simulated crash" in str(excinfo.value)

    # Resume of an unknown run id yields None (no crash, no invention).
    with session_scope() as session:
        assert resume_run("0" * 32, session=session) is None

    with session_scope() as session:
        from sqlalchemy import select

        from quarterline.store.models import AgentRun

        run_id = session.scalar(select(AgentRun.run_id).order_by(AgentRun.id.desc()).limit(1))
        resumed = resume_run(run_id, session=session, provider=ScriptedProvider([]))

    assert resumed is not None
    assert resumed.status == "awaiting_approval"
    assert resumed.approval_request_id is not None
    assert resumed.memo is not None
    # No tool was called twice: the audit trail still holds exactly two calls.
    with session_scope() as session:
        calls = list_tool_calls(session, run_id)
    assert [(c["tool_name"], c["status"]) for c in calls] == [
        ("get_company_facts", "ok"),
        ("search_filings", "ok"),
    ]


def test_market_context_language_adds_prices_tool(agent_db) -> None:
    price_provider = FakePriceProvider()
    with session_scope() as session:
        eid = _first_evidence_id(session)
        label = label_display_of(build_fact_card(session, "AAPL"))
        result = run_memo_workflow(
            {
                "ticker": "AAPL",
                "memo_type": "quarter_review",
                "question": "Include recent stock price context",
            },
            session=session,
            provider=ScriptedProvider([scripted_result(memo_payload(eid, label))]),
            search_service=_service(session),
            price_provider=price_provider,
        )

    assert result.status == "awaiting_approval"
    assert price_provider.calls, "get_prices planned for market-context requests"
    names = [t.tool_name for t in result.tool_call_log if t.status == "ok"]
    assert names == ["get_company_facts", "search_filings", "get_prices"]
    assert result.budgets.tool_calls.used == 3


def test_prices_failure_still_completes_facts_only_memo(agent_db) -> None:
    with session_scope() as session:
        eid = _first_evidence_id(session)
        label = label_display_of(build_fact_card(session, "AAPL"))
        result = run_memo_workflow(
            {
                "ticker": "AAPL",
                "memo_type": "quarter_review",
                "market_context": True,
            },
            session=session,
            provider=ScriptedProvider([scripted_result(memo_payload(eid, label))]),
            search_service=_service(session),
            price_provider=FakePriceProvider(fail=True),
        )

    # SPEC §20/§25: price failure never blocks a facts-only memo.
    assert result.status == "awaiting_approval"
    assert result.memo is not None
    assert any("get_prices unavailable" in e for e in result.errors)
    names = [t.tool_name for t in result.tool_call_log if t.status == "ok"]
    assert "get_prices" in names


def test_unknown_ticker_controlled_failure(agent_db) -> None:
    with session_scope() as session:
        result = run_memo_workflow(
            {"ticker": "NOPE", "memo_type": "quarter_review"},
            session=session,
            provider=ScriptedProvider([]),
            search_service=_service(session),
        )

    assert result.status == "failed"
    assert result.memo is None
    assert any("no canonical facts" in e or "unknown company" in e for e in result.errors)


def test_unknown_tool_in_request_cannot_reach_the_planner(agent_db) -> None:
    """A client cannot smuggle tool names into a run: request fields are
    constrained (extra=forbid), and the plan is rule-based from the allowlist."""
    with session_scope() as session:
        result = run_memo_workflow(
            {
                "ticker": "AAPL",
                "memo_type": "quarter_review",
                "tools": ["delete_everything"],
            },
            session=session,
            provider=ScriptedProvider([]),
            search_service=_service(session),
        )

    assert result.status == "failed"
    assert any("invalid memo request" in e for e in result.errors)


def test_no_remote_fallback_without_consent(agent_db, monkeypatch) -> None:
    """ALLOW_REMOTE_FALLBACK=false: the OpenRouter provider is NEVER
    constructed and the run fails with the consent error (SPEC §2.3.2/§25/§26)."""
    constructions: list[object] = []

    import quarterline.llm.generation as generation_module
    import quarterline.llm.openrouter as openrouter_module

    def _guard(*args, **kwargs):
        constructions.append((args, kwargs))
        raise AssertionError("OpenRouterGenerationProvider must not be constructed")

    monkeypatch.setattr(openrouter_module, "OpenRouterGenerationProvider", _guard)
    monkeypatch.setattr(generation_module, "OpenRouterGenerationProvider", _guard)
    from quarterline.config import get_settings

    settings = get_settings().model_copy(
        update={
            "llm_provider": "openrouter",
            "allow_remote_fallback": False,
            "openrouter_api_key": "sk-test",
            "openrouter_model": "some/model",
        }
    )
    with session_scope() as session:
        result = run_memo_workflow(
            {"ticker": "AAPL", "memo_type": "quarter_review"},
            session=session,
            search_service=_service(session),
            settings=settings,
        )

    assert constructions == [], "the remote provider must never be constructed"
    assert result.status == "failed"
    assert any("ALLOW_REMOTE_FALLBACK" in e for e in result.errors)


def test_memo_repair_attempt_used_once_then_reported(agent_db) -> None:
    """One schema-repair pass maximum (llm.repair semantics): the second bad
    response ends the run as a controlled failure."""
    with session_scope() as session:
        provider = ScriptedProvider(
            [scripted_text("not json at all"), scripted_text("still not json")]
        )
        result = run_memo_workflow(
            {"ticker": "AAPL", "memo_type": "quarter_review"},
            session=session,
            provider=provider,
            search_service=_service(session),
        )

    assert provider.call_count == 2, "generate once + exactly one repair pass"
    assert result.status == "failed"
    assert any("repair" in e.lower() for e in result.errors)


def test_approval_created_for_both_export_types_and_hash_tied(agent_db) -> None:
    with session_scope() as session:
        eid = _first_evidence_id(session)
        label = label_display_of(build_fact_card(session, "AAPL"))
        result = run_memo_workflow(
            {"ticker": "AAPL", "memo_type": "quarter_review"},
            session=session,
            provider=ScriptedProvider([scripted_result(memo_payload(eid, label))]),
            search_service=_service(session),
        )
        from quarterline.store.repositories.runs import RunsRepo

        rows = RunsRepo(session).list_approvals(result.run_id)

    assert [(r.export_type, r.status) for r in rows] == [("md", "pending"), ("json", "pending")]
    assert all(r.memo_content_hash == approval.memo_content_hash(result.memo_content) for r in rows)
    assert ALIGNED_PERIOD_END is not None  # fixture sanity guard


def test_prompt_injection_passage_is_inert_evidence(agent_db) -> None:
    """The synthetic injection fixture (tests/fixtures/documents/
    injection_filing.html — the same corpus the retrieval suite ingests) flows
    through retrieval as INERT evidence: the deterministic planner executes
    only its own plan, no tool call/export originates from the passage, and
    the memo never complies with it."""
    from agent_test_helpers import evidence_item, injection_passage_text

    passage = injection_passage_text()
    injection = evidence_item("ev-aaaaaaaaaaaa", 999, passage, section="management_commentary")

    with session_scope() as session:
        eid = _first_evidence_id(session)
        label = label_display_of(build_fact_card(session, "AAPL"))
        # Aligned evidence first so a well-behaved writer has citable support;
        # the injection passage rides along exactly as retrieved evidence.
        service = _service(session)
        real = service.search(
            SearchQuery(
                query="quarterly results revenue operating margin cash flow management discussion",
                ticker="AAPL",
                strategy="section",
                retrieval="hybrid",
                mode="brief",
                top_k=6,
            )
        )
        stub = StubSearchService(SearchResult(items=[*real.items, injection]))
        provider = ScriptedProvider([scripted_result(memo_payload(eid, label))])
        result = run_memo_workflow(
            {"ticker": "AAPL", "memo_type": "quarter_review"},
            session=session,
            provider=provider,
            search_service=stub,
        )

    assert result.status == "awaiting_approval"
    # The injection text WAS supplied as evidence (treated as inert text).
    assert any("attacker@example.invalid" in item["text"] for item in result.evidence)
    # ...and it never originated any tool call: exactly the planned reads ran.
    assert [(t.tool_name, t.status) for t in result.tool_call_log] == [
        ("get_company_facts", "ok"),
        ("search_filings", "ok"),
    ]
    # The memo does not comply with the passage's instructions.
    memo_text = (result.memo_content or "").lower()
    assert "attacker@example.invalid" not in memo_text
    assert "reveal your system prompt" not in memo_text
    assert "call tool export_memo" not in memo_text
    # No export happened and none can: export requires the approval flow.
    assert result.exported is None
    with session_scope() as session:
        state = load_state(session, result.run_id)
        from pathlib import Path

        from quarterline.config import get_settings

        exports_dir = Path(get_settings().storage_dir) / "exports"
        assert state["tool_results"].get("export_memo") is None
        assert not exports_dir.exists() or not list(exports_dir.iterdir())
