"""IND-8 India brief pipeline + validation gate tests (offline).

Runs the real ``generate_india_brief`` pipeline over the seeded INFY+HUL store
(committed exchange fixtures through the IND-4 data layer + a SYNTHETIC,
clearly test-only narrative corpus indexed with FakeEmbeddingProvider) with
the ScriptedProvider fake. Every gate behavior the mission pins is asserted:

valid scripted brief -> ok; invented citation rejected; wrong-issuer citation
rejected; off-allowlist metric reference rejected; ``reported_change`` on a
non-growth metric rejected; consolidated/standalone confusion dropped;
quarterly cash-flow fabrication dropped against the fact layer's typed
status; commentary stated as unattributed fact dropped (attributed
survives); advice request refused without a provider call; repair exactly
once; cache hit; idempotent outcome.
"""

from __future__ import annotations

import pytest
from india_generation_test_helpers import (
    HUL,
    INFY,
    Q1_FY27_END,
    ScriptedProvider,
    brief_context_passages,
    india_brief_payload,
    india_generation_db_fixture,  # noqa: F401 (registers the fixture)
    passage_ids_by_slug,
    scripted_result,
)
from sqlalchemy import delete

from quarterline.store.db import session_scope
from quarterline.store.models import BriefCache


@pytest.fixture
def ids_and_labels(india_generation_db) -> dict:
    """Evidence ids of the seeded pages + the code-generated period labels."""
    with session_scope() as session:
        ids = passage_ids_by_slug(brief_context_passages(session), india_generation_db["documents"])
        ids_h = passage_ids_by_slug(
            brief_context_passages(session, ticker="HINDUNILVR"), india_generation_db["documents"]
        )
        from quarterline.sources.india.brief import application_label_of
        from quarterline.sources.india.factcard import build_india_fact_card

        return {
            "infy_statement": ids["infy-statement"],
            "infy_presentation": ids["infy-presentation"],
            "hul_statement": ids_h["hul-statement"],
            "hul_notes": ids_h["hul-notes"],
            "infy_label": application_label_of(
                build_india_fact_card(session, INFY, period_end=Q1_FY27_END)
            ),
            "hul_label": application_label_of(
                build_india_fact_card(session, HUL, period_end=Q1_FY27_END)
            ),
        }


def _clear_cache() -> None:
    with session_scope() as session:
        session.execute(delete(BriefCache))


def _run(payload: dict, issuer: str = INFY, *, focus: str | None = None, provider=None):
    from quarterline.sources.india.brief import generate_india_brief

    provider = provider if provider is not None else ScriptedProvider([scripted_result(payload)])
    _clear_cache()
    outcome = generate_india_brief(
        issuer, Q1_FY27_END, scope="consolidated", focus=focus, provider=provider
    )
    return outcome, provider


def test_valid_scripted_brief_passes_the_whole_gate(ids_and_labels) -> None:
    outcome, provider = _run(
        india_brief_payload(
            ids_and_labels["infy_label"],
            ids_and_labels["infy_statement"],
            bullets=[
                {
                    "text": (
                        "Management stated demand for the quarter's services "
                        f"remained strong. [{ids_and_labels['infy_statement']}]"
                    ),
                    "evidence_ids": [ids_and_labels["infy_statement"]],
                }
            ],
        )
    )

    assert outcome.status == "ok"
    assert provider.call_count == 1
    assert outcome.brief is not None
    assert len(outcome.brief.bullets) == 1
    assert outcome.brief.label_echo == ids_and_labels["infy_label"]
    assert all(check.passed for check in outcome.validation.checks)
    # the india_* metric mention rendered BY PYTHON (percent, not pp)
    assert outcome.metric_facts == [
        (
            "Revenue changed +3.9% versus the immediately preceding fiscal "
            "quarter (percent, not percentage points)."
        )
    ]
    assert outcome.cache_hit is False


def test_invented_citation_drops_the_whole_statement(ids_and_labels) -> None:
    invented = "ev-000000000000"
    outcome, _provider = _run(
        india_brief_payload(
            ids_and_labels["infy_label"],
            ids_and_labels["infy_statement"],
            bullets=[
                {
                    "text": f"Management stated demand remained strong. [{invented}]",
                    "evidence_ids": [invented],
                }
            ],
            metric_mentions=[],
        )
    )

    assert outcome.status == "insufficient_evidence"
    assert any(
        "was not supplied (invented or out-of-context citation)" in reason
        for reason in outcome.reasons
    )
    assert any(check.check_id == "4" and not check.passed for check in outcome.validation.checks)


