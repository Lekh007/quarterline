"""Unit tests for the four typed agent tools (SPEC §20 Tools, §26 agent list).

Offline: audit rows land in a tmp SQLite DB; the price provider is a fake; the
search service is a stub. Covers: unknown-tool rejection, tool budget
enforcement (5th call blocked), read-once rule, read-only facts access (typed
args only — no SQL surface), path-traversal rejection, export filename being
server-generated, and the graceful price failure that cannot block a memo.
"""

from __future__ import annotations

import json

import pytest
from agent_test_helpers import FakePriceProvider, StubSearchService, create_schema

from quarterline.agent import tools
from quarterline.agent.tools import ToolContext, execute_tool
from quarterline.retrieve.models import SearchResult
from quarterline.store.db import session_scope
from quarterline.store.repositories.runs import RunsRepo


@pytest.fixture
def tool_env(offline_env):
    """Fresh schema + one checkpointed agent run (memo content seeded so the
    export approval verification reaches the approval lookup)."""
    from quarterline.agent.approval import memo_content_hash
    from quarterline.agent.checkpoints import persist_checkpoint
    from quarterline.agent.state import initial_state

    create_schema()
    state = initial_state(
        "a" * 32,
        {"ticker": "AAPL", "memo_type": "quarter_review"},
        tool_budget={"max_tool_calls": 4, "max_graph_transitions": 12},
        deadline_seconds=60.0,
        started_at_monotonic=0.0,
    )
    state["memo_content"] = "# memo\n\nSeeded content."
    state["memo_content_hash"] = memo_content_hash(state["memo_content"])
    with session_scope() as session:
        run_row_id = persist_checkpoint(session, state)
        yield session, run_row_id, str(offline_env)


def _context(session, run_row_id, storage_dir, **kwargs) -> ToolContext:
    defaults: dict = {
        "price_provider": FakePriceProvider(),
        "search_service": StubSearchService(SearchResult()),
    }
    defaults.update(kwargs)
    return ToolContext(
        session=session,
        run_id="a" * 32,
        run_row_id=run_row_id,
        storage_dir=storage_dir,
        **defaults,
    )


def test_unknown_tool_rejected_and_never_executes(tool_env) -> None:
    session, run_row_id, storage = tool_env
    context = _context(session, run_row_id, storage)

    outcome = execute_tool(context, "delete_all_filings", {"confirm": "yes"})

    assert outcome.executed is False
    assert outcome.rejection == "unknown_tool"
    assert "unknown tool" in (outcome.error or "")
    # The rejected attempt is still on the audit trail (never silent).
    calls = RunsRepo(session).list_tool_calls(run_row_id)
    assert [(c.tool_name, c.status) for c in calls] == [
        ("delete_all_filings", "rejected_unknown_tool")
    ]


def test_unknown_tool_rejected_before_budget_check(tool_env) -> None:
    """Allowlist lookup precedes the budget check (SPEC §20: rejected before
    invocation) — an unknown name is rejected even with budget exhausted."""
    session, run_row_id, storage = tool_env
    context = _context(session, run_row_id, storage, max_tool_calls=0)

    outcome = execute_tool(context, "run_sql", {"query": "DROP TABLE facts"})

    assert outcome.rejection == "unknown_tool"


def test_tool_budget_exhaustion_fifth_call_blocked(tool_env) -> None:
    session, run_row_id, storage = tool_env
    context = _context(session, run_row_id, storage, max_tool_calls=4)

    results = [
        execute_tool(context, "export_memo", {"run_id": "a" * 32, "export_type": "md"})
        for _ in range(5)
    ]

    assert [r.executed for r in results[:4]] == [True, True, True, True]
    fifth = results[4]
    assert fifth.executed is False
    assert fifth.rejection == "budget_exceeded"
    assert "budget exhausted" in (fifth.error or "")
    statuses = [c.status for c in RunsRepo(session).list_tool_calls(run_row_id)]
    assert statuses.count("ok") == 4, "export attempts executed (handler ran, approval missing)"
    assert statuses[-1] == "rejected_budget_exceeded"


def test_read_tool_at_most_once_per_run(tool_env) -> None:
    session, run_row_id, storage = tool_env
    context = _context(session, run_row_id, storage, max_tool_calls=4)

    first = execute_tool(context, "get_prices", {"ticker": "AAPL"})
    second = execute_tool(context, "get_prices", {"ticker": "AAPL"})

    assert first.executed and first.ok
    assert second.executed is False
    assert second.rejection == "repeated_tool"
    assert "at most once" in (second.error or "")


