"""Master-data import for entity resolution (vendor, customer, employee and group-company masters).

A master file becomes evidence exactly like a ledger (staged, hashed, stored read-only, audit-logged) and
its
records are stored verbatim in `master_records` for the fields of an EXPLICIT mapping:
- record_id, name, address, city, phone, email, bank_account, bank_code, tax_id → source column.

`suggest_mapping` proposes a column only when exactly one header matches a field's aliases. The proposal is
never applied silently: the import needs an explicit mapping, or the caller must accept the suggestion
explicitly (`use_suggested`). A field that is not mapped is absent: identifiers are never guessed from other
columns. Unmapped columns are not stored. Imported records are immutable; a new version of a master is a new
import.
"""

import json
import sqlite3
from pathlib import Path
from typing import Any

from ledgerspy.cases import audit, evidence
from ledgerspy.cases.db import transaction
from ledgerspy.config.loader import EffectiveConfig
from ledgerspy.config.schema import normalize_header
from ledgerspy.errors import LedgerSpyError
from ledgerspy.hashing import canonical_json, sha256_hex, utc_now_iso
from ledgerspy.ingest.readers import detect_format, iter_records, open_csv, open_xlsx
from ledgerspy.ingest.values import cell_text
from ledgerspy.resolution.records import MASTER_FIELDS, MASTER_TYPES, MasterRecord

ALIASES: dict[str, tuple[str, ...]] = {
    "record_id": (
        "id",
        "vendor id",
        "vendor code",
        "supplier id",
        "supplier code",
        "customer id",
        "customer code",
        "employee id",
        "employee code",
        "emp id",
        "entity code",
        "party id",
    ),
    "name": (
        "name",
        "vendor name",
        "supplier name",
        "customer name",
        "employee name",
        "legal name",
        "party name",
        "company name",
    ),
    "address": ("address", "home address", "registered address", "street address", "office address"),
    "city": ("city", "town"),
    "phone": ("phone", "phone number", "mobile", "mobile number", "telephone", "contact number"),
    "email": ("email", "e mail", "email address", "e mail address"),
    "bank_account": ("bank account", "bank account number", "account number", "a c no", "bank a c"),
    "bank_code": ("bank code", "ifsc", "ifsc code", "swift", "swift code", "routing number", "sort code"),
    "tax_id": ("tax id", "gstin", "gst number", "pan", "pan number", "tax number", "vat number", "tin"),
}
_ALIAS_KEYS = {f: {normalize_header(a) for a in al} for f, al in ALIASES.items()}


class MasterImportError(LedgerSpyError):
    pass


def suggest_mapping(header: list[str]) -> dict[str, Any]:
    """{field: column} for unambiguous single matches; ambiguous fields are reported, never chosen."""
    mapping, ambiguous = {}, {}
    for f in MASTER_FIELDS:
        cols = [c for c in header if normalize_header(c) in _ALIAS_KEYS[f]]
        if len(cols) == 1:
            mapping[f] = cols[0]
        elif len(cols) > 1:
            ambiguous[f] = cols
    return {
        "mapping": mapping,
        "ambiguous": ambiguous,
        "unmapped_fields": [f for f in MASTER_FIELDS if f not in mapping],
    }


def _validate(mapping: dict[str, str], header: list[str]) -> None:
    unknown = sorted(set(mapping) - set(MASTER_FIELDS))
    if unknown:
        raise MasterImportError(
            "invalid_mapping", f"unknown master fields: {unknown}", {"fields": list(MASTER_FIELDS)}
        )
    missing = sorted({c for c in mapping.values() if c not in header})
    if missing:
        raise MasterImportError("invalid_mapping", f"columns not in the file: {missing}", {"header": header})
    if "name" not in mapping:
        raise MasterImportError("invalid_mapping", "a master mapping must map the name field")
    if len(set(mapping.values())) != len(mapping):
        raise MasterImportError("invalid_mapping", "one column is mapped to two fields")


def master_file_id(case_id: str, sha: str, entity_type: str, mapping: dict[str, str]) -> str:
    return "MF-" + sha256_hex(canonical_json([case_id, sha, entity_type, mapping]))[:16]


