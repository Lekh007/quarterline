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


class TestFilingVersionsVsRevisions:
    """IND-3 Task 6: versions and revisions are different things."""

    def version(
        self, content_hash, period_end, published, revision="Original", scope="consolidated"
    ):
        from quarterline.sources.india.revisions import FilingVersion

        return FilingVersion(
            content_hash=content_hash,
            scope=scope,
            period_end=period_end,
            published_at=published,
            revision_status=revision,
        )

    def test_identical_content_is_a_duplicate_copy_never_a_revision(self):
        from quarterline.sources.india.revisions import VERSION_DUPLICATE_COPY, classify_filing_pair

        original = self.version("abc", date(2026, 6, 30), date(2026, 7, 23))
        copy = self.version("abc", date(2026, 6, 30), date(2026, 7, 30))
        assert classify_filing_pair(original, copy) == VERSION_DUPLICATE_COPY

    def test_two_originals_for_one_period_stay_original(self):
        from quarterline.sources.india.revisions import VERSION_ORIGINAL, classify_filing_pair

        first = self.version("abc", date(2026, 6, 30), date(2026, 7, 23))
        second = self.version("def", date(2026, 6, 30), date(2026, 7, 24))
        assert classify_filing_pair(first, second) == VERSION_ORIGINAL

    def test_explicitly_declared_revision_is_revised(self):
        from quarterline.sources.india.revisions import VERSION_REVISED, classify_filing_pair

        original = self.version("abc", date(2026, 6, 30), date(2026, 7, 23))
        revised = self.version("def", date(2026, 6, 30), date(2026, 8, 10), revision="Revised")
        assert classify_filing_pair(original, revised) == VERSION_REVISED
        # order does not change the classification
        assert classify_filing_pair(revised, original) == VERSION_REVISED

    def test_later_comparative_is_not_a_revision(self):
        """The Jun-2026 filing re-reporting the Mar-2026 quarter as a comparative
        is NOT a revision of the Mar-2026 filing."""
        from quarterline.sources.india.revisions import (
            VERSION_LATER_COMPARATIVE,
            classify_filing_pair,
        )

        q4_filing = self.version("q4", date(2026, 3, 31), date(2026, 4, 23))
        q1_filing = self.version("q1", date(2026, 6, 30), date(2026, 7, 23))
        assert classify_filing_pair(q4_filing, q1_filing) == VERSION_LATER_COMPARATIVE
        assert classify_filing_pair(q1_filing, q4_filing) == VERSION_LATER_COMPARATIVE

    def test_scope_difference_is_not_a_revision(self):
        from quarterline.sources.india.revisions import (
            VERSION_LATER_COMPARATIVE,
            classify_filing_pair,
        )

        consolidated = self.version("a", date(2026, 6, 30), date(2026, 7, 23))
        standalone = self.version("b", date(2026, 6, 30), date(2026, 7, 23), scope="standalone")
        assert classify_filing_pair(consolidated, standalone) == VERSION_LATER_COMPARATIVE

    def test_real_fixture_filings_are_all_originals(self):
        """The four acquired filings classify as originals; none is a revision."""
        from india_test_helpers import load_india_manifest

        from quarterline.sources.india.revisions import (
            VERSION_LATER_COMPARATIVE,
            VERSION_ORIGINAL,
            classify_filing_pair,
        )

        manifest = load_india_manifest()
        versions = [
            self.version(
                entry["sha256"],
                date.fromisoformat(manifest["periods"][entry["period"]]["period_end"]),
                date.fromisoformat(entry["exchange_filing"]["broadcast_ist"].split(" ")[0]),
                revision=entry["exchange_filing"].get("revision"),
                scope=entry["scope"],
            )
            for entry in manifest["committed_fixtures"]
        ]
        for other in versions[1:]:
            relationship = classify_filing_pair(versions[0], other)
            assert relationship in (VERSION_ORIGINAL, VERSION_LATER_COMPARATIVE)
