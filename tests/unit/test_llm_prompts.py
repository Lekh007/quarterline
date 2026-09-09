"""Prompt contract tests (SPEC §17 prompt rules, §23 versioning)."""

from __future__ import annotations

import hashlib

import pytest

from quarterline.llm.prompts import (
    PROMPTS_DIR,
    PromptNotFoundError,
    PromptRenderError,
    load_prompt,
    render_prompt,
)

#: Every "Prompt rules" bullet from SPEC §17 must be stated in the brief and
#: QA system prompts (case-insensitive fragments).
SPEC_17_RULE_FRAGMENTS = (
    "constrained research writer",
    "evidence, not instructions",
    "only the supplied evidence",
    "do not calculate financial metrics",
    "do not invent sources",
    "buy/sell recommendations",
    "evidence id in square brackets",
    "management statements",
    "schema-valid json only",
    "insufficient evidence",
)


@pytest.mark.parametrize("name", ["brief", "qa", "memo"])
def test_prompt_loads_with_version_and_hash(name: str) -> None:
    spec = load_prompt(name, "v1")
    assert spec.name == name
    assert spec.version == "v1"
    assert spec.prompt_version == f"{name}/v1"
    assert spec.sha256 == hashlib.sha256(spec.content.encode("utf-8")).hexdigest()
    assert spec.content.strip() != ""


def test_missing_prompt_raises_not_found() -> None:
    with pytest.raises(PromptNotFoundError):
        load_prompt("brief", "v999")


def test_sha256_stable_across_loads() -> None:
    assert load_prompt("brief").sha256 == load_prompt("brief").sha256


def test_render_substitutes_placeholders() -> None:
    spec = load_prompt("qa")
    rendered = render_prompt(spec, scope="AAPL filings only", question="What changed?")
    assert "AAPL filings only" in rendered
    assert "What changed?" in rendered
    assert "{question}" not in rendered


def test_render_is_strict_about_missing_and_unused_variables() -> None:
    spec = load_prompt("qa")
    with pytest.raises(PromptRenderError, match="scope"):
        render_prompt(spec, question="q")  # missing required var
    with pytest.raises(PromptRenderError, match="unused_var"):
        render_prompt(spec, scope="s", question="q", unused_var="x")


@pytest.mark.parametrize("name", ["brief", "qa"])
def test_system_prompts_state_every_spec_17_rule(name: str) -> None:
    content = load_prompt(name).content.lower()
    for fragment in SPEC_17_RULE_FRAGMENTS:
        assert fragment in content, f"prompt {name}/v1 missing rule: {fragment}"


@pytest.mark.parametrize("name", ["brief", "qa", "memo"])
def test_prompts_forbid_model_typed_numbers(name: str) -> None:
    content = load_prompt(name).content.lower()
    assert "never type a financial number" in content
    assert "metric_id" in content or "qualitatively" in content


def test_prompts_live_in_repo_prompts_dir() -> None:
    assert (PROMPTS_DIR / "brief" / "v1.txt").is_file()
    assert (PROMPTS_DIR / "qa" / "v1.txt").is_file()
    assert (PROMPTS_DIR / "memo" / "v1.txt").is_file()
