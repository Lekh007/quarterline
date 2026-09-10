"""India sources package (IND-2): SEBI in-capmkt parsing + reconciliation.

Feasibility milestone for the India expansion (SPEC 27): deterministic parsing of
NSE Integrated Filing (Finance) XBRL instances, lakh/crore scale handling,
Indian fiscal periods, original-vs-revised carrying, manual document import, and
source-linked reconciliation output. No scoring, no recommendations, no LLM.

Importing this package registers the India CLI handlers into
``quarterline.cli.SUBCOMMAND_REGISTRY``:

- ``ingest:india-document`` (``quarterline.sources.india.ir_documents``)
- ``verify:india`` (``quarterline.sources.india.reconcile``)

KNOWN WIRING GAP (reported; orchestrator to fix — ``cli.py`` is not owned by
this wave):

1. ``quarterline.cli._WAVE_PACKAGES`` does not list ``quarterline.sources.india``,
   so ``cli.main`` never imports this package and the handlers never register.
   Fix: add ``"quarterline.sources.india"`` to the tuple.
2. ``cli.build_parser()`` declares no subparsers for these commands, so
   ``quarterline ingest india-document ...`` / ``quarterline verify india ...``
   fail at argparse level regardless of registration. Fix: add an
   ``ingest``/``india-document`` subparser (args: --issuer --file --type
   --period-start --period-end --scope --published [--source-url] [--notes]
   [--exchange] [--seq-id] [--audited] [--revision], registry_key
   ``ingest:india-document``) and a ``verify``/``india`` subparser (arg --issuer,
   registry_key ``verify:india``).

Until wired, call the handlers programmatically::

    import quarterline.sources.india  # registers handlers
    from quarterline.cli import SUBCOMMAND_REGISTRY
    SUBCOMMAND_REGISTRY["verify:india"](argparse.Namespace(issuer="IN-INFY"))
"""

from __future__ import annotations

from quarterline.sources.india import (
    bse,
    cash_flow,
    concept_map,
    data_status,
    errors,
    ir_documents,
    issuers,
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
from quarterline.sources.india.ir_documents import ImportReport, import_document
from quarterline.sources.india.pipeline import IndiaIngestReport, ingest_observations
from quarterline.sources.india.reconcile import (
    ReconciliationRow,
    reconciliation_table,
    write_supersession_note,
    write_validation_csv,
)

__all__ = [
    "ImportReport",
    "IndiaIngestReport",
    "MissingDataStatus",
    "ReconciliationRow",
    "ReportedCashFlow",
    "bse",
    "cash_flow",
    "concept_map",
    "data_status",
    "derive_by_subtraction",
    "errors",
    "import_document",
    "ingest_observations",
    "ir_documents",
    "issuers",
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
