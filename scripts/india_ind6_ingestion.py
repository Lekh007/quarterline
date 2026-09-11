"""IND-6 ingestion driver: the full 10-issuer corpus, revision cases included.

Run against the DEV store (default):   uv run python scripts/india_ind6_ingestion.py
Run against a throwaway scratch store: uv run python scripts/india_ind6_ingestion.py --scratch

Steps, in order (every step is idempotent; pass 2 must add zero rows):

1. Import the 20 committed consolidated fixtures (manifest order: the Asian
   Paints Q4 REVISED fixture precedes its superseded ORIGINAL so identical
   re-reported values retain the in-force filing's metadata).
2. Import the two storage-only revision-evidence documents:
   - Asian Paints Q4 consolidated ORIGINAL (seq 163991, revision=Original);
   - L&T Q4 standalone latest REVISION (seq 156063, revision=Revised; the
     documented chain is 155701 -> 155858 -> 156063, standalone-only).
3. Ingest observations per issuer (exchange XBRL), the reviewed INFY quarterly
   PDF cash flow, and the IND-6 reviewed prior-year PDF comparatives.
4. Normalize canonical facts and compute india-metrics-v1 for all 10 issuers.
5. Re-run everything and assert zero new rows at every layer.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tests"))

ISSUERS = (
    "IN-INFY",
    "IN-HINDUNILVR",
    "IN-TCS",
    "IN-HCLTECH",
    "IN-ITC",
    "IN-ASIANPAINT",
    "IN-MARUTI",
    "IN-ULTRACEMCO",
    "IN-SUNPHARMA",
    "IN-LT",
)

AP_ORIGINAL_STORAGE = (
    "storage/raw/india/asianpaints/q4_fy2025-26/exchange_nse/"
    "ASIANPAINT-Q4FY26-consolidated-nse-integrated-filing-xbrl-ORIGINAL.xml"
)
AP_ORIGINAL_URL = (
    "https://nsearchives.nseindia.com/corporate/xbrl/"
    "INTEGRATED_FILING_INDAS_1676549_29052026075148_WEB.xml"
)
LT_REVISION_STORAGE = (
    "storage/raw/india/larsen_toubro/q4_fy2025-26/exchange_nse/"
    "LT-Q4FY26-standalone-nse-integrated-filing-xbrl-REVISION.xml"
)
LT_REVISION_URL = (
    "https://nsearchives.nseindia.com/corporate/xbrl/"
    "INTEGRATED_FILING_INDAS_1664542_07052026062845_WEB.xml"
)


def _published(filing: dict) -> date:
    stamp = filing.get("broadcast_ist") or filing.get("revised_ist")
    assert stamp, "filing metadata must carry broadcast_ist or revised_ist"
    return date.fromisoformat(str(stamp).split(" ")[0])


def import_fixtures(manifest: dict, fixtures: Path) -> None:
    from quarterline.sources.india.ir_documents import import_document

    for entry in manifest["committed_fixtures"]:
        info = manifest["periods"][entry["period"]]
        filing = entry["exchange_filing"]
        report = import_document(
            issuer_id=entry["issuer_id"],
            path=fixtures / entry["file"],
            doc_type=entry["doc_type"],
            period_start=date.fromisoformat(info["period_start"]),
            period_end=date.fromisoformat(info["period_end"]),
            scope=entry["scope"],
            published_at=_published(filing),
            source_url=entry["source_url"],
            exchange="NSE",
            seq_id=filing.get("seq_id"),
            audited_status=filing.get("audited_status"),
            revision_status=filing.get("revision"),
            notes=(entry.get("description") or "")[:180],
        )
        print(
            f"  imported {entry['issuer_id']} {entry['file']}: artifact={report.artifact_id} "
            f"facts={report.fact_count} tax={report.taxonomy_version}"
        )


def import_revision_evidence(manifest: dict, period_key: str) -> None:
    from quarterline.sources.india.ir_documents import import_document

    info = manifest["periods"][period_key]
    period_start = date.fromisoformat(info["period_start"])
    period_end = date.fromisoformat(info["period_end"])

    ap_original = next(
        e
        for e in manifest["storage_cache_only"]
        if e["path"].endswith(
            "ASIANPAINT-Q4FY26-consolidated-nse-integrated-filing-xbrl-ORIGINAL.xml"
        )
    )
    report = import_document(
        issuer_id="IN-ASIANPAINT",
        path=REPO_ROOT / AP_ORIGINAL_STORAGE,
        doc_type="financial_results",
        period_start=period_start,
        period_end=period_end,
        scope="consolidated",
        published_at=date.fromisoformat(
            ap_original["exchange_filing"]["broadcast_ist"].split(" ")[0]
        ),
        source_url=AP_ORIGINAL_URL,
        exchange="NSE",
        seq_id=ap_original["exchange_filing"]["seq_id"],
        audited_status=ap_original["exchange_filing"]["audited_status"],
        revision_status="Original",
        notes="Superseded original of the revised committed fixture (revision evidence)",
    )
    print(
        f"  imported AP superseded ORIGINAL: artifact={report.artifact_id} facts={report.fact_count}"
    )

    lt_revision = next(
        e
        for e in manifest["storage_cache_only"]
        if e["path"].endswith("LT-Q4FY26-standalone-nse-integrated-filing-xbrl-REVISION.xml")
    )
    report = import_document(
        issuer_id="IN-LT",
        path=REPO_ROOT / LT_REVISION_STORAGE,
        doc_type="financial_results",
        period_start=period_start,
        period_end=period_end,
        scope="standalone",
        published_at=date.fromisoformat(
            lt_revision["exchange_filing"]["revised_ist"].split(" ")[0]
        ),
        source_url=LT_REVISION_URL,
        exchange="NSE",
        seq_id=lt_revision["exchange_filing"]["seq_id"],
        audited_status=lt_revision["exchange_filing"]["audited_status"],
        revision_status="Revised",
        notes=(
            "Latest of two standalone Q4 revisions (chain 155701 -> 155858 -> 156063); "
            "paid-up-capital metadata fix, fact-neutral per the listing remark"
        ),
    )
    print(
        f"  imported LT standalone REVISION: artifact={report.artifact_id} facts={report.fact_count}"
    )


def run_pipeline(label: str, expect_zero_new: bool = False) -> None:
    from quarterline.sources.india.metrics import compute_india_metrics
    from quarterline.sources.india.normalization import normalize_canonical_facts
    from quarterline.sources.india.pipeline import (
        ingest_observations,
        ingest_reviewed_pdf_cash_flow,
        ingest_reviewed_pdf_comparatives,
    )

    total_obs = total_cmp = 0
    for issuer_id in ISSUERS:
        report = ingest_observations(issuer_id)
        if expect_zero_new:
            assert report.observations_inserted == 0, (issuer_id, report.observations_inserted)
        total_obs += report.observations_inserted
    cfo = ingest_reviewed_pdf_cash_flow("IN-INFY")
    if expect_zero_new:
        assert cfo.observations_inserted == 0
    total_obs += cfo.observations_inserted
    for issuer_id in ISSUERS:
        report = ingest_reviewed_pdf_comparatives(issuer_id)
        if expect_zero_new:
            assert report.observations_inserted == 0, (issuer_id, report.observations_inserted)
        total_cmp += report.observations_inserted
    for issuer_id in ISSUERS:
        normalize_canonical_facts(issuer_id)
        compute_india_metrics(issuer_id)
    print(f"  {label}: new observations={total_obs} (of which comparatives={total_cmp})")


def store_totals() -> tuple[int, int, int, int]:
    from sqlalchemy import select

    from quarterline.store.db import session_scope
    from quarterline.store.models import DerivedMetric, FactLineage, FactObservation, NormalizedFact

    with session_scope() as session:
        return (
            len(
                list(
                    session.scalars(
                        select(FactObservation).where(FactObservation.taxonomy == "in-capmkt")
                    )
                )
            ),
            len(list(session.scalars(select(NormalizedFact)))),
            len(list(session.scalars(select(FactLineage)))),
            len(list(session.scalars(select(DerivedMetric)))),
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch", action="store_true", help="use a throwaway store")
    args = parser.parse_args()

    if args.scratch:
        scratch = Path(tempfile.mkdtemp(prefix="india_ind6_ingest_"))
        os.environ["STORAGE_DIR"] = str(scratch / "storage")
        os.environ["DATABASE_URL"] = f"sqlite:///{(scratch / 'app.db').as_posix()}"

    from quarterline.store.db import get_engine
    from quarterline.store.models import Base

    Base.metadata.create_all(get_engine())

    fixtures = REPO_ROOT / "tests" / "fixtures" / "india"
    manifest = json.loads((fixtures / "manifest.json").read_text(encoding="utf-8"))

    print("== pass 1: fixtures + revision evidence")
    import_fixtures(manifest, fixtures)
    import_revision_evidence(manifest, "q4_fy2025-26")
    run_pipeline("pass 1")

    totals = store_totals()
    print(
        f"store after pass 1: obs={totals[0]} facts={totals[1]} lineage={totals[2]} metrics={totals[3]}"
    )

    print("== pass 2: idempotency (must add zero rows)")
    run_pipeline("pass 2", expect_zero_new=True)
    totals_after = store_totals()
    print(
        f"store after pass 2: obs={totals_after[0]} facts={totals_after[1]} lineage={totals_after[2]} metrics={totals_after[3]}"
    )
    if totals_after != totals:
        print("IDEMPOTENCY BROKEN: store totals changed")
        return 1
    print("IDEMPOTENT: zero new rows at observation/fact/lineage/metric layer")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
