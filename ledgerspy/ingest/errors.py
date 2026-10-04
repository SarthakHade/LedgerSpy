"""Explicit, structured ingestion errors. Every rejection carries a stable code and a human message."""

from ledgerspy.errors import LedgerSpyError


class IngestError(LedgerSpyError):
    """A file (or mapping) is rejected. `http_status` is the API status the error maps to."""
