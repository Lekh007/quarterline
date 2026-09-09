"""Document discovery + ingestion pipeline (SPEC §13).

Per company (from the watchlist CSV):

1. Discover via the submissions feed: latest 4 x 10-Q, latest 2 x 10-K, plus
   8-K filings carrying Item 2.02 whose EX-99-style earnings-release exhibit is
   reliably identifiable through the filing index (``index.json`` directory).
   Full accession + exact exhibit filename identity are preserved.
2. Download via ``SecClient`` (cache-first), validate content, write a
   ``source_artifacts`` provenance row, clean HTML, map sections, and persist
   ``documents`` + ``sections`` rows.
3. Idempotent: a document already ingested with the same accession, kind, and
   content hash is skipped; changed content is re-extracted (sections replaced).

8-K three-date rule (SPEC §13.1): filing date (``filed_at``), event date (the
8-K period of report), and the financial period *discussed* are three distinct
things. The discussed period is derived from the exhibit text only — never from
the filing date alone. It is stored in ``period_end`` when determinable, else
stays null with a note. Deviation note: the W0 ``documents`` schema has no
event_date/notes column, so the event date and the determination note ride on
:class:`IngestReport` (``eight_k_dates``) alongside the persisted row.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from quarterline.config import get_settings
from quarterline.ingest.cache import cache_get
from quarterline.ingest.html_clean import CleanedDocument, ContentValidationError, clean_html
from quarterline.ingest.pdf_extract import PDF_EXTRACT_VERSION, PdfExtraction, extract_pdf
from quarterline.ingest.sections import (
    SectionCandidate,
    detect_sections,
    earnings_release_candidate,
)
from quarterline.sources.sec.client import (
    SecClient,
    SecDownloadResult,
    SecRequestError,
)
from quarterline.sources.sec.companyfacts import cik10
from quarterline.sources.sec.submissions import (
    FilingRef,
    build_filing_url,
    fetch_submissions,
    list_recent_filings,
)
from quarterline.store.db import session_scope
from quarterline.store.models import SourceArtifact
from quarterline.store.repositories.documents import DocumentsRepo, SectionInput

#: Version stamped on document rows produced by this pipeline.
DOCUMENTS_EXTRACTION_VERSION = "documents-1"

DEFAULT_WATCHLIST = "data/watchlist_us.csv"


# ---------------------------------------------------------------------------
# Configuration and watchlist
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FormsConfig:
    """Discovery limits (SPEC §13.1: latest 4 x 10-Q, latest 2 x 10-K, 8-K Item 2.02)."""

    forms: tuple[str, ...] = ("10-Q", "10-K", "8-K")
    ten_q_limit: int = 4
    ten_k_limit: int = 2
    eight_k_scan_limit: int = 12
    eight_k_max_exhibits: int = 1


@dataclass(frozen=True)
class WatchlistEntry:
    ticker: str
    cik: str
    name: str
    sector: str
    country: str


def read_watchlist(path: str | Path) -> list[WatchlistEntry]:
    """Read the watchlist CSV (ticker,cik,name,sector,country)."""
    entries: list[WatchlistEntry] = []
    with open(path, encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            ticker = (row.get("ticker") or "").strip()
            cik = (row.get("cik") or "").strip()
            if not ticker or not cik:
                continue
            entries.append(
                WatchlistEntry(
                    ticker=ticker.upper(),
                    cik=cik,
                    name=(row.get("name") or "").strip(),
                    sector=(row.get("sector") or "").strip(),
                    country=(row.get("country") or "").strip(),
                )
            )
    return entries


# ---------------------------------------------------------------------------
# 8-K three-date logic (SPEC §13.1)
# ---------------------------------------------------------------------------

_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
_DATE_PATTERN = (
    r"(?P<month>January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+(?P<day>\d{1,2}),?\s+(?P<year>\d{4})"
)
_PERIOD_PATTERNS = (
    re.compile(r"(?:three|six|nine)\s+months\s+ended\s+" + _DATE_PATTERN, re.IGNORECASE),
    re.compile(r"quarter(?:\s+\w+)?\s+ended\s+" + _DATE_PATTERN, re.IGNORECASE),
    re.compile(r"(?:fiscal\s+)?year\s+ended\s+" + _DATE_PATTERN, re.IGNORECASE),
)


@dataclass(frozen=True)
class ThreeDates:
    """The three distinct 8-K dates (SPEC §13.1); never conflated."""

    filed_at: date | None
    event_date: date | None
    discussed_period: date | None
    note: str


def _find_discussed_period(exhibit_text: str) -> date | None:
    """Earliest explicit period phrase in the exhibit text, or None."""
    window = exhibit_text[:8000]
    best: tuple[int, date] | None = None
    for pattern in _PERIOD_PATTERNS:
        for match in pattern.finditer(window):
            parsed = _parse_month_day_year(match)
            if parsed is not None and (best is None or match.start() < best[0]):
                best = (match.start(), parsed)
    return best[1] if best else None


def _parse_month_day_year(match: re.Match) -> date | None:
    month = _MONTHS.get(match.group("month").lower())
    day = int(match.group("day"))
    year = int(match.group("year"))
    if month is None or not 1 <= day <= 31:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def resolve_8k_dates(
    *, filed_at: date | None, event_date: date | None, exhibit_text: str
) -> ThreeDates:
    """Distinguish filing date, event date, and discussed financial period.

    The discussed period comes from an explicit phrase in the exhibit text
    ("quarter ended March 28, 2026" etc.). It is never inferred from the
    filing or event date alone; when absent it stays None with a note.
    """
    discussed = _find_discussed_period(exhibit_text or "")
    notes: list[str] = []
    if discussed is None:
        notes.append(
            "discussed financial period not determinable from exhibit text; "
            "not inferred from filing or event date"
        )
    elif event_date is not None and discussed != event_date:
        notes.append(
            f"discussed period {discussed.isoformat()} differs from 8-K event date "
            f"{event_date.isoformat()}"
        )
    return ThreeDates(
        filed_at=filed_at, event_date=event_date, discussed_period=discussed, note="; ".join(notes)
    )


# ---------------------------------------------------------------------------
# Report shapes
# ---------------------------------------------------------------------------


@dataclass
class DocumentOutcome:
    ticker: str
    accession: str | None
    form: str | None
    document_kind: str
    source_url: str
    outcome: str  # ingested | skipped | failed
    document_id: int | None = None
    extraction_status: str = "pending"
    note: str = ""


@dataclass
class IngestReport:
    companies: int = 0
    documents_ingested: int = 0
    documents_skipped: int = 0
    documents_failed: int = 0
    sections_created: int = 0
    by_form: dict[str, int] = field(default_factory=dict)
    by_extraction_status: dict[str, int] = field(default_factory=dict)
    section_types: dict[str, int] = field(default_factory=dict)
    eight_k_dates: list[dict[str, object]] = field(default_factory=list)
    low_confidence_sections: list[dict[str, object]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    outcomes: list[DocumentOutcome] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            "Document ingestion report (research only, not investment advice)",
            f"  companies scanned: {self.companies}",
            (
                f"  documents: {self.documents_ingested} ingested, "
                f"{self.documents_skipped} skipped (already ingested), "
                f"{self.documents_failed} failed"
            ),
            f"  sections written: {self.sections_created}",
            f"  by form: {self.by_form or '{}'}",
            f"  extraction status: {self.by_extraction_status or '{}'}",
            f"  section types: {self.section_types or '{}'}",
        ]
        if self.eight_k_dates:
            lines.append("  8-K dates (filing / event / discussed):")
            for entry in self.eight_k_dates:
                note = f" — {entry['note']}" if entry.get("note") else ""
                lines.append(
                    f"    {entry['ticker']} {entry['accession']} {entry['exhibit']}: "
                    f"filed={entry['filed_at']} event={entry['event_date']} "
                    f"discussed={entry['discussed_period']}{note}"
                )
        if self.low_confidence_sections:
            lines.append(
                f"  low-confidence sections ({len(self.low_confidence_sections)}): "
                "recorded as 'other'/low confidence, not mislabelled"
            )
            for entry in self.low_confidence_sections[:10]:
                lines.append(f"    {entry}")
        for error in self.errors[:20]:
            lines.append(f"  error: {error}")
        if len(self.errors) > 20:
            lines.append(f"  ... and {len(self.errors) - 20} more errors")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 8-K exhibit discovery
# ---------------------------------------------------------------------------

#: EX-99-style exhibit filenames: ex99_1.htm, ex-99.1.htm, exhibit99-1.htm,
#: a8-kex991q2202603282026.htm ... The exhibit marker is glued to company
#: prefixes in real EDGAR names, so there is no word-boundary anchor; index
#: listings ("...-index.htm", "index.json") are excluded by name instead.
_8K_EX99_NAME_RE = re.compile(r"(?i)ex[a-z]{0,8}[\s_.\-]*99")


def _is_ex99_candidate(name: str) -> bool:
    lowered = name.lower()
    if "index" in lowered:
        return False
    return bool(_8K_EX99_NAME_RE.search(name))


def _list_item_202_8k_filings(client: SecClient, cik: str, limit: int) -> list[FilingRef]:
    """Recent 8-Ks carrying Item 2.02, from the submissions parallel arrays.

    ``list_recent_filings`` does not expose the ``items`` array, so this walks
    the arrays here with the same lockstep discipline.
    """
    data = fetch_submissions(client, cik)
    recent = (data.get("filings") or {}).get("recent") or {}
    accessions = recent.get("accessionNumber") or []
    forms = recent.get("form") or []
    filed_dates = recent.get("filingDate") or []
    report_dates = recent.get("reportDate") or []
    primary_documents = recent.get("primaryDocument") or []
    items_lists = recent.get("items") or []

    refs: list[FilingRef] = []
    for accession, form, filed, report, document, items in zip(
        accessions, forms, filed_dates, report_dates, primary_documents, items_lists, strict=False
    ):
        if str(form).upper() != "8-K":
            continue
        item_codes = [code.strip() for code in str(items or "").split(",") if code.strip()]
        if "2.02" not in item_codes:
            continue
        refs.append(
            FilingRef(
                accession=str(accession).replace("-", ""),
                accession_raw=str(accession),
                form=str(form),
                filed_at=str(filed),
                report_period_end=str(report),
                primary_document=str(document),
            )
        )
        if len(refs) >= limit:
            break
    return refs


def _find_ex99_exhibits(
    client: SecClient, cik: str, ref: FilingRef, max_exhibits: int
) -> list[tuple[str, str]]:
    """(filename, url) of reliably identifiable EX-99 exhibits via the index."""
    index_url = f"https://www.sec.gov/Archives/edgar/data/{cik10(cik)}/{ref.accession}/index.json"
    data = client.get_json(index_url)
    names = [
        str(entry.get("name") or "") for entry in ((data.get("directory") or {}).get("item") or [])
    ]
    matches = sorted(
        name
        for name in names
        if name.lower().endswith((".htm", ".html")) and _is_ex99_candidate(name)
    )
    return [
        (name, f"https://www.sec.gov/Archives/edgar/data/{cik10(cik)}/{ref.accession}/{name}")
        for name in matches[:max_exhibits]
    ]


# ---------------------------------------------------------------------------
# Ingestion internals
# ---------------------------------------------------------------------------


def _parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def _artifact_details(url: str) -> tuple[str | None, str | None, datetime | None]:
    """(local_path, content_type, fetched_at) from the raw disk cache, if any."""
    entry = cache_get(url)
    if entry is None:
        return None, None, None
    fetched = None
    if entry.fetched_at:
        try:
            fetched = datetime.fromisoformat(entry.fetched_at)
        except ValueError:
            fetched = None
    return str(entry.path), entry.content_type, fetched


def _add_artifact(
    repo: DocumentsRepo,
    *,
    source: str,
    url: str,
    content_hash: str,
    content_type: str | None,
    etag: str | None,
    last_modified: str | None,
    parser_version: str,
    local_path: str | None,
    fetched_at: datetime | None,
) -> SourceArtifact:
    return repo.add_source_artifact(
        source=source,
        source_url=url,
        fetched_at=fetched_at or datetime.now(UTC),
        local_path=local_path,
        content_hash=content_hash,
        content_type=content_type,
        http_etag=etag,
        http_last_modified=last_modified,
        parser_version=parser_version,
    )


def _section_inputs(
    candidates: list[SectionCandidate], cleaned: CleanedDocument
) -> list[SectionInput]:
    inputs = []
    for candidate in candidates:
        inputs.append(
            SectionInput(
                section_type=candidate.section_type,
                heading=candidate.heading,
                # Exact slice of the cleaned text (roundtrip invariant).
                text=cleaned.text[candidate.start_offset : candidate.end_offset],
                start_offset=candidate.start_offset,
                end_offset=candidate.end_offset,
                confidence=candidate.confidence,
                notes=candidate.notes or None,
            )
        )
    return inputs


class _Ingestor:
    """Shared download -> validate -> persist flow for one company."""

    def __init__(self, repo: DocumentsRepo, client: SecClient, report: IngestReport) -> None:
        self.repo = repo
        self.client = client
        self.report = report

    def _download(self, url: str) -> SecDownloadResult | None:
        try:
            return self.client.download(url)
        except SecRequestError as exc:
            self.report.errors.append(f"download failed for {url}: {exc}")
            return None

    def _already_ingested(
        self,
        company_id: int,
        *,
        accession: str | None,
        document_kind: str,
        content_hash: str,
    ) -> int | None:
        existing = self.repo.find_document_by_content(
            company_id=company_id,
            accession=accession,
            document_kind=document_kind,
            content_hash=content_hash,
        )
        return existing.id if existing is not None else None

    def _record_outcome(self, outcome: DocumentOutcome) -> None:
        self.report.outcomes.append(outcome)
        if outcome.outcome == "ingested":
            self.report.documents_ingested += 1
            form_key = outcome.form or outcome.document_kind
            self.report.by_form[form_key] = self.report.by_form.get(form_key, 0) + 1
            self.report.by_extraction_status[outcome.extraction_status] = (
                self.report.by_extraction_status.get(outcome.extraction_status, 0) + 1
            )
        elif outcome.outcome == "skipped":
            self.report.documents_skipped += 1
        else:
            self.report.documents_failed += 1

    def _record_sections(
        self, ticker: str, candidates: list[SectionCandidate], section_rows: list[object]
    ) -> None:
        self.report.sections_created += len(section_rows)
        for candidate in candidates:
            kind = candidate.section_type
            self.report.section_types[kind] = self.report.section_types.get(kind, 0) + 1
            if kind == "other" or candidate.confidence < 0.5:
                self.report.low_confidence_sections.append(
                    {
                        "ticker": ticker,
                        "section_type": kind,
                        "heading": candidate.heading,
                        "confidence": candidate.confidence,
                        "notes": candidate.notes,
                        "start_offset": candidate.start_offset,
                        "end_offset": candidate.end_offset,
                    }
                )

    def ingest_periodic(self, entry, company, ref: FilingRef, kind: str) -> None:
        """One 10-Q / 10-K primary document."""
        url = build_filing_url(ref.accession, ref.primary_document, entry.cik)
        outcome = DocumentOutcome(
            ticker=entry.ticker,
            accession=ref.accession,
            form=ref.form,
            document_kind=kind,
            source_url=url,
            outcome="failed",
        )
        downloaded = self._download(url)
        if downloaded is None:
            outcome.note = "download failed"
            self._record_outcome(outcome)
            return
        content = downloaded.content
        content_hash = _sha256_hex(content)
        already = self._already_ingested(
            company.id, accession=ref.accession, document_kind=kind, content_hash=content_hash
        )
        if already is not None:
            outcome.outcome = "skipped"
            outcome.document_id = already
            outcome.extraction_status = "ok"
            self._record_outcome(outcome)
            return

        local_path, cached_type, fetched_at = _artifact_details(url)
        artifact = _add_artifact(
            self.repo,
            source="sec",
            url=url,
            content_hash=content_hash,
            content_type=cached_type or "text/html",
            etag=downloaded.etag,
            last_modified=downloaded.last_modified,
            parser_version=DOCUMENTS_EXTRACTION_VERSION,
            local_path=local_path,
            fetched_at=fetched_at,
        )

        period_end = _parse_iso_date(ref.report_period_end)
        filed_at = _parse_iso_date(ref.filed_at)

        try:
            cleaned = clean_html(content)
        except ContentValidationError as exc:
            document, _created = self.repo.upsert_document(
                company_id=company.id,
                accession=ref.accession,
                form=ref.form,
                document_kind=kind,
                period_end=period_end,
                filed_at=filed_at,
                source_url=url,
                source_artifact_id=artifact.id,
                extraction_version=DOCUMENTS_EXTRACTION_VERSION,
                extraction_status="failed",
            )
            outcome.outcome = "failed"
            outcome.document_id = document.id
            outcome.extraction_status = "failed"
            outcome.note = f"content validation failed: {exc}"
            self.report.errors.append(f"{entry.ticker} {kind} {ref.accession_raw}: {exc}")
            self._record_outcome(outcome)
            return

        if not cleaned.blocks:
            document, _created = self.repo.upsert_document(
                company_id=company.id,
                accession=ref.accession,
                form=ref.form,
                document_kind=kind,
                period_end=period_end,
                filed_at=filed_at,
                source_url=url,
                source_artifact_id=artifact.id,
                extraction_version=DOCUMENTS_EXTRACTION_VERSION,
                extraction_status="failed",
            )
            outcome.outcome = "failed"
            outcome.document_id = document.id
            outcome.extraction_status = "failed"
            outcome.note = "no text blocks extracted"
            self._record_outcome(outcome)
            return

        candidates = detect_sections(ref.form, cleaned)
        document, _created = self.repo.upsert_document(
            company_id=company.id,
            accession=ref.accession,
            form=ref.form,
            document_kind=kind,
            period_end=period_end,
            filed_at=filed_at,
            source_url=url,
            source_artifact_id=artifact.id,
            extraction_version=DOCUMENTS_EXTRACTION_VERSION,
            extraction_status="ok",
        )
        rows = self.repo.replace_sections(document.id, _section_inputs(candidates, cleaned))
        outcome.outcome = "ingested"
        outcome.document_id = document.id
        outcome.extraction_status = "ok"
        self._record_outcome(outcome)
        self._record_sections(entry.ticker, candidates, rows)

    def ingest_earnings_release(self, entry, company, ref: FilingRef, cfg: FormsConfig) -> None:
        """One 8-K Item 2.02: locate its EX-99 exhibit and ingest it."""
        try:
            exhibits = _find_ex99_exhibits(self.client, entry.cik, ref, cfg.eight_k_max_exhibits)
        except (SecRequestError, ValueError) as exc:
            # HTTP failure or a non-JSON / malformed index payload: record and skip.
            self.report.errors.append(
                f"{entry.ticker} 8-K {ref.accession_raw}: exhibit index unavailable ({exc}); skipped"
            )
            return
        if not exhibits:
            self.report.errors.append(
                f"{entry.ticker} 8-K {ref.accession_raw}: no reliably identifiable "
                "EX-99 exhibit in filing index; skipped (not guessed)"
            )
            return

        for filename, url in exhibits:
            kind = "8k_earnings_release"
            outcome = DocumentOutcome(
                ticker=entry.ticker,
                accession=ref.accession,
                form=ref.form,
                document_kind=kind,
                source_url=url,
                outcome="failed",
                note=f"exhibit {filename}",
            )
            downloaded = self._download(url)
            if downloaded is None:
                self._record_outcome(outcome)
                continue
            content = downloaded.content
            content_hash = _sha256_hex(content)
            already = self._already_ingested(
                company.id, accession=ref.accession, document_kind=kind, content_hash=content_hash
            )
            if already is not None:
                outcome.outcome = "skipped"
                outcome.document_id = already
                outcome.extraction_status = "ok"
                self._record_outcome(outcome)
                continue

            local_path, cached_type, fetched_at = _artifact_details(url)
            artifact = _add_artifact(
                self.repo,
                source="sec",
                url=url,
                content_hash=content_hash,
                content_type=cached_type or "text/html",
                etag=downloaded.etag,
                last_modified=downloaded.last_modified,
                parser_version=DOCUMENTS_EXTRACTION_VERSION,
                local_path=local_path,
                fetched_at=fetched_at,
            )
            try:
                cleaned = clean_html(content)
            except ContentValidationError as exc:
                document, _created = self.repo.upsert_document(
                    company_id=company.id,
                    accession=ref.accession,
                    form=ref.form,
                    document_kind=kind,
                    period_end=None,
                    filed_at=_parse_iso_date(ref.filed_at),
                    source_url=url,
                    source_artifact_id=artifact.id,
                    extraction_version=DOCUMENTS_EXTRACTION_VERSION,
                    extraction_status="failed",
                )
                outcome.outcome = "failed"
                outcome.document_id = document.id
                outcome.extraction_status = "failed"
                outcome.note = f"content validation failed: {exc}"
                self.report.errors.append(f"{entry.ticker} 8-K exhibit {filename}: {exc}")
                self._record_outcome(outcome)
                continue

            if not cleaned.blocks:
                outcome.outcome = "failed"
                outcome.note = "no text blocks extracted"
                self._record_outcome(outcome)
                continue

            dates = resolve_8k_dates(
                filed_at=_parse_iso_date(ref.filed_at),
                event_date=_parse_iso_date(ref.report_period_end),
                exhibit_text=cleaned.text,
            )
            candidate = earnings_release_candidate(cleaned)
            document, _created = self.repo.upsert_document(
                company_id=company.id,
                accession=ref.accession,
                form=ref.form,
                document_kind=kind,
                # discussed period only — never inferred from the filing date
                period_end=dates.discussed_period,
                filed_at=dates.filed_at,
                source_url=url,
                source_artifact_id=artifact.id,
                extraction_version=DOCUMENTS_EXTRACTION_VERSION,
                extraction_status="ok",
                event_date=dates.event_date,
                extraction_notes=dates.note or None,
            )
            rows = self.repo.replace_sections(document.id, _section_inputs([candidate], cleaned))
            self.report.eight_k_dates.append(
                {
                    "ticker": entry.ticker,
                    "accession": ref.accession_raw,
                    "exhibit": filename,
                    "filed_at": dates.filed_at.isoformat() if dates.filed_at else None,
                    "event_date": dates.event_date.isoformat() if dates.event_date else None,
                    "discussed_period": (
                        dates.discussed_period.isoformat() if dates.discussed_period else None
                    ),
                    "note": dates.note,
                }
            )
            outcome.outcome = "ingested"
            outcome.document_id = document.id
            outcome.extraction_status = "ok"
            self._record_outcome(outcome)
            self._record_sections(entry.ticker, [candidate], rows)


def _sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def ingest_documents(
    watchlist_csv: str | Path = DEFAULT_WATCHLIST,
    forms_config: FormsConfig | None = None,
    *,
    client: SecClient | None = None,
) -> IngestReport:
    """Discover and ingest SEC documents for every watchlist company."""
    cfg = forms_config or FormsConfig()
    if client is None:
        client = SecClient(get_settings())

    entries = read_watchlist(watchlist_csv)
    report = IngestReport(companies=len(entries))

    with session_scope() as session:
        repo = DocumentsRepo(session)
        ingestor = _Ingestor(repo, client, report)
        for entry in entries:
            company = repo.get_or_create_company(
                entry.ticker,
                cik=entry.cik,
                name=entry.name or None,
                sector=entry.sector or None,
                country=entry.country or None,
            )
            if "10-Q" in cfg.forms:
                for ref in list_recent_filings(client, entry.cik, ["10-Q"], cfg.ten_q_limit):
                    ingestor.ingest_periodic(entry, company, ref, "10-Q")
            if "10-K" in cfg.forms:
                for ref in list_recent_filings(client, entry.cik, ["10-K"], cfg.ten_k_limit):
                    ingestor.ingest_periodic(entry, company, ref, "10-K")
            if "8-K" in cfg.forms:
                for ref in _list_item_202_8k_filings(client, entry.cik, cfg.eight_k_scan_limit):
                    ingestor.ingest_earnings_release(entry, company, ref, cfg)

    # Reflect persisted roll-ups (also catches rows from earlier runs).
    with session_scope() as session:
        repo = DocumentsRepo(session)
        report.by_form = repo.count_documents_by_form()
        report.by_extraction_status = repo.count_documents_by_status()
        report.section_types = repo.count_sections_by_type()
    return report


def ingest_pdf_file(
    company: str,
    source_url: str,
    doc_date: date | str,
    path: str | Path,
    *,
    session: Session | None = None,
) -> int:
    """Ingest a real company IR PDF (SPEC §13.3); returns the document id.

    Retains source URL, company, document date, per-page offsets/status, and
    content hash. A ``needs_ocr`` result is stored with no sections — empty
    text is never silently indexed.
    """
    content = Path(path).read_bytes()
    extraction = extract_pdf(content)
    parsed_date = doc_date if isinstance(doc_date, date) else _parse_iso_date(str(doc_date))
    if parsed_date is None:
        raise ValueError(f"doc_date must be ISO YYYY-MM-DD, got {doc_date!r}")

    if session is not None:
        return _ingest_pdf_into(
            session, company, source_url, parsed_date, path, content, extraction
        )
    with session_scope() as sess:
        return _ingest_pdf_into(sess, company, source_url, parsed_date, path, content, extraction)


def _ingest_pdf_into(
    session: Session,
    company: str,
    source_url: str,
    doc_date: date,
    path: str | Path,
    content: bytes,
    extraction: PdfExtraction,
) -> int:
    repo = DocumentsRepo(session)
    company_row = repo.get_or_create_company(company)
    artifact = repo.add_source_artifact(
        source="ir",
        source_url=source_url,
        fetched_at=datetime.now(UTC),
        local_path=str(path),
        content_hash=extraction.content_hash,
        content_type="application/pdf",
        parser_version=PDF_EXTRACT_VERSION,
    )
    existing = repo.find_document(
        company_id=company_row.id,
        accession=None,
        document_kind="pdf",
        source_url=source_url,
    )
    if existing is not None and existing.source_artifact_id is not None:
        prior = repo.get_source_artifact(existing.source_artifact_id)
        if prior is not None and prior.content_hash == extraction.content_hash:
            return existing.id  # idempotent: same content, same document

    document, _created = repo.upsert_document(
        company_id=company_row.id,
        accession=None,
        form=None,
        document_kind="pdf",
        period_end=None,
        filed_at=doc_date,
        source_url=source_url,
        source_artifact_id=artifact.id,
        extraction_version=PDF_EXTRACT_VERSION,
        extraction_status=extraction.extraction_status,
    )
    if extraction.extraction_status == "ok":
        repo.replace_sections(
            document.id,
            [
                SectionInput(
                    section_type="other",
                    heading=f"Page {page.page_number}",
                    text=page.text,
                    start_offset=page.start_offset,
                    end_offset=page.end_offset,
                    page_start=page.page_number,
                    page_end=page.page_number,
                )
                for page in extraction.pages
            ],
        )
    # needs_ocr / failed: no sections — never silently index empty text.
    return document.id


# ---------------------------------------------------------------------------
# CLI registration (contract: SUBCOMMAND_REGISTRY, no cli.py edits)
# ---------------------------------------------------------------------------


def handle_ingest_documents(args: argparse.Namespace) -> int:
    """Handler for ``quarterline ingest documents --watchlist <csv>``."""
    forms = tuple(str(form).upper() for form in (getattr(args, "forms", None) or []))
    cfg = FormsConfig(forms=forms) if forms else None
    watchlist = getattr(args, "watchlist", DEFAULT_WATCHLIST) or DEFAULT_WATCHLIST
    report = ingest_documents(watchlist, cfg)
    print(report.summary())
    if report.errors and not (report.documents_ingested or report.documents_skipped):
        return 1
    return 0


def register_cli() -> None:
    """Register the handler in the W0 SUBCOMMAND_REGISTRY (no cli.py edit)."""
    from quarterline.cli import register_subcommand

    register_subcommand("ingest:documents", handle_ingest_documents)


register_cli()
