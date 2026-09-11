"""India sources package (IND-2/3/4): SEBI in-capmkt parsing, reconciliation,
canonical facts, metrics, and the fact card.

Feasibility-through-data-layer milestone for the India expansion (SPEC 27):
deterministic parsing of NSE Integrated Filing (Finance) XBRL instances,
lakh/crore scale handling, Indian fiscal periods, original-vs-revised carrying,
manual document import, source-linked reconciliation, canonical fact selection
with lineage (``india-normalization-v1``), versioned metrics
(``india-metrics-v1``), and the India fact card. No scoring, no recommendations,
no LLM.

Importing this package registers the India CLI handlers into
``quarterline.cli.SUBCOMMAND_REGISTRY``:

- ``ingest:india-document`` (``quarterline.sources.india.ir_documents``)
- ``verify:india`` (``quarterline.sources.india.reconcile``)
- ``verify:india-facts`` (``quarterline.sources.india.factcard``)

WIRING GAP (reported; orchestrator to fix — ``cli.py`` is not owned by this
wave): ``quarterline.sources.india`` IS listed in ``cli._WAVE_PACKAGES`` and the
``ingest india-document`` / ``verify india`` subparsers exist, but
``cli.build_parser()`` declares NO ``verify india-facts`` subparser, so
``quarterline verify india-facts --issuer IN-INFY`` fails at argparse level
(``invalid choice``) regardless of registration. Fix: add under the ``verify``
subparsers::

    verify_india_facts = verify_sub.add_parser(
        "india-facts", help="canonical India fact card (facts + metrics + coverage)"
    )
    verify_india_facts.add_argument("--issuer", required=True,
        help="issuer_id from data/watchlist_india.csv")
    verify_india_facts.add_argument("--scope", default="consolidated",
        choices=["consolidated", "standalone"])
    verify_india_facts.add_argument("--period-end", default=None,
        help="period end to show (YYYY-MM-DD; default: latest)")
    verify_india_facts.set_defaults(registry_key="verify:india-facts")

Until wired, call the handler programmatically::

    import quarterline.sources.india  # registers handlers
    from quarterline.cli import SUBCOMMAND_REGISTRY
    SUBCOMMAND_REGISTRY["verify:india-facts"](
        argparse.Namespace(issuer="IN-INFY", scope="consolidated", period_end=None)
    )
"""

from __future__ import annotations

from quarterline.sources.india import (
    bse,
    cash_flow,
    concept_map,
    data_status,
    errors,
    factcard,
    ir_documents,
    issuers,
    metrics,
    normalization,
    nse,
    pdf_results,
    periods,
    reconcile,
    revisions,
    units,
    xbrl_parse,
)
from quarterline.sources.india.cash_flow import (
    ReportedCashFlow,
    derive_by_subtraction,
    reporting_duration,
)
from quarterline.sources.india.data_status import MissingDataStatus
from quarterline.sources.india.factcard import (
    IndiaFactCard,
    build_india_fact_card,
    coverage_report,
)
from quarterline.sources.india.ir_documents import ImportReport, import_document
from quarterline.sources.india.metrics import (
    FORMULA_VERSION_INDIA_METRICS,
    INDIA_METRIC_IDS,
    IndiaMetricResult,
    compute_india_metrics,
)
from quarterline.sources.india.normalization import (
    NORMALIZATION_VERSION,
    IndiaNormalizationReport,
    normalize_canonical_facts,
)
from quarterline.sources.india.pipeline import (
    IndiaIngestReport,
    ingest_observations,
    ingest_reviewed_pdf_cash_flow,
)
from quarterline.sources.india.reconcile import (
    ReconciliationRow,
    reconciliation_table,
    write_supersession_note,
    write_validation_csv,
)

__all__ = [
    "FORMULA_VERSION_INDIA_METRICS",
    "INDIA_METRIC_IDS",
    "NORMALIZATION_VERSION",
    "ImportReport",
    "IndiaFactCard",
    "IndiaIngestReport",
    "IndiaMetricResult",
    "IndiaNormalizationReport",
    "MissingDataStatus",
    "ReconciliationRow",
    "ReportedCashFlow",
    "bse",
    "build_india_fact_card",
    "cash_flow",
    "compute_india_metrics",
    "concept_map",
    "coverage_report",
    "data_status",
    "derive_by_subtraction",
    "errors",
    "factcard",
    "import_document",
    "ingest_observations",
    "ingest_reviewed_pdf_cash_flow",
    "ir_documents",
    "issuers",
    "metrics",
    "normalization",
    "normalize_canonical_facts",
    "nse",
    "pdf_results",
    "periods",
    "reconcile",
    "reconciliation_table",
    "reporting_duration",
    "revisions",
    "units",
    "write_supersession_note",
    "write_validation_csv",
    "xbrl_parse",
]
