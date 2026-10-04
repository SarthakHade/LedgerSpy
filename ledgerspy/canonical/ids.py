"""Canonical ID scheme (docs/architecture.md §4).

Transaction IDs are deterministic: the same evidence file and the same 1-based data row always give the same
`txn_id`, regardless of when, where or in which order rows are processed. Entity and case IDs are ULIDs,
assigned once and then stored.
"""

import re
import secrets
import time

from ledgerspy.hashing import sha256_hex

TXN_ID_PREFIX = "T-"
# 16 hex = 64 bits. Birthday bound for 10^6 rows of one file: ~2.7e-8 collision probability
# (12 hex / 48 bits, the original design value, would be ~1.8e-3). See architecture.md §4.
TXN_ID_HEX_LEN = 16

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_CROCKFORD32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def txn_id(evidence_sha256: str, source_row: int, source_id: str | None = None) -> str:
    """Transaction ID.

    If the dataset has a source ID column whose values are unique and stable (checked by ingestion), that
    value is the ID. Otherwise: ``T-`` + first 16 hex of ``sha256(evidence_sha256 + ":" + source_row)``.
    The input is unambiguous: the file hash is exactly 64 lowercase hex characters (no ``:``), followed by
    the decimal row number, so distinct (file, row) pairs never produce the same hash input.
    """
    if source_id is not None:
        sid = source_id.strip()
        if not sid:
            raise ValueError("source_id must be non-empty when given")
        return sid
    if not isinstance(evidence_sha256, str) or not _SHA256_HEX.match(evidence_sha256):
        raise ValueError("evidence_sha256 must be 64 lowercase hex characters")
    if isinstance(source_row, bool) or not isinstance(source_row, int) or source_row < 1:
        raise ValueError("source_row must be a 1-based integer data row")
    digest = sha256_hex(f"{evidence_sha256}:{source_row}")
    return TXN_ID_PREFIX + digest[:TXN_ID_HEX_LEN]


def new_ulid(timestamp_ms: int | None = None) -> str:
    """26-character Crockford base32 ULID: 48-bit millisecond timestamp + 80 random bits."""
    ts = int(time.time() * 1000) if timestamp_ms is None else timestamp_ms
    if not 0 <= ts < 1 << 48:
        raise ValueError("timestamp out of ULID range")
    value = (ts << 80) | int.from_bytes(secrets.token_bytes(10), "big")
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD32[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def new_entity_id() -> str:
    return "E-" + new_ulid()


def new_case_id() -> str:
    return "C-" + new_ulid()
