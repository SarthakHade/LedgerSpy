"""PDF statement datasets: upload, extraction (subprocess), storage, review, confirmation input, page images.

Lifecycle and data states (each is explicit in the API):
1. EVIDENCE — the uploaded PDF, stored byte-for-byte with its SHA-256 (P3 evidence store, never rewritten).
2. EXTRACTED — `pdf_extractions` + `pdf_extraction_items`: every line of every page, classified, with page,
   box, source text, cells, the extraction method and confidences. Immutable; re-extraction adds a new one.
3. REVIEWED — `pdf_review_decisions` (append-only): `confirm` or `exclude` per review-required item.
4. CONFIRMED / NORMALISED — the P3 mapping confirmation (`PUT /api/datasets/{id}/mapping`) turns the extracted
   table into canonical transactions. It is REFUSED while the extraction is incomplete or any review-required
   item has no decision; excluded rows never enter. Each canonical row keeps its provenance in `extra`
   (`pdf_item_id`, `pdf_page`, `pdf_line_bbox`, `pdf_text`, `pdf_method`, `pdf_min_ocr_confidence`,
   `pdf_review`, `pdf_extraction_id`). P4 normalisation and P14 reconciliation then work unchanged.

The extracted table handed to P3 has role-named columns: `Withdrawal` / `Deposit` only when the PDF's own
header says withdrawal / deposit (so the bank proposal may state the direction), otherwise `Debit` / `Credit`
(the direction must be confirmed); `Currency` and `Account No` only when the statement states them.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import ledgerspy
from ledgerspy.cases import audit, evidence
from ledgerspy.cases.db import transaction
from ledgerspy.config import EffectiveConfig
from ledgerspy.errors import LedgerSpyError
from ledgerspy.hashing import canonical_json, sha256_hex, utc_now_iso
from ledgerspy.ingest.errors import IngestError
from ledgerspy.ingest.readers import TableSource
from ledgerspy.pdfstatements import layout, validate

PROVENANCE = [
    "pdf_item_id",
    "pdf_page",
    "pdf_line_bbox",
    "pdf_text",
    "pdf_method",
    "pdf_min_ocr_confidence",
    "pdf_review",
    "pdf_extraction_id",
]
TRUSTED = ("auto_accepted", "reviewer_confirmed")
ROLE_COLUMNS = [
    ("date", "Txn Date"),
    ("value_date", "Value Dt"),
    ("description", "Narration"),
    ("reference", "Ref No"),
    ("debit", None),
    ("credit", None),
    ("amount", "Amount"),
    ("drcr", "Dr/Cr"),
    ("balance", None),
]
_ERROR_STATUS = {
    "pdf_too_many_pages": 413,
    "pdf_encrypted": 422,
    "pdf_malformed": 422,
    "pdf_no_pages": 422,
    "page_not_found": 404,
    "page_too_large": 422,
}


def settings_for_worker(cfg: EffectiveConfig) -> dict[str, Any]:
    s = cfg.settings
    return {
        "pdf": s.pdf.model_dump(mode="json"),
        "date_formats": list(s.mapping.date_formats),
        "min_parse_rate": s.mapping.min_parse_rate,
    }


def config_sha(cfg: EffectiveConfig) -> str:
    return sha256_hex(canonical_json(settings_for_worker(cfg)))


def _worker(payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    env = {k: v for k, v in os.environ.items()}
    root = str(Path(ledgerspy.__file__).resolve().parents[1])
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        r = subprocess.run(  # noqa: S603 — this interpreter, fixed module, request on stdin
            [sys.executable, "-m", "ledgerspy.pdfstatements.worker"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            env=env,
            cwd=root,
        )
    except subprocess.TimeoutExpired as exc:
        raise LedgerSpyError(
            "pdf_extraction_timeout",
            f"PDF processing was stopped after {timeout} s (pdf.worker_timeout_seconds); nothing was "
            "registered as extracted",
            http_status=422,
        ) from exc
    if r.returncode != 0:
        raise LedgerSpyError(
            "pdf_extraction_failed",
            "the PDF worker failed: " + (r.stderr.strip().splitlines() or ["(no message)"])[-1][:300],
            http_status=422,
        )
    out = json.loads(r.stdout)
    if "error" in out:
        e = out["error"]
        raise LedgerSpyError(
            e["code"], e["message"], e.get("details") or {}, http_status=_ERROR_STATUS.get(e["code"], 422)
        )
    return out


def ocr_status(cfg: EffectiveConfig) -> dict[str, Any]:
    return _worker({"mode": "ocr_status", "ocr": cfg.settings.pdf.ocr.model_dump(mode="json")}, 60)


# ------------------------------------------------------------------------------------------- extraction


def item_id(evidence_sha256: str, page: int, line_no: int) -> str:
    return "PX-" + sha256_hex(f"{evidence_sha256}:{page}:{line_no}")[:16]


def run_extraction(path: Path, evidence_sha256: str, cfg: EffectiveConfig, dataset_id: str) -> dict[str, Any]:
    result = _worker(
        {"mode": "extract", "path": str(path), "settings": settings_for_worker(cfg)},
        cfg.settings.pdf.worker_timeout_seconds,
    )
    for it in result["items"]:
        it["item_id"] = item_id(evidence_sha256, it["page"], it["line_no"])
    by_no = {it["item_no"]: it["item_id"] for it in result["items"]}
    for it in result["items"]:
        if "continues_item" in it:
            it["continues_item_id"] = by_no[it["continues_item"]]
        if it.get("continuation_items"):
            it["continuation_item_ids"] = [by_no[n] for n in it["continuation_items"]]
    cfg_sha = config_sha(cfg)
    engine = result["engine"]
    ident = [dataset_id, evidence_sha256, cfg_sha, engine]
    result["extraction_id"] = "PE-" + sha256_hex(canonical_json(ident))[:16]
    result["config_sha256"] = cfg_sha
    result["items_sha256"] = sha256_hex(canonical_json(result["items"]))
    return result


def _column_names(items: list[dict[str, Any]]) -> dict[str, str]:
    """Role → column name of the extracted table. Direction is named only as the PDF's header names it."""
    lits: dict[str, set[str]] = {"debit": set(), "credit": set()}
    for x in items:
        for c in x.get("columns", []) if x["kind"] == "table_header" else []:
            if c["role"] in lits:
                lits[c["role"]].add(layout.key(layout._CUR_IN_HEADER.sub("", c["literal"])))
    names = dict(ROLE_COLUMNS)
    names["debit"] = "Withdrawal" if lits["debit"] and lits["debit"] <= layout.OUTFLOW_LITERALS else "Debit"
    names["credit"] = "Deposit" if lits["credit"] and lits["credit"] <= layout.INFLOW_LITERALS else "Credit"
    txns = [x for x in items if x["kind"] == "transaction"]
    indic = any(validate.indicator(t["fields"].get("balance")) for t in txns)
    names["balance"] = "Balance (Dr/Cr)" if indic else "Balance"  # an indicator-signed balance is not checked
    return names


