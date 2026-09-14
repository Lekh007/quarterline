"""India source reconciliation output (IND-2 table; IND-3 validation CSV).

Two outputs live here:

1. ``reconciliation_table(issuer_id)`` — the IND-2 source-linked CLI table for
   every mapped fact parsed from the issuer's cached XBRL artifacts.

2. ``write_validation_csv()`` — the IND-3 financial-reconciliation artifact at
   ``data/validation/india_reconciliation.csv`` (supersedes the IND-2
   ``data/india_reconciliation.csv`` demonstration table, whose 9-column shape
   could not carry document hashes, context ids, precision, rendered-reference
   cross-checks, or review states). Every row keeps the raw value AND the
   normalized value; structured facts are cross-checked against official
   rendered documents (Reg-33 / condensed-FS / results-letter PDFs) with
   reference page + displayed value wherever the rendered text is reliable.
   Comparison statuses: ``matched``, ``matched_within_declared_precision``,
   ``scope_or_basis_difference_recorded``, ``extraction_scrambled``,
   ``no_rendered_comparison_available``, ``mismatch_flagged_do_not_force``.
   Review statuses: ``automated_check_passed``, ``agent_checked_against_document``,
   ``human_review_pending`` — human approval is NEVER recorded by this code.

The rendered-document references below were verified against the cached PDFs
(``storage/raw/india/``) by text extraction on 2026-09-11; a test re-extracts
them live when the cache is present so the recorded references cannot silently
drift from the documents.
"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from quarterline.cli import register_subcommand
from quarterline.sources.india.concept_map import INDIA_CONCEPTS, map_tag
from quarterline.sources.india.issuers import load_issuers
from quarterline.sources.india.periods import period_label
from quarterline.sources.india.pipeline import _india_artifacts
from quarterline.sources.india.revisions import FilingMeta
from quarterline.sources.india.units import format_crores
from quarterline.sources.india.xbrl_parse import IndiaFact, parse_instance
from quarterline.store.db import session_scope

REPO_ROOT = Path(__file__).resolve().parents[4]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "india"
MANIFEST_PATH = FIXTURES_DIR / "manifest.json"
STORAGE_ROOT = REPO_ROOT / "storage" / "raw" / "india"
DEFAULT_VALIDATION_CSV_PATH = REPO_ROOT / "data" / "validation" / "india_reconciliation.csv"
SUPERSEDED_CSV_PATH = REPO_ROOT / "data" / "india_reconciliation.csv"

REVIEW_RECONCILED = "reconciled_to_source"  # legacy IND-2 table status (CLI table only)
REVIEW_REQUIRED = "review_required"

#: Review statuses for the IND-3 validation CSV. human_approved is deliberately
#: absent — no code path may record human approval.
REVIEW_AUTOMATED_PASSED = "automated_check_passed"
REVIEW_AGENT_CHECKED = "agent_checked_against_document"
REVIEW_HUMAN_PENDING = "human_review_pending"
REVIEW_STATUSES: frozenset[str] = frozenset(
    {REVIEW_AUTOMATED_PASSED, REVIEW_AGENT_CHECKED, REVIEW_HUMAN_PENDING}
)

COMPARISON_MATCHED = "matched"
COMPARISON_WITHIN_PRECISION = "matched_within_declared_precision"
COMPARISON_SCOPE_BASIS = "scope_or_basis_difference_recorded"
COMPARISON_SCRAMBLED = "extraction_scrambled"
COMPARISON_UNAVAILABLE = "no_rendered_comparison_available"
COMPARISON_MISMATCH = "mismatch_flagged_do_not_force"
COMPARISON_STATUSES: frozenset[str] = frozenset(
    {
        COMPARISON_MATCHED,
        COMPARISON_WITHIN_PRECISION,
        COMPARISON_SCOPE_BASIS,
        COMPARISON_SCRAMBLED,
        COMPARISON_UNAVAILABLE,
        COMPARISON_MISMATCH,
    }
)

_CSV_COLUMNS = (
    "issuer_id",
    "source_document_id",
    "source_url",
    "document_hash",
    "filing_identifier",
    "filed_at",
    "period_start",
    "period_end",
    "fiscal_label",
    "reporting_scope",
    "concept",
    "original_tag_or_label",
    "context_id",
    "raw_value",
    "raw_unit",
    "source_precision",
    "presentation_scale",
    "normalized_value",
    "normalized_unit",
    "reference_document_id",
    "reference_page_or_location",
    "reference_display_value",
    "comparison_status",
    "review_status",
    "review_notes",
)


#: Legacy IND-2 CLI-table columns (kept for `verify india` output only; the
#: committed CSV contract is _CSV_COLUMNS above).
_LEGACY_CSV_COLUMNS = (
    "issuer",
    "document",
    "page_or_tag",
    "concept",
    "period",
    "scope",
    "reported_value",
    "normalized_value",
    "review_status",
)


@dataclass(frozen=True)
class ReconciliationRow:
    """One source-linked reported value."""

    issuer_id: str
    document: str
    page_or_tag: str
    concept: str
    period: str
    scope: str
    reported_value: str
    normalized_value: str
    review_status: str = REVIEW_RECONCILED

    def csv_row(self) -> dict[str, str]:
        values: dict[str, str] = {}
        for column in _LEGACY_CSV_COLUMNS:
            # CSV column "issuer" maps to the issuer_id field.
            values[column] = getattr(self, "issuer_id" if column == "issuer" else column)
        return values


def _concept_order(concept: str) -> int:
    try:
        return INDIA_CONCEPTS.index(concept)
    except ValueError:  # pragma: no cover - only mapped concepts reach rows
        return len(INDIA_CONCEPTS)


def _reported_and_normalized(fact: IndiaFact, rounding_trait: str | None) -> tuple[str, str]:
    value = fact.value_decimal
    if value is None:
        return fact.value_text, fact.value_text
    if fact.is_per_share_unit:
        # Per-share values never inherit a crore/lakh multiplier.
        return f"{fact.value_text} (per share)", str(value)
    return (
        f"{format_crores(value)} @ {rounding_trait or 'Units'}",
        str(value),
    )


def _rows_for_instance(
    issuer_id: str,
    document: str,
    instance,
    meta: FilingMeta | None,
) -> list[ReconciliationRow]:
    rows: list[ReconciliationRow] = []
    scope = (meta.scope if meta else None) or instance.declared_scope or "consolidated"
    for fact in instance.undimensioned_numeric_facts():
        mapping = map_tag(fact.tag)
        if not mapping.mapped:
            continue
        period_end_text = fact.context.instant or fact.context.period_end
        if period_end_text is None:
            continue
        kind = fact.context.period_kind
        label = period_label(
            date.fromisoformat(fact.context.period_start) if fact.context.period_start else None,
            date.fromisoformat(period_end_text),
            kind,
        )
        reported, normalized = _reported_and_normalized(fact, instance.rounding_trait)
        rows.append(
            ReconciliationRow(
                issuer_id=issuer_id,
                document=document,
                page_or_tag=fact.tag,
                concept=mapping.concept or "",
                period=label,
                scope=scope,
                reported_value=reported,
                normalized_value=normalized,
                review_status=REVIEW_RECONCILED,
            )
        )
    rows.sort(key=lambda r: (r.document, r.period, _concept_order(r.concept), r.page_or_tag))
    return rows


def rows_from_artifacts(issuer_id: str, database_url: str | None = None) -> list[ReconciliationRow]:
    """Reconcile the issuer's cached India XBRL artifacts against their sources."""
    issuer = next((i for i in load_issuers() if i.issuer_id == issuer_id.strip().upper()), None)
    if issuer is None:
        raise LookupError(f"unknown India issuer {issuer_id!r}")
    rows: list[ReconciliationRow] = []
    with session_scope(database_url) as session:
        pairs = _india_artifacts(session, issuer.issuer_id, issuer.isin)
        if not pairs:
            raise LookupError(
                f"no cached India XBRL artifacts for {issuer.issuer_id} — import one with "
                "`quarterline ingest india-document` first (browser-acquired file)"
            )
        for artifact, meta in pairs:
            assert artifact.local_path is not None
            instance = parse_instance(Path(artifact.local_path).read_bytes(), filing_meta=meta)
            rows.extend(
                _rows_for_instance(
                    issuer.issuer_id,
                    f"{Path(artifact.local_path).name} [seq {meta.seq_id if meta else '?'}]",
                    instance,
                    meta,
                )
            )
    return rows


def rows_from_fixture_dir(fixtures_dir: Path | None = None) -> list[ReconciliationRow]:
    """Reconcile rows straight from the committed fixtures + manifest (no database)."""
    fixtures = fixtures_dir or FIXTURES_DIR
    manifest = json.loads((fixtures / "manifest.json").read_text(encoding="utf-8"))
    rows: list[ReconciliationRow] = []
    for entry in manifest.get("committed_fixtures", []):
        meta = FilingMeta(
            issuer_id=entry["issuer_id"],
            scope=entry["scope"],
            period_start=None,
            period_end=_period_end_from_entry(entry, manifest),
            published_at=_published_from_entry(entry),
            revision_status=(entry.get("exchange_filing") or {}).get("revision"),
            audited_status=(entry.get("exchange_filing") or {}).get("audited_status"),
            exchange="NSE",
            seq_id=(entry.get("exchange_filing") or {}).get("seq_id"),
            source_url=entry.get("source_url"),
        )
        instance = parse_instance((fixtures / entry["file"]).read_bytes(), filing_meta=meta)
        rows.extend(
            _rows_for_instance(
                entry["issuer_id"],
                entry["file"],
                instance,
                meta,
            )
        )
    return rows


def _period_end_from_entry(entry: dict, manifest: dict) -> date:
    periods = manifest.get("periods", {})
    period_info = periods.get(entry.get("period", ""), {})
    return date.fromisoformat(
        str(period_info.get("period_end") or entry.get("period") or "1970-01-01")
    )


def _published_from_entry(entry: dict) -> date:
    broadcast = (entry.get("exchange_filing") or {}).get("broadcast_ist") or ""
    day = broadcast.split(" ")[0] if broadcast else "1970-01-01"
    return date.fromisoformat(str(day))


def render_table(rows: list[ReconciliationRow]) -> str:
    """Aligned plain-text reconciliation table (CLI `verify india` output)."""
    header = (
        f"{'period':<44} {'scope':<13} {'concept':<28} {'tag':<48} "
        f"{'reported':<26} {'normalized (INR)':>18} {'document':<40} review"
    )
    lines = [
        "India reconciliation (source-linked, parsed exchange XBRL — research only)",
        header,
        "-" * len(header),
    ]
    for row in rows:
        lines.append(
            f"{row.period:<44} {row.scope:<13} {row.concept:<28} {row.page_or_tag:<48} "
            f"{row.reported_value:<26} {row.normalized_value:>18} {row.document:<40} "
            f"{row.review_status}"
        )
    total = len(rows)
    lines.append("-" * len(header))
    lines.append(
        f"{total} rows | reconciled_to_source="
        f"{sum(1 for r in rows if r.review_status == REVIEW_RECONCILED)} | "
        f"review_required={sum(1 for r in rows if r.review_status == REVIEW_REQUIRED)}"
    )
    return "\n".join(lines)


def reconciliation_table(issuer_id: str, database_url: str | None = None) -> str:
    """Render the source-linked reconciliation table for one issuer."""
    return render_table(rows_from_artifacts(issuer_id, database_url))


# ---------------------------------------------------------------------------
# IND-3 financial reconciliation (data/validation/india_reconciliation.csv)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DocumentProvenance:
    """Identity of one source document (from the committed manifest)."""

    document_id: str
    issuer_id: str
    file_name: str
    storage_path: str | None  # storage-relative cache copy, when one exists
    source_url: str
    sha256: str
    seq_id: str | None
    broadcast: str | None  # "YYYY-MM-DD HH:MM:SS" IST listing timestamp
    revision: str | None
    audited_status: str | None


#: Rendered-document cross-references verified by text extraction on 2026-09-11
#: (agent-checked; see docs/india_reconciliation_review.md for the full table).
#: Keyed by (issuer_id, document_id, context_kind, tag) -> (reference document
#: id, page/location, displayed value, comparison status, notes).
ReferenceKey = tuple[str, str, str, str]


@dataclass(frozen=True)
class RenderedReference:
    """One agent-verified rendered-document reference for reconciliation rows."""

    reference_document_id: str
    page_or_location: str
    display_value: str
    comparison_status: str
    notes: str


