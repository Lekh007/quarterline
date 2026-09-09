"""Citation validation tests (SPEC §2.2, §18 checks 4–7)."""

from __future__ import annotations

from datetime import date

from quarterline.core.citations import (
    CHECK_CONTEXT,
    CHECK_EXISTENCE,
    CHECK_FORMAT,
    CHECK_MEMBERSHIP,
    EvidenceRef,
    filter_statements,
    validate_statement_citations,
)

EV_OK = "ev-aaaaaaaaaaaa"
EV_OLD = "ev-bbbbbbbbbbbb"
EV_OTHER = "ev-cccccccccccc"

MAP = {
    EV_OK: EvidenceRef(EV_OK, 11, "AAPL", date(2026, 6, 27), "supplied text"),
    EV_OLD: EvidenceRef(EV_OLD, 12, "AAPL", date(2026, 3, 28), "old-quarter text"),
    EV_OTHER: EvidenceRef(EV_OTHER, 13, "MSFT", date(2026, 6, 27), "other company"),
}


def _validate(text: str, ids: list[str]):
    return validate_statement_citations(
        text, ids, MAP, expected_ticker="AAPL", expected_period_end=date(2026, 6, 27)
    )


def test_valid_statement_passes_all_checks() -> None:
    reasons = _validate(f"Management attributed growth to services. [{EV_OK}]", [EV_OK])
    assert reasons == []


def test_invented_citation_rejected() -> None:
    reasons = _validate("Statement. [ev-000000000000]", ["ev-000000000000"])
    assert any(reason.startswith(CHECK_EXISTENCE) for reason in reasons)
    assert any(reason.startswith(CHECK_MEMBERSHIP) for reason in reasons)


def test_citation_not_in_supplied_context_rejected() -> None:
    reasons = _validate(f"Statement cites undeclared id. [{EV_OK}]", [])
    assert any(reason.startswith(CHECK_MEMBERSHIP) for reason in reasons)


def test_declared_but_absent_from_text_rejected() -> None:
    reasons = _validate("Statement with no citation marker.", [EV_OK])
    assert any(reason.startswith(CHECK_MEMBERSHIP) for reason in reasons)


def test_wrong_period_citation_rejected() -> None:
    reasons = _validate(f"Statement from an older quarter. [{EV_OLD}]", [EV_OLD])
    assert any(reason.startswith(CHECK_CONTEXT) for reason in reasons)


def test_wrong_company_citation_rejected() -> None:
    reasons = _validate(f"Statement citing another company's evidence. [{EV_OTHER}]", [EV_OTHER])
    assert any(reason.startswith(CHECK_CONTEXT) for reason in reasons)


def test_unknown_period_citation_rejected() -> None:
    unknown = "ev-dddddddddddd"
    mapping = {
        **MAP,
        unknown: EvidenceRef(unknown, 14, "AAPL", None, "no period known"),
    }
    reasons = validate_statement_citations(
        f"Statement. [{unknown}]",
        [unknown],
        mapping,
        expected_ticker="AAPL",
        expected_period_end=date(2026, 6, 27),
    )
    assert any(reason.startswith(CHECK_CONTEXT) for reason in reasons)


def test_mid_sentence_citation_format_rejected() -> None:
    reasons = _validate(
        f"This [{EV_OK}] citation sits mid-sentence and is not attached to a sentence end.",
        [EV_OK],
    )
    assert any(reason.startswith(CHECK_FORMAT) for reason in reasons)


def test_uncited_statement_passes_validator_gate_adds_rule() -> None:
    # The validator itself passes a citation-free statement (nothing to
    # check); the GATE adds the filing-assertion-needs-citation rule for
    # bullets/risks (covered in test_validation_gate.py).
    assert _validate("Plain research question with no citation.", []) == []


def test_filter_statements_drops_whole_statement_and_records_reasons() -> None:
    statements = [
        (0, f"Good statement with citation. [{EV_OK}]", [EV_OK]),
        (1, f"Bad statement from old quarter. [{EV_OLD}]", [EV_OLD]),
        (2, "Uncited statement that will survive (question-like)", []),
    ]
    kept, report = filter_statements(
        statements, MAP, expected_ticker="AAPL", expected_period_end=date(2026, 6, 27)
    )
    assert [index for index, _text, _ids in kept] == [0, 2]
    assert report.checked == 3
    assert report.valid_count == 2
    assert report.invalid_count == 1
    assert report.citation_valid is False
    failure = next(r for r in report.results if not r.valid)
    assert failure.statement_index == 1
    assert failure.reasons


def test_filter_statements_all_valid_reports_citation_valid() -> None:
    kept, report = filter_statements(
        [(0, f"Only good. [{EV_OK}]", [EV_OK])],
        MAP,
        expected_ticker="AAPL",
        expected_period_end=date(2026, 6, 27),
    )
    assert len(kept) == 1
    assert report.citation_valid is True