def table(result: dict[str, Any], decisions: dict[str, str] | None) -> tuple[list[str], list[list[Any]]]:
    """The extracted table (header + rows). decisions=None: every transaction (upload preview); otherwise only
    auto-accepted or reviewer-confirmed transactions, excluded ones dropped."""
    items = result["items"]
    names = _column_names(items)
    txns = [x for x in items if x["kind"] == "transaction"]
    roles = [r for r, _ in ROLE_COLUMNS if any(r in t["roles_present"] for t in txns)]
    has_cur = any(t["context"].get("currency") for t in txns)
    has_acc = any(t["context"].get("account") for t in txns)
    others = sorted({k for t in txns for k in t.get("other_columns", {})})
    header = (
        [names[r] for r in roles] + (["Currency"] if has_cur else []) + (["Account No"] if has_acc else [])
    )
    header += [f"Other: {o}" for o in others] + PROVENANCE
    rows = []
    for t in txns:
        if decisions is not None:
            d = decisions.get(t["item_id"])
            if t["review_required"] and d != "confirm":
                continue
            review = "reviewer_confirmed" if t["review_required"] else "auto_accepted"
        else:
            review = "review_required" if t["review_required"] else "auto_accepted"
        f = t["fields"]
        row: list[Any] = [f.get(r) for r in roles]
        if has_cur:
            row.append(t["context"].get("currency"))
        if has_acc:
            row.append(t["context"].get("account"))
        row += [t.get("other_columns", {}).get(o) for o in others]
        row += [
            t["item_id"],
            str(t["page"]),
            json.dumps(t["bbox"]),
            t["text"],
            t["method"],
            None if t.get("ocr_min_confidence") is None else str(t["ocr_min_confidence"]),
            review,
            result["extraction_id"],
        ]
        rows.append(row)
    return header, rows


