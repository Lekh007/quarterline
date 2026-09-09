"""Session-scoped repository base (contract C5).

Repositories are thin, typed data-access objects bound to one SQLAlchemy
``Session``. No raw SQL escapes past this layer (SPEC 2.3.5): the agent/LLM
layer may only call these methods, never issue queries.
"""

from __future__ import annotations

from sqlalchemy.orm import Session


class BaseRepo:
    """Common session holder for all repositories."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def flush(self) -> None:
        self.session.flush()
