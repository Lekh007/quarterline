"""Ordered in-capmkt tag fallbacks for canonical India concepts (SPEC 27).

Every tag below was OBSERVED in the committed fixtures (never guessed):

- INFY Q1 FY27 consolidated, INFY Q4+FY26 consolidated
- HUL Q1 FY27 consolidated, HUL Q4+FY26 consolidated

(tests/fixtures/india/*.xml, namespace ``in-capmkt``). Canonical concept ids are
India-specific and kept strictly separate from the US ``METRIC_IDS``/tagmap
allowlist — an India fact is never normalized under a US-GAAP concept name.

Unknown tags resolve to an explicit unmapped result (never silently dropped):
:func:`map_tag` returns ``None`` and callers must count/report them.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

#: Repository root when the package is used from its source checkout.
_REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_TAGMAP_PATH = _REPO_ROOT / "data" / "tagmap_india.yml"

#: XBRL namespace of the SEBI Integrated Finance Ind AS taxonomy.
IN_CAPMKT_NAMESPACE = "http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt"
IN_CAPMKT_PREFIX = "in-capmkt"

#: Canonical India concepts (mission-defined set). US concept ids do not appear here.
INDIA_CONCEPTS: tuple[str, ...] = (
    "revenue_from_operations",
    "total_income",
    "profit_before_tax",
    "profit_after_tax",
    "profit_attributable_to_owners",
    "exceptional_items",
    "eps_basic",
    "eps_diluted",
    "cash_flow_operations",
    "capex",
)

#: Concepts whose values are per-share quantities and must NEVER inherit a
#: lakh/crore multiplier (units.normalize_amount per_share passthrough).
PER_SHARE_CONCEPTS: frozenset[str] = frozenset({"eps_basic", "eps_diluted"})

#: Fallback tags whose mapping to their concept is uncertain. Documented here and
#: in docs/india_source_audit.md §IND-2; never silently merged into the primary tag.
UNCERTAIN_TAGS: frozenset[str] = frozenset(
    {
        "ProfitBeforeExceptionalItemsAndTax",
        "ProfitLossForPeriodFromContinuingOperations",
        "BasicEarningsLossPerShareFromContinuingOperations",
        "DilutedEarningsLossPerShareFromContinuingOperations",
        "PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities",
    }
)


@dataclass(frozen=True)
class MappingResult:
    """Outcome of mapping one raw tag (explicit for both mapped and unmapped)."""

    tag: str
    concept: str | None
    priority: int | None
    per_share: bool
    uncertain: bool
    mapped: bool

    @property
    def is_unmapped(self) -> bool:
        return not self.mapped


_UNMAPPED_CACHE: dict[str, MappingResult] = {}


def unmapped_result(tag: str) -> MappingResult:
    """Explicit unmapped outcome (unknown tags are reported, never dropped)."""
    if tag not in _UNMAPPED_CACHE:
        _UNMAPPED_CACHE[tag] = MappingResult(
            tag=tag,
            concept=None,
            priority=None,
            per_share=False,
            uncertain=False,
            mapped=False,
        )
    return _UNMAPPED_CACHE[tag]


@lru_cache(maxsize=4)
def _parse_tagmap(path: str) -> dict[str, tuple[str, ...]]:
    tagmap_path = Path(path)
    with tagmap_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise TypeError(f"tag map must be a mapping of concept -> tag list: {tagmap_path}")
    parsed: dict[str, tuple[str, ...]] = {}
    for concept, tags in raw.items():
        if not isinstance(tags, list) or not all(isinstance(tag, str) and tag for tag in tags):
            raise TypeError(f"tag list for concept {concept!r} must be non-empty strings")
        concept_key = str(concept)
        if concept_key not in INDIA_CONCEPTS:
            raise ValueError(
                f"concept {concept_key!r} is not in the India canonical allowlist "
                "(US concept ids must not leak into the India tagmap)"
            )
        parsed[concept_key] = tuple(tags)
    return parsed


def load_tagmap(path: str | Path | None = None) -> dict[str, list[str]]:
    """Load ``data/tagmap_india.yml`` as ``{concept: [tags in priority order]}``.

    Returns a fresh copy on every call so callers cannot mutate the cached map.
    """
    tagmap_path = str(Path(path)) if path else str(DEFAULT_TAGMAP_PATH)
    return {concept: list(tags) for concept, tags in _parse_tagmap(tagmap_path).items()}


@lru_cache(maxsize=1)
def _reverse_tagmap(path: str) -> dict[str, tuple[str, int]]:
    reversed_map: dict[str, tuple[str, int]] = {}
    for concept, tags in _parse_tagmap(path).items():
        for priority, tag in enumerate(tags):
            # First claim wins so a tag maps to exactly one canonical concept.
            reversed_map.setdefault(tag, (concept, priority))
    return reversed_map


def local_tag_name(qualified: str) -> str:
    """``in-capmkt:RevenueFromOperations`` -> ``RevenueFromOperations``."""
    return qualified.split(":", 1)[1] if ":" in qualified else qualified


def map_tag(tag: str, path: str | Path | None = None) -> MappingResult:
    """Map one raw in-capmkt tag (local or ``in-capmkt:``-qualified) to a concept.

    Unknown tags yield an explicit unmapped :class:`MappingResult` (``mapped=False``)
    so callers can count and surface them instead of silently dropping them.
    """
    tagmap_path = str(Path(path)) if path else str(DEFAULT_TAGMAP_PATH)
    local = local_tag_name(tag)
    claim = _reverse_tagmap(tagmap_path).get(local)
    if claim is None:
        return unmapped_result(local)
    concept, priority = claim
    return MappingResult(
        tag=local,
        concept=concept,
        priority=priority,
        per_share=concept in PER_SHARE_CONCEPTS,
        uncertain=local in UNCERTAIN_TAGS,
        mapped=True,
    )
