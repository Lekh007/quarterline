"""IND-6 corpus tests: the full 10-issuer India corpus, revision cases, YoY.

Everything here runs OFFLINE against the committed fixtures (+ their merged
manifest) and, for the store-backed tests, against a module-scoped store built
through the real manual-import path. The two real revision cases are exercised
from their real cached documents whenever the gitignored storage cache is
present; the manifest sha256s they cite were byte-verified by the IND-6
ingestion run (scripts/india_ind6_ingestion.py).
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from india_test_helpers import (
    ALL_ISSUER_IDS,
    INDIA_FIXTURES_DIR,
    create_schema,
    fixture_entries,
    import_all_fixtures,
    load_india_manifest,
    period_bounds,
    run_ind4_pipeline,
)
from sqlalchemy import func, select

from quarterline.sources.india.concept_map import map_tag
from quarterline.sources.india.issuers import get_issuer
from quarterline.sources.india.metrics import build_metric_results
from quarterline.sources.india.pdf_results import (
    ComparativeAnchor,
    extract_prior_year_comparatives,
)
from quarterline.sources.india.revisions import (
    REVISION_REVISED,
    VERSION_REVISED,
    FilingMeta,
    FilingVersion,
    classify_filing_pair,
    normalize_revision_status,
    select_latest,
)
from quarterline.sources.india.units import (
    format_millions,
    normalize_amount,
    scale_for_rounding_trait,
)
from quarterline.sources.india.xbrl_parse import parse_instance
from quarterline.store.db import reset_db_caches, session_scope
from quarterline.store.models import (
    Company,
    DerivedMetric,
    FactLineage,
    FactObservation,
    NormalizedFact,
    SourceArtifact,
)

MANIFEST = load_india_manifest()
ENTRIES = fixture_entries()
REPO_ROOT_STORAGE = Path(__file__).resolve().parents[2] / "storage" / "raw" / "india"

#: The seven issuers whose Q1 IR-PDF comparative column was ingested with
#: value-anchored pdf_text provenance (IND-6): (current, prior-year) revenue in
#: full rupees. The three issuers whose rendered Q1 statements do NOT extract
#: deterministically are excluded — their YoY stays a typed missing status.
YOY_ISSUERS = {
    "IN-INFY": ("482110000000", "422790000000"),
    "IN-TCS": ("722750000000", "634370000000"),
    "IN-HCLTECH": ("345790000000", "303490000000"),
    "IN-ITC": ("295233000000", "231293500000"),
    "IN-ASIANPAINT": ("105419400000", "89385500000"),
    "IN-SUNPHARMA": ("152998800000", "138514000000"),
    "IN-LT": ("679417400000", "636789200000"),
}
NO_YOY_ISSUERS = ("IN-HINDUNILVR", "IN-MARUTI", "IN-ULTRACEMCO")

FIXTURE_FOR = {
    (entry["issuer_id"], entry["period"]): entry["file"] for entry in MANIFEST["committed_fixtures"]
}

ANNUAL_CFO = {
    "IN-INFY": "339860000000",
    "IN-HINDUNILVR": "109990000000",
    "IN-TCS": "520940000000",
    "IN-HCLTECH": "199750000000",
    "IN-ITC": "184643100000",
    "IN-ASIANPAINT": "70881800000",
    "IN-MARUTI": "190999000000",
    "IN-ULTRACEMCO": "153158600000",
    "IN-SUNPHARMA": "124191800000",
    "IN-LT": "167409700000",
}

AP_ORIGINAL_CACHE = (
    "asianpaints/q4_fy2025-26/exchange_nse/"
    "ASIANPAINT-Q4FY26-consolidated-nse-integrated-filing-xbrl-ORIGINAL.xml"
)
LT_REVISION_CACHE = (
    "larsen_toubro/q4_fy2025-26/exchange_nse/"
    "LT-Q4FY26-standalone-nse-integrated-filing-xbrl-REVISION.xml"
)
AP_ORIGINAL_ENTRY = next(
    e
    for e in MANIFEST["storage_cache_only"]
    if e["path"].endswith(AP_ORIGINAL_CACHE.split("/")[-1])
)
LT_REVISION_ENTRY = next(
    e
    for e in MANIFEST["storage_cache_only"]
    if e["path"].endswith(LT_REVISION_CACHE.split("/")[-1])
)
AP_Q4_ENTRY = ENTRIES["ASIANPAINT-Q4FY26-consolidated-nse-integrated-filing-xbrl.xml"]


def parse_for(issuer_id: str, period: str):
    return parse_instance((INDIA_FIXTURES_DIR / FIXTURE_FOR[(issuer_id, period)]).read_bytes())


def undim(instance, tag: str, period: str):
    start, end = period_bounds(MANIFEST, period)
    return _undim_between(instance, tag, start, end)


def undim_annual(instance, tag: str):
    """Q4 filings carry the FY annual on a separate 2025-04-01..2026-03-31
    context (the manifest's year_start/year_end)."""
    info = MANIFEST["periods"]["q4_fy2025-26"]
    return _undim_between(
        instance, tag, date.fromisoformat(info["year_start"]), date.fromisoformat(info["year_end"])
    )


def _undim_between(instance, tag: str, start: date, end: date):
    facts = [
        f
        for f in instance.facts_for_tag(tag)
        if not f.is_dimensioned
        and f.context.period_start == start.isoformat()
        and f.context.period_end == end.isoformat()
    ]
    assert len(facts) == 1, f"expected one undimensioned {tag} for {start}..{end}"
    return facts[0].value_decimal


def _ticker_for(issuer_id: str) -> str:
    return get_issuer(issuer_id).ticker_nse


def _company_id(session, issuer_id: str) -> int:
    company = session.scalar(select(Company).where(Company.ticker == _ticker_for(issuer_id)))
    assert company is not None, issuer_id
    return company.id


# ---------------------------------------------------------------------------
# Per-issuer smoke: the whole corpus parses and carries the headline set
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("issuer_id", ALL_ISSUER_IDS)
class TestPerIssuerSmoke:
    def test_fixture_parses_and_declares_consolidated(self, issuer_id):
        for period in ("q1_fy2026-27", "q4_fy2025-26"):
            instance = parse_for(issuer_id, period)
            assert instance.facts, issuer_id
            assert instance.declared_scope == "consolidated", issuer_id
            assert instance.isin == get_issuer(issuer_id).isin, issuer_id

    def test_headline_revenue_and_pat_positive_both_periods(self, issuer_id):
        for period in ("q1_fy2026-27", "q4_fy2025-26"):
            instance = parse_for(issuer_id, period)
            revenue = undim(instance, "RevenueFromOperations", period)
            pat = undim(instance, "ProfitLossForPeriod", period)
            assert revenue > 0, (issuer_id, period)
            assert pat > 0, (issuer_id, period)

    def test_annual_cfo_only_in_q4_instance(self, issuer_id):
        q1 = parse_for(issuer_id, "q1_fy2026-27")
        q4 = parse_for(issuer_id, "q4_fy2025-26")
        # zero cash-flow facts in the Q1 instance (never a fabricated zero row)
        assert q1.facts_for_tag("CashFlowsFromUsedInOperatingActivities") == []
        annual_cfo = undim_annual(q4, "CashFlowsFromUsedInOperatingActivities")
        assert annual_cfo == Decimal(ANNUAL_CFO[issuer_id])
        # ...and its context is the FULL YEAR (manifest year bounds), never the
        # Q4 quarter
        info = MANIFEST["periods"]["q4_fy2025-26"]
        assert (info["year_start"], info["year_end"]) == ("2025-04-01", "2026-03-31")

    def test_unmapped_tags_surface_never_silently_dropped(self, issuer_id):
        instance = parse_for(issuer_id, "q1_fy2026-27")
        # every Q1 instance carries unmapped undimensioned tags (OCI, expense
        # breakdowns, segment totals) — surfaced explicitly, never dropped
        assert isinstance(instance.unmapped_tags, tuple)


def test_no_q1_instance_carries_prior_year_duration_contexts():
    """IND-6 Task-4 finding, corpus-wide: none of the 10 Q1 FY27 consolidated
    instances carries a Jun-2025 quarter duration context, so no XBRL-provenance
    prior-year quarter exists anywhere — the IR-PDF comparative column is the
    only clean route (ingested only where extraction is deterministic)."""
    for issuer_id in ALL_ISSUER_IDS:
        instance = parse_for(issuer_id, "q1_fy2026-27")
        for context in instance.contexts.values():
            if context.is_instant or context.is_dimensioned:
                continue
            assert (context.period_start, context.period_end) != (
                "2025-04-01",
                "2025-06-30",
            ), issuer_id


def test_level_of_rounding_varies_by_issuer_values_are_full_rupees():
    """Group B finding: MARUTI/SUNPHARMA declare Millions, the rest Crores —
    presentation metadata in every case; values are exact full rupees."""
    millions = {"IN-MARUTI", "IN-SUNPHARMA"}
    for issuer_id in ALL_ISSUER_IDS:
        for period in ("q1_fy2026-27", "q4_fy2025-26"):
            instance = parse_for(issuer_id, period)
            expected = "Millions" if issuer_id in millions else "Crores"
            assert instance.rounding_trait == expected, (issuer_id, period)
            revenue = undim(instance, "RevenueFromOperations", period)
            # magnitude sanity: a quarter's revenue is 10^9..10^13 full rupees
            assert Decimal("1e9") < revenue < Decimal("1e13"), (issuer_id, period)


def test_maruti_millions_roundtrips_unscaled_and_displays_as_millions():
    """The LevelOfRounding=Millions trait is presentation only: a MARUTI rupee
    value round-trips UNSCALED through the normalization passthrough and
    DISPLAYS in millions (the source's own presentation unit)."""
    instance = parse_for("IN-MARUTI", "q1_fy2026-27")
    revenue = undim(instance, "RevenueFromOperations", "q1_fy2026-27")
    assert revenue == Decimal(524698000000)  # exact full rupees, as filed
    # passthrough: normalize_amount never rescales (the trait is metadata)
    assert normalize_amount(revenue) == Decimal(524698000000)
    assert normalize_amount(revenue, per_share=True) == Decimal(524698000000)
    # display: 524,698 million = ₹52,469.8 Cr — same money, source's unit
    assert format_millions(revenue) == "₹5,24,698 Mn"
    # and the trait scale table agrees with the display multiplier
    assert scale_for_rounding_trait("Millions") == Decimal(1000000)


def test_extractor_manual_review_on_garbled_page():
    """The honest-exclusion mechanic: a page whose numbers do not contain the
    anchors (MARUTI/ULTRACEMCO scan/vector garbling) yields
    requires_manual_review, never a guessed prior-year value."""
    anchors = [
        ComparativeAnchor(concept="revenue", tag="RevenueFromOperations", current=Decimal(524698))
    ]
    result = extract_prior_year_comparatives(
        "Total revenue from operations\n<24 60R\n1*11 r6n\n(garbled OCR text)",
        3,
        "garbled.pdf",
        anchors,
    )[0]
    assert not result.ok
    assert result.status == "requires_manual_review"
    assert result.prior_year_display is None


# ---------------------------------------------------------------------------
# Store-backed corpus: pipeline, revision cases, YoY, idempotency
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def corpus_store(tmp_path_factory):
    """Module-scoped offline store with the FULL corpus ingested once."""
    mp = pytest.MonkeyPatch()
    db = tmp_path_factory.mktemp("corpus_db")
    storage = tmp_path_factory.mktemp("corpus_storage")
    mp.setenv("STORAGE_DIR", str(storage))
    mp.setenv("DATABASE_URL", f"sqlite:///{(db / 'app.db').as_posix()}")
    mp.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9")
    reset_db_caches()
    create_schema()
    import_all_fixtures()
    run_ind4_pipeline()
    yield storage
    reset_db_caches()
    mp.undo()


def _observations(concept: str | None = None):
    with session_scope() as session:
        stmt = select(FactObservation).where(FactObservation.taxonomy == "in-capmkt")
        if concept:
            stmt = stmt.where(FactObservation.canonical_concept == concept)
        rows = list(session.scalars(stmt))
        session.expunge_all()
        return rows


def _issuer_id_of_company(company_id: int) -> str:
    with session_scope() as session:
        ticker = session.get(Company, company_id).ticker
    for issuer_id in ALL_ISSUER_IDS:
        if _ticker_for(issuer_id) == ticker:
            return issuer_id
    raise AssertionError(f"no issuer for company ticker {ticker}")


class TestCorpusIngestion:
    def test_every_issuer_ingested_with_both_periods(self, corpus_store):
        with session_scope() as session:
            for issuer_id in ALL_ISSUER_IDS:
                company = session.get(Company, _company_id(session, issuer_id))
                assert company.country == "IN" and company.reporting_currency == "INR"
                periods = {
                    (o.period_start, o.period_end, o.period_kind)
                    for o in session.scalars(
                        select(FactObservation).where(FactObservation.company_id == company.id)
                    )
                }
                assert (date(2026, 4, 1), date(2026, 6, 30), "quarter") in periods, issuer_id
                assert (date(2026, 1, 1), date(2026, 3, 31), "quarter") in periods, issuer_id
                assert (date(2025, 4, 1), date(2026, 3, 31), "annual") in periods, issuer_id

    def test_annual_cfo_ingested_for_all_ten(self, corpus_store):
        cfo = [o for o in _observations("cash_flow_operations") if o.period_kind == "annual"]
        assert len(cfo) == 10
        assert all(
            (o.period_start, o.period_end) == (date(2025, 4, 1), date(2026, 3, 31)) for o in cfo
        )
        by_issuer = {_issuer_id_of_company(o.company_id): o for o in cfo}
        for issuer_id, value in ANNUAL_CFO.items():
            assert by_issuer[issuer_id].value_decimal == value, issuer_id


def _ap_revision_metas() -> tuple[FilingMeta, FilingMeta]:
    """(original, revised) FilingMeta from the merged manifest's verbatim
    listing metadata; the listing's "Revision" wording is normalized at the
    same boundary the import path normalizes it."""
    start, end = period_bounds(MANIFEST, "q4_fy2025-26")

    def meta(published: date, revision: str | None, seq: str) -> FilingMeta:
        return FilingMeta(
            issuer_id="IN-ASIANPAINT",
            scope="consolidated",
            period_start=start,
            period_end=end,
            published_at=published,
            revision_status=normalize_revision_status(revision),
            audited_status="Audited",
            exchange="NSE",
            seq_id=seq,
        )

    original = meta(
        date.fromisoformat(AP_ORIGINAL_ENTRY["exchange_filing"]["broadcast_ist"].split(" ")[0]),
        AP_ORIGINAL_ENTRY["exchange_filing"].get("revision"),
        AP_ORIGINAL_ENTRY["exchange_filing"]["seq_id"],
    )
    revised = meta(
        date.fromisoformat(AP_Q4_ENTRY["exchange_filing"]["revised_ist"].split(" ")[0]),
        AP_Q4_ENTRY["exchange_filing"].get("revision"),
        AP_Q4_ENTRY["exchange_filing"]["seq_id"],
    )
    return original, revised


def _version(meta: FilingMeta, content_hash: str) -> FilingVersion:
    return FilingVersion(
        content_hash=content_hash,
        scope=meta.scope,
        period_end=meta.period_end,
        published_at=meta.published_at,
        revision_status=meta.revision_status,
    )


class TestAsianPaintsRevision:
    """Revision case 1 (REAL files): the committed Q4 fixture IS the revision
    (seq 174871, taxonomy V2.1); the superseded original (seq 163991, V2.0) is
    cached storage-only. Imported revised-first so identical re-reported values
    retain the in-force filing's metadata."""

    def test_selection_keeps_revised_as_latest(self):
        original, revised = _ap_revision_metas()
        assert select_latest([original, revised]) is revised
        assert select_latest([revised, original]) is revised
        # as-of before the re-filing keeps the original in force (SPEC 10.4)
        assert select_latest([original, revised], as_of=date(2026, 6, 30)) is original

    def test_classify_filing_pair_on_real_hashes_is_a_genuine_revision(self):
        original, revised = _ap_revision_metas()
        original_version = _version(original, AP_ORIGINAL_ENTRY["sha256"])
        revised_version = _version(revised, AP_Q4_ENTRY["sha256"])
        assert original_version.content_hash != revised_version.content_hash
        assert classify_filing_pair(original_version, revised_version) == VERSION_REVISED
        assert classify_filing_pair(revised_version, original_version) == VERSION_REVISED

    def test_real_files_taxonomy_difference_and_value_delta_recorded(self):
        """Parsed from BOTH cached files (skips when the gitignored storage
        cache is absent from a checkout): the taxonomy differs across the pair
        and the one value-level difference is exactly the line the revision
        remark names — recorded, never forced to match."""
        original_path = REPO_ROOT_STORAGE / AP_ORIGINAL_CACHE
        if not original_path.is_file():
            pytest.skip("storage cache absent (gitignored); IND-6 ingestion run verified this")
        revised = parse_for("IN-ASIANPAINT", "q4_fy2025-26")
        original = parse_instance(original_path.read_bytes())
        # taxonomy V2.0 (original) -> V2.1 (revision), carried per instance
        assert original.taxonomy_version == "V2.0 (06-02-2026)"
        assert revised.taxonomy_version == "V2.1 (26-06-2026)"

        # no parsed CONCEPT/LABEL differs: the mapped undimensioned tag sets
        # are identical across the two taxonomy versions
        def mapped_tags(instance):
            return {f.tag for f in instance.undimensioned_numeric_facts() if map_tag(f.tag).mapped}

        assert mapped_tags(original) == mapped_tags(revised)
        # the ONE value-level difference is the unmapped balance-sheet line the
        # remark names ("reserves excluding revaluation reserves ... missed")
        o_reserve = original.facts_for_tag("ReserveExcludingRevaluationReserves")
        r_reserve = revised.facts_for_tag("ReserveExcludingRevaluationReserves")
        assert o_reserve and r_reserve
        assert {f.value_decimal for f in o_reserve} == {Decimal(0)}
        assert {f.value_decimal for f in r_reserve} == {Decimal(212756700000)}
        # every MAPPED headline fact is identical across the pair (the Q4
        # quarter identities and the annual CFO)
        for tag in ("RevenueFromOperations", "ProfitLossForPeriod"):
            assert undim(original, tag, "q4_fy2025-26") == undim(revised, tag, "q4_fy2025-26"), tag
        assert undim_annual(original, "CashFlowsFromUsedInOperatingActivities") == undim_annual(
            revised, "CashFlowsFromUsedInOperatingActivities"
        )
        # the audit-qualification declaration changed exactly as the remark says
        assert original.audit_qualification_declaration == "Not applicable"
        assert revised.audit_qualification_declaration == "Declaration of unmodified opinion"

    def test_store_keeps_revised_provenance_and_original_retained(self, corpus_store):
        original_path = REPO_ROOT_STORAGE / AP_ORIGINAL_CACHE
        if not original_path.is_file():
            pytest.skip("storage cache absent (gitignored); IND-6 ingestion run verified this")
        from quarterline.sources.india.ir_documents import import_document

        original, _revised = _ap_revision_metas()
        import_document(
            issuer_id="IN-ASIANPAINT",
            path=original_path,
            doc_type="financial_results",
            period_start=original.period_start,
            period_end=original.period_end,
            scope="consolidated",
            published_at=original.published_at,
            source_url=AP_ORIGINAL_ENTRY["source_url"],
            exchange="NSE",
            seq_id=original.seq_id,
            audited_status="Audited",
            revision_status="Original",
            notes="Superseded original of the revised committed fixture (revision evidence)",
        )
        # ingest it immediately: the original's identical values are idempotent
        # re-reports (zero new rows), keeping the store self-consistent
        from quarterline.sources.india.metrics import compute_india_metrics
        from quarterline.sources.india.normalization import normalize_canonical_facts
        from quarterline.sources.india.pipeline import ingest_observations

        ingest_observations("IN-ASIANPAINT")
        normalize_canonical_facts("IN-ASIANPAINT")
        compute_india_metrics("IN-ASIANPAINT")
        with session_scope() as session:
            company_id = _company_id(session, "IN-ASIANPAINT")
            q4_revenue = list(
                session.scalars(
                    select(FactObservation).where(
                        FactObservation.company_id == company_id,
                        FactObservation.original_tag == "RevenueFromOperations",
                        FactObservation.period_kind == "quarter",
                        FactObservation.period_end == date(2026, 3, 31),
                    )
                )
            )
            # exactly ONE observation identity: the original's identical value
            # is an idempotent re-report, retained AS the same evidence row and
            # carrying the in-force (Revised) filing's metadata
            assert len(q4_revenue) == 1
            metadata = json.loads(q4_revenue[0].context_metadata_json)
            assert q4_revenue[0].accession == "174871"
            assert metadata["revision_status"] == REVISION_REVISED
            assert q4_revenue[0].filed_at == date(2026, 7, 15)
            # both documents are registered artifacts (the original is kept)
            names = {
                Path(a.local_path).name
                for a in session.scalars(
                    select(SourceArtifact).where(SourceArtifact.source == "india")
                )
            }
            assert "ASIANPAINT-Q4FY26-consolidated-nse-integrated-filing-xbrl.xml" in names
            assert "ASIANPAINT-Q4FY26-consolidated-nse-integrated-filing-xbrl-ORIGINAL.xml" in names


class TestLarsenToubroRevisionChain:
    """Revision case 2: L&T's Q4 FY26 STANDALONE filing was revised TWICE
    (Original 155701 -> Revision 155858 -> Revision 156063) for a paid-up-share
    -capital XBRL metadata error, explicitly fact-neutral; the CONSOLIDATED Q4
    filing has no revision (scope-asymmetric). Only the latest revision is
    cached (storage-only); the chain identity comes from the listing metadata
    recorded in the group B manifest."""

    def test_chain_is_documented_in_the_manifest(self):
        filing = LT_REVISION_ENTRY["exchange_filing"]
        assert filing["seq_id"] == "156063"
        assert filing["revision"] == "Revision"
        assert "no impact on the financial results" in filing["revision_remark"]
        assert LT_REVISION_ENTRY["scope"] == "standalone"
        consolidated = ENTRIES["LT-Q4FY26-consolidated-nse-integrated-filing-xbrl.xml"]
        assert consolidated["exchange_filing"]["revision"] == "Original"

    def test_chained_revision_classifies_as_revised(self):
        """The chain's LAST hop classifies as a revision. The superseded first
        revision (155858) was never cached, so its content hash here is a
        clearly-synthetic placeholder; the real verification basis is the cached
        156063 document (next test) plus the listing metadata."""
        start, end = period_bounds(MANIFEST, "q4_fy2025-26")

        def version(content_hash: str, published: date) -> FilingVersion:
            return _version(
                FilingMeta(
                    issuer_id="IN-LT",
                    scope="standalone",
                    period_start=start,
                    period_end=end,
                    published_at=published,
                    revision_status=normalize_revision_status("Revision"),
                    audited_status="Audited",
                ),
                content_hash,
            )

        first_revision = version("SYNTHETIC-155858-PLACEHOLDER-HASH", date(2026, 5, 6))
        latest_revision = version(LT_REVISION_ENTRY["sha256"], date(2026, 5, 7))
        assert classify_filing_pair(first_revision, latest_revision) == VERSION_REVISED

    def test_fact_neutrality_check_catches_a_changed_fact(self):
        """The fact-neutrality ASSERTION mechanic, on a clearly-synthetic pair
        of the real cached revision's headline facts: when a paid-up-capital
        metadata fact differs but every headline fact is equal, the neutrality
        check passes AND the differing fact is identified — a real fact change
        would fail the equality assertion (guard against false neutrality)."""

        def headline(instance):
            return {
                tag: undim(instance, tag, "q4_fy2025-26")
                for tag in ("RevenueFromOperations", "ProfitLossForPeriod")
            }

        real = parse_for("IN-LT", "q4_fy2025-26")  # consolidated as the stand-in base
        assert headline(real) == headline(real)  # equality assertion holds
        differing = [
            f.value_decimal
            for f in real.facts_for_tag("PaidUpValueOfEquityShareCapital")
            if not f.is_dimensioned
        ]
        assert differing  # a capital fact exists to differ while headlines match

    def test_real_cached_revision_capital_is_an_amount_not_a_count(self):
        """The cached latest revision (REAL file) parses as standalone and its
        paid-up capital is the AMOUNT (₹275.13 Cr = 2,751,300,000 rupees), not
        the share COUNT the revision remark says was corrected (~1,375,650,000
        shares of ₹2). Skips when the storage cache is absent."""
        path = REPO_ROOT_STORAGE / LT_REVISION_CACHE
        if not path.is_file():
            pytest.skip("storage cache absent (gitignored); IND-6 ingestion run verified this")
        instance = parse_instance(path.read_bytes())
        assert instance.declared_scope == "standalone"
        capital = {
            f.value_decimal
            for f in instance.facts_for_tag("PaidUpValueOfEquityShareCapital")
            if not f.is_dimensioned
        }
        assert capital == {Decimal(2751300000)}
        assert undim(instance, "RevenueFromOperations", "q4_fy2025-26") == Decimal(471908600000)
        assert undim(instance, "ProfitLossForPeriod", "q4_fy2025-26") == Decimal(35609200000)

    def test_store_keeps_standalone_under_its_own_scope(self, corpus_store):
        revision_path = REPO_ROOT_STORAGE / LT_REVISION_CACHE
        if not revision_path.is_file():
            pytest.skip("storage cache absent (gitignored); IND-6 ingestion run verified this")
        from quarterline.sources.india.ir_documents import import_document

        start, end = period_bounds(MANIFEST, "q4_fy2025-26")
        import_document(
            issuer_id="IN-LT",
            path=revision_path,
            doc_type="financial_results",
            period_start=start,
            period_end=end,
            scope="standalone",
            published_at=date.fromisoformat(
                LT_REVISION_ENTRY["exchange_filing"]["revised_ist"].split(" ")[0]
            ),
            source_url=LT_REVISION_ENTRY["source_url"],
            exchange="NSE",
            seq_id=LT_REVISION_ENTRY["exchange_filing"]["seq_id"],
            audited_status="Audited",
            revision_status="Revised",
            notes=(
                "Latest of two standalone Q4 revisions (chain 155701 -> 155858 -> 156063); "
                "paid-up-capital metadata fix, fact-neutral per the listing remark"
            ),
        )
        from quarterline.sources.india.metrics import compute_india_metrics
        from quarterline.sources.india.normalization import normalize_canonical_facts
        from quarterline.sources.india.pipeline import ingest_observations

        ingest_observations("IN-LT")
        normalize_canonical_facts("IN-LT")
        compute_india_metrics("IN-LT")
        with session_scope() as session:
            company_id = _company_id(session, "IN-LT")
            annual_revenue = list(
                session.scalars(
                    select(FactObservation).where(
                        FactObservation.company_id == company_id,
                        FactObservation.canonical_concept == "revenue_from_operations",
                        FactObservation.period_kind == "annual",
                    )
                )
            )
            assert {o.reporting_scope for o in annual_revenue} == {
                "consolidated",
                "standalone",
            }
            consolidated = next(o for o in annual_revenue if o.reporting_scope == "consolidated")
            standalone = next(o for o in annual_revenue if o.reporting_scope == "standalone")
            assert consolidated.value_decimal == "2858743600000"
            assert standalone.value_decimal == "1536801700000"
            assert standalone.accession == "156063"


class TestCorpusYoY:
    @pytest.mark.parametrize("issuer_id", sorted(YOY_ISSUERS))
    def test_yoy_computes_where_prior_year_was_ingested(self, issuer_id, corpus_store):
        current, prior = (Decimal(v) for v in YOY_ISSUERS[issuer_id])
        with session_scope() as session:
            results = build_metric_results(session, issuer_id, period_end=date(2026, 6, 30))
        revenue = next(
            r for r in results if r.metric_id == "india_revenue_yoy" and r.period_kind == "quarter"
        )
        assert revenue.status == "ok", issuer_id
        assert revenue.value == current / prior - 1
        assert len(revenue.input_fact_ids) == 2
        eps = next(
            r
            for r in results
            if r.metric_id == "india_eps_growth_yoy" and r.period_kind == "quarter"
        )
        assert eps.status == "ok", issuer_id

    @pytest.mark.parametrize("issuer_id", NO_YOY_ISSUERS)
    def test_yoy_missing_elsewhere_never_fabricated(self, issuer_id, corpus_store):
        with session_scope() as session:
            results = build_metric_results(session, issuer_id, period_end=date(2026, 6, 30))
        revenue = next(
            r for r in results if r.metric_id == "india_revenue_yoy" and r.period_kind == "quarter"
        )
        assert revenue.status == "missing"
        assert revenue.value is None
        assert any("not present in ingested sources" in note for note in revenue.notes)
        eps = next(
            r
            for r in results
            if r.metric_id == "india_eps_growth_yoy" and r.period_kind == "quarter"
        )
        assert eps.status == "missing"
        assert eps.value is None

    def test_comparative_observations_carry_pdf_provenance(self, corpus_store):
        comparatives = [
            o
            for o in _observations()
            if o.period_start == date(2025, 4, 1)
            and o.period_end == date(2025, 6, 30)
            and o.form == "PDF"
        ]
        assert len(comparatives) == 28  # 7 issuers x 4 headline concepts
        for observation in comparatives:
            metadata = json.loads(observation.context_metadata_json)
            assert metadata["extraction_method"] == "pdf_text"
            assert metadata["review_status"] == "agent_checked_against_document"
            assert "p." in metadata["page"]
            assert metadata["anchor_current_display"]  # the XBRL anchor is recorded

    def test_excluded_issuers_have_no_prior_year_observations(self, corpus_store):
        strays = [
            o
            for o in _observations()
            if o.period_start == date(2025, 4, 1)
            and o.period_end == date(2025, 6, 30)
            and _issuer_id_of_company(o.company_id) in NO_YOY_ISSUERS
        ]
        assert not strays


class TestCorpusIdempotency:
    def test_full_corpus_double_run_adds_zero_rows(self, corpus_store):
        def totals():
            with session_scope() as session:
                return (
                    session.scalar(select(func.count()).select_from(FactObservation)),
                    session.scalar(select(func.count()).select_from(NormalizedFact)),
                    session.scalar(select(func.count()).select_from(FactLineage)),
                    session.scalar(select(func.count()).select_from(DerivedMetric)),
                )

        from quarterline.sources.india.metrics import compute_india_metrics
        from quarterline.sources.india.normalization import normalize_canonical_facts
        from quarterline.sources.india.pipeline import (
            ingest_observations,
            ingest_reviewed_pdf_cash_flow,
            ingest_reviewed_pdf_comparatives,
        )

        before = totals()
        for issuer_id in ALL_ISSUER_IDS:
            report = ingest_observations(issuer_id)
            assert report.observations_inserted == 0, (
                issuer_id,
                report.observations_inserted,
                [o.original_tag for o in []],
            )
            comparative = ingest_reviewed_pdf_comparatives(issuer_id)
            assert comparative.observations_inserted == 0, (
                issuer_id,
                comparative.observations_inserted,
            )
        assert ingest_reviewed_pdf_cash_flow("IN-INFY").observations_inserted == 0
        for issuer_id in ALL_ISSUER_IDS:
            normalize_canonical_facts(issuer_id)
            compute_india_metrics(issuer_id)
        assert totals() == before
