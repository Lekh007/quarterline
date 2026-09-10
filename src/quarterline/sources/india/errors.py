"""India-source specific exceptions (IND-2)."""


class ManualImportRequired(Exception):
    """A source cannot be fetched from the runtime and requires a manual document import.

    Raised by the ``nse``/``bse`` stubs (every Indian exchange/IR endpoint rejects
    non-browser HTTP clients — docs/india_source_audit.md §2) and by code paths
    whose input was never acquired. Never a silent failure: the exception message
    points at the audit documentation and the manual-import command.
    """