def test_facts_tool_is_typed_no_sql_surface(tool_env) -> None:
    """The facts tool has NO SQL-shaped argument: extra/unknown fields fail
    validation and a SQL string can never reach the repo layer (SPEC §2.3.5,
    §26 read-only facts access)."""
    session, run_row_id, storage = tool_env
    context = _context(session, run_row_id, storage)

    outcome = execute_tool(
        context,
        "get_company_facts",
        {"ticker": "AAPL", "sql": "SELECT * FROM normalized_facts"},
    )

    assert outcome.executed is False
    assert outcome.rejection == "invalid_arguments"
    assert "invalid arguments" in (outcome.error or "")
    args_model = tools.GetCompanyFactsArgs.model_fields
    assert set(args_model) == {"ticker", "period_end"}, "no SQL/paths surface on the facts tool"


def test_export_rejects_path_traversal_in_run_id(tool_env) -> None:
    session, run_row_id, storage = tool_env
    context = _context(session, run_row_id, storage)

    malicious_ids = [
        "../" + "a" * 29,  # 32 chars, ../ prefix
        "a" * 29 + "\\..",  # 32 chars, trailing backslash-dot-dot
        "a" * 10 + "/" + "b" * 10 + "/" + "c" * 10,  # path separators
        "." + "a" * 31,  # leading dot
    ]
    for malicious in malicious_ids:
        assert len(malicious) == 32
        outcome = execute_tool(context, "export_memo", {"run_id": malicious, "export_type": "md"})
        assert outcome.result.get("status") == "rejected", malicious
        assert "not a valid run id" in (outcome.result.get("error") or ""), malicious


def test_export_filename_is_server_generated(tmp_path, tool_env) -> None:
    """The filename is memo-{run_id}.{ext} — never client-controlled; the args
    model has no filename field at all."""
    assert set(tools.ExportMemoArgs.model_fields) == {"run_id", "export_type"}
    session, run_row_id, _storage = tool_env
    context = _context(session, run_row_id, str(tmp_path))
    # No approval exists: the handler must reject before touching the filesystem.
    outcome = execute_tool(context, "export_memo", {"run_id": "a" * 32, "export_type": "md"})
    assert outcome.result.get("status") == "rejected"
    assert not (tmp_path / "exports").exists() or not list((tmp_path / "exports").iterdir())


def test_export_requires_approval(tool_env) -> None:
    session, run_row_id, storage = tool_env
    context = _context(session, run_row_id, storage)
    execute_tool(
        context,
        "export_memo",
        {"run_id": "a" * 32, "export_type": "json"},
    )
    calls = RunsRepo(session).list_tool_calls(run_row_id)
    assert calls[-1].status == "ok"  # the handler ran and rejected in-result
    payload = json.loads(calls[-1].result_json)
    assert "no approved, unexpired approval" in payload["result"]["error"]


def test_prices_failure_is_graceful_and_non_blocking(tool_env) -> None:
    session, run_row_id, storage = tool_env
    context = _context(session, run_row_id, storage, price_provider=FakePriceProvider(fail=True))

    outcome = execute_tool(context, "get_prices", {"ticker": "AAPL"})

    assert outcome.executed and outcome.ok  # the tool call itself "succeeded"
    assert outcome.result["status"] == "price_unavailable"
    assert "simulated price outage" in outcome.result["error"]
    assert outcome.result.get("rows") is None


def test_prices_rows_are_informational_context(tool_env) -> None:
    session, run_row_id, storage = tool_env
    context = _context(session, run_row_id, storage)

    outcome = execute_tool(context, "get_prices", {"ticker": "AAPL"})

    assert outcome.result["status"] == "ok"
    assert outcome.result["informational_only"] is True
    assert outcome.result["rows"], "fake provider returned rows"


def test_audit_rows_carry_args_hash_and_duration(tool_env) -> None:
    session, run_row_id, storage = tool_env
    context = _context(session, run_row_id, storage)

    execute_tool(context, "get_prices", {"ticker": "AAPL"})

    call = RunsRepo(session).list_tool_calls(run_row_id)[0]
    arguments = json.loads(call.arguments_json)
    assert len(arguments["args_sha256"]) == 64
    assert arguments["arguments"] == {"ticker": "AAPL", "period_end": None}
    result = json.loads(call.result_json)
    assert result["duration_ms"] >= 0
