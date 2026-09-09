"""Context assembly tests (SPEC §16): passage count/token caps, budget with
headroom, span deduplication, and source diversity."""

from __future__ import annotations

from quarterline.config import Settings
from quarterline.retrieve.context import (
    MAX_PASSAGE_TOKENS,
    MAX_PASSAGES,
    assemble_context,
    context_budget_from_settings,
)
from quarterline.retrieve.models import EvidenceItem


def _item(
    item_id: int,
    document_id: int,
    text: str,
    start: int = 0,
    score: float = 1.0,
) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=f"ev-{item_id:012d}",
        document_id=document_id,
        start_offset=start,
        end_offset=start + len(text),
        text=text,
        scores={"rrf": score},
        chunk_id=item_id,
        strategy="fixed",
    )


def _text(words: int) -> str:
    return " ".join(f"w{n}" for n in range(words))


def test_budget_and_caps_enforced() -> None:
    items = [_item(i, 1, _text(120), score=1.0 / i) for i in range(1, 12)]
    bundle = assemble_context(items, budget_tokens=10_000)
    assert len(bundle.passages) <= MAX_PASSAGES
    for passage in bundle.passages:
        assert passage.token_count <= MAX_PASSAGE_TOKENS
    assert bundle.total_tokens <= bundle.effective_budget_tokens <= bundle.budget_tokens


def test_total_budget_stops_selection_with_drop_accounting() -> None:
    items = [_item(i, i, _text(150)) for i in range(1, 7)]  # six docs, ~150 tok each
    bundle = assemble_context(items, budget_tokens=320)
    assert bundle.total_tokens <= int(320 * 0.9)  # 10% estimator headroom
    assert bundle.dropped_for_budget >= 1
    assert bundle.passages  # some evidence still fits


def test_overlapping_spans_from_one_document_deduplicate() -> None:
    text = _text(60)
    items = [
        _item(1, 1, text, start=0, score=0.9),
        _item(2, 1, text, start=10, score=0.8),  # overlaps the first span
    ]
    bundle = assemble_context(items, budget_tokens=5_000)
    assert len(bundle.passages) == 1
    assert bundle.passages[0].evidence_id == "ev-000000000001"  # higher score kept
    assert bundle.dropped_for_overlap == 1


def test_source_diversity_prefers_a_second_document() -> None:
    doc1_a = _item(1, 1, _text(40), score=0.9)
    doc1_b = _item(2, 1, _text(40), start=100, score=0.85)
    doc2_a = _item(3, 2, _text(40), score=0.8)
    doc2_b = _item(4, 2, _text(40), start=100, score=0.7)
    bundle = assemble_context([doc1_a, doc1_b, doc2_a, doc2_b], budget_tokens=5_000)
    assert {p.document_id for p in bundle.passages} == {1, 2}
    assert bundle.passages[0].document_id == 1  # best item first
    assert bundle.passages[1].document_id == 2  # second passage = other source


def test_oversized_passages_are_skipped_whole_never_truncated() -> None:
    big = _item(1, 1, _text(MAX_PASSAGE_TOKENS * 4 + 100))  # ~2,700 tokens
    small = _item(2, 2, _text(50))
    bundle = assemble_context([big, small], budget_tokens=5_000)
    assert [p.evidence_id for p in bundle.passages] == ["ev-000000000002"]
    assert bundle.skipped_oversized == 1
    # The oversized text is NOT truncated into the bundle (resolvability).
    assert all(len(p.text) < MAX_PASSAGE_TOKENS * 4 for p in bundle.passages)


def test_passages_carry_resolvable_evidence_ids() -> None:
    items = [_item(i, 1, _text(30), start=i * 100) for i in range(1, 4)]
    bundle = assemble_context(items, budget_tokens=5_000)
    assert bundle.passages
    for passage in bundle.passages:
        assert passage.evidence_id.startswith("ev-")
        assert len(passage.evidence_id) == 15  # "ev-" + 12 hex


def test_context_budget_from_settings_reserves_output_and_headroom() -> None:
    settings = Settings(max_prompt_tokens=7500, max_output_tokens=900, _env_file=None)
    budget = context_budget_from_settings(settings)
    assert budget == int((7500 - 900) * 0.9)  # 5940: output reserved + headroom