_DOC_INFY_Q1 = "INFY-Q1FY27-consolidated-nse-integrated-filing-xbrl.xml"
_DOC_INFY_Q4 = "INFY-Q4FY26-consolidated-nse-integrated-filing-xbrl.xml"
_DOC_HUL_Q1 = "HUL-Q1FY27-consolidated-nse-integrated-filing-xbrl.xml"
_DOC_HUL_Q4 = "HUL-Q4FY26-consolidated-nse-integrated-filing-xbrl.xml"

REF_INFY_REG33_Q1 = "INFY-IR-q1-fy27-financial-results-auditorsreports.pdf"
REF_INFY_CONSO_Q1 = "INFY-IR-consol-fy27-q1-finstatement.pdf"
REF_INFY_CONSO_Q4 = "INFY-IR-consol-fy26-q4-and-12m-finstatement.pdf"
REF_HUL_Q4_PDF = "HUL-IR-hul-mq26-financial-results.pdf"
REF_HUL_Q1_PDF = "HUL-IR-hul-jq26-financial-results.pdf"
REF_TCS_CONSO_Q1 = "TCS-IR-tcs-q1fy27-consolidated-indas-finstatement.pdf"
REF_HCLTECH_REG33_Q1 = "HCLTECH-IR-hcltech-q1fy27-unaudited-financial-results.pdf"
REF_ITC_CFS_Q1 = "ITC-IR-itc-q1fy27-financial-result-cfs.pdf"
REF_ASIANPAINT_RESULTS_Q1 = "ASIANPAINT-IR-asianpaints-q1fy27-results.pdf"
REF_MARUTI_RESULTS_Q1 = "MARUTI-IR-maruti-q1fy27-unaudited-financial-results.pdf"
REF_ULTRACEMCO_RESULTS_Q1 = "ULTRACEMCO-IR-ultratech-q1fy27-results.pdf"
REF_SUNPHARMA_RESULTS_Q1 = "SUNPHARMA-IR-sunpharma-q1fy27-financial-results.pdf"
REF_LT_RESULTS_Q1 = "LT-IR-lt-q1fy27-financial-results.pdf"

_INFY_P12_NOTE = (
    "Reg-33 consolidated P&L page 12; columns: Q1 FY27 (Jun-30-2026), Q4 FY26 (Mar-31-2026), "
    "Q1 FY26 (Jun-30-2025), FY26 (year ended Mar-31-2026); header declares '(in ₹ crore, except "
    "per equity share data)'"
)
_HUL_SCRAMBLED_NOTE = (
    "HUL renders a 4-column P&L (Jun-2026 qtr, Jun-2025 qtr, Mar-2026 qtr, FY26); linear PDF text "
    "extraction interleaves the columns (e.g. component values of the Mar-2026 quarter appear on "
    "the Jun-2026 rows), so no value-level automated match is reliable — manual reading required, "
    "never guessed"
)
_HUL_CAPAFX_SIGN_NOTE = (
    "sign convention: the rendered statement shows investing outflows parenthesized; the XBRL "
    "purchase facts are positive magnitudes"
)

_INFY_SCRAMBLE_FREE = (
    "Infosys P&L renders one line per item with a plain number per column; linear extraction "
    "preserves label/value adjacency (verified token-unique on the page)"
)

_SCRAMBLED = RenderedReference("", "", "", COMPARISON_SCRAMBLED, _HUL_SCRAMBLED_NOTE)
_UNAVAILABLE = RenderedReference(
    "", "", "", COMPARISON_UNAVAILABLE, "no rendered-document reference established"
)

_PBT_HUL_BASIS_NOTE = (
    "basis difference recorded, never forced: XBRL ProfitBeforeTax (13,827 Cr FY26) = PBIT 14,062 + "
    "exceptional (-235), i.e. BEFORE the share of equity-accounted investees; the results letter "
    "(p.1) states consolidated PBT of 13,812 Cr 'from continuing operations' = 13,827 - 15 share of "
    "JV loss; same +15 offset appears in continuing PAT (XBRL 10,667 vs letter 10,652)"
)

