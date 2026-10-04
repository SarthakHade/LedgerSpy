"""Fix decisions, applying accepted fixes as logged transformations, and reverting them.

Lifecycle of a fix:  proposed → accepted | rejected → (accepted) applied → reverted → accepted | rejected …

* Nothing is applied without an explicit `accept`. Reinterpretations with several candidates need the chosen
  value; a rejection needs a reason.
* `apply_accepted` writes one `transformations` row per fix (original value, normalised value, transform,
  reason, run, actor, time) and the new value into `normalized_values`. Canonical rows never change.
* `revert` restores the exact original value from the log and appends a `revert` row.
* A fix is applied only if the field still holds the value it was proposed for (no lost updates).
"""

import sqlite3
from typing import Any

from ledgerspy.canonical.ids import new_ulid
from ledgerspy.cases import audit
from ledgerspy.cases.db import transaction
from ledgerspy.errors import LedgerSpyError
from ledgerspy.hashing import utc_now_iso
from ledgerspy.normalize.proposals import Proposal
from ledgerspy.normalize.view import canonical_value, decode, encode

_DECIDABLE = {"proposed", "accepted", "rejected", "reverted"}


def save_proposals(conn: sqlite3.Connection, dataset_id: str, proposals: list[Proposal], run_id: str) -> None:
    """Record proposals (inside the caller's transaction). An existing fix keeps its status and decisions."""
    conn.executemany(
        "INSERT OR IGNORE INTO fix_proposals (fix_id, dataset_id, txn_id, source_row, field, transform, "
        "original_value, proposed_value, candidates, requires_confirmation, reason, status, first_run_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?)",
        [
            (
                p.fix_id,
                dataset_id,
                p.txn_id,
                p.source_row,
                p.field,
                p.transform,
                p.original_value,
                p.proposed_value,
                encode(p.candidates),
                int(p.requires_confirmation),
                p.reason,
                run_id,
            )
            for p in proposals
        ],
    )


