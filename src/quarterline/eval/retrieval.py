"""Retrieval evaluation with PRECISE metric definitions (SPEC §22).

Metric definitions (exact; these names are load-bearing):

- **Hit@5** — over questions that have at least one gold evidence unit, the
  fraction of questions where at least one of the top-5 retrieved units
  overlaps a gold span ("at least one relevant result found").
- **Recall@5** — per question, ``|gold units covered by the top-5 retrieved
  units| / |gold units|``, averaged over questions with gold units. This is
  an OVERLAP COUNT, not "any hit": a question with two gold units of which
  only one is retrieved contributes 0.5 to Recall@5 but a full 1 to Hit@5.
  The two metrics coincide only when every question has exactly one gold
  unit — our dataset has such a case in unit tests to keep them distinct.
- **MRR** — mean over questions with gold units of the reciprocal rank of
  the FIRST retrieved unit that overlaps any gold span (0 when none in top-k).
- **Wrong-company retrieval rate** — retrieved units whose document belongs
  to a different company than the question's ticker, divided by all
  retrieved units across the run.
- **Period-filter error rate** — over questions that specify a period, the
  fraction of retrieved units whose document period differs from the
  requested period. Units from documents with an unknown period are not
  counted as errors (they are counted in the denominator's sibling "unknown"
  bucket, reported alongside).
- **Latency** — wall time around :meth:`SearchService.search` per question,
  aggregated as P50/P95 in milliseconds. Rerank latency is reported only
  when a reranker actually ran (fixture-corpus runs have none: null).

A "gold unit" is one span-anchored entry of ``relevant_evidence`` (SPEC §21:
spans, not chunk ids, so both chunking strategies compare fairly). A
retrieved unit overlaps a gold unit when it belongs to the same document and
its [start, end) offsets intersect the gold span — the section strategy's
bounded windows and the fixed strategy's chunks are therefore judged on
identical terms.

Everything here runs OFFLINE against the frozen fixture corpus with the
deterministic (NON-PRODUCTION) FakeEmbeddingProvider. Fake embeddings carry
no semantic signal, so dense arms measure the plumbing, not semantic
quality; the real-model matrix is Wave 5 (SPEC §22/§23). Numbers from this
corpus are labeled fixture-corpus-only wherever they appear.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from quarterline.config import get_settings
from quarterline.observability.metrics import percentile
from quarterline.retrieve.embeddings import FakeEmbeddingProvider
from quarterline.retrieve.models import SearchQuery, SearchResult
from quarterline.retrieve.search import SearchService
from quarterline.store.models import Company, Document

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCUMENTS_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "documents"
AAPL_CIK = "0000320193"

#: Dataset version = first 12 hex of the questions file sha256 (baked into
#: eval_runs rows and baselines so a dataset change invalidates comparisons).
DATASET_PATH = REPO_ROOT / "data" / "eval" / "questions.jsonl"

STRATEGIES = ("fixed", "section")
RETRIEVAL_CONFIGS = ("lexical", "dense", "hybrid", "hybrid-rerank")


def dataset_version(dataset_path: str | Path = DATASET_PATH) -> str:
    """Stable version stamp of the dataset file content (sha256, first 12 hex)."""
    return hashlib.sha256(Path(dataset_path).read_bytes()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Gold spans and overlap logic
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Span:
    """An offset span inside one document (document ids are DB row ids)."""

    document_id: int
    start: int
    end: int

    def overlaps(self, other: Span) -> bool:
        return (
            self.document_id == other.document_id
            and self.start < other.end
            and other.start < self.end
        )


@dataclass
class QuestionRetrievalRecord:
    """Per-question retrieval outcome (the unit every metric aggregates)."""

    question_id: str
    ticker: str
    task_type: str
    expected_behavior: str
    has_gold: bool
    question_period: date | None = None
    retrieved: list[Span] = field(default_factory=list)
    gold: list[Span] = field(default_factory=list)
    retrieved_companies: list[str] = field(default_factory=list)
    retrieved_periods: list[date | None] = field(default_factory=list)
    latency_ms: float = 0.0
    rerank_latency_ms: float | None = None
    insufficient_evidence: bool = False
    degraded: list[str] = field(default_factory=list)

    # -- metrics --------------------------------------------------------------

    def hit_at_k(self, k: int = 5) -> bool | None:
        """None when the question has no gold units (excluded from Hit@5)."""
        if not self.gold:
            return None
        return any(unit.overlaps(gold) for unit in self.retrieved[:k] for gold in self.gold)

    def covered_gold(self, k: int = 5) -> int:
        """Gold units overlapped by at least one of the top-k retrieved units."""
        return sum(
            1 for gold in self.gold if any(unit.overlaps(gold) for unit in self.retrieved[:k])
        )

    def recall_at_k(self, k: int = 5) -> float | None:
        """None when the question has no gold units (excluded from Recall@5)."""
        if not self.gold:
            return None
        return self.covered_gold(k) / len(self.gold)

    def reciprocal_rank(self, k: int = 5) -> float:
        for rank, unit in enumerate(self.retrieved[:k], start=1):
            if any(unit.overlaps(gold) for gold in self.gold):
                return 1.0 / rank
        return 0.0

    def wrong_company_units(self, k: int = 5) -> int:
        return sum(1 for company in self.retrieved_companies[:k] if company != self.ticker)


# ---------------------------------------------------------------------------
# Aggregate report
# ---------------------------------------------------------------------------


@dataclass
class RetrievalEvalReport:
    """Aggregate retrieval metrics for one (strategy x retrieval) config."""

    strategy: str
    retrieval: str
    embedding_model: str
    corpus: str = "fixture"  # the frozen test fixture corpus, NOT production
    n_questions: int = 0
    n_with_gold: int = 0
    hit_at_5: float | None = None
    recall_at_5: float | None = None
    mrr: float | None = None
    wrong_company_rate: float | None = None
    wrong_company_units: int = 0
    retrieved_units: int = 0
    period_filter_error_rate: float | None = None
    period_unknown_rate: float | None = None
    insufficient_evidence_rate: float | None = None
    retrieval_latency_p50_ms: float | None = None
    retrieval_latency_p95_ms: float | None = None
    rerank_latency_p50_ms: float | None = None
    rerank_degraded: bool = True
    records: list[QuestionRetrievalRecord] = field(default_factory=list)

    def summary_row(self) -> str:
        def pct(value: float | None) -> str:
            return f"{value * 100:.1f}%" if value is not None else "n/a"

        lat = (
            f"{self.retrieval_latency_p50_ms:.1f}/{self.retrieval_latency_p95_ms:.1f}"
            if self.retrieval_latency_p50_ms is not None
            else "n/a"
        )
        return (
            f"{self.strategy:>7} | {self.retrieval:>13} | Hit@5 {pct(self.hit_at_5):>7} | "
            f"Recall@5 {pct(self.recall_at_5):>7} | MRR {self.mrr if self.mrr is not None else -1:.3f} | "
            f"wrong-co {pct(self.wrong_company_rate):>6} | period-err {pct(self.period_filter_error_rate):>6} | "
            f"lat p50/p95 {lat:>11} | n={self.n_questions}"
        )

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "retrieval": self.retrieval,
            "embedding_model": self.embedding_model,
            "corpus": self.corpus,
            "n_questions": self.n_questions,
            "n_with_gold": self.n_with_gold,
            "hit_at_5": self.hit_at_5,
            "recall_at_5": self.recall_at_5,
            "mrr": self.mrr,
            "wrong_company_rate": self.wrong_company_rate,
            "wrong_company_units": self.wrong_company_units,
            "retrieved_units": self.retrieved_units,
            "period_filter_error_rate": self.period_filter_error_rate,
            "period_unknown_rate": self.period_unknown_rate,
            "insufficient_evidence_rate": self.insufficient_evidence_rate,
            "retrieval_latency_p50_ms": self.retrieval_latency_p50_ms,
            "retrieval_latency_p95_ms": self.retrieval_latency_p95_ms,
            "rerank_latency_p50_ms": self.rerank_latency_p50_ms,
            "rerank_degraded": self.rerank_degraded,
        }


def aggregate(
    records: list[QuestionRetrievalRecord],
    *,
    strategy: str,
    retrieval: str,
    embedding_model: str,
    corpus: str = "fixture",
) -> RetrievalEvalReport:
    """Fold per-question records into a :class:`RetrievalEvalReport`."""
    report = RetrievalEvalReport(
        strategy=strategy, retrieval=retrieval, embedding_model=embedding_model, corpus=corpus
    )
    report.records = records
    report.n_questions = len(records)
    gold_records = [record for record in records if record.has_gold]
    report.n_with_gold = len(gold_records)

    hits = [bool(record.hit_at_k(5)) for record in gold_records]
    report.hit_at_5 = sum(hits) / len(hits) if hits else None
    recalls = [record.recall_at_k(5) for record in gold_records]
    recalls = [value for value in recalls if value is not None]
    report.recall_at_5 = sum(recalls) / len(recalls) if recalls else None
    report.mrr = (
        sum(record.reciprocal_rank(5) for record in gold_records) / len(gold_records)
        if gold_records
        else None
    )

    total_units = sum(min(5, len(record.retrieved)) for record in records)
    report.retrieved_units = total_units
    wrong = sum(record.wrong_company_units(5) for record in records)
    report.wrong_company_units = wrong
    report.wrong_company_rate = wrong / total_units if total_units else None

    period_units = 0
    period_errors = 0
    period_unknown = 0
    for record in records:
        if record.expected_behavior == "answer" and record.retrieved_periods:
            for period in record.retrieved_periods[:5]:
                period_units += 1
                if period is None:
                    period_unknown += 1
                elif record.question_period is not None and period != record.question_period:
                    period_errors += 1
    report.period_filter_error_rate = period_errors / period_units if period_units else None
    report.period_unknown_rate = period_unknown / period_units if period_units else None

    answerable = [record for record in records if record.expected_behavior == "answer"]
    report.insufficient_evidence_rate = (
        sum(1 for record in answerable if record.insufficient_evidence) / len(answerable)
        if answerable
        else None
    )

    latencies = sorted(record.latency_ms for record in records)
    report.retrieval_latency_p50_ms = percentile(latencies, 50)
    report.retrieval_latency_p95_ms = percentile(latencies, 95)
    reranks = sorted(value for record in records if (value := record.rerank_latency_ms) is not None)
    report.rerank_latency_p50_ms = percentile(reranks, 50) if reranks else None
    report.rerank_degraded = not reranks
    return report


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def document_directory(session: Session) -> dict[int, dict]:
    """document id -> {accession, ticker, period_end} for metrics lookups."""
    rows = session.execute(
        select(Document.id, Document.accession, Document.period_end, Company.ticker)
        .join(Company, Company.id == Document.company_id)
        .where(Document.extraction_status == "ok")
    ).all()
    return {
        row.id: {
            "accession": row.accession,
            "ticker": row.ticker,
            "period_end": row.period_end,
        }
        for row in rows
    }


def evaluate_retrieval(
    questions,
    session: Session,
    *,
    strategy: str = "section",
    retrieval: str = "hybrid",
    top_k: int = 5,
    corpus: str = "fixture",
    service: SearchService | None = None,
    provider=None,
) -> RetrievalEvalReport:
    """Run every question through :class:`SearchService` and score it.

    ``questions`` are :class:`quarterline.eval.dataset.EvalQuestion` values.
    Uses the FakeEmbeddingProvider-backed fixture corpus offline; pass an
    explicit ``service`` to evaluate a different configuration.
    """
    from quarterline.eval.dataset import resolve_document_ids

    if service is None:
        provider = provider or FakeEmbeddingProvider()
        service = SearchService(session, provider)
    embedding_model = (
        f"{getattr(service.provider, 'provider_name', 'unknown')}/{service.provider.model_id}"
        if service.provider is not None
        else "n/a (lexical only)"
    )

    directory = document_directory(session)
    gold_doc_ids = resolve_document_ids(session, questions)

    records: list[QuestionRetrievalRecord] = []
    for question in questions:
        started = time.perf_counter()
        query = SearchQuery(
            query=question.question,
            ticker=question.ticker,
            strategy=strategy,  # type: ignore[arg-type]
            retrieval=retrieval,  # type: ignore[arg-type]
            period_end=question.period_end,
            top_k=top_k,
        )
        result: SearchResult = service.search(query)
        latency = (time.perf_counter() - started) * 1000

        gold = [
            Span(
                gold_doc_ids[span.document_id.replace("-", "")], span.start_offset, span.end_offset
            )
            for span in question.relevant_evidence
        ]
        retrieved = [
            Span(item.document_id, item.start_offset, item.end_offset) for item in result.items
        ]
        records.append(
            QuestionRetrievalRecord(
                question_id=question.id,
                ticker=question.ticker,
                task_type=question.task_type,
                expected_behavior=question.expected_behavior,
                has_gold=bool(gold),
                retrieved=retrieved,
                gold=gold,
                retrieved_companies=[
                    directory.get(item.document_id, {}).get("ticker") or "" for item in result.items
                ],
                retrieved_periods=[
                    directory.get(item.document_id, {}).get("period_end") for item in result.items
                ],
                latency_ms=round(latency, 2),
                rerank_latency_ms=None,  # no cross-encoder runs on the fixture corpus
                insufficient_evidence=result.insufficient_evidence,
                degraded=sorted(result.degraded),
                question_period=question.period_end,
            )
        )
    return aggregate(
        records,
        strategy=strategy,
        retrieval=retrieval,
        embedding_model=embedding_model,
        corpus=corpus,
    )


def run_matrix(
    questions,
    session: Session,
    *,
    strategies: tuple[str, ...] = STRATEGIES,
    configs: tuple[str, ...] = RETRIEVAL_CONFIGS,
    provider=None,
) -> list[RetrievalEvalReport]:
    """The SPEC §22 experiment matrix: strategies x retrieval configurations.

    Fixture-corpus only. The reranker arm degrades to hybrid (no cross-encoder
    in the offline fixture environment) and is reported as degraded.
    """
    provider = provider or FakeEmbeddingProvider()
    reports = []
    for strategy in strategies:
        for retrieval in configs:
            reports.append(
                evaluate_retrieval(
                    questions,
                    session,
                    strategy=strategy,
                    retrieval=retrieval,
                    provider=provider,
                )
            )
    return reports


# ---------------------------------------------------------------------------
# Frozen fixture corpus bootstrap (offline; mirrors tests/retrieval_test_helpers)
# ---------------------------------------------------------------------------


def ensure_fixture_corpus(settings=None) -> dict:
    """Ingest + index the frozen fixture corpus into the configured database.

    Mirrors ``tests/retrieval_test_helpers.py`` exactly (same canned client,
    same ingestion order -> same document ids 1..4) so the eval corpus is the
    same frozen corpus the retrieval tests use. Fully offline and idempotent.
    """
    from quarterline.ingest.documents import FormsConfig, ingest_documents, ingest_pdf_file
    from quarterline.retrieve.search import build_index
    from quarterline.sources.sec.client import SecDownloadResult
    from quarterline.store.db import get_engine, session_scope
    from quarterline.store.models import Base

    settings = settings or get_settings()
    Base.metadata.create_all(get_engine())

    real_exhibit = (DOCUMENTS_FIXTURES / "real_aapl_8k_ex991_q2fy26.htm").read_bytes()
    injection = (DOCUMENTS_FIXTURES / "injection_filing.html").read_bytes()
    ten_q_html = _TEN_Q_HTML_BYTES
    base = f"https://www.sec.gov/Archives/edgar/data/{AAPL_CIK}"

    class _FixtureClient:
        """Offline SecClient stand-in (same payloads as the retrieval tests)."""

        def __init__(self) -> None:
            self.json_by_url = {
                f"https://data.sec.gov/submissions/CIK{AAPL_CIK}.json": {
                    "filings": {
                        "recent": {
                            "accessionNumber": [
                                "0000320193-26-000099",
                                "0000320193-26-000011",
                                "0000320193-26-000013",
                            ],
                            "form": ["10-Q", "8-K", "8-K"],
                            "filingDate": ["2026-05-01", "2026-04-30", "2026-01-29"],
                            "reportDate": ["2026-03-28", "2026-04-30", "2026-01-29"],
                            "primaryDocument": ["a10-q.htm", "a8-k.htm", "a8-k2.htm"],
                            "items": ["", "2.02", "2.02"],
                        }
                    }
                },
                f"{base}/000032019326000011/index.json": {
                    "directory": {"item": [{"name": "a8-kex991q2202603282026.htm"}]}
                },
                f"{base}/000032019326000013/index.json": {
                    "directory": {"item": [{"name": "exhibit-99-pressrelease.htm"}]}
                },
            }
            self.bytes_by_url = {
                f"{base}/000032019326000099/a10-q.htm": ten_q_html,
                f"{base}/000032019326000011/a8-kex991q2202603282026.htm": real_exhibit,
                f"{base}/000032019326000013/exhibit-99-pressrelease.htm": injection,
            }

        def get_json(self, url: str) -> object:
            return self.json_by_url[url]

        def download(self, url: str) -> SecDownloadResult:
            return SecDownloadResult(
                content=self.bytes_by_url[url],
                source_url=url,
                etag='"fixture-etag"',
                last_modified="Wed, 09 Sep 2026 00:00:00 GMT",
                cache_hit=False,
            )

        def close(self) -> None:
            pass

    watchlist = settings.storage_dir / "eval_fixture_watchlist.csv"
    watchlist.parent.mkdir(parents=True, exist_ok=True)
    if not watchlist.is_file():
        watchlist.write_text(
            f"ticker,cik,name,sector,country\nAAPL,{AAPL_CIK},Apple Inc.,Information Technology,US\n",
            encoding="utf-8",
        )

    report = ingest_documents(watchlist, FormsConfig(), client=_FixtureClient())
    ingest_pdf_file(
        "ACME",
        "https://ir.example.invalid/reports/q2-review.pdf",
        date(2026, 7, 15),
        DOCUMENTS_FIXTURES / "synthetic_mda.pdf",
    )
    provider = FakeEmbeddingProvider(dim=64, seed=20260909)
    with session_scope() as session:
        fixed = build_index(session, "fixed", provider, settings=settings)
        section = build_index(session, "section", provider, settings=settings)
    return {
        "documents_ingested": report.documents_ingested,
        "documents_skipped": report.documents_skipped,
        "fixed": fixed.summary(),
        "section": section.summary(),
    }


#: Same fictional-but-structurally-realistic 10-Q payload as the retrieval tests.
_TEN_Q_HTML_BYTES = b"""<!DOCTYPE html>
<html><head><title>10-Q</title></head><body>
<h1>Part I - Financial Information</h1>
<table>
<tr><td>Item 1.</td><td>Financial Statements</td><td>3</td></tr>
<tr><td>Item 2.</td><td>Management's Discussion and Analysis of Financial Condition and Results of Operations</td><td>12</td></tr>
</table>
<h2>Item 2. Management's Discussion and Analysis of Financial Condition and Results of Operations</h2>
<p>Revenue increased 12 percent driven by strong services growth across every segment during the quarter, while operating expenses grew more slowly than revenue and gross margin expanded. The company continued returning capital to shareholders while operating cash flow remained robust.</p>
<h1>Part II - Other Information</h1>
<h2>Item 1A. Risk Factors</h2>
<p>The risk factors previously disclosed in the annual report have not changed materially, although supply chain concentration and evolving data regulation remain areas of continued attention for the business this year.</p>
<h2>Item 6. Exhibits</h2>
<p>Exhibit 31 certification rules apply.</p>
</body></html>"""


__all__ = [
    "RETRIEVAL_CONFIGS",
    "STRATEGIES",
    "QuestionRetrievalRecord",
    "RetrievalEvalReport",
    "Span",
    "aggregate",
    "dataset_version",
    "document_directory",
    "ensure_fixture_corpus",
    "evaluate_retrieval",
    "run_matrix",
]
