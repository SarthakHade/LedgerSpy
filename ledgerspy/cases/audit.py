"""Append-only, hash-chained audit log (docs/architecture.md §12).

Each entry stores ``hash = sha256(prev_hash || canonical_json(entry))`` where ``entry`` is
``{seq, ts, actor, action, object, details}`` and ``details`` is the exact stored JSON text. The first entry
chains to 64 zeros. SQLite triggers reject UPDATE and DELETE; `verify()` also checks those triggers are
intact, so a manual edit (which must drop or bypass them) is detected.

Limitation: removing entries from the *end* of the chain cannot be detected from the database alone. `verify`
reports the head hash and entry count so they can be recorded outside the database (e.g. in reports).
"""

import getpass
import sqlite3
from dataclasses import dataclass
from typing import Any

from ledgerspy.hashing import canonical_json, sha256_hex, utc_now_iso

GENESIS_HASH = "0" * 64

_NO_UPDATE = (
    "CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log "
    "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END"
)
_NO_DELETE = (
    "CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log "
    "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END"
)
_TRIGGERS = {"audit_log_no_update": _NO_UPDATE, "audit_log_no_delete": _NO_DELETE}

DDL = [
    """CREATE TABLE audit_log (
        seq       INTEGER PRIMARY KEY,
        ts        TEXT NOT NULL,
        actor     TEXT NOT NULL,
        action    TEXT NOT NULL,
        object    TEXT NOT NULL,
        details   TEXT NOT NULL,
        prev_hash TEXT NOT NULL,
        hash      TEXT NOT NULL UNIQUE
    )""",
    _NO_UPDATE,
    _NO_DELETE,
]


@dataclass(frozen=True)
class AuditEntry:
    seq: int
    ts: str
    actor: str
    action: str
    object: str
    details: str
    prev_hash: str
    hash: str


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    entries: int
    head_hash: str | None
    message: str
    failed_seq: int | None = None


def local_actor() -> str:
    """The local OS user name (single-user tool; architecture §13)."""
    try:
        return getpass.getuser() or "local-user"
    except (OSError, KeyError, ImportError):
        return "local-user"


def entry_hash(prev_hash: str, seq: int, ts: str, actor: str, action: str, obj: str, details: str) -> str:
    entry = {"seq": seq, "ts": ts, "actor": actor, "action": action, "object": obj, "details": details}
    return sha256_hex(prev_hash.encode("ascii") + canonical_json(entry))


def append(
    conn: sqlite3.Connection, actor: str, action: str, obj: str, details: dict[str, Any]
) -> AuditEntry:
    """Append one entry. Must be called inside `db.transaction()` together with the change it records."""
    if not conn.in_transaction:
        raise RuntimeError("audit.append must run inside a transaction")
    last = conn.execute("SELECT seq, hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
    seq, prev = (1, GENESIS_HASH) if last is None else (last[0] + 1, last[1])
    ts = utc_now_iso()
    details_json = canonical_json(details).decode("utf-8")
    digest = entry_hash(prev, seq, ts, actor, action, obj, details_json)
    conn.execute(
        "INSERT INTO audit_log (seq, ts, actor, action, object, details, prev_hash, hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (seq, ts, actor, action, obj, details_json, prev, digest),
    )
    return AuditEntry(seq, ts, actor, action, obj, details_json, prev, digest)


def verify(conn: sqlite3.Connection) -> VerifyResult:
    """Recompute the whole chain. Any edited, inserted, reordered or deleted (non-tail) entry fails."""
    triggers = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'audit_log'"
        )
    }
    for name, sql in _TRIGGERS.items():
        if triggers.get(name) != sql:
            return VerifyResult(False, 0, None, f"append-only trigger {name} is missing or altered")

    expected_seq, prev, count = 1, GENESIS_HASH, 0
    rows = conn.execute(
        "SELECT seq, ts, actor, action, object, details, prev_hash, hash FROM audit_log ORDER BY seq"
    )
    for seq, ts, actor, action, obj, details, prev_hash, stored in rows:
        if seq != expected_seq:
            return VerifyResult(
                False, count, prev, f"sequence gap: expected {expected_seq}, found {seq}", seq
            )
        if prev_hash != prev:
            return VerifyResult(
                False, count, prev, f"entry {seq}: prev_hash does not match entry {seq - 1}", seq
            )
        if not all(isinstance(v, str) for v in (ts, actor, action, obj, details)):
            return VerifyResult(False, count, prev, f"entry {seq}: field has wrong type", seq)
        if entry_hash(prev_hash, seq, ts, actor, action, obj, details) != stored:
            return VerifyResult(False, count, prev, f"entry {seq}: hash mismatch (entry was modified)", seq)
        prev, expected_seq, count = stored, seq + 1, count + 1
    if count == 0:
        return VerifyResult(
            False, 0, None, "audit log is empty (expected at least the schema migration entry)"
        )
    return VerifyResult(True, count, prev, f"{count} entries verified")
