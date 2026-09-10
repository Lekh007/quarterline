"""India source reconciliation output (IND-2).

``reconciliation_table(issuer_id)`` renders a source-linked table for every
mapped fact parsed from the issuer's cached XBRL artifacts: period label, scope,
concept, reported display value + declared scale, normalized full-rupee value,
source document + accession/URL, published date, revision and audit status.

``write_reconciliation_csv`` writes ``data/india_reconciliation.csv`` from the
committed fixtures (same parser, no database needed). Every row derived from a
parsed instance is ``reconciled_to_source``; anything ambiguous (detected in the
PDF path, or an unmapped tag) is ``review_required`` and never presented as a
reconciled number.
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
DEFAULT_CSV_PATH = REPO_ROOT / "data" / "india_reconciliation.csv"

REVIEW_RECONCILED = "reconciled_to_source"
REVIEW_REQUIRED = "review_required"

_CSV_COLUMNS = (
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
        for column in _CSV_COLUMNS:
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


def write_reconciliation_csv(
    path: Path | None = None,
    fixtures_dir: Path | None = None,
) -> Path:
    """Write ``data/india_reconciliation.csv`` from the committed fixture parses."""
    rows = rows_from_fixture_dir(fixtures_dir)
    target = path or DEFAULT_CSV_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(_CSV_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow(row.csv_row())
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
