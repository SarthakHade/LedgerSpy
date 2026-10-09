"""Ingestion pipeline: upload → evidence → profile + mapping proposal → confirmation → canonical rows.

    stage (hash + size limit) → detect format → safe read + profile → suggest mapping
        → store read-only evidence + dataset record (status mapping_required)
    confirm mapping → re-verify evidence hash → validate mapping → check source-ID uniqueness
        → convert every row with the adapter → transactions + labels + ingest_issues (status canonicalised)

P14b: a PDF bank statement is extracted (ledgerspy.pdfstatements, in a subprocess) at upload; the extracted
table takes the place of the CSV / XLSX reader. Its confirmation is refused until the extraction is complete
and every review-required item has a decision; only trusted rows are converted.

Every state change is audit-logged in the same transaction as the change.
"""

import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from ledgerspy.canonical.ids import new_dataset_id
from ledgerspy.cases import audit, evidence
from ledgerspy.cases.db import transaction
from ledgerspy.cases.evidence import StagedUpload
from ledgerspy.config import EffectiveConfig
from ledgerspy.errors import LedgerSpyError
from ledgerspy.hashing import canonical_json, sha256_hex, utc_now_iso
from ledgerspy.ingest.adapters import CONVERTERS
from ledgerspy.ingest.errors import IngestError
from ledgerspy.ingest.mapping import Mapping, Profile, mapping_json, profile_table, suggest, validate_mapping
from ledgerspy.ingest.readers import TableSource, detect_format, iter_records, open_csv, open_xlsx, reopen
from ledgerspy.ingest.values import cell_text
from ledgerspy.pdfstatements import service as pdf_service

# A ledger is analysed (P4–P13); a bank statement (P14) is reconciled against a ledger of the same case.
SUPPORTED_KINDS = ("ledger", "bank_statement")
_BATCH = 5000
_ISSUE_EXAMPLES = 20
_MAX_SOURCE_ID_CHARS = 128
_FIT_CODES = ("unparseable", "unknown_value", "too_many_decimals")
LABEL_NOTE = (
    "Label columns are stored only in the separate `labels` table (for evaluation); "
    "they are not fields of, and "
    "never appear in, canonical transactions."
)


def _json(obj: Any) -> str:
    return canonical_json(obj).decode("utf-8")


def _open(
    path: Path, fmt: str, cfg: EffectiveConfig, sheet: str | None, delimiter: str | None
) -> TableSource:
    ingest = cfg.settings.ingest
    return open_csv(path, ingest, delimiter) if fmt == "csv" else open_xlsx(path, ingest, sheet)


# ----------------------------------------------------------------------------------------------- upload


def ingest_upload(
    conn: sqlite3.Connection,
    cfg: EffectiveConfig,
    workspace: Path,
    case_id: str,
    staged: StagedUpload,
    filename: str,
    kind: str,
    actor: str,
    sheet: str | None = None,
    delimiter: str | None = None,
) -> dict[str, Any]:
    """Inspect a staged upload. On success the file becomes evidence and a dataset awaits mapping."""
    try:
        if conn.execute("SELECT 1 FROM cases WHERE case_id = ?", (case_id,)).fetchone() is None:
            raise IngestError("case_not_found", "case not found", http_status=404)
        if kind not in SUPPORTED_KINDS:
            raise IngestError(
                "kind_not_supported",
                f"kind {kind!r} is not supported (ledgers and CSV/XLSX bank statements only; PDF and "
                "scanned statements come in P14b).",
                {"supported": list(SUPPORTED_KINDS)},
            )
        fmt = detect_format(staged.path, filename, allow_pdf=kind == "bank_statement")
        dataset_id = new_dataset_id()
        pdf_result = None
        if fmt == "pdf":
            try:
                pdf_result = pdf_service.run_extraction(staged.path, staged.sha256, cfg, dataset_id)
            except LedgerSpyError as exc:
                raise IngestError(exc.code, exc.message, exc.details, http_status=exc.http_status) from exc
            src = pdf_service.table_source(staged.path, pdf_result, None)
        else:
            src = _open(staged.path, fmt, cfg, sheet, delimiter)
        profile = profile_table(src, cfg.settings)
        if profile.rows == 0 and fmt != "pdf":  # a PDF without rows is kept: its extraction report says why
            raise IngestError("no_data_rows", "The file has a header but no data rows.")
        proposal = suggest(profile, cfg.settings, kind)
        if pdf_result is not None:
            proposal = pdf_service.pdf_suggestion(proposal, pdf_result)
    except IngestError as exc:
        staged.path.unlink(missing_ok=True)
        if exc.code != "case_not_found":
            with transaction(conn):
                audit.append(
                    conn,
                    actor,
                    "upload.rejected",
                    f"case:{case_id}",
                    {
                        "filename": filename,
                        "sha256": staged.sha256,
                        "size_bytes": staged.size_bytes,
                        "code": exc.code,
                        "message": exc.message,
                    },
                )
        raise

    created_at = utc_now_iso()
    with transaction(conn):
        stored_name, newly_stored = evidence.store(conn, staged, "." + fmt, workspace, actor)
        conn.execute(
            "INSERT INTO datasets (dataset_id, case_id, evidence_sha256, original_filename, kind, format, "
            "format_details, header, row_count, profile, suggestion, upload_config_sha256, status, "
            "created_by, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'mapping_required', ?, ?)",
            (
                dataset_id,
                case_id,
                staged.sha256,
                filename,
                kind,
                fmt,
                _json(pdf_service.stored_details(src) if fmt == "pdf" else src.details),
                _json(src.header),
                profile.rows,
                _json(_profile_json(profile)),
                _json(proposal),
                cfg.sha256,
                actor,
                created_at,
            ),
        )
        audit.append(
            conn,
            actor,
            "dataset.created",
            f"dataset:{dataset_id}",
            {
                "case_id": case_id,
                "evidence_sha256": staged.sha256,
                "original_filename": filename,
                "format": fmt,
                "rows": profile.rows,
                "suggested_adapter": proposal["adapter"]["suggested"],
            },
        )
        if pdf_result is not None:
            pdf_service.insert_extraction(conn, dataset_id, staged.sha256, pdf_result, actor)
    out = get_dataset(conn, dataset_id) | {"evidence_newly_stored": newly_stored}
    if pdf_result is not None:
        out["pdf"] = pdf_service.summary(conn, dataset_id)
    return out


