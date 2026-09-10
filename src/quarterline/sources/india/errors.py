"""India-source specific exceptions (IND-2)."""


class ManualImportRequired(Exception):
    """A source cannot be fetched from the runtime and requires a manual document import.

    Raised by the ``nse``/``bse`` stubs (every India endpoint tested rejected the
    non-browser HTTPS client — docs/india_source_audit.md §2, rescoped in §10:
    browser-assisted acquisition is a prototype, not proof that all automated
    access is impossible) and by code paths whose input was never acquired.
    Never a silent failure: the exception message points at the audit
    documentation and the manual-import command.
    """
