"""Repair-path tests (SPEC §17 JSON repair: generate once, at most one repair)."""

from __future__ import annotations

import json

import pytest
from generation_test_helpers import ScriptedProvider, scripted_result, scripted_text
from pydantic import BaseModel

from quarterline.llm.base import GenerationResult
from quarterline.llm.repair import (
    GenerationFailure,
    generate_and_parse,
    parse_model_output,
)

MESSAGES = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]


class Box(BaseModel):
    """Tiny target schema."""

    value: int


def _box_result(value: int) -> GenerationResult:
    return scripted_result({"value": value})


def test_valid_first_pass_single_provider_call() -> None:
    provider = ScriptedProvider([_box_result(1)])
    outcome = generate_and_parse(provider, messages=MESSAGES, model_cls=Box)
    assert outcome.model.value == 1
    assert provider.call_count == 1
    assert outcome.first_pass_valid is True
    assert outcome.repair_used is False


def test_invalid_json_then_valid_exactly_two_provider_calls() -> None:
    provider = ScriptedProvider([scripted_text("not json at all"), _box_result(2)])
    outcome = generate_and_parse(provider, messages=MESSAGES, model_cls=Box)
    assert outcome.model.value == 2
    assert provider.call_count == 2, "repair must be exactly one extra pass"
    assert outcome.first_pass_valid is False
    assert outcome.repair_used is True
    # the repair pass carries the invalid output + the schema back to the model
    repair_messages = provider.calls[1]["messages"]
    assert repair_messages[:2] == MESSAGES
    assert repair_messages[2]["role"] == "assistant"
    assert "not json at all" in repair_messages[2]["content"]
    assert repair_messages[3]["role"] == "user"
    assert "schema" in repair_messages[3]["content"]


def test_fenced_json_is_recovered_without_repair() -> None:
    fenced = "```json\n" + json.dumps({"value": 7}) + "\n```"
    provider = ScriptedProvider([scripted_text(fenced)])
    outcome = generate_and_parse(provider, messages=MESSAGES, model_cls=Box)
    assert outcome.model.value == 7
    assert provider.call_count == 1


def test_pydantic_failure_triggers_the_single_repair() -> None:
    provider = ScriptedProvider([scripted_result({"wrong_key": 1}), _box_result(3)])
    outcome = generate_and_parse(provider, messages=MESSAGES, model_cls=Box)
    assert outcome.model.value == 3
    assert provider.call_count == 2


def test_still_invalid_after_repair_raises_controlled_failure() -> None:
    provider = ScriptedProvider([scripted_text("bad"), scripted_text("still bad")])
    with pytest.raises(GenerationFailure, match="single repair pass"):
        generate_and_parse(provider, messages=MESSAGES, model_cls=Box)
    assert provider.call_count == 2, "no loop: generate once + one repair, then stop"


def test_parse_model_output_reports_reasons() -> None:
    model, reason = parse_model_output("garbage", Box)
    assert model is None
    assert reason is not None and "json" in reason.lower()
    model, reason = parse_model_output('{"value": "nope"}', Box)
    assert model is None
    assert reason is not None and "validation failed" in reason
