"""Numeric validation and deterministic metric rendering (SPEC §17, §18).

Two responsibilities:

1. **Metric-mention expansion (the happy path).** The model never types
   financial numbers; it selects an allowlisted ``metric_id`` + ``template``
   and Python renders the sentence from the fact card (Decimal-exact,
   SPEC §17 "Safer numerical output design"). Rendering happens here, BEFORE
   final response assembly, so the user never sees a model-typed number.

2. **Free-text numeric checking (defense in depth).** Numbers that DO appear
   in generated free text are extracted with a documented normalizer
   (currency symbols, thousands/millions/billions suffixes, % and pp,
   negative signs and accounting parentheses, rounded displays) and matched
   SEMANTICALLY against fact-card metrics: unit class, sign and scale must be
   consistent and the value must match within a maximum 0.5% relative
   tolerance AFTER the semantic match (SPEC §18). Zero is handled explicitly.
   Presence of the same digits elsewhere is NOT proof; metadata digits
   (evidence ids, ISO dates, fiscal-year labels, form numbers) are never
   treated as financial claims. A claim that cannot be reliably validated
   drops the WHOLE statement.

Free-text concept identity is inherently ambiguous — that limitation is
exactly why the metric-mention channel exists and is preferred. This module
records what it could and could not verify; it never claims that numeric
validation guarantees factual correctness (SPEC §2.2.6).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Literal, Protocol

from quarterline.core.models import FactCard, MetricStatus, MetricValue

#: Version of the normalizer, tolerance and template tables (cache/run meta).
FACTCHECK_VERSION = "factcheck-v1"

#: Maximum relative tolerance, applied ONLY after semantic matching (SPEC §18).
MAX_RELATIVE_TOLERANCE = Decimal("0.005")

ClaimKind = Literal["percent", "pp", "money", "shares", "eps", "ratio", "plain"]

#: Unit class each non-percent claim kind must match (semantic matching).
_UNIT_BY_KIND: dict[str, str] = {
    "pp": "percentage_points",
    "money": "USD",
    "eps": "USD/shares",
    "shares": "shares",
}


class MentionLike(Protocol):
    """Structural duck-type of ``llm.schemas.MetricMention`` (kept local so the
    core layer never imports the llm package)."""

    metric_id: str
    template: str


# ---------------------------------------------------------------------------
# Template validity (structural; SPEC §26 "valid number used as wrong metric")
# ---------------------------------------------------------------------------

#: Change-shaped metrics: the only ones that may use ``reported_change``.
CHANGE_METRICS: frozenset[str] = frozenset(
    {"revenue_yoy", "shares_yoy", "operating_margin_change_pp"}
)

#: Level metrics that may use ``reported_value`` (numeric card metrics that
#: are not change-shaped and not the categorical label).
LEVEL_VALUE_METRICS: frozenset[str] = frozenset(
    {
        "revenue",
        "gross_profit",
        "operating_income",
        "net_income",
        "diluted_eps",
        "shares_diluted",
        "cfo",
        "capex_outflow",
        "fcf",
        "cash",
        "total_assets",
        "total_liabilities",
        "long_term_debt",
        "long_term_net_debt_proxy",
        "current_ratio",
        "gross_margin",
        "operating_margin",
        "net_margin",
        "fcf_margin",
        "cfo_to_net_income",
        "fundamental_score",
        "data_coverage",
    }
)

#: Metrics with documented qualitative-level buckets for ``reported_level``.
LEVEL_WORDING_METRICS: frozenset[str] = frozenset(
    {
        "gross_margin",
        "operating_margin",
        "net_margin",
        "cfo_to_net_income",
        "current_ratio",
        "quarter_label",
        "data_coverage",
    }
)

#: Human names used in rendered sentences.
METRIC_DISPLAY_NAMES: dict[str, str] = {
    "revenue": "Revenue",
    "revenue_yoy": "Revenue",
    "gross_profit": "Gross profit",
    "gross_margin": "Gross margin",
    "operating_income": "Operating income",
    "operating_margin": "Operating margin",
    "operating_margin_change_pp": "Operating margin",
    "net_income": "Net income",
    "net_margin": "Net margin",
    "diluted_eps": "Diluted EPS",
    "shares_diluted": "Diluted shares",
    "shares_yoy": "Diluted shares",
    "cfo": "Operating cash flow",
    "capex_outflow": "Capital expenditure",
    "fcf": "Free cash flow",
    "fcf_margin": "Free-cash-flow margin",
    "current_ratio": "Current ratio",
    "cash": "Cash and cash equivalents",
    "total_assets": "Total assets",
    "total_liabilities": "Total liabilities",
    "long_term_debt": "Long-term debt",
    "long_term_net_debt_proxy": "Long-term net debt (proxy)",
    "cfo_to_net_income": "Cash conversion (CFO / net income)",
    "quarter_label": "Quarter label",
    "fundamental_score": "Experimental fundamental score",
    "data_coverage": "Data coverage",
}

PERCENT_DISPLAY_METRICS: frozenset[str] = frozenset(
    {
        "revenue_yoy",
        "shares_yoy",
        "gross_margin",
        "operating_margin",
        "net_margin",
        "fcf_margin",
        "data_coverage",
    }
)


# ---------------------------------------------------------------------------
# Metric-mention expansion
# ---------------------------------------------------------------------------


@dataclass
class RenderedFact:
    """One Python-rendered numeric sentence (never model-typed)."""

    metric_id: str
    template: str
    text: str


@dataclass
class ExpansionResult:
    rendered: list[RenderedFact] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


def _card_metric(card: FactCard, metric_id: str) -> MetricValue | None:
    return next((m for m in card.metrics if m.metric_id == metric_id), None)


def _quantize(value: Decimal, places: str) -> Decimal:
    return value.quantize(Decimal(places), rounding=ROUND_HALF_UP)


def format_value(metric: MetricValue) -> str:
    """Deterministic display of one fact-card metric (documented scales)."""
    value = metric.value
    unit = metric.unit or ""
    if metric.metric_id == "quarter_label":
        for ref in metric.provenance.derived_from:
            if ref.startswith("label:"):
                return ref.split(":", 1)[1]
        return "n/a"
    if value is None:
        return "n/a"
    if unit == "USD":
        return _format_money(value)
    if unit == "shares":
        return _format_scaled(value) + " shares"
    if unit == "USD/shares":
        return f"${_quantize(value, '0.01')} per share"
    if unit == "percentage_points":
        return f"{_quantize(value, '0.1'):+} pp" if value != 0 else "0.0 pp"
    if unit == "score":
        return f"{_quantize(value, '0.1')}"
    if unit == "ratio":
        if metric.metric_id in PERCENT_DISPLAY_METRICS:
            return f"{_quantize(value * 100, '0.1')}%"
        return f"{_quantize(value, '0.01')}x"
    return str(value)


def _format_money(value: Decimal) -> str:
    abs_value = abs(value)
    sign = "-" if value < 0 else ""
    if abs_value >= Decimal(1_000_000_000):
        return f"{sign}${_quantize(abs_value / Decimal(1_000_000_000), '0.1')} billion"
    if abs_value >= Decimal(1_000_000):
        return f"{sign}${_quantize(abs_value / Decimal(1_000_000), '0.1')} million"
    return f"{sign}${_quantize(abs_value, '0.01')}"


def _format_scaled(value: Decimal) -> str:
    abs_value = abs(value)
    sign = "-" if value < 0 else ""
    if abs_value >= Decimal(1_000_000_000):
        return f"{sign}{_quantize(abs_value / Decimal(1_000_000_000), '0.1')} billion"
    if abs_value >= Decimal(1_000_000):
        return f"{sign}{_quantize(abs_value / Decimal(1_000_000), '0.1')} million"
    return f"{sign}{_quantize(abs_value, '0.01')}"


def _render_change(metric: MetricValue) -> str:
    value = metric.value
    if value is None:
        return ""
    if metric.unit == "percentage_points":
        rendered = f"{_quantize(value, '0.1'):+} pp" if value != 0 else "0.0 pp"
        return f"changed {rendered} versus the prior-year quarter"
    # ratio displayed as percent (revenue_yoy / shares_yoy)
    rendered = f"{_quantize(value * 100, '0.1'):+}%" if value != 0 else "0.0%"
    return f"changed {rendered} year over year"


def _render_level(metric: MetricValue) -> str:
    value = metric.value
    metric_id = metric.metric_id
    if metric_id == "quarter_label":
        label = format_value(metric)
        return f"was rated {label} by the documented rule-based label"
    if value is None:
        return ""
    if metric_id == "cfo_to_net_income":
        if value >= Decimal("1.0"):
            return "was at or above cash-conversion parity (documented threshold 1.0)"
        if value >= Decimal("0.8"):
            return "was adequate relative to net income (documented threshold 0.8)"
        if value >= Decimal("0.4"):
            return "was below the documented 0.4 parity band used by the label veto"
        return "was in the weak band of the documented cash-conversion thresholds"
    if metric_id == "current_ratio":
        if value >= Decimal("2.0"):
            return "was at a comfortable level (documented threshold 2.0)"
        if value >= Decimal("1.0"):
            return "covered current liabilities at least once (documented threshold 1.0)"
        return "was below 1.0 (current liabilities exceeded current assets)"
    # margin-style ratios (documented SPEC §12.2 scale anchors)
    percent = value * 100
    if percent >= Decimal(30):
        return "was at a high level (documented scale anchor 30%)"
    if percent >= Decimal(10):
        return "was at a moderate level (documented scale anchor 10%)"
    if percent >= 0:
        return "was positive but low (documented scale anchors 0-30%)"
    return "was negative for the period"


def template_valid_for(metric_id: str, template: str) -> bool:
    """Structural check (SPEC §26): a template must fit the metric's shape."""
    if template == "reported_change":
        return metric_id in CHANGE_METRICS
    if template == "reported_value":
        return metric_id in LEVEL_VALUE_METRICS
    if template == "reported_level":
        return metric_id in LEVEL_WORDING_METRICS
    return False


