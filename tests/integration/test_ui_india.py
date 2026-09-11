"""India UI (IND-5; IND-6 corpus): /in landing, /in/{issuer_id} pages, coverage.

Honest-display contract: 10 registry rows, ALL verified since IND-6 (group A+B
identifier evidence), each linking onward; period identities with source labels;
both PAT variants co-labeled; ₹-crore display WITH the exact full-rupee amount;
formula captions on metrics; YoY rendered from the ingested prior-year
comparatives where they exist and as typed missing-status text where they do
not (never a fabricated comparative, never a zero); cash flow only at reported
frequencies (never divided); HUL review-pending cells flagged and linked to the
review packet; and the fixed no-score note. No route imports or calls any LLM
code; everything runs offline with Ollama unreachable.
"""

from __future__ import annotations

import html as html_module
import re
from decimal import ROUND_HALF_UP, Decimal

import pytest
from fastapi.testclient import TestClient
from india_test_helpers import (
    create_schema,
    import_all_fixtures,
    run_ind4_pipeline,
)
from ui_test_helpers import block_llm_modules, make_client  # noqa: F401 (fixture)

from quarterline.store.db import session_scope

pytestmark = pytest.mark.usefixtures("block_llm_modules")

DISCLAIMER = "Research and education only. Not investment advice."
INDIA_DISCLAIMER = (
    "Quarter labels/scores are not provided for India issuers in this release; indicators only."
)
REVIEW_PACKET_HREF = 'href="/in/review-packet"'


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    """Dev-style store: all 20 committed India fixtures + the full pipeline."""
    storage = tmp_path / "storage"
    monkeypatch.setenv("STORAGE_DIR", str(storage))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9")  # nothing listens here
    create_schema()
    import_all_fixtures()
    run_ind4_pipeline()
    with make_client() as testclient:
        yield testclient


def unescaped(text: str) -> str:
    return html_module.unescape(text)


# ---------------------------------------------------------------------------
# GET /in — landing with verification statuses
# ---------------------------------------------------------------------------


def test_landing_lists_all_10_issuers_with_verification_statuses(client) -> None:
    response = client.get("/in")

    assert response.status_code == 200
    html = response.text
    issuer_ids = re.findall(r'data-issuer-id="(IN-[A-Z]+)"', html)
    assert len(issuer_ids) == 10, "the whole India watchlist renders"
    assert set(issuer_ids) == {
        "IN-INFY",
        "IN-HINDUNILVR",
        "IN-TCS",
        "IN-HCLTECH",
        "IN-ITC",
        "IN-ASIANPAINT",
        "IN-MARUTI",
        "IN-ULTRACEMCO",
        "IN-SUNPHARMA",
        "IN-LT",
    }

    # IND-6: every issuer's identifiers are verified against NSE/BSE/instance
    # evidence (group A + B acquisition), so ALL TEN link onward
    for issuer_id in issuer_ids:
        assert f'href="/in/{issuer_id}"' in html, f"{issuer_id} is verified and must link onward"

    assert html.count(">verified<") == 10
    assert "proposed — identifiers not yet verified" not in html
    assert INDIA_DISCLAIMER in html
    assert DISCLAIMER in html
    assert "Coverage caveat" in html


def test_landing_is_reachable_when_registry_has_only_proposed_rows(client) -> None:
    """(Regression guard) the caveat text renders for verified issuers too."""
    html = client.get("/in").text
    assert "Open indicators" in html


# ---------------------------------------------------------------------------
# GET /in/IN-INFY — the full honest-display contract
# ---------------------------------------------------------------------------


def test_infy_page_period_strip_source_labels_and_dates(client) -> None:
    text = unescaped(client.get("/in/IN-INFY").text)

    # application label AND source label, dates primary
    assert "Q1 FY2026-27 (2026-04-01..2026-06-30)" in text
    assert 'ReportingQuarter="First quarter"' in text
    assert 'TypeOfReportingPeriod="Quarterly"' in text
    assert "NSE seq 177385" in text
    assert "published 2026-07-23" in text
    assert "Audited" in text  # audited/Original carried on the strip


def test_infy_page_pat_variants_distinct_and_scale_presentation(client) -> None:
    html = client.get("/in/IN-INFY").text

    pat_row = re.search(r'<tr data-concept="profit_after_tax">.*?</tr>', html, re.DOTALL).group(0)
    owners_row = re.search(
        r'<tr data-concept="profit_attributable_to_owners">.*?</tr>', html, re.DOTALL
    ).group(0)
    assert "Profit after tax (group)" in pat_row
    assert "Profit attributable to owners" in owners_row
    assert "₹7,775 Cr" in pat_row and "₹7,769 Cr" in owners_row  # distinct values

    # revenue: ₹ Cr display AND the exact full-rupee amount
    revenue_row = re.search(
        r'<tr data-concept="revenue_from_operations">.*?</tr>', html, re.DOTALL
    ).group(0)
    assert "₹48,211 Cr" in revenue_row
    assert "482,110,000,000" in revenue_row
    assert 'title="₹ 482,110,000,000' in revenue_row

    # EPS always carries "/share"
    assert html.count("/share") >= 2


