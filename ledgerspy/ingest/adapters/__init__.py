"""Source adapters. Every adapter produces the same canonical Transaction; there is one downstream schema."""

from ledgerspy.ingest.adapters.generic_ledger import GenericLedgerConverter
from ledgerspy.ingest.adapters.paysim import PaySimConverter

CONVERTERS: dict[str, type[GenericLedgerConverter]] = {
    "generic_ledger": GenericLedgerConverter,
    "paysim": PaySimConverter,
}

__all__ = ["CONVERTERS", "GenericLedgerConverter", "PaySimConverter"]
