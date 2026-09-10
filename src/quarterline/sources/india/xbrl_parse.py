"""Deterministic parse of SEBI in-capmkt Integrated Filing XBRL instances (IND-2).

Scope of this parser (feasibility milestone — no scoring, no LLM):

- contexts: start/end durations and instants, plus ``xbrldi`` explicit-member
  dimensions (segments, expense breakdowns, auditor). Scope (consolidated vs
  standalone) is NOT a dimension in these instances — it is expressed per
  filing/instance and declared by the ``NatureOfReportStandaloneConsolidated``
  qualifier fact (docs/india_source_audit.md §6.4);
- units: ``iso4217:INR`` money and ``INR``/``shares`` divided per-share units;
- decimals/precision attributes on every numeric fact;
- the ``LevelOfRounding`` presentation trait (values are full rupees — units.py);
- entity/period facts and the Reg-33 qualifier facts (audit status, declaration
  of unmodified opinion, reporting-period dates, identifiers).

All values are kept EXACTLY as they appear in the instance (Decimal for numeric
facts, raw string otherwise). A taxonomy revision/audit status is not present in
the instance itself — it comes from the exchange listing metadata and is carried
via :class:`FilingMeta` (revisions.py); the parser still surfaces every
audit-related qualifier the instance does carry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.etree import ElementTree

from quarterline.sources.india.concept_map import (
    IN_CAPMKT_NAMESPACE,
    IN_CAPMKT_PREFIX,
)
from quarterline.sources.india.periods import classify_period
from quarterline.sources.india.revisions import FilingMeta

XBRLI_NAMESPACE = "http://www.xbrl.org/2003/instance"
XBRLDI_NAMESPACE = "http://xbrl.org/2006/xbrldi"
XLINK_NAMESPACE = "http://www.w3.org/1999/xlink"
LINK_NAMESPACE = "http://www.xbrl.org/2003/linkbase"

#: Per-share unit name used by the IFIndAs instances (unit id ``INRPerShare``:
#: iso4217:INR divided by shares).
PER_SHARE_UNIT = "INRPerShare"

_TAXONOMY_COMMENT_RE = re.compile(
    r"IFIndAs\s+(?P<version>[Vv][\d.]+[^\s]*)\s*(?:\((?P<date>[^)]*)\))?"
)


@dataclass(frozen=True)
class IndiaContext:
    """One ``xbrli:context``: period plus optional explicit dimensions."""

    context_id: str
    period_start: str | None = None  # ISO date string, exactly as in the instance
    period_end: str | None = None
    instant: str | None = None
    dimensions: tuple[tuple[str, str], ...] = ()  # (dimension, member), qualified names

    @property
    def is_instant(self) -> bool:
        return self.instant is not None

    @property
    def is_dimensioned(self) -> bool:
        return bool(self.dimensions)

    @property
    def period_kind(self) -> str:
        """Classified from dates only (periods.classify_period)."""
        start = self.period_start
        end = self.instant or self.period_end
        return classify_period(
            date.fromisoformat(start) if start else None,
            date.fromisoformat(end) if end else None,
        )


@dataclass(frozen=True)
class IndiaFact:
    """One instance fact, value kept exactly as filed."""

    tag: str  # local name, e.g. RevenueFromOperations
    value_text: str  # exact character content of the element
    context_ref: str
    unit_ref: str | None  # None for string/date/boolean (non-numeric) facts
    decimals: str | None
    precision: str | None
    is_numeric: bool
    context: IndiaContext

    @property
    def value_decimal(self) -> Decimal | None:
        """Exact Decimal for numeric facts; ``None`` for non-numeric content."""
        if not self.is_numeric:
            return None
        try:
            return Decimal(self.value_text.strip())
        except InvalidOperation:
            return None

    @property
    def is_dimensioned(self) -> bool:
        return self.context.is_dimensioned

    @property
    def is_per_share_unit(self) -> bool:
        """True for divided units (e.g. INR/shares): never scale-multiplied."""
        return self.unit_ref is not None and "/" in self.unit_ref


@dataclass
class IndiaInstance:
    """Parsed instance: facts, contexts, entity identity and qualifier facts."""

    facts: list[IndiaFact] = field(default_factory=list)
    contexts: dict[str, IndiaContext] = field(default_factory=dict)
    entity_scheme: str | None = None
    entity_identifier: str | None = None  # BSE scrip code in the IFIndAs instances
    schema_ref: str | None = None
    taxonomy_version: str | None = None  # e.g. "V2.1 (26-06-2026)" from the header comment

    #: String/qualifier facts by local tag (single-valued qualifiers only; when the
    #: same qualifier repeats across contexts the distinct values are kept).
    qualifiers: dict[str, str] = field(default_factory=dict)
    multi_valued_qualifiers: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: Raw tags encountered with no canonical mapping (explicit, never dropped).
    unmapped_tags: tuple[str, ...] = field(default_factory=tuple)
    filing_meta: FilingMeta | None = None

    # -- qualifier accessors (instance-declared, carried not trusted) ----------

    def qualifier(self, tag: str) -> str | None:
        return self.qualifiers.get(tag)

    @property
    def rounding_trait(self) -> str | None:
        """``LevelOfRounding`` value, e.g. "Crores" (presentation scale only)."""
        return self.qualifier("LevelOfRounding")

    @property
    def declared_scope(self) -> str | None:
        """``NatureOfReportStandaloneConsolidated`` -> "consolidated"/"standalone"."""
        value = self.qualifier("NatureOfReportStandaloneConsolidated")
        if value is None:
            return None
        lowered = value.strip().lower()
        if lowered.startswith("consolidated"):
            return "consolidated"
        if lowered.startswith("standalone"):
            return "standalone"
        return None

    @property
    def audited_status(self) -> str | None:
        """``WhetherResultsAreAuditedOrUnaudited`` exactly as declared (Audited/Unaudited)."""
        return self.qualifier("WhetherResultsAreAuditedOrUnaudited")

    @property
    def audit_qualification_declaration(self) -> str | None:
        return self.qualifier(
            "DeclarationOfUnmodifiedOpinionOrStatementOnImpactOfAuditQualification"
        )

    @property
    def isin(self) -> str | None:
        return self.qualifier("ISIN")

    @property
    def symbol(self) -> str | None:
        return self.qualifier("Symbol")

    @property
    def company_name(self) -> str | None:
        return self.qualifier("NameOfTheCompany")

    def facts_for_tag(self, tag: str) -> list[IndiaFact]:
        return [fact for fact in self.facts if fact.tag == tag]

    def undimensioned_numeric_facts(self) -> list[IndiaFact]:
        """Numeric facts whose context carries no segment dimensions.

        Dimensioned facts (segments, expense-detail members) must never masquerade
        as statement totals, so canonical mapping consumes only these.
        """
        return [fact for fact in self.facts if fact.is_numeric and not fact.is_dimensioned]


def _taxonomy_version_from_prolog(raw: bytes) -> str | None:
    """Extract ``IFIndAs V2.1 (26-06-2026)`` from the XML comment prolog."""
    head = raw[:2048].decode("utf-8", errors="replace")
    match = _TAXONOMY_COMMENT_RE.search(head)
    if match is None:
        return None
    version = match.group("version")
    date = match.group("date")
    return f"{version} ({date})" if date else version


def _parse_contexts(root: ElementTree.Element) -> dict[str, IndiaContext]:
    contexts: dict[str, IndiaContext] = {}
    for ctx in root.findall(f"{{{XBRLI_NAMESPACE}}}context"):
        ctx_id = ctx.get("id") or ""
        instant = ctx.findtext(f"{{{XBRLI_NAMESPACE}}}period/{{{XBRLI_NAMESPACE}}}instant")
        start = ctx.findtext(f"{{{XBRLI_NAMESPACE}}}period/{{{XBRLI_NAMESPACE}}}startDate")
        end = ctx.findtext(f"{{{XBRLI_NAMESPACE}}}period/{{{XBRLI_NAMESPACE}}}endDate")
        dimensions: list[tuple[str, str]] = []
        for member in ctx.findall(f".//{{{XBRLDI_NAMESPACE}}}explicitMember"):
            dimension = member.get("dimension") or ""
            dimensions.append((dimension, (member.text or "").strip()))
        typed_members = ctx.findall(f".//{{{XBRLDI_NAMESPACE}}}typedMember")
        contexts[ctx_id] = IndiaContext(
            context_id=ctx_id,
            period_start=start,
            period_end=end,
            instant=instant,
            dimensions=tuple(dimensions),
        )
        if typed_members:
            # Typed dimensions are not used by the current IFIndAs instances;
            # record their presence via a synthetic dimension entry so the
            # context is conservatively treated as dimensioned.
            context = contexts[ctx_id]
            contexts[ctx_id] = IndiaContext(
                context_id=context.context_id,
                period_start=context.period_start,
                period_end=context.period_end,
                instant=context.instant,
                dimensions=context.dimensions
                + tuple((f"typed:{idx}", "") for idx in range(len(typed_members))),
            )
    return contexts


def _parse_units(root: ElementTree.Element) -> dict[str, str]:
    """unit id -> logical name ("INR" or "INRPerShare")."""
    units: dict[str, str] = {}
    for unit in root.findall(f"{{{XBRLI_NAMESPACE}}}unit"):
        unit_id = unit.get("id") or ""
        measure = unit.findtext(f"{{{XBRLI_NAMESPACE}}}measure")
        divide = unit.find(f"{{{XBRLI_NAMESPACE}}}divide")
        if divide is not None:
            numerator = divide.findtext(
                f"{{{XBRLI_NAMESPACE}}}unitNumerator/{{{XBRLI_NAMESPACE}}}measure"
            )
            denominator = divide.findtext(
                f"{{{XBRLI_NAMESPACE}}}unitDenominator/{{{XBRLI_NAMESPACE}}}measure"
            )
            if numerator and denominator:
                num_local = numerator.split(":", 1)[-1]
                den_local = denominator.split(":", 1)[-1]
                units[unit_id] = f"{num_local}/{den_local}"
        elif measure:
            units[unit_id] = measure.split(":", 1)[-1]
    return units


#: Tags whose values are identifiers/dates/labels/booleans (never numeric facts).
def _is_numeric_fact(unit_ref: str | None) -> bool:
    return unit_ref is not None


def parse_instance(
    source: bytes | str,
    filing_meta: FilingMeta | None = None,
) -> IndiaInstance:
    """Parse one in-capmkt XBRL instance from bytes or a file path.

    Deterministic stdlib XML parsing (no taxonomy fetch — the ``schemaRef`` is a
    relative reference and is recorded, not resolved).
    """
    if isinstance(source, bytes):
        raw = source
    else:
        raw = Path(source).read_bytes()
    root = ElementTree.fromstring(raw)

    instance = IndiaInstance()
    instance.taxonomy_version = _taxonomy_version_from_prolog(raw)
    instance.filing_meta = filing_meta
    schema_ref = root.find(f"{{{LINK_NAMESPACE}}}schemaRef")
    if schema_ref is not None:
        instance.schema_ref = schema_ref.get(f"{{{XLINK_NAMESPACE}}}href")

    identifier = root.find(
        f"{{{XBRLI_NAMESPACE}}}context/{{{XBRLI_NAMESPACE}}}entity/{{{XBRLI_NAMESPACE}}}identifier"
    )
    if identifier is not None:
        instance.entity_scheme = identifier.get("scheme")
        instance.entity_identifier = (identifier.text or "").strip()

    instance.contexts = _parse_contexts(root)
    units = _parse_units(root)

    qualifiers: dict[str, str] = {}
    multi: dict[str, list[str]] = {}
    unmapped: list[str] = []
    for element in root:
        if not element.tag.startswith(f"{{{IN_CAPMKT_NAMESPACE}}}"):
            continue
        tag = element.tag.split("}", 1)[1]
        context_ref = element.get("contextRef") or ""
        unit_ref = element.get("unitRef")
        fact = IndiaFact(
            tag=tag,
            value_text=element.text or "",
            context_ref=context_ref,
            unit_ref=units.get(unit_ref) if unit_ref else None,
            decimals=element.get("decimals"),
            precision=element.get("precision"),
            is_numeric=_is_numeric_fact(unit_ref),
            context=instance.contexts.get(
                context_ref,
                IndiaContext(context_id=context_ref),
            ),
        )
        instance.facts.append(fact)
        if not fact.is_numeric:
            value = fact.value_text.strip()
            if tag in qualifiers:
                if value != qualifiers[tag]:
                    values = multi.setdefault(tag, [qualifiers[tag]])
                    if value not in values:
                        values.append(value)
            else:
                qualifiers[tag] = value

    instance.qualifiers = qualifiers
    instance.multi_valued_qualifiers = {k: tuple(v) for k, v in multi.items()}

    seen_unmapped: set[str] = set()
    for fact in instance.undimensioned_numeric_facts():
        if fact.tag not in seen_unmapped and _reverse_has_no_claim(fact.tag):
            seen_unmapped.add(fact.tag)
            unmapped.append(fact.tag)
    instance.unmapped_tags = tuple(unmapped)
    return instance


def _reverse_has_no_claim(tag: str) -> bool:
    from quarterline.sources.india.concept_map import map_tag

    return not map_tag(tag).mapped


__all__ = [
    "IN_CAPMKT_NAMESPACE",
    "IN_CAPMKT_PREFIX",
    "IndiaContext",
    "IndiaFact",
    "IndiaInstance",
    "parse_instance",
]
