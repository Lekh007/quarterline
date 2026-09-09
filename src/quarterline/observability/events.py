"""Observability events stub (contract C12).

Wave 0 writes JSON lines to ``{STORAGE_DIR}/logs/events.jsonl``. Wave F6
replaces the backend (persists to ``runs``/``run_events`` tables) but must keep
these exact signatures.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from quarterline.config import get_settings

_WRITE_LOCK = threading.Lock()


def new_run_id() -> str:
    """Return a fresh run id (uuid4 hex)."""
    return uuid.uuid4().hex


def emit_run_event(run_id: str, event: dict) -> None:
    """Append one JSON line with a timestamp and the shared run_id."""
    settings = get_settings()
    log_dir = Path(settings.storage_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    record: dict[str, object] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "run_id": run_id,
    }
    record.update(event)
    line = json.dumps(record, default=str, ensure_ascii=False)
    with _WRITE_LOCK, (log_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
