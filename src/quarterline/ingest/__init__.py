"""Ingestion pipelines.

Importing this package registers the ``ingest``/``verify`` CLI handlers of
every implemented track into :data:`quarterline.cli.SUBCOMMAND_REGISTRY`
(cli.py lazy-imports wave packages in ``main()``).
"""

from quarterline.ingest import cache, documents, facts

__all__ = ["cache", "documents", "facts"]
