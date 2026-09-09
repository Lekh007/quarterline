"""End-to-end generation pipeline tests over the offline fixture store.

Covers the SPEC §26 generation list on the real pipeline: valid provider
response, single repair pass, cache hit, provider-unavailable degradation,
insufficient evidence, and the ask flow (advice refusal + grounded answer).
The provider is the ScriptedProvider fake — no live model anywhere.
"""

from __future__ import annotations

import json
from pathlib import Path

from generation_test_helpers import (  # noqa: F401 (registers generation_db)
    ALIGNED_PERIOD_END,
    ScriptedProvider,
    document_periods,
    fixture_embedding_provider,
    generation_db_fixture,
    scripted_result,
    scripted_text,
)

from quarterline.core.factcheck import (
    expand_metric_mentions,
    template_valid_for,
)
from quarterline.core.models import MetricStatus
from quarterline.core.provenance import build_fact_card
from quarterline.llm.base import GenerationProviderUnavailable
from quarterline.llm.generation import (
    BRIEF_RETRIEVAL_QUERY,
    answer_question,
    generate_brief,
    label_display_of,
)
from quarterline.llm.schemas import MetricMention
from quarterline.retrieve.context import assemble_context
from quarterline.retrieve.context import (
    context_budget_from_settings as _context_budget,
)
from quarterline.retrieve.models import SearchQuery
from quarterline.retrieve.search import SearchService
from quarterline.store.db import session_scope


class StubSearchService:
    """Deterministic stand-in returning one fixed SearchResult."""

    def __init__(self, result) -> None:
        self.result = result

    def search(self, query: SearchQuery):
        return self.result


def _service(session) -> SearchService:
    from quarterline.config import get_settings

    return SearchService(session, fixture_embedding_provider(), settings=get_settings())


def _bundle(session, query_text: str, ticker: str, *, mode: str, period_end=None):
    service = _service(session)
    result = service.search(
        SearchQuery(
            query=query_text,
            ticker=ticker,
            strategy="section",
            retrieval="hybrid",
            mode=mode,  # type: ignore[arg-type]
            period_end=period_end,
            top_k=12,
        )
    )
    return result, assemble_context(result.items, _context_budget(service.settings))


def _valid_payload(session) -> tuple[dict, object]:
    """A model response that should survive the whole gate on this corpus."""

    card = build_fact_card(session, "AAPL")
    _result, bundle = _bundle(
        session, BRIEF_RETRIEVAL_QUERY, "AAPL", mode="brief", period_end=card.period_end
    )
    evidence_id = bundle.passages[0].evidence_id
    mention = None
    for metric_id, template in (
        ("revenue_yoy", "reported_change"),
        ("operating_margin_change_pp", "reported_change"),
        ("gross_margin", "reported_value"),
        ("operating_margin", "reported_value"),
    ):
        metric = next((m for m in card.metrics if m.metric_id == metric_id), None)
        if (
            metric is not None
            and metric.status is MetricStatus.ok
            and metric.value is not None
            and template_valid_for(metric_id, template)
        ):
            mention = {"metric_id": metric_id, "template": template}
            break
    assert mention is not None, "fixture card must expose at least one ok metric"
    return {
        "status": "ok",
        "label_echo": label_display_of(card),
        "metric_mentions": [mention],
        "bullets": [
            {
                "text": f"Management attributed services growth to the quarter's "
                f"results. [{evidence_id}]",
                "evidence_ids": [evidence_id],
            }
        ],
        "risks": [
            {
                "text": f"Management identified supply chain concentration as an "
                f"area of attention. [{evidence_id}]",
                "evidence_ids": [evidence_id],
            }
        ],
        "open_questions": [
            {
                "text": "Would independent evidence confirm that the services trend is recurring?",
                "evidence_ids": [],
            }
        ],
    }, card


def test_valid_provider_response_ok_brief_with_rendered_numbers(generation_db) -> None:
    with session_scope() as session:
        payload, card = _valid_payload(session)
        provider = ScriptedProvider([scripted_result(payload)])
        outcome = generate_brief(
            "AAPL", session=session, provider=provider, search_service=_service(session)
        )

    assert provider.call_count == 1
    assert outcome.status == "ok"
    assert outcome.cache_hit is False
    assert outcome.brief is not None
    assert outcome.brief.label_echo == label_display_of(card)
    assert outcome.brief.bullets, "valid bullet must survive"
    assert outcome.metric_facts, "metric mention must be expanded by Python"
    # rendered numbers are Decimal-exact against the card
    mention = payload["metric_mentions"][0]
    expansion = expand_metric_mentions([MetricMention(**mention)], card)
    assert expansion.rendered, "mention must render against the fixture card"
    assert outcome.metric_facts[0] == expansion.rendered[0].text
    # every cited id resolves against the SUPPLIED evidence
    supplied = {item.evidence_id for item in outcome.evidence}
    for bullet in outcome.brief.bullets:
        assert set(bullet.evidence_ids) <= supplied
        assert bullet.text.count("[") == len(bullet.evidence_ids)
    # the misaligned document never supplied evidence for this period
    periods = document_periods()
    for item in outcome.evidence:
        assert periods[item.document_id] == ALIGNED_PERIOD_END