def test_infy_page_metrics_captions_qoq_and_computed_yoy(client) -> None:
    response = client.get("/in/IN-INFY")
    assert response.status_code == 200
    text = unescaped(response.text)

    # formula captions state the exact inputs
    assert "revenue from operations, current vs immediately preceding fiscal quarter" in text
    assert "profit attributable to owners / revenue from operations" in text
    assert "profit after tax (group) / revenue from operations" in text

    # QoQ computed exactly (Q4 FY26 -> Q1 FY27), rendered as a percentage
    qoq = Decimal(482110000000) / Decimal(464020000000) - 1
    expected = f"{(qoq * 100).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}%"
    assert expected in text  # "3.90%"

    # IND-6: YoY now computes from the ingested IR-PDF comparative
    # (48,211 / 42,279 - 1, pdf_text provenance) and renders as a percentage
    yoy = Decimal(482110000000) / Decimal(422790000000) - 1
    expected_yoy = f"{(yoy * 100).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}%"
    yoy_row = re.search(r'<tr data-metric="india_revenue_yoy">.*?</tr>', text, re.DOTALL).group(0)
    assert expected_yoy in yoy_row


def test_tcs_page_renders_new_corpus_issuer(client) -> None:
    """IND-6: the eight newly verified issuers are first-class — a TCS spot
    check on the exact ingested headline values and the computed YoY."""
    text = unescaped(client.get("/in/IN-TCS").text)

    assert "Tata Consultancy Services Limited" in text
    assert "₹72,275 Cr" in text  # Q1 FY27 revenue
    assert "₹63,437 Cr" in text  # the ingested prior-year comparative
    yoy = Decimal(722750000000) / Decimal(634370000000) - 1
    expected = f"{(yoy * 100).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}%"
    assert expected in text  # "13.93%"
    assert "₹52,094 Cr" in text  # annual CFO from the Q4 instance
    # TCS's Q1 PDF carries a quarterly CFO but it is NOT ingested in this
    # corpus (only its availability is documented) — no fabricated row
    assert "12,171" not in text


def test_infy_page_cashflow_reported_frequencies_only(client) -> None:
    text = unescaped(client.get("/in/IN-INFY").text)

    assert (
        "Cash flow is shown only at the frequencies actually reported; values "
        "are never divided into artificial quarters." in text
    )
    # annual row and quarterly row, separately dated
    assert "FY2025-26 (annual)" in text
    assert "Q1 FY2026-27 (quarter)" in text
    assert "₹33,986 Cr" in text  # annual CFO (FY2025-26)
    assert "₹9,330 Cr" in text  # INFY's reported Q1 quarterly CFO
    # distinct PDF provenance for the quarterly row
    assert "company-IR PDF" in text
    assert "pdf_text" in text
    assert "p.6" in text
    # blocked H2 derivation shown as status text
    assert "source_not_ingested" in text


def test_infy_page_no_score_note_and_badges_and_links(client) -> None:
    html = client.get("/in/IN-INFY").text

    assert "No aggregate score exists for India issuers" in html
    assert "agent_checked_against_document" in html
    assert 'rel="noopener noreferrer"' in html
    assert "nsearchives.nseindia.com" in html  # external source link
    assert "/filings/" in html  # local filing copy link
    assert "not_present_in_ingested_sources" in html  # Q1 capex missing cell


# ---------------------------------------------------------------------------
# GET /in/IN-HINDUNILVR — review badges + missing quarterly CF + standalone
# ---------------------------------------------------------------------------


def test_hul_page_review_badges_link_to_review_packet(client) -> None:
    html = client.get("/in/IN-HINDUNILVR").text

    assert "requires_manual_review" in html
    assert REVIEW_PACKET_HREF in html  # amber badges link to the review packet
    review_row = re.search(
        r'<tr data-concept="revenue_from_operations">.*?</tr>', html, re.DOTALL
    ).group(0)
    assert "requires_manual_review" in review_row
    assert REVIEW_PACKET_HREF in review_row
    assert "₹17,341 Cr" in review_row  # carried as ingested, never resolved


def test_hul_page_quarterly_cash_flow_missing_not_zero(client) -> None:
    text = unescaped(client.get("/in/IN-HINDUNILVR").text)

    cf_rows = re.findall(
        r'<tr data-concept="cash_flow_operations" data-period-kind="[^"]*">.*?</tr>',
        text,
        re.DOTALL,
    )
    assert len(cf_rows) == 3  # annual + two quarter identities (cash-flow panel only)
    annual_row = next(row for row in cf_rows if "FY2025-26 (annual)" in row)
    assert "₹10,999 Cr" in annual_row
    for row in cf_rows:
        if "(quarter)" in row:
            assert "not_present_in_ingested_sources" in row
            assert "10,999" not in row  # annual value never leaked into quarters