def test_wrong_issuer_citation_rejected(ids_and_labels) -> None:
    outcome, _provider = _run(
        india_brief_payload(
            ids_and_labels["hul_label"],
            ids_and_labels["infy_statement"],  # INFY page cited in a HUL brief
            bullets=[
                {
                    "text": (
                        "Management stated demand remained strong. "
                        f"[{ids_and_labels['infy_statement']}]"
                    ),
                    "evidence_ids": [ids_and_labels["infy_statement"]],
                }
            ],
            metric_mentions=[],
        ),
        issuer=HUL,
    )

    assert outcome.status == "insufficient_evidence"
    assert any(
        "was not supplied (invented or out-of-context citation)" in reason
        for reason in outcome.reasons
    )


def test_off_allowlist_metric_reference_cannot_reach_the_user(ids_and_labels) -> None:
    """A metric_id outside INDIA_METRIC_REFERENCES is rejected by the schema
    itself (pydantic validator + enum-constrained JSON schema); after the
    single repair pass the output is still invalid, so the pipeline fails in a
    controlled way: facts and evidence, never the unvalidated prose."""
    from pydantic import ValidationError

    from quarterline.sources.india.brief import INDIA_METRIC_REFERENCES, IndiaMetricMention

    assert "revenue_yoy" not in INDIA_METRIC_REFERENCES  # the US id is not India
    with pytest.raises(ValidationError):
        IndiaMetricMention(metric_id="revenue_yoy", template="reported_change")

    bad_payload = india_brief_payload(
        ids_and_labels["infy_label"],
        ids_and_labels["infy_statement"],
        metric_mentions=[{"metric_id": "ebitda", "template": "reported_value"}],
    )
    outcome, _provider = _run(
        None,
        provider=ScriptedProvider([scripted_result(bad_payload), scripted_result(bad_payload)]),
    )

    assert outcome.status == "provider_unavailable"
    assert outcome.brief is None
    assert any("controlled generation failure" in reason for reason in outcome.reasons)
    assert outcome.facts and outcome.evidence  # facts + evidence still shown


def test_reported_change_on_a_non_growth_metric_rejected(ids_and_labels) -> None:
    """The structural half of check 8 (the allowlist half is the schema, see
    the off-allowlist test): reported_change is only valid for the three
    growth metrics, so a margin 'change' drops the mention."""
    outcome, _provider = _run(
        india_brief_payload(
            ids_and_labels["infy_label"],
            ids_and_labels["infy_statement"],
            metric_mentions=[
                {"metric_id": "india_pat_margin_owners", "template": "reported_change"}
            ],
        )
    )

    assert outcome.status == "partial"
    assert any("not structurally valid" in reason for reason in outcome.reasons)
    assert any(check.check_id == "8" and not check.passed for check in outcome.validation.checks)
    # the margin is a LEVEL: percent change wording never renders
    assert not any("changed" in fact for fact in outcome.metric_facts)


def test_scope_confusion_dropped_by_check_11(ids_and_labels) -> None:
    outcome, _provider = _run(
        india_brief_payload(
            ids_and_labels["infy_label"],
            ids_and_labels["infy_statement"],
            bullets=[
                {
                    "text": (
                        f"Standalone revenue from operations grew in the quarter. "
                        f"[{ids_and_labels['infy_statement']}]"
                    ),
                    "evidence_ids": [ids_and_labels["infy_statement"]],
                }
            ],
            metric_mentions=[],
        )
    )

    assert outcome.status == "insufficient_evidence"
    assert any("scope_attribution" in reason for reason in outcome.reasons)
    assert any(check.check_id == "11" and not check.passed for check in outcome.validation.checks)


def test_quarterly_cash_flow_fabrication_dropped_against_the_fact_layer(
    ids_and_labels,
) -> None:
    """HUL has NO quarterly cash-flow fact at the Q1 FY27 identity (typed
    status not_present_in_ingested_sources), so a scripted quarterly-CFO
    number has no compatible fact-card target and drops the whole statement."""
    from quarterline.sources.india.brief import primary_identity
    from quarterline.sources.india.factcard import build_india_fact_card

    with session_scope() as session:
        card = build_india_fact_card(session, HUL, period_end=Q1_FY27_END)
        identity = primary_identity(card)
        cashflow_cells = [
            cell
            for cell in card.coverage
            if cell.concept == "cash_flow_operations"
            and (cell.period_start, cell.period_end, cell.period_kind) == identity
        ]
        assert cashflow_cells and cashflow_cells[0].status == "missing"
        assert cashflow_cells[0].missing_status == "not_present_in_ingested_sources"

    outcome, _provider = _run(
        india_brief_payload(
            ids_and_labels["hul_label"],
            ids_and_labels["hul_statement"],
            bullets=[
                {
                    "text": (
                        "Operating cash flow for the quarter was 9,330 crore "
                        f"rupees. [{ids_and_labels['hul_statement']}]"
                    ),
                    "evidence_ids": [ids_and_labels["hul_statement"]],
                }
            ],
            metric_mentions=[],
        ),
        issuer=HUL,
    )

    assert outcome.status == "insufficient_evidence"
    assert any(
        "numeric_consistency" in reason and "unsupported claim" in reason
        for reason in outcome.reasons
    )
    assert any(check.check_id == "9" and not check.passed for check in outcome.validation.checks)


