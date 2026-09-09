"""SEC submissions metadata: parallel-arrays parsing and filing URLs (contract C3)."""

from __future__ import annotations

from dataclasses import dataclass

from quarterline.sources.sec.client import SecClient
from quarterline.sources.sec.companyfacts import cik10

SUBMISSIONS_URL_TEMPLATE = "https://data.sec.gov/submissions/CIK{cik}.json"


@dataclass(frozen=True)
class FilingRef:
    """Reference to one filing from the submissions feed."""

    #: accession number with dashes stripped (archive URL form)
    accession: str
    #: original accession number as SEC reports it (with dashes)
    accession_raw: str
    form: str
    filed_at: str  # ISO date string from filingDate
    report_period_end: str  # ISO date string from reportDate
    primary_document: str


def submissions_url(cik: int | str) -> str:
    return SUBMISSIONS_URL_TEMPLATE.format(cik=cik10(cik))


def fetch_submissions(client: SecClient, cik: int | str) -> dict:
    """Fetch (and cache) the submissions payload; caller handles ``filings.recent``."""
    return client.get_json(submissions_url(cik))


def list_recent_filings(
    client: SecClient,
    cik: int | str,
    forms: list[str],
    limit: int = 10,
) -> list[FilingRef]:
    """List the most recent filings of the given form types for ``cik``.

    The submissions endpoint encodes each attribute as a parallel array under
    ``filings.recent``; this walks the arrays in lockstep and returns the first
    ``limit`` matches (most recent first).
    """
    data = fetch_submissions(client, cik)
    recent = (data.get("filings") or {}).get("recent") or {}
    accessions = recent.get("accessionNumber") or []
    form_list = recent.get("form") or []
    filing_dates = recent.get("filingDate") or []
    report_dates = recent.get("reportDate") or []
    primary_documents = recent.get("primaryDocument") or []

    wanted = {form.upper() for form in forms}
    refs: list[FilingRef] = []
    for accession, form, filed, report, document in zip(
        accessions, form_list, filing_dates, report_dates, primary_documents, strict=False
    ):
        if str(form).upper() not in wanted:
            continue
        refs.append(
            FilingRef(
                accession=str(accession).replace("-", ""),
                accession_raw=str(accession),
                form=str(form),
                filed_at=str(filed),
                report_period_end=str(report),
                primary_document=str(document),
            )
        )
        if len(refs) >= limit:
            break
    return refs


def build_filing_url(accession: str, primary_document: str, cik: int | str | None = None) -> str:
    """Build the EDGAR archive URL for a filing document.

    Canonical form uses the CIK:
        https://www.sec.gov/Archives/edgar/data/{cik}/{accession-no-dashes}/{primary_document}
    When no CIK is supplied, the accession-only path is used (SEC also serves
    documents at https://www.sec.gov/Archives/edgar/data/{accession-no-dashes}/{doc}).
    """
    clean_accession = accession.replace("-", "")
    document = primary_document.lstrip("/")
    if cik is None:
        return f"https://www.sec.gov/Archives/edgar/data/{clean_accession}/{document}"
    return f"https://www.sec.gov/Archives/edgar/data/{cik10(cik)}/{clean_accession}/{document}"