def test_single_repair_pass_invalid_then_valid(generation_db, tmp_path) -> None:
    with session_scope() as session:
        payload, _card = _valid_payload(session)
        provider = ScriptedProvider(
            [scripted_text("this is not valid json at all"), scripted_result(payload)]
        )
        outcome = generate_brief(
            "AAPL", session=session, provider=provider, search_service=_service(session)
        )

    assert outcome.status == "ok"
    assert provider.call_count == 2, "exactly one repair pass after the first call"
    # run events recorded the repair (C12 observability)
    events_file = Path(tmp_path / "storage" / "logs" / "events.jsonl")
    assert events_file.is_file()
    events = [json.loads(line) for line in events_file.read_text("utf-8").splitlines()]
    brief_events = [
        event
        for event in events
        if event.get("endpoint") == "brief" and event.get("answer_status") == "ok"
    ]
    assert brief_events, events
    repair_event = brief_events[-1]
    assert repair_event["repair_used"] is True
    assert repair_event["json_valid_first_pass"] is False
    assert repair_event["json_valid"] is True
    assert repair_event["retained_bullet_count"] == 1
    assert repair_event["citation_valid"] is True


def test_cache_hit_second_call_skips_provider(generation_db) -> None:
    with session_scope() as session:
        payload, _card = _valid_payload(session)
        first = ScriptedProvider([scripted_result(payload)])
        outcome_1 = generate_brief(
            "AAPL", session=session, provider=first, search_service=_service(session)
        )
        assert first.call_count == 1
        second = ScriptedProvider([])  # would raise if called
        outcome_2 = generate_brief(
            "AAPL", session=session, provider=second, search_service=_service(session)
        )

    assert outcome_1.status == "ok"
    assert outcome_2.cache_hit is True
    assert second.call_count == 0, "cache hit must not call the provider"
    assert outcome_2.brief is not None and outcome_1.brief is not None
    assert outcome_2.brief.bullets == outcome_1.brief.bullets
    assert outcome_2.metric_facts == outcome_1.metric_facts


def test_provider_unavailable_degrades_to_facts_and_evidence(generation_db) -> None:
    with session_scope() as session:
        provider = ScriptedProvider([GenerationProviderUnavailable("ollama down for tests")])
        outcome = generate_brief(
            "AAPL", session=session, provider=provider, search_service=_service(session)
        )

    assert outcome.status == "provider_unavailable"
    assert outcome.brief is None
    assert outcome.facts, "fact card still shown"
    assert outcome.evidence, "retrieved evidence still shown"
    assert any("provider unavailable" in reason.lower() for reason in outcome.reasons)


def test_insufficient_evidence_abstains_without_provider_call(generation_db) -> None:
    from quarterline.retrieve.models import SearchResult

    with session_scope() as session:
        stub = StubSearchService(
            SearchResult(
                items=[],
                insufficient_evidence=True,
                insufficient_evidence_reason="no evidence retrieved",
            )
        )
        provider = ScriptedProvider([])  # would raise if called
        outcome = generate_brief("AAPL", session=session, provider=provider, search_service=stub)

    assert outcome.status == "insufficient_evidence"
    assert provider.call_count == 0, "abstention must not call the provider"
    assert any("insufficient evidence" in reason for reason in outcome.reasons)


def test_ask_advice_refusal_without_provider_call(generation_db) -> None:
    with session_scope() as session:
        provider = ScriptedProvider([])  # would raise if called
        outcome = answer_question(
            "Should I buy AAPL right now?",
            "AAPL",
            session=session,
            provider=provider,
            search_service=_service(session),
        )

    assert outcome.status == "refused"
    assert provider.call_count == 0
    assert outcome.answer is not None
    assert "does not provide investment advice" in outcome.answer.text
    assert outcome.advice_policy_version == "advice-policy-v1"


def test_ask_insufficient_evidence_no_provider_call(generation_db) -> None:
    with session_scope() as session:
        provider = ScriptedProvider([])
        outcome = answer_question(
            "zzzqquirky quantum flux capacitor warranty terms?",
            "AAPL",
            session=session,
            provider=provider,
            search_service=_service(session),
        )

    assert outcome.status == "insufficient_evidence"
    assert provider.call_count == 0


def test_ask_grounded_answer_with_resolvable_citation(generation_db) -> None:
    question = "What did management say about revenue growth and margin?"
    with session_scope() as session:
        _result, bundle = _bundle(session, question, "AAPL", mode="general")
        evidence_id = bundle.passages[0].evidence_id
        provider = ScriptedProvider(
            [
                scripted_result(
                    {
                        "status": "ok",
                        "text": f"Management stated that revenue grew on services strength. "
                        f"[{evidence_id}]",
                        "evidence_ids": [evidence_id],
                    }
                )
            ]
        )
        outcome = answer_question(
            question,
            "AAPL",
            session=session,
            provider=provider,
            search_service=_service(session),
        )

    assert provider.call_count == 1
    assert outcome.status == "ok"
    assert outcome.answer is not None
    assert outcome.answer.evidence_ids == [evidence_id]
    supplied = {item.evidence_id for item in outcome.evidence}
    assert set(outcome.answer.evidence_ids) <= supplied


def test_ask_without_ticker_returns_explicit_company_scope_notice(generation_db) -> None:
    with session_scope() as session:
        outcome = answer_question(
            "What changed this quarter?",
            None,
            session=session,
            provider=ScriptedProvider([]),
        )
    assert outcome.status == "insufficient_evidence"
    assert any("no ticker supplied" in reason for reason in outcome.reasons)
