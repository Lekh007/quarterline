"""Unit tests for section mapping (SPEC §13.2) — synthetic 10-Q/10-K HTML plus
the committed REAL 8-K earnings-release fixture."""

from __future__ import annotations

from pathlib import Path

from quarterline.ingest.html_clean import clean_html
from quarterline.ingest.sections import (
    SECTION_TYPES,
    detect_sections,
    earnings_release_candidate,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "documents"

RISK_BODY = (
    "The risk factors previously disclosed in our annual report have not changed "
    "materially, although supply chain concentration and evolving data regulation "
    "remain areas of continued attention for the business this year."
)

TEN_Q_HTML = f"""<!DOCTYPE html>
<html><head><title>10-Q</title></head><body>
<h1>Part I - Financial Information</h1>
<table>
<tr><td>Item 1.</td><td>Financial Statements</td><td>3</td></tr>
<tr><td>Item 2.</td><td>Management's Discussion and Analysis of Financial Condition and Results of Operations</td><td>12</td></tr>
<tr><td>Item 3.</td><td>Quantitative and Qualitative Disclosures About Market Risk</td><td>20</td></tr>
</table>
<p>Condensed consolidated financial statements follow this table.</p>
<h2>Item 2. Management's Discussion and Analysis of Financial Condition and Results of Operations</h2>
<p>Revenue increased 12 percent driven by strong services growth across every segment during the quarter, while operating cash flow remained robust and the company continued returning capital to shareholders.</p>
<h3>Results of Operations</h3>
<p>Products revenue declined modestly while services revenue rose against a strong prior-year comparison.</p>
<h2>Item 3. Quantitative and Qualitative Disclosures About Market Risk</h2>
<p>Interest rate risk is limited to the investment portfolio.</p>
<h1>Part II - Other Information</h1>
<h2>Item 1A. Risk Factors</h2>
<p>{RISK_BODY}</p>
<h2>Item 1B. Unresolved Staff Comments</h2>
<p>None.</p>
</body></html>"""

#: ToC rendered as heading tags (worst case) *before* the real section bodies.
TEN_Q_TOC_AS_HEADINGS_HTML = """<html><body>
<h2>Item 2. Management's Discussion and Analysis of Financial Condition and Results of Operations</h2>
<h2>Item 3. Quantitative and Qualitative Disclosures About Market Risk</h2>
<p>Financial statements follow.</p>
<h1>Part I - Financial Information</h1>
<h2>Item 2. Management's Discussion and Analysis of Financial Condition and Results of Operations</h2>
<p>Revenue increased because services grew across every segment and pricing improved in all regions during the quarter, while products revenue declined modestly against a difficult prior-year comparison in two markets.</p>
<h2>Item 3. Quantitative and Qualitative Disclosures About Market Risk</h2>
<p>Interest rate exposure is modest and actively monitored.</p>
</body></html>"""

#: Only ToC-scale matches exist; nothing may be labelled mda.
TEN_Q_TOC_ONLY_HTML = """<html><body>
<h2>Item 2. Management's Discussion and Analysis of Financial Condition and Results of Operations</h2>
<h2>Item 3. Quantitative and Qualitative Disclosures About Market Risk</h2>
</body></html>"""

#: No MD&A / risk sections at all — nothing may be labelled.
TEN_Q_NO_SECTIONS_HTML = """<html><body>
<h1>Part I - Financial Information</h1>
<h2>Item 1. Financial Statements</h2>
<p>The condensed statements follow on the next pages.</p>
<h1>Part II - Other Information</h1>
<h2>Item 6. Exhibits</h2>
<p>Exhibit 31 certification rules apply.</p>
</body></html>"""

TEN_K_HTML = f"""<html><body>
<table>
<tr><td>Item 7.</td><td>Management's Discussion and Analysis of Financial Condition and Results of Operations</td><td>40</td></tr>
<tr><td>Item 7A.</td><td>Quantitative and Qualitative Disclosures About Market Risk</td><td>55</td></tr>
<tr><td>Item 1A.</td><td>Risk Factors</td><td>12</td></tr>
</table>
<h2>Item 7. Management's Discussion and Analysis of Financial Condition and Results of Operations</h2>
<p>Annual revenue grew 8 percent to 52 billion dollars as cloud services scaled and cost discipline held across all divisions for the fiscal year just ended, while gross margin expanded.</p>
<h2>Item 7A. Quantitative and Qualitative Disclosures About Market Risk</h2>
<p>Foreign currency exposure is hedged with derivatives.</p>
<h2>Item 1A. Risk Factors</h2>
<p>{RISK_BODY}</p>
</body></html>"""


def _detect(html: str, form: str):
    doc = clean_html(html.encode("utf-8"))
    return doc, detect_sections(form, doc)


def test_section_type_vocabulary() -> None:
    assert set(SECTION_TYPES) == {"mda", "risk_factors", "earnings_release", "other"}


def test_ten_q_maps_mda_part_i_item2_and_risk_part_ii_item1a() -> None:
    doc, candidates = _detect(TEN_Q_HTML, "10-Q")
    by_type = {c.section_type: c for c in candidates}
    assert set(by_type) == {"mda", "risk_factors"}

    mda = by_type["mda"]
    assert "Management's Discussion" in mda.heading
    assert mda.confidence >= 0.9
    mda_body = doc.text[mda.start_offset : mda.end_offset]
    assert "Revenue increased 12 percent" in mda_body
    assert "Results of Operations" in mda_body  # sub-headings stay inside MD&A
    assert "Interest rate risk" not in mda_body  # ends at the next item boundary

    risk = by_type["risk_factors"]
    assert "Risk Factors" in risk.heading
    risk_body = doc.text[risk.start_offset : risk.end_offset]
    assert "supply chain concentration" in risk_body
    assert "Unresolved Staff Comments" not in risk_body

    for candidate in candidates:
        # The section span starts at its heading block and slices the cleaned text.
        assert doc.text[candidate.start_offset : candidate.end_offset].startswith(candidate.heading)
        assert 0.0 <= candidate.confidence <= 1.0
        assert candidate.start_offset < candidate.end_offset


def test_table_of_contents_entries_never_become_sections() -> None:
    doc, candidates = _detect(TEN_Q_HTML, "10-Q")
    mda = next(c for c in candidates if c.section_type == "mda")
    # The ToC table mentions Item 2 near the top; the candidate must start at
    # the real heading, not the ToC row.
    first_toc_mention = doc.text.find("Item 2.")
    real_heading = doc.text.find("Item 2. Management's Discussion", first_toc_mention + 1)
    assert mda.start_offset == real_heading


def test_toc_as_headings_largest_body_wins() -> None:
    doc, candidates = _detect(TEN_Q_TOC_AS_HEADINGS_HTML, "10-Q")
    mda = [c for c in candidates if c.section_type == "mda"]
    assert len(mda) == 1
    real_body_pos = doc.text.find("Revenue increased because")
    assert mda[0].start_offset <= real_body_pos < mda[0].end_offset
    assert "Revenue increased because" in doc.text[mda[0].start_offset : mda[0].end_offset]
    assert mda[0].confidence >= 0.9


def test_toc_only_matches_demoted_to_other_with_low_confidence() -> None:
    _doc, candidates = _detect(TEN_Q_TOC_ONLY_HTML, "10-Q")
    assert candidates, "uncertainty must be recorded, not hidden"
    assert all(c.section_type == "other" for c in candidates)
    assert all(c.confidence <= 0.2 for c in candidates)
    assert all("not labelled" in c.notes for c in candidates)


def test_never_mislabels_whole_filing_when_sections_absent() -> None:
    _doc, candidates = _detect(TEN_Q_NO_SECTIONS_HTML, "10-Q")
    assert all(c.section_type != "mda" for c in candidates)
    assert all(c.section_type != "risk_factors" for c in candidates)


def test_unknown_form_has_no_item_candidates() -> None:
    _doc, candidates = _detect(TEN_Q_HTML, "8-K")
    assert candidates == []


def test_ten_k_maps_item7_and_item1a_but_not_item7a() -> None:
    doc, candidates = _detect(TEN_K_HTML, "10-K")
    by_type = {c.section_type: c for c in candidates}
    assert set(by_type) == {"mda", "risk_factors"}

    mda = by_type["mda"]
    assert mda.heading.startswith("Item 7.")  # not Item 7A
    mda_body = doc.text[mda.start_offset : mda.end_offset]
    assert "Annual revenue grew 8 percent" in mda_body
    assert "Foreign currency exposure" not in mda_body

    risk_body = doc.text[by_type["risk_factors"].start_offset : by_type["risk_factors"].end_offset]
    assert "supply chain concentration" in risk_body


def test_short_body_item_gets_reduced_confidence_with_note() -> None:
    html = (
        "<html><body><h1>Part II - Other Information</h1>"
        "<h2>Item 1A. Risk Factors</h2>"
        "<p>There have been no material changes to the risk factors previously "
        "disclosed in the annual report.</p>"
        "</body></html>"
    )
    _doc, candidates = _detect(html, "10-Q")
    risk = [c for c in candidates if c.section_type == "risk_factors"]
    assert len(risk) == 1
    assert risk[0].confidence <= 0.5
    assert "short body" in risk[0].notes


def test_real_8k_fixture_maps_to_earnings_release_not_mda() -> None:
    content = (FIXTURES / "real_aapl_8k_ex991_q2fy26.htm").read_bytes()
    doc = clean_html(content)
    # Item-based mapping does not apply to 8-K exhibits.
    assert detect_sections("8-K", doc) == []

    candidate = earnings_release_candidate(doc)
    assert candidate.section_type == "earnings_release"
    assert candidate.start_offset == 0
    assert candidate.end_offset == len(doc.text)
    assert candidate.confidence >= 0.85
    assert "Apple reports second quarter results" in doc.text
    assert doc.text[candidate.start_offset : candidate.end_offset] == doc.text


def test_real_8k_fixture_offsets_roundtrip_and_no_wrapper_leakage() -> None:
    content = (FIXTURES / "real_aapl_8k_ex991_q2fy26.htm").read_bytes()
    doc = clean_html(content)
    assert doc.blocks, "real fixture must produce blocks"
    for block in doc.blocks:
        assert doc.text[block.start_offset : block.end_offset] == block.text
    # SGML <DOCUMENT> wrapper labels must not leak into cleaned text.
    assert "<DOCUMENT>" not in doc.text
    assert "EX-99.1" not in doc.text