def expand_metric_mentions(mentions: list[MentionLike], card: FactCard) -> ExpansionResult:
    """Render every valid mention into a Python-authored sentence.

    Invalid mentions (unknown template shape for the metric, metric not ``ok``,
    or value missing) are dropped with recorded issues — never silently.
    """
    result = ExpansionResult()
    for mention in mentions or []:
        metric = _card_metric(card, mention.metric_id)
        name = METRIC_DISPLAY_NAMES.get(mention.metric_id, mention.metric_id)
        if metric is None:
            result.issues.append(
                f"metric_mention {mention.metric_id!r} is not part of this fact card"
            )
            continue
        if not template_valid_for(mention.metric_id, mention.template):
            result.issues.append(
                f"metric_mention {mention.metric_id!r} template {mention.template!r} is "
                "not structurally valid for this metric"
            )
            continue
        if metric.status is not MetricStatus.ok:
            result.issues.append(
                f"metric_mention {mention.metric_id!r} is unavailable "
                f"(status {metric.status.value}); missing stays missing"
            )
            continue
        if metric.value is None and mention.metric_id != "quarter_label":
            # quarter_label is categorical: its value is the label reference.
            result.issues.append(
                f"metric_mention {mention.metric_id!r} has no value; missing stays missing"
            )
            continue
        if mention.template == "reported_change":
            text = f"{name} {_render_change(metric)}."
        elif mention.template == "reported_value":
            text = f"{name} was {format_value(metric)} for the reported period."
        else:
            text = f"{name} {_render_level(metric)}."
        result.rendered.append(
            RenderedFact(metric_id=mention.metric_id, template=mention.template, text=text)
        )
    return result


