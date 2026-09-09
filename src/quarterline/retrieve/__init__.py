"""Retrieval track (SPEC §14–§16).

Importing this package registers the ``index build`` / ``search`` CLI
handlers into :data:`quarterline.cli.SUBCOMMAND_REGISTRY` (cli.py
lazy-imports wave packages in ``main()``).
"""

from quarterline.retrieve import (
    chunk_fixed,
    chunk_section,
    context,
    embeddings,
    fusion,
    lexical,
    models,
    reranker,
    search,
    vector,
)
from quarterline.retrieve.chunk_fixed import ChunkStrategy, FixedWindowChunker, estimate_tokens
from quarterline.retrieve.chunk_section import SectionParentChildChunker
from quarterline.retrieve.context import ContextBundle, assemble_context
from quarterline.retrieve.embeddings import (
    EmbeddingModelMismatchError,
    EmbeddingProvider,
    EmbeddingProviderUnavailable,
    FakeEmbeddingProvider,
    OllamaEmbeddingProvider,
)
from quarterline.retrieve.models import (
    EVIDENCE_POLICY_VERSION,
    ChunkDraft,
    DocumentMeta,
    EvidenceItem,
    IndexStats,
    SearchQuery,
    SearchResult,
    chunk_identity,
    evidence_id_for,
)

__all__ = [
    "EVIDENCE_POLICY_VERSION",
    "ChunkDraft",
    "ChunkStrategy",
    "ContextBundle",
    "DocumentMeta",
    "EmbeddingModelMismatchError",
    "EmbeddingProvider",
    "EmbeddingProviderUnavailable",
    "EvidenceItem",
    "FakeEmbeddingProvider",
    "FixedWindowChunker",
    "IndexStats",
    "OllamaEmbeddingProvider",
    "SearchQuery",
    "SearchResult",
    "SectionParentChildChunker",
    "assemble_context",
    "chunk_fixed",
    "chunk_identity",
    "chunk_section",
    "context",
    "embeddings",
    "estimate_tokens",
    "evidence_id_for",
    "fusion",
    "lexical",
    "models",
    "reranker",
    "search",
    "vector",
]