def table_source(path: Path, result: dict[str, Any], decisions: dict[str, str] | None) -> TableSource:
    header, rows = table(result, decisions)
    details = {
        "extraction_id": result["extraction_id"],
        "pages": len(result["pages"]),
        "_rows": [header, *rows],
    }
    return TableSource(path, "pdf", details, header)


def stored_details(src: TableSource) -> dict[str, Any]:
    return {k: v for k, v in src.details.items() if k != "_rows"}


def report_of(result: dict[str, Any]) -> dict[str, Any]:
    return {
        k: result[k] for k in ("pages", "not_processed_pages", "statement", "coverage", "warnings", "seconds")
    }


def insert_extraction(
    conn, dataset_id: str, evidence_sha256: str, result: dict[str, Any], actor: str
) -> bool:
    """Store an extraction (inside a transaction). False when the identical extraction already exists."""
    row = conn.execute(
        "SELECT items_sha256 FROM pdf_extractions WHERE extraction_id = ?", (result["extraction_id"],)
    ).fetchone()
    if row is not None:
        if row[0] != result["items_sha256"]:  # pragma: no cover — same input, configuration and engine
            raise LedgerSpyError(
                "pdf_nondeterministic", "re-extraction produced different items", http_status=500
            )
        return False
    conn.execute(
        "INSERT INTO pdf_extractions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            result["extraction_id"],
            dataset_id,
            evidence_sha256,
            result["config_sha256"],
            result["status"],
            canonical_json(result["engine"]).decode(),
            canonical_json(report_of(result)).decode(),
            result["items_sha256"],
            None,
            actor,
            utc_now_iso(),
        ),
    )
    conn.executemany(
        "INSERT INTO pdf_extraction_items VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                result["extraction_id"],
                it["item_no"],
                it["item_id"],
                it["page"],
                it["kind"],
                it["method"],
                int(bool(it.get("review_required"))),
                canonical_json(it).decode(),
            )
            for it in result["items"]
        ],
    )
    audit.append(
        conn,
        actor,
        "pdf.extracted",
        f"dataset:{dataset_id}",
        {
            "extraction_id": result["extraction_id"],
            "status": result["status"],
            "items": len(result["items"]),
            "items_sha256": result["items_sha256"],
            "methods": sorted({p["method"] for p in result["pages"]}),
            "engine": result["engine"],
        },
    )
    return True


