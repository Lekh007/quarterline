"""Manual document import tests (IND-2): verify, hash, cache, register, company upsert."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from india_test_helpers import INDIA_FIXTURES_DIR, INFY_Q1, import_all_fixtures
from sqlalchemy import select

from quarterline.sources.india.bse import fetch_not_available_in_runtime as bse_fetch
from quarterline.sources.india.errors import ManualImportRequired
from quarterline.sources.india.ir_documents import (
    SOURCE_INDIA,
    import_document,
    read_filing_sidecar,
)
from quarterline.sources.india.nse import fetch_not_available_in_runtime as nse_fetch
from quarterline.store.db import session_scope
from quarterline.store.models import Company, SourceArtifact
from quarterline.store.repositories.companies import CompaniesRepo


class TestImportDocument:
    def test_import_fixture_reports_parse(self, india_imported):
        _, reports = india_imported
        assert len(reports) == 4
        infy_q1 = reports[0]
        assert infy_q1.issuer_id == "IN-INFY"
        assert infy_q1.parsed is True
        assert infy_q1.format == "xbrl"
        assert infy_q1.fact_count == 171
        assert infy_q1.taxonomy_version == "V2.1 (26-06-2026)"
        assert infy_q1.company_ticker == "INFY"

    def test_sha256_and_cached_path(self, india_imported):
        _, reports = india_imported
        infy_q1 = reports[0]
        # sha256 matches the manifest's recorded hash for the same bytes.
        manifest_hash = "5928e8ca820f60f944a66367291a7dc76c8df4536dfaf5627f3d3ff077487bae"
        assert infy_q1.content_hash == manifest_hash
        assert f"india/{infy_q1.issuer_slug}/q1_fy2026-27/" in infy_q1.local_path.replace("\\", "/")
        assert infy_q1.size_bytes == 53642

    def test_source_artifact_rows_written(self, india_imported):
        _store, _reports = india_imported
        with session_scope() as session:
            artifacts = list(
                session.scalars(select(SourceArtifact).where(SourceArtifact.source == SOURCE_INDIA))
            )
            assert len(artifacts) == 4
            first = artifacts[0]
            assert first.content_type == "xbrl"
            assert first.parser_version == "india-xbrl-1"
            assert first.content_hash is not None

    def test_company_upserted_inr_india(self, india_imported):
        _, _ = india_imported
        with session_scope() as session:
            infy = CompaniesRepo(session).get_by_ticker("INFY")
            assert infy is not None
            assert infy.country == "IN"
            assert infy.reporting_currency == "INR"
            assert infy.name == "Infosys Limited"
            # cik column (String(10), too short for a 12-char ISIN) carries the
            # BSE scrip code — the XBRL entity identifier.
            assert infy.cik == "500209"

    def test_filing_sidecar_round_trip(self, india_imported):
        _, reports = india_imported
        infy_q1 = reports[0]
        meta = read_filing_sidecar(Path(infy_q1.local_path))
        assert meta is not None
        assert meta.issuer_id == "IN-INFY"
        assert meta.scope == "consolidated"
        assert meta.period_end == date(2026, 6, 30)
        assert meta.published_at == date(2026, 7, 23)
        assert meta.revision_status == "Original"
        assert meta.seq_id == "177385"

    def test_scope_mismatch_rejected(self, india_store):
        with pytest.raises(ValueError, match="scope mismatch"):
            import_document(
                issuer_id="IN-INFY",
                path=INDIA_FIXTURES_DIR / INFY_Q1,
                doc_type="financial_results",
                period_start=date(2026, 4, 1),
                period_end=date(2026, 6, 30),
                scope="standalone",  # instance declares Consolidated
                published_at=date(2026, 7, 23),
            )

    def test_unverified_issuer_refused(self, india_store):
        with pytest.raises(ValueError, match="not verified"):
            import_document(
                issuer_id="IN-TCS",
                path=INDIA_FIXTURES_DIR / INFY_Q1,
                doc_type="financial_results",
                period_start=date(2026, 4, 1),
                period_end=date(2026, 6, 30),
                scope="consolidated",
                published_at=date(2026, 7, 23),
            )

    def test_missing_file_refused(self, india_store):
        with pytest.raises(FileNotFoundError):
            import_document(
                issuer_id="IN-INFY",
                path=INDIA_FIXTURES_DIR / "does-not-exist.xml",
                doc_type="financial_results",
                period_start=date(2026, 4, 1),
                period_end=date(2026, 6, 30),
                scope="consolidated",
                published_at=date(2026, 7, 23),
            )

    def test_reimport_is_idempotent_on_cache(self, india_store):
        first = import_all_fixtures()
        second = import_all_fixtures()
        assert [r.content_hash for r in first] == [r.content_hash for r in second]
        assert first[0].local_path == second[0].local_path

    def test_import_requires_company_created_before_pipeline(self, india_imported):
        _, _ = india_imported
        with session_scope() as session:
            companies = list(session.scalars(select(Company)).all())
            assert {c.ticker for c in companies} >= {"INFY", "HINDUNILVR"}


class TestExchangeStubs:
    def test_nse_raises_manual_import_required(self):
        with pytest.raises(ManualImportRequired, match="ingest india-document"):
            nse_fetch()

    def test_bse_raises_manual_import_required(self):
        with pytest.raises(ManualImportRequired, match="unassessed"):
            bse_fetch()

    def test_stubs_do_no_network_io(self):
        # Documented stubs: constants only + a raising helper; nothing callable
        # performs I/O in this milestone.
        from quarterline.sources.india import bse, nse

        assert nse.NSE_INTEGRATED_FILING_API.startswith("https://www.nseindia.com/")
        assert nse.NSE_ARCHIVE_BASE_URL.startswith("https://nsearchives.nseindia.com")
        assert bse.BSE_BASE_URL.startswith("https://www.bseindia.com")
        assert nse.MIN_EXCHANGE_REQUEST_INTERVAL_SECONDS >= 1.0
