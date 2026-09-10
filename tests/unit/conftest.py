"""India-wave fixtures for unit tests (IND-2; new file, sibling of existing conftest).

Fixtures must live in a conftest for pytest to collect them; the logic itself
stays in ``tests/india_test_helpers.py`` so it remains importable by tests.
"""

from __future__ import annotations

import pytest
from india_test_helpers import create_schema, import_all_fixtures


@pytest.fixture
def india_store(offline_env):
    """Offline store with schema created (imports nothing yet)."""
    create_schema()
    return offline_env


@pytest.fixture
def india_imported(india_store):
    """Offline store with the 4 committed fixtures imported."""
    reports = import_all_fixtures()
    return india_store, reports
