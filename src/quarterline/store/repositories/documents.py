"""Session-scoped repository for documents, sections, and source artifacts.

Contract C5: typed methods only — no raw SQL strings ever leave this module
(SPEC §2.3.5). Identity rules:

- a document is unique by (company, accession, document_kind, source_url);
  ``source_url`` carries the exact exhibit filename identity for 8-K exhibits
  (SPEC §13.1) and is the sole discriminator for accession-less IR PDFs;
- offset lookups power later evidence-span resolution (contract C8).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from quarterline.store.models import Company, Document, Section, SourceArtifact


@dataclass(frozen=True)
class SectionInput:
    """Value object for writing one section row."""

    section_type: str
    heading: str
    text: str
    start_offset: int | None = None
    end_offset: int | None = None
    page_start: int | None = None
    page_end: int | None = None
    confidence: float | None = None
    notes: str | None = None


class DocumentsRepo:
    """Typed, session-scoped access to the documents track tables."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # -- companies ----------------------------------------------------------

    def get_company_by_ticker(self, ticker: str) -> Company | None:
        stmt = select(Company).where(Company.ticker == ticker.strip().upper())
        return self.session.scalars(stmt).first()

    def get_or_create_company(
        self,
        ticker: str,
        *,
        cik: str | None = None,
        name: str | None = None,
        sector: str | None = None,
        country: str | None = None,
    ) -> Company:
        """Find by ticker (falling back to CIK), create when absent."""
        clean_ticker = ticker.strip().upper()
        company = self.get_company_by_ticker(clean_ticker)
        if company is None and cik:
            stmt = select(Company).where(Company.cik == cik.strip().zfill(10))
            company = self.session.scalars(stmt).first()
        if company is None:
            company = Company(
                ticker=clean_ticker,
                # CIK is required knowledge for SEC-sourced companies; an
                # accession-less IR PDF may arrive without one. Missing stays
                # missing, but the column is NOT NULL -> honest short marker.
                cik=(cik or "").strip().zfill(10) or f"UNK-{clean_ticker[:6]}",
                name=name,
                sector=sector,
                country=country,
            )
            self.session.add(company)
            self.session.flush()
        elif cik and company.cik != cik.strip().zfill(10):
            company.cik = cik.strip().zfill(10)
        if name and not company.name:
            company.name = name
        return company

    # -- source artifacts ---------------------------------------------------

    def add_source_artifact(
        self,
        *,
        source: str,
        source_url: str,
        fetched_at: datetime | None = None,
        local_path: str | None = None,
        content_hash: str | None = None,
        content_type: str | None = None,
        http_etag: str | None = None,
        http_last_modified: str | None = None,
        parser_version: str | None = None,
    ) -> SourceArtifact:
        artifact = SourceArtifact(
            source=source,
            source_url=source_url,
            fetched_at=fetched_at,
            local_path=local_path,
            content_hash=content_hash,
            content_type=content_type,
            http_etag=http_etag,
            http_last_modified=http_last_modified,
            parser_version=parser_version,
        )
        self.session.add(artifact)
        self.session.flush()
        return artifact

    def get_source_artifact(self, artifact_id: int) -> SourceArtifact | None:
        return self.session.get(SourceArtifact, artifact_id)

    # -- documents ----------------------------------------------------------

    def find_document(
        self,
        *,
        company_id: int,
        accession: str | None,
        document_kind: str,
        source_url: str | None,
    ) -> Document | None:
        stmt = select(Document).where(
            Document.company_id == company_id,
            Document.document_kind == document_kind,
        )
        if accession is None:
            stmt = stmt.where(Document.accession.is_(None))
        else:
            stmt = stmt.where(Document.accession == accession)
        if source_url is not None:
            stmt = stmt.where(Document.source_url == source_url)
        return self.session.scalars(stmt).first()

    def find_document_by_content(
        self,
        *,
        company_id: int,
        accession: str | None,
        document_kind: str,
        content_hash: str,
    ) -> Document | None:
        """Document already ingested with exactly this content hash (idempotency)."""
        stmt = (
            select(Document)
            .join(SourceArtifact, Document.source_artifact_id == SourceArtifact.id)
            .where(
                Document.company_id == company_id,
                Document.document_kind == document_kind,
                SourceArtifact.content_hash == content_hash,
            )
        )
        if accession is None:
            stmt = stmt.where(Document.accession.is_(None))
        else:
            stmt = stmt.where(Document.accession == accession)
        return self.session.scalars(stmt).first()

    def get_document(self, document_id: int) -> Document | None:
        return self.session.get(Document, document_id)

    def upsert_document(
        self,
        *,
        company_id: int,
        accession: str | None,
        form: str | None,
        document_kind: str,
        period_end: date | None = None,
        filed_at: date | None = None,
        source_url: str | None = None,
        source_artifact_id: int | None = None,
        extraction_version: str | None = None,
        extraction_status: str | None = None,
        event_date: date | None = None,
        extraction_notes: str | None = None,
    ) -> tuple[Document, bool]:
        """Insert or update a document keyed by the identity rule of this module."""
        existing = self.find_document(
            company_id=company_id,
            accession=accession,
            document_kind=document_kind,
            source_url=source_url,
        )
        if existing is None:
            document = Document(
                company_id=company_id,
                accession=accession,
                form=form,
                document_kind=document_kind,
                period_end=period_end,
                filed_at=filed_at,
                source_url=source_url,
                source_artifact_id=source_artifact_id,
                extraction_version=extraction_version,
                extraction_status=extraction_status,
                event_date=event_date,
                extraction_notes=extraction_notes,
            )
            self.session.add(document)
            self.session.flush()
            return document, True
        existing.form = form
        existing.period_end = period_end
        existing.filed_at = filed_at
        if event_date is not None:
            existing.event_date = event_date
        if extraction_notes is not None:
            existing.extraction_notes = extraction_notes
        if source_artifact_id is not None:
            existing.source_artifact_id = source_artifact_id
        if extraction_version is not None:
            existing.extraction_version = extraction_version
        if extraction_status is not None:
            existing.extraction_status = extraction_status
        self.session.flush()
        return existing, False

    def list_documents(
        self,
        *,
        company_id: int | None = None,
        form: str | None = None,
        document_kind: str | None = None,
        extraction_status: str | None = None,
    ) -> list[Document]:
        stmt = select(Document).order_by(Document.id)
        if company_id is not None:
            stmt = stmt.where(Document.company_id == company_id)
        if form is not None:
            stmt = stmt.where(Document.form == form)
        if document_kind is not None:
            stmt = stmt.where(Document.document_kind == document_kind)
        if extraction_status is not None:
            stmt = stmt.where(Document.extraction_status == extraction_status)
        return list(self.session.scalars(stmt).all())

    # -- sections -----------------------------------------------------------

    def replace_sections(self, document_id: int, sections: Sequence[SectionInput]) -> list[Section]:
        """Replace all sections of a document (re-extraction is destructive)."""
        for existing in self.list_sections(document_id):
            self.session.delete(existing)
        self.session.flush()
        rows = [
            Section(
                document_id=document_id,
                section_type=s.section_type,
                heading=s.heading,
                text=s.text,
                start_offset=s.start_offset,
                end_offset=s.end_offset,
                page_start=s.page_start,
                page_end=s.page_end,
                confidence=s.confidence,
                notes=s.notes,
            )
            for s in sections
        ]
        self.session.add_all(rows)
        self.session.flush()
        return rows

    def list_sections(self, document_id: int) -> list[Section]:
        stmt = (
            select(Section)
            .where(Section.document_id == document_id)
            .order_by(Section.start_offset.nulls_first())
        )
        return list(self.session.scalars(stmt).all())

    def get_section(self, section_id: int) -> Section | None:
        return self.session.get(Section, section_id)

    # -- offset lookups (evidence spans, contract C8) ------------------------

    def find_section_containing(self, document_id: int, offset: int) -> Section | None:
        """Section whose [start_offset, end_offset) contains ``offset``."""
        stmt = select(Section).where(
            Section.document_id == document_id,
            Section.start_offset.is_not(None),
            Section.end_offset.is_not(None),
            Section.start_offset <= offset,
            Section.end_offset > offset,
        )
        return self.session.scalars(stmt).first()

    def resolve_span(self, document_id: int, start: int, end: int) -> Section | None:
        """Section fully containing the span [start, end]."""
        stmt = select(Section).where(
            Section.document_id == document_id,
            Section.start_offset.is_not(None),
            Section.end_offset.is_not(None),
            Section.start_offset <= start,
            Section.end_offset >= end,
        )
        return self.session.scalars(stmt).first()

    # -- extraction-report queries -------------------------------------------

    def count_documents_by_status(self) -> dict[str, int]:
        return self._count_by(Document.extraction_status)

    def count_documents_by_form(self) -> dict[str, int]:
        return self._count_by(Document.form)

    def count_sections_by_type(self) -> dict[str, int]:
        stmt = select(Section.section_type, func.count()).group_by(Section.section_type)
        return {
            (label if label is not None else "none"): count
            for label, count in self.session.execute(stmt).all()
        }

    def documents_without_sections(self) -> list[Document]:
        stmt = (
            select(Document)
            .outerjoin(Section, Section.document_id == Document.id)
            .where(Section.id.is_(None))
            .order_by(Document.id)
        )
        return list(self.session.scalars(stmt).all())

    # -- helpers --------------------------------------------------------------

    def _count_by(self, column: Any) -> dict[str, int]:
        stmt = select(column, func.count()).group_by(column)
        return {
            (value if value is not None else "none"): count
            for value, count in self.session.execute(stmt).all()
        }