#: (issuer, source document, context kind, tag) -> rendered reference.
RENDERED_REFERENCES: dict[ReferenceKey, RenderedReference] = {
    # --- INFY: Reg-33 consolidated P&L, PDF page 12 (all four columns) ---
    ("IN-INFY", _DOC_INFY_Q1, "quarter", "RevenueFromOperations"): RenderedReference(
        REF_INFY_REG33_Q1,
        "PDF p.12 (consolidated P&L)",
        "48,211",
        COMPARISON_MATCHED,
        _INFY_P12_NOTE,
    ),
    ("IN-INFY", _DOC_INFY_Q1, "quarter", "Income"): RenderedReference(
        REF_INFY_REG33_Q1,
        "PDF p.12 (consolidated P&L)",
        "49,195",
        COMPARISON_MATCHED,
        _INFY_P12_NOTE,
    ),
    ("IN-INFY", _DOC_INFY_Q1, "quarter", "ProfitBeforeExceptionalItemsAndTax"): RenderedReference(
        REF_INFY_REG33_Q1,
        "PDF p.12 (consolidated P&L)",
        "11,028",
        COMPARISON_MATCHED,
        _INFY_P12_NOTE,
    ),
    ("IN-INFY", _DOC_INFY_Q1, "quarter", "ProfitBeforeTax"): RenderedReference(
        REF_INFY_REG33_Q1,
        "PDF p.12 (consolidated P&L)",
        "11,028",
        COMPARISON_MATCHED,
        _INFY_P12_NOTE + "; Q1/Q4 exceptional items are nil so PBT equals PBIT",
    ),
    ("IN-INFY", _DOC_INFY_Q1, "quarter", "ProfitLossForPeriod"): RenderedReference(
        REF_INFY_REG33_Q1,
        "PDF p.12 (consolidated P&L)",
        "7,775",
        COMPARISON_MATCHED,
        _INFY_P12_NOTE,
    ),
    (
        "IN-INFY",
        _DOC_INFY_Q1,
        "quarter",
        "ProfitLossForPeriodFromContinuingOperations",
    ): RenderedReference(
        REF_INFY_REG33_Q1,
        "PDF p.12 (consolidated P&L)",
        "7,775",
        COMPARISON_MATCHED,
        _INFY_P12_NOTE + "; no discontinued operations in Q1 FY27, continuing = total",
    ),
    (
        "IN-INFY",
        _DOC_INFY_Q1,
        "quarter",
        "ProfitOrLossAttributableToOwnersOfParent",
    ): RenderedReference(
        REF_INFY_REG33_Q1,
        "PDF p.12 (consolidated P&L)",
        "7,769",
        COMPARISON_MATCHED,
        _INFY_P12_NOTE + "; 'Profit attributable to: Owners of the company'",
    ),
    ("IN-INFY", _DOC_INFY_Q1, "quarter", "ExceptionalItemsBeforeTax"): RenderedReference(
        REF_INFY_REG33_Q1,
        "PDF p.12 (consolidated P&L)",
        "- (nil)",
        COMPARISON_MATCHED,
        _INFY_P12_NOTE + "; rendered as '-' for both quarters",
    ),
    (
        "IN-INFY",
        _DOC_INFY_Q1,
        "quarter",
        "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
    ): RenderedReference(
        REF_INFY_REG33_Q1,
        "PDF p.12 (EPS table)",
        "19.19",
        COMPARISON_MATCHED,
        _INFY_P12_NOTE + "; footnote: EPS not annualized for interim quarters",
    ),
    (
        "IN-INFY",
        _DOC_INFY_Q1,
        "quarter",
        "BasicEarningsLossPerShareFromContinuingOperations",
    ): RenderedReference(
        REF_INFY_REG33_Q1,
        "PDF p.12 (EPS table)",
        "19.19",
        COMPARISON_MATCHED,
        _INFY_P12_NOTE + "; no discontinued operations, continuing = total",
    ),
    (
        "IN-INFY",
        _DOC_INFY_Q1,
        "quarter",
        "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
    ): RenderedReference(
        REF_INFY_REG33_Q1, "PDF p.12 (EPS table)", "19.17", COMPARISON_MATCHED, _INFY_P12_NOTE
    ),
    (
        "IN-INFY",
        _DOC_INFY_Q1,
        "quarter",
        "DilutedEarningsLossPerShareFromContinuingOperations",
    ): RenderedReference(
        REF_INFY_REG33_Q1,
        "PDF p.12 (EPS table)",
        "19.17",
        COMPARISON_MATCHED,
        _INFY_P12_NOTE + "; no discontinued operations, continuing = total",
    ),
    # --- INFY Q4 quarter column (same Reg-33 page 12) ---
    ("IN-INFY", _DOC_INFY_Q4, "quarter", "RevenueFromOperations"): RenderedReference(
        REF_INFY_REG33_Q1,
        "PDF p.12 (consolidated P&L, 'Quarter ended March 31, 2026')",
        "46,402",
        COMPARISON_MATCHED,
        _INFY_P12_NOTE,
    ),
    ("IN-INFY", _DOC_INFY_Q4, "quarter", "Income"): RenderedReference(
        REF_INFY_REG33_Q1,
        "PDF p.12 (consolidated P&L, 'Quarter ended March 31, 2026')",
        "47,561",
        COMPARISON_MATCHED,
        _INFY_P12_NOTE,
    ),
    ("IN-INFY", _DOC_INFY_Q4, "quarter", "ProfitBeforeExceptionalItemsAndTax"): RenderedReference(
        REF_INFY_REG33_Q1,
        "PDF p.12 (consolidated P&L, 'Quarter ended March 31, 2026')",
        "10,797",
        COMPARISON_MATCHED,
        _INFY_P12_NOTE,
    ),
    ("IN-INFY", _DOC_INFY_Q4, "quarter", "ProfitBeforeTax"): RenderedReference(
        REF_INFY_REG33_Q1,
        "PDF p.12 (consolidated P&L, 'Quarter ended March 31, 2026')",
        "10,797",
        COMPARISON_MATCHED,
        _INFY_P12_NOTE + "; Q4 exceptional nil in the quarter",
    ),
    ("IN-INFY", _DOC_INFY_Q4, "quarter", "ProfitLossForPeriod"): RenderedReference(
        REF_INFY_REG33_Q1,
        "PDF p.12 (consolidated P&L, 'Quarter ended March 31, 2026')",
        "8,509",
        COMPARISON_MATCHED,
        _INFY_P12_NOTE,
    ),
    (
        "IN-INFY",
        _DOC_INFY_Q4,
        "quarter",
        "ProfitLossForPeriodFromContinuingOperations",
    ): RenderedReference(
        REF_INFY_REG33_Q1,
        "PDF p.12 (consolidated P&L, 'Quarter ended March 31, 2026')",
        "8,509",
        COMPARISON_MATCHED,
        _INFY_P12_NOTE + "; no discontinued operations in Q4 FY26",
    ),
    (
        "IN-INFY",
        _DOC_INFY_Q4,
        "quarter",
        "ProfitOrLossAttributableToOwnersOfParent",
    ): RenderedReference(
        REF_INFY_REG33_Q1,
        "PDF p.12 (consolidated P&L, 'Quarter ended March 31, 2026')",
        "8,501",
        COMPARISON_MATCHED,
        _INFY_P12_NOTE,
    ),
    ("IN-INFY", _DOC_INFY_Q4, "quarter", "ExceptionalItemsBeforeTax"): RenderedReference(
        REF_INFY_REG33_Q1,
        "PDF p.12 (consolidated P&L, 'Quarter ended March 31, 2026')",
        "- (nil)",
        COMPARISON_MATCHED,
        _INFY_P12_NOTE,
    ),
    (
        "IN-INFY",
        _DOC_INFY_Q4,
        "quarter",
        "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
    ): RenderedReference(
        REF_INFY_REG33_Q1, "PDF p.12 (EPS table)", "21.01", COMPARISON_MATCHED, _INFY_P12_NOTE
    ),
    (
        "IN-INFY",
        _DOC_INFY_Q4,
        "quarter",
        "BasicEarningsLossPerShareFromContinuingOperations",
    ): RenderedReference(
        REF_INFY_REG33_Q1,
        "PDF p.12 (EPS table)",
        "21.01",
        COMPARISON_MATCHED,
        _INFY_P12_NOTE + "; no discontinued operations, continuing = total",
    ),
    (
        "IN-INFY",
        _DOC_INFY_Q4,
        "quarter",
        "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
    ): RenderedReference(
        REF_INFY_REG33_Q1, "PDF p.12 (EPS table)", "20.98", COMPARISON_MATCHED, _INFY_P12_NOTE
    ),
    (
        "IN-INFY",
        _DOC_INFY_Q4,
        "quarter",
        "DilutedEarningsLossPerShareFromContinuingOperations",
    ): RenderedReference(
        REF_INFY_REG33_Q1,
        "PDF p.12 (EPS table)",
        "20.98",
        COMPARISON_MATCHED,
        _INFY_P12_NOTE + "; no discontinued operations, continuing = total",
    ),
    # --- INFY FY26 annual column (same Reg-33 page 12) ---
    ("IN-INFY", _DOC_INFY_Q4, "annual", "RevenueFromOperations"): RenderedReference(
        REF_INFY_REG33_Q1,
        "PDF p.12 (consolidated P&L, 'Year ended March 31, 2026')",
        "178,650",
        COMPARISON_MATCHED,
        _INFY_P12_NOTE,
    ),
    ("IN-INFY", _DOC_INFY_Q4, "annual", "Income"): RenderedReference(
        REF_INFY_REG33_Q1,
        "PDF p.12 (consolidated P&L, 'Year ended March 31, 2026')",
        "182,972",
        COMPARISON_MATCHED,
        _INFY_P12_NOTE,
    ),
    ("IN-INFY", _DOC_INFY_Q4, "annual", "ProfitBeforeExceptionalItemsAndTax"): RenderedReference(
        REF_INFY_REG33_Q1,
        "PDF p.12 (consolidated P&L, 'Year ended March 31, 2026')",
        "41,284",
        COMPARISON_MATCHED,
        _INFY_P12_NOTE,
    ),
    ("IN-INFY", _DOC_INFY_Q4, "annual", "ProfitBeforeTax"): RenderedReference(
        REF_INFY_REG33_Q1,
        "PDF p.12 (consolidated P&L, 'Year ended March 31, 2026')",
        "39,995",
        COMPARISON_MATCHED,
        _INFY_P12_NOTE + "; 41,284 - 1,289 exceptional = 39,995",
    ),
    ("IN-INFY", _DOC_INFY_Q4, "annual", "ProfitLossForPeriod"): RenderedReference(
        REF_INFY_REG33_Q1,
        "PDF p.12 (consolidated P&L, 'Year ended March 31, 2026')",
        "29,474",
        COMPARISON_MATCHED,
        _INFY_P12_NOTE,
    ),
    (
        "IN-INFY",
        _DOC_INFY_Q4,
        "annual",
        "ProfitLossForPeriodFromContinuingOperations",
    ): RenderedReference(
        REF_INFY_REG33_Q1,
        "PDF p.12 (consolidated P&L, 'Year ended March 31, 2026')",
        "29,474",
        COMPARISON_MATCHED,
        _INFY_P12_NOTE + "; no discontinued operations in FY26",
    ),
    (
        "IN-INFY",
        _DOC_INFY_Q4,
        "annual",
        "ProfitOrLossAttributableToOwnersOfParent",
    ): RenderedReference(
        REF_INFY_REG33_Q1,
        "PDF p.12 (consolidated P&L, 'Year ended March 31, 2026')",
        "29,440",
        COMPARISON_MATCHED,
        _INFY_P12_NOTE,
    ),
    ("IN-INFY", _DOC_INFY_Q4, "annual", "ExceptionalItemsBeforeTax"): RenderedReference(
        REF_INFY_REG33_Q1,
        "PDF p.12 (consolidated P&L, 'Year ended March 31, 2026')",
        "1,289",
        COMPARISON_SCOPE_BASIS,
        _INFY_P12_NOTE
        + "; SIGN CONVENTION: rendered P&L prints the exceptional item (Impact of Labour Codes) as a "
        "positive 1,289 expense deducted to reach PBT; the XBRL fact signs it -12,890,000,000 "
        "(exceptional = PBT - PBIT). Same magnitude, opposite display sign — recorded, never coerced",
    ),
    (
        "IN-INFY",
        _DOC_INFY_Q4,
        "annual",
        "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
    ): RenderedReference(
        REF_INFY_REG33_Q1, "PDF p.12 (EPS table)", "71.58", COMPARISON_MATCHED, _INFY_P12_NOTE
    ),
    (
        "IN-INFY",
        _DOC_INFY_Q4,
        "annual",
        "BasicEarningsLossPerShareFromContinuingOperations",
    ): RenderedReference(
        REF_INFY_REG33_Q1,
        "PDF p.12 (EPS table)",
        "71.58",
        COMPARISON_MATCHED,
        _INFY_P12_NOTE + "; no discontinued operations, continuing = total",
    ),
    (
        "IN-INFY",
        _DOC_INFY_Q4,
        "annual",
        "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
    ): RenderedReference(
        REF_INFY_REG33_Q1, "PDF p.12 (EPS table)", "71.46", COMPARISON_MATCHED, _INFY_P12_NOTE
    ),
    (
        "IN-INFY",
        _DOC_INFY_Q4,
        "annual",
        "DilutedEarningsLossPerShareFromContinuingOperations",
    ): RenderedReference(
        REF_INFY_REG33_Q1,
        "PDF p.12 (EPS table)",
        "71.46",
        COMPARISON_MATCHED,
        _INFY_P12_NOTE + "; no discontinued operations, continuing = total",
    ),
    # --- INFY cash flow (annual, from the Q4 consolidated condensed FS) ---
    (
        "IN-INFY",
        _DOC_INFY_Q4,
        "annual",
        "CashFlowsFromUsedInOperatingActivities",
    ): RenderedReference(
        REF_INFY_CONSO_Q4,
        "PDF p.6 (Condensed Consolidated Statement of Cash Flows)",
        "33,986",
        COMPARISON_MATCHED,
        _INFY_SCRAMBLE_FREE,
    ),
    (
        "IN-INFY",
        _DOC_INFY_Q4,
        "annual",
        "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
    ): RenderedReference(
        REF_INFY_CONSO_Q4,
        "PDF p.6 (cash-flow investing section)",
        "(2,727)",
        COMPARISON_MATCHED,
        _INFY_SCRAMBLE_FREE
        + "; value token unique on page; rendered as a parenthesized investing outflow, XBRL fact is "
        "the positive magnitude 27,270,000,000 (sign convention, not a discrepancy)",
    ),
    # INFY intangible purchases (XBRL 0, FY26): no rendered reference established
    # here — falls back to no_rendered_comparison_available / human_review_pending.
    # --- HUL cash flow + exceptional (rendered, verified tokens unique on page) ---
    (
        "IN-HINDUNILVR",
        _DOC_HUL_Q4,
        "annual",
        "CashFlowsFromUsedInOperatingActivities",
    ): RenderedReference(
        REF_HUL_Q4_PDF,
        "PDF p.11 (consolidated 'A CASH FLOWS FROM OPERATING ACTIVITIES')",
        "10,999",
        COMPARISON_MATCHED,
        "'(Rs in Crores)' header; 'Year ended 31st March, 2026'; 'Net cash flows generated from "
        "operating activities - [A] 10,999' (token unique on page)",
    ),
    (
        "IN-HINDUNILVR",
        _DOC_HUL_Q4,
        "annual",
        "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
    ): RenderedReference(
        REF_HUL_Q4_PDF,
        "PDF p.11 (consolidated investing section)",
        "(1,258)",
        COMPARISON_MATCHED,
        "'Purchase of property, plant and equipment (1,258)'. " + _HUL_CAPAFX_SIGN_NOTE,
    ),
    (
        "IN-HINDUNILVR",
        _DOC_HUL_Q4,
        "annual",
        "PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities",
    ): RenderedReference(
        REF_HUL_Q4_PDF,
        "PDF p.11 (consolidated investing section)",
        "(103)",
        COMPARISON_MATCHED,
        "'Purchase of intangible assets (103)'. " + _HUL_CAPAFX_SIGN_NOTE,
    ),
    ("IN-HINDUNILVR", _DOC_HUL_Q4, "annual", "ExceptionalItemsBeforeTax"): RenderedReference(
        REF_HUL_Q4_PDF,
        "PDF p.1 (Board-approval results letter)",
        "loss of Rs. 235 crores",
        COMPARISON_MATCHED,
        "SIGN CONVENTION: HUL signs exceptional items as their profit impact — the letter states FY26 "
        "exceptional items 'amounted to a loss of Rs. 235 crores' and the XBRL fact is -2,350,000,000 "
        "(−235 Cr); the Q4-quarter fact is +2,470,000,000 (+247 Cr gain, demerger-related)",
    ),
    ("IN-HINDUNILVR", _DOC_HUL_Q4, "annual", "ProfitBeforeTax"): RenderedReference(
        REF_HUL_Q4_PDF,
        "PDF p.1 (Board-approval results letter)",
        "13,812 (continuing operations)",
        COMPARISON_SCOPE_BASIS,
        _PBT_HUL_BASIS_NOTE,
    ),
    (
        "IN-HINDUNILVR",
        _DOC_HUL_Q4,
        "annual",
        "ProfitLossForPeriodFromContinuingOperations",
    ): RenderedReference(
        REF_HUL_Q4_PDF,
        "PDF p.1 (Board-approval results letter)",
        "10,652 (continuing operations)",
        COMPARISON_SCOPE_BASIS,
        _PBT_HUL_BASIS_NOTE
        + "; XBRL continuing PAT 10,667 vs letter 10,652 — same 15 Cr equity-accounted presentation "
        "difference; recorded, never forced",
    ),
    # -------------------------------------------------------------------
    # IND-6 group A/B issuers: Q1 FY27 consolidated rows whose CURRENT-quarter
    # column was verified against the issuer's own rendered Q1 statement
    # (value-anchored extraction, pdf_results.extract_prior_year_comparatives:
    # the displayed current value equals the XBRL fact exactly, which also
    # verifies the declared display scale). Prior-year columns from the same
    # rows are carried in REVIEWED_PDF_COMPARATIVES below.
    # -------------------------------------------------------------------
}

#: Notes for the IND-6 rendered references (shared per issuer).
_TCS_P2_NOTE = (
    "TCS condensed consolidated Ind AS statement p.2; two columns: 'Three months ended "
    "June 30, 2026' then 'Three months ended June 30, 2025'; '(₹ crore)' header; linear "
    "extraction preserves row-label/value adjacency"
)
_HCLTECH_P2_NOTE = (
    "HCLTech Reg-33 consolidated results p.2; columns: three months ended 30-June-2026 "
    "(Unaudited), 31-March-2026 (Audited), 30-June-2025 (Unaudited); ₹ in crores; row-major "
    "three-value blocks"
)
_ITC_P1_NOTE = (
    "ITC consolidated results (cfs) p.1; columns: 3 months ended 30.06.2026 (Unaudited), "
    "30.06.2025 (Unaudited), 31.03.2026 (Audited), twelve months ended 31.03.2026 (Audited); "
    "'(₹ in Crores)' header"
)
_ASIANPAINT_P10_NOTE = (
    "Asian Paints Reg-33 consolidated results p.10; quarter block columns: 30.06.2026 "
    "(Unaudited), 31.03.2026 (Audited), 30.06.2025 (Unaudited) with the year column in a "
    "separate block; '(₹ in Crores)'; row identity verified by the internal identity "
    "(a) + (b) = (1) across all three columns plus the XBRL anchors"
)
_SUNPHARMA_P1_NOTE = (
    "Sun Pharma consolidated results statement p.1; columns: 30.06.2026 (Unaudited), "
    "31.03.2026 (Audited), 30.06.2025 (Unaudited), year ended 31.03.2026 (Audited); "
    "declared display unit is ₹ MILLION (the instance declares LevelOfRounding=Millions)"
)
_LT_P1_NOTE = (
    "L&T consolidated results statement p.1; columns: June 30, 2026 (Reviewed), March 31, "
    "2026 (Audited), June 30, 2025 (Reviewed), year ended March 31, 2026 (Audited); ₹ Crore"
)
_ITC_PBT_BASIS_NOTE = (
    "BASIS DIFFERENCE recorded, never forced: ITC's rendered 'Profit before tax' line "
    "(5,860.85) INCLUDES the 85.91 share of profit/loss of associates and JV, while the XBRL "
    "ProfitBeforeTax fact (5,774.94) EXCLUDES it: rendered 5,369.06 (before exceptional, "
    "before associate share) + 405.88 exceptional gain + 85.91 associate share = 5,860.85; "
    "XBRL: 5,369.06 + 405.88 = 5,774.94. Both identities reconcile exactly"
)

