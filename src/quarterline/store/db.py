"""Database engine and session management (contract C2)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from quarterline.config import get_settings


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


@cache
def get_engine(database_url: str | None = None) -> Engine:
    """Return a cached engine for ``database_url`` (or the configured URL).

    SQLite connections get ``PRAGMA foreign_keys=ON`` on every connect so FK
    constraints are enforced without per-session setup.
    """
    url = database_url or get_settings().database_url
    connect_args = {"check_same_thread": False} if _is_sqlite(url) else {}
    engine = create_engine(url, connect_args=connect_args, future=True)
    if _is_sqlite(url):

        @event.listens_for(engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


@contextmanager
def session_scope(database_url: str | None = None) -> Iterator[Session]:
    """Provide a transactional session: commit on success, rollback on error."""
    engine = get_engine(database_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_db_caches() -> None:
    """Clear cached settings/engines. Used by tests and after env changes."""
    get_settings.cache_clear()
    get_engine.cache_clear()