def test_hul_page_annual_capex_present(client) -> None:
    text = unescaped(client.get("/in/IN-HINDUNILVR").text)
    capex_rows = re.findall(r'<tr data-concept="capex"[^>]*>.*?</tr>', text, re.DOTALL)
    annual_row = next(row for row in capex_rows if "FY2025-26 (annual)" in row)
    assert "₹1,258 Cr" in annual_row


def test_hul_standalone_toggle_explicit_empty_state(client) -> None:
    response = client.get("/in/IN-HINDUNILVR?scope=standalone")

    assert response.status_code == 200
    html = response.text
    assert "No standalone facts ingested" in html
    assert "never substituted" in html
    # consolidated data is NEVER silently shown on the standalone view
    assert "₹17,341 Cr" not in html
    assert "₹48,211 Cr" not in html
    assert INDIA_DISCLAIMER in html


def test_infy_standalone_toggle_also_empty(client) -> None:
    response = client.get("/in/IN-INFY?scope=standalone")

    assert response.status_code == 200
    assert "No standalone facts ingested" in response.text


def test_htmx_scope_toggle_receives_panels_partial_only(client) -> None:
    consolidated = client.get("/in/IN-INFY", headers={"HX-Request": "true"})
    standalone = client.get("/in/IN-INFY?scope=standalone", headers={"HX-Request": "true"})

    assert consolidated.status_code == standalone.status_code == 200
    assert "<html" not in consolidated.text.lower()
    assert 'id="india-panels"' in consolidated.text
    assert 'data-concept="revenue_from_operations"' in consolidated.text
    assert "<html" not in standalone.text.lower()
    assert "No standalone facts ingested" in standalone.text


# ---------------------------------------------------------------------------
# 404s: unknown issuers never get pages (all registry rows are verified now)
# ---------------------------------------------------------------------------


def test_unknown_issuer_404(client) -> None:
    response = client.get("/in/IN-NOPE")

    assert response.status_code == 404
    assert "Unknown India issuer" in response.text


def test_unknown_scope_404(client) -> None:
    response = client.get("/in/IN-INFY?scope=group")

    assert response.status_code == 404
    assert "scope" in response.text


# ---------------------------------------------------------------------------
# GET /in/{issuer_id}/coverage — JSON contract
# ---------------------------------------------------------------------------


def test_coverage_json_matches_coverage_report(client) -> None:
    from quarterline.sources.india.factcard import coverage_report

    response = client.get("/in/IN-INFY/coverage")

    assert response.status_code == 200
    actual = response.json()
    with session_scope() as session:
        expected = coverage_report(session, "IN-INFY").model_dump(mode="json")
    actual.pop("generated_at")
    expected.pop("generated_at")
    assert actual == expected
    kinds = {block["period_kind"] for block in actual["identities"]}
    assert kinds == {"quarter", "annual"}
    missing = [
        cell["missing_status"]
        for block in actual["identities"]
        for cell in block["cells"]
        if cell["status"] == "missing"
    ]
    assert "not_present_in_ingested_sources" in missing


def test_coverage_json_404_for_unknown_and_standalone(client) -> None:
    assert client.get("/in/IN-NOPE/coverage").status_code == 404
    standalone = client.get("/in/IN-INFY/coverage?scope=standalone")
    assert standalone.status_code == 404
    assert "error" in standalone.json()


# ---------------------------------------------------------------------------
# Disclaimers everywhere + review-packet route + no-LLM guarantee
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,status",
    [
        ("/in", 200),
        ("/in/IN-INFY", 200),
        ("/in/IN-HINDUNILVR", 200),
        ("/in/IN-HINDUNILVR?scope=standalone", 200),
        ("/in/IN-TCS", 200),
        ("/in/IN-MARUTI", 200),
        ("/in/IN-NOPE", 404),
    ],
)
def test_every_india_page_carries_the_disclaimers(client, path, status) -> None:
    response = client.get(path)

    assert response.status_code == status
    assert DISCLAIMER in response.text
    if status == 200:
        assert INDIA_DISCLAIMER in response.text


def test_review_packet_route_serves_the_document(client) -> None:
    response = client.get("/in/review-packet")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "Review checklist" in response.text  # the §9 checklist is in the doc
    assert "human_review_pending" in response.text


def test_nav_has_india_link(client) -> None:
    html = client.get("/").text
    assert 'href="/in"' in html


def test_india_templates_never_mark_content_safe() -> None:
    """Filing-derived content must pass through autoescape (no |safe anywhere)."""
    from pathlib import Path

    templates_dir = (
        Path(__file__).resolve().parents[2] / "src" / "quarterline" / "api" / "templates"
    )
    for path in sorted(templates_dir.rglob("*.html")):
        if "india" in path.name:
            assert "|safe" not in path.read_text(encoding="utf-8"), path
