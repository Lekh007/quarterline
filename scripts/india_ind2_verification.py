"""IND-2 verification driver: INFY import -> ingest -> reconciliation table.

Run: uv run python scripts/india_ind2_verification.py
Uses a throwaway storage dir + sqlite database under the system temp directory,
so the project's own storage/ and database are never touched.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tests"))


def main() -> int:
    scratch = Path(tempfile.mkdtemp(prefix="india_ind2_verify_"))
    os.environ["STORAGE_DIR"] = str(scratch / "storage")
    os.environ["DATABASE_URL"] = f"sqlite:///{(scratch / 'app.db').as_posix()}"

    import quarterline.sources.india  # noqa: F401 - registers the India CLI handlers
    from quarterline.cli import SUBCOMMAND_REGISTRY

    print("CLI handlers registered:", sorted(k for k in SUBCOMMAND_REGISTRY if "india" in k))
    print()

    from quarterline.store.db import get_engine
    from quarterline.store.models import Base

    Base.metadata.create_all(get_engine())

    fixtures = REPO_ROOT / "tests" / "fixtures" / "india"
    manifest = json.loads((fixtures / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["committed_fixtures"]:
        if entry["issuer_id"] != "IN-INFY":
            continue
        info = manifest["periods"][entry["period"]]
        filing = entry["exchange_filing"]
        args = argparse.Namespace(
            issuer="IN-INFY",
            file=str(fixtures / entry["file"]),
            type=entry["doc_type"],
            period_start=info["period_start"],
            period_end=info["period_end"],
            scope=entry["scope"],
            published=filing["broadcast_ist"].split(" ")[0],
            source_url=entry["source_url"],
            notes=None,
            exchange="NSE",
            seq_id=filing["seq_id"],
            audited=filing["audited_status"],
            revision=filing["revision"],
        )
        # Imported through the registered CLI handler (the argparse wiring is the
        # orchestrator's pending task; the handler itself is live).
        rc = SUBCOMMAND_REGISTRY["ingest:india-document"](args)
        assert rc == 0

    from quarterline.sources.india.pipeline import ingest_observations

    first = ingest_observations("IN-INFY")
    print()
    print(
        f"ingest run 1: parsed={first.artifacts_parsed} inserted={first.observations_inserted} "
        f"skipped={first.observations_skipped} unmapped_tag_kinds={len(first.unmapped_tags)}"
    )
    second = ingest_observations("IN-INFY")
    print(
        f"ingest run 2 (idempotency): inserted={second.observations_inserted} "
        f"skipped={second.observations_skipped}"
    )
    print()
    rc = SUBCOMMAND_REGISTRY["verify:india"](argparse.Namespace(issuer="IN-INFY"))
    assert rc == 0
    print()
    print(f"(scratch store: {scratch})")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
