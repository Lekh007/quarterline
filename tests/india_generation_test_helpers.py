"""Shared support for IND-8 generation-track tests (India briefs + memos).

Seeds a temporary SQLite store with BOTH halves the India pipeline needs:

- the committed INFY + HUL consolidated XBRL fixtures through the real
  import path, then the IND-4 data layer (observations, canonical facts,
  metrics) restricted to those two issuers; and
- a SYNTHETIC India narrative corpus (clearly test-only page documents in the
  India kind vocabulary, commentary flags set) indexed with the deterministic
  FakeEmbeddingProvider, so retrieval + page-level evidence ids work offline.

Everything is offline: no network, no Ollama (PLAN ground rule 8). The
:class:`ScriptedProvider` fake from ``generation_test_helpers`` stands in for
the generation provider.
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from generation_test_helpers import (
    ScriptedProvider,
    fixture_embedding_provider,
    scripted_result,
    scripted_text,
)
from india_test_helpers import (
    INDIA_FIXTURES_DIR,
    create_schema,
    period_bounds,
    run_ind4_pipeline,
)

from quarterline.store.db import session_scope

INFY = "IN-INFY"
HUL = "IN-HINDUNILVR"
BRIEF_ISSUERS = (INFY, HUL)

#: The Q1 FY27 period end both seeded narrative corpora and the brief target.
Q1_FY27_END = date(2026, 6, 30)

_INFY_STATEMENT_PAGE = (
    "Condensed consolidated statement (test-only synthetic page). Quarterly "
    "results: revenue from operations and profit after tax for the quarter, "
    "with margin commentary. This page is a financial_results kind document."
)
_INFY_PRESENTATION_PAGE = (
    "Results presentation (test-only synthetic page). Management stated the "
    "quarter showed the highest revenue growth in many quarters, with margin "
    "guidance for the full year. Earnings presentation commentary."
)
_HUL_STATEMENT_PAGE = (
    "Consolidated financial results page (test-only synthetic page). Quarterly "
    "results revenue and profit commentary for the quarter. financial_results."
)
_HUL_NOTES_PAGE = (
    "Press release (test-only synthetic page). Management stated quarterly "
    "results showed resilient revenue with margin commentary. results_notes."
)


def _seed_documents() -> dict[str, dict]:
    """Insert the synthetic narrative corpus; return {key: document row dict}.

    Every document: one section = one page (the India page-level citation
    shape), document_kind + management_commentary carried in extraction_notes
    exactly like narrative.ingest_narratives writes them.
    """
    from quarterline.sources.india.issuers import get_issuer
    from quarterline.store.repositories.companies import CompaniesRepo
    from quarterline.store.repositories.documents import DocumentsRepo, SectionInput

    specs = [
        (
            INFY,
            "infy-statement",
            "financial_results",
            False,
            _INFY_STATEMENT_PAGE,
        ),
        (
            INFY,
            "infy-presentation",
            "earnings_presentation",
            True,
            _INFY_PRESENTATION_PAGE,
        ),
        (HUL, "hul-statement", "financial_results", False, _HUL_STATEMENT_PAGE),
        (HUL, "hul-notes", "results_notes", True, _HUL_NOTES_PAGE),
    ]
    seeded: dict[str, dict] = {}
    with session_scope() as session:
        docs = DocumentsRepo(session)
        companies = CompaniesRepo(session)
        for issuer_id, slug, kind, commentary, text in specs:
            issuer = get_issuer(issuer_id)
            company = companies.get_by_ticker(issuer.ticker_nse or issuer.issuer_id)
            assert company is not None, f"{issuer_id} company row must exist"
            metadata = {
                "document_kind": kind,
                "management_commentary": commentary,
                "source_tier": "company_ir",
                "reporting_scope": "consolidated",
                "issuer_id": issuer_id,
                "test_synthetic": True,
            }
            document, _created = docs.upsert_document(
                company_id=company.id,
                accession=None,
                form=None,
                document_kind=kind,
                period_end=Q1_FY27_END,
                filed_at=date(2026, 7, 23),
                source_url="https://test.invalid/" + slug,
                source_artifact_id=None,
                extraction_version="india-test-1",
                extraction_status="ok",
                extraction_notes=json.dumps(metadata, sort_keys=True),
            )
            sections = docs.replace_sections(
                document.id,
                [
                    SectionInput(
                        section_type=kind,
                        heading="Page 1",
                        text=text,
                        start_offset=0,
                        end_offset=len(text),
                        page_start=1,
                        page_end=1,
                        confidence=1.0,
                    )
                ],
            )
            seeded[slug] = {
                "document_id": document.id,
                "section_id": sections[0].id,
                "kind": kind,
                "commentary": commentary,
                "text": text,
            }
    return seeded


def _import_two_issuers() -> None:
    """Import only the INFY + HUL committed fixtures (keeps tests fast)."""
    from india_test_helpers import _published_date

    from quarterline.sources.india.ir_documents import import_document

    manifest_data = None
    from india_test_helpers import load_india_manifest

    manifest_data = load_india_manifest()
    for entry in manifest_data["committed_fixtures"]:
        if entry["issuer_id"] not in BRIEF_ISSUERS:
            continue
        period_key = entry["period"]
        period_start, period_end = period_bounds(manifest_data, period_key)
        filing = entry["exchange_filing"]
        import_document(
            issuer_id=entry["issuer_id"],
            path=INDIA_FIXTURES_DIR / entry["file"],
            doc_type=entry["doc_type"],
            period_start=period_start,
            period_end=period_end,
            scope=entry["scope"],
            published_at=_published_date(filing),
            source_url=entry["source_url"],
            exchange="NSE",
            seq_id=filing.get("seq_id"),
            audited_status=filing.get("audited_status"),
            revision_status=filing.get("revision"),
        )


@pytest.fixture(name="india_generation_db")
def india_generation_db_fixture(tmp_path, monkeypatch) -> dict:
    """INFY+HUL facts + metrics + synthetic narrative corpus + section index."""
    storage = tmp_path / "storage"
    monkeypatch.setenv("STORAGE_DIR", str(storage))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9")  # nothing listens here
    create_schema()
    _import_two_issuers()
    run_ind4_pipeline(BRIEF_ISSUERS)
    documents = _seed_documents()

    from quarterline.retrieve.search import build_index

    with session_scope() as session:
        stats = build_index(session, "section", fixture_embedding_provider())
    assert stats.documents_indexed == 4, stats.summary()
    return {"documents": documents, "index": stats}


def brief_search_service(session):
    """SearchService over the seeded store with the deterministic fake."""
    from quarterline.retrieve.search import SearchService

    return SearchService(session, fixture_embedding_provider())


def brief_context_passages(session, *, ticker: str = "INFY") -> list:
    """The exact passages the India brief pipeline supplies to the model."""
    from quarterline.retrieve.context import assemble_context
    from quarterline.retrieve.models import SearchQuery
    from quarterline.sources.india.brief import (
        INDIA_BRIEF_RETRIEVAL_QUERY,
        INDIA_BRIEF_SECTIONS,
    )

    service = brief_search_service(session)
    result = service.search(
        SearchQuery(
            query=INDIA_BRIEF_RETRIEVAL_QUERY,
            ticker=ticker,
            strategy="section",
            retrieval="hybrid",
            mode="general",
            sections=list(INDIA_BRIEF_SECTIONS),
            period_end=Q1_FY27_END,
            top_k=12,
        )
    )
    assert not result.insufficient_evidence, result.insufficient_evidence_reason
    bundle = assemble_context(result.items, 6000)
    return bundle.passages


def passage_ids_by_slug(passages, documents) -> dict[str, str]:
    """{slug: evidence_id} for the seeded synthetic pages."""
    by_document = {p.document_id: p.evidence_id for p in passages}
    return {
        slug: by_document[doc["document_id"]]
        for slug, doc in documents.items()
        if doc["document_id"] in by_document
    }


def india_brief_payload(
    label: str,
    evidence_id: str,
    *,
    bullets: list[dict] | None = None,
    risks: list[dict] | None = None,
    metric_mentions: list[dict] | None = None,
    open_questions: list[dict] | None = None,
    status: str = "ok",
) -> dict:
    """A scripted provider response that survives the whole India gate."""
    return {
        "status": status,
        "label_echo": label,
        "metric_mentions": metric_mentions
        if metric_mentions is not None
        else [{"metric_id": "india_revenue_qoq", "template": "reported_change"}],
        "bullets": bullets
        if bullets is not None
        else [
            {
                "text": (
                    "Management stated demand for the quarter's services "
                    f"remained strong. [{evidence_id}]"
                ),
                "evidence_ids": [evidence_id],
            }
        ],
        "risks": risks if risks is not None else [],
        "open_questions": open_questions if open_questions is not None else [],
    }


def india_memo_payload(
    label: str,
    evidence_id: str,
    *,
    extra_sections: list[dict] | None = None,
    metric_mentions: list[dict] | None = None,
    status: str = "ok",
) -> dict:
    """A scripted India memo response that survives the whole India gate."""
    sections = [
        {
            "heading": "overview",
            "text": (
                "Management stated demand for digital services remained strong "
                f"during the quarter. [{evidence_id}]"
            ),
            "evidence_ids": [evidence_id],
        },
        {
            "heading": "what_changed",
            "text": (
                "The condensed statement reports growth in revenue from "
                f"operations for the quarter. [{evidence_id}]"
            ),
            "evidence_ids": [evidence_id],
        },
        {
            "heading": "management_explanation",
            "text": (
                "Management stated margin guidance for the full year remained "
                f"within the communicated band. [{evidence_id}]"
            ),
            "evidence_ids": [evidence_id],
        },
        {
            "heading": "risks_and_open_questions",
            "text": (
                "Management identified macro uncertainty as an area of "
                f"attention for the coming quarters. [{evidence_id}]"
            ),
            "evidence_ids": [evidence_id],
        },
        {
            "heading": "evidence_gaps",
            "text": "Would independent evidence confirm the demand commentary?",
            "evidence_ids": [],
        },
    ]
    if extra_sections:
        sections.extend(extra_sections)
    return {
        "status": status,
        "label_echo": label,
        "metric_mentions": metric_mentions
        if metric_mentions is not None
        else [{"metric_id": "india_pat_margin_owners", "template": "reported_value"}],
        "sections": sections,
    }


__all__ = [
    "BRIEF_ISSUERS",
    "HUL",
    "INFY",
    "Q1_FY27_END",
    "ScriptedProvider",
    "brief_context_passages",
    "brief_search_service",
    "india_brief_payload",
    "india_generation_db_fixture",
    "india_memo_payload",
    "passage_ids_by_slug",
    "scripted_result",
    "scripted_text",
]