# ---------------------------------------------------------------------------
# Free-text numeric claim extraction + semantic checking
# ---------------------------------------------------------------------------


@dataclass
class NumericClaim:
    raw: str
    value: Decimal
    kind: ClaimKind


# Masked BEFORE scanning: metadata digits are not financial claims (SPEC §18).
_MASKS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("evidence_id", re.compile(r"\[?ev-[0-9a-f]{12}\]?")),
    # accession-like strings must be masked BEFORE the ISO-date pattern so a
    # fragment like "0193-26-00" inside them is not mistaken for a date.
    ("accession_like", re.compile(r"\b\d{3,}[-]\d{2,}[-]\d{3,}\b")),
    ("iso_date", re.compile(r"\b\d{4}-\d{2}-\d{2}\b")),
    (
        "month_day_year",
        re.compile(
            r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}",
            re.IGNORECASE,
        ),
    ),
    (
        "day_month_year",
        re.compile(
            r"\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}",
            re.IGNORECASE,
        ),
    ),
    ("fiscal_label", re.compile(r"\bFY\s?(19|20)\d{2}\b", re.IGNORECASE)),
    ("quarter_label", re.compile(r"\b(19|20)\d{2}\s+Q[1-4]\b", re.IGNORECASE)),
    ("bare_year", re.compile(r"\b(18|19|20|21)\d{2}\b")),
    (
        "form_number",
        re.compile(
            r"\b(?:10|20)-[KQ]\b|\bForm\s+\d+\b|\bEX-\d+(?:\.\d+)?\b"
            r"|\bItem\s+\d+(?:\.\d+)?\b|\bPart\s+(?:I{1,3}|IV)\b",
            re.IGNORECASE,
        ),
    ),
)

