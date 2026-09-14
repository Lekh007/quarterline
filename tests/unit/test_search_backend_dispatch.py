"""The repo and the query functions must agree on the storage backend.

Regression test for a real failure: ``SearchService`` selected the PostgreSQL
*repo* from ``DATABASE_URL`` but still called the SQLite query functions, so
every lexical or hybrid search on the pgvector profile died in psycopg with
``syntax error at or near "MATCH"`` — SQLite FTS5 syntax sent to PostgreSQL.

The pgvector contract tests could not catch it: they call
``search_lexical_postgres`` directly and never go through the service.

No database is touched here — the dispatch is the whole contract.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from quarterline.retrieve import search as search_module
from quarterline.retrieve.lexical import search_lexical
from quarterline.retrieve.search import SearchService
from quarterline.retrieve.vector import search_dense
from quarterline.store.repositories.search_postgres import (
    search_dense_postgres,
    search_lexical_postgres,
)


class _StubRepo:
    def __init__(self) -> None:
        self.ensured = False

    def ensure_schema(self) -> None:
        self.ensured = True


@pytest.fixture
def stub_repo(monkeypatch) -> _StubRepo:
    repo = _StubRepo()
    monkeypatch.setattr(search_module, "_repo_for", lambda session, settings: repo)
    return repo


def _service(url: str) -> SearchService:
    return SearchService(session=None, settings=SimpleNamespace(database_url=url))


POSTGRES_URLS = [
    "postgresql://quarterline:quarterline@127.0.0.1:5432/quarterline",
    "postgres://quarterline:quarterline@127.0.0.1:5432/quarterline",
    "postgresql+psycopg://quarterline:quarterline@db:5432/quarterline",
    "postgresql+psycopg2://quarterline:quarterline@db:5432/quarterline",
]


@pytest.mark.parametrize("url", POSTGRES_URLS)
def test_postgres_urls_bind_the_postgres_query_functions(url: str, stub_repo) -> None:
    service = _service(url)
    assert service._search_lexical is search_lexical_postgres
    assert service._search_dense is search_dense_postgres


@pytest.mark.parametrize(
    "url",
    ["sqlite:///storage/quarterline.db", "sqlite://", ""],
)
def test_sqlite_urls_keep_the_sqlite_query_functions(url: str, stub_repo) -> None:
    service = _service(url)
    assert service._search_lexical is search_lexical
    assert service._search_dense is search_dense


def test_schema_is_ensured_on_construction(stub_repo) -> None:
    _service("sqlite://")
    assert stub_repo.ensured is True


@pytest.mark.parametrize("url", [POSTGRES_URLS[0], "sqlite://"])
def test_query_functions_share_signatures(url: str, stub_repo) -> None:
    """Drop-in compatibility is what makes the dispatch safe."""
    import inspect

    service = _service(url)
    assert inspect.signature(service._search_lexical) == inspect.signature(search_lexical)
    assert inspect.signature(service._search_dense) == inspect.signature(search_dense)
