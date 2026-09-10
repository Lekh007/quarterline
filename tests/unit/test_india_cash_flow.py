"""India cash-flow policy tests (IND-3, reviewer correction A).

Covers the corrected cash-flow contract:

- reported observations are accepted at their ACTUAL reported duration
  (quarter/half_year/nine_month_ytd/annual/other_duration) — the IND-2
  "annual instances only" restriction is gone;
- the missing-data statuses are distinct and never zero;
- derivation is subtraction-only with compatibility checks (annual − H1 is
  labeled H2, never Q4), and fabrication (annual/4, half/2) has no code path;
- text-level extraction from company-IR PDFs with page provenance, where table
  ambiguity is ``requires_manual_review`` — including live re-verification of
  the recorded evidence when the real cached PDFs are present.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from quarterline.sources.india.cash_flow import (
    DURATION_ANNUAL,
    DURATION_HALF_YEAR,
    DURATION_NINE_MONTH_YTD,
    DURATION_OTHER,
    DURATION_QUARTER,
    REPORTED_CF_DURATIONS,
    IncompatibleDerivation,
    ReportedCashFlow,
    cash_flow_availability,
    derive_by_subtraction,
    derived_interval_label,
    reported_from_observation_rows,
    reporting_duration,
)
from quarterline.sources.india.data_status import (
    MISSING_DATA_STATUSES,
    MissingDataStatus,
    require_status,
)
from quarterline.sources.india.pdf_results import (
    STATUS_EXTRACTED,
    STATUS_MANUAL_REVIEW,
    extract_cash_flow_statement,
)
from quarterline.sources.india.reconcile import REVIEWED_PDF_CASH_FLOW

# -- missing-data statuses ---------------------------------------------------


class TestMissingDataStatus:
    def test_five_distinct_statuses(self):
        assert MISSING_DATA_STATUSES == {
            "not_present_in_ingested_sources",
            "source_not_ingested",
            "extraction_failed",
            "requires_manual_review",
            "not_applicable",
        }
        assert len(MissingDataStatus) == 5

    def test_status_is_never_zero(self):
        # The whole point of correction A: absence is a status, not a 0.
        for status in MissingDataStatus:
            assert status.value != "0"
            assert status.value != "zero"

    def test_require_status_rejects_unknown(self):
        assert (
            require_status("not_present_in_ingested_sources")
            is MissingDataStatus.NOT_PRESENT_IN_INGESTED_SOURCES
        )
        with pytest.raises(ValueError, match="unknown missing-data status"):
            require_status("has_no_quarterly_cash_flow")


# -- reporting-duration vocabulary -------------------------------------------


class TestReportingDuration:
    def test_standard_durations(self):
        assert reporting_duration(date(2026, 4, 1), date(2026, 6, 30)) == DURATION_QUARTER
        assert reporting_duration(date(2026, 4, 1), date(2026, 9, 30)) == DURATION_HALF_YEAR
        assert reporting_duration(date(2026, 4, 1), date(2026, 12, 31)) == DURATION_NINE_MONTH_YTD
        assert reporting_duration(date(2025, 4, 1), date(2026, 3, 31)) == DURATION_ANNUAL

    def test_non_fy_twelve_month_is_other_duration(self):
        assert reporting_duration(date(2025, 1, 1), date(2025, 12, 31)) == DURATION_OTHER

    def test_exact_dates_stored_regardless_of_label(self):
        # An unusual span still maps to a known vocabulary entry (other_duration)
        # and keeps its exact boundaries — never silently re-bucketed.
        assert reporting_duration(date(2026, 5, 1), date(2026, 6, 30)) == DURATION_OTHER
        assert reporting_duration(date(2024, 4, 1), date(2026, 3, 31)) == DURATION_OTHER
        assert REPORTED_CF_DURATIONS == {
            "quarter",
            "half_year",
            "nine_month_ytd",
            "annual",
            "other_duration",
        }

    def test_inverted_period_raises(self):
        with pytest.raises(ValueError):
            reporting_duration(date(2026, 6, 30), date(2026, 4, 1))


# -- derivation (subtraction-only, compatibility-checked) --------------------


def _cf(
    start: date,
    end: date,
    value: str,
    *,
    concept: str = "cash_flow_operations",
    unit: str = "INR",
    scope: str = "consolidated",
    revision: str | None = "Original",
) -> ReportedCashFlow:
    return ReportedCashFlow(
        concept=concept,
        value=Decimal(value),
        unit=unit,
        scope=scope,
        period_start=start,
        period_end=end,
        source_document_id="SYNTHETIC",
        revision_status=revision,
    )


class TestDeriveBySubtraction:
    def test_annual_minus_h1_is_labeled_h2_never_q4(self):
        annual = _cf(date(2025, 4, 1), date(2026, 3, 31), "339860000000")
        h1 = _cf(date(2025, 4, 1), date(2025, 9, 30), "160000000000")
        h2 = derive_by_subtraction(annual, h1)
        assert h2.value == Decimal(179860000000)
        assert (h2.period_start, h2.period_end) == (date(2025, 10, 1), date(2026, 3, 31))
        assert derived_interval_label(h2) == "H2"

    def test_nine_month_minus_h1_is_q3(self):
        nine = _cf(date(2025, 4, 1), date(2025, 12, 31), "250000000000")
        h1 = _cf(date(2025, 4, 1), date(2025, 9, 30), "160000000000")
        q3 = derive_by_subtraction(nine, h1)
        assert q3.value == Decimal(90000000000)
        assert (q3.period_start, q3.period_end) == (date(2025, 10, 1), date(2025, 12, 31))
        assert derived_interval_label(q3) == "Q3"

    def test_annual_minus_nine_month_is_q4(self):
        annual = _cf(date(2025, 4, 1), date(2026, 3, 31), "339860000000")
        nine = _cf(date(2025, 4, 1), date(2025, 12, 31), "250000000000")
        q4 = derive_by_subtraction(annual, nine)
        assert derived_interval_label(q4) == "Q4"
        assert (q4.period_start, q4.period_end) == (date(2026, 1, 1), date(2026, 3, 31))

    def test_both_sources_preserved(self):
        """Derivation returns a NEW record; the inputs are untouched objects."""
        annual = _cf(date(2025, 4, 1), date(2026, 3, 31), "300")
        h1 = _cf(date(2025, 4, 1), date(2025, 9, 30), "100")
        derive_by_subtraction(annual, h1)
        assert annual.value == Decimal(300)
        assert h1.value == Decimal(100)

    def test_boundary_mismatch_rejected(self):
        annual = _cf(date(2025, 4, 1), date(2026, 3, 31), "300")
        q3_only = _cf(date(2025, 10, 1), date(2025, 12, 31), "90")
        with pytest.raises(IncompatibleDerivation, match="starting boundary"):
            derive_by_subtraction(annual, q3_only)

    def test_unit_mismatch_rejected(self):
        annual = _cf(date(2025, 4, 1), date(2026, 3, 31), "300")
        h1_other_unit = _cf(date(2025, 4, 1), date(2025, 9, 30), "100", unit="INR/share")
        with pytest.raises(IncompatibleDerivation, match="unit mismatch"):
            derive_by_subtraction(annual, h1_other_unit)

    def test_scope_mismatch_rejected(self):
        annual = _cf(date(2025, 4, 1), date(2026, 3, 31), "300")
        h1_standalone = _cf(date(2025, 4, 1), date(2025, 9, 30), "100", scope="standalone")
        with pytest.raises(IncompatibleDerivation, match="scope mismatch"):
            derive_by_subtraction(annual, h1_standalone)

    def test_concept_mismatch_rejected(self):
        annual = _cf(date(2025, 4, 1), date(2026, 3, 31), "300")
        h1_capex = _cf(date(2025, 4, 1), date(2025, 9, 30), "100", concept="capex")
        with pytest.raises(IncompatibleDerivation, match="concept mismatch"):
            derive_by_subtraction(annual, h1_capex)

    def test_revision_mismatch_rejected(self):
        annual = _cf(date(2025, 4, 1), date(2026, 3, 31), "300")
        h1_revised = _cf(date(2025, 4, 1), date(2025, 9, 30), "100", revision="Revised")
        with pytest.raises(IncompatibleDerivation, match="revision status mismatch"):
            derive_by_subtraction(annual, h1_revised)

    def test_unknown_revision_compatible_with_unknown(self):
        annual = _cf(date(2025, 4, 1), date(2026, 3, 31), "300", revision=None)
        h1 = _cf(date(2025, 4, 1), date(2025, 9, 30), "100", revision=None)
        assert derive_by_subtraction(annual, h1).value == Decimal(200)

    def test_no_division_code_path_exists(self):
        # Structural guard for correction A: there is no half/4 or /2 helper in
        # the module — the only arithmetic is subtraction.
        import inspect

        import quarterline.sources.india.cash_flow as cash_flow_module

        source = inspect.getsource(cash_flow_module)
        assert "/ 4" not in source
        assert "/ 2" not in source
        assert "/4" not in source
        assert "/2" not in source


class TestAvailability:
    def test_reported_observation_found(self):
        reported = [
            _cf(date(2026, 4, 1), date(2026, 6, 30), "93300000000"),
        ]
        status, observation = cash_flow_availability(
            "cash_flow_operations",
            reported,
            scope="consolidated",
            period_start=date(2026, 4, 1),
            period_end=date(2026, 6, 30),
        )
        assert status == "reported"
        assert observation is not None and observation.value == Decimal(93300000000)

    def test_absent_is_not_present_in_ingested_sources_not_zero(self):
        status, observation = cash_flow_availability(
            "cash_flow_operations",
            [],  # nothing reported anywhere in the ingested corpus
            scope="consolidated",
            period_start=date(2026, 4, 1),
            period_end=date(2026, 6, 30),
        )
        assert status == MissingDataStatus.NOT_PRESENT_IN_INGESTED_SOURCES.value
        assert observation is None

    def test_scope_mismatch_counts_as_absent(self):
        reported = [_cf(date(2026, 4, 1), date(2026, 6, 30), "1", scope="standalone")]
        status, _ = cash_flow_availability(
            "cash_flow_operations",
            reported,
            scope="consolidated",
            period_start=date(2026, 4, 1),
            period_end=date(2026, 6, 30),
        )
        assert status == MissingDataStatus.NOT_PRESENT_IN_INGESTED_SOURCES.value

    def test_converter_ignores_non_cf_rows(self):
        class Row:
            canonical_concept = "revenue_from_operations"
            value_decimal = "100"
            unit = "INR"
            reporting_scope = "consolidated"
            period_start = date(2026, 4, 1)
            period_end = date(2026, 6, 30)

        assert reported_from_observation_rows([Row()]) == []


# -- PDF cash-flow extraction (text-level, page provenance) ------------------

_INFY_STYLE_PAGE = (
    "INFOSYS LIMITED AND SUBSIDIARIES\n"
    "Condensed Consolidated Statement of Cash Flows\n"
    "Accounting policy\n"
    "(In ₹ crore)\n"
    "Particulars\n"
    "Note No.\n"
    "2026\n"
    "2025\n"
    "Cash flow from operating activities\n"
    "Profit for the period\n"
    "                                7,775                                     6,924\n"
    "Cash generated from operations\n"
    "                              11,441                                     9,506\n"
    "Income taxes paid\n"
    "                              (2,111)                                   (1,874)\n"
    "Net cash generated by operating activities\n"
    "                                9,330                                     7,632\n"
    "Cash flows from investing activities\n"
)

_INFY_TITLE = (
    "INFOSYS LIMITED AND SUBSIDIARIES\n"
    "Condensed Consolidated Financial Statements under\n"
    "Indian Accounting Standards (Ind AS)\n"
    "for the three months ended June 30, 2026\n"
)

_HUL_STYLE_PAGE = (
    "(Rs in Crores)\n"
    "Year ended\n"
    "31st March, 2026\n"
    "Year ended\n"
    "31st March, 2025\n"
    "A CASH FLOWS FROM OPERATING ACTIVITIES:\n"
    "Profit before tax from Continuing Operations\n"
    "13,812\n"
    "14,428\n"
    "Cash flows generated from operations\n"
    "15,839\n"
    "14,154\n"
    "Taxes paid, net of refunds\n"
    "(4,840)\n"
    "(2,268)\n"
    "Net cash flows generated from operating activities - [A]\n"
    "10,999\n"
    "11,886\n"
)


class TestExtractCashFlowStatement:
    def test_infosys_quarterly_cfo_extracted(self):
        # SYNTHETIC page text mimicking the real condensed-FS layout (clearly
        # labeled; never presented as company results beyond the real check below).
        result = extract_cash_flow_statement(
            6, _INFY_STYLE_PAGE, "SYNTHETIC-INFY-STYLE", period_context=_INFY_TITLE
        )
        assert result.ok
        assert result.value == Decimal(93300000000)  # ₹9,330 Cr in full rupees
        assert (result.period_start, result.period_end) == (date(2026, 4, 1), date(2026, 6, 30))
        assert result.period_source == "document_title"
        assert result.declared_scale is not None
        assert "Net cash generated by operating activities" in (result.matched_line or "")

    def test_hul_annual_cfo_extracted_with_parenthesized_prior(self):
        result = extract_cash_flow_statement(11, _HUL_STYLE_PAGE, "SYNTHETIC-HUL-STYLE")
        assert result.ok
        assert result.value == Decimal(109990000000)  # ₹10,999 Cr
        assert (result.period_start, result.period_end) == (date(2025, 4, 1), date(2026, 3, 31))
        assert result.period_source == "page"

    def test_missing_period_header_is_manual_review(self):
        page = "(In ₹ crore)\nNet cash generated by operating activities\n9,330\n"
        result = extract_cash_flow_statement(1, page, "SYNTHETIC")
        assert not result.ok
        assert result.status == STATUS_MANUAL_REVIEW
        assert result.value is None
        assert "period" in (result.reason or "")

    def test_missing_scale_header_is_manual_review(self):
        page = (
            "for the three months ended June 30, 2026\n"
            "Net cash generated by operating activities\n"
            "9,330\n"
        )
        result = extract_cash_flow_statement(1, page, "SYNTHETIC")
        assert not result.ok
        assert result.status == STATUS_MANUAL_REVIEW
        assert result.value is None
        assert "scale" in (result.reason or "")

    def test_ambiguous_column_order_is_manual_review(self):
        # Prior-year column listed first on the page: the extractor must NOT
        # pick a column — ambiguity goes to review, never a guess.
        page = _INFY_STYLE_PAGE.replace("2026\n2025\n", "2025\n2026\n", 1)
        result = extract_cash_flow_statement(
            6, page, "SYNTHETIC-AMBIGUOUS", period_context=_INFY_TITLE
        )
        assert result.status == STATUS_MANUAL_REVIEW
        assert result.value is None
        assert "column order" in (result.reason or "")

    def test_no_cfo_line_is_manual_review(self):
        page = (
            "for the three months ended June 30, 2026\n(In ₹ crore)\nProfit for the period\n7,775\n"
        )
        result = extract_cash_flow_statement(1, page, "SYNTHETIC")
        assert result.status == STATUS_MANUAL_REVIEW
        assert "no net operating cash flow line" in (result.reason or "")

    def test_extracted_status_constant(self):
        assert STATUS_EXTRACTED == "extracted"


class TestLivePdfEvidence:
    """Re-verify the recorded evidence against the REAL cached PDFs.

    Skipped when the gitignored storage cache is absent (fresh clone); the
    committed CSV keeps the recorded values either way, and this test proves
    they cannot silently drift from the documents.
    """

    @pytest.fixture(autouse=True)
    def _require_cache(self):
        from quarterline.sources.india.reconcile import RENDERED_DOCUMENT_STORAGE, STORAGE_ROOT

        missing = [
            relative
            for relative in RENDERED_DOCUMENT_STORAGE.values()
            if not (STORAGE_ROOT / relative).is_file()
        ]
        if missing:
            pytest.skip(f"storage cache missing: {missing}")

    def test_recorded_quarterly_cfo_still_extracts_from_real_pdf(self):
        from quarterline.sources.india.reconcile import (
            RENDERED_DOCUMENT_STORAGE,
            STORAGE_ROOT,
        )

        recorded = REVIEWED_PDF_CASH_FLOW[0]
        assert recorded["value"] == "93300000000"
        from quarterline.sources.india.pdf_results import extract_cash_flow_from_pdf

        pdf = STORAGE_ROOT / RENDERED_DOCUMENT_STORAGE[recorded["reference_document_id"]]
        extractions = extract_cash_flow_from_pdf(pdf.read_bytes(), recorded["document"])
        extracted = [e for e in extractions if e.ok]
        assert len(extracted) == 1
        assert str(extracted[0].value) == recorded["value"]
        assert extracted[0].page_number == 6
        assert extracted[0].period_start.isoformat() == recorded["period_start"]
        assert extracted[0].period_end.isoformat() == recorded["period_end"]

    def test_recorded_display_tokens_present_on_cited_pages(self):
        """Every recorded displayed value must still appear on its cited page."""
        from quarterline.sources.india.pdf_results import page_texts
        from quarterline.sources.india.reconcile import (
            RENDERED_DOCUMENT_STORAGE,
            STORAGE_ROOT,
        )

        checks = [
            (
                "INFY-IR-q1-fy27-financial-results-auditorsreports.pdf",
                12,
                [
                    "48,211",
                    "49,195",
                    "11,028",
                    "7,775",
                    "7,769",
                    "19.19",
                    "46,402",
                    "8,509",
                    "8,501",
                    "21.01",
                    "178,650",
                    "182,972",
                    "41,284",
                    "39,995",
                    "29,474",
                    "29,440",
                    "71.58",
                    "1,289",
                ],
            ),
            (
                "INFY-IR-consol-fy27-q1-finstatement.pdf",
                6,
                ["9,330"],
            ),
            (
                "INFY-IR-consol-fy26-q4-and-12m-finstatement.pdf",
                6,
                ["33,986", "2,727"],
            ),
            (
                "HUL-IR-hul-mq26-financial-results.pdf",
                11,
                ["10,999", "(1,258)", "(103)"],
            ),
            (
                "HUL-IR-hul-mq26-financial-results.pdf",
                1,
                ["13,812", "10,652", "235 crores"],
            ),
            (
                "HUL-IR-hul-jq26-financial-results.pdf",
                7,
                ["17,341"],
            ),
        ]
        for document_id, page_number, tokens in checks:
            pdf = STORAGE_ROOT / RENDERED_DOCUMENT_STORAGE[document_id]
            pages = dict(page_texts(pdf.read_bytes()))
            text = pages[page_number]
            for token in tokens:
                assert token in text, f"{token} missing from {document_id} p.{page_number}"
