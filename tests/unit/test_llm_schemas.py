"""Schema tests (contract C11, SPEC §17): allowlist, forbidden extras, shapes."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from quarterline.core.models import METRIC_IDS
from quarterline.llm.schemas import (
    Answer,
    Brief,
    BriefBullet,
    BriefRisks,
    MetricMention,
    OpenQuestion,
    answer_json_schema,
    brief_json_schema,
)


def test_metric_mention_accepts_allowlisted_metric_id() -> None:
    mention = MetricMention(metric_id="revenue_yoy", template="reported_change")
    assert mention.metric_id in METRIC_IDS


def test_metric_mention_rejects_invented_metric_id() -> None:
    with pytest.raises(ValidationError, match="allowlist"):
        MetricMention(metric_id="made_up_metric", template="reported_change")


def test_metric_mention_rejects_unknown_template() -> None:
    with pytest.raises(ValidationError):
        MetricMention(metric_id="revenue_yoy", template="invented_template")


def test_brief_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError, match="extra"):
        Brief.model_validate(
            {
                "status": "ok",
                "label_echo": "Strong",
                "metric_mentions": [],
                "bullets": [],
                "risks": [],
                "open_questions": [],
                "smuggled": "field",
            }
        )


def test_brief_rejects_invalid_status() -> None:
    with pytest.raises(ValidationError):
        Brief.model_validate(
            {
                "status": "dancing",
                "label_echo": None,
                "metric_mentions": [],
                "bullets": [],
                "risks": [],
                "open_questions": [],
            }
        )


def test_brief_roundtrip_full_shape() -> None:
    payload = {
        "status": "ok",
        "label_echo": "Strong",
        "metric_mentions": [{"metric_id": "revenue_yoy", "template": "reported_change"}],
        "bullets": [
            {
                "text": "Management stated growth. [ev-0123456789ab]",
                "evidence_ids": ["ev-0123456789ab"],
            }
        ],
        "risks": [
            {
                "text": "Management identified a risk. [ev-0123456789ab]",
                "evidence_ids": ["ev-0123456789ab"],
            }
        ],
        "open_questions": [{"text": "Is the trend recurring?", "evidence_ids": []}],
    }
    brief = Brief.model_validate(payload)
    assert brief.bullets[0].evidence_ids == ["ev-0123456789ab"]
    assert Brief.model_validate_json(brief.model_dump_json()) == brief


def test_bullet_and_risk_require_text() -> None:
    with pytest.raises(ValidationError):
        BriefBullet(text="", evidence_ids=[])
    with pytest.raises(ValidationError):
        BriefRisks(text="", evidence_ids=[])
    with pytest.raises(ValidationError):
        OpenQuestion(text="", evidence_ids=[])


def test_answer_schema_shape_and_extras_forbidden() -> None:
    answer = Answer(
        status="ok", text="Grounded answer. [ev-0123456789ab]", evidence_ids=["ev-0123456789ab"]
    )
    assert answer.evidence_ids == ["ev-0123456789ab"]
    with pytest.raises(ValidationError, match="extra"):
        Answer.model_validate({"status": "ok", "text": "x", "evidence_ids": [], "bonus": 1})


def test_json_schema_descriptions_match_documented_keys() -> None:
    brief_schema = brief_json_schema()
    assert set(brief_schema["properties"]) == {
        "status",
        "label_echo",
        "metric_mentions",
        "bullets",
        "risks",
        "open_questions",
    }
    answer_schema = answer_json_schema()
    assert set(answer_schema["properties"]) == {"status", "text", "evidence_ids"}
