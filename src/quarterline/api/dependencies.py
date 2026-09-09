"""Shared FastAPI dependencies."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy.orm import Session

from quarterline.config import Settings, get_settings
from quarterline.store.db import session_scope


def db_session() -> Iterator[Session]:
    """Yield a transactional session per request (commit/rollback via session_scope)."""
    with session_scope() as session:
        yield session


def current_settings() -> Settings:
    """Request-time settings lookup (reads env changes made after app creation)."""
    return get_settings()
