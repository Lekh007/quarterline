"""Unit tests for the observability events stub (contract C12) and CLI registry."""

from __future__ import annotations

import json
from pathlib import Path

from quarterline.cli import SUBCOMMAND_REGISTRY
from quarterline.observability.events import emit_run_event, new_run_id


def test_new_run_id_is_uuid4_hex() -> None:
    run_id = new_run_id()
    assert len(run_id) == 32
    int(run_id, 16)  # hex
    assert run_id != new_run_id()


def test_emit_run_event_appends_json_lines(offline_env: Path) -> None:
    run_id = new_run_id()
    emit_run_event(run_id, {"event": "test_start", "endpoint": "/health"})
    emit_run_event(new_run_id(), {"event": "other"})

    log_path = offline_env / "logs" / "events.jsonl"
    assert log_path.is_file()
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["run_id"] == run_id
    assert first["event"] == "test_start"
    assert first["endpoint"] == "/health"
    assert "timestamp" in first


def test_registry_has_wave0_handlers() -> None:
    assert "db:upgrade" in SUBCOMMAND_REGISTRY
    assert "serve" in SUBCOMMAND_REGISTRY


def test_eval_cli_handlers_registered_at_wave3() -> None:
    # Wave-3 boundary update (same as the wave-1/wave-2 updates before it):
    # `eval retrieval` and `eval generation` are implemented by the F6
    # evaluation track, so the stub-notice assertions become registration
    # assertions. cli.main lazy-imports quarterline.eval, which registers
    # its handlers into SUBCOMMAND_REGISTRY at import time.
    import quarterline.eval  # noqa: F401 (registers the eval handlers)

    assert "eval:retrieval" in SUBCOMMAND_REGISTRY
    assert "eval:generation" in SUBCOMMAND_REGISTRY