def import_master(
    conn: sqlite3.Connection,
    cfg: EffectiveConfig,
    workspace: Path,
    case_id: str,
    path: Path,
    entity_type: str,
    actor: str,
    mapping: dict[str, str] | None = None,
    use_suggested: bool = False,
    filename: str | None = None,
) -> dict[str, Any]:
    if entity_type not in MASTER_TYPES:
        raise MasterImportError("invalid_entity_type", f"entity type must be one of {list(MASTER_TYPES)}")
    if conn.execute("SELECT 1 FROM cases WHERE case_id = ?", (case_id,)).fetchone() is None:
        raise MasterImportError("case_not_found", f"no case {case_id}", http_status=404)
    ing = cfg.settings.ingest
    filename = filename or Path(path).name
    staged = evidence.stage_file(Path(path), workspace, ing.max_file_bytes)
    try:
        fmt = detect_format(staged.path, filename)
        src = open_csv(staged.path, ing) if fmt == "csv" else open_xlsx(staged.path, ing)
        header = list(src.header)
        suggestion = suggest_mapping(header)
        if mapping is None:
            if not use_suggested:
                raise MasterImportError(
                    "mapping_required",
                    "an explicit mapping is required (or accept the suggestion explicitly)",
                    {"suggestion": suggestion, "header": header},
                )
            mapping = suggestion["mapping"]
        mapping = dict(sorted(mapping.items()))
        _validate(mapping, header)
        index = {c: header.index(c) for c in mapping.values()}
        rows = []
        for source_row, cells in iter_records(src, ing):
            vals = {
                f: cell_text(cells[index[c]]) if index[c] < len(cells) else None for f, c in mapping.items()
            }
            rows.append((source_row, vals))
    except BaseException:
        staged.path.unlink(missing_ok=True)
        raise
    mfid = master_file_id(case_id, staged.sha256, entity_type, mapping)
    if conn.execute("SELECT 1 FROM master_files WHERE master_file_id = ?", (mfid,)).fetchone():
        staged.path.unlink(missing_ok=True)
        return get_master_file(conn, mfid) | {"already_imported": True}
    with transaction(conn):
        evidence.store(conn, staged, "." + fmt, workspace, actor)
        conn.execute(
            "INSERT INTO master_files (master_file_id, case_id, evidence_sha256, original_filename, "
            "entity_type, format, mapping, record_count, imported_by, imported_at) VALUES (?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?)",
            (
                mfid,
                case_id,
                staged.sha256,
                filename,
                entity_type,
                fmt,
                json.dumps(mapping, sort_keys=True),
                len(rows),
                actor,
                utc_now_iso(),
            ),
        )
        conn.executemany(
            "INSERT INTO master_records (master_file_id, source_row, " + ", ".join(MASTER_FIELDS) + ") "
            "VALUES (?, ?, " + ", ".join("?" for _ in MASTER_FIELDS) + ")",
            [(mfid, r, *(v.get(f) for f in MASTER_FIELDS)) for r, v in rows],
        )
        audit.append(
            conn,
            actor,
            "master.imported",
            f"master:{mfid}",
            {
                "case_id": case_id,
                "evidence_sha256": staged.sha256,
                "entity_type": entity_type,
                "records": len(rows),
                "mapping": mapping,
            },
        )
    return get_master_file(conn, mfid) | {"already_imported": False}


def get_master_file(conn: sqlite3.Connection, mfid: str) -> dict[str, Any]:
    r = conn.execute("SELECT * FROM master_files WHERE master_file_id = ?", (mfid,)).fetchone()
    if r is None:
        raise MasterImportError("not_found", f"no master file {mfid}", http_status=404)
    out = dict(r)
    out["mapping"] = json.loads(out["mapping"])
    return out


def list_master_files(conn: sqlite3.Connection, case_id: str) -> list[dict[str, Any]]:
    ids = [
        r[0]
        for r in conn.execute(
            "SELECT master_file_id FROM master_files WHERE case_id = ? ORDER BY imported_at, master_file_id",
            (case_id,),
        )
    ]
    return [get_master_file(conn, i) for i in ids]


def records_for_case(conn: sqlite3.Connection, case_id: str) -> tuple[MasterRecord, ...]:
    """Every imported master record of the case, in a fixed order (file ID, source row)."""
    out = []
    files = {
        r[0]: (r[1], json.loads(r[2]))
        for r in conn.execute(
            "SELECT master_file_id, entity_type, mapping FROM master_files WHERE case_id = ?", (case_id,)
        )
    }
    for r in conn.execute(
        "SELECT * FROM master_records WHERE master_file_id IN (SELECT master_file_id FROM master_files "
        "WHERE case_id = ?) ORDER BY master_file_id, source_row",
        (case_id,),
    ):
        etype, mapping = files[r["master_file_id"]]
        out.append(
            MasterRecord(
                r["master_file_id"],
                etype,
                r["source_row"],
                tuple((f, r[f]) for f in MASTER_FIELDS if f in mapping),
            )
        )
    return tuple(out)


def records_sha256(records: tuple[MasterRecord, ...]) -> str:
    return sha256_hex(
        canonical_json([[m.master_file_id, m.entity_type, m.source_row, list(m.values)] for m in records])
    )