_Q1_DOC_IDS = {
    "IN-TCS": "TCS-Q1FY27-consolidated-nse-integrated-filing-xbrl.xml",
    "IN-HCLTECH": "HCLTECH-Q1FY27-consolidated-nse-integrated-filing-xbrl.xml",
    "IN-ITC": "ITC-Q1FY27-consolidated-nse-integrated-filing-xbrl.xml",
    "IN-ASIANPAINT": "ASIANPAINT-Q1FY27-consolidated-nse-integrated-filing-xbrl.xml",
    "IN-SUNPHARMA": "SUNPHARMA-Q1FY27-consolidated-nse-integrated-filing-xbrl.xml",
    "IN-LT": "LT-Q1FY27-consolidated-nse-integrated-filing-xbrl.xml",
}

#: (issuer, Q1 XBRL doc, "quarter", tag) -> rendered current-quarter cross-check.
_INDUCE6_RENDERED: dict[ReferenceKey, RenderedReference] = {
    ("IN-TCS", _Q1_DOC_IDS["IN-TCS"], "quarter", "RevenueFromOperations"): RenderedReference(
        REF_TCS_CONSO_Q1,
        "PDF p.2 (Condensed Consolidated Statement of Profit and Loss)",
        "72,275",
        COMPARISON_MATCHED,
        _TCS_P2_NOTE,
    ),
    (
        "IN-TCS",
        _Q1_DOC_IDS["IN-TCS"],
        "quarter",
        "ProfitBeforeExceptionalItemsAndTax",
    ): RenderedReference(
        REF_TCS_CONSO_Q1,
        "PDF p.2",
        "18,612",
        COMPARISON_MATCHED,
        _TCS_P2_NOTE,
    ),
    ("IN-TCS", _Q1_DOC_IDS["IN-TCS"], "quarter", "ProfitBeforeTax"): RenderedReference(
        REF_TCS_CONSO_Q1,
        "PDF p.2",
        "17,944",
        COMPARISON_MATCHED,
        _TCS_P2_NOTE + "; 18,612 - 668 exceptional (legal claim) = 17,944",
    ),
    ("IN-TCS", _Q1_DOC_IDS["IN-TCS"], "quarter", "ProfitLossForPeriod"): RenderedReference(
        REF_TCS_CONSO_Q1,
        "PDF p.2",
        "13,420",
        COMPARISON_MATCHED,
        _TCS_P2_NOTE,
    ),
    (
        "IN-TCS",
        _Q1_DOC_IDS["IN-TCS"],
        "quarter",
        "ProfitOrLossAttributableToOwnersOfParent",
    ): RenderedReference(
        REF_TCS_CONSO_Q1,
        "PDF p.2",
        "13,349",
        COMPARISON_MATCHED,
        _TCS_P2_NOTE + "; 'Profit for the period attributable to: Shareholders of the Company'",
    ),
    (
        "IN-TCS",
        _Q1_DOC_IDS["IN-TCS"],
        "quarter",
        "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
    ): RenderedReference(
        REF_TCS_CONSO_Q1,
        "PDF p.2 (EPS table)",
        "36.90",
        COMPARISON_MATCHED,
        _TCS_P2_NOTE + "; TCS prints one 'Basic and diluted' row (identical values)",
    ),
    (
        "IN-TCS",
        _Q1_DOC_IDS["IN-TCS"],
        "quarter",
        "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
    ): RenderedReference(
        REF_TCS_CONSO_Q1,
        "PDF p.2 (EPS table)",
        "36.90",
        COMPARISON_MATCHED,
        _TCS_P2_NOTE,
    ),
    (
        "IN-HCLTECH",
        _Q1_DOC_IDS["IN-HCLTECH"],
        "quarter",
        "RevenueFromOperations",
    ): RenderedReference(
        REF_HCLTECH_REG33_Q1,
        "PDF p.2 (consolidated Reg-33 statement)",
        "34,579",
        COMPARISON_MATCHED,
        _HCLTECH_P2_NOTE,
    ),
    (
        "IN-HCLTECH",
        _Q1_DOC_IDS["IN-HCLTECH"],
        "quarter",
        "ProfitBeforeExceptionalItemsAndTax",
    ): RenderedReference(
        REF_HCLTECH_REG33_Q1,
        "PDF p.2",
        "6,108",
        COMPARISON_MATCHED,
        _HCLTECH_P2_NOTE + "; exceptional items nil in Q1 FY27, PBT equals PBIT",
    ),
    ("IN-HCLTECH", _Q1_DOC_IDS["IN-HCLTECH"], "quarter", "ProfitBeforeTax"): RenderedReference(
        REF_HCLTECH_REG33_Q1,
        "PDF p.2",
        "6,108",
        COMPARISON_MATCHED,
        _HCLTECH_P2_NOTE,
    ),
    ("IN-HCLTECH", _Q1_DOC_IDS["IN-HCLTECH"], "quarter", "ProfitLossForPeriod"): RenderedReference(
        REF_HCLTECH_REG33_Q1,
        "PDF p.2",
        "4,626",
        COMPARISON_MATCHED,
        _HCLTECH_P2_NOTE,
    ),
    (
        "IN-HCLTECH",
        _Q1_DOC_IDS["IN-HCLTECH"],
        "quarter",
        "ProfitOrLossAttributableToOwnersOfParent",
    ): RenderedReference(
        REF_HCLTECH_REG33_Q1,
        "PDF p.2",
        "4,624",
        COMPARISON_MATCHED,
        _HCLTECH_P2_NOTE,
    ),
    (
        "IN-HCLTECH",
        _Q1_DOC_IDS["IN-HCLTECH"],
        "quarter",
        "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
    ): RenderedReference(
        REF_HCLTECH_REG33_Q1,
        "PDF p.2 (EPS table)",
        "17.09",
        COMPARISON_MATCHED,
        _HCLTECH_P2_NOTE,
    ),
    (
        "IN-HCLTECH",
        _Q1_DOC_IDS["IN-HCLTECH"],
        "quarter",
        "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
    ): RenderedReference(
        REF_HCLTECH_REG33_Q1,
        "PDF p.2 (EPS table)",
        "17.06",
        COMPARISON_MATCHED,
        _HCLTECH_P2_NOTE,
    ),
    ("IN-ITC", _Q1_DOC_IDS["IN-ITC"], "quarter", "RevenueFromOperations"): RenderedReference(
        REF_ITC_CFS_Q1,
        "PDF p.1 (consolidated results statement)",
        "29,523.30",
        COMPARISON_MATCHED,
        _ITC_P1_NOTE,
    ),
    (
        "IN-ITC",
        _Q1_DOC_IDS["IN-ITC"],
        "quarter",
        "ProfitBeforeExceptionalItemsAndTax",
    ): RenderedReference(
        REF_ITC_CFS_Q1,
        "PDF p.1",
        "5,369.06",
        COMPARISON_MATCHED,
        _ITC_P1_NOTE,
    ),
    ("IN-ITC", _Q1_DOC_IDS["IN-ITC"], "quarter", "ProfitBeforeTax"): RenderedReference(
        REF_ITC_CFS_Q1,
        "PDF p.1",
        "5,860.85 (rendered, incl. 85.91 associate share) vs 5,774.94 (XBRL, excl.)",
        COMPARISON_SCOPE_BASIS,
        _ITC_P1_NOTE + "; " + _ITC_PBT_BASIS_NOTE,
    ),
    ("IN-ITC", _Q1_DOC_IDS["IN-ITC"], "quarter", "ProfitLossForPeriod"): RenderedReference(
        REF_ITC_CFS_Q1,
        "PDF p.1",
        "4,508.79",
        COMPARISON_MATCHED,
        _ITC_P1_NOTE,
    ),
    (
        "IN-ITC",
        _Q1_DOC_IDS["IN-ITC"],
        "quarter",
        "ProfitOrLossAttributableToOwnersOfParent",
    ): RenderedReference(
        REF_ITC_CFS_Q1,
        "PDF p.1",
        "4,394.13",
        COMPARISON_MATCHED,
        _ITC_P1_NOTE,
    ),
    (
        "IN-ITC",
        _Q1_DOC_IDS["IN-ITC"],
        "quarter",
        "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
    ): RenderedReference(
        REF_ITC_CFS_Q1,
        "PDF p.1 (EPS table)",
        "3.51",
        COMPARISON_MATCHED,
        _ITC_P1_NOTE,
    ),
    (
        "IN-ITC",
        _Q1_DOC_IDS["IN-ITC"],
        "quarter",
        "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
    ): RenderedReference(
        REF_ITC_CFS_Q1,
        "PDF p.1 (EPS table)",
        "3.51",
        COMPARISON_MATCHED,
        _ITC_P1_NOTE,
    ),
    (
        "IN-ASIANPAINT",
        _Q1_DOC_IDS["IN-ASIANPAINT"],
        "quarter",
        "RevenueFromOperations",
    ): RenderedReference(
        REF_ASIANPAINT_RESULTS_Q1,
        "PDF p.10 (consolidated Reg-33 statement)",
        "10,541.94",
        COMPARISON_MATCHED,
        _ASIANPAINT_P10_NOTE,
    ),
    (
        "IN-ASIANPAINT",
        _Q1_DOC_IDS["IN-ASIANPAINT"],
        "quarter",
        "ProfitBeforeTax",
    ): RenderedReference(
        REF_ASIANPAINT_RESULTS_Q1,
        "PDF p.10",
        "2,095.75",
        COMPARISON_MATCHED,
        _ASIANPAINT_P10_NOTE,
    ),
    (
        "IN-ASIANPAINT",
        _Q1_DOC_IDS["IN-ASIANPAINT"],
        "quarter",
        "ProfitLossForPeriod",
    ): RenderedReference(
        REF_ASIANPAINT_RESULTS_Q1,
        "PDF p.10",
        "1,559.45",
        COMPARISON_MATCHED,
        _ASIANPAINT_P10_NOTE,
    ),
    (
        "IN-ASIANPAINT",
        _Q1_DOC_IDS["IN-ASIANPAINT"],
        "quarter",
        "ProfitOrLossAttributableToOwnersOfParent",
    ): RenderedReference(
        REF_ASIANPAINT_RESULTS_Q1,
        "PDF p.10",
        "1,539.25",
        COMPARISON_MATCHED,
        _ASIANPAINT_P10_NOTE,
    ),
    (
        "IN-ASIANPAINT",
        _Q1_DOC_IDS["IN-ASIANPAINT"],
        "quarter",
        "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
    ): RenderedReference(
        REF_ASIANPAINT_RESULTS_Q1,
        "PDF p.10 (EPS table)",
        "16.06",
        COMPARISON_MATCHED,
        _ASIANPAINT_P10_NOTE,
    ),
    (
        "IN-ASIANPAINT",
        _Q1_DOC_IDS["IN-ASIANPAINT"],
        "quarter",
        "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
    ): RenderedReference(
        REF_ASIANPAINT_RESULTS_Q1,
        "PDF p.10 (EPS table)",
        "16.05",
        COMPARISON_MATCHED,
        _ASIANPAINT_P10_NOTE,
    ),
    (
        "IN-SUNPHARMA",
        _Q1_DOC_IDS["IN-SUNPHARMA"],
        "quarter",
        "RevenueFromOperations",
    ): RenderedReference(
        REF_SUNPHARMA_RESULTS_Q1,
        "PDF p.1 (consolidated results statement)",
        "152,998.8 (₹ million)",
        COMPARISON_MATCHED,
        _SUNPHARMA_P1_NOTE,
    ),
    ("IN-SUNPHARMA", _Q1_DOC_IDS["IN-SUNPHARMA"], "quarter", "ProfitBeforeTax"): RenderedReference(
        REF_SUNPHARMA_RESULTS_Q1,
        "PDF p.1",
        "40,988.1 (₹ million)",
        COMPARISON_MATCHED,
        _SUNPHARMA_P1_NOTE,
    ),
    (
        "IN-SUNPHARMA",
        _Q1_DOC_IDS["IN-SUNPHARMA"],
        "quarter",
        "ProfitLossForPeriod",
    ): RenderedReference(
        REF_SUNPHARMA_RESULTS_Q1,
        "PDF p.1",
        "29,012.3 (₹ million)",
        COMPARISON_MATCHED,
        _SUNPHARMA_P1_NOTE,
    ),
    (
        "IN-SUNPHARMA",
        _Q1_DOC_IDS["IN-SUNPHARMA"],
        "quarter",
        "ProfitOrLossAttributableToOwnersOfParent",
    ): RenderedReference(
        REF_SUNPHARMA_RESULTS_Q1,
        "PDF p.1",
        "28,947.9 (₹ million)",
        COMPARISON_MATCHED,
        _SUNPHARMA_P1_NOTE,
    ),
    (
        "IN-SUNPHARMA",
        _Q1_DOC_IDS["IN-SUNPHARMA"],
        "quarter",
        "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
    ): RenderedReference(
        REF_SUNPHARMA_RESULTS_Q1,
        "PDF p.1 (EPS table)",
        "12.1",
        COMPARISON_MATCHED,
        _SUNPHARMA_P1_NOTE,
    ),
    (
        "IN-SUNPHARMA",
        _Q1_DOC_IDS["IN-SUNPHARMA"],
        "quarter",
        "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
    ): RenderedReference(
        REF_SUNPHARMA_RESULTS_Q1,
        "PDF p.1 (EPS table)",
        "12.1",
        COMPARISON_MATCHED,
        _SUNPHARMA_P1_NOTE,
    ),
    ("IN-LT", _Q1_DOC_IDS["IN-LT"], "quarter", "RevenueFromOperations"): RenderedReference(
        REF_LT_RESULTS_Q1,
        "PDF p.1 (consolidated results statement)",
        "67,941.74",
        COMPARISON_MATCHED,
        _LT_P1_NOTE,
    ),
    ("IN-LT", _Q1_DOC_IDS["IN-LT"], "quarter", "ProfitBeforeTax"): RenderedReference(
        REF_LT_RESULTS_Q1,
        "PDF p.1",
        "6,922.26",
        COMPARISON_MATCHED,
        _LT_P1_NOTE,
    ),
    ("IN-LT", _Q1_DOC_IDS["IN-LT"], "quarter", "ProfitLossForPeriod"): RenderedReference(
        REF_LT_RESULTS_Q1,
        "PDF p.1",
        "4,988.03",
        COMPARISON_MATCHED,
        _LT_P1_NOTE,
    ),
    (
        "IN-LT",
        _Q1_DOC_IDS["IN-LT"],
        "quarter",
        "ProfitOrLossAttributableToOwnersOfParent",
    ): RenderedReference(
        REF_LT_RESULTS_Q1,
        "PDF p.1",
        "4,122.85",
        COMPARISON_MATCHED,
        _LT_P1_NOTE,
    ),
    (
        "IN-LT",
        _Q1_DOC_IDS["IN-LT"],
        "quarter",
        "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
    ): RenderedReference(
        REF_LT_RESULTS_Q1,
        "PDF p.1 (EPS table)",
        "29.97",
        COMPARISON_MATCHED,
        _LT_P1_NOTE,
    ),
    (
        "IN-LT",
        _Q1_DOC_IDS["IN-LT"],
        "quarter",
        "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
    ): RenderedReference(
        REF_LT_RESULTS_Q1,
        "PDF p.1 (EPS table)",
        "29.96",
        COMPARISON_MATCHED,
        _LT_P1_NOTE,
    ),
}

