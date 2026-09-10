"""Revision-selection tests (IND-2): latest wins for latest_available; as_of excludes later.

The "Revised" duplicate below is CLEARLY SYNTHETIC (no revised submission existed
in the acquired filings — docs/india_source_audit.md §6.8); it exists only to
prove the selection policy discriminates. Both originals and revisions stay
retained in observations (asserted in test_india_pipeline).
"""

from __future__ import annotations

from datetime import date

import pytest

from quarterline.sources.india.revisions import (
    REVISION_ORIGINAL,
    REVISION_REVISED,
    FilingMeta,
    select_latest,
)


def filing(published: date, revision: str = REVISION_ORIGINAL, seq: str = "1") -> FilingMeta:
    return FilingMeta(
        issuer_id="IN-INFY",
        scope="consolidated",
        period_start=date(2026, 4, 1),
        period_end=date(2026, 6, 30),
        published_at=published,
        revision_status=revision,
        audited_status="Audited",
        seq_id=seq,
    )


class TestSelectLatest:
    def test_single_original(self):
        original = filing(date(2026, 7, 23))
        assert select_latest([original]) is original

    def test_synthetic_revised_wins_for_latest_available(self):
        original = filing(date(2026, 7, 23), seq="177385")
        revised = filing(date(2026, 8, 10), revision=REVISION_REVISED, seq="180001")
        assert select_latest([original, revised]) is revised

    def test_order_independent(self):
        original = filing(date(2026, 7, 23), seq="177385")
        revised = filing(date(2026, 8, 10), revision=REVISION_REVISED, seq="180001")
        assert select_latest([revised, original]) is revised

    def test_as_of_before_revision_returns_original(self):
        original = filing(date(2026, 7, 23), seq="177385")
        revised = filing(date(2026, 8, 10), revision=REVISION_REVISED, seq="180001")
        as_of = date(2026, 7, 31)
        assert select_latest([original, revised], as_of=as_of) is original

    def test_as_of_before_everything_returns_none(self):
        original = filing(date(2026, 7, 23))
        assert select_latest([original], as_of=date(2026, 1, 1)) is None

    def test_empty(self):
        assert select_latest([]) is None

    def test_mixed_periods_rejected(self):
        other_period = FilingMeta(
            issuer_id="IN-INFY",
            scope="consolidated",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 3, 31),
            published_at=date(2026, 4, 23),
            revision_status=REVISION_ORIGINAL,
            audited_status="Audited",
        )
        with pytest.raises(ValueError, match="one \\(scope, period\\)"):
            select_latest([filing(date(2026, 7, 23)), other_period])

    def test_policy_documented_no_instance_revision_field(self):
        # The revision status is carried from the listing, never inferred from
        # instance content: FilingMeta allows None (unknown stays unknown).
        unknown = filing(date(2026, 7, 23), revision=None)  # type: ignore[arg-type]
        assert unknown.revision_status is None
        assert select_latest([unknown]) is unknown
