"""OCR / extraction field corrections (P15): explicit, append-only review overlays — never a change of the
PDF, the stored extraction or a canonical row.

Each correction records the field, the ORIGINAL extracted value, the corrected value, the reviewer, the
rationale, the page and source text of the line, and the time (`ocr_corrections`, audit-logged).

- BEFORE confirmation (the statement's mapping is not confirmed yet): the correction overlays the extracted
  table handed to the confirmation; the row becomes `reviewer_corrected` (its review-required decision is
  still needed) and its `pdf_corrections` provenance keeps original and corrected values.
- AFTER confirmation: the corrected value is applied to the canonical field it maps to as ONE logged P4
  transformation (fix proposal → accepted decision → transformation, transform `ocr_correction`), so it is
  reversible through the existing P4 revert. This changes the dataset's normalised data version: every
  recorded reconciliation run that used the statement becomes stale (`inputs_changed`) and must be run again
  — matches computed from the uncorrected value are never left marked valid. Fields that are not canonical
  (balance, value date, Dr/Cr indicator) can only be corrected before confirmation.
"""

import json
from decimal import Decimal
from typing import Any

from ledgerspy.canonical.ids import new_ulid
from ledgerspy.cases import audit
from ledgerspy.cases.db import transaction
from ledgerspy.config import EffectiveConfig
from ledgerspy.errors import LedgerSpyError
from ledgerspy.hashing import utc_now_iso
from ledgerspy.ingest.values import parse_datetime, parse_decimal, to_minor_units
from ledgerspy.normalize.proposals import fix_id
from ledgerspy.normalize.service import _current
from ledgerspy.normalize.view import encode
from ledgerspy.pdfstatements import layout, validate
from ledgerspy.pdfstatements import service as pdf_service

FIELDS = ("date", "value_date", "description", "reference", "debit", "credit", "amount", "drcr", "balance")
AMOUNT_FIELDS = {"debit_amount", "credit_amount", "amount"}


def _err(code: str, message: str, status: int = 422, **details: Any) -> LedgerSpyError:
    return LedgerSpyError(code, message, details, http_status=status)


def _validate_text(field: str, value: str, sep: str | None, formats: list[str]) -> None:
    if field in ("date", "value_date") and not layout.is_date(value, formats):
        raise _err("invalid_value", f"{value!r} is not a date in any configured format")
    if field in ("debit", "credit", "amount", "balance"):
        core = value[:-2].strip() if field == "balance" and validate.indicator(value) else value
        if validate.number(core, sep or ".") is None:
            raise _err("invalid_value", f"{value!r} is not an amount (decimal separator {sep or '.'!r})")