#: HUL Q4 P&L rows and all HUL Q1 rows except revenue: rendered pages 6/8
#: interleave columns (see _HUL_SCRAMBLED_NOTE) — pending manual reading.
_SCRAMBLED_KEYS: set[ReferenceKey] = set()

#: Explicit scrambled entry for HUL Q1 revenue: the rendered document itself
#: carries two candidate readings that only a human reading can resolve.
_HUL_Q1_REVENUE = RenderedReference(
    REF_HUL_Q1_PDF,
    "PDF p.6 (consolidated P&L) and p.7 (segment note)",
    "17,341 (segment note p.7, quarter ended 30.06.2026) vs 17,149 (P&L p.6 linear text)",
    COMPARISON_SCRAMBLED,
    _HUL_SCRAMBLED_NOTE
    + "; UNRESOLVED TENSION flagged for human review: the segment-note page shows total revenue "
    "17,341 Cr for the June-2026 quarter (consistent with the XBRL fact 17,341,000,000), while the "
    "P&L page's linear extraction yields 17,149 and its component rows (16,172 + 35 + 144 = 16,351) "
    "match the PRIOR (Mar-2026) quarter column — strong evidence of column interleaving, but the "
    "P&L line must be read manually; never guessed",
)


def _build_scrambled_keys() -> None:
    hul_pnl_tags = {
        "RevenueFromOperations",
        "Income",
        "ProfitBeforeTax",
        "ProfitBeforeExceptionalItemsAndTax",
        "ProfitLossForPeriod",
        "ProfitLossForPeriodFromContinuingOperations",
        "ProfitOrLossAttributableToOwnersOfParent",
        "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
        "BasicEarningsLossPerShareFromContinuingOperations",
        "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
        "DilutedEarningsLossPerShareFromContinuingOperations",
        "ExceptionalItemsBeforeTax",
    }
    for context_kind in ("quarter", "annual"):
        for tag in hul_pnl_tags:
            _SCRAMBLED_KEYS.add(("IN-HINDUNILVR", _DOC_HUL_Q4, context_kind, tag))
            _SCRAMBLED_KEYS.add(("IN-HINDUNILVR", _DOC_HUL_Q1, context_kind, tag))
    # IND-6: HUL's scrambling is not unique. Maruti's Q1 results PDF is a scan
    # whose OCR text layer garbles the consolidated statement (rows like
    # "<24 60R", "1*11 r6n" where 524,608 / 1,871,617 would be); UltraTech's
    # Reg-33 page extracts with vector artifacts ("130E....i.-", detached value
    # blocks) so row/column association is not machine-reliable for either.
    for issuer_id, document_id in (
        ("IN-MARUTI", "MARUTI-Q1FY27-consolidated-nse-integrated-filing-xbrl.xml"),
        ("IN-MARUTI", "MARUTI-Q4FY26-consolidated-nse-integrated-filing-xbrl.xml"),
        ("IN-ULTRACEMCO", "ULTRACEMCO-Q1FY27-consolidated-nse-integrated-filing-xbrl.xml"),
        ("IN-ULTRACEMCO", "ULTRACEMCO-Q4FY26-consolidated-nse-integrated-filing-xbrl.xml"),
    ):
        for context_kind in ("quarter", "annual"):
            for tag in hul_pnl_tags:
                _SCRAMBLED_KEYS.add((issuer_id, document_id, context_kind, tag))


_build_scrambled_keys()


def review_status_for(issuer_id: str, document_id: str, context_kind: str, tag: str) -> str | None:
    """Review status the IND-3 packet recorded for one reported fact.

    ``agent_checked_against_document`` for rows whose rendered reference matched
    (or recorded a scope/basis difference); ``human_review_pending`` for rows
    whose rendered comparison interleaved or was never established; ``None`` when
    the packet does not cover the fact at all (e.g. synthetic test observations).
    Human approval is never returned — no code path records it.
    """
    key: ReferenceKey = (issuer_id, document_id, context_kind, tag)
    reference = (
        RENDERED_REFERENCES.get(key) or _INDUCE6_RENDERED.get(key) or _RENDERED_OVERRIDES.get(key)
    )
    if reference is not None:
        if reference.comparison_status in (
            COMPARISON_MATCHED,
            COMPARISON_WITHIN_PRECISION,
            COMPARISON_SCOPE_BASIS,
        ):
            return REVIEW_AGENT_CHECKED
        return REVIEW_HUMAN_PENDING
    if key in _SCRAMBLED_KEYS:
        return REVIEW_HUMAN_PENDING
    return None


#: Explicit rendered references that OVERRIDE the scrambled fallback with a
#: documented (unresolved or recorded) outcome.
_RENDERED_OVERRIDES: dict[ReferenceKey, RenderedReference] = {
    ("IN-HINDUNILVR", _DOC_HUL_Q1, "quarter", "RevenueFromOperations"): RenderedReference(
        REF_HUL_Q1_PDF,
        "PDF p.6 (consolidated P&L, quarter ended 30.06.2026 column)",
        "17,341 (= 17,149 sale of products + 35 sale of services + 157 other operating revenue; "
        "the P&L prints no revenue subtotal)",
        COMPARISON_MATCHED,
        "VISUAL PAGE INSPECTION 2026-09-12 (owner-delegated): page rendered to an image and read; "
        "RESOLVES the earlier 17,149-vs-17,341 tension — 17,149 is the SALE-OF-PRODUCTS line, the "
        "subtotal is not printed, and the component sum 17,341 agrees with the segment note (p.7) "
        "and the XBRL fact. The linear-extraction reading was the component line, not an error in "
        "the stored fact",
    ),
}


#: -----------------------------------------------------------------------
#: Visual page inspection (2026-09-12, owner-delegated): the HUL Q1 and
#: Q4/annual consolidated P&L pages were rendered to images and read; every
#: value below was transcribed from the rendered page. These entries
#: supersede the linear-text ``extraction_scrambled`` fallback for their
#: keys. Human approval is still never recorded — these remain
#: agent-level verification.
_VISUAL_NOTE = (
    "VISUAL PAGE INSPECTION 2026-09-12 (owner-delegated): page rendered to an "
    "image and read; value transcribed from the rendered table"
)
_HUL_PBT_PLACEMENT_NOTE = (
    "BASIS DIFFERENCE visually confirmed: the printed 'Profit before tax' line "
    "sits AFTER the share of equity-accounted investee loss, while the XBRL "
    "ProfitBeforeTax fact sits BEFORE it; the difference equals the "
    "equity-accounted share exactly; recorded, never forced"
)
_HUL_COMPONENT_SUM_NOTE = (
    "the P&L prints no revenue subtotal; the displayed figure is the printed "
    "component sum (sale of products + sale of services + other operating revenue)"
)


