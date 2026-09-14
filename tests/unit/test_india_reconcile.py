"""Reconciliation output tests (IND-2 CLI table; IND-3 validation CSV contract)."""

from __future__ import annotations

import csv
from decimal import Decimal

import pytest
from india_test_helpers import INDIA_FIXTURES_DIR

from quarterline.sources.india.reconcile import (
    COMPARISON_MATCHED,
    COMPARISON_SCOPE_BASIS,
    COMPARISON_SCRAMBLED,
    COMPARISON_UNAVAILABLE,
    DEFAULT_VALIDATION_CSV_PATH,
    REVIEW_AGENT_CHECKED,
    REVIEW_HUMAN_PENDING,
    render_table,
    rows_from_artifacts,
    rows_from_fixture_dir,
    write_supersession_note,
    write_validation_csv,
)


class TestLegacyRowsFromFixtures:
    def test_rows_generated_for_all_issuers_and_periods(self):
        rows = rows_from_fixture_dir(INDIA_FIXTURES_DIR)
        documents = {row.document for row in rows}
        assert len(documents) == 20  # 2 periods x 10 issuers (IND-6 corpus)
        issuers = {row.issuer_id for row in rows}
        assert len(issuers) == 10
        assert {"IN-INFY", "IN-HINDUNILVR"} <= issuers

    def test_expected_core_rows_present(self):
        rows = rows_from_fixture_dir(INDIA_FIXTURES_DIR)
        infy_q1 = [r for r in rows if r.document.startswith("INFY-Q1FY27")]
        concepts = {r.concept for r in infy_q1}
        assert {
            "revenue_from_operations",
            "profit_before_tax",
            "profit_after_tax",
            "eps_basic",
            "eps_diluted",
        } <= concepts

    def test_reported_values_carry_declared_scale(self):
        rows = rows_from_fixture_dir(INDIA_FIXTURES_DIR)
        infy_q1_revenue = next(
            r
            for r in rows
            if r.document.startswith("INFY-Q1FY27") and r.concept == "revenue_from_operations"
        )
        assert infy_q1_revenue.reported_value == "₹48,211 Cr @ Crores"
        assert infy_q1_revenue.normalized_value == "482110000000"


class TestRenderTable:
    def test_table_renders_every_row(self):
        rows = rows_from_fixture_dir(INDIA_FIXTURES_DIR)
        table = render_table(rows)
        lines = table.strip().splitlines()
        assert str(len(rows)) in lines[-1]
        for row in rows:
            assert row.concept in table
            assert row.normalized_value in table


class TestRowsFromArtifacts:
    def test_artifact_backed_rows_match_fixture_rows(self, india_imported):
        _, _ = india_imported
        infy_rows = rows_from_artifacts("IN-INFY")
        fixture_rows = [
            r for r in rows_from_fixture_dir(INDIA_FIXTURES_DIR) if r.issuer_id == "IN-INFY"
        ]
        assert len(infy_rows) == len(fixture_rows)
        assert {(r.concept, r.normalized_value) for r in infy_rows} == {
            (r.concept, r.normalized_value) for r in fixture_rows
        }

    def test_issuer_without_artifacts_raises_helpful_error(self, india_store):
        with pytest.raises(LookupError, match="ingest india-document"):
            rows_from_artifacts("IN-INFY")


