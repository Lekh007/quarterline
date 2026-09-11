"""Shared helpers for Quarterline India tests (IND-2 wave).

Loads the committed NSE Integrated Filing fixtures (real, unmodified exchange
XBRL instances) and their manifest, and imports them into a temp store through
the real manual-import path so pipeline tests never touch the network (SPEC 26:
offline tests only; IND-1 proved every India source rejects non-browser clients).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from quarterline.store.db import get_engine
from quarterline.store.models import Base

TESTS_DIR = Path(__file__).resolve().parent
INDIA_FIXTURES_DIR = TESTS_DIR / "fixtures" / "india"
INDIA_MANIFEST_PATH = INDIA_FIXTURES_DIR / "manifest.json"

#: The four committed consolidated XBRL fixtures.
INFY_Q1 = "INFY-Q1FY27-consolidated-nse-integrated-filing-xbrl.xml"
INFY_Q4 = "INFY-Q4FY26-consolidated-nse-integrated-filing-xbrl.xml"
HUL_Q1 = "HUL-Q1FY27-consolidated-nse-integrated-filing-xbrl.xml"
HUL_Q4 = "HUL-Q4FY26-consolidated-nse-integrated-filing-xbrl.xml"

COMMITTED_FIXTURES = (INFY_Q1, INFY_Q4, HUL_Q1, HUL_Q4)


def load_india_manifest() -> dict:
    return json.loads(INDIA_MANIFEST_PATH.read_text(encoding="utf-8"))


def fixture_entries() -> dict[str, dict]:
    """{file name: manifest entry} for the committed fixtures."""
    return {entry["file"]: entry for entry in load_india_manifest()["committed_fixtures"]}


def period_bounds(manifest: dict, period_key: str) -> tuple[date | None, date]:
    info = manifest["periods"][period_key]
    return (
        date.fromisoformat(info["period_start"]),
        date.fromisoformat(info["period_end"]),
    )


def import_all_fixtures() -> list:
    """Import the 4 committed fixtures through the real manual-import path.

    Requires the offline_env fixture (storage/database pointed at tmp) and the
    schema created. Returns the ImportReports in manifest order.
    """
    from quarterline.sources.india.ir_documents import import_document

    manifest = load_india_manifest()
    reports = []
    for entry in manifest["committed_fixtures"]:
        period_key = entry["period"]
        period_start, period_end = period_bounds(manifest, period_key)
        filing = entry["exchange_filing"]
        reports.append(
            import_document(
                issuer_id=entry["issuer_id"],
                path=INDIA_FIXTURES_DIR / entry["file"],
                doc_type=entry["doc_type"],
                period_start=period_start,
                period_end=period_end,
                scope=entry["scope"],
                published_at=date.fromisoformat(filing["broadcast_ist"].split(" ")[0]),
                source_url=entry["source_url"],
                exchange="NSE",
                seq_id=filing.get("seq_id"),
                audited_status=filing.get("audited_status"),
                revision_status=filing.get("revision"),
            )
        )
    return reports


@pytest.fixture
def india_store(offline_env):
    """Offline store with schema created (imports nothing yet)."""
    create_schema()
    return offline_env


@pytest.fixture
def india_imported(india_store):
    """Offline store with the 4 committed fixtures imported."""
    reports = import_all_fixtures()
    return india_store, reports


def create_schema(database_url: str | None = None) -> None:
    """Create all tables on the configured (or given) database."""
    engine = get_engine(database_url) if database_url else get_engine()
    Base.metadata.create_all(engine)


def run_ind4_pipeline() -> None:
    """Run the full IND-4 data layer for both issuers on the current store.

    Observations (exchange XBRL + the reviewed INFY PDF cash flow), canonical
    normalization, and metric computation. Assumes the fixtures are already
    imported (``import_all_fixtures``) and the schema exists.
    """
    from quarterline.sources.india.metrics import compute_india_metrics
    from quarterline.sources.india.normalization import normalize_canonical_facts
    from quarterline.sources.india.pipeline import (
        ingest_observations,
        ingest_reviewed_pdf_cash_flow,
    )

    for issuer_id in ("IN-INFY", "IN-HINDUNILVR"):
        ingest_observations(issuer_id)
    ingest_reviewed_pdf_cash_flow("IN-INFY")
    for issuer_id in ("IN-INFY", "IN-HINDUNILVR"):
        normalize_canonical_facts(issuer_id)
        compute_india_metrics(issuer_id)


def synthetic_instance_xml(
    *,
    period_start: str,
    period_end: str,
    revenue_value: str,
    scope: str = "consolidated",
    context_id: str = "OneD",
) -> bytes:
    """A CLEARLY SYNTHETIC in-capmkt instance carrying ONE revenue fact.

    Used only to prove selection-policy behaviour (latest publication wins;
    standalone retained, never substituted) — never presented as company
    results. ``scope="standalone"`` declares Standalone in the instance so the
    import path's scope cross-check passes.
    """
    scope_word = "Consolidated" if scope == "consolidated" else "Standalone"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!-- SYNTHETIC TEST FIXTURE: clearly-labeled parser test, not a company filing -->\n"
        '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"'
        ' xmlns:in-capmkt="http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt"'
        ' xmlns:iso4217="http://www.xbrl.org/2003/iso4217">\n'
        f'<xbrli:context id="{context_id}">\n'
        "  <xbrli:entity>\n"
        '    <xbrli:identifier scheme="http://www.sebi.gov.in/in-capmkt/ScripCode">500209</xbrli:identifier>\n'
        "  </xbrli:entity>\n"
        f"  <xbrli:period><xbrli:startDate>{period_start}</xbrli:startDate>"
        f"<xbrli:endDate>{period_end}</xbrli:endDate></xbrli:period>\n"
        "</xbrli:context>\n"
        '<xbrli:unit id="INR"><xbrli:measure>iso4217:INR</xbrli:measure></xbrli:unit>\n'
        f'<in-capmkt:NatureOfReportStandaloneConsolidated contextRef="{context_id}">{scope_word}</in-capmkt:NatureOfReportStandaloneConsolidated>\n'
        f'<in-capmkt:LevelOfRounding contextRef="{context_id}">Crores</in-capmkt:LevelOfRounding>\n'
        f'<in-capmkt:RevenueFromOperations contextRef="{context_id}"'
        f' unitRef="INR" decimals="-7">{revenue_value}</in-capmkt:RevenueFromOperations>\n'
        "</xbrli:xbrl>\n"
    ).encode()