def _profile_json(profile: Profile) -> dict[str, Any]:
    return {
        "rows": profile.rows,
        "records": profile.records,
        "blank_records_skipped": profile.blank_records,
        "columns": [c.to_dict() for c in profile.columns],
    }


# ----------------------------------------------------------------------------------------------- read


def _dataset_row(conn: sqlite3.Connection, dataset_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM datasets WHERE dataset_id = ?", (dataset_id,)).fetchone()
    if row is None:
        raise IngestError("dataset_not_found", "dataset not found", http_status=404)
    return row


def get_dataset(conn: sqlite3.Connection, dataset_id: str) -> dict[str, Any]:
    row = _dataset_row(conn, dataset_id)
    ev = conn.execute("SELECT * FROM evidence_files WHERE sha256 = ?", (row["evidence_sha256"],)).fetchone()
    load = lambda v: None if v is None else json.loads(v)  # noqa: E731
    return {
        "dataset_id": row["dataset_id"],
        "case_id": row["case_id"],
        "status": row["status"],
        "kind": row["kind"],
        "evidence": {
            "sha256": ev["sha256"],
            "size_bytes": ev["size_bytes"],
            "stored_name": ev["stored_name"],
            "stored_at": ev["stored_at"],
            "original_filename": row["original_filename"],
            "format": row["format"],
        },
        "format_details": load(row["format_details"]),
        "row_count": row["row_count"],
        "profile": load(row["profile"]),
        "mapping_suggestion": load(row["suggestion"]),
        "mapping": load(row["mapping"]),
        "canonical": None
        if row["status"] != "canonicalised"
        else {
            "adapter": row["adapter"],
            "adapter_version": row["adapter_version"],
            "mapping_sha256": row["mapping_sha256"],
            "config_sha256": row["config_sha256"],
            "time_basis": row["time_basis"],
            "synthetic": None if row["synthetic"] is None else bool(row["synthetic"]),
            "id_source": row["id_source"],
            "issues": load(row["issue_summary"]),
            "canonicalised_by": row["canonicalised_by"],
            "canonicalised_at": row["canonicalised_at"],
        },
        "created_by": row["created_by"],
        "created_at": row["created_at"],
    }


def get_transactions(
    conn: sqlite3.Connection, dataset_id: str, page: int, size: int, view: str = "normalized"
) -> dict[str, Any]:
    """Page canonical rows. view="normalized" (default) applies logged P4 transformations and lists the
    fields they changed in `normalized_fields`; view="canonical" returns the P3 rows untouched."""
    row = _dataset_row(conn, dataset_id)
    if row["status"] != "canonicalised":
        raise IngestError(
            "not_canonicalised", "the dataset's mapping has not been confirmed yet", http_status=409
        )
    total = conn.execute("SELECT COUNT(*) FROM transactions WHERE dataset_id = ?", (dataset_id,)).fetchone()[
        0
    ]
    rows = conn.execute(
        "SELECT * FROM transactions WHERE dataset_id = ? ORDER BY source_row LIMIT ? OFFSET ?",
        (dataset_id, size, (page - 1) * size),
    ).fetchall()
    items = [{k: (json.loads(r[k]) if k == "extra" else r[k]) for k in r.keys()} for r in rows]  # noqa: SIM118
    if view == "normalized":
        overlay: dict[str, dict[str, Any]] = {}
        for t, f, v in conn.execute(
            "SELECT txn_id, field, value FROM normalized_values WHERE dataset_id = ?", (dataset_id,)
        ):
            overlay.setdefault(t, {})[f] = json.loads(v)
        for item in items:
            changed = overlay.get(item["txn_id"], {})
            item.update(changed)
            item["normalized_fields"] = sorted(changed)
    return {
        "dataset_id": dataset_id,
        "view": view,
        "page": page,
        "size": size,
        "total": total,
        "items": items,
    }


# ----------------------------------------------------------------------------------------------- confirm


def _check_source_ids(src: TableSource, column: str, cfg: EffectiveConfig) -> None:
    idx = src.header.index(column)
    first_seen: dict[str, int] = {}
    blanks, duplicates, invalid = [], [], []
    for row, cells in iter_records(src, cfg.settings.ingest):
        value = cell_text(cells[idx])
        if value is None:
            blanks.append(row)
        elif len(value) > _MAX_SOURCE_ID_CHARS or not value.isprintable():
            invalid.append(row)
        elif value in first_seen:
            duplicates.append({"value": value, "rows": [first_seen[value], row]})
        else:
            first_seen[value] = row
    if blanks or duplicates or invalid:
        raise IngestError(
            "source_ids_not_unique",
            f"Column {column!r} cannot be the transaction ID: {len(blanks)} blank, "
            f"{len(duplicates)} duplicate and "
            f"{len(invalid)} invalid values. Map it as `reference` instead, or leave txn_id unmapped to use "
            "derived IDs.",
            {
                "column": column,
                "blank_rows": blanks[:20],
                "duplicates": duplicates[:20],
                "invalid_rows": invalid[:20],
            },
        )


def confirm_mapping(
    conn: sqlite3.Connection,
    cfg: EffectiveConfig,
    workspace: Path,
    dataset_id: str,
    mapping: Mapping,
    actor: str,
) -> dict[str, Any]:
    row = _dataset_row(conn, dataset_id)
    if row["status"] != "mapping_required":
        raise IngestError(
            "already_canonicalised",
            "This dataset is already canonicalised and immutable. "
            "Upload the file again to map it differently.",
            http_status=409,
        )
    path = evidence.verify_one(conn, row["evidence_sha256"], workspace)
    if row["format"] == "pdf":  # P14b: the reviewed extraction, only trusted rows
        src = pdf_service.confirm_source(conn, row, path)
    else:
        src = reopen(path, row["format"], json.loads(row["format_details"]), cfg.settings.ingest)
    if src.header != json.loads(row["header"]):  # pragma: no cover — guarded by the hash check above
        raise IngestError("evidence_changed", "The evidence header differs from the one recorded at upload.")
    profile = profile_table(src, cfg.settings)
    errors = validate_mapping(mapping, profile, cfg.settings, row["kind"])
    if errors:
        raise IngestError("mapping_invalid", "The mapping is not valid for this file.", {"errors": errors})
    use_source_ids = "txn_id" in mapping.fields
    if use_source_ids:
        _check_source_ids(src, mapping.fields["txn_id"], cfg)

    converter = CONVERTERS[mapping.adapter](
        mapping, src.header, cfg.settings, dataset_id, row["evidence_sha256"], use_source_ids
    )
    counts: dict[str, Counter] = defaultdict(Counter)
    examples: list[dict[str, Any]] = []
    rows = label_values = 0
    preview: list[dict[str, Any]] = []
    with transaction(conn):
        txn_batch: list[tuple] = []
        label_batch: list[tuple] = []
        issue_batch: list[tuple] = []

        def flush() -> None:
            conn.executemany(
                "INSERT INTO transactions (dataset_id, txn_id, source_row, posted_at, document_date, "
                "amount_minor, currency, direction, account, payer_raw, payee_raw, payer_entity_id, "
                "payee_entity_id, reference, "
                "description, txn_type, extra) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                txn_batch,
            )
            conn.executemany("INSERT INTO labels VALUES (?, ?, ?, ?)", label_batch)
            conn.executemany("INSERT INTO ingest_issues VALUES (?, ?, ?, ?, ?, ?)", issue_batch)
            txn_batch.clear()
            label_batch.clear()
            issue_batch.clear()

        for source_row, cells in iter_records(src, cfg.settings.ingest):
            txn, labels, issues = converter.convert(source_row, cells)
            rows += 1
            d = txn.model_dump(mode="json")
            if len(preview) < 3:
                preview.append(d)
            txn_batch.append(
                tuple(
                    d[k]
                    for k in (
                        "dataset_id",
                        "txn_id",
                        "source_row",
                        "posted_at",
                        "document_date",
                        "amount_minor",
                        "currency",
                        "direction",
                        "account",
                        "payer_raw",
                        "payee_raw",
                        "payer_entity_id",
                        "payee_entity_id",
                        "reference",
                        "description",
                        "txn_type",
                    )
                )
                + (_json(d["extra"]),)
            )
            for column, value in labels.items():
                label_batch.append((dataset_id, txn.txn_id, column, cell_text(value) or ""))
                label_values += 1
            for i in issues:
                counts[i["field"]][i["code"]] += 1
                if len(examples) < _ISSUE_EXAMPLES:
                    examples.append(i)
                issue_batch.append(
                    (dataset_id, i["source_row"], i["field"], i["column"], i["code"], i["raw_value"])
                )
            if len(txn_batch) >= _BATCH:
                flush()
        flush()

        # A declared mapping that does not fit the data (e.g. a wrong date format) is refused, not stored.
        max_bad = cfg.settings.mapping.max_unconvertible_share * rows
        misfit = {f: sum(c[k] for k in _FIT_CODES) for f, c in counts.items()}
        misfit = {f: n for f, n in misfit.items() if n > max_bad}
        if misfit:
            raise IngestError(
                "mapping_does_not_fit",
                "The confirmed mapping leaves too many values unconvertible: "
                + ", ".join(f"{f} ({n} of {rows} rows)" for f, n in misfit.items())
                + ". Check the column choice, date format and decimal separator.",
                {"unconvertible": misfit, "rows": rows, "examples": examples},
            )

        summary = {
            "total": sum(sum(c.values()) for c in counts.values()),
            "by_field": {f: dict(sorted(c.items())) for f, c in sorted(counts.items())},
        }
        stored_mapping = mapping_json(mapping)
        mapping_sha = sha256_hex(canonical_json(stored_mapping))
        synthetic = converter.synthetic
        now = utc_now_iso()
        conn.execute(
            "UPDATE datasets SET status = 'canonicalised', mapping = ?, mapping_sha256 = ?, adapter = ?, "
            "adapter_version = ?, config_sha256 = ?, time_basis = ?, synthetic = ?, id_source = ?, "
            "issue_summary = ?, "
            "canonicalised_by = ?, canonicalised_at = ? WHERE dataset_id = ?",
            (
                _json(stored_mapping),
                mapping_sha,
                converter.name,
                converter.version,
                cfg.sha256,
                converter.time_basis(),
                None if synthetic is None else int(synthetic),
                "source_column" if use_source_ids else "derived",
                _json(summary),
                actor,
                now,
                dataset_id,
            ),
        )
        audit.append(
            conn,
            actor,
            "dataset.canonicalised",
            f"dataset:{dataset_id}",
            {
                "evidence_sha256": row["evidence_sha256"],
                "adapter": converter.name,
                "mapping_sha256": mapping_sha,
                "config_sha256": cfg.sha256,
                "rows": rows,
                "issues": summary["total"],
                "label_columns": mapping.label_columns,
            },
        )

    notes = []
    if converter.name == "bank_statement":
        notes.append(
            "Bank statement: directions are in/out from the account holder's view, as confirmed in the "
            "mapping. It is reconciled against a ledger of the same case (/api/reconciliation)."
        )
    if converter.name == "paysim":
        notes.append(
            "SYNTHETIC: PaySim is a simulation. posted_at is derived from `step` "
            f"({cfg.settings.adapters.paysim.step_epoch} + (step − 1) h) and is not an accounting timestamp."
        )
    return {
        "status": "canonicalised",
        "dataset_id": dataset_id,
        "rows": rows,
        "adapter": converter.name,
        "id_source": "source_column" if use_source_ids else "derived",
        "time_basis": converter.time_basis(),
        "synthetic": synthetic,
        "mapping_sha256": mapping_sha,
        "config_sha256": cfg.sha256,
        "labels": {"columns": mapping.label_columns, "values_stored": label_values, "note": LABEL_NOTE},
        "issues": summary | {"examples": examples},
        "preview": preview,
        "notes": notes,
    }