def correct(
    conn,
    cfg: EffectiveConfig,
    dataset_id: str,
    item_id: str,
    field: str,
    value: str,
    rationale: str,
    actor: str,
    investigation_id: str | None = None,
) -> dict[str, Any]:
    ds = conn.execute("SELECT * FROM datasets WHERE dataset_id = ?", (dataset_id,)).fetchone()
    if ds is None:
        raise _err("dataset_not_found", "dataset not found", 404)
    if ds["format"] != "pdf":
        raise _err("not_a_pdf_dataset", "this dataset was not imported from a PDF", 409)
    if field not in FIELDS:
        raise _err("invalid_field", f"field must be one of {list(FIELDS)}")
    value = (value or "").strip()
    rationale = (rationale or "").strip()
    if not value or len(value) > 500:
        raise _err("invalid_value", "the corrected value must have 1–500 characters")
    if len(rationale) < 3:
        raise _err("rationale_required", "a correction needs a rationale")
    if investigation_id is not None:
        inv = conn.execute(
            "SELECT case_id, status FROM investigations WHERE investigation_id = ?", (investigation_id,)
        ).fetchone()
        if inv is None:
            raise _err("investigation_not_found", "investigation not found", 404)
        if inv["case_id"] != ds["case_id"]:
            raise _err("cross_scope_reference", "the investigation belongs to another engagement case")
        if inv["status"] == "closed":
            raise _err("investigation_closed", "the investigation is closed", 409)
    eid = pdf_service.current_extraction_id(ds)
    r = (
        None
        if eid is None
        else conn.execute(
            "SELECT item FROM pdf_extraction_items WHERE extraction_id = ? AND item_id = ?", (eid, item_id)
        ).fetchone()
    )
    if r is None:
        raise _err("item_not_found", "extraction item not found", 404)
    item = json.loads(r[0])
    if item["kind"] != "transaction":
        raise _err("invalid_item", f"a {item['kind']} line has no fields to correct")
    if field not in item.get("roles_present", []):
        raise _err(
            "invalid_field", f"the statement has no {field} column", allowed=item.get("roles_present", [])
        )
    result = pdf_service.load_result(conn, eid)
    sep = result["statement"]["decimal_separator"]
    _validate_text(field, value, sep, list(cfg.settings.mapping.date_formats))
    original = item["fields"].get(field)
    if value == (original or ""):
        raise _err("no_change", "the corrected value equals the extracted value")
    cid = "OC-" + new_ulid()
    now = utc_now_iso()
    if ds["status"] != "canonicalised":
        if pdf_service.CORRECTIONS_COLUMN not in json.loads(ds["header"]):
            raise _err(
                "reextract_required",
                "this PDF was extracted before corrections existed: re-run the "
                "extraction (POST /api/datasets/{id}/pdf/extract) to correct values before confirmation",
                409,
            )
        with transaction(conn):
            _insert(
                conn,
                cid,
                ds,
                eid,
                item,
                field,
                original,
                value,
                rationale,
                "before_confirmation",
                None,
                investigation_id,
                actor,
                now,
            )
        return get(conn, cid)
    return _after_confirmation(
        conn, cfg, cid, ds, eid, item, field, original, value, rationale, investigation_id, actor, now
    )


def _insert(
    conn, cid, ds, eid, item, field, original, value, rationale, stage, tid, inv_id, actor, now
) -> None:
    conn.execute(
        "INSERT INTO ocr_corrections VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            cid,
            ds["dataset_id"],
            eid,
            item["item_id"],
            field,
            original,
            value,
            rationale,
            item["page"],
            item["text"],
            stage,
            tid,
            inv_id,
            actor,
            now,
        ),
    )
    audit.append(
        conn,
        actor,
        "ocr.corrected",
        f"dataset:{ds['dataset_id']}",
        {
            "correction_id": cid,
            "item_id": item["item_id"],
            "field": field,
            "original_value": original,
            "corrected_value": value,
            "stage": stage,
            "transformation_id": tid,
            "investigation_id": inv_id,
            "page": item["page"],
        },
    )


