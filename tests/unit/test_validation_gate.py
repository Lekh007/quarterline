"""Validation-gate tests (SPEC §18 checks 1–10, §26 generation list).

These exercise :func:`quarterline.llm.generation.validate_generation` +
``finalize_status`` directly with a synthetic fact card and evidence map, so
every rejection path is observable without a database or provider.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from quarterline.core.citations import EvidenceRef
from quarterline.core.models import FactCard, MetricProvenance, MetricStatus, MetricValue
from quarterline.llm.generation import (
    finalize_status,
    label_display_of,
    validate_generation,
)
from quarterline.llm.schemas import Brief

PERIOD = date(2026, 6, 27)
EV = "ev-aaaaaaaaaaaa"
EV_OLD = "ev-bbbbbbbbbbbb"

EVIDENCE_MAP = {
    EV: EvidenceRef(EV, 11, "AAPL", PERIOD, "supplied text"),
    EV_OLD: EvidenceRef(EV_OLD, 12, "AAPL", date(2026, 3, 28), "old text"),
}


def _metric(metric_id: str, value: Decimal | None, unit: str | None) -> MetricValue:
    return MetricValue(
        metric_id=metric_id,
        value=value,
        unit=unit,
        status=MetricStatus.ok,
        provenance=MetricProvenance(formula_version="test"),
    )


def _card() -> FactCard:
    now = datetime.now(UTC)
    card = FactCard(
        ticker="AAPL",
        cik="0000320193",
        period_end=PERIOD,
        generated_at=now,
        metrics=[
            _metric("revenue", Decimal(84200000000), "USD"),
            _metric("revenue_yoy", Decimal("0.095"), "ratio"),
            _metric("quarter_label", None, None),
        ],
    )
    card.metrics[-1].provenance.derived_from = ["label:strong"]
    return card


def _valid_brief(**overrides) -> dict:
    payload = {
        "status": "ok",
        "label_echo": "Strong",
        "metric_mentions": [{"metric_id": "revenue_yoy", "template": "reported_change"}],
        "bullets": [
            {
                "text": f"Management attributed growth to services. [{EV}]",
                "evidence_ids": [EV],
            }
        ],
        "risks": [
            {
                "text": f"Management identified supply concentration. [{EV}]",
                "evidence_ids": [EV],
            }
        ],
        "open_questions": [{"text": "Is the services trend recurring?", "evidence_ids": []}],
    }
    payload.update(overrides)
    return payload


def _run(payload: dict):
    card = _card()
    parsed = Brief.model_validate(payload)
    surviving, report = validate_generation(
        parsed, card=card, label_display=label_display_of(card), evidence_map=EVIDENCE_MAP
    )
    return surviving, report, finalize_status(surviving, report)


def test_valid_provider_response_survives_intact() -> None:
    surviving, report, status = _run(_valid_brief())
    assert status == "ok"
    assert len(surviving.bullets) == 1 and len(surviving.risks) == 1
    assert surviving.label_echo == "Strong"
    assert report.citation_valid and report.factcheck_passed
    assert report.retained_bullet_count == 1 and report.dropped_bullet_count == 0


def test_label_mismatch_rejection_drops_everything() -> None:
    payload = _valid_brief(label_echo="Mixed")
    surviving, report, status = _run(payload)
    assert status == "insufficient_evidence"
    assert not surviving.bullets and not surviving.risks
    check = next(c for c in report.checks if c.name == "label_echo_equality")
    assert not check.passed and "does not equal" in check.reasons[0]


def test_invented_citation_rejection() -> None:
    payload = _valid_brief(
        bullets=[
            {
                "text": "Invented source. [ev-000000000000]",
                "evidence_ids": ["ev-000000000000"],
            }
        ]
    )
    surviving, report, status = _run(payload)
    assert surviving.bullets == []  # whole statement dropped, id not stripped
    assert report.dropped_bullet_count == 1
    check = next(c for c in report.checks if c.name == "citation_existence")
    assert not check.passed
    # valid risk/open-question/mention content survives -> partial, not empty
    assert status == "partial"


def test_wrong_period_citation_rejection() -> None:
    payload = _valid_brief(
        bullets=[
            {
                "text": f"Statement from an older quarter. [{EV_OLD}]",
                "evidence_ids": [EV_OLD],
            }
        ]
    )
    surviving, report, _status = _run(payload)
    assert surviving.bullets == []
    check = next(c for c in report.checks if c.name == "citation_company_period")
    assert not check.passed
    assert any("period" in reason for reason in check.reasons)


def test_unsupported_typed_number_rejection() -> None:
    payload = _valid_brief(
        bullets=[
            {
                "text": f"Management stated that revenue grew 44.7% year over year. [{EV}]",
                "evidence_ids": [EV],
            }
        ]
    )
    surviving, report, _status = _run(payload)
    assert surviving.bullets == []
    check = next(c for c in report.checks if c.name == "numeric_consistency")
    assert not check.passed
    assert any("44.7%" in reason for reason in check.reasons)


def test_supported_typed_number_survives() -> None:
    payload = _valid_brief(
        bullets=[
            {
                "text": f"Management stated that revenue grew 9.5% year over year. [{EV}]",
                "evidence_ids": [EV],
            }
        ]
    )
    surviving, _report, status = _run(payload)
    assert len(surviving.bullets) == 1
    assert status == "ok"


def test_valid_number_used_as_wrong_metric_rejected_structurally() -> None:
    # 9.5% is a valid revenue_yoy value, but revenue (a level metric) with a
    # change template is structurally invalid (SPEC §26).
    payload = _valid_brief(
        metric_mentions=[{"metric_id": "revenue", "template": "reported_change"}]
    )
    surviving, report, _status = _run(payload)
    assert surviving.metric_mentions == []
    check = next(c for c in report.checks if c.name == "metric_reference_validity")
    assert not check.passed
    assert "structurally valid" in check.reasons[0]


def test_empty_output_becomes_insufficient() -> None:
    payload = _valid_brief(
        status="insufficient_evidence",
        label_echo=None,
        metric_mentions=[],
        bullets=[],
        risks=[],
        open_questions=[],
    )
    surviving, report, status = _run(payload)
    assert status == "insufficient_evidence"
    assert surviving.bullets == [] and surviving.metric_mentions == []
    check = next(c for c in report.checks if c.name == "status_validity")
    assert "abstention" in check.reasons[0]


def test_uncited_bullet_rejected_but_open_question_survives() -> None:
    payload = _valid_brief(
        bullets=[{"text": "An uncited factual claim about the quarter.", "evidence_ids": []}]
    )
    surviving, report, status = _run(payload)
    assert surviving.bullets == []
    assert len(surviving.open_questions) == 1
    assert status == "partial"  # something survived, something was dropped
    check = next(c for c in report.checks if c.name == "citation_existence")
    assert not check.passed
    assert "no supplied citation" in check.reasons[0]


def test_partial_survival_with_recorded_reasons() -> None:
    payload = _valid_brief(
        bullets=[
            {"text": f"Good cited statement. [{EV}]", "evidence_ids": [EV]},
            {"text": f"Statement citing an old period. [{EV_OLD}]", "evidence_ids": [EV_OLD]},
        ]
    )
    surviving, report, status = _run(payload)
    assert len(surviving.bullets) == 1
    assert report.retained_bullet_count == 1
    assert report.dropped_bullet_count == 1
    assert status == "partial"
    assert any("period" in reason for reason in report.reasons())


def test_advice_content_refusal() -> None:
    payload = _valid_brief(
        metric_mentions=[],
        bullets=[
            {
                "text": f"You should buy the stock before earnings. [{EV}]",
                "evidence_ids": [EV],
            }
        ],
        risks=[],
        open_questions=[],
    )
    surviving, report, status = _run(payload)
    assert surviving.bullets == []
    assert status == "refused"  # advice drove the total content loss
    check = next(c for c in report.checks if c.name == "advice_policy_compliance")
    assert not check.passed


def test_advice_statement_dropped_but_other_content_survives_partial() -> None:
    payload = _valid_brief(
        bullets=[
            {
                "text": f"You should buy the stock before earnings. [{EV}]",
                "evidence_ids": [EV],
            }
        ]
    )
    surviving, _report, status = _run(payload)
    assert surviving.bullets == []
    assert surviving.risks  # valid risk statement survives
    assert status == "partial"


def test_metric_mention_of_missing_metric_drops_mention_only() -> None:
    card = _card()
    card.metrics[0].status = MetricStatus.missing
    payload = _valid_brief(metric_mentions=[{"metric_id": "revenue", "template": "reported_value"}])
    parsed = Brief.model_validate(payload)
    surviving, report = validate_generation(
        parsed, card=card, label_display="Strong", evidence_map=EVIDENCE_MAP
    )
    # the mention passes the structural check (reported_value fits revenue);
    # the expansion layer later refuses to render a missing metric.
    assert len(surviving.metric_mentions) == 1
    assert finalize_status(surviving, report) == "ok"