def pdf_suggestion(suggestion: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """P3 bank proposal + the item ID as transaction ID + provenance notes."""
    pm = suggestion["proposed_mapping"]
    for f, col in list(pm["fields"].items()):
        if col in PROVENANCE:
            pm["fields"].pop(f)
    pm["fields"]["txn_id"] = "pdf_item_id"
    suggestion["fields"]["txn_id"] = {
        "column": "pdf_item_id",
        "status": "proposed",
        "basis": "the extraction item ID (PX-…): unique and stable per page line",
    }
    suggestion["unmapped_columns"] = [c for c in suggestion["unmapped_columns"] if c != "pdf_item_id"]
    suggestion["notes"].insert(
        0,
        "PDF statement: the table below was EXTRACTED from the PDF. Review-required "
        "rows must be confirmed or excluded before this mapping can be confirmed; the "
        "pdf_* columns are provenance kept in each transaction's `extra`.",
    )
    suggestion["pdf"] = {
        "extraction_id": result["extraction_id"],
        "status": result["status"],
        "transactions": result["coverage"]["transactions"],
        "review_required": result["coverage"]["review_required"],
    }
    return suggestion


# ------------------------------------------------------------------------------------------- reads


def _dataset(conn, dataset_id: str):
    row = conn.execute("SELECT * FROM datasets WHERE dataset_id = ?", (dataset_id,)).fetchone()
    if row is None:
        raise LedgerSpyError("dataset_not_found", "dataset not found", http_status=404)
    if row["format"] != "pdf":
        raise LedgerSpyError("not_a_pdf_dataset", "this dataset was not imported from a PDF", http_status=409)
    return row


def current_extraction_id(row) -> str | None:
    return json.loads(row["format_details"]).get("extraction_id")


def load_result(conn, extraction_id: str) -> dict[str, Any]:
    e = conn.execute("SELECT * FROM pdf_extractions WHERE extraction_id = ?", (extraction_id,)).fetchone()
    items = [
        json.loads(r[0])
        for r in conn.execute(
            "SELECT item FROM pdf_extraction_items WHERE extraction_id = ? ORDER BY item_no", (extraction_id,)
        )
    ]
    rep = json.loads(e["report"])
    return {
        "extraction_id": e["extraction_id"],
        "status": e["status"],
        "engine": json.loads(e["engine"]),
        "config_sha256": e["config_sha256"],
        "items_sha256": e["items_sha256"],
        "items": items,
        "created_at": e["created_at"],
        "created_by": e["created_by"],
        **rep,
    }


def decisions_of(conn, extraction_id: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in conn.execute(
        "SELECT * FROM pdf_review_decisions WHERE extraction_id = ? ORDER BY seq", (extraction_id,)
    ):
        out[r["item_id"]] = {
            "decision": r["decision"],
            "reason": r["reason"],
            "decided_by": r["decided_by"],
            "decided_at": r["decided_at"],
            "seq": r["seq"],
        }
    return out


def _review_state(it: dict[str, Any], dec: dict[str, Any] | None) -> str:
    if not it.get("review_required"):
        return "auto_accepted" if it["kind"] == "transaction" else "not_reviewable"
    if dec is None:
        return "pending"
    return "confirmed" if dec["decision"] == "confirm" else "excluded"


def gate(conn, row) -> tuple[dict[str, Any], dict[str, str]]:
    """The extraction and decisions a confirmation may use, or an explicit refusal."""
    eid = current_extraction_id(row)
    if eid is None:
        raise IngestError("pdf_no_extraction", "this PDF has no extraction", http_status=409)
    result = load_result(conn, eid)
    if result["status"] != "completed":
        raise IngestError(
            "pdf_extraction_incomplete",
            f"the extraction is {result['status']}: page(s) {result['not_processed_pages']} were not "
            "processed (see GET /api/datasets/{id}/pdf). Fix the cause (e.g. install Tesseract and its "
            "language data) and re-run POST /api/datasets/{id}/pdf/extract; an incomplete statement cannot "
            "be confirmed.",
            {"not_processed_pages": result["not_processed_pages"]},
            http_status=409,
        )
    decs = decisions_of(conn, eid)
    pending = [
        it["item_id"] for it in result["items"] if it.get("review_required") and it["item_id"] not in decs
    ]
    if pending:
        raise IngestError(
            "pdf_review_pending",
            f"{len(pending)} review-required item(s) have no decision; confirm or exclude each before "
            "confirming the mapping",
            {"pending": len(pending), "examples": pending[:20]},
            http_status=409,
        )
    if not any(it["kind"] == "transaction" for it in result["items"]):
        raise IngestError(
            "pdf_no_transactions", "no transaction rows were extracted from this PDF", http_status=409
        )
    return result, {k: v["decision"] for k, v in decs.items()}


def confirm_source(conn, row, path: Path) -> TableSource:
    result, decs = gate(conn, row)
    src = table_source(path, result, decs)
    if len(src.details["_rows"]) < 2:
        raise IngestError(
            "pdf_no_trusted_rows", "every transaction row was excluded: nothing to confirm", http_status=409
        )
    return src


def summary(conn, dataset_id: str) -> dict[str, Any]:
    row = _dataset(conn, dataset_id)
    eid = current_extraction_id(row)
    history = [
        dict(r)
        for r in conn.execute(
            "SELECT extraction_id, status, created_at, created_by, items_sha256 FROM pdf_extractions "
            "WHERE dataset_id = ? ORDER BY created_at, extraction_id",
            (dataset_id,),
        )
    ]
    if eid is None:
        return {"dataset_id": dataset_id, "extraction": None, "history": history}
    res = load_result(conn, eid)
    decs = decisions_of(conn, eid)
    states: dict[str, int] = {}
    for it in res["items"]:
        s = _review_state(it, decs.get(it["item_id"]))
        states[s] = states.get(s, 0) + 1
    confirmed = row["status"] == "canonicalised"
    checks_confirmed = None
    if confirmed:
        mapping = json.loads(row["mapping"])
        dm = (mapping.get("bank") or {}).get("debit_means")
        incl = [
            it
            for it in res["items"]
            if it["kind"] == "transaction"
            and (not it["review_required"] or (decs.get(it["item_id"]) or {}).get("decision") == "confirm")
        ]
        copies = [json.loads(json.dumps(t)) for t in incl]
        checks_confirmed = validate.statement_checks(
            copies, res["statement"]["stated"], dm, res["statement"]["decimal_separator"]
        )
    return {
        "dataset_id": dataset_id,
        "evidence": {
            "sha256": row["evidence_sha256"],
            "original_filename": row["original_filename"],
            "immutable": True,
        },
        "data_state": "confirmed" if confirmed else "extracted_awaiting_review_and_confirmation",
        "states": {
            "extracted": "pdf_extraction_items: classified lines with source text, page and box (immutable)",
            "reviewed": "pdf_review_decisions: confirm / exclude per review-required item (append-only)",
            "confirmed": "canonical transactions created by the confirmed mapping (only trusted rows)",
            "normalized": "P4 logged transformations over canonical rows (after confirmation)",
        },
        "extraction": {
            "extraction_id": eid,
            "status": res["status"],
            "created_at": res["created_at"],
            "config_sha256": res["config_sha256"],
            "items_sha256": res["items_sha256"],
            "engine": res["engine"],
            "not_processed_pages": res["not_processed_pages"],
            "warnings": res["warnings"],
            "seconds": res["seconds"],
        },
        "pages": res["pages"],
        "coverage": res["coverage"],
        "review": {
            "by_state": dict(sorted(states.items())),
            "pending": states.get("pending", 0),
            "can_confirm": res["status"] == "completed"
            and not states.get("pending")
            and res["coverage"]["transactions"] > 0,
        },
        "statement": res["statement"],
        "statement_checks_with_confirmed_mapping": checks_confirmed,
        "history": history,
        "note": "Text, OCR and parse confidences are reported separately; none of them is a measure of "
        "financial correctness. OCR-derived rows are always review-required.",
    }


def items_page(
    conn, dataset_id: str, kind: str | None, review: str | None, pdf_page: int | None, page: int, size: int
) -> dict[str, Any]:
    row = _dataset(conn, dataset_id)
    eid = current_extraction_id(row)
    if eid is None:
        return {"dataset_id": dataset_id, "page": page, "size": size, "total": 0, "items": []}
    res = load_result(conn, eid)
    decs = decisions_of(conn, eid)
    out = []
    for it in res["items"]:
        state = _review_state(it, decs.get(it["item_id"]))
        if kind and it["kind"] != kind:
            continue
        if pdf_page and it["page"] != pdf_page:
            continue
        if review == "required" and not it.get("review_required"):
            continue
        if review and review != "required" and state != review:
            continue
        out.append(_public(it) | {"review_state": state, "decision": decs.get(it["item_id"])})
    return {
        "dataset_id": dataset_id,
        "extraction_id": eid,
        "page": page,
        "size": size,
        "total": len(out),
        "items": out[(page - 1) * size : page * size],
    }


def _public(it: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "item_id",
        "item_no",
        "page",
        "line_no",
        "kind",
        "method",
        "bbox",
        "text",
        "fields",
        "other_columns",
        "cells",
        "parse",
        "reasons",
        "review_required",
        "info",
        "context",
        "uncertain_words",
        "low_confidence_words",
        "columns_source",
        "continues_item_id",
        "continuation_item_ids",
        "reason",
        "columns",
        "ocr_min_confidence",
        "replacement_chars",
    )
    return {k: it[k] for k in keep if k in it}


def item_detail(conn, dataset_id: str, iid: str) -> dict[str, Any]:
    row = _dataset(conn, dataset_id)
    eid = current_extraction_id(row)
    r = (
        None
        if eid is None
        else conn.execute(
            "SELECT item FROM pdf_extraction_items WHERE extraction_id = ? AND item_id = ?", (eid, iid)
        ).fetchone()
    )
    if r is None:
        raise LedgerSpyError("item_not_found", "extraction item not found", http_status=404)
    it = json.loads(r[0])
    hist = [
        dict(x)
        for x in conn.execute(
            "SELECT seq, decision, reason, decided_by, decided_at FROM pdf_review_decisions WHERE "
            "extraction_id = ? AND item_id = ? ORDER BY seq",
            (eid, iid),
        )
    ]
    return {
        "dataset_id": dataset_id,
        "extraction_id": eid,
        "item": _public(it),
        "review_state": _review_state(it, hist[-1] if hist else None),
        "decisions": hist,
        "evidence_sha256": row["evidence_sha256"],
    }


# ------------------------------------------------------------------------------------------- writes


def review(conn, dataset_id: str, decisions: list[dict[str, Any]], actor: str) -> dict[str, Any]:
    row = _dataset(conn, dataset_id)
    if row["status"] == "canonicalised":
        raise LedgerSpyError(
            "already_confirmed", "the dataset is confirmed; its review is closed", http_status=409
        )
    eid = current_extraction_id(row)
    if eid is None:
        raise LedgerSpyError("pdf_no_extraction", "this PDF has no extraction", http_status=409)
    items = {
        json.loads(r[0])["item_id"]: json.loads(r[0])
        for r in conn.execute("SELECT item FROM pdf_extraction_items WHERE extraction_id = ?", (eid,))
    }
    errors = []
    for d in decisions:
        it = items.get(d["item_id"])
        if it is None:
            errors.append({"item_id": d["item_id"], "error": "unknown item in the current extraction"})
        elif not it.get("review_required"):
            errors.append({"item_id": d["item_id"], "error": "item does not require review"})
        elif it["kind"] != "transaction" and d["decision"] == "confirm":
            errors.append(
                {
                    "item_id": d["item_id"],
                    "error": f"a {it['kind']} line can only be excluded (acknowledged as not a transaction)",
                }
            )
    if errors:
        raise LedgerSpyError(
            "review_invalid",
            "some decisions were refused; none was stored",
            {"errors": errors},
            http_status=422,
        )
    now = utc_now_iso()
    with transaction(conn):
        conn.executemany(
            "INSERT INTO pdf_review_decisions (extraction_id, item_id, decision, reason, decided_by, "
            "decided_at) VALUES (?, ?, ?, ?, ?, ?)",
            [(eid, d["item_id"], d["decision"], d.get("reason"), actor, now) for d in decisions],
        )
        audit.append(
            conn,
            actor,
            "pdf.reviewed",
            f"dataset:{dataset_id}",
            {
                "extraction_id": eid,
                "decisions": [{"item_id": d["item_id"], "decision": d["decision"]} for d in decisions],
            },
        )
    return summary(conn, dataset_id)


def reextract(conn, cfg: EffectiveConfig, workspace: Path, dataset_id: str, actor: str) -> dict[str, Any]:
    """Run the extraction again with the current configuration and engines (e.g. after installing OCR)."""
    from ledgerspy.ingest.mapping import profile_table, suggest

    row = _dataset(conn, dataset_id)
    if row["status"] == "canonicalised":
        raise LedgerSpyError(
            "already_confirmed", "a confirmed dataset is immutable; upload the PDF again", http_status=409
        )
    path = evidence.verify_one(conn, row["evidence_sha256"], workspace)
    try:
        result = run_extraction(path, row["evidence_sha256"], cfg, dataset_id)
    except LedgerSpyError as exc:
        with transaction(conn):
            audit.append(
                conn,
                actor,
                "pdf.extraction_failed",
                f"dataset:{dataset_id}",
                {"code": exc.code, "message": exc.message},
            )
        raise
    src = table_source(path, result, None)
    profile = profile_table(src, cfg.settings)
    sugg = pdf_suggestion(suggest(profile, cfg.settings, "bank_statement"), result)
    from ledgerspy.ingest.service import _json, _profile_json

    with transaction(conn):
        new = insert_extraction(conn, dataset_id, row["evidence_sha256"], result, actor)
        conn.execute(
            "UPDATE datasets SET format_details = ?, header = ?, row_count = ?, profile = ?, suggestion = ? "
            "WHERE dataset_id = ?",
            (
                _json(stored_details(src)),
                _json(src.header),
                profile.rows,
                _json(_profile_json(profile)),
                _json(sugg),
                dataset_id,
            ),
        )
    return summary(conn, dataset_id) | {"new_extraction": new}


def page_image(
    conn, cfg: EffectiveConfig, workspace: Path, dataset_id: str, page: int, dpi: int | None
) -> bytes:
    row = _dataset(conn, dataset_id)
    rc = cfg.settings.pdf.render
    dpi = dpi or rc.preview_dpi
    if dpi > rc.max_preview_dpi:
        raise LedgerSpyError(
            "dpi_too_high",
            f"dpi must be at most {rc.max_preview_dpi}",
            {"max_preview_dpi": rc.max_preview_dpi},
            http_status=422,
        )
    path = evidence.verify_one(conn, row["evidence_sha256"], workspace)
    with tempfile.TemporaryDirectory(prefix="ls-page-") as tmp:
        out = Path(tmp) / "page.png"
        _worker(
            {
                "mode": "render",
                "path": str(path),
                "page": page,
                "dpi": dpi,
                "max_pixels": rc.max_pixels,
                "out": str(out),
            },
            120,
        )
        return out.read_bytes()