def _after_confirmation(conn, cfg, cid, ds, eid, item, field, original, value, rationale, inv_id, actor, now):
    mapping = json.loads(ds["mapping"])
    names = pdf_service._column_names(pdf_service.load_result(conn, eid)["items"])
    column = names.get(field)
    canon = next((f for f, c in mapping["fields"].items() if c == column), None)
    if canon is None or canon not in ("posted_at", "description", "reference", *AMOUNT_FIELDS):
        raise _err(
            "not_correctable_after_confirmation",
            f"{field} is not a canonical field of the confirmed "
            "statement (it is kept as provenance only); it can be corrected only before confirmation",
            409,
        )
    txn = conn.execute(
        "SELECT * FROM transactions WHERE dataset_id = ? AND txn_id = ?", (ds["dataset_id"], item["item_id"])
    ).fetchone()
    if txn is None:
        raise _err("not_in_dataset", "this row was excluded and is not in the confirmed statement", 409)
    opts = mapping["options"]
    if canon == "posted_at":
        target, new = "posted_at", parse_datetime(value, opts.get("date_format"))
        if new is None:
            raise _err("invalid_value", f"{value!r} does not match the confirmed date format")
        new = new.isoformat()
    elif canon in AMOUNT_FIELDS:
        target = "amount_minor"
        number = parse_decimal(value, opts.get("decimal_separator") or ".")
        if number is None or Decimal(number) <= 0:
            raise _err(
                "invalid_value",
                "the corrected amount must be a positive number; a change of direction "
                "cannot be made after confirmation",
            )
        mu = cfg.settings.ingest.minor_units
        digits = (
            mu.by_currency.get(txn["currency"], mu.default_digits) if txn["currency"] else mu.default_digits
        )
        new = to_minor_units(Decimal(number), digits)
        if item["fields"].get(field) is None:
            raise _err("invalid_field", f"this row has no {field} value: its amount is in another column")
    else:
        target, new = canon, value
    current_enc, _ = _current(conn, ds["dataset_id"], item["item_id"], target)
    new_enc = encode(new)
    if new_enc == current_enc:
        raise _err("no_change", "the canonical value already equals the correction")
    fid = fix_id(ds["dataset_id"], item["item_id"], target, f"ocr_correction:{cid}", current_enc)
    tid = "N-" + new_ulid()
    reason = f"OCR correction {cid}: {rationale}"
    with transaction(conn):
        conn.execute(
            "INSERT INTO fix_proposals (fix_id, dataset_id, txn_id, source_row, field, transform, "
            "original_value, proposed_value, candidates, requires_confirmation, reason, status, "
            "chosen_value, first_run_id, decided_by, decided_at, decision_reason) VALUES (?, ?, ?, ?, ?, "
            "'ocr_correction', ?, ?, ?, 1, ?, 'applied', ?, ?, ?, ?, ?)",
            (
                fid,
                ds["dataset_id"],
                item["item_id"],
                txn["source_row"],
                target,
                current_enc,
                new_enc,
                encode([new_enc]),
                reason,
                new_enc,
                cid,
                actor,
                now,
                rationale,
            ),
        )
        conn.execute(
            "INSERT INTO fix_decisions (fix_id, dataset_id, decision, chosen_value, reason, decided_by, "
            "decided_at) VALUES (?, ?, 'accept', ?, ?, ?, ?)",
            (fid, ds["dataset_id"], new_enc, rationale, actor, now),
        )
        conn.execute(
            "INSERT INTO transformations (transformation_id, dataset_id, normalize_run_id, kind, reverts, "
            "fix_id, txn_id, source_row, field, transform, original_value, normalized_value, reason, "
            "applied_by, applied_at) "
            "VALUES (?, ?, ?, 'apply', NULL, ?, ?, ?, ?, 'ocr_correction', ?, ?, ?, ?, ?)",
            (
                tid,
                ds["dataset_id"],
                cid,
                fid,
                item["item_id"],
                txn["source_row"],
                target,
                current_enc,
                new_enc,
                reason,
                actor,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO normalized_values (dataset_id, txn_id, field, value, transformation_id) VALUES (?, "
            "?, ?, ?, ?) ON CONFLICT (dataset_id, txn_id, field) DO UPDATE SET value = excluded.value, "
            "transformation_id = excluded.transformation_id",
            (ds["dataset_id"], item["item_id"], target, new_enc, tid),
        )
        _insert(
            conn,
            cid,
            ds,
            eid,
            item,
            field,
            original,
            value,
            rationale,
            "after_confirmation",
            tid,
            inv_id,
            actor,
            now,
        )
    from ledgerspy.reconciliation.service import stale_run_ids

    return get(conn, cid) | {
        "canonical_field": target,
        "invalidated_reconciliation_runs": sorted(stale_run_ids(conn, ds["dataset_id"])),
    }


def get(conn, cid: str) -> dict[str, Any]:
    r = conn.execute("SELECT * FROM ocr_corrections WHERE correction_id = ?", (cid,)).fetchone()
    if r is None:
        raise _err("correction_not_found", "correction not found", 404)
    return dict(r)


def list_for(conn, dataset_id: str, item_id: str | None) -> dict[str, Any]:
    sql, args = "SELECT * FROM ocr_corrections WHERE dataset_id = ?", [dataset_id]
    if item_id:
        sql, args = sql + " AND item_id = ?", [*args, item_id]
    rows = conn.execute(sql + " ORDER BY created_at, correction_id", args).fetchall()
    return {
        "dataset_id": dataset_id,
        "items": [dict(r) for r in rows],
        "note": "corrections are overlays: the PDF and the extraction keep the original values",
    }