_NUMBER_RE = re.compile(
    r"""
    (?P<open>\()?\s*
    (?P<currency>[$€£])?\s*
    (?P<negative>-)?\s*
    (?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)
    (?:\s?(?P<word_scale>thousand|million|billion|mil|bn|mm)\b
       |(?P<letter_scale>[kKmMbB])(?![a-zA-Z]))?
    (?:\s?(?P<percent>%|percent(?:age)?\s+points?|percent))?
    (?:\s?(?P<pp>pp|p\.p\.|percentage\s+points?))?
    \s*(?P<close>\))?
    """,
    re.VERBOSE | re.IGNORECASE,
)

_WORD_SCALES = {
    "thousand": Decimal(1_000),
    "million": Decimal(1_000_000),
    "mil": Decimal(1_000_000),
    "billion": Decimal(1_000_000_000),
    "bn": Decimal(1_000_000_000),
    "mm": Decimal(1_000_000),
}
_LETTER_SCALES = {"k": Decimal(1_000), "m": Decimal(1_000_000), "b": Decimal(1_000_000_000)}

_SHARES_CONTEXT = re.compile(r"\bshares?\b|\bper\s+share\b", re.IGNORECASE)
_EPS_CONTEXT = re.compile(r"\bper\s+share\b", re.IGNORECASE)


def _mask(text: str) -> tuple[str, list[str]]:
    masked_kinds: list[str] = []
    masked = text or ""
    for kind, pattern in _MASKS:
        masked, count = pattern.subn(lambda m: " " + "#" * len(m.group(0)) + " ", masked)
        if count:
            masked_kinds.append(kind)
    return masked, masked_kinds


def _classify(match: re.Match[str]) -> NumericClaim | None:
    num_text = match.group("num").replace(",", "")
    try:
        value = Decimal(num_text)
    except InvalidOperation:  # pragma: no cover - regex guarantees digits
        return None
    negative = bool(match.group("negative"))
    if match.group("open") and match.group("close"):
        negative = True  # accounting parentheses
    if negative:
        value = -value
    scale = Decimal(1)
    if match.group("word_scale"):
        scale = _WORD_SCALES[match.group("word_scale").lower()]
    elif match.group("letter_scale"):
        scale = _LETTER_SCALES[match.group("letter_scale").lower()]
    value = value * scale

    percent = match.group("percent")
    pp = match.group("pp")
    context = _context(match)
    if percent and re.fullmatch(r"percent(?:age)?\s+points?", percent, re.IGNORECASE):
        return NumericClaim(raw=match.group(0).strip(), value=value, kind="pp")
    if pp:
        return NumericClaim(raw=match.group(0).strip(), value=value, kind="pp")
    if percent:
        return NumericClaim(raw=match.group(0).strip(), value=value, kind="percent")
    if match.group("currency"):
        kind = "eps" if _EPS_CONTEXT.search(context) else "money"
        return NumericClaim(raw=match.group(0).strip(), value=value, kind=kind)
    if _SHARES_CONTEXT.search(context):
        return NumericClaim(raw=match.group(0).strip(), value=value, kind="shares")
    return NumericClaim(raw=match.group(0).strip(), value=value, kind="plain")


def _context(match: re.Match[str]) -> str:
    """Text around the match: per-share/share words may sit BEFORE the number
    ("Diluted shares were 15.2 billion") as well as after it."""
    text = match.string
    behind = text[max(0, match.start() - 40) : match.start()]
    ahead = text[match.end() : match.end() + 40]
    return f"{behind} | {ahead}"


