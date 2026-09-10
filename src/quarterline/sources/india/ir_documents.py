"""Manual document import path for India sources (IND-2).

Every India financial source rejects non-browser HTTP clients (Akamai 403 on
Infosys/HUL/BSE, silent TCP drop on NSE — docs/india_source_audit.md §2), so this
milestone's ingestion is a **manual-import** path: a browser-acquired file is
verified, hashed, content-addressed into the raw cache, registered as a
``source_artifacts`` row, the issuer is upserted as a company (country ``IN``,
currency ``INR``), and XBRL instances are parsed immediately.

The filing metadata (scope, published date, revision/audit status, seq id) comes
from the caller / exchange listing, is written to a ``.filing.json`` sidecar next
to the cached file, and is carried — never inferred — through parsing and
ingestion (revisions.py).
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import select

from quarterline.cli import register_subcommand
from quarterline.ingest.cache import cache_file
from quarterline.sources.india.issuers import Issuer, require_verified
from quarterline.sources.india.periods import (
    KIND_ANNUAL,
    KIND_QUARTER,
    classify_period,
    fiscal_quarter,
    fiscal_year,
    fiscal_year_label,
)
from quarterline.sources.india.revisions import FilingMeta
from quarterline.sources.india.xbrl_parse import IndiaInstance, parse_instance
from quarterline.store.db import session_scope
from quarterline.store.models import SourceArtifact
from quarterline.store.repositories.companies import CompaniesRepo

#: source_artifacts.source value for India documents.
SOURCE_INDIA = "india"

#: Version stamped on India XBRL parses.
INDIA_XBRL_PARSER_VERSION = "india-xbrl-1"

#: Accepted content types by file extension (format detection is extension-based
#: and verified by actually parsing XBRL; nothing else is attempted).
_FORMATS: dict[str, str] = {
    ".xml": "xbrl",
    ".pdf": "pdf",
    ".xlsx": "xlsx",
    ".xls": "xls",
    ".html": "ixbrl_html",
    ".htm": "ixbrl_html",
}


@dataclass(frozen=True)
class ImportReport:
    """Typed result of one manual document import."""

    issuer_id: str
    issuer_slug: str
    doc_type: str
    scope: str
    period_start: date | None
    period_end: date
    published_at: date
    format: str
    local_path: str
    content_hash: str
    size_bytes: int
    artifact_id: int
    company_id: int
    company_ticker: str
    parsed: bool
    fact_count: int
    taxonomy_version: str | None
    source_url: str | None
    notes: str | None


def _quarter_dir_label(period_start: date | None, period_end: date) -> str:
    """IND-1-compatible cache subdirectory label (q1_fy2026-27 / q4_fy2025-26)."""
    fy = fiscal_year_label(fiscal_year(period_end))  # "FY2025-26"
    short = fy.lower()  # "fy2025-26"
    quarter = fiscal_quarter(period_end)
    kind = classify_period(period_start, period_end)
    if kind == KIND_QUARTER and quarter is not None:
        return f"{quarter.lower()}_{short}"  # q1_fy2026-27
    if kind == KIND_ANNUAL:
        # Q4 and FY are filed together; the IND-1 layout calls the directory q4_*.
        return f"q4_{short}"
    return period_end.isoformat().replace("-", "")


def _parse_date(value: str | date) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def _detect_format(path: Path) -> str:
    fmt = _FORMATS.get(path.suffix.lower())
    if fmt is None:
        raise ValueError(f"unsupported India document format {path.suffix!r} for {path}")
    return fmt


def filing_sidecar_path(cached_path: Path) -> Path:
    """``.filing.json`` sidecar next to a cached document body."""
    return cached_path.with_name(cached_path.name + ".filing.json")


def write_filing_sidecar(cached_path: Path, meta: FilingMeta) -> Path:
    """Persist the exchange-listing metadata next to the cached file (provenance)."""
    sidecar = filing_sidecar_path(cached_path)
    payload = {
        "issuer_id": meta.issuer_id,
        "scope": meta.scope,
        "period_start": meta.period_start.isoformat() if meta.period_start else None,
        "period_end": meta.period_end.isoformat(),
        "published_at": meta.published_at.isoformat(),
        "revision_status": meta.revision_status,
        "audited_status": meta.audited_status,
        "doc_type": meta.doc_type,
        "exchange": meta.exchange,
        "seq_id": meta.seq_id,
        "source_url": meta.source_url,
        "notes": meta.notes,
    }
    sidecar.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return sidecar


def read_filing_sidecar(cached_path: Path) -> FilingMeta | None:
    """Load the sidecar written at import time; ``None`` when absent/corrupt."""
    sidecar = filing_sidecar_path(cached_path)
    if not sidecar.is_file():
        return None
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return FilingMeta(
        issuer_id=str(payload.get("issuer_id") or ""),
        scope=str(payload.get("scope") or "consolidated"),
        period_start=(
            date.fromisoformat(payload["period_start"]) if payload.get("period_start") else None
        ),
        period_end=date.fromisoformat(str(payload.get("period_end"))),
        published_at=date.fromisoformat(str(payload.get("published_at"))),
        revision_status=payload.get("revision_status"),
        audited_status=payload.get("audited_status"),
        doc_type=str(payload.get("doc_type") or "financial_results"),
        exchange=payload.get("exchange"),
        seq_id=payload.get("seq_id"),
        source_url=payload.get("source_url"),
        notes=payload.get("notes"),
    )


def import_document(
    issuer_id: str,
    path: str | Path,
    doc_type: str,
    period_start: str | date | None,
    period_end: str | date,
    scope: str,
    published_at: str | date,
    source_url: str | None = None,
    notes: str | None = None,
    *,
    exchange: str | None = None,
    seq_id: str | None = None,
    audited_status: str | None = None,
    revision_status: str | None = None,
) -> ImportReport:
    """Verify, cache, register and (for XBRL) parse one manually acquired document.

    - verifies the file exists and is non-empty (never import silent zero bytes);
    - computes sha256 and stores the content under
      ``storage/raw/india/<issuer-slug>/<period>/`` via ``ingest.cache.cache_file``;
    - writes a ``source_artifacts`` row (``source="india"``) and upserts the
      company (country ``IN``, ``reporting_currency`` ``INR``); the ``cik``
      column (max 10 chars, too short for a 12-char ISIN) carries the BSE scrip
      code — the same identifier the XBRL entity uses;
    - parses the file when it is an XBRL instance and reports the fact count.
    """
    issuer: Issuer = require_verified(issuer_id)
    src = Path(path)
    if not src.is_file():
        raise FileNotFoundError(f"India document not found: {src}")
    content = src.read_bytes()
    if not content:
        raise ValueError(f"refusing to import empty file: {src}")

    fmt = _detect_format(src)
    period_end_d = _parse_date(period_end)
    period_start_d = _parse_date(period_start) if period_start is not None else None
    published_d = _parse_date(published_at)
    scope_normalized = scope.strip().lower()
    if scope_normalized not in ("consolidated", "standalone"):
        raise ValueError(
            f"scope must be 'consolidated' or 'standalone', got {scope!r} "
            "(bundled PDFs must be split by section first)"
        )

    entry = cache_file(
        src,
        relative_location=f"india/{issuer.slug}/{_quarter_dir_label(period_start_d, period_end_d)}",
    )
    meta = FilingMeta(
        issuer_id=issuer.issuer_id,
        scope=scope_normalized,
        period_start=period_start_d,
        period_end=period_end_d,
        published_at=published_d,
        revision_status=revision_status,
        audited_status=audited_status,
        doc_type=doc_type,
        exchange=exchange,
        seq_id=seq_id,
        source_url=source_url,
        notes=notes,
    )
    write_filing_sidecar(entry.path, meta)

    parsed_instance: IndiaInstance | None = None
    if fmt == "xbrl":
        parsed_instance = parse_instance(entry.content, filing_meta=meta)
        declared = parsed_instance.declared_scope
        if declared is not None and declared != scope_normalized:
            raise ValueError(
                f"scope mismatch for {src.name}: caller said {scope_normalized!r}, "
                f"instance declares {declared!r} "
                "(NatureOfReportStandaloneConsolidated)"
            )

    with session_scope() as session:
        companies = CompaniesRepo(session)
        company = companies.upsert_company(
            ticker=issuer.ticker_nse or issuer.issuer_id,
            cik=issuer.bse_code or issuer.issuer_id,
            name=issuer.name,
            sector=issuer.sector,
            country="IN",
        )
        company.reporting_currency = "INR"
        companies.flush()
        # Content-addressed: re-importing the same official file must reuse
        # the existing artifact row rather than duplicate it.
        artifact = session.scalars(
            select(SourceArtifact).where(
                SourceArtifact.source == SOURCE_INDIA,
                SourceArtifact.content_hash == entry.content_hash,
            )
        ).first()
        if artifact is None:
            artifact = SourceArtifact(
                source=SOURCE_INDIA,
                source_url=source_url or f"manual-import://{src.name}",
                fetched_at=datetime.now(UTC),
                local_path=str(entry.path),
                content_hash=entry.content_hash,
                content_type=fmt,
                parser_version=INDIA_XBRL_PARSER_VERSION if fmt == "xbrl" else None,
            )
            session.add(artifact)
            session.flush()
        elif source_url and artifact.source_url != source_url:
            artifact.source_url = source_url
        artifact_id = artifact.id
        company_id = company.id
        ticker = company.ticker

    return ImportReport(
        issuer_id=issuer.issuer_id,
        issuer_slug=issuer.slug,
        doc_type=doc_type,
        scope=scope_normalized,
        period_start=period_start_d,
        period_end=period_end_d,
        published_at=published_d,
        format=fmt,
        local_path=str(entry.path),
        content_hash=entry.content_hash,
        size_bytes=len(content),
        artifact_id=artifact_id,
        company_id=company_id,
        company_ticker=ticker,
        parsed=parsed_instance is not None,
        fact_count=len(parsed_instance.facts) if parsed_instance else 0,
        taxonomy_version=parsed_instance.taxonomy_version if parsed_instance else None,
        source_url=source_url,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# CLI handler (registry key "ingest:india-document"; argparse subparser wiring
# is an orchestrator task — cli.py is not owned by this wave).
# ---------------------------------------------------------------------------


def _handle_import_india_document(args: argparse.Namespace) -> int:
    try:
        report = import_document(
            issuer_id=args.issuer,
            path=args.file,
            doc_type=args.type,
            period_start=args.period_start,
            period_end=args.period_end,
            scope=args.scope,
            published_at=args.published,
            source_url=args.source_url,
            notes=args.notes,
            exchange=args.exchange,
            seq_id=args.seq_id,
            audited_status=args.audited,
            revision_status=args.revision,
        )
    except (LookupError, ValueError, FileNotFoundError) as exc:
        print(str(exc))
        return 1
    print(
        f"imported {report.doc_type} for {report.issuer_id} ({report.scope}, "
        f"ended {report.period_end.isoformat()})"
    )
    print(f"  format={report.format} bytes={report.size_bytes} sha256={report.content_hash}")
    print(f"  cached: {report.local_path}")
    if report.parsed:
        print(f"  parsed XBRL: {report.fact_count} facts, taxonomy {report.taxonomy_version}")
    print(f"  artifact_id={report.artifact_id} company={report.company_ticker}")
    print(f"  next: quarterline verify india --issuer {report.issuer_id}")
    return 0


register_subcommand("ingest:india-document", _handle_import_india_document)