class TestValidationCsv:
    """The IND-3 contract: data/validation/india_reconciliation.csv (25 columns)."""

    @pytest.fixture(autouse=True)
    def _rows(self, tmp_path):
        target = tmp_path / "india_reconciliation.csv"
        written = write_validation_csv(path=target)
        with written.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            self.rows = list(reader)
            self.fieldnames = reader.fieldnames

    def test_exact_contract_columns(self):
        assert self.fieldnames == [
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
        ]

    def test_covers_all_ten_issuers_all_ten_concepts(self):
        from india_test_helpers import ALL_ISSUER_IDS

        assert {r["issuer_id"] for r in self.rows} == set(ALL_ISSUER_IDS)
        assert {r["concept"] for r in self.rows} == {
            "revenue_from_operations",
            "total_income",
            "profit_before_tax",
            "profit_after_tax",
            "profit_attributable_to_owners",
            "exceptional_items",
            "eps_basic",
            "eps_diluted",
            "cash_flow_operations",
            "capex",
        }

    def test_every_row_carries_document_identity(self):
        for row in self.rows:
            assert row["document_hash"]
            assert len(row["document_hash"]) == 64
            assert row["source_url"].startswith("https://")
            assert row["filing_identifier"]
            assert row["filed_at"]
            assert row["period_start"] and row["period_end"]
            assert row["fiscal_label"]
            assert row["reporting_scope"] == "consolidated"
            assert row["raw_value"] and row["normalized_value"]

    def test_raw_values_never_rescaled(self):
        """raw and normalized are both preserved; money is exact full rupees."""
        revenue = next(
            r
            for r in self.rows
            if r["issuer_id"] == "IN-INFY"
            and r["original_tag_or_label"] == "RevenueFromOperations"
            and r["period_end"] == "2026-06-30"
        )
        assert revenue["raw_value"] == "482110000000"
        assert revenue["normalized_value"] == "482110000000"
        assert revenue["normalized_unit"] == "INR"
        # displayed crore value reconciles exactly with the declared precision
        assert Decimal(revenue["normalized_value"]) / Decimal(10**7) == Decimal(48211)

    def test_eps_stays_per_share(self):
        eps = next(
            r
            for r in self.rows
            if r["concept"] == "eps_basic"
            and r["period_end"] == "2026-06-30"
            and r["issuer_id"] == "IN-INFY"
        )
        assert eps["normalized_unit"] == "INR/share"
        assert eps["normalized_value"] == "19.19"
        assert "per share" in eps["presentation_scale"]

    def test_quarterly_cfo_row_is_pdf_sourced(self):
        """The INFY Q1 quarterly CFO row exists with full provenance (correction A)."""
        cfo = [
            r
            for r in self.rows
            if r["concept"] == "cash_flow_operations" and r["period_end"] == "2026-06-30"
        ]
        assert len(cfo) == 1
        row = cfo[0]
        assert row["issuer_id"] == "IN-INFY"
        assert row["source_document_id"] == "consol-fy27-q1-finstatement.pdf"
        assert row["raw_value"] == "9,330"
        assert row["normalized_value"] == "93300000000"
        assert row["comparison_status"] == COMPARISON_MATCHED
        assert row["review_status"] == REVIEW_AGENT_CHECKED
        assert "never" in row["review_notes"] or "provenance" in row["review_notes"]

    def test_annual_cfo_rows_are_not_relabelled_as_q4(self):
        """The Q4-filing annual CFO rows keep period_kind 'annual' (FY label), never Q4."""
        for row in self.rows:
            if row["concept"] == "cash_flow_operations" and row["period_end"] == "2026-03-31":
                assert row["period_start"] == "2025-04-01"
                assert "annual" in row["fiscal_label"]

    def test_review_statuses_are_from_the_allowed_vocabulary(self):
        allowed = {REVIEW_AGENT_CHECKED, REVIEW_HUMAN_PENDING, "automated_check_passed"}
        for row in self.rows:
            assert row["review_status"] in allowed
            assert row["review_status"] != "human_approved"  # NEVER recorded by code
            assert row["comparison_status"] in {
                COMPARISON_MATCHED,
                "matched_within_declared_precision",
                COMPARISON_SCOPE_BASIS,
                COMPARISON_SCRAMBLED,
                COMPARISON_UNAVAILABLE,
                "mismatch_flagged_do_not_force",
            }

    def test_unmatched_rendered_comparisons_are_human_review_pending(self):
        for row in self.rows:
            if row["comparison_status"] in (COMPARISON_SCRAMBLED, COMPARISON_UNAVAILABLE):
                assert row["review_status"] == REVIEW_HUMAN_PENDING
                assert row["review_notes"]  # the reason is always recorded

    def test_hul_q1_revenue_tension_resolved_by_visual_inspection(self):
        # 2026-09-12: the P&L page was rendered to an image and read
        # (owner-delegated). The printed statement has no revenue subtotal;
        # 17,341 is the component sum (17,149 + 35 + 157), so the earlier
        # linear-extraction reading (17,149 = sale of products) was the
        # artifact. The stored fact was always correct.
        row = next(
            r
            for r in self.rows
            if r["issuer_id"] == "IN-HINDUNILVR"
            and r["concept"] == "revenue_from_operations"
            and r["period_end"] == "2026-06-30"
        )
        assert row["comparison_status"] == COMPARISON_MATCHED
        assert row["review_status"] == REVIEW_AGENT_CHECKED
        assert "17,341" in row["reference_display_value"]
        assert "17,149" in row["reference_display_value"]
        assert "VISUAL PAGE INSPECTION" in row["review_notes"]

    def test_hul_sign_semantics_recorded(self):
        exceptional = next(
            r
            for r in self.rows
            if r["issuer_id"] == "IN-HINDUNILVR"
            and r["original_tag_or_label"] == "ExceptionalItemsBeforeTax"
            and r["period_end"] == "2026-03-31"
            and r["period_start"] == "2025-04-01"
        )
        assert exceptional["raw_value"] == "-2350000000"
        assert exceptional["comparison_status"] == COMPARISON_MATCHED
        assert "loss of Rs. 235 crores" in exceptional["reference_display_value"]
        assert "SIGN CONVENTION" in exceptional["review_notes"]
        # INFY's rendered statement shows the same tag as a POSITIVE deduction:
        infy_exceptional = next(
            r
            for r in self.rows
            if r["issuer_id"] == "IN-INFY"
            and r["original_tag_or_label"] == "ExceptionalItemsBeforeTax"
            and r["period_start"] == "2025-04-01"
        )
        assert infy_exceptional["raw_value"] == "-12890000000"
        assert infy_exceptional["comparison_status"] == COMPARISON_SCOPE_BASIS
        assert "positive 1,289" in infy_exceptional["review_notes"]

    def test_cash_flow_availability_matrix_rows(self):
        """INFY: quarter (IR PDF) + annual; HUL: annual only, in this corpus."""
        infy_cf = {
            (r["period_start"], r["period_end"])
            for r in self.rows
            if r["issuer_id"] == "IN-INFY" and r["concept"] == "cash_flow_operations"
        }
        assert infy_cf == {("2026-04-01", "2026-06-30"), ("2025-04-01", "2026-03-31")}
        hul_cf = {
            (r["period_start"], r["period_end"])
            for r in self.rows
            if r["issuer_id"] == "IN-HINDUNILVR" and r["concept"] == "cash_flow_operations"
        }
        assert hul_cf == {("2025-04-01", "2026-03-31")}


class TestCommittedArtifacts:
    def test_repo_default_validation_csv_is_current(self, tmp_path):
        """The committed data/validation CSV is regenerable and byte-identical."""
        assert DEFAULT_VALIDATION_CSV_PATH.is_file()
        fresh = tmp_path / "fresh.csv"
        write_validation_csv(path=fresh)
        assert fresh.read_text(encoding="utf-8") == DEFAULT_VALIDATION_CSV_PATH.read_text(
            encoding="utf-8"
        )

    def test_legacy_csv_holds_supersession_note(self):
        """The IND-2 data/india_reconciliation.csv is superseded, not silently deleted."""
        from quarterline.sources.india.reconcile import SUPERSEDED_CSV_PATH

        assert SUPERSEDED_CSV_PATH.is_file()
        with SUPERSEDED_CSV_PATH.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        assert len(rows) == 1
        assert rows[0]["superseded_by"] == "data/validation/india_reconciliation.csv"
        assert "IND-3" in rows[0]["note"]

    def test_write_supersession_note_is_idempotent(self, tmp_path):
        target = tmp_path / "old.csv"
        first = write_supersession_note(target)
        second = write_supersession_note(target)
        assert first == second == target
        assert "IND-3" in target.read_text(encoding="utf-8")
