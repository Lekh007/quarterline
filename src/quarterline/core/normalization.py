"""Financial fact normalization (SPEC 10, Milestone 2).

Pipeline over raw companyfacts payloads:

1. ``build_observation_drafts`` — turn raw XBRL observations into
   ``ObservationDraft`` rows (raw ``fy``/``fp`` kept in context metadata;
   ``canonical_concept`` from the tag map; deterministic ``observation_hash``).
2. ``select_facts`` — choose one value per (concept, economic period, scope),
   applying tag priority ONLY AFTER filtering for compatible unit, scope and
   economic period. Grouping by economic period identity makes it structurally
   impossible for a higher-priority annual value to displace a quarterly one.
3. ``derive_missing_quarters`` — additive flow concepts only:
   Q2 = 6m − Q1, Q3 = 9m − 6m, Q4 = annual − 9m, under ALL conditions of
   SPEC 10.3 (same company/concept/unit/scope, compatible accounting basis and
   revision vintage, correct consecutive fiscal periods). EPS, weighted shares,
   ratios, margins and instant balance-sheet values are NEVER derived by
   subtraction; if direct quarterly values are missing they stay missing.

Selection policies: ``latest_available`` and ``as_of(timestamp)``; an as-of
selection never sees an observation filed after the timestamp (SPEC 10.4).
Missing concepts are logged, never filled (SPEC 10.1). All money math is
``Decimal``.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from quarterline.core.models import PeriodKind
from quarterline.core.periods import (
    FY_QUARTERS,
    fiscal_year_of,
    fiscal_year_of_duration_end,
    period_kind,
    quarter_index_from_days_into_year,
    quarter_index_in_year,
)

#: Bumped whenever normalization semantics change; persisted on every fact row.
NORMALIZATION_VERSION = "norm-v1"

SELECTION_LATEST = "latest_available"
SELECTION_AS_OF = "as_of"

#: Unit compatibility per concept (SPEC 10.1: priority applies only after unit
#: filtering). Values in any other unit are excluded, never converted.
CONCEPT_UNITS: dict[str, frozenset[str]] = {
    "revenue": frozenset({"USD"}),
    "gross_profit": frozenset({"USD"}),
    "operating_income": frozenset({"USD"}),
    "net_income": frozenset({"USD"}),
    "diluted_eps": frozenset({"USD/shares"}),
    "shares_diluted": frozenset({"shares"}),
    "cfo": frozenset({"USD"}),
    "capex_outflow": frozenset({"USD"}),
    "cash": frozenset({"USD"}),
    "total_assets": frozenset({"USD"}),
    "total_liabilities": frozenset({"USD"}),
    "long_term_debt": frozenset({"USD"}),
    "current_assets": frozenset({"USD"}),
    "current_liabilities": frozenset({"USD"}),
}

#: Additive flow concepts eligible for YTD/annual subtraction (SPEC 10.3).
ADDITIVE_FLOW_CONCEPTS: frozenset[str] = frozenset(
    {"revenue", "gross_profit", "operating_income", "net_income", "cfo", "capex_outflow"}
)

#: Concepts that must never be derived by subtraction. Instant balance-sheet
#: values, EPS and weighted shares: if direct quarterly values are missing they
#: stay missing (SPEC 10.3 "Do not derive this way").
NEVER_DERIVE_CONCEPTS: frozenset[str] = frozenset(CONCEPT_UNITS) - ADDITIVE_FLOW_CONCEPTS

#: Consolidated scope: companyfacts XBRL data is the consolidated issuer view.
DEFAULT_SCOPE = "consolidated"

#: Two derivation inputs are revision-compatible when filed within ~one quarter
#: of each other (they normally appear in the same 10-Q). This stops a
#: pre-restatement YTD being subtracted from a post-restatement annual total
#: (which would silently attribute the whole restatement to one quarter).
REVISION_VINTAGE_WINDOW_DAYS = 100

#: Annual-vs-fiscal-year identity: cumulative durations accepted as the 6m/9m
#: rungs of the derivation ladder.
YTD_6_MONTH_DAYS = (168, 195)
YTD_9_MONTH_DAYS = (255, 290)


@dataclass(frozen=True)
class ObservationRecord:
    """A raw reported observation (draft or persisted)."""

    observation_id: int | None
    company_id: int
    cik: str
    taxonomy: str
    original_tag: str
    canonical_concept: str
    tag_priority: int
    value: Decimal | None
    unit: str | None
    period_start: date | None
    period_end: date | None
    period_kind: PeriodKind
    accession: str | None
    form: str | None
    filed_at: date | None
    source_fy: int | None
    source_fp: str | None
    frame: str | None = None
    reporting_scope: str | None = DEFAULT_SCOPE
    context_metadata_json: str | None = None
    #: Earliest filing date of ANY observation with the same reported value for
    #: the same (tag, unit, period). Revision vintage attaches to the VALUE:
    #: a later re-report of an unchanged value does not move the vintage.
    first_filed_at: date | None = None


@dataclass(frozen=True)
class FactSource:
    """One lineage input of a selected fact."""

    observation: ObservationRecord
    role: str  # direct_source | annual_total | prior_ytd


@dataclass
class SelectedFact:
    """One normalized fact value for a (concept, economic period, scope)."""

    concept: str
    period_start: date | None
    period_end: date
    period_kind: PeriodKind
    fiscal_year: int | None
    fiscal_quarter: str | None
    reporting_scope: str
    value: Decimal | None
    unit: str | None
    is_derived: bool
    derivation_method: str | None
    selection_policy: str
    sources: list[FactSource] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def identity(self) -> tuple:
        return (
            self.concept,
            self.period_start,
            self.period_end,
            self.period_kind,
            self.reporting_scope,
        )


@dataclass
class NormalizationResult:
    """Selected facts plus everything that was logged, never filled."""

    facts: list[SelectedFact]
    missing_concepts: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def facts_by_identity(self) -> dict[tuple, SelectedFact]:
        return {fact.identity: fact for fact in self.facts}


# ---------------------------------------------------------------------------
# Observation building
# ---------------------------------------------------------------------------


def observation_hash(
    cik: str,
    taxonomy: str,
    tag: str,
    unit: str | None,
    period_start: date | None,
    period_end: date | None,
    value: Decimal | None,
    frame: str | None,
) -> str:
    """Stable identity of a reported observation (re-ingestion idempotency).

    Deliberately excludes accession/form/filed/fy/fp: the same fact value for
    the same period re-reported in a later filing is one observation, while a
    genuinely revised value (different number) hashes differently and is
    preserved alongside the original (SPEC 2.1.10).
    """
    canonical = "|".join(
        [
            str(cik),
            taxonomy,
            tag,
            unit or "",
            period_start.isoformat() if period_start else "",
            period_end.isoformat() if period_end else "",
            format(value, "f") if value is not None else "",
            frame or "",
        ]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def record_observation_hash(record: ObservationRecord) -> str:
    """The deduplication hash for a built observation record."""
    return observation_hash(
        record.cik,
        record.taxonomy,
        record.original_tag,
        record.unit,
        record.period_start,
        record.period_end,
        record.value,
        record.frame,
    )


def _to_decimal(raw: Any) -> Decimal | None:
    if raw is None or isinstance(raw, bool):
        return None
    return Decimal(str(raw))


def reverse_tagmap(tagmap: dict[str, list[str]]) -> dict[str, list[tuple[str, int]]]:
    """Map tag -> [(concept, priority)]. A tag claimed by several concepts keeps
    every claim; selection treats the lowest-priority-index concept as canonical
    and logs the ambiguity."""
    reversed_map: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for concept, tags in tagmap.items():
        for priority, tag in enumerate(tags):
            reversed_map[tag].append((concept, priority))
    return dict(reversed_map)


def build_observation_drafts(
    company_id: int,
    cik: str,
    companyfacts: dict[str, Any],
    tagmap: dict[str, list[str]],
) -> tuple[list[ObservationRecord], list[str]]:
    """Convert a raw companyfacts payload into observation drafts.

    Returns ``(drafts, notes)``. Notes record missing concepts and skipped
    entries; notes are log material, never filled data (SPEC 10.1).
    """
    reversed_map = reverse_tagmap(tagmap)
    drafts: list[ObservationRecord] = []
    seen_hashes: set[str] = set()
    notes: list[str] = []

    concepts_seen: set[str] = set()
    for taxonomy, tags in (companyfacts.get("facts") or {}).items():
        for tag, node in tags.items():
            claims = reversed_map.get(tag)
            if not claims:
                continue  # tag not in the tag map
            concept, priority = claims[0]
            if len(claims) > 1:
                notes.append(
                    f"tag {tag!r} maps to multiple concepts {[c for c, _ in claims]}; using {concept!r}"
                )
            for unit, entries in (node.get("units") or {}).items():
                for entry in entries:
                    period_end_text = entry.get("end")
                    period_start_text = entry.get("start")
                    if not period_end_text:
                        notes.append(f"skipping {tag} entry without end date")
                        continue
                    period_end = date.fromisoformat(period_end_text)
                    period_start = (
                        date.fromisoformat(period_start_text) if period_start_text else None
                    )
                    kind = period_kind(period_start, period_end)
                    value = _to_decimal(entry.get("val"))
                    if value is None:
                        notes.append(f"skipping {tag} entry without numeric value")
                        continue
                    frame = entry.get("frame")
                    digest = observation_hash(
                        cik, taxonomy, tag, unit, period_start, period_end, value, frame
                    )
                    if digest in seen_hashes:
                        continue
                    seen_hashes.add(digest)
                    concepts_seen.add(concept)
                    context = {
                        "fy": entry.get("fy"),
                        "fp": entry.get("fp"),
                        "filed": entry.get("filed"),
                        "accn": entry.get("accn"),
                        "form": entry.get("form"),
                        "frame": frame,
                    }
                    filed_at = date.fromisoformat(entry["filed"]) if entry.get("filed") else None
                    drafts.append(
                        ObservationRecord(
                            observation_id=None,
                            company_id=company_id,
                            cik=str(cik),
                            taxonomy=taxonomy,
                            original_tag=tag,
                            canonical_concept=concept,
                            tag_priority=priority,
                            value=value,
                            unit=unit,
                            period_start=period_start,
                            period_end=period_end,
                            period_kind=kind,
                            accession=entry.get("accn"),
                            form=entry.get("form"),
                            filed_at=filed_at,
                            source_fy=entry.get("fy"),
                            source_fp=str(entry["fp"]) if entry.get("fp") is not None else None,
                            frame=frame,
                            reporting_scope=DEFAULT_SCOPE,
                            context_metadata_json=json.dumps(context, sort_keys=True),
                        )
                    )

    for concept in tagmap:
        if concept not in concepts_seen:
            notes.append(f"concept {concept!r}: no observations found; leaving missing")
    missing = sorted(set(tagmap) - concepts_seen)
    # Revision vintage attaches to the reported VALUE: a value first filed in
    # some earlier 10-Q keeps that vintage even when a later filing re-reports
    # it as a comparative (same number, new accession/frame).
    value_vintage: dict[tuple, date] = {}
    for draft in drafts:
        if draft.filed_at is None:
            continue
        key = (
            draft.taxonomy,
            draft.original_tag,
            draft.unit,
            draft.period_start,
            draft.period_end,
            draft.value,
        )
        known = value_vintage.get(key)
        if known is None or draft.filed_at < known:
            value_vintage[key] = draft.filed_at
    drafts = [
        replace(draft, first_filed_at=value_vintage.get(_value_key(draft), draft.filed_at))
        for draft in drafts
    ]
    return drafts, notes + [f"missing concepts: {missing}" if missing else ""]


def _value_key(draft: ObservationRecord) -> tuple:
    return (
        draft.taxonomy,
        draft.original_tag,
        draft.unit,
        draft.period_start,
        draft.period_end,
        draft.value,
    )


def infer_fiscal_year_end_month(drafts: list[ObservationRecord]) -> int:
    """Infer the FYE month from annual durations (modal end month)."""
    months = Counter(
        d.period_end.month
        for d in drafts
        if d.period_kind is PeriodKind.annual and d.period_end is not None
    )
    if not months:
        return 12  # calendar year is the documented default
    return months.most_common(1)[0][0]


# ---------------------------------------------------------------------------
# Fiscal identity (anchored on annual periods, tolerant of 52/53-week calendars)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FiscalAnchor:
    fiscal_year: int
    start: date
    end: date


def build_fiscal_anchors(
    drafts: list[ObservationRecord], fiscal_year_end_month: int
) -> list[FiscalAnchor]:
    anchors: dict[tuple[date, date], FiscalAnchor] = {}
    for draft in drafts:
        if draft.period_kind is not PeriodKind.annual or not draft.period_start:
            continue
        fy = fiscal_year_of_duration_end(draft.period_end, fiscal_year_end_month)
        key = (draft.period_start, draft.period_end)
        anchors.setdefault(
            key, FiscalAnchor(fiscal_year=fy, start=draft.period_start, end=draft.period_end)
        )
    return sorted(anchors.values(), key=lambda a: a.end)


def fiscal_identity(
    period_start: date | None,
    period_end: date,
    kind: PeriodKind,
    anchors: list[FiscalAnchor],
    fiscal_year_end_month: int,
) -> tuple[int, str]:
    """Return (fiscal_year, fiscal_quarter label) for a duration period.

    Quarter/YTD periods inside a known fiscal-year window get their identity
    from that window (robust for 52/53-week calendars); everything else falls
    back to month-based rules. Instant facts must not be routed here.
    """
    if kind is PeriodKind.annual:
        return fiscal_year_of_duration_end(period_end, fiscal_year_end_month), "FY"
    anchor = next((a for a in anchors if a.start <= period_end <= a.end), None)
    if anchor is not None:
        days_into_year = (period_end - anchor.start).days
        index = quarter_index_from_days_into_year(days_into_year)
        return anchor.fiscal_year, FY_QUARTERS[index - 1]
    fy = fiscal_year_of(period_end, fiscal_year_end_month)
    index = quarter_index_in_year(period_end, fiscal_year_end_month)
    return fy, FY_QUARTERS[index - 1]


# ---------------------------------------------------------------------------
# Selection (SPEC 10.1, 10.4)
# ---------------------------------------------------------------------------


def _compatible_unit(record: ObservationRecord) -> bool:
    allowed = CONCEPT_UNITS.get(record.canonical_concept)
    return allowed is not None and record.unit in allowed


def _scope_of(record: ObservationRecord) -> str:
    return record.reporting_scope or DEFAULT_SCOPE


def select_facts(
    observations: list[ObservationRecord],
    fiscal_year_end_month: int,
    policy: str = SELECTION_LATEST,
    as_of: date | None = None,
) -> NormalizationResult:
    """Select one fact per (concept, economic period, scope) from observations.

    Tag priority is applied ONLY AFTER filtering for compatible unit, scope and
    economic period: observations are grouped by exact period identity first,
    then the best-priority candidate wins within the group. An annual value
    therefore can never displace a quarterly value, and an incompatible-unit
    observation can never outrank a compatible one (SPEC 10.1).

    ``policy='as_of'`` requires ``as_of`` and excludes observations filed after
    it (SPEC 10.4). Within one tag priority, the latest filing wins, so a
    restatement supersedes the original value while both observations remain
    preserved upstream.
    """
    if policy == SELECTION_AS_OF and as_of is None:
        raise ValueError("as_of selection policy requires an as_of timestamp")
    anchors = build_fiscal_anchors(observations, fiscal_year_end_month)
    notes: list[str] = []
    missing: set[str] = set()

    by_concept: dict[str, list[ObservationRecord]] = defaultdict(list)
    for record in observations:
        by_concept[record.canonical_concept].append(record)

    facts: list[SelectedFact] = []
    for concept, records in sorted(by_concept.items()):
        compatible = [r for r in records if _compatible_unit(r)]
        skipped_units = len(records) - len(compatible)
        if skipped_units:
            notes.append(
                f"concept {concept!r}: skipped {skipped_units} observation(s) with incompatible units"
            )
        if not compatible:
            missing.add(concept)
            continue
        scope_groups: dict[str, list[ObservationRecord]] = defaultdict(list)
        for record in compatible:
            scope_groups[_scope_of(record)].append(record)
        for scope, scoped in scope_groups.items():
            period_groups: dict[tuple, list[ObservationRecord]] = defaultdict(list)
            for record in scoped:
                period_groups[(record.period_start, record.period_end, record.period_kind)].append(
                    record
                )
            for (_start, _end, kind), group in sorted(
                period_groups.items(), key=lambda item: (item[0][1] or date.min, str(item[0][2]))
            ):
                if policy == SELECTION_AS_OF:
                    dated = [r for r in group if r.filed_at is not None and r.filed_at <= as_of]
                    if not dated:
                        continue  # not yet filed at as_of: stays missing
                    group = dated
                best_priority = min(r.tag_priority for r in group)
                finalists = [r for r in group if r.tag_priority == best_priority]
                winner = max(
                    finalists, key=lambda r: (r.filed_at or date.min, r.observation_id or 0)
                )
                if len(finalists) > 1:
                    notes.append(
                        f"concept {concept!r} period ending {_end}: {len(finalists)} same-priority "
                        f"observations; latest filing wins ({winner.form} filed {winner.filed_at})"
                    )
                fiscal_year, label = fiscal_identity(
                    _start, _end, kind, anchors, fiscal_year_end_month
                )
                facts.append(
                    SelectedFact(
                        concept=concept,
                        period_start=_start,
                        period_end=_end,
                        period_kind=kind,
                        fiscal_year=fiscal_year,
                        fiscal_quarter=label if kind is not PeriodKind.instant else None,
                        reporting_scope=scope,
                        value=winner.value,
                        unit=winner.unit,
                        is_derived=False,
                        derivation_method=None,
                        selection_policy=policy,
                        sources=[FactSource(observation=winner, role="direct_source")],
                    )
                )

    cleaned_notes = [n for n in notes if n]
    if missing:
        cleaned_notes.append(f"concepts with no usable observations: {sorted(missing)}")
    return NormalizationResult(facts=facts, missing_concepts=sorted(missing), notes=cleaned_notes)


# ---------------------------------------------------------------------------
# Quarterly derivation (SPEC 10.3)
# ---------------------------------------------------------------------------


ONE_DAY = timedelta(days=1)


def _revision_compatible(a: FactSource, b: FactSource) -> bool:
    """Inputs whose value vintages are within ~one quarter of each other.

    The vintage of an input is the first filing of its reported VALUE
    (``first_filed_at``), so a later comparative re-report of an unchanged
    number does not manufacture a vintage mismatch.
    """
    fa = a.observation.first_filed_at or a.observation.filed_at
    fb = b.observation.first_filed_at or b.observation.filed_at
    if fa is None or fb is None:
        return False  # undated inputs cannot be vintaged
    return abs((fa - fb).days) <= REVISION_VINTAGE_WINDOW_DAYS


def _find_ytd(
    index: dict[tuple, SelectedFact],
    concept: str,
    scope: str,
    unit: str,
    fiscal_year: int,
    label: str,
    expected_days: tuple[int, int],
) -> SelectedFact | None:
    """Find the YTD rung: a cumulative fact of the expected duration length.

    The lookup key uses the *cumulative-through* label (Q2 for 6m, Q3 for 9m);
    the duration check guards against mislabelled source data.
    """
    candidate = index.get((concept, scope, unit, fiscal_year, label))
    if candidate is None or candidate.period_start is None:
        return None
    if candidate.period_kind is not PeriodKind.year_to_date:
        return None
    days = (candidate.period_end - candidate.period_start).days
    if not expected_days[0] <= days <= expected_days[1]:
        return None
    return candidate


def derive_missing_quarters(
    direct_facts: list[SelectedFact],
    fiscal_year_end_month: int,
) -> tuple[list[SelectedFact], list[str]]:
    """Derive missing fiscal quarters of additive flow concepts (SPEC 10.3).

    Q2 = six-month YTD - Q1, Q3 = nine-month YTD - six-month YTD,
    Q4 = annual - nine-month YTD.

    SPEC 10.3 conditions hold by construction: same company (inputs are
    per-company), same concept and unit (matched below), same reporting scope
    (matched below), compatible accounting basis and revision vintage (filing
    date window), and correct consecutive fiscal periods (same fiscal year AND
    contiguous actual dates). Direct facts always win over derivations.
    EPS, weighted shares, ratios and instant values never enter this function;
    if direct quarterly values are missing they stay missing.

    Returns (derived facts, notes).
    """
    del fiscal_year_end_month  # fiscal identities are already resolved on inputs
    notes: list[str] = []
    direct_identities = {fact.identity for fact in direct_facts}

    # (concept, scope, unit, fy, label) -> direct fact, derivable concepts only.
    index: dict[tuple, SelectedFact] = {}
    annual_by_key: dict[tuple[str, str, str, int], SelectedFact] = {}
    for fact in direct_facts:
        if fact.concept not in ADDITIVE_FLOW_CONCEPTS or fact.value is None:
            continue
        if fact.fiscal_year is None:
            continue
        if fact.fiscal_quarter in FY_QUARTERS:
            index[
                (
                    fact.concept,
                    fact.reporting_scope,
                    fact.unit,
                    fact.fiscal_year,
                    fact.fiscal_quarter,
                )
            ] = fact
        elif fact.period_kind is PeriodKind.annual:
            annual_by_key[(fact.concept, fact.reporting_scope, fact.unit, fact.fiscal_year)] = fact

    derived: list[SelectedFact] = []
    concepts = sorted({f.concept for f in direct_facts if f.concept in ADDITIVE_FLOW_CONCEPTS})
    scopes = sorted({f.reporting_scope for f in direct_facts})
    # Iterate every fiscal year that could feed the ladder: years with an
    # annual total (Q4) AND years with only quarter/YTD rungs (an in-progress
    # fiscal year has no annual yet but Q2/Q3 may still be derivable).
    fiscal_years = sorted(
        {fy for (_, _, _, fy, _label) in index} | {fy for (_c, _s, _u, fy) in annual_by_key},
    )

    for concept in concepts:
        for scope in scopes:
            for fy in fiscal_years:
                # The annual total (when the year is complete) anchors the Q4 rung;
                # Q2/Q3 are derivable for in-progress years without an annual.
                annual = next(
                    (
                        fact
                        for (c, s, _u, year), fact in sorted(annual_by_key.items())
                        if c == concept and s == scope and year == fy
                    ),
                    None,
                )
                units_present = sorted(
                    {
                        unit
                        for (c, s, unit, year, _label) in index
                        if c == concept and s == scope and year == fy
                    }
                )
                for unit in units_present:
                    q1 = index.get((concept, scope, unit, fy, "Q1"))
                    ytd6 = _find_ytd(index, concept, scope, unit, fy, "Q2", YTD_6_MONTH_DAYS)
                    ytd9 = _find_ytd(index, concept, scope, unit, fy, "Q3", YTD_9_MONTH_DAYS)

                    # (target label, cumulative fact, subtracted fact, start, end)
                    candidates: list[tuple[str, SelectedFact, SelectedFact, date, date]] = []
                    if (
                        q1
                        and ytd6
                        and q1.period_start is not None
                        and q1.period_end is not None
                        and ytd6.period_end is not None
                        # Consecutive fiscal periods: the YTD starts at the fiscal
                        # year start (where Q1 starts) and strictly contains Q1.
                        and ytd6.period_start == q1.period_start
                        and q1.period_end < ytd6.period_end
                    ):
                        candidates.append(
                            ("Q2", ytd6, q1, q1.period_end + ONE_DAY, ytd6.period_end)
                        )
                    if (
                        ytd6
                        and ytd9
                        and ytd6.period_end is not None
                        and ytd9.period_start is not None
                        and ytd9.period_end is not None
                        and ytd9.period_start == ytd6.period_start
                        and ytd6.period_end < ytd9.period_end
                    ):
                        candidates.append(
                            ("Q3", ytd9, ytd6, ytd6.period_end + ONE_DAY, ytd9.period_end)
                        )
                    if (
                        annual is not None
                        and annual.unit == unit
                        and ytd9
                        and annual.period_start is not None
                        and ytd9.period_end is not None
                        and ytd9.period_start == annual.period_start
                        and ytd9.period_end < annual.period_end
                    ):
                        candidates.append(
                            ("Q4", annual, ytd9, ytd9.period_end + ONE_DAY, annual.period_end)
                        )

                    for target_label, cumulative, subtracted, start, end in candidates:
                        cumulative_source = FactSource(
                            cumulative.sources[0].observation, "annual_total"
                        )
                        subtracted_source = FactSource(
                            subtracted.sources[0].observation, "prior_ytd"
                        )
                        if not _revision_compatible(cumulative_source, subtracted_source):
                            notes.append(
                                f"{concept} FY{fy} {target_label}: inputs not revision-compatible; "
                                "not derived"
                            )
                            continue
                        if cumulative.value is None or subtracted.value is None:
                            continue
                        value = cumulative.value - subtracted.value
                        if (concept, start, end, PeriodKind.quarter, scope) in direct_identities:
                            notes.append(
                                f"{concept} FY{fy} {target_label}: direct fact already present; "
                                "not derived"
                            )
                            continue
                        derived.append(
                            SelectedFact(
                                concept=concept,
                                period_start=start,
                                period_end=end,
                                period_kind=PeriodKind.quarter,
                                fiscal_year=fy,
                                fiscal_quarter=target_label,
                                reporting_scope=scope,
                                value=value,
                                unit=unit,
                                is_derived=True,
                                derivation_method=(
                                    "annual_minus_ytd9"
                                    if target_label == "Q4"
                                    else "ytd_minus_prior_ytd"
                                ),
                                selection_policy=cumulative.selection_policy,
                                sources=[cumulative_source, subtracted_source],
                            )
                        )
    if derived:
        notes.append(
            f"derived {len(derived)} quarter fact(s) for concepts {sorted({d.concept for d in derived})}"
        )
    return derived, notes


# ---------------------------------------------------------------------------
# Full normalization entry point
# ---------------------------------------------------------------------------


def normalize_companyfacts(
    company_id: int,
    cik: str,
    companyfacts: dict[str, Any],
    tagmap: dict[str, list[str]],
    fiscal_year_end_month: int | None = None,
    policy: str = SELECTION_LATEST,
    as_of: date | None = None,
) -> tuple[list[ObservationRecord], NormalizationResult, int]:
    """Full normalization: drafts -> selection -> derivation.

    Returns (observation drafts, result incl. direct + derived facts, FYE month).
    Never derives EPS, weighted shares, margins, ratios or instant values
    (SPEC 10.3); those stay missing when not directly reported.
    """
    drafts, build_notes = build_observation_drafts(company_id, cik, companyfacts, tagmap)
    fye_month = fiscal_year_end_month or infer_fiscal_year_end_month(drafts)
    result = select_facts(drafts, fye_month, policy=policy, as_of=as_of)
    # Concepts with no observations at all never reach selection; log them as
    # missing so gaps are visible and never filled (SPEC 10.1).
    concepts_with_observations = {d.canonical_concept for d in drafts}
    result.missing_concepts = sorted(
        set(result.missing_concepts) | (set(tagmap) - concepts_with_observations)
    )
    direct_facts = [f for f in result.facts if not f.is_derived]
    derived, derive_notes = derive_missing_quarters(direct_facts, fye_month)
    result.facts.extend(derived)
    result.notes.extend(build_notes)
    result.notes.extend(derive_notes)
    result.notes = [n for n in result.notes if n]
    return drafts, result, fye_month