def test_commentary_stated_as_unattributed_fact_dropped(ids_and_labels) -> None:
    payload = india_brief_payload(
        ids_and_labels["infy_label"],
        ids_and_labels["infy_presentation"],
        bullets=[
            {
                "text": (
                    "Revenue growth was the highest in many quarters. "
                    f"[{ids_and_labels['infy_presentation']}]"
                ),
                "evidence_ids": [ids_and_labels["infy_presentation"]],
            }
        ],
        metric_mentions=[],
    )
    unattributed, _provider = _run(payload)

    assert unattributed.status == "insufficient_evidence"
    assert any("commentary_attribution" in reason for reason in unattributed.reasons)
    assert any(
        check.check_id == "12" and not check.passed for check in unattributed.validation.checks
    )

    # the SAME statement with the attribution marker survives the gate
    attributed = india_brief_payload(
        ids_and_labels["infy_label"],
        ids_and_labels["infy_presentation"],
        bullets=[
            {
                "text": (
                    "Management stated revenue growth was the highest in many "
                    f"quarters. [{ids_and_labels['infy_presentation']}]"
                ),
                "evidence_ids": [ids_and_labels["infy_presentation"]],
            }
        ],
        metric_mentions=[],
    )
    outcome, _provider = _run(attributed)
    assert outcome.status == "ok"


def test_advice_focus_refused_without_a_provider_call(ids_and_labels) -> None:
    outcome, provider = _run(
        india_brief_payload(ids_and_labels["infy_label"], ids_and_labels["infy_statement"]),
        focus="Should I buy HUL shares now?",
    )

    assert outcome.status == "refused"
    assert provider.call_count == 0
    assert outcome.brief is None
    assert "advice-policy" in outcome.reasons[0]
    assert outcome.advice_policy_version == "advice-policy-v1"


def test_repair_runs_exactly_once(ids_and_labels) -> None:
    class BrokenThenValid:
        provider_name = "scripted"
        model_id = "scripted-test-model"
        calls = 0

        def verify_model(self) -> None:
            return None

        def generate(self, messages, json_schema=None):
            BrokenThenValid.calls += 1
            if BrokenThenValid.calls == 1:
                return scripted_result({"status": "ok", "bullets": "not-an-array"})
            return scripted_result(
                india_brief_payload(ids_and_labels["infy_label"], ids_and_labels["infy_statement"])
            )

    provider = BrokenThenValid()
    outcome, _ = _run(None, provider=provider)

    assert BrokenThenValid.calls == 2  # one generation + exactly ONE repair pass
    assert outcome.status == "ok"


def test_cache_hit_is_idempotent_and_skips_the_provider(ids_and_labels) -> None:
    payload = india_brief_payload(ids_and_labels["infy_label"], ids_and_labels["infy_statement"])
    first, first_provider = _run(payload)
    assert first.status == "ok"
    assert first_provider.call_count == 1

    second_provider = ScriptedProvider([])
    from quarterline.sources.india.brief import generate_india_brief

    second = generate_india_brief(INFY, Q1_FY27_END, scope="consolidated", provider=second_provider)

    assert second_provider.call_count == 0  # served from the cache
    assert second.cache_hit is True
    assert second.status == first.status
    assert second.brief == first.brief
    assert second.metric_facts == first.metric_facts


def test_rendered_levels_and_values_for_the_allowlist(ids_and_labels) -> None:
    """reported_value on a concept renders crore + exact rupees; margins carry
    documented level wording."""
    from quarterline.sources.india.brief import (
        IndiaMetricMention,
        expand_india_metric_mentions,
    )
    from quarterline.sources.india.factcard import build_india_fact_card

    with session_scope() as session:
        card = build_india_fact_card(session, INFY, period_end=Q1_FY27_END)

    expansion = expand_india_metric_mentions(
        [
            IndiaMetricMention(metric_id="revenue_from_operations", template="reported_value"),
            IndiaMetricMention(metric_id="india_pat_margin_owners", template="reported_level"),
            IndiaMetricMention(metric_id="india_pat_margin_owners", template="reported_value"),
            IndiaMetricMention(metric_id="eps_diluted", template="reported_value"),
        ],
        card,
    )
    assert expansion.issues == []
    revenue, margin_level, margin_value, eps = (fact.text for fact in expansion.rendered)
    assert "₹48,211 Cr" in revenue and "482,110,000,000" in revenue
    assert "moderate level" in margin_level and "documented scale anchor 10%" in margin_level
    assert "16.1%" in margin_value and "revenue from operations" in margin_value
    assert eps.startswith("Diluted EPS was ₹19.17/share")