def extract_numeric_claims(text: str) -> list[NumericClaim]:
    """Extract normalized numeric claims from free text (documented rules).

    Metadata digits (evidence ids, dates, fiscal labels, form/item numbers,
    accession-like strings, bare years) are masked first and never become
    claims. Suffix scales k/m/b and the words thousand/million/billion are
    applied; signs include accounting parentheses, with the documented
    currency-wrapped form ``$(2.0)`` normalized to ``($2.0)`` before scanning.
    """
    masked, _kinds = _mask(text or "")
    # Accounting parentheses (negative by convention) are rewritten to the
    # equivalent minus form BEFORE scanning so following scale words compose
    # naturally: "$(2.0) million" -> "-$2.0 million".
    masked = re.sub(r"\(\s*([$€£]?)\s*([\d,]+(?:\.\d+)?)\s*\)", r"-\1\2", masked)
    claims: list[NumericClaim] = []
    for match in _NUMBER_RE.finditer(masked):
        raw = match.group(0)
        if not re.search(r"\d", raw):
            continue
        claim = _classify(match)
        if claim is not None:
            claims.append(claim)
    return claims


def _candidate_targets(card: FactCard, kind: ClaimKind) -> list[tuple[str, Decimal]]:
    """Semantically compatible (metric_id, comparable-value) pairs.

    Percent claims compare against ratio metrics displayed as percent
    (value x 100); pp claims against percentage_points metrics; money claims
    against USD metrics; eps against USD/shares; shares against shares; plain
    and ratio claims against non-percent ratio metrics. Concept identity is
    NOT claimable from free text — unit class, sign and scale are (see module
    docstring).
    """
    targets: list[tuple[str, Decimal]] = []
    for metric in card.metrics:
        if metric.status is not MetricStatus.ok or metric.value is None:
            continue
        metric_id = metric.metric_id
        unit = metric.unit or ""
        value = metric.value
        if kind == "percent" and unit == "ratio" and metric_id in PERCENT_DISPLAY_METRICS:
            targets.append((metric_id, value * 100))
            continue
        if kind in ("ratio", "plain"):
            compatible = unit == "ratio" and metric_id not in PERCENT_DISPLAY_METRICS
        else:
            compatible = unit == _UNIT_BY_KIND.get(kind)
        if compatible:
            targets.append((metric_id, value))
    return targets


def _matches_target(claim: NumericClaim, target: Decimal) -> bool:
    """Tolerance rule: <=0.5% relative after semantic matching, plus explicit
    zero handling and display-rounding respect (SPEC §18)."""
    if claim.value == 0 or target == 0:
        return claim.value == target  # zero matches only zero exactly
    if (claim.value < 0) != (target < 0):
        return False  # sign must be consistent where determinable
    relative = abs(claim.value - target) / abs(target)
    if relative <= MAX_RELATIVE_TOLERANCE:
        return True
    # Respect display-rounding precision: a claim equal to the metric rounded
    # to 1 or 2 decimals (the rendered displays) is a rounded display.
    for places in ("0.1", "0.01"):
        if _quantize(target, places) == _quantize(claim.value, places) and relative <= Decimal(
            "0.05"
        ):
            return True
    return False


def check_statement_numbers(text: str, card: FactCard) -> list[str]:
    """Validate every free-text numeric claim in one statement.

    Returns rejection reasons; empty list = every claim is supported by a
    semantically compatible fact-card metric within tolerance. A claim with no
    compatible metric is unsupported -> the caller drops the WHOLE statement
    (SPEC §2.2.5).
    """
    reasons: list[str] = []
    for claim in extract_numeric_claims(text):
        targets = _candidate_targets(card, claim.kind)
        if not targets:
            reasons.append(
                f"numeric_consistency: {claim.raw!r} has no semantically compatible "
                f"fact-card metric (kind {claim.kind}); unsupported claim"
            )
            continue
        if not any(_matches_target(claim, target) for _metric_id, target in targets):
            reasons.append(
                f"numeric_consistency: {claim.raw!r} matches no {claim.kind}-class "
                "fact-card metric within the documented tolerance; unsupported claim"
            )
    return reasons


__all__ = [
    "CHANGE_METRICS",
    "FACTCHECK_VERSION",
    "LEVEL_VALUE_METRICS",
    "LEVEL_WORDING_METRICS",
    "MAX_RELATIVE_TOLERANCE",
    "METRIC_DISPLAY_NAMES",
    "ExpansionResult",
    "NumericClaim",
    "RenderedFact",
    "check_statement_numbers",
    "expand_metric_mentions",
    "extract_numeric_claims",
    "format_value",
    "template_valid_for",
]
