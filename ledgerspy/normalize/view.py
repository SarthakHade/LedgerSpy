"""The normalised view of a canonical dataset.

    effective value = normalized_values[txn, field]  if a transformation is applied
                      canonical transactions[txn].field  otherwise

P3 canonical rows are never modified (a trigger enforces it). Every override in `normalized_values` was
written by a logged transformation, so the view can always be explained and reverted.
"""

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ledgerspy.errors import LedgerSpyError
from ledgerspy.hashing import canonical_json, sha256_hex

# Canonical fields that quality checks read and transformations may change.
FIELDS = (
    "posted_at",
    "document_date",
    "amount_minor",
    "currency",
    "direction",
    "account",
    "payer_raw",
    "payee_raw",
    "reference",
    "description",
    "txn_type",
)
# P3 issue `field` names → canonical field they concern.
P3_FIELD = {
    "posted_at": "posted_at",
    "document_date": "document_date",
    "amount": "amount_minor",
    "debit_amount": "amount_minor",
    "credit_amount": "amount_minor",
    "direction": "direction",
    "currency": "currency",
}


def encode(value: Any) -> str:
    """Exact, comparable text form of a field value for the log and the overlay (JSON)."""
    return canonical_json(value).decode("utf-8")


def decode(text: str) -> Any:
    return json.loads(text)


@dataclass
class DatasetView:
    dataset_id: str
    case_id: str
    evidence_sha256: str
    adapter: str
    time_basis: str
    synthetic: bool | None
    id_source: str
    mapping: dict[str, Any]
    header: list[str]
    period: tuple[date | None, date | None]
    rows: list[dict[str, Any]]
    p3_issues: dict[tuple[int, str], list[dict[str, Any]]]
    overlay: dict[tuple[str, str], str]
    data_version: str
    by_txn: dict[str, dict[str, Any]] = field(default_factory=dict)

    def p3(self, source_row: int, canonical_field: str) -> list[dict[str, Any]]:
        """P3 ingestion issues of a row that concern a canonical field (e.g. amount_minor ← debit_amount)."""
        return self.p3_issues.get((source_row, canonical_field), [])

    def mapped(self, canonical_field: str) -> bool:
        fields = self.mapping["fields"]
        if canonical_field == "amount_minor":
            return "amount" in fields or "debit_amount" in fields
        if canonical_field == "currency" and self.mapping["options"].get("currency"):
            return True
        if canonical_field == "direction":
            return True  # always required by the mapping (column, debit/credit pair, declaration or adapter)
        return canonical_field in fields


def _dataset(conn: sqlite3.Connection, dataset_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM datasets WHERE dataset_id = ?", (dataset_id,)).fetchone()
    if row is None:
        raise LedgerSpyError("dataset_not_found", "dataset not found", http_status=404)
    if row["status"] != "canonicalised":
        raise LedgerSpyError(
            "not_canonicalised", "the dataset's mapping has not been confirmed yet", http_status=409
        )
    return row


def overlay_rows(conn: sqlite3.Connection, dataset_id: str) -> dict[tuple[str, str], str]:
    return {
        (r[0], r[1]): r[2]
        for r in conn.execute(
            "SELECT txn_id, field, value FROM normalized_values WHERE dataset_id = ? ORDER BY txn_id, field",
            (dataset_id,),
        )
    }


def data_version(overlay: dict[tuple[str, str], str]) -> str:
    """Identifies the normalised state: SHA-256 of the sorted overlay (no transformations → a fixed hash)."""
    return sha256_hex(canonical_json(sorted([t, f, v] for (t, f), v in overlay.items())))


def load_view(conn: sqlite3.Connection, dataset_id: str) -> DatasetView:
    ds = _dataset(conn, dataset_id)
    case = conn.execute(
        "SELECT period_start, period_end FROM cases WHERE case_id = ?", (ds["case_id"],)
    ).fetchone()
    period = tuple(date.fromisoformat(v) if v else None for v in (case["period_start"], case["period_end"]))
    overlay = overlay_rows(conn, dataset_id)
    rows = []
    for r in conn.execute(
        "SELECT * FROM transactions WHERE dataset_id = ? ORDER BY source_row", (dataset_id,)
    ):
        row = {"txn_id": r["txn_id"], "source_row": r["source_row"], "extra": json.loads(r["extra"])}
        normalized = []
        for f in FIELDS:
            key = (r["txn_id"], f)
            if key in overlay:
                row[f] = decode(overlay[key])
                normalized.append(f)
            else:
                row[f] = r[f]
        row["normalized_fields"] = normalized
        rows.append(row)
    issues: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for i in conn.execute(
        "SELECT source_row, field, source_column, code, raw_value FROM ingest_issues WHERE dataset_id = ? "
        "ORDER BY source_row, field, code",
        (dataset_id,),
    ):
        issues.setdefault((i["source_row"], P3_FIELD[i["field"]]), []).append(dict(i))
    view = DatasetView(
        dataset_id=dataset_id,
        case_id=ds["case_id"],
        evidence_sha256=ds["evidence_sha256"],
        adapter=ds["adapter"],
        time_basis=ds["time_basis"],
        synthetic=None if ds["synthetic"] is None else bool(ds["synthetic"]),
        id_source=ds["id_source"],
        mapping=json.loads(ds["mapping"]),
        header=json.loads(ds["header"]),
        period=period,  # type: ignore[arg-type]
        rows=rows,
        p3_issues=issues,
        overlay=overlay,
        data_version=data_version(overlay),
    )
    view.by_txn = {r["txn_id"]: r for r in rows}
    return view


def canonical_value(conn: sqlite3.Connection, dataset_id: str, txn_id: str, field_name: str) -> Any:
    if field_name not in FIELDS:
        raise ValueError(f"not a normalisable field: {field_name}")
    row = conn.execute(
        f"SELECT {field_name} FROM transactions WHERE dataset_id = ? AND txn_id = ?",  # noqa: S608 — allow-listed
        (dataset_id, txn_id),
    ).fetchone()
    if row is None:
        raise LedgerSpyError("txn_not_found", f"transaction {txn_id} not found", http_status=404)
    return row[0]


def effective_value(conn: sqlite3.Connection, dataset_id: str, txn_id: str, field_name: str) -> Any:
    row = conn.execute(
        "SELECT value FROM normalized_values WHERE dataset_id = ? AND txn_id = ? AND field = ?",
        (dataset_id, txn_id, field_name),
    ).fetchone()
    return decode(row[0]) if row else canonical_value(conn, dataset_id, txn_id, field_name)
