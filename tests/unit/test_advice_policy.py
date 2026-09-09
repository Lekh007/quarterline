"""Advice policy tests (SPEC §12.2, §18 check 10, §26 advice refusal)."""

from __future__ import annotations

import pytest

from quarterline.core.advice_policy import (
    ADVICE_POLICY_VERSION,
    NOT_A_RECOMMENDATION,
    RESEARCH_ONLY_REFUSAL,
    detect_advice_content,
    detect_advice_request,
)


@pytest.mark.parametrize(
    "question",
    [
        "Should I buy AAPL?",
        "should i invest in this company",
        "Is AAPL a good stock?",
        "What are the best stocks right now?",
        "Is it priced to buy here?",
        "Is Microsoft worth buying?",
        "Give me a price target for AAPL.",
        "Should we sell the stock now?",
        "Is the stock undervalued?",
        "do you recommend holding shares",
    ],
)
def test_advice_seeking_requests_detected(question: str) -> None:
    decision = detect_advice_request(question)
    assert decision.is_advice, question
    assert decision.matched
    assert decision.policy_version == ADVICE_POLICY_VERSION


@pytest.mark.parametrize(
    "question",
    [
        "What did management say about revenue this quarter?",
        "How did gross margin change year over year?",
        "Summarize the risk factors in the latest 10-Q.",
        "What was the operating cash flow trend?",
    ],
)
def test_research_questions_not_flagged(question: str) -> None:
    assert detect_advice_request(question).is_advice is False


def test_buyback_wording_not_flagged() -> None:
    # word boundaries: "buyback"/"holdings" are not buy/hold advice.
    assert (
        detect_advice_request("What did the company say about its buyback program?").is_advice
        is False
    )


@pytest.mark.parametrize(
    "statement",
    [
        "You should buy the stock before earnings. [ev-0123456789ab]",
        "We recommend selling the position. [ev-0123456789ab]",
        "The stock is a buy at current levels. [ev-0123456789ab]",
        "Our price target of 250 implies upside. [ev-0123456789ab]",
        "Investors can expect guaranteed returns. [ev-0123456789ab]",
    ],
)
def test_generated_advice_content_detected(statement: str) -> None:
    decision = detect_advice_content(statement)
    assert decision.is_advice, statement


def test_factual_content_not_flagged_as_advice() -> None:
    decision = detect_advice_content(
        "Management stated that revenue grew on services. [ev-0123456789ab]"
    )
    assert decision.is_advice is False


def test_refusal_text_contains_research_only_alternative_and_disclaimer() -> None:
    lowered = RESEARCH_ONLY_REFUSAL.lower()
    assert "does not provide investment advice" in lowered
    assert "provenance" in lowered or "fact card" in lowered
    assert "screener" in lowered or "quarter label" in lowered
    assert NOT_A_RECOMMENDATION in RESEARCH_ONLY_REFUSAL
