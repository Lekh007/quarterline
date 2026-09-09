"""SQLAlchemy 2.0 ORM models for every SPEC §9 table (contract C2).

Decimal financial values are stored losslessly as canonical decimal *text*
(``decimal_to_text`` / ``text_to_decimal`` helpers below); repositories convert
to/from ``decimal.Decimal`` at the boundary. No float ever touches a reported
financial value.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# ---------------------------------------------------------------------------
# Decimal persistence helpers (lossless, canonical decimal strings)
# ---------------------------------------------------------------------------


def decimal_to_text(value: Decimal | int | str | None) -> str | None:
    """Convert to a canonical decimal string without float round-tripping.

    ``format(value, "f")`` avoids scientific notation and preserves the exact
    digits given. ``None`` passes through as ``None`` (missing stays missing).
    """
    if value is None:
        return None
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return format(value, "f")


def text_to_decimal(value: str | None) -> Decimal | None:
    """Read a canonical decimal string back into ``Decimal`` (exact)."""
    if value is None:
        return None
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"not a canonical decimal string: {value!r}") from exc


# ---------------------------------------------------------------------------
# Companies (SPEC §9.1)
# ---------------------------------------------------------------------------


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(16), unique=True, index=True, nullable=False)
    # Issuer identity: one CIK is one issuer regardless of listing/ticker changes.
    cik: Mapped[str] = mapped_column(String(10), unique=True, index=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(255))
    sector: Mapped[str | None] = mapped_column(String(64))
    industry: Mapped[str | None] = mapped_column(String(128))
    country: Mapped[str | None] = mapped_column(String(8))
    reporting_currency: Mapped[str | None] = mapped_column(String(8))
    fiscal_year_end: Mapped[str | None] = mapped_column(String(5))  # e.g. "09-30"

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ---------------------------------------------------------------------------
# Source artifacts (SPEC §9.2)
# ---------------------------------------------------------------------------


class SourceArtifact(Base):
    __tablename__ = "source_artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)  # e.g. "sec"
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    local_path: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    content_type: Mapped[str | None] = mapped_column(String(128))
    http_etag: Mapped[str | None] = mapped_column(String(255))
    http_last_modified: Mapped[str | None] = mapped_column(String(255))
    parser_version: Mapped[str | None] = mapped_column(String(32))


# ---------------------------------------------------------------------------
# Reported fact observations (SPEC §9.3)
# ---------------------------------------------------------------------------


class FactObservation(Base):
    __tablename__ = "fact_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    source_artifact_id: Mapped[int | None] = mapped_column(ForeignKey("source_artifacts.id"))
    accession: Mapped[str | None] = mapped_column(String(32))  # dashes stripped
    form: Mapped[str | None] = mapped_column(String(16))
    filed_at: Mapped[date | None] = mapped_column(Date)

    taxonomy: Mapped[str] = mapped_column(String(64), nullable=False)  # e.g. "us-gaap"
    original_tag: Mapped[str] = mapped_column(String(128), nullable=False)
    canonical_concept: Mapped[str] = mapped_column(String(64), nullable=False)

    # Canonical decimal string; convert with text_to_decimal().
    value_decimal: Mapped[str | None] = mapped_column(Text)
    unit: Mapped[str | None] = mapped_column(String(32))  # USD, shares, pure...
    currency: Mapped[str | None] = mapped_column(String(8))

    period_start: Mapped[date | None] = mapped_column(Date)  # null for instants
    period_end: Mapped[date | None] = mapped_column(Date)
    period_kind: Mapped[str] = mapped_column(String(20), nullable=False)

    # Original source fiscal labels, kept separate from economic period identity.
    source_fy: Mapped[int | None] = mapped_column(Integer)
    source_fp: Mapped[str | None] = mapped_column(String(8))

    reporting_scope: Mapped[str | None] = mapped_column(String(32))
    context_metadata_json: Mapped[str | None] = mapped_column(Text)
    # Deduplication identity for a reported observation (SPEC §9.3).
    observation_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)


# ---------------------------------------------------------------------------
# Normalized facts (SPEC §9.4)
# ---------------------------------------------------------------------------


class NormalizedFact(Base):
    __tablename__ = "normalized_facts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    concept: Mapped[str] = mapped_column(String(64), nullable=False)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    fiscal_year: Mapped[int | None] = mapped_column(Integer)
    fiscal_quarter: Mapped[str | None] = mapped_column(String(8))
    period_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    reporting_scope: Mapped[str | None] = mapped_column(
        String(32), nullable=False, default="consolidated"
    )

    value_decimal: Mapped[str | None] = mapped_column(Text)  # canonical decimal string
    unit: Mapped[str | None] = mapped_column(String(32))

    selection_policy: Mapped[str | None] = mapped_column(String(32))  # latest_available / as_of
    normalization_version: Mapped[str | None] = mapped_column(String(32))
    is_derived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    derivation_method: Mapped[str | None] = mapped_column(String(128))
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    data_quality_status: Mapped[str | None] = mapped_column(String(32))

    # Annual and Q4 facts cannot share one conflicting key (SPEC §9.4).
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "concept",
            "period_start",
            "period_end",
            "period_kind",
            "reporting_scope",
            name="uq_normalized_facts_period_identity",
        ),
    )


# ---------------------------------------------------------------------------
# Fact lineage (SPEC §9.5)
# ---------------------------------------------------------------------------


class FactLineage(Base):
    __tablename__ = "fact_lineage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    normalized_fact_id: Mapped[int] = mapped_column(
        ForeignKey("normalized_facts.id"), nullable=False, index=True
    )
    source_observation_id: Mapped[int] = mapped_column(
        ForeignKey("fact_observations.id"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # direct_source|annual_total|prior_ytd


# ---------------------------------------------------------------------------
# Derived metrics (SPEC §9.6)
# ---------------------------------------------------------------------------


class DerivedMetric(Base):
    __tablename__ = "derived_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    period_end: Mapped[date | None] = mapped_column(Date)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    value_decimal: Mapped[str | None] = mapped_column(Text)  # canonical decimal string
    unit: Mapped[str | None] = mapped_column(String(32))
    formula_version: Mapped[str | None] = mapped_column(String(32))
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    input_fact_ids_json: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(String(20))  # ok|missing|invalid|unsuitable


# ---------------------------------------------------------------------------
# Documents, sections, chunks (SPEC §9.7)
# ---------------------------------------------------------------------------


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    accession: Mapped[str | None] = mapped_column(String(32))
    form: Mapped[str | None] = mapped_column(String(16))
    document_kind: Mapped[str | None] = mapped_column(String(32))  # 10-Q | 10-K | 8-K-exhibit | pdf
    period_end: Mapped[date | None] = mapped_column(Date)
    filed_at: Mapped[date | None] = mapped_column(Date)
    source_url: Mapped[str | None] = mapped_column(Text)
    source_artifact_id: Mapped[int | None] = mapped_column(ForeignKey("source_artifacts.id"))
    extracted_path: Mapped[str | None] = mapped_column(Text)
    extraction_version: Mapped[str | None] = mapped_column(String(32))
    extraction_status: Mapped[str | None] = mapped_column(String(32))  # ok|needs_ocr|failed|pending


class Section(Base):
    __tablename__ = "sections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False, index=True)
    section_type: Mapped[str | None] = mapped_column(String(32))  # mda|risk_factors|other...
    heading: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str | None] = mapped_column(Text)
    start_offset: Mapped[int | None] = mapped_column(Integer)
    end_offset: Mapped[int | None] = mapped_column(Integer)
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False, index=True)
    section_id: Mapped[int | None] = mapped_column(ForeignKey("sections.id"))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("chunks.id"))
    strategy: Mapped[str] = mapped_column(String(32), nullable=False)  # fixed | section
    strategy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    text: Mapped[str | None] = mapped_column(Text)
    text_hash: Mapped[str | None] = mapped_column(String(64))
    token_count: Mapped[int | None] = mapped_column(Integer)
    start_offset: Mapped[int | None] = mapped_column(Integer)
    end_offset: Mapped[int | None] = mapped_column(Integer)
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "strategy",
            "strategy_version",
            "text_hash",
            name="uq_chunks_identity",
        ),
    )


# ---------------------------------------------------------------------------
# Embeddings (SPEC §9.8)
# ---------------------------------------------------------------------------


class Embedding(Base):
    __tablename__ = "embeddings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chunk_id: Mapped[int] = mapped_column(ForeignKey("chunks.id"), nullable=False, index=True)
    text_hash: Mapped[str | None] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    model_revision: Mapped[str | None] = mapped_column(String(128))
    dimension: Mapped[int | None] = mapped_column(Integer)
    normalized: Mapped[bool | None] = mapped_column(Boolean)
    vector: Mapped[bytes | None] = mapped_column(LargeBinary)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "chunk_id",
            "provider",
            "model",
            "model_revision",
            name="uq_embeddings_identity",
        ),
    )


# ---------------------------------------------------------------------------
# Operational records (SPEC §9.9)
# ---------------------------------------------------------------------------


class Price(Base):
    __tablename__ = "prices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    trade_date: Mapped[date | None] = mapped_column(Date)
    open: Mapped[str | None] = mapped_column(Text)
    high: Mapped[str | None] = mapped_column(Text)
    low: Mapped[str | None] = mapped_column(Text)
    close: Mapped[str | None] = mapped_column(Text)
    volume: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str | None] = mapped_column(String(8))
    source: Mapped[str | None] = mapped_column(String(32), default="yfinance")
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("company_id", "trade_date", name="uq_prices_company_trade_date"),
    )


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    endpoint: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str | None] = mapped_column(String(32))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    params_json: Mapped[str | None] = mapped_column(Text)
    error_type: Mapped[str | None] = mapped_column(String(128))


class RunEvent(Base):
    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    event_json: Mapped[str | None] = mapped_column(Text)


class BriefCache(Base):
    """Generation cache. Keys must reflect content versions, not only ticker+model."""

    __tablename__ = "brief_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cache_key: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"))
    period_end: Mapped[date | None] = mapped_column(Date)
    provider: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    response_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"))
    intent: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str | None] = mapped_column(String(32))
    tool_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    transition_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    state_json: Mapped[str | None] = mapped_column(Text)  # checkpoint state
    error_type: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AgentToolCall(Base):
    __tablename__ = "agent_tool_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_run_id: Mapped[int] = mapped_column(
        ForeignKey("agent_runs.id"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    arguments_json: Mapped[str | None] = mapped_column(Text)
    result_json: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ApprovalRequest(Base):
    """Approval tied to run id + exact memo content hash + export type + expiry."""

    __tablename__ = "approval_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    memo_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    export_type: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EvalRun(Base):
    __tablename__ = "eval_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_version: Mapped[str | None] = mapped_column(String(64))
    chunking_strategy: Mapped[str | None] = mapped_column(String(32))
    retrieval_config: Mapped[str | None] = mapped_column(String(64))
    config_json: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(String(32))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)


class EvalResult(Base):
    __tablename__ = "eval_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    eval_run_id: Mapped[int] = mapped_column(ForeignKey("eval_runs.id"), nullable=False, index=True)
    question_id: Mapped[str] = mapped_column(String(128), nullable=False)
    metric_name: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[str | None] = mapped_column(Text)  # decimal string or bool/int as text
    details_json: Mapped[str | None] = mapped_column(Text)


# Convenience re-export for typing tables that hold arbitrary JSON blobs.
JSONText = Any

__all__ = [
    "AgentRun",
    "AgentToolCall",
    "ApprovalRequest",
    "Base",
    "BriefCache",
    "Chunk",
    "Company",
    "DerivedMetric",
    "Document",
    "Embedding",
    "EvalResult",
    "EvalRun",
    "FactLineage",
    "FactObservation",
    "NormalizedFact",
    "Price",
    "Run",
    "RunEvent",
    "Section",
    "SourceArtifact",
    "decimal_to_text",
    "text_to_decimal",
]
