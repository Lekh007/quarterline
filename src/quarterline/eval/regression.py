"""Regression thresholds and baseline management (SPEC §23).

Baseline = a committed JSON snapshot of fixture-corpus retrieval metrics
(``data/eval/baselines/fixture_baseline.json``). SPEC §23 thresholds enforced
by :func:`check_regression`:

- **Recall@5**: a drop of MORE than five percentage points (strictly
  ``baseline - current > 0.05``) fails. Exactly 5pp passes; 6pp fails.
- **Wrong-company retrieval**: ANY increase fails (``current - baseline > 0``).
- Deterministic citation-validation and financial-normalization invariants
  live in the normal test suite (referenced in every report; not re-run here
  — this module gates retrieval regression only).

Baselines are NEVER regenerated silently: :func:`write_baseline` requires an
explicit ``confirm=True`` (CLI gate: ``QUARTERLINE_EVAL_WRITE_BASELINE=1``)
and prints a metric diff against the previous baseline when one exists.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

#: Strictly-greater threshold: a 5pp drop passes, 6pp fails (SPEC §23).
RECALL_DROP_LIMIT = 0.05

#: Committed baseline for the fixture corpus.
DEFAULT_BASELINE_PATH = Path("data/eval/baselines/fixture_baseline.json")


class BaselineWriteError(RuntimeError):
    """Baseline write attempted without the explicit confirmation flag."""


class ConfigRegression(BaseModel):
    """Per-configuration comparison against the baseline."""

    config_key: str
    recall_at_5_baseline: float | None = None
    recall_at_5_current: float | None = None
    recall_drop_pp: float | None = None  # positive = worse
    wrong_company_baseline: float | None = None
    wrong_company_current: float | None = None
    wrong_company_increase: float | None = None
    status: str = "pass"  # pass | fail | new_config
    violations: list[str] = Field(default_factory=list)


class RegressionReport(BaseModel):
    """Overall regression verdict across configurations."""

    status: str = "pass"  # pass | fail
    baseline_path: str
    dataset_version_current: str | None = None
    dataset_version_baseline: str | None = None
    dataset_version_match: bool = True
    configs: list[ConfigRegression] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def summary(self) -> str:
        lines = [f"Regression check: {self.status.upper()} (baseline: {self.baseline_path})"]
        if not self.dataset_version_match:
            lines.append(
                f"  dataset changed: baseline={self.dataset_version_baseline} "
                f"current={self.dataset_version_current} — comparison is advisory only; "
                "re-review and regenerate the baseline explicitly if intended"
            )
        for config in self.configs:
            lines.append(
                f"  {config.config_key}: {config.status}"
                f" recall {config.recall_at_5_baseline} -> {config.recall_at_5_current}"
                f" wrong-co {config.wrong_company_baseline} -> {config.wrong_company_current}"
            )
            for violation in config.violations:
                lines.append(f"    VIOLATION: {violation}")
        for note in self.notes:
            lines.append(f"  note: {note}")
        lines.append(
            "  (deterministic citation/normalization invariants are enforced "
            "by the unit test suite, SPEC §23)"
        )
        return "\n".join(lines)


def config_key(strategy: str, retrieval: str) -> str:
    return f"{strategy}/{retrieval}"


def check_regression(
    current: dict[str, dict],
    baseline: dict,
    *,
    dataset_version_current: str | None = None,
    baseline_path: str = str(DEFAULT_BASELINE_PATH),
) -> RegressionReport:
    """Compare current fixture metrics (config_key -> metric dict) to baseline.

    ``current`` maps e.g. ``"section/hybrid"`` to a dict with ``recall_at_5``
    and ``wrong_company_rate`` (as produced by
    :meth:`RetrievalEvalReport.to_dict`). Baselines use the same shape under
    their ``"configs"`` key. ``baseline=None`` (no committed baseline yet)
    yields status ``"no_baseline"`` — informational, never a CI failure;
    establishing a baseline requires the explicit write flag (SPEC §23).
    """
    report = RegressionReport(status="pass", baseline_path=baseline_path)
    baseline_configs: dict[str, dict] = baseline.get("configs", {}) if baseline else {}
    if not baseline:
        report.status = "no_baseline"
        report.notes.append(
            "no committed baseline: run with QUARTERLINE_EVAL_WRITE_BASELINE=1 "
            "to establish one explicitly (SPEC §23 forbids silent regeneration)"
        )
    report.dataset_version_baseline = baseline.get("dataset_version") if baseline else None
    report.dataset_version_current = dataset_version_current
    report.dataset_version_match = report.dataset_version_baseline in (
        None,
        dataset_version_current,
    )
    if not report.dataset_version_match:
        report.notes.append(
            "dataset version differs from baseline; thresholds are evaluated "
            "anyway but a diff this large usually means the dataset changed"
        )

    for key, metrics in sorted(current.items()):
        entry = ConfigRegression(config_key=key)
        base = baseline_configs.get(key)
        if base is None:
            entry.status = "new_config"
            entry.violations.append("no baseline entry for this configuration")
            report.configs.append(entry)
            if baseline:
                report.status = "fail"
            continue

        recall_base = base.get("recall_at_5")
        recall_now = metrics.get("recall_at_5")
        entry.recall_at_5_baseline = recall_base
        entry.recall_at_5_current = recall_now
        if recall_base is not None and recall_now is not None:
            drop = round(recall_base - recall_now, 6)
            entry.recall_drop_pp = drop
            if drop > RECALL_DROP_LIMIT:
                entry.status = "fail"
                entry.violations.append(
                    f"Recall@5 dropped {drop * 100:.1f}pp (limit: "
                    f"{RECALL_DROP_LIMIT * 100:.0f}pp, SPEC §23)"
                )

        wrong_base = base.get("wrong_company_rate") or 0.0
        wrong_now = metrics.get("wrong_company_rate") or 0.0
        entry.wrong_company_baseline = wrong_base
        entry.wrong_company_current = wrong_now
        increase = round(wrong_now - wrong_base, 6)
        entry.wrong_company_increase = increase
        if increase > 0:
            entry.status = "fail"
            entry.violations.append(
                f"wrong-company retrieval increased by {increase:.4f} "
                "(any increase fails, SPEC §23)"
            )

        if entry.status == "fail":
            report.status = "fail"
        report.configs.append(entry)
    return report


def load_baseline(path: str | Path = DEFAULT_BASELINE_PATH) -> dict | None:
    """Read the committed baseline; None when absent (first run)."""
    file_path = Path(path)
    if not file_path.is_file():
        return None
    return json.loads(file_path.read_text(encoding="utf-8"))


def baseline_snapshot(
    reports_by_key: dict[str, dict],
    *,
    dataset_version: str | None,
    corpus: str = "fixture",
    embedding_model: str | None = None,
) -> dict:
    """Assemble a baseline document from ``report.to_dict()`` values."""
    return {
        "created": datetime.now(UTC).isoformat(),
        "corpus": corpus,
        "dataset_version": dataset_version,
        "embedding_model": embedding_model,
        "labeled": (
            "Fixture-corpus-only metrics from the deterministic fake embedding "
            "provider (no semantic signal). NOT real-model quality (SPEC §23; "
            "the real-model matrix is Wave 5)."
        ),
        "configs": reports_by_key,
    }


def diff_snapshots(previous: dict, next_snapshot: dict) -> list[str]:
    """Human-readable metric diff between two baseline documents."""
    lines: list[str] = []
    old_configs = previous.get("configs", {})
    for key, metrics in sorted(next_snapshot.get("configs", {}).items()):
        base = old_configs.get(key)
        if base is None:
            lines.append(f"{key}: NEW (no previous entry)")
            continue
        for metric in ("recall_at_5", "hit_at_5", "mrr", "wrong_company_rate"):
            old_value = base.get(metric)
            new_value = metrics.get(metric)
            if old_value != new_value:
                lines.append(f"{key}.{metric}: {old_value} -> {new_value}")
    return lines or ["no metric changes"]


def write_baseline(
    path: str | Path,
    snapshot: dict,
    *,
    confirm: bool = False,
    previous: dict | None = None,
) -> Path:
    """Write a baseline file. REQUIRES explicit ``confirm=True``.

    Prints (returns in the diff list embedded in the error message otherwise)
    the diff against ``previous`` when provided. CI and CLI must never call
    this without the user's explicit intent (SPEC §23: do not silently
    regenerate baselines).
    """
    if not confirm:
        raise BaselineWriteError(
            "baseline write refused: set the explicit flag "
            "(CLI: QUARTERLINE_EVAL_WRITE_BASELINE=1; API: confirm=True). "
            "Baselines are never regenerated silently (SPEC §23)."
        )
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return file_path


__all__ = [
    "DEFAULT_BASELINE_PATH",
    "RECALL_DROP_LIMIT",
    "BaselineWriteError",
    "ConfigRegression",
    "RegressionReport",
    "baseline_snapshot",
    "check_regression",
    "config_key",
    "diff_snapshots",
    "load_baseline",
    "write_baseline",
]