def _build_visual_inspection_overrides() -> dict[ReferenceKey, RenderedReference]:
    overrides: dict[ReferenceKey, RenderedReference] = {}

    def add(doc, context, tag, ref, page, display, status, extra=""):
        key = ("IN-HINDUNILVR", doc, context, tag)
        if key in _RENDERED_OVERRIDES:
            return  # an explicit reference already resolves this row
        overrides[key] = RenderedReference(
            ref,
            page,
            display,
            status,
            (_VISUAL_NOTE + ("; " + extra if extra else "")),
        )

    # --- HUL Q1 FY2026-27 (quarter ended 30.06.2026): PDF p.6 -------------
    q1_page = "PDF p.6 (consolidated P&L, quarter ended 30.06.2026 column)"
    add(_DOC_HUL_Q1, "quarter", "Income", REF_HUL_Q1_PDF, q1_page,
        "17,529 (Total income)", COMPARISON_MATCHED)
    add(_DOC_HUL_Q1, "quarter", "ProfitBeforeExceptionalItemsAndTax", REF_HUL_Q1_PDF, q1_page,
        "3,707 (before exceptional items and tax; equity-accounted share nil this quarter)",
        COMPARISON_MATCHED)
    add(_DOC_HUL_Q1, "quarter", "ProfitBeforeTax", REF_HUL_Q1_PDF, q1_page,
        "3,632 (Profit before tax, after exceptional items (75))", COMPARISON_MATCHED)
    add(_DOC_HUL_Q1, "quarter", "ProfitLossForPeriod", REF_HUL_Q1_PDF, q1_page,
        "2,680 (Profit for the period C = A+B; discontinued operations nil)", COMPARISON_MATCHED)
    add(_DOC_HUL_Q1, "quarter", "ProfitLossForPeriodFromContinuingOperations", REF_HUL_Q1_PDF,
        q1_page, "2,680 (A: profit from continuing operations)", COMPARISON_MATCHED)
    add(_DOC_HUL_Q1, "quarter", "ProfitOrLossAttributableToOwnersOfParent", REF_HUL_Q1_PDF,
        q1_page, "2,673 (owners of the holding company; non-controlling interest 7)",
        COMPARISON_MATCHED)
    add(_DOC_HUL_Q1, "quarter", "ExceptionalItemsBeforeTax", REF_HUL_Q1_PDF, q1_page,
        "(75) exceptional items [net] charge/credit", COMPARISON_MATCHED)
    add(_DOC_HUL_Q1, "quarter", "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
        REF_HUL_Q1_PDF, q1_page, "11.38 (basic, continuing and discontinued operations)",
        COMPARISON_MATCHED)
    add(_DOC_HUL_Q1, "quarter", "BasicEarningsLossPerShareFromContinuingOperations",
        REF_HUL_Q1_PDF, q1_page, "11.38 (basic, continuing operations)", COMPARISON_MATCHED)
    add(_DOC_HUL_Q1, "quarter", "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
        REF_HUL_Q1_PDF, q1_page, "11.38 (diluted, continuing and discontinued operations)",
        COMPARISON_MATCHED)
    add(_DOC_HUL_Q1, "quarter", "DilutedEarningsLossPerShareFromContinuingOperations",
        REF_HUL_Q1_PDF, q1_page, "11.38 (diluted, continuing operations)", COMPARISON_MATCHED)
    add(_DOC_HUL_Q1, "quarter", "RevenueFromOperations", REF_HUL_Q1_PDF, q1_page,
        "17,341 (= 17,149 + 35 + 157)", COMPARISON_MATCHED, _HUL_COMPONENT_SUM_NOTE)

    # --- HUL Q4 FY2025-26 (quarter ended 31.03.2026): PDF p.8 -------------
    q4_page = "PDF p.8 (consolidated P&L, quarter ended 31.03.2026 column)"
    add(_DOC_HUL_Q4, "quarter", "RevenueFromOperations", REF_HUL_Q4_PDF, q4_page,
        "16,351 (= 16,172 + 35 + 144)", COMPARISON_MATCHED, _HUL_COMPONENT_SUM_NOTE)
    add(_DOC_HUL_Q4, "quarter", "Income", REF_HUL_Q4_PDF, q4_page,
        "16,615 (Total income)", COMPARISON_MATCHED)
    add(_DOC_HUL_Q4, "quarter", "ProfitBeforeExceptionalItemsAndTax", REF_HUL_Q4_PDF, q4_page,
        "3,681 (before exceptional items and tax and before share of equity-accounted investee)",
        COMPARISON_MATCHED)
    add(_DOC_HUL_Q4, "quarter", "ProfitBeforeTax", REF_HUL_Q4_PDF, q4_page,
        "3,928 fact vs printed 'Profit before tax' 3,924", COMPARISON_SCOPE_BASIS,
        _HUL_PBT_PLACEMENT_NOTE + " (share for the quarter: (4))")
    add(_DOC_HUL_Q4, "quarter", "ProfitLossForPeriod", REF_HUL_Q4_PDF, q4_page,
        "2,994 (Profit for the period C = A+B)", COMPARISON_MATCHED)
    add(_DOC_HUL_Q4, "quarter", "ProfitLossForPeriodFromContinuingOperations", REF_HUL_Q4_PDF,
        q4_page, "3,002 (A: profit from continuing operations)", COMPARISON_MATCHED)
    add(_DOC_HUL_Q4, "quarter", "ProfitOrLossAttributableToOwnersOfParent", REF_HUL_Q4_PDF,
        q4_page, "2,992 (owners of the holding company; non-controlling interest 2)",
        COMPARISON_MATCHED)
    add(_DOC_HUL_Q4, "quarter", "ExceptionalItemsBeforeTax", REF_HUL_Q4_PDF, q4_page,
        "247 exceptional items [net] gain/credit (demerger-related)", COMPARISON_MATCHED)
    add(_DOC_HUL_Q4, "quarter", "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
        REF_HUL_Q4_PDF, q4_page, "12.73 (basic, continuing and discontinued operations)",
        COMPARISON_MATCHED)
    add(_DOC_HUL_Q4, "quarter", "BasicEarningsLossPerShareFromContinuingOperations",
        REF_HUL_Q4_PDF, q4_page, "12.76 (basic, continuing operations)", COMPARISON_MATCHED)
    add(_DOC_HUL_Q4, "quarter", "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
        REF_HUL_Q4_PDF, q4_page, "12.72 (diluted, continuing and discontinued operations)",
        COMPARISON_MATCHED)
    add(_DOC_HUL_Q4, "quarter", "DilutedEarningsLossPerShareFromContinuingOperations",
        REF_HUL_Q4_PDF, q4_page, "12.76 (diluted, continuing operations)", COMPARISON_MATCHED)

    # --- HUL FY2025-26 (year ended 31.03.2026): PDF p.8 -------------------
    fy_page = "PDF p.8 (consolidated P&L, year ended 31.03.2026 column)"
    add(_DOC_HUL_Q4, "annual", "RevenueFromOperations", REF_HUL_Q4_PDF, fy_page,
        "64,468 (= 63,636 + 127 + 705)", COMPARISON_MATCHED, _HUL_COMPONENT_SUM_NOTE)
    add(_DOC_HUL_Q4, "annual", "Income", REF_HUL_Q4_PDF, fy_page,
        "65,219 (Total income)", COMPARISON_MATCHED)
    add(_DOC_HUL_Q4, "annual", "ProfitBeforeExceptionalItemsAndTax", REF_HUL_Q4_PDF, fy_page,
        "14,047 (after share of equity-accounted investee (15))", COMPARISON_MATCHED)
    add(_DOC_HUL_Q4, "annual", "ProfitLossForPeriod", REF_HUL_Q4_PDF, fy_page,
        "15,059 (C = A+B: 10,652 continuing + 4,407 discontinued)", COMPARISON_MATCHED)
    add(_DOC_HUL_Q4, "annual", "ProfitOrLossAttributableToOwnersOfParent", REF_HUL_Q4_PDF,
        fy_page, "15,040 (owners of the holding company; non-controlling interest 19)",
        COMPARISON_MATCHED)
    add(_DOC_HUL_Q4, "annual", "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
        REF_HUL_Q4_PDF, fy_page, "64.01 (basic, continuing and discontinued operations)",
        COMPARISON_MATCHED)
    add(_DOC_HUL_Q4, "annual", "BasicEarningsLossPerShareFromContinuingOperations",
        REF_HUL_Q4_PDF, fy_page, "45.25 (basic, continuing operations)", COMPARISON_MATCHED)
    add(_DOC_HUL_Q4, "annual", "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
        REF_HUL_Q4_PDF, fy_page, "64.00 (diluted, continuing and discontinued operations)",
        COMPARISON_MATCHED)
    add(_DOC_HUL_Q4, "annual", "DilutedEarningsLossPerShareFromContinuingOperations",
        REF_HUL_Q4_PDF, fy_page, "45.25 (diluted, continuing operations)", COMPARISON_MATCHED)
    return overrides


_RENDERED_OVERRIDES.update(_build_visual_inspection_overrides())

#: Document provenance for rendered references (verified against the cached
#: PDFs; hashes recorded in tests/fixtures/india/manifest.json).
RENDERED_DOCUMENT_STORAGE: dict[str, str] = {
    REF_INFY_REG33_Q1: "infosys/q1_fy2026-27/q1-fy27-financial-results-auditorsreports.pdf",
    REF_INFY_CONSO_Q1: "infosys/q1_fy2026-27/consol-fy27-q1-finstatement.pdf",
    REF_INFY_CONSO_Q4: "infosys/q4_fy2025-26/consol-fy26-q4-and-12m-finstatement.pdf",
    REF_HUL_Q4_PDF: "hindustan_unilever/q4_fy2025-26/hul-mq26-financial-results.pdf",
    REF_HUL_Q1_PDF: "hindustan_unilever/q1_fy2026-27/hul-jq26-financial-results.pdf",
    REF_TCS_CONSO_Q1: "tcs/q1_fy2026-27/tcs-q1fy27-consolidated-indas-finstatement.pdf",
    REF_HCLTECH_REG33_Q1: (
        "hcltech/q1_fy2026-27/hcltech-q1fy27-unaudited-financial-results-qe-june-30-2026.pdf"
    ),
    REF_ITC_CFS_Q1: "itc/q1_fy2026-27/itc-q1fy27-financial-result-cfs.pdf",
    REF_ASIANPAINT_RESULTS_Q1: "asianpaints/q1_fy2026-27/asianpaints-q1fy27-results.pdf",
    REF_MARUTI_RESULTS_Q1: "maruti_suzuki/q1_fy2026-27/maruti-q1fy27-unaudited-financial-results.pdf",
    REF_ULTRACEMCO_RESULTS_Q1: "ultratech_cement/q1_fy2026-27/ultratech-q1fy27-results.pdf",
    REF_SUNPHARMA_RESULTS_Q1: (
        "sun_pharmaceutical/q1_fy2026-27/sunpharma-q1fy27-financial-results.pdf"
    ),
    REF_LT_RESULTS_Q1: "larsen_toubro/q1_fy2026-27/lt-q1fy27-financial-results.pdf",
}


@dataclass(frozen=True)
class ValidationRow:
    """One row of data/validation/india_reconciliation.csv (IND-3 contract)."""

    issuer_id: str
    source_document_id: str
    source_url: str
    document_hash: str
    filing_identifier: str
    filed_at: str
    period_start: str
    period_end: str
    fiscal_label: str
    reporting_scope: str
    concept: str
    original_tag_or_label: str
    context_id: str
    raw_value: str
    raw_unit: str
    source_precision: str
    presentation_scale: str
    normalized_value: str
    normalized_unit: str
    reference_document_id: str = ""
    reference_page_or_location: str = ""
    reference_display_value: str = ""
    comparison_status: str = COMPARISON_UNAVAILABLE
    review_status: str = REVIEW_HUMAN_PENDING
    review_notes: str = ""

    def csv_row(self) -> dict[str, str]:
        return {column: getattr(self, column) for column in _CSV_COLUMNS}


def _documents_from_manifest() -> dict[str, DocumentProvenance]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    documents: dict[str, DocumentProvenance] = {}
    for entry in manifest["committed_fixtures"]:
        filing = entry.get("exchange_filing") or {}
        documents[entry["file"]] = DocumentProvenance(
            document_id=entry["file"],
            issuer_id=entry["issuer_id"],
            file_name=entry["file"],
            storage_path=entry.get("storage_copy"),
            source_url=entry.get("source_url") or "",
            sha256=entry.get("sha256") or "",
            seq_id=filing.get("seq_id"),
            broadcast=filing.get("broadcast_ist"),
            revision=filing.get("revision"),
            audited_status=filing.get("audited_status"),
        )
    for entry in manifest.get("storage_cache_only", []):
        file_name = Path(entry["path"]).name
        documents.setdefault(
            file_name,
            DocumentProvenance(
                document_id=file_name,
                issuer_id=entry.get("issuer_id") or "",
                file_name=file_name,
                storage_path=entry["path"],
                source_url=entry.get("source_url") or "",
                sha256=entry.get("sha256") or "",
                seq_id=(entry.get("exchange_filing") or {}).get("seq_id"),
                broadcast=(entry.get("exchange_filing") or {}).get("broadcast_ist"),
                revision=(entry.get("exchange_filing") or {}).get("revision"),
                audited_status=(entry.get("exchange_filing") or {}).get("audited_status"),
            ),
        )
    return documents


def _application_fiscal_label(period_start: date | None, end: date, kind: str) -> str:
    """The APPLICATION's presentation label (derived from dates only).

    This is Quarterline's own presentation layer — the issuer's own labels
    ("June 2026 quarter" style / ``ReportingQuarter="First quarter"``) differ,
    and the calendar dates in period_start/period_end remain the primary truth
    (docs/india_financial_methodology.md §3).
    """
    return period_label(period_start, end, kind)


