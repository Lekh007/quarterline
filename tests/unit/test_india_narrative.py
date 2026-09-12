"""India narrative ingestion + India retrieval tests (IND-7, offline).

The narrative corpus files are browser-acquired company-IR PDFs cached under
the gitignored ``storage/raw/india/`` tree (SPEC 2.4.5 posture, IND-1 layout).
Following the IND-6 test convention, tests that need the cached PDFs skip
cleanly when the cache is absent; the ingestion run that produced the measured
results in docs/india_evaluation.md verified them against the live cache.

Everything here runs OFFLINE: indexes and searches use the deterministic
(NON-PRODUCTION) FakeEmbeddingProvider; the real nomic index build is a
runtime verification step (scripts/eval_india.py), never a test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from india_test_helpers import create_schema
from sqlalchemy import select

from quarterline.retrieve.embeddings import FakeEmbeddingProvider
from quarterline.retrieve.models import SearchQuery
from quarterline.retrieve.search import SearchService, build_index
from quarterline.sources.india.narrative import (
    KIND_EARNINGS_PRESENTATION,
    KIND_FINANCIAL_RESULTS,
    KIND_MANAGEMENT_TRANSCRIPT,
    KIND_RESULTS_NOTES,
    ingest_narratives,
    ingest_reviewed_tcs_cash_flow,
    is_management_commentary,
    narrative_entries,
    narrative_metadata,
)
from quarterline.store.db import session_scope
from quarterline.store.models import Document, FactObservation, Section, SourceArtifact
from quarterline.store.repositories.search_sqlite import SearchIndexRepo

REPO_ROOT = Path(__file__).resolve().parents[2]


def cache_present() -> bool:
    return all((REPO_ROOT / entry["path"]).is_file() for entry in narrative_entries())


pytestmark = pytest.mark.skipif(
    not cache_present(),
    reason="India narrative storage cache absent (gitignored); the IND-7 "
    "ingestion run verified against the live cache",
)


@pytest.fixture
def narrative_store(offline_env):
    """Offline store with the schema created and the narrative corpus ingested."""
    create_schema()
    report = ingest_narratives()
    assert report.documents_failed == 0, report.errors
    return offline_env, report


def _document_by_file(session, file_name: str) -> Document:
    row = session.scalars(
        select(Document)
        .join(SourceArtifact, SourceArtifact.id == Document.source_artifact_id)
        .where(SourceArtifact.local_path.like(f"%{file_name}"))
    ).first()
    assert row is not None, f"no document for {file_name}"
    return row


# ---------------------------------------------------------------------------
# Registration, per-page sections, honesty
# ---------------------------------------------------------------------------


class TestNarrativeIngestion:
    def test_all_cached_narrative_files_registered(self, narrative_store):
        _env, report = narrative_store
        expected = len(narrative_entries())
        assert report.documents_ingested == expected
        # All 10 issuers are covered (press releases + presentations at minimum).
        assert len(report.per_issuer) == 10
        assert set(report.by_kind) == {
            KIND_FINANCIAL_RESULTS,
            KIND_RESULTS_NOTES,
            KIND_MANAGEMENT_TRANSCRIPT,
            KIND_EARNINGS_PRESENTATION,
        }

    def test_ingestion_is_idempotent(self, narrative_store):
        _env, first = narrative_store
        second = ingest_narratives()
        assert second.documents_ingested == 0
        assert second.documents_skipped == first.documents_ingested
        assert second.documents_failed == 0

    def test_one_section_per_page_with_page_numbers(self, narrative_store):
        with session_scope() as session:
            document = _document_by_file(session, "consol-fy27-q1-finstatement.pdf")
            sections = list(
                session.scalars(
                    select(Section)
                    .where(Section.document_id == document.id)
                    .order_by(Section.page_start)
                )
            )
            assert len(sections) >= 30  # the condensed statement is a 35-page PDF
            for number, section in enumerate(sections, start=1):
                assert section.page_start == number
                assert section.page_end == number
                assert section.section_type == KIND_FINANCIAL_RESULTS
                assert section.heading == f"Page {number}"
            # offsets are contiguous in the assembled document text
            # (pdf_extract joins pages with a single "\n" separator)
            cursor = 0
            for number, section in enumerate(sections, start=1):
                if number > 1:
                    cursor += 1  # the page separator
                assert section.start_offset == cursor
                cursor = section.end_offset

    def test_management_commentary_labeling_carried_on_the_document_row(self, narrative_store):
        with session_scope() as session:
            press = _document_by_file(session, "infosys-q1fy27-ifrs-inr-press-release.pdf")
            press_meta = narrative_metadata(press)
            assert press.document_kind == KIND_RESULTS_NOTES
            assert press_meta["management_commentary"] is True
            assert press_meta["source_doc_type"] == "press_release"
            assert press_meta["source_tier"] == "company_ir"
            assert is_management_commentary(press.document_kind)

            transcript = _document_by_file(session, "hul-jq26-earnings-call-transcript.pdf")
            assert transcript.document_kind == KIND_MANAGEMENT_TRANSCRIPT
            assert narrative_metadata(transcript)["management_commentary"] is True

            presentation = _document_by_file(session, "hul-jq26-results-presentation.pdf")
            assert presentation.document_kind == KIND_EARNINGS_PRESENTATION
            assert narrative_metadata(presentation)["management_commentary"] is True

            # condensed financial statements are company-prepared, NOT commentary,
            # and always carry the company_ir tier (source-tier laundering guard)
            condensed = _document_by_file(session, "consol-fy27-q1-finstatement.pdf")
            condensed_meta = narrative_metadata(condensed)
            assert condensed.document_kind == KIND_FINANCIAL_RESULTS
            assert condensed_meta["management_commentary"] is False
            assert condensed_meta["source_tier"] == "company_ir"

    def test_issuer_company_link_period_dates_and_source_url(self, narrative_store):
        with session_scope() as session:
            press = _document_by_file(session, "infosys-q1fy27-ifrs-inr-press-release.pdf")
            meta = narrative_metadata(press)
            assert meta["issuer_id"] == "IN-INFY"
            assert meta["period_key"] == "q1_fy2026-27"
            assert meta["period_start"] == "2026-04-01"
            assert meta["period_end"] == "2026-06-30"
            assert press.source_url.startswith("https://www.infosys.com/")
            # filed_at from the manifest's own consolidated broadcast date
            assert press.filed_at is not None

    def test_empty_pages_keep_low_confidence_with_a_note(self, narrative_store):
        with session_scope() as session:
            empties = list(
                session.scalars(select(Section).where(Section.confidence == 0.0).limit(5))
            )
            assert empties, "expected at least one image-only page in the corpus"
            for section in empties:
                assert section.text is not None and len(section.text.strip()) < 25
                assert section.notes is not None
                assert "scanned" in section.notes or "image" in section.notes

    def test_garbled_pages_keep_low_confidence_with_a_note(self):
        """Replacement-glyph corruption is honest degradation, never a clean label."""
        from quarterline.sources.india.narrative import _page_confidence

        garbled = ("Net profit \ufffd\ufffd\ufffd for the quarter " * 8) + "\ufffd" * 60
        confidence, note = _page_confidence(garbled)
        assert confidence < 1.0
        assert note is not None and "garbled" in note
        # clean financial-statement-like text stays 1.0 (no crying wolf)
        clean = "Revenue from operations 48,211 exception 9,330 (1,289) 41,284 " * 4
        assert _page_confidence(clean) == (1.0, None)

    def test_manifest_hash_drift_refuses_ingestion(self, offline_env, tmp_path):
        """Bytes that no longer match the manifest are an error, never ingested."""
        create_schema()
        entries = narrative_entries()
        entry = entries[0]
        real_file = REPO_ROOT / entry["path"]
        assert real_file.is_file()
        tampered = dict(entry)
        tampered["sha256"] = "0" * 64
        tampered_manifest = tmp_path / "manifest.json"
        # reuse the real manifest structure with one drifted entry
        full = json.loads((REPO_ROOT / "tests/fixtures/india/manifest.json").read_text("utf-8"))
        for cached in full["storage_cache_only"]:
            if cached["path"] == entry["path"]:
                cached["sha256"] = "0" * 64
        tampered_manifest.write_text(json.dumps(full), encoding="utf-8")
        report = ingest_narratives(manifest_path=tampered_manifest)
        assert report.documents_failed >= 1
        assert any("does not match the manifest record" in e for e in report.errors)
        assert real_file.is_file()  # nothing replaced


# ---------------------------------------------------------------------------
# TCS reported quarterly cash flow (IND-6 open item closed by IND-7)
# ---------------------------------------------------------------------------


class TestTcsQuarterlyCashFlow:
    def test_tcs_cf_observation_present_with_full_provenance(self, narrative_store):
        report = ingest_reviewed_tcs_cash_flow()
        assert report.observations_inserted == 1
        with session_scope() as session:
            rows = list(
                session.scalars(
                    select(FactObservation).where(
                        FactObservation.original_tag
                        == "NetCashFlowsGeneratedFromOperatingActivitiesRenderedLine"
                    )
                )
            )
            assert len(rows) == 1
            obs = rows[0]
            assert obs.canonical_concept == "cash_flow_operations"
            assert obs.form == "PDF"
            assert str(obs.value_decimal) == "121710000000"
            assert obs.unit == "INR"
            assert obs.period_kind == "quarter"
            assert str(obs.period_start) == "2026-04-01"
            assert str(obs.period_end) == "2026-06-30"
            metadata = json.loads(obs.context_metadata_json)
            assert metadata["extraction_method"] == "pdf_text"
            assert metadata["page"] == "PDF p.6 (Consolidated Interim Statement of Cash Flows)"
            assert metadata["display_value"] == "12,171"
            assert metadata["prior_year_display_value"] == "11,919"
            assert metadata["review_status"] == "agent_checked_against_document"
            assert metadata["reporting_scope"] == "consolidated"

    def test_tcs_cf_ingestion_is_idempotent(self, narrative_store):
        first = ingest_reviewed_tcs_cash_flow()
        second = ingest_reviewed_tcs_cash_flow()
        assert first.observations_inserted + first.observations_skipped == 1
        assert second.observations_inserted == 0
        assert second.observations_skipped == 1

    def test_prior_year_corroboration_requires_order(self):
        from quarterline.sources.india.narrative import _verify_prior_year_in_row_window

        # current value missing from the window -> refuse
        with pytest.raises(ValueError, match="refusing to ingest"):
            _verify_prior_year_in_row_window(
                page_text="Net cash flows generated from operating activities\n  12,171",
                matched_line="Net cash flows generated from operating activities",
                display="12,171",
                prior_display="11,919",
            )
        # prior-year column BEFORE the current column -> refuse (column order)
        with pytest.raises(ValueError, match="refusing to ingest"):
            _verify_prior_year_in_row_window(
                page_text=("Net cash flows generated from operating activities\n  11,919   12,171"),
                matched_line="Net cash flows generated from operating activities",
                display="12,171",
                prior_display="11,919",
            )
        # correct order -> passes
        _verify_prior_year_in_row_window(
            page_text=("Net cash flows generated from operating activities\n  12,171   11,919"),
            matched_line="Net cash flows generated from operating activities",
            display="12,171",
            prior_display="11,919",
        )


# ---------------------------------------------------------------------------
# India retrieval: company filter isolation + evidence id roundtrip (offline)
# ---------------------------------------------------------------------------


def _ticker_of(session, document_id: int) -> str:
    from quarterline.store.models import Company

    return session.execute(
        select(Company.ticker)
        .join(Document, Document.company_id == Company.id)
        .where(Document.id == document_id)
    ).scalar_one()


class TestIndiaRetrieval:
    @pytest.fixture
    def indexed_india_store(self, narrative_store):
        _env, _report = narrative_store
        provider = FakeEmbeddingProvider()
        with session_scope() as session:
            build_index(session, "fixed", provider)
            build_index(session, "section", provider)
        return provider

    def test_company_filter_returns_only_that_issuer(self, indexed_india_store):
        provider = indexed_india_store
        cases = [
            ("INFY", "management commentary on margin guidance"),
            ("HINDUNILVR", "underlying sales growth highest growth in 13 quarters"),
            ("TCS", "operating cash flows generated from operating activities"),
            ("ITC", "revenue from operations for the quarter"),
        ]
        with session_scope() as session:
            service = SearchService(session, provider)
            for ticker, query in cases:
                result = service.search(
                    SearchQuery(
                        query=query, ticker=ticker, strategy="section", retrieval="hybrid", top_k=5
                    )
                )
                assert result.items, f"{ticker}: expected evidence for {query!r}"
                for item in result.items:
                    assert _ticker_of(session, item.document_id) == ticker, (
                        f"wrong-company leak: {ticker} query returned a document of "
                        f"{_ticker_of(session, item.document_id)}"
                    )

    def test_question_about_issuer_a_never_returns_issuer_b(self, indexed_india_store):
        """The wrong-company isolation test: ask about HUL's consumer-staples
        quarter under the INFY filter (and vice versa): zero cross-issuer units."""
        provider = indexed_india_store
        with session_scope() as session:
            service = SearchService(session, provider)
            pairs = [
                ("INFY", "HINDUNILVR", "turnover and underlying sales growth in the June quarter"),
                ("HINDUNILVR", "INFY", "guidance for revenue growth in constant currency"),
            ]
            for asker, other, query in pairs:
                result = service.search(
                    SearchQuery(
                        query=query, ticker=asker, strategy="fixed", retrieval="hybrid", top_k=10
                    )
                )
                assert result.items
                for item in result.items:
                    assert _ticker_of(session, item.document_id) == asker, (
                        f"{asker!r} query surfaced {other!r} material: "
                        f"{_ticker_of(session, item.document_id)}"
                    )

    def test_evidence_id_roundtrip_resolves_chunks(self, indexed_india_store):
        provider = indexed_india_store
        with session_scope() as session:
            service = SearchService(session, provider)
            result = service.search(
                SearchQuery(
                    query="net cash generated by operating activities",
                    ticker="INFY",
                    strategy="section",
                    retrieval="hybrid",
                    top_k=5,
                )
            )
            assert result.items
            repo = SearchIndexRepo(session)
            for item in result.items:
                chunk = repo.get_chunk_by_evidence_id(item.evidence_id)
                assert chunk is not None, f"evidence id {item.evidence_id} unresolved"
                assert chunk.id == item.chunk_id

    def test_india_chunks_carry_page_numbers(self, indexed_india_store):
        provider = indexed_india_store
        with session_scope() as session:
            service = SearchService(session, provider)
            result = service.search(
                SearchQuery(
                    query="revenue from operations consolidated",
                    ticker="TCS",
                    strategy="fixed",
                    retrieval="hybrid",
                    top_k=5,
                )
            )
            assert result.items
            assert all(item.page is not None for item in result.items), (
                "page-level citations are the India requirement: chunks must carry pages"
            )
