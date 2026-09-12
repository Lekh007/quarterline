"""IND-8 one real-model India brief run (INFY Q1 FY27, Ollama qwen3:4b).

Runs the REAL pipeline against the dev store (storage/quarterline.db, the
IND-7 real-embedding index) and the default Ollama provider. Prints the
outcome verbatim: status, gate reasons, latencies, validation checks. The
outcome is recorded for docs/india_briefs.md; an unvalidated brief is NEVER
presented as one.
"""

import json
import sys
import time

from quarterline.config import Settings
from quarterline.sources.india.brief import generate_india_brief

timeout_seconds = float(sys.argv[1]) if len(sys.argv) > 1 else None
settings = Settings(generation_timeout_seconds=timeout_seconds) if timeout_seconds else None

start = time.perf_counter()
outcome = generate_india_brief("IN-INFY", "2026-06-30", scope="consolidated", settings=settings)
total = (time.perf_counter() - start) * 1000

print("=== IND-8 real-model India brief run ===")
print(
    "issuer:", outcome.issuer_id, "| scope:", outcome.scope, "| period:", outcome.application_label
)
print("provider:", outcome.provider, "| model:", outcome.model)
print("STATUS:", outcome.status)
print("cache_hit:", outcome.cache_hit)
print("run_id:", outcome.run_id)
print("reasons (verbatim):")
for reason in outcome.reasons:
    print("  -", reason)
if outcome.validation is not None:
    print("gate:", outcome.validation.version)
    for check in outcome.validation.checks:
        print(
            f"  check[{check.check_id}] {check.name}: "
            f"{'PASS' if check.passed else 'FAIL'} (dropped {check.dropped_count})"
        )
print("brief bullets retained:", len(outcome.brief.bullets) if outcome.brief else 0)
print("metric_facts:", outcome.metric_facts)
print("evidence windows supplied:", len(outcome.evidence))
print("total latency ms:", round(total, 1))
print("outcome kind:", outcome.kind, "| brief is None:", outcome.brief is None)
print(
    json.dumps(
        {
            "status": outcome.status,
            "issuer": outcome.issuer_id,
            "scope": outcome.scope,
            "application_label": outcome.application_label,
            "provider": outcome.provider,
            "model": outcome.model,
            "reasons": outcome.reasons,
            "checks": (
                [
                    {
                        "id": c.check_id,
                        "name": c.name,
                        "passed": c.passed,
                        "dropped": c.dropped_count,
                    }
                    for c in outcome.validation.checks
                ]
                if outcome.validation
                else []
            ),
            "retained_bullets": len(outcome.brief.bullets) if outcome.brief else 0,
            "total_latency_ms": round(total, 1),
        },
        indent=2,
        ensure_ascii=False,
    )
)
