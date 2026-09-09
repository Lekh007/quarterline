"""Unit tests for the observability events stub (contract C12) and CLI registry."""

from __future__ import annotations

import json
from pathlib import Path

from quarterline.cli import SUBCOMMAND_REGISTRY, main
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


def test_unimplemented_subcommands_exit_2_with_notice(capsys) -> None:
    # Wave-2 boundary update (same as the wave-1 update that removed
    # `ingest facts`): `index build` and `search` are implemented by the
    # retrieval track, so the still-stubbed assertions cover the wave-3
    # evaluation subcommands instead.
    assert main(["eval", "retrieval"]) == 2
    assert "not implemented until wave 3" in capsys.readouterr().err

    assert main(["eval", "generation"]) == 2
    assert "not implemented until wave 3" in capsys.readouterr().err
