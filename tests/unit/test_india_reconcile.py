"""Reconciliation output tests (IND-2): table rows + fixture-derived CSV."""

from __future__ import annotations

import csv

from india_test_helpers import INDIA_FIXTURES_DIR

from quarterline.sources.india.reconcile import (
    REVIEW_RECONCILED,
    render_table,
    rows_from_artifacts,
    rows_from_fixture_dir,
    write_reconciliation_csv,
)


class TestRowsFromFixtures:
    def test_rows_generated_for_both_issuers_and_periods(self):
        rows = rows_from_fixture_dir(INDIA_FIXTURES_DIR)
        documents = {row.document for row in rows}
        assert len(documents) == 4
        issuers = {row.issuer_id for row in rows}
        assert issuers == {"IN-INFY", "IN-HINDUNILVR"}

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

    def test_review_status_non_empty_and_reconciled(self):
        rows = rows_from_fixture_dir(INDIA_FIXTURES_DIR)
        assert rows
        for row in rows:
            assert row.review_status == REVIEW_RECONCILED  # parsed straight from instances
            assert row.review_status  # non-empty by construction

    def test_reported_values_carry_declared_scale(self):
        rows = rows_from_fixture_dir(INDIA_FIXTURES_DIR)
        infy_q1_revenue = next(
            r
            for r in rows
            if r.document.startswith("INFY-Q1FY27") and r.concept == "revenue_from_operations"
        )
        assert infy_q1_revenue.reported_value == "₹48,211 Cr @ Crores"
        assert infy_q1_revenue.normalized_value == "482110000000"

    def test_eps_row_is_per_share(self):
        rows = rows_from_fixture_dir(INDIA_FIXTURES_DIR)
        eps_row = next(
            r for r in rows if r.document.startswith("INFY-Q1FY27") and r.concept == "eps_basic"
        )
        assert "(per share)" in eps_row.reported_value
        assert eps_row.normalized_value == "19.19"


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
        import pytest

        with pytest.raises(LookupError, match="ingest india-document"):
            rows_from_artifacts("IN-INFY")


class TestCsv:
    def test_csv_written_with_required_columns(self, tmp_path):
        target = tmp_path / "india_reconciliation.csv"
        written = write_reconciliation_csv(path=target, fixtures_dir=INDIA_FIXTURES_DIR)
        assert written == target
        with target.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
        assert rows
        assert reader.fieldnames == [
            "issuer",
            "document",
            "page_or_tag",
            "concept",
            "period",
            "scope",
            "reported_value",
            "normalized_value",
            "review_status",
        ]
        for row in rows:
            assert row["review_status"]
            assert row["review_status"] == REVIEW_RECONCILED
            assert row["issuer"] in ("IN-INFY", "IN-HINDUNILVR")
            assert row["scope"] == "consolidated"

    def test_repo_default_csv_exists_and_matches(self, tmp_path):
        """The committed data/india_reconciliation.csv is regenerable and current."""
        from quarterline.sources.india.reconcile import DEFAULT_CSV_PATH

        assert DEFAULT_CSV_PATH.is_file(), "run write_reconciliation_csv() to (re)generate"
        fresh = tmp_path / "fresh.csv"
        write_reconciliation_csv(path=fresh, fixtures_dir=INDIA_FIXTURES_DIR)
        assert fresh.read_text(encoding="utf-8") == DEFAULT_CSV_PATH.read_text(encoding="utf-8")