def _validation_rows_for_instance(
    provenance: DocumentProvenance,
    instance,
    meta: FilingMeta | None,
) -> list[ValidationRow]:
    scope = (meta.scope if meta else None) or instance.declared_scope or "consolidated"
    filed_at = (meta.published_at.isoformat() if meta and meta.published_at else None) or (
        (provenance.broadcast or "").split(" ")[0] or ""
    )
    rows: list[ValidationRow] = []
    for fact in instance.undimensioned_numeric_facts():
        mapping = map_tag(fact.tag)
        if not mapping.mapped:
            continue
        period_end_text = fact.context.instant or fact.context.period_end
        if period_end_text is None:
            continue
        kind = fact.context.period_kind
        if kind == "instant":
            continue  # mapped concepts are flows/per-share; instants are not reconciled
        start = date.fromisoformat(fact.context.period_start) if fact.context.period_start else None
        end = date.fromisoformat(period_end_text)
        value = fact.value_decimal
        if value is None:
            continue
        if fact.is_per_share_unit:
            raw_unit, normalized_unit, presentation_scale = (
                "INR (per-share fact)",
                "INR/share",
                "Units (per share; not scaled)",
            )
        else:
            raw_unit, normalized_unit = "INR", "INR"
            presentation_scale = f"LevelOfRounding={instance.rounding_trait} (presentation only)"
        key: ReferenceKey = (provenance.issuer_id, provenance.document_id, kind, fact.tag)
        reference = (
            RENDERED_REFERENCES.get(key)
            or _INDUCE6_RENDERED.get(key)
            or _RENDERED_OVERRIDES.get(key)
        )
        if reference is None:
            reference = _SCRAMBLED if key in _SCRAMBLED_KEYS else _UNAVAILABLE
        review_status = (
            REVIEW_AGENT_CHECKED
            if reference.comparison_status
            in (COMPARISON_MATCHED, COMPARISON_WITHIN_PRECISION, COMPARISON_SCOPE_BASIS)
            else REVIEW_HUMAN_PENDING
        )
        rows.append(
            ValidationRow(
                issuer_id=provenance.issuer_id,
                source_document_id=provenance.document_id,
                source_url=provenance.source_url,
                document_hash=provenance.sha256,
                filing_identifier=f"NSE seq {provenance.seq_id}" if provenance.seq_id else "",
                filed_at=filed_at,
                period_start=start.isoformat() if start else "",
                period_end=end.isoformat(),
                fiscal_label=_application_fiscal_label(start, end, kind),
                reporting_scope=scope,
                concept=mapping.concept or "",
                original_tag_or_label=fact.tag,
                context_id=fact.context_ref,
                raw_value=fact.value_text,
                raw_unit=raw_unit,
                source_precision=f"decimals={fact.decimals}" if fact.decimals else "",
                presentation_scale=presentation_scale,
                normalized_value=str(value),
                normalized_unit=normalized_unit,
                reference_document_id=reference.reference_document_id,
                reference_page_or_location=reference.page_or_location,
                reference_display_value=reference.display_value,
                comparison_status=reference.comparison_status,
                review_status=review_status,
                review_notes=reference.notes,
            )
        )
    rows.sort(key=lambda r: (r.period_end, r.original_tag_or_label))
    return rows


#: Agent-verified PRIOR-YEAR QUARTER comparatives (IND-6) extracted from the
#: issuers' own Q1 FY27 rendered statements — the exchange Q1 instances carry NO
#: prior-year duration contexts (verified for all 10 issuers), so this rendered
#: comparative column is the only clean route to a prior-year quarter.
#: Extraction is VALUE-ANCHORED (pdf_results.extract_prior_year_comparatives):
#: the current-quarter / preceding-quarter / annual values of the same row are
#: known exactly from the committed XBRL instances, so a matching page token
#: window identifies the row AND verifies the declared display scale; the
#: remaining column is the prior-year quarter (2025-04-01..2025-06-30, Q1 FY26).
#: Three issuers were excluded because their Q1 statements do NOT extract
#: deterministically: HUL (column interleaving, IND-3 §7), MARUTI (scan with a
#: garbled OCR text layer), ULTRACEMCO (vector artifacts) — their YoY stays a
#: typed missing status, never guessed. When the cached PDF is present, the
#: ingestion re-extracts live and REFUSES on any drift from these records.
REVIEWED_PDF_COMPARATIVES: tuple[dict[str, object], ...] = (
    {
        "issuer_id": "IN-INFY",
        "reference_document_id": REF_INFY_CONSO_Q1,
        "source_document": "consol-fy27-q1-finstatement.pdf",
        "page": "PDF p.3 (Condensed Consolidated Statement of Profit and Loss)",
        "page_number": 3,
        "declared_scale_label": "'(In ₹ crore, except equity share and per equity share data)' page header",
        "scale_multiplier": "10000000",
        "filed_at": "2026-07-23",
        "period_start": "2025-04-01",
        "period_end": "2025-06-30",
        "source_xbrl_document": "INFY-Q1FY27-consolidated-nse-integrated-filing-xbrl.xml",
        "column_order": "current, prior_year (condensed two-column; row-major label adjacency)",
        "anchor_preceding_quarter": None,
        "anchor_annual": None,
        "concepts": {
            "revenue_from_operations": {
                "tag": "RevenueFromOperations",
                "prior_display": "42,279",
                "prior_value": "422790000000",
                "anchor_current": "48211",
            },
            "profit_after_tax": {
                "tag": "ProfitLossForPeriod",
                "prior_display": "6,924",
                "prior_value": "69240000000",
                "anchor_current": "7775",
            },
            "eps_basic": {
                "tag": "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                "prior_display": "16.70",
                "prior_value": "16.70",
                "anchor_current": "19.19",
            },
            "eps_diluted": {
                "tag": "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                "prior_display": "16.68",
                "prior_value": "16.68",
                "anchor_current": "19.17",
            },
        },
        "note": (
            "Prior-year quarter (three months ended June 30, 2025) comparatives from the same "
            "deterministic condensed statement IND-3 verified; current-column values equal the "
            "XBRL facts exactly (scale verified by the anchor match)."
        ),
    },
    {
        "issuer_id": "IN-TCS",
        "reference_document_id": REF_TCS_CONSO_Q1,
        "source_document": "tcs-q1fy27-consolidated-indas-finstatement.pdf",
        "page": "PDF p.2 (Condensed Consolidated Statement of Profit and Loss)",
        "page_number": 2,
        "declared_scale_label": "'(₹ crore)' page header",
        "scale_multiplier": "10000000",
        "filed_at": "2026-07-09",
        "period_start": "2025-04-01",
        "period_end": "2025-06-30",
        "source_xbrl_document": "TCS-Q1FY27-consolidated-nse-integrated-filing-xbrl.xml",
        "column_order": "current, prior_year (condensed two-column; row-major label adjacency)",
        "anchor_preceding_quarter": None,
        "anchor_annual": None,
        "concepts": {
            "revenue_from_operations": {
                "tag": "RevenueFromOperations",
                "prior_display": "63,437",
                "prior_value": "634370000000",
                "anchor_current": "72275",
            },
            "profit_after_tax": {
                "tag": "ProfitLossForPeriod",
                "prior_display": "12,819",
                "prior_value": "128190000000",
                "anchor_current": "13420",
            },
            "eps_basic": {
                "tag": "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                "prior_display": "35.27",
                "prior_value": "35.27",
                "anchor_current": "36.90",
            },
            "eps_diluted": {
                "tag": "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                "prior_display": "35.27",
                "prior_value": "35.27",
                "anchor_current": "36.90",
            },
        },
        "note": (
            "TCS prints one 'Basic and diluted (₹)' EPS row (identical values, 36.90 / 35.27); "
            "both concepts are carried from the same verified row."
        ),
    },
    {
        "issuer_id": "IN-HCLTECH",
        "reference_document_id": REF_HCLTECH_REG33_Q1,
        "source_document": "hcltech-q1fy27-unaudited-financial-results-qe-june-30-2026.pdf",
        "page": "PDF p.2 (Consolidated Reg-33 statement)",
        "page_number": 2,
        "declared_scale_label": "Reg-33 statement, ₹ in crores",
        "scale_multiplier": "10000000",
        "filed_at": "2026-07-13",
        "period_start": "2025-04-01",
        "period_end": "2025-06-30",
        "source_xbrl_document": "HCLTECH-Q1FY27-consolidated-nse-integrated-filing-xbrl.xml",
        "column_order": "current, preceding_quarter, prior_year (three-column quarter block)",
        "anchor_preceding_quarter": True,
        "anchor_annual": None,
        "concepts": {
            "revenue_from_operations": {
                "tag": "RevenueFromOperations",
                "prior_display": "30,349",
                "prior_value": "303490000000",
                "anchor_current": "34579",
                "anchor_preceding": "33981",
            },
            "profit_after_tax": {
                "tag": "ProfitLossForPeriod",
                "prior_display": "3,844",
                "prior_value": "38440000000",
                "anchor_current": "4626",
                "anchor_preceding": "4490",
            },
            "eps_basic": {
                "tag": "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                "prior_display": "14.18",
                "prior_value": "14.18",
                "anchor_current": "17.09",
                "anchor_preceding": "16.59",
            },
            "eps_diluted": {
                "tag": "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                "prior_display": "14.17",
                "prior_value": "14.17",
                "anchor_current": "17.06",
                "anchor_preceding": "16.56",
            },
        },
        "note": (
            "HCLTech's Reg-33 prints three quarter columns (30-June-2026 / 31-March-2026 / "
            "30-June-2025); both known columns of each row match the XBRL facts before the "
            "prior-year value is read."
        ),
    },
    {
        "issuer_id": "IN-ITC",
        "reference_document_id": REF_ITC_CFS_Q1,
        "source_document": "itc-q1fy27-financial-result-cfs.pdf",
        "page": "PDF p.1 (consolidated results statement)",
        "page_number": 1,
        "declared_scale_label": "'(₹ in Crores)' page header",
        "scale_multiplier": "10000000",
        "filed_at": "2026-07-31",
        "period_start": "2025-04-01",
        "period_end": "2025-06-30",
        "source_xbrl_document": "ITC-Q1FY27-consolidated-nse-integrated-filing-xbrl.xml",
        "column_order": "current, prior_year, preceding_quarter, annual (four columns)",
        "anchor_preceding_quarter": True,
        "anchor_annual": True,
        "concepts": {
            "revenue_from_operations": {
                "tag": "RevenueFromOperations",
                "prior_display": "23,129.35",
                "prior_value": "231293500000",
                "anchor_current": "29523.30",
                "anchor_preceding": "23821.48",
                "anchor_annual": "89913.33",
            },
            "profit_after_tax": {
                "tag": "ProfitLossForPeriod",
                "prior_display": "5,343.41",
                "prior_value": "53434100000",
                "anchor_current": "4508.79",
                "anchor_preceding": "5469.74",
                "anchor_annual": "21018.15",
            },
            "eps_basic": {
                "tag": "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                "prior_display": "4.19",
                "prior_value": "4.19",
                "anchor_current": "3.51",
                "anchor_preceding": "4.30",
                "anchor_annual": "16.52",
            },
            "eps_diluted": {
                "tag": "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                "prior_display": "4.18",
                "prior_value": "4.18",
                "anchor_current": "3.51",
                "anchor_preceding": "4.30",
                "anchor_annual": "16.51",
            },
        },
        "note": (
            "Three of the four columns of every anchored row equal the XBRL facts (Q1 FY27, "
            "Q4 FY26 quarter, FY26 annual) before the prior-year quarter value is read."
        ),
    },
    {
        "issuer_id": "IN-ASIANPAINT",
        "reference_document_id": REF_ASIANPAINT_RESULTS_Q1,
        "source_document": "asianpaints-q1fy27-results.pdf",
        "page": "PDF p.10 (consolidated Reg-33 statement)",
        "page_number": 10,
        "declared_scale_label": "'(₹ in Crores)' page footer",
        "scale_multiplier": "10000000",
        "filed_at": "2026-07-29",
        "period_start": "2025-04-01",
        "period_end": "2025-06-30",
        "source_xbrl_document": "ASIANPAINT-Q1FY27-consolidated-nse-integrated-filing-xbrl.xml",
        "column_order": "current, preceding_quarter, prior_year (three-column quarter block)",
        "anchor_preceding_quarter": True,
        "anchor_annual": None,
        "concepts": {
            "revenue_from_operations": {
                "tag": "RevenueFromOperations",
                "prior_display": "8,938.55",
                "prior_value": "89385500000",
                "anchor_current": "10541.94",
                "anchor_preceding": "9246.70",
            },
            "profit_after_tax": {
                "tag": "ProfitLossForPeriod",
                "prior_display": "1,117.05",
                "prior_value": "11170500000",
                "anchor_current": "1559.45",
                "anchor_preceding": "1185.49",
            },
            "eps_basic": {
                "tag": "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                "prior_display": "11.47",
                "prior_value": "11.47",
                "anchor_current": "16.06",
                "anchor_preceding": "12.23",
            },
            "eps_diluted": {
                "tag": "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                "prior_display": "11.47",
                "prior_value": "11.47",
                "anchor_current": "16.05",
                "anchor_preceding": "12.22",
            },
        },
        "note": (
            "The rendered page's header labels extract out of visual order, so identification "
            "is purely value-anchored: the internal identity 'revenue from sales + other "
            "operating revenue = revenue from operations' holds in all three columns and two "
            "known columns per row equal the XBRL facts. Page-10 linear text only."
        ),
    },
    {
        "issuer_id": "IN-SUNPHARMA",
        "reference_document_id": REF_SUNPHARMA_RESULTS_Q1,
        "source_document": "sunpharma-q1fy27-financial-results.pdf",
        "page": "PDF p.1 (consolidated results statement)",
        "page_number": 1,
        "declared_scale_label": "declared display unit ₹ MILLION (instance LevelOfRounding=Millions)",
        "scale_multiplier": "1000000",
        "filed_at": "2026-07-31",
        "period_start": "2025-04-01",
        "period_end": "2025-06-30",
        "source_xbrl_document": "SUNPHARMA-Q1FY27-consolidated-nse-integrated-filing-xbrl.xml",
        "column_order": "current, preceding_quarter, prior_year, annual (four columns)",
        "anchor_preceding_quarter": True,
        "anchor_annual": True,
        "concepts": {
            "revenue_from_operations": {
                "tag": "RevenueFromOperations",
                "prior_display": "138,514.0",
                "prior_value": "138514000000",
                "anchor_current": "152998.8",
                "anchor_preceding": "146117.9",
                "anchor_annual": "584620.4",
            },
            "profit_after_tax": {
                "tag": "ProfitLossForPeriod",
                "prior_display": "22,928.7",
                "prior_value": "22928700000",
                "anchor_current": "29012.3",
                "anchor_preceding": "27096.6",
                "anchor_annual": "115086.5",
            },
            "eps_basic": {
                "tag": "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                "prior_display": "9.5",
                "prior_value": "9.5",
                "anchor_current": "12.1",
                "anchor_preceding": "11.3",
                "anchor_annual": "47.8",
            },
            "eps_diluted": {
                "tag": "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                "prior_display": "9.5",
                "prior_value": "9.5",
                "anchor_current": "12.1",
                "anchor_preceding": "11.3",
                "anchor_annual": "47.8",
            },
        },
        "note": (
            "Sun Pharma displays in ₹ MILLION (revenue 152,998.8 Mn = ₹15,299.88 Cr); the "
            "stored prior-year values are full rupees as everywhere else."
        ),
    },
    {
        "issuer_id": "IN-LT",
        "reference_document_id": REF_LT_RESULTS_Q1,
        "source_document": "lt-q1fy27-financial-results.pdf",
        "page": "PDF p.1 (consolidated results statement)",
        "page_number": 1,
        "declared_scale_label": "'₹ Crore' statement header",
        "scale_multiplier": "10000000",
        "filed_at": "2026-07-28",
        "period_start": "2025-04-01",
        "period_end": "2025-06-30",
        "source_xbrl_document": "LT-Q1FY27-consolidated-nse-integrated-filing-xbrl.xml",
        "column_order": "current, preceding_quarter, prior_year, annual (four columns)",
        "anchor_preceding_quarter": True,
        "anchor_annual": True,
        "concepts": {
            "revenue_from_operations": {
                "tag": "RevenueFromOperations",
                "prior_display": "63,678.92",
                "prior_value": "636789200000",
                "anchor_current": "67941.74",
                "anchor_preceding": "82762.16",
                "anchor_annual": "285874.36",
            },
            "profit_after_tax": {
                "tag": "ProfitLossForPeriod",
                "prior_display": "4,318.17",
                "prior_value": "43181700000",
                "anchor_current": "4988.03",
                "anchor_preceding": "6133.06",
                "anchor_annual": "18953.88",
            },
            "eps_basic": {
                "tag": "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                "prior_display": "26.30",
                "prior_value": "26.30",
                "anchor_current": "29.97",
                "anchor_preceding": "38.71",
                "anchor_annual": "116.93",
            },
            "eps_diluted": {
                "tag": "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                "prior_display": "26.29",
                "prior_value": "26.29",
                "anchor_current": "29.96",
                "anchor_preceding": "38.70",
                "anchor_annual": "116.88",
            },
        },
        "note": (
            "The same anchored row repeats on p.4 (segment-total revenue) with the identical "
            "prior-year value — an internal consistency cross-check, recorded here from p.1."
        ),
    },
)

