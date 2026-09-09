"""Deterministic research-only advice policy (SPEC §1 boundary, §12.2, §18
check 10, §26 "Advice refusal").

Two versioned, purely deterministic detectors (no LLM):

- :func:`detect_advice_request` — recognizes advice-SEEKING user questions
  (buy/sell/hold, "should I invest", "is it a good stock", "best stocks",
  "priced to buy", price targets). A matching request receives the
  research-only refusal (:data:`RESEARCH_ONLY_REFUSAL`) plus pointers to what
  the tool CAN do; status ``refused``. "Best stocks" language is refused
  (SPEC §12.2).
- :func:`detect_advice_content` — recognizes advice-ISSUING language inside
  generated content ("you should buy", "we recommend selling", a "price
  target"); matching generated statements are dropped by the validation gate.

Label/score pages always carry the caption "not a recommendation or forecast"
(the label pipeline attaches it; this module re-exports the constant for UI
reuse).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Version of the pattern lists below (bump when patterns change; recorded in
#: run events and refusal payloads).
ADVICE_POLICY_VERSION = "advice-policy-v1"

#: SPEC §12.1 caption that must accompany every label/score display.
NOT_A_RECOMMENDATION = "Rule-based quarterly performance label. Not a recommendation or forecast."

# -- advice-SEEKING requests (user questions) ---------------------------------
# Each entry: (detector label, compiled pattern). Word boundaries avoid
# "buyback"/"holdings" false positives; bare buy/sell/hold only trigger near
# investment context within the same sentence.
_ADVICE_REQUEST_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "should_i_buy_sell_hold_invest",
        re.compile(r"\bshould\s+(i|we|one|you)\s+(buy|sell|hold|invest)", re.IGNORECASE),
    ),
    (
        "is_it_a_good_stock",
        re.compile(r"\bis\s+(it|[a-z.]+)\s+a\s+good\s+(stock|investment|buy|pick)", re.IGNORECASE),
    ),
    ("good_investment", re.compile(r"\bgood\s+(stock|investment|pick|entry)\b", re.IGNORECASE)),
    (
        "best_stocks_language",
        re.compile(r"\b(best|top)\s+(stocks?|picks?|investments?|buy)\b", re.IGNORECASE),
    ),
    ("priced_to_buy_sell", re.compile(r"\bpriced\s+to\s+(buy|sell)\b", re.IGNORECASE)),
    (
        "recommend_buy_sell_hold",
        re.compile(
            r"\b(recommend|suggest)\w*\s+(buying|selling|holding|investing|a\s+buy"
            r"|a\s+sell)\b",
            re.IGNORECASE,
        ),
    ),
    ("worth_buying", re.compile(r"\bworth\s+(buying|investing|a\s+buy)\b", re.IGNORECASE)),
    ("price_target_request", re.compile(r"\b(price\s+target|target\s+price)\b", re.IGNORECASE)),
    (
        "invest_in_company",
        re.compile(
            r"\binvest(ing)?\s+in\s+(this\s+|the\s+)?(company|stock|shares?|business)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "buy_sell_hold_near_context",
        re.compile(
            r"\b(buy|sell|hold)\b[^.?!]{0,40}\b(stock|shares?|invest|position|portfolio)\b"
            r"|\b(stock|shares?|invest|position|portfolio)\b[^.?!]{0,40}\b(buy|sell|hold)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "undervalued_overvalued_trade",
        re.compile(
            r"\b(undervalued|overvalued|cheap\s+stock|short\s+(this|the)?\s*stock)\b", re.IGNORECASE
        ),
    ),
)

# -- advice-ISSUING content (generated statements) ----------------------------
_ADVICE_CONTENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "you_should_directive",
        re.compile(
            r"\byou\s+(should|must|may\s+want\s+to)\s+(buy|sell|hold|invest|avoid)", re.IGNORECASE
        ),
    ),
    (
        "recommendation_verb",
        re.compile(
            r"\b(we|i)\s+(recommend|suggest)\s+(buying|selling|holding|investing|to\s+buy"
            r"|to\s+sell|to\s+hold)",
            re.IGNORECASE,
        ),
    ),
    (
        "rating_label",
        re.compile(r"\b(buy|sell|hold)\s+rating\b|\brating:\s*(buy|sell|hold)\b", re.IGNORECASE),
    ),
    ("is_a_buy_sell", re.compile(r"\b(is|remains)\s+a\s+(buy|sell|hold)\b", re.IGNORECASE)),
    ("price_target_issued", re.compile(r"\bprice\s+target\s+(of|for|at)\b", re.IGNORECASE)),
    (
        "expectation_of_returns",
        re.compile(r"\b(guaranteed|expected)\s+(returns?|profit)\b", re.IGNORECASE),
    ),
)


@dataclass
class AdviceDecision:
    """Outcome of one deterministic policy evaluation."""

    is_advice: bool
    matched: list[str] = field(default_factory=list)
    policy_version: str = ADVICE_POLICY_VERSION


def _detect(text: str, patterns: tuple[tuple[str, re.Pattern[str]], ...]) -> AdviceDecision:
    matched = [label for label, pattern in patterns if pattern.search(text or "")]
    return AdviceDecision(is_advice=bool(matched), matched=matched)


def detect_advice_request(text: str) -> AdviceDecision:
    """True when the USER question seeks investment advice (v1 pattern list)."""
    return _detect(text, _ADVICE_REQUEST_PATTERNS)


def detect_advice_content(text: str) -> AdviceDecision:
    """True when GENERATED text issues investment advice (v1 pattern list)."""
    return _detect(text, _ADVICE_CONTENT_PATTERNS)


#: The research-only refusal shown for advice-seeking requests (SPEC §26:
#: "Advice requests receive a research-only refusal and alternative").
RESEARCH_ONLY_REFUSAL = (
    "Quarterline is a research tool and does not provide investment advice. "
    "It cannot tell you whether to buy, sell, or hold a stock, and it does not "
    "produce price targets or recommendations. "
    "Instead, you can: inspect the company's fact card and every metric's "
    "provenance, read the rule-based quarter label together with all of its "
    "inputs, use the explainable screener over your own criteria, and read the "
    "cited filing passages behind any statement. " + NOT_A_RECOMMENDATION
)

#: Short banner variant for generated-content panels.
RESEARCH_ONLY_BANNER = (
    "Research and education only. Not investment advice; no buy/sell "
    "recommendations or price targets."
)


__all__ = [
    "ADVICE_POLICY_VERSION",
    "NOT_A_RECOMMENDATION",
    "RESEARCH_ONLY_BANNER",
    "RESEARCH_ONLY_REFUSAL",
    "AdviceDecision",
    "detect_advice_content",
    "detect_advice_request",
]
