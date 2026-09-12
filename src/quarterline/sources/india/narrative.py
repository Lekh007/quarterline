"""India narrative document ingestion (IND-7): cached company-IR narrative
PDFs -> ``documents`` + per-page ``sections`` rows, searchable with page-level
citations.

Narrative corpus = the manifests' ``storage_cache_only`` company-IR PDFs:
press releases, results presentations, earnings-call transcripts, and the
condensed/Reg-33 financial-statement PDFs. Exchange XBRL instances and iXBRL
renderings are NOT narrative (they belong to the IND-2/IND-6 fact path);
Excel workbooks are not text-extractable here and are reported, not guessed.

Document kinds (the India vocabulary; stored on ``documents.document_kind``):
``financial_results | results_notes | earnings_presentation | annual_report |
management_transcript | exchange_announcement``. Manifest ``doc_type`` maps:

- ``financial_results`` (pdf)      -> ``financial_results``
- ``results_presentation``         -> ``earnings_presentation``
- ``press_release`` + transcript   -> ``management_transcript``
- ``press_release`` (other)        -> ``results_notes`` (management's own
  results commentary; the precise origin stays in the row metadata
  ``source_doc_type``)

Management-commentary labeling (docs/india_financial_methodology.md):
presentations, transcripts and press releases are MANAGEMENT STATEMENTS — a
distinct, labeled document type, never independently verified financial
explanation. The label travels ON the document row (``extraction_notes`` JSON:
``management_commentary`` flag + kind-based :func:`is_management_commentary`)
so every later rendered view can say so. Condensed financial-statement PDFs
are company-prepared statements, not commentary: flagged ``false`` and always
``source_tier="company_ir"`` (the tier that blocks source-tier laundering,
e.g. presenting an IR-PDF cash flow as if it came from the NSE filing).

Provenance reuses the IND-3 import path's discipline: manifest sha256 is
verified against the cached bytes BEFORE anything is ingested (drift is an
error, never silently ingested), the file dedupes into ``source_artifacts`` by
content hash, and document identity dedupes by (company, kind, content hash)
so re-runs are idempotent.

Extraction is per page via ``quarterline.ingest.pdf_extract`` — one section per
page with ``page_start``/``page_end`` set, because PAGE-LEVEL CITATIONS are the
India retrieval requirement (SPEC 27 / audit §7 tier 4). Scanned PDFs come back
``needs_ocr`` and get NO sections (empty text is never silently indexed);
near-empty or replacement-garbled pages keep low per-page confidence with a
recorded note instead of a fake clean label.

Also here (IND-6 open item closed): TCS's REPORTED Q1 FY27 quarterly CFO from
its IR condensed statement (p.6, Rs 12,171 Cr vs Rs 11,919 Cr year-ago quarter),
ingested as a ``pdf_text`` observation under EXACTLY the INFY provenance
standard of ``pipeline.ingest_reviewed_pdf_cash_flow`` — live drift guard
against the cached PDF, agent-checked review status, page provenance, distinct
rendered-line tag, idempotent observation hash.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from quarterline.core.normalization import observation_hash
from quarterline.ingest.pdf_extract import PDF_EXTRACT_VERSION, PdfExtraction, extract_pdf
from quarterline.sources.india.issuers import require_verified
from quarterline.sources.india.periods import fiscal_year, source_fp
from quarterline.sources.india.pipeline import FORM_PDF
from quarterline.sources.india.reconcile import (
    REF_TCS_CONSO_Q1,
    RENDERED_DOCUMENT_STORAGE,
    REVIEW_AGENT_CHECKED,
)
from quarterline.store.db import session_scope
from quarterline.store.models import FactObservation, SourceArtifact, decimal_to_text
from quarterline.store.repositories.companies import CompaniesRepo
from quarterline.store.repositories.documents import DocumentsRepo, SectionInput

#: Repository root when the package is used from its source checkout.
REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_MANIFEST_PATH = REPO_ROOT / "tests" / "fixtures" / "india" / "manifest.json"

#: Version stamped on India narrative document rows.
NARRATIVE_EXTRACTION_VERSION = "india-narrative-1"

#: ``source_artifacts.source`` for India narrative artifacts (same value as the
#: IND-2 import path so dedupe-by-hash spans both routes).
SOURCE_INDIA = "india"

#: The India document-kind vocabulary (documents.document_kind).
KIND_FINANCIAL_RESULTS = "financial_results"
KIND_RESULTS_NOTES = "results_notes"
KIND_EARNINGS_PRESENTATION = "earnings_presentation"
KIND_ANNUAL_REPORT = "annual_report"
KIND_MANAGEMENT_TRANSCRIPT = "management_transcript"
KIND_EXCHANGE_ANNOUNCEMENT = "exchange_announcement"

INDIA_DOCUMENT_KINDS = (
    KIND_FINANCIAL_RESULTS,
    KIND_RESULTS_NOTES,
    KIND_EARNINGS_PRESENTATION,
    KIND_ANNUAL_REPORT,
    KIND_MANAGEMENT_TRANSCRIPT,
    KIND_EXCHANGE_ANNOUNCEMENT,
)

#: Kinds that ARE management commentary (methodology: presentations,
#: transcripts and press releases are management statements, never
#: independently verified financial explanations).
COMMENTARY_KINDS = frozenset(
    {KIND_RESULTS_NOTES, KIND_EARNINGS_PRESENTATION, KIND_MANAGEMENT_TRANSCRIPT}
)

#: Manifest narrative doc types ingested here (pdf only).
_NARRATIVE_DOC_TYPES = frozenset({"press_release", "results_presentation", "financial_results"})

#: Page-quality honesty thresholds: below these the page keeps a low
#: confidence and an explicit note (never a clean default).
_MIN_PAGE_CHARS = 25
_REPLACEMENT_CHAR = "\ufffd"


def is_management_commentary(kind: str | None) -> bool:
    """Kind-based commentary label (the data IND-8's rendered views carry)."""
    return kind in COMMENTARY_KINDS


def narrative_metadata(document) -> dict:
    """Parse the structured metadata this module writes to
    ``documents.extraction_notes`` (empty dict when absent/corrupt)."""
    raw = getattr(document, "extraction_notes", None)
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _document_kind(entry: dict) -> str | None:
    """Map one manifest entry to the India document-kind vocabulary.

    ``None`` = not a narrative text document (xlsx workbooks, exchange XBRL /
    iXBRL renderings) — reported, never force-labeled.
    """
    doc_type = str(entry.get("doc_type") or "")
    fmt = str(entry.get("format") or "")
    if fmt != "pdf" or doc_type not in _NARRATIVE_DOC_TYPES:
        return None
    name = Path(str(entry.get("path") or "")).name.lower()
    if doc_type == "results_presentation":
        return KIND_EARNINGS_PRESENTATION
    if doc_type == "press_release":
        return KIND_MANAGEMENT_TRANSCRIPT if "transcript" in name else KIND_RESULTS_NOTES
    return KIND_FINANCIAL_RESULTS


def narrative_entries(manifest_path: str | Path | None = None) -> list[dict]:
    """The manifest's narrative storage-cache-only entries (pdf, IR tier).

    Sorted by (issuer_id, period, path) for deterministic ingestion order.
    """
    manifest = json.loads(Path(manifest_path or DEFAULT_MANIFEST_PATH).read_text(encoding="utf-8"))
    entries = []
    for entry in manifest.get("storage_cache_only", []):
        if entry.get("source_tier") != "company_ir":
            continue
        kind = _document_kind(entry)
        if kind is None:
            continue
        enriched = dict(entry)
        enriched["document_kind"] = kind
        entries.append(enriched)
    entries.sort(key=lambda e: (str(e.get("issuer_id")), str(e.get("period")), str(e.get("path"))))
    return entries


def _broadcast_lookup(manifest_path: str | Path | None = None) -> dict[tuple[str, str], date]:
    """(issuer_id, period) -> consolidated filing broadcast date, from the
    manifest's own exchange-filing metadata (provenance, never a guess)."""
    manifest = json.loads(Path(manifest_path or DEFAULT_MANIFEST_PATH).read_text(encoding="utf-8"))
    lookup: dict[tuple[str, str], date] = {}
    pool = list(manifest.get("committed_fixtures", [])) + list(
        manifest.get("storage_cache_only", [])
    )
    for entry in pool:
        filing = entry.get("exchange_filing") or {}
        stamp = filing.get("broadcast_ist") or filing.get("revised_ist")
        if not stamp:
            continue
        if str(entry.get("scope")) != "consolidated":
            continue
        key = (str(entry.get("issuer_id")), str(entry.get("period")))
        day = date.fromisoformat(str(stamp).split(" ")[0])
        if key not in lookup or day < lookup[key]:
            lookup[key] = day
    return lookup


def _period_bounds(manifest_path: str | Path | None, period_key: str) -> tuple[date | None, date]:
    manifest = json.loads(Path(manifest_path or DEFAULT_MANIFEST_PATH).read_text(encoding="utf-8"))
    info = manifest["periods"][period_key]
    start = info.get("period_start")
    return (
        date.fromisoformat(start) if start else None,
        date.fromisoformat(str(info["period_end"])),
    )


def _page_confidence(text: str) -> tuple[float, str | None]:
    """Honest per-page extraction confidence with a note when degraded.

    Financial-statement pages mix digits, symbols and whitespace heavily, so
    the heuristic only flags CLEAR degradation: near-empty pages (the scan
    case) and replacement-glyph pages (encoding corruption). Anything else is
    1.0 — the low-confidence bookkeeping must not cry wolf.
    """
    stripped = text.strip()
    if len(stripped) < _MIN_PAGE_CHARS:
        return 0.0, "no extractable text on this page (likely scanned/image page)"
    replacements = stripped.count(_REPLACEMENT_CHAR)
    if replacements > max(3, len(stripped) // 100):
        ratio = 1.0 - replacements / len(stripped)
        return round(max(0.0, ratio), 3), "replacement characters in extracted text (garbled page)"
    return 1.0, None


def _page_sections(kind: str, extraction: PdfExtraction) -> list[SectionInput]:
    """One section per page (page-level citations are the India requirement)."""
    sections: list[SectionInput] = []
    for page in extraction.pages:
        confidence, note = _page_confidence(page.text)
        sections.append(
            SectionInput(
                section_type=kind,
                heading=f"Page {page.page_number}",
                text=page.text,
                start_offset=page.start_offset,
                end_offset=page.end_offset,
                page_start=page.page_number,
                page_end=page.page_number,
                confidence=confidence,
                notes=note,
            )
        )
    return sections


# ---------------------------------------------------------------------------
# Report shapes
# ---------------------------------------------------------------------------


@dataclass
class NarrativeDocumentOutcome:
    """One narrative file's ingestion outcome."""

    issuer_id: str
    ticker: str
    document_kind: str
    source_doc_type: str
    file: str
    outcome: str  # ingested | skipped | failed
    document_id: int | None = None
    extraction_status: str = "pending"
    pages: int = 0
    sections: int = 0
    note: str = ""


@dataclass
class NarrativeIngestReport:
    """Summary of one :func:`ingest_narratives` run."""

    documents_ingested: int = 0
    documents_skipped: int = 0
    documents_failed: int = 0
    sections_written: int = 0
    pages_extracted: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    by_extraction_status: dict[str, int] = field(default_factory=dict)
    per_issuer: dict[str, dict[str, int]] = field(default_factory=dict)
    outcomes: list[NarrativeDocumentOutcome] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def _issuer_bucket(self, issuer_id: str) -> dict[str, int]:
        return self.per_issuer.setdefault(issuer_id, {"documents": 0, "pages": 0, "sections": 0})

    def summary(self) -> str:
        lines = [
            "India narrative ingestion report (research only, not investment advice)",
            (
                f"  documents: {self.documents_ingested} ingested, "
                f"{self.documents_skipped} skipped (already ingested), "
                f"{self.documents_failed} failed"
            ),
            f"  pages extracted: {self.pages_extracted}; sections written: {self.sections_written}",
            f"  by document kind: {self.by_kind or '{}'}",
            f"  by extraction status: {self.by_extraction_status or '{}'}",
        ]
        for issuer_id in sorted(self.per_issuer):
            bucket = self.per_issuer[issuer_id]
            lines.append(
                f"    {issuer_id}: {bucket['documents']} documents, "
                f"{bucket['pages']} pages, {bucket['sections']} sections"
            )
        for outcome in self.outcomes:
            if outcome.extraction_status != "ok" or outcome.outcome == "failed":
                lines.append(
                    f"    honesty: {outcome.issuer_id} {outcome.file} "
                    f"status={outcome.extraction_status} outcome={outcome.outcome} "
                    f"{outcome.note}".rstrip()
                )
        for error in self.errors:
            lines.append(f"  error: {error}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


def _verify_manifest_hash(path: Path, expected_sha256: str) -> str:
    """Return the actual sha256 hex of the cached file; raise on drift.

    The manifest is the provenance record: bytes that no longer match it are
    an error, never silently ingested.
    """
    content = path.read_bytes()
    actual = hashlib.sha256(content).hexdigest()
    if actual != expected_sha256:
        raise ValueError(
            f"cached narrative file {path} sha256 {actual} does not match the "
            f"manifest record {expected_sha256}; refusing to ingest unverified bytes"
        )
    return actual


def ingest_narratives(
    database_url: str | None = None,
    *,
    issuer_ids: list[str] | None = None,
    manifest_path: str | Path | None = None,
) -> NarrativeIngestReport:
    """Ingest every cached India narrative PDF into ``documents``/``sections``.

    Idempotent: a file whose content hash already has a document row for the
    company and kind is skipped; hash-verified files dedupe into
    ``source_artifacts`` exactly like the IND-3 import path.
    """
    manifest_path = manifest_path or DEFAULT_MANIFEST_PATH
    wanted = {i.upper() for i in (issuer_ids or [])}
    broadcast = _broadcast_lookup(manifest_path)
    report = NarrativeIngestReport()

    with session_scope(database_url) as session:
        companies = CompaniesRepo(session)
        docs = DocumentsRepo(session)
        for entry in narrative_entries(manifest_path):
            issuer_id = str(entry["issuer_id"])
            if wanted and issuer_id not in wanted:
                continue
            kind = str(entry["document_kind"])
            relative = str(entry["path"])  # e.g. storage/raw/india/<slug>/<period>/<file>
            path = REPO_ROOT / relative
            file_name = path.name
            issuer = require_verified(issuer_id)
            ticker = issuer.ticker_nse or issuer.issuer_id
            outcome = NarrativeDocumentOutcome(
                issuer_id=issuer_id,
                ticker=ticker,
                document_kind=kind,
                source_doc_type=str(entry.get("doc_type") or ""),
                file=file_name,
                outcome="failed",
            )

            try:
                content_hash = _verify_manifest_hash(path, str(entry["sha256"]))
            except (OSError, ValueError) as exc:
                outcome.note = str(exc)
                report.documents_failed += 1
                report.errors.append(str(exc))
                report.outcomes.append(outcome)
                continue

            extraction = extract_pdf(path.read_bytes())
            outcome.extraction_status = extraction.extraction_status
            outcome.pages = extraction.page_count

            company = companies.upsert_company(
                ticker=ticker,
                cik=issuer.bse_code or issuer.issuer_id,
                name=issuer.name,
                sector=issuer.sector,
                country="IN",
            )
            company.reporting_currency = "INR"
            companies.flush()

            existing = docs.find_document_by_content(
                company_id=company.id,
                accession=None,
                document_kind=kind,
                content_hash=content_hash,
            )
            if existing is not None:
                outcome.outcome = "skipped"
                outcome.document_id = existing.id
                outcome.sections = len(docs.list_sections(existing.id))
                report.documents_skipped += 1
                report.outcomes.append(outcome)
                continue

            artifact = _find_or_add_artifact(session, entry, content_hash, path)

            period_start, period_end = _period_bounds(manifest_path, str(entry["period"]))
            commentary = is_management_commentary(kind)
            metadata = {
                "document_kind": kind,
                "management_commentary": commentary,
                "commentary_note": (
                    "management statement (company narrative; NOT an independently "
                    "verified financial explanation)"
                    if commentary
                    else "company-prepared financial statements (company_ir tier; not commentary)"
                ),
                "source_doc_type": entry.get("doc_type"),
                "source_tier": entry.get("source_tier"),
                "reporting_scope": entry.get("scope"),
                "issuer_id": issuer_id,
                "period_key": entry.get("period"),
                "period_start": period_start.isoformat() if period_start else None,
                "period_end": period_end.isoformat() if period_end else None,
                "page_count": extraction.page_count,
                "manifest_sha256": entry.get("sha256"),
                "extraction_notes_extractor": (extraction.notes if extraction.notes else None),
            }
            period_end_value = period_end
            filed_at = broadcast.get((issuer_id, str(entry["period"])))

            document, _created = docs.upsert_document(
                company_id=company.id,
                accession=None,
                form=None,
                document_kind=kind,
                period_end=period_end_value,
                filed_at=filed_at,
                source_url=str(entry.get("source_url") or ""),
                source_artifact_id=artifact.id,
                extraction_version=NARRATIVE_EXTRACTION_VERSION,
                extraction_status=extraction.extraction_status,
                extraction_notes=json.dumps(metadata, sort_keys=True),
            )

            sections_written = 0
            if extraction.extraction_status == "ok":
                sections = _page_sections(kind, extraction)
                rows = docs.replace_sections(document.id, sections)
                sections_written = len(rows)

            outcome.outcome = "ingested"
            outcome.document_id = document.id
            outcome.sections = sections_written
            report.documents_ingested += 1
            report.sections_written += sections_written
            report.pages_extracted += extraction.page_count
            report.by_kind[kind] = report.by_kind.get(kind, 0) + 1
            report.by_extraction_status[extraction.extraction_status] = (
                report.by_extraction_status.get(extraction.extraction_status, 0) + 1
            )
            bucket = report._issuer_bucket(issuer_id)
            bucket["documents"] += 1
            bucket["pages"] += extraction.page_count
            bucket["sections"] += sections_written
            report.outcomes.append(outcome)
    return report


def _find_or_add_artifact(session, entry: dict, content_hash: str, path: Path) -> SourceArtifact:
    """Dedupe by content hash exactly like the IND-3 import path: re-registering
    a byte-identical file reuses the existing ``source_artifacts`` row."""
    from sqlalchemy import select

    artifact = session.scalars(
        select(SourceArtifact).where(
            SourceArtifact.source == SOURCE_INDIA,
            SourceArtifact.content_hash == content_hash,
        )
    ).first()
    if artifact is None:
        artifact = SourceArtifact(
            source=SOURCE_INDIA,
            source_url=str(entry.get("source_url") or ""),
            fetched_at=None,
            local_path=str(path),
            content_hash=content_hash,
            content_type="pdf",
            parser_version=PDF_EXTRACT_VERSION,
        )
        session.add(artifact)
        session.flush()
    elif not artifact.local_path:
        artifact.local_path = str(path)
    return artifact


# ---------------------------------------------------------------------------
# TCS reported quarterly cash flow (IND-6 open item, closed IND-7)
# ---------------------------------------------------------------------------

#: The TCS reported quarterly CFO, verified against the cached condensed
#: statement on 2026-09-11 under exactly the INFY provenance standard
#: (``pipeline.ingest_reviewed_pdf_cash_flow``): extraction
#: ``quarterline.sources.india.pdf_results.extract_cash_flow_from_pdf``,
#: page provenance, declared scale header, current-period column order
#: verified from the page's own year headers, agent-checked review status.
#: When the storage cache is present, ingestion re-extracts live and any
#: drift REFUSES the ingest (never a stale constant).
REVIEWED_TCS_PDF_CASH_FLOW: dict[str, str] = {
    "issuer_id": "IN-TCS",
    "document": "tcs-q1fy27-consolidated-indas-finstatement.pdf",
    "reference_document_id": REF_TCS_CONSO_Q1,
    "page_number": "6",
    "page": "PDF p.6 (Consolidated Interim Statement of Cash Flows)",
    "display": "12,171",
    "prior_display": "11,919",
    "value": "121710000000",
    "period_start": "2026-04-01",
    "period_end": "2026-06-30",
    "duration": "quarter",
    "filed_at": "2026-07-09",  # NSE seq 173420, broadcast 2026-07-09 18:36:12 IST
    "declared_scale_label": "(` crore) — rupee glyph extracts as a backtick",
    "note": (
        "REPORTED quarterly CFO (IND-3 correction A: legitimate at its actual reported "
        "frequency). Text-level extraction with page provenance; period resolved from the "
        "statement header 'Three months ended June 30, 2026' (period_source=page); page scale "
        "header '(` crore)' (the PDF font renders the rupee glyph as a backtick); first numeric "
        "column verified as the current period (year header 2026 before 2025); prior-year "
        "quarter column 11,919 present on the same verified line. Closes the IND-6 open item "
        "recorded in docs/india_financial_methodology.md section 4 and "
        "docs/india_source_audit.md section 12 item 10."
    ),
}

#: Rendered-line provenance tag, distinct from any XBRL tag (and from INFY's
#: rendered-line tag) so the observation identity never collides.
_TCS_PDF_CFO_TAG = "NetCashFlowsGeneratedFromOperatingActivitiesRenderedLine"


def _page_text(content: bytes, page_number: int) -> str:
    from quarterline.sources.india.pdf_results import page_texts

    return dict(page_texts(content)).get(page_number, "")


def _verify_prior_year_in_row_window(
    *, page_text: str, matched_line: str, display: str, prior_display: str
) -> None:
    """The recorded prior-year display must sit in the verified row's window
    (label line + its number continuation lines), AFTER the current display."""
    lines = page_text.splitlines()
    label = " ".join(matched_line.split())
    index = next(
        (i for i, line in enumerate(lines) if label and label in " ".join(line.split())),
        None,
    )
    if index is None:
        raise ValueError(
            "reviewed TCS PDF cash-flow verified line not found on the page; refusing to ingest"
        )
    window = "\n".join(lines[index : index + 2])  # label line + its number line
    current_at = window.find(display)
    prior_at = window.find(prior_display)
    if current_at < 0 or prior_at < 0 or prior_at < current_at:
        raise ValueError(
            "reviewed TCS PDF cash-flow prior-year corroboration token "
            f"({prior_display}) not found after ({display}) in the verified row "
            "window; refusing to ingest"
        )


def ingest_reviewed_tcs_cash_flow(database_url: str | None = None):
    """Carry TCS's agent-verified REPORTED quarterly CFO (IR PDF) as an observation.

    Mirrors ``pipeline.ingest_reviewed_pdf_cash_flow`` exactly: live drift
    guard against the cached PDF (value AND prior-year corroboration token on
    the same verified line), full page/tier provenance, ``pdf_text``
    extraction method, ``agent_checked_against_document`` review status,
    idempotent via the observation hash. Returns the IndiaIngestReport-shaped
    result from the pipeline module.
    """
    from quarterline.sources.india.pipeline import IndiaIngestReport

    reviewed = REVIEWED_TCS_PDF_CASH_FLOW
    reference_document_id = reviewed["reference_document_id"]
    if reference_document_id != REF_TCS_CONSO_Q1:  # pragma: no cover - guard
        raise ValueError("reviewed TCS cash-flow constant drifted; refusing to ingest")
    issuer = require_verified(reviewed["issuer_id"])
    report = IndiaIngestReport(issuer_id=issuer.issuer_id)

    storage_relative = RENDERED_DOCUMENT_STORAGE[reference_document_id]
    cached_pdf = REPO_ROOT / "storage" / "raw" / "india" / storage_relative
    sha256: str | None = None
    if cached_pdf.is_file():
        content = cached_pdf.read_bytes()
        sha256 = hashlib.sha256(content).hexdigest()
        from quarterline.sources.india.pdf_results import extract_cash_flow_from_pdf

        extractions = extract_cash_flow_from_pdf(content, reference_document_id)
        page_number = int(reviewed["page_number"])
        live = next(
            (e for e in extractions if e.page_number == page_number and e.ok),
            None,
        )
        if live is None or live.value != Decimal(reviewed["value"]):
            raise ValueError(
                "reviewed TCS PDF cash-flow constant no longer matches the cached "
                f"document ({reference_document_id} p.{page_number}); refusing to "
                "ingest a stale value — re-verify the extraction"
            )
        # Prior-year corroboration: the year-ago quarter figure must sit in the
        # SAME row window as the current figure (the label line plus its
        # number continuation lines), current column first — else the column
        # order the record claims is not what the page shows.
        _verify_prior_year_in_row_window(
            page_text=_page_text(content, page_number),
            matched_line=str(live.matched_line or ""),
            display=reviewed["display"],
            prior_display=reviewed["prior_display"],
        )
    # When the cache is absent the recorded constant stands (agent-checked
    # against the document; provenance below records that).

    with session_scope(database_url) as session:
        companies = CompaniesRepo(session)
        company = companies.get_by_ticker(issuer.ticker_nse or issuer.issuer_id)
        if company is None:
            raise LookupError(
                f"no company for {issuer.issuer_id} — ingest narratives or import a document first"
            )
        from quarterline.store.repositories.facts import FactsRepo

        facts_repo = FactsRepo(session)
        period_start = date.fromisoformat(reviewed["period_start"])
        period_end = date.fromisoformat(reviewed["period_end"])
        value = Decimal(reviewed["value"])
        metadata = {
            "namespace": "in-capmkt",
            "extraction_method": "pdf_text",
            "reference_document_id": reference_document_id,
            "source_document": reviewed["document"],
            "source_document_sha256": sha256,
            "page": reviewed["page"],
            "display_value": reviewed["display"],
            "prior_year_display_value": reviewed["prior_display"],
            "declared_scale": reviewed["declared_scale_label"],
            "period_source": "statement header (Three months ended June 30, 2026)",
            "review_status": REVIEW_AGENT_CHECKED,
            "reporting_scope": "consolidated",
            "revision_status": None,
            "audited_status": None,
            "note": reviewed["note"],
        }
        digest = observation_hash(
            issuer.isin,
            "in-capmkt",
            _TCS_PDF_CFO_TAG,
            "INR",
            period_start,
            period_end,
            value,
            "consolidated",
        )
        observation = FactObservation(
            company_id=company.id,
            source_artifact_id=None,
            accession=None,
            form=FORM_PDF,
            filed_at=date.fromisoformat(reviewed["filed_at"]),
            taxonomy="in-capmkt",
            original_tag=_TCS_PDF_CFO_TAG,
            canonical_concept="cash_flow_operations",
            value_decimal=decimal_to_text(value),
            unit="INR",
            currency="INR",
            period_start=period_start,
            period_end=period_end,
            period_kind="quarter",
            source_fy=fiscal_year(period_end),
            source_fp=source_fp(period_start, period_end, "quarter"),
            reporting_scope="consolidated",
            context_metadata_json=json.dumps(metadata, sort_keys=True),
            observation_hash=digest,
        )
        _, inserted = facts_repo.insert_observation_skip_duplicate(observation)
        if inserted:
            report.observations_inserted += 1
            report.by_concept["cash_flow_operations"] = 1
        else:
            report.observations_skipped += 1
    return report


__all__ = [
    "COMMENTARY_KINDS",
    "INDIA_DOCUMENT_KINDS",
    "NARRATIVE_EXTRACTION_VERSION",
    "REVIEWED_TCS_PDF_CASH_FLOW",
    "NarrativeDocumentOutcome",
    "NarrativeIngestReport",
    "ingest_narratives",
    "ingest_reviewed_tcs_cash_flow",
    "is_management_commentary",
    "narrative_entries",
    "narrative_metadata",
]