#: Agent-verified reported cash flow extracted from the cached company-IR PDFs
#: on 2026-09-11 (extraction: quarterline.sources.india.pdf_results.
#: extract_cash_flow_from_pdf; provenance = manifest sha256 of each document).
#: When the storage cache is present, a test re-extracts live and asserts these
#: recorded values still hold, so the constants cannot silently drift.
REVIEWED_PDF_CASH_FLOW: tuple[dict[str, str], ...] = (
    {
        "document": "consol-fy27-q1-finstatement.pdf",
        "reference_document_id": REF_INFY_CONSO_Q1,
        "page": "PDF p.6 (Condensed Consolidated Statement of Cash Flows)",
        "display": "9,330",
        "value": "93300000000",
        "period_start": "2026-04-01",
        "period_end": "2026-06-30",
        "duration": "quarter",
        "note": (
            "REPORTED quarterly CFO (IND-3 correction A: legitimate at its actual reported "
            "frequency). Text-level extraction with page provenance; period resolved from the "
            "document title 'for the three months ended June 30, 2026' (period_source="
            "document_title); '(In ₹ crore)' declared scale; first numeric column verified as the "
            "current period (year header 2026 before 2025)."
        ),
    },
    {
        "document": "consol-fy26-q4-and-12m-finstatement.pdf",
        "reference_document_id": REF_INFY_CONSO_Q4,
        "page": "PDF p.6 (Condensed Consolidated Statement of Cash Flows)",
        "display": "33,986",
        "value": "339860000000",
        "period_start": "2025-04-01",
        "period_end": "2026-03-31",
        "duration": "annual",
        "note": (
            "Rendered corroboration of the exchange XBRL annual CFO; same figure as "
            "CashFlowsFromUsedInOperatingActivities in INFY-Q4FY26 consolidated instance."
        ),
    },
)


def _validation_rows_from_fixtures() -> list[ValidationRow]:
    documents = _documents_from_manifest()
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    rows: list[ValidationRow] = []
    for entry in manifest["committed_fixtures"]:
        provenance = documents[entry["file"]]
        meta = FilingMeta(
            issuer_id=entry["issuer_id"],
            scope=entry["scope"],
            period_start=None,
            period_end=date.fromisoformat(str(manifest["periods"][entry["period"]]["period_end"])),
            published_at=date.fromisoformat((provenance.broadcast or "1970-01-01").split(" ")[0]),
            revision_status=provenance.revision,
            audited_status=provenance.audited_status,
            exchange="NSE",
            seq_id=provenance.seq_id,
            source_url=provenance.source_url,
        )
        instance = parse_instance((FIXTURES_DIR / entry["file"]).read_bytes(), filing_meta=meta)
        rows.extend(_validation_rows_for_instance(provenance, instance, meta))
    return rows


def write_validation_csv(
    path: Path | None = None,
    fixtures_dir: Path | None = None,
) -> Path:
    """Write ``data/validation/india_reconciliation.csv`` (IND-3 contract).

    Deterministic: built from the committed fixture parses plus the
    agent-verified rendered-document references and reviewed PDF cash-flow
    evidence recorded above — no storage cache or network needed.
    """
    del fixtures_dir  # the committed manifest pins the fixture set
    rows = _validation_rows_from_fixtures()
    # The reported INFY quarterly CFO row (PDF-sourced, IND-3 correction A).
    documents = _documents_from_manifest()
    quarterly = REVIEWED_PDF_CASH_FLOW[0]
    provenance = documents[quarterly["document"]]
    rows.append(
        ValidationRow(
            issuer_id="IN-INFY",
            source_document_id=quarterly["document"],
            source_url=provenance.source_url,
            document_hash=provenance.sha256,
            filing_identifier="company-ir (accompanies NSE seq 177385)",
            filed_at="2026-07-23",
            period_start=quarterly["period_start"],
            period_end=quarterly["period_end"],
            fiscal_label=_application_fiscal_label(
                date.fromisoformat(quarterly["period_start"]),
                date.fromisoformat(quarterly["period_end"]),
                "quarter",
            ),
            reporting_scope="consolidated",
            concept="cash_flow_operations",
            original_tag_or_label="Net cash generated by operating activities (rendered line)",
            context_id="PDF p.6 (text-level extraction)",
            raw_value=quarterly["display"],
            raw_unit="INR (displayed ₹ crore)",
            source_precision="declared scale 'In ₹ crore'; integer crores",
            presentation_scale="crore (page header; presentation only)",
            normalized_value=quarterly["value"],
            normalized_unit="INR",
            reference_document_id=quarterly["reference_document_id"],
            reference_page_or_location=quarterly["page"],
            reference_display_value=quarterly["display"],
            comparison_status=COMPARISON_MATCHED,
            review_status=REVIEW_AGENT_CHECKED,
            review_notes=quarterly["note"],
        )
    )
    # IND-6 prior-year quarter comparatives (PDF-sourced, value-anchored).
    for record in REVIEWED_PDF_COMPARATIVES:
        concepts: dict[str, dict[str, str]] = record["concepts"]  # type: ignore[assignment]
        document = documents.get(str(record["source_document"]))
        for concept, entry in sorted(concepts.items()):
            rows.append(
                ValidationRow(
                    issuer_id=str(record["issuer_id"]),
                    source_document_id=str(record["source_document"]),
                    source_url=document.source_url if document else "",
                    document_hash=document.sha256 if document else "",
                    filing_identifier=(
                        f"company-ir (accompanies NSE seq of {record['source_xbrl_document']})"
                    ),
                    filed_at=str(record["filed_at"]),
                    period_start=str(record["period_start"]),
                    period_end=str(record["period_end"]),
                    fiscal_label=_application_fiscal_label(
                        date.fromisoformat(str(record["period_start"])),
                        date.fromisoformat(str(record["period_end"])),
                        "quarter",
                    ),
                    reporting_scope="consolidated",
                    concept=concept,
                    original_tag_or_label=(
                        f"{entry['tag']} (rendered prior-year comparative column)"
                    ),
                    context_id=f"{record['page']} (value-anchored text extraction)",
                    raw_value=str(entry["prior_display"]),
                    raw_unit="INR (displayed declared scale)",
                    source_precision=(
                        f"declared display scale verified by anchor match; "
                        f"multiplier {record['scale_multiplier']} to full rupees"
                    ),
                    presentation_scale=str(record["declared_scale_label"]),
                    normalized_value=str(entry["prior_value"]),
                    normalized_unit="INR/share" if concept.startswith("eps_") else "INR",
                    reference_document_id=str(record["reference_document_id"]),
                    reference_page_or_location=str(record["page"]),
                    reference_display_value=(
                        f"current {entry['anchor_current']} (== XBRL) -> "
                        f"prior-year {entry['prior_display']}"
                    ),
                    comparison_status=COMPARISON_MATCHED,
                    review_status=REVIEW_AGENT_CHECKED,
                    review_notes=(f"{record['column_order']}; {record['note']}"),
                )
            )
    rows.sort(
        key=lambda r: (r.issuer_id, r.source_document_id, r.period_end, r.original_tag_or_label)
    )
    target = path or DEFAULT_VALIDATION_CSV_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(_CSV_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow(row.csv_row())
    return target


def write_supersession_note(path: Path | None = None) -> Path:
    """Replace the IND-2 ``data/india_reconciliation.csv`` with a supersession stub."""
    target = path or SUPERSEDED_CSV_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["superseded_by", "note"])
        note = (
            "Superseded 2026-09-11 (IND-3): the IND-2 9-column demonstration table could not "
            "carry document hashes, context ids, precision, rendered-document cross-references, "
            "or review states. The full reconciliation contract (25 columns, reviewer "
            "corrections A-C) now lives at data/validation/india_reconciliation.csv; see "
            "docs/india_reconciliation_review.md. Regenerate with "
            "quarterline.sources.india.reconcile.write_validation_csv()."
        )
        writer.writerow(["data/validation/india_reconciliation.csv", note])
    return target


# ---------------------------------------------------------------------------
# CLI handler (registry key "verify:india"; argparse subparser wiring is an
# orchestrator task — cli.py is not owned by this wave).
# ---------------------------------------------------------------------------


def _handle_verify_india(args: argparse.Namespace) -> int:
    try:
        print(reconciliation_table(args.issuer))
    except LookupError as exc:
        print(str(exc))
        return 1
    return 0


register_subcommand("verify:india", _handle_verify_india)