def _fix_dict(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    out["candidates"] = [decode(c) for c in decode(row["candidates"])]
    for k in ("original_value", "proposed_value", "chosen_value"):
        out[k] = None if row[k] is None else decode(row[k])
    out["requires_confirmation"] = bool(row["requires_confirmation"])
    return out


def list_fixes(conn: sqlite3.Connection, dataset_id: str, status: str | None = None) -> list[dict[str, Any]]:
    sql, args = "SELECT * FROM fix_proposals WHERE dataset_id = ?", [dataset_id]
    if status:
        sql, args = sql + " AND status = ?", [*args, status]
    return [_fix_dict(r) for r in conn.execute(sql + " ORDER BY source_row, field, fix_id", args)]


def decide(
    conn: sqlite3.Connection, dataset_id: str, decisions: list[dict[str, Any]], actor: str
) -> list[dict]:
    """Accept or reject fixes, all-or-nothing. Each decision: {fix_id, decision, value?, reason?}."""
    errors: list[dict[str, str]] = []
    planned: list[tuple[sqlite3.Row, str, str | None, str | None]] = []
    for d in decisions:
        fid, decision = d.get("fix_id"), d.get("decision")
        row = conn.execute(
            "SELECT * FROM fix_proposals WHERE fix_id = ? AND dataset_id = ?", (fid, dataset_id)
        ).fetchone()
        if row is None:
            errors.append({"fix_id": str(fid), "message": "no such fix in this dataset"})
            continue
        if row["status"] not in _DECIDABLE:
            errors.append(
                {"fix_id": fid, "message": f"a {row['status']} fix cannot be decided; revert it first"}
            )
            continue
        reason = (d.get("reason") or "").strip() or None
        if decision == "reject":
            if not reason:
                errors.append({"fix_id": fid, "message": "a rejection needs a reason"})
                continue
            planned.append((row, "reject", None, reason))
        elif decision == "accept":
            candidates = decode(row["candidates"])
            chosen = d.get("value")
            if chosen is None:
                if row["proposed_value"] is None:
                    errors.append(
                        {
                            "fix_id": fid,
                            "message": "several candidates: choose one with `value`",
                            "candidates": [decode(c) for c in candidates],
                        }
                    )
                    continue
                chosen_enc = row["proposed_value"]
            else:
                chosen_enc = encode(chosen)
                if chosen_enc not in candidates:
                    errors.append({"fix_id": fid, "message": "`value` must be one of the candidates"})
                    continue
            planned.append((row, "accept", chosen_enc, reason))
        else:
            errors.append({"fix_id": str(fid), "message": "decision must be 'accept' or 'reject'"})
    if errors or not planned:
        raise LedgerSpyError(
            "invalid_decisions",
            "No decision was recorded.",
            {"errors": errors or [{"message": "no decisions given"}]},
        )
    now = utc_now_iso()
    with transaction(conn):
        for row, decision, chosen, reason in planned:
            status = "accepted" if decision == "accept" else "rejected"
            conn.execute(
                "UPDATE fix_proposals SET status = ?, chosen_value = ?, decided_by = ?, decided_at = ?, "
                "decision_reason = ? WHERE fix_id = ?",
                (status, chosen, actor, now, reason, row["fix_id"]),
            )
            conn.execute(
                "INSERT INTO fix_decisions (fix_id, dataset_id, decision, chosen_value, reason, decided_by, "
                "decided_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (row["fix_id"], dataset_id, decision, chosen, reason, actor, now),
            )
            audit.append(
                conn,
                actor,
                f"fix.{status}",
                f"fix:{row['fix_id']}",
                {
                    "dataset_id": dataset_id,
                    "txn_id": row["txn_id"],
                    "field": row["field"],
                    "transform": row["transform"],
                    "chosen_value": chosen,
                    "reason": reason,
                },
            )
    return [
        _fix_dict(r)
        for r in conn.execute(
            f"SELECT * FROM fix_proposals WHERE fix_id IN ({','.join('?' * len(planned))})",  # noqa: S608
            [p[0]["fix_id"] for p in planned],
        )
    ]


def _current(conn: sqlite3.Connection, dataset_id: str, txn_id: str, field: str) -> tuple[str, str | None]:
    """(encoded effective value, transformation_id that set it or None if canonical)."""
    row = conn.execute(
        "SELECT value, transformation_id FROM normalized_values "
        "WHERE dataset_id = ? AND txn_id = ? AND field = ?",
        (dataset_id, txn_id, field),
    ).fetchone()
    if row:
        return row[0], row[1]
    return encode(canonical_value(conn, dataset_id, txn_id, field)), None


def apply_accepted(conn: sqlite3.Connection, dataset_id: str, actor: str) -> dict[str, Any]:
    """Apply every accepted fix as a logged transformation. Returns what was applied and any conflicts."""
    run_id = "M-" + new_ulid()
    now = utc_now_iso()
    applied, conflicts = [], []
    with transaction(conn):
        fixes = conn.execute(
            "SELECT * FROM fix_proposals WHERE dataset_id = ? AND status = 'accepted' "
            "ORDER BY source_row, field, fix_id",
            (dataset_id,),
        ).fetchall()
        for fix in fixes:
            current, _ = _current(conn, dataset_id, fix["txn_id"], fix["field"])
            if current != fix["original_value"]:
                conflicts.append(
                    {
                        "fix_id": fix["fix_id"],
                        "expected": decode(fix["original_value"]),
                        "found": decode(current),
                    }
                )
                continue
            tid = "N-" + new_ulid()
            new_value = fix["chosen_value"] or fix["proposed_value"]
            conn.execute(
                "INSERT INTO transformations (transformation_id, dataset_id, normalize_run_id, kind, "
                "reverts, "
                "fix_id, txn_id, source_row, field, transform, original_value, normalized_value, reason, "
                "applied_by, applied_at) VALUES (?, ?, ?, 'apply', NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tid,
                    dataset_id,
                    run_id,
                    fix["fix_id"],
                    fix["txn_id"],
                    fix["source_row"],
                    fix["field"],
                    fix["transform"],
                    fix["original_value"],
                    new_value,
                    fix["reason"],
                    actor,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO normalized_values (dataset_id, txn_id, field, value, transformation_id) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (dataset_id, txn_id, field) "
                "DO UPDATE SET value = excluded.value, transformation_id = excluded.transformation_id",
                (dataset_id, fix["txn_id"], fix["field"], new_value, tid),
            )
            conn.execute("UPDATE fix_proposals SET status = 'applied' WHERE fix_id = ?", (fix["fix_id"],))
            applied.append(
                {
                    "transformation_id": tid,
                    "fix_id": fix["fix_id"],
                    "txn_id": fix["txn_id"],
                    "source_row": fix["source_row"],
                    "field": fix["field"],
                    "transform": fix["transform"],
                    "original_value": decode(fix["original_value"]),
                    "normalized_value": decode(new_value),
                }
            )
        audit.append(
            conn,
            actor,
            "dataset.normalized",
            f"dataset:{dataset_id}",
            {
                "normalize_run_id": run_id,
                "applied": [a["transformation_id"] for a in applied],
                "conflicts": [c["fix_id"] for c in conflicts],
            },
        )
    return {"normalize_run_id": run_id, "applied": applied, "conflicts": conflicts}


def revert(
    conn: sqlite3.Connection, dataset_id: str, transformation_id: str, actor: str, reason: str
) -> dict:
    """Undo an applied transformation using the original value recorded in the log."""
    if not (reason or "").strip():
        raise LedgerSpyError("reason_required", "Reverting a transformation needs a reason.")
    with transaction(conn):
        t = conn.execute(
            "SELECT * FROM transformations WHERE transformation_id = ? AND dataset_id = ?",
            (transformation_id, dataset_id),
        ).fetchone()
        if t is None or t["kind"] != "apply":
            raise LedgerSpyError("not_found", "no applied transformation with this ID", http_status=404)
        if conn.execute("SELECT 1 FROM transformations WHERE reverts = ?", (transformation_id,)).fetchone():
            raise LedgerSpyError(
                "already_reverted", "this transformation was already reverted", http_status=409
            )
        _, setter = _current(conn, dataset_id, t["txn_id"], t["field"])
        if setter != transformation_id:
            raise LedgerSpyError(
                "not_latest",
                "a later transformation changed this field; revert that one first",
                http_status=409,
            )
        rid = "N-" + new_ulid()
        conn.execute(
            "INSERT INTO transformations (transformation_id, dataset_id, normalize_run_id, kind, reverts, "
            "fix_id, "
            "txn_id, source_row, field, transform, original_value, normalized_value, reason, applied_by, "
            "applied_at) VALUES (?, ?, ?, 'revert', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rid,
                dataset_id,
                "M-" + new_ulid(),
                transformation_id,
                t["fix_id"],
                t["txn_id"],
                t["source_row"],
                t["field"],
                "revert:" + t["transform"],
                t["normalized_value"],
                t["original_value"],
                reason.strip(),
                actor,
                utc_now_iso(),
            ),
        )
        # Hand the field back to whatever set the original value: an earlier, still-applied transformation,
        # or the canonical row (then the override disappears).
        previous = conn.execute(
            "SELECT transformation_id FROM transformations a "
            "WHERE dataset_id = ? AND txn_id = ? AND field = ? "
            "AND kind = 'apply' AND transformation_id <> ? AND normalized_value = ? "
            "AND NOT EXISTS (SELECT 1 FROM transformations r WHERE r.reverts = a.transformation_id) "
            "ORDER BY applied_at DESC, transformation_id DESC LIMIT 1",
            (dataset_id, t["txn_id"], t["field"], transformation_id, t["original_value"]),
        ).fetchone()
        if previous is None:
            canonical = encode(canonical_value(conn, dataset_id, t["txn_id"], t["field"]))
            if t["original_value"] != canonical:  # pragma: no cover — the log chain guarantees this
                raise LedgerSpyError(
                    "log_inconsistent", "the transformation log does not lead back to the row"
                )
            conn.execute(
                "DELETE FROM normalized_values WHERE dataset_id = ? AND txn_id = ? AND field = ?",
                (dataset_id, t["txn_id"], t["field"]),
            )
        else:
            conn.execute(
                "UPDATE normalized_values SET value = ?, transformation_id = ? "
                "WHERE dataset_id = ? AND txn_id = ? "
                "AND field = ?",
                (t["original_value"], previous[0], dataset_id, t["txn_id"], t["field"]),
            )
        conn.execute("UPDATE fix_proposals SET status = 'reverted' WHERE fix_id = ?", (t["fix_id"],))
        audit.append(
            conn,
            actor,
            "transformation.reverted",
            f"transformation:{transformation_id}",
            {
                "dataset_id": dataset_id,
                "revert_id": rid,
                "txn_id": t["txn_id"],
                "field": t["field"],
                "restored_value": decode(t["original_value"]),
                "reason": reason.strip(),
            },
        )
    return {"revert_id": rid, "reverted": transformation_id, "restored_value": decode(t["original_value"])}


def list_transformations(conn: sqlite3.Connection, dataset_id: str) -> list[dict[str, Any]]:
    out = []
    for r in conn.execute(
        "SELECT * FROM transformations WHERE dataset_id = ? ORDER BY applied_at, transformation_id",
        (dataset_id,),
    ):
        d = dict(r)
        d["original_value"], d["normalized_value"] = (
            decode(r["original_value"]),
            decode(r["normalized_value"]),
        )
        out.append(d)
    return out
