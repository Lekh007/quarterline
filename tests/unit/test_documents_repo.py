"""Unit tests for the documents-track repository (contract C5)."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.orm import Session, sessionmaker

from quarterline.store.db import get_engine
from quarterline.store.models import Base
from quarterline.store.repositories.documents import DocumentsRepo, SectionInput


@pytest.fixture
def session(offline_env) -> Session:
    Base.metadata.create_all(get_engine())
    factory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    sess = factory()
    yield sess
    sess.close()


def test_get_or_create_company_dedupes_by_ticker(session: Session) -> None:
    repo = DocumentsRepo(session)
    first = repo.get_or_create_company("aapl", cik="320193", name="Apple Inc.")
    second = repo.get_or_create_company("AAPL", cik="0000320193", name="Apple Inc.")
    assert first.id == second.id
    assert second.cik == "0000320193"
    assert second.name == "Apple Inc."


def test_upsert_document_identity_and_update(session: Session) -> None:
    repo = DocumentsRepo(session)
    company = repo.get_or_create_company("MSFT", cik="0000789019")

    doc, created = repo.upsert_document(
        company_id=company.id,
        accession="000078901926000001",
        form="10-Q",
        document_kind="10-Q",
        period_end=date(2026, 3, 31),
        filed_at=date(2026, 4, 29),
        source_url="https://example.invalid/msft.htm",
        extraction_version="documents-1",
        extraction_status="ok",
    )
    assert created is True

    again, created2 = repo.upsert_document(
        company_id=company.id,
        accession="000078901926000001",
        form="10-Q",
        document_kind="10-Q",
        source_url="https://example.invalid/msft.htm",
        extraction_status="failed",
    )
    assert created2 is False
    assert again.id == doc.id
    assert again.extraction_status == "failed"

    # Same accession/kind but a different exhibit URL is a different document.
    exhibit, created3 = repo.upsert_document(
        company_id=company.id,
        accession="000078901926000001",
        form="8-K",
        document_kind="8k_earnings_release",
        source_url="https://example.invalid/ex991.htm",
    )
    assert created3 is True
    assert exhibit.id != doc.id


def test_sections_replace_lookup_and_offsets(session: Session) -> None:
    repo = DocumentsRepo(session)
    company = repo.get_or_create_company("AAPL", cik="0000320193")
    doc, _ = repo.upsert_document(
        company_id=company.id,
        accession="000032019326000009",
        form="10-Q",
        document_kind="10-Q",
    )
    text = "Heading one\n\nRisk factors body text.\n\nClosing remarks."
    rows = repo.replace_sections(
        doc.id,
        [
            SectionInput("mda", "Item 2. MD&A", text[0:11], 0, 11),
            SectionInput("risk_factors", "Item 1A. Risk Factors", text[13:37], 13, 37),
        ],
    )
    assert len(rows) == 2

    assert [s.section_type for s in repo.list_sections(doc.id)] == ["mda", "risk_factors"]

    containing = repo.find_section_containing(doc.id, 20)
    assert containing is not None and containing.section_type == "risk_factors"
    assert repo.find_section_containing(doc.id, 12) is None  # gap between sections

    span = repo.resolve_span(doc.id, 13, 37)
    assert span is not None and span.section_type == "risk_factors"
    assert repo.resolve_span(doc.id, 0, 40) is None  # not fully inside one section

    # Re-extraction replaces sections instead of duplicating them.
    repo.replace_sections(doc.id, [SectionInput("other", "Reset", "only", 0, 4)])
    assert len(repo.list_sections(doc.id)) == 1


def test_find_document_by_content_hash(session: Session) -> None:
    repo = DocumentsRepo(session)
    company = repo.get_or_create_company("AAPL", cik="0000320193")
    artifact = repo.add_source_artifact(
        source="sec",
        source_url="https://example.invalid/x.htm",
        content_hash="ab" * 32,
    )
    doc, _ = repo.upsert_document(
        company_id=company.id,
        accession="000032019326000011",
        form="8-K",
        document_kind="8k_earnings_release",
        source_url="https://example.invalid/x.htm",
        source_artifact_id=artifact.id,
    )
    found = repo.find_document_by_content(
        company_id=company.id,
        accession="000032019326000011",
        document_kind="8k_earnings_release",
        content_hash="ab" * 32,
    )
    assert found is not None and found.id == doc.id
    assert (
        repo.find_document_by_content(
            company_id=company.id,
            accession="000032019326000011",
            document_kind="8k_earnings_release",
            content_hash="cd" * 32,
        )
        is None
    )


def test_extraction_report_queries(session: Session) -> None:
    repo = DocumentsRepo(session)
    company = repo.get_or_create_company("COST", cik="0000909832")
    ok_doc, _ = repo.upsert_document(
        company_id=company.id,
        accession="000090983226000001",
        form="10-Q",
        document_kind="10-Q",
        extraction_status="ok",
    )
    repo.replace_sections(ok_doc.id, [SectionInput("mda", "Item 7.", "text", 0, 4)])
    ocr_doc, _ = repo.upsert_document(
        company_id=company.id,
        accession=None,
        form=None,
        document_kind="pdf",
        extraction_status="needs_ocr",
    )

    assert repo.count_documents_by_status() == {"ok": 1, "needs_ocr": 1}
    assert repo.count_documents_by_form() == {"10-Q": 1, "none": 1}
    assert repo.count_sections_by_type() == {"mda": 1}
    without = repo.documents_without_sections()
    assert [d.id for d in without] == [ocr_doc.id]

    listed = repo.list_documents(company_id=company.id, document_kind="pdf")
    assert [d.id for d in listed] == [ocr_doc.id]
