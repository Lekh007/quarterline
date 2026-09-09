"""Lexical retrieval over SQLite FTS5 (SPEC §16).

Sign convention (documented, SPEC §16 "account for its score ordering
correctly"): FTS5's ``bm25()`` returns *more negative = better*. This module
returns that raw value as ``score`` and attaches a 1-based ``rank`` computed
after ``ORDER BY score ASC`` (best first) — equivalent to FTS5's ``rank``
ordering. Fusion only uses ranks, so the sign never leaks into RRF.

Query sanitization (SPEC §2.3.4 — user input is untrusted): the raw query
never reaches MATCH. It is split into alphanumeric word tokens; every token is
double-quoted (internal double quotes escaped by doubling) and tokens are
joined with ``OR`` for recall. Anything that is not a word/phrase — operators
like ``NEAR``/``NOT``/``*``/``^``, column filters, parentheses — is dropped.

SQL hygiene (SPEC §2.3.5): fixed statement fragments + bound parameters only;
list filters use expanding bind params. Strategy arms for ``strategy="any"``
are composed from the same fixed fragments.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

#: Candidate pool size for the hybrid flow (SPEC §16: lexical top 20).
LEXICAL_TOP_K = 20

_WORD_RE = re.compile(r"[A-Za-z0-9_]{2,}", re.UNICODE)


@dataclass(frozen=True)
class LexicalHit:
    chunk_id: int
    score: float  # raw FTS5 bm25(): more negative = better (see module docstring)
    rank: int  # 1-based, best first


def sanitize_fts_query(query: str) -> str:
    """Build a safe FTS5 MATCH expression from untrusted user input.

    Every token is quoted as a phrase; quotes inside tokens are doubled
    (FTS5's phrase-escape rule). Returns an empty string when nothing
    searchable remains — callers then skip lexical search entirely.
    """
    tokens = _WORD_RE.findall(query or "")
    quoted = []
    for token in tokens:
        escaped = token.replace('"', '""')
        quoted.append(f'"{escaped}"')
    return " OR ".join(quoted)


@dataclass(frozen=True)
class LexicalFilters:
    """Metadata filters applied *with* the MATCH (SPEC §16)."""

    ticker: str
    #: e.g. ["fixed"], ["section"], or both for strategy="any".
    strategies: list[str]
    strategy_version_by_strategy: dict[str, str]
    sections: list[str] | None = None
    forms: list[str] | None = None
    period_end: date | None = None
    filed_before: date | None = None


_ARM_SQL = """
SELECT chunks_fts.chunk_id AS chunk_id, bm25(chunks_fts) AS score
FROM chunks_fts
JOIN chunks ON chunks.id = chunks_fts.chunk_id
JOIN documents ON documents.id = chunks.document_id
JOIN companies ON companies.id = documents.company_id
LEFT JOIN sections secs ON secs.id = chunks.section_id
WHERE chunks_fts MATCH :match
  AND companies.ticker = {ticker_param}
  AND chunks.strategy = {strategy_param}
  AND chunks.strategy_version = {version_param}{extra}
"""


def search_lexical(
    session: Session, query: str, filters: LexicalFilters, top_k: int = LEXICAL_TOP_K
) -> list[LexicalHit]:
    """FTS5 MATCH joined against the metadata filters, best-first."""
    match_expr = sanitize_fts_query(query)
    if not match_expr:
        return []

    params: dict[str, object] = {"match": match_expr, "k": top_k}
    bindparams_spec = [
        bindparam("match"),
        bindparam("k"),
    ]
    arms: list[str] = []
    for index, strategy in enumerate(filters.strategies):
        version = filters.strategy_version_by_strategy.get(strategy)
        if version is None:
            continue
        suffixed = {name: f"{name}_{index}" for name in ("strategy", "strategy_version")}
        extra_clauses: list[str] = []
        arm_params: dict[str, object] = {
            suffixed["strategy"]: strategy,
            suffixed["strategy_version"]: version,
        }
        bindparams_spec.extend(
            [bindparam(suffixed["strategy"]), bindparam(suffixed["strategy_version"])]
        )
        if filters.sections:
            # Explicit per-item params (fixed fragments, bound values) — list
            # values stay out of the SQL text entirely.
            placeholders = []
            for pos, section in enumerate(s.lower() for s in filters.sections):
                name = f"sections_{index}_{pos}"
                arm_params[name] = section
                bindparams_spec.append(bindparam(name))
                placeholders.append(f":{name}")
            extra_clauses.append(f"secs.section_type IN ({', '.join(placeholders)})")
        if filters.forms:
            placeholders = []
            for pos, form in enumerate(f.upper() for f in filters.forms):
                name = f"forms_{index}_{pos}"
                arm_params[name] = form
                bindparams_spec.append(bindparam(name))
                placeholders.append(f":{name}")
            extra_clauses.append(f"documents.form IN ({', '.join(placeholders)})")
        if filters.period_end is not None:
            arm_params[f"period_end_{index}"] = filters.period_end.isoformat()
            bindparams_spec.append(bindparam(f"period_end_{index}"))
            extra_clauses.append(f"documents.period_end = :period_end_{index}")
        if filters.filed_before is not None:
            arm_params[f"filed_before_{index}"] = filters.filed_before.isoformat()
            bindparams_spec.append(bindparam(f"filed_before_{index}"))
            extra_clauses.append(f"documents.filed_at <= :filed_before_{index}")
        extra = (" AND " + " AND ".join(extra_clauses)) if extra_clauses else ""
        ticker_param = f"ticker_{index}"
        arm_params[ticker_param] = filters.ticker
        bindparams_spec.append(bindparam(ticker_param))
        arm_sql = _ARM_SQL.format(
            extra=extra,
            ticker_param=f":{ticker_param}",
            strategy_param=f":{suffixed['strategy']}",
            version_param=f":{suffixed['strategy_version']}",
        )
        params.update(arm_params)
        arms.append(arm_sql)

    if not arms:
        return []

    # Strategy arms are disjoint row families (one row family each), so a
    # plain UNION ALL + ORDER BY score is correct; ties break on chunk id for
    # determinism.
    union_sql = "\nUNION ALL\n".join(arms)
    stmt = text(
        f"""
        SELECT chunk_id, score
        FROM ( {union_sql} )
        ORDER BY score ASC, chunk_id ASC
        LIMIT :k
        """
    ).bindparams(*bindparams_spec)
    rows = session.execute(stmt, params).all()
    return [
        LexicalHit(chunk_id=int(row.chunk_id), score=float(row.score), rank=i + 1)
        for i, row in enumerate(rows)
    ]


__all__ = [
    "LEXICAL_TOP_K",
    "LexicalFilters",
    "LexicalHit",
    "sanitize_fts_query",
    "search_lexical",
]
