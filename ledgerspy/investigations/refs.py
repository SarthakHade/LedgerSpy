"""Evidence references: validate that a linked record exists, belongs to the investigation's engagement case,
and snapshot it (a small display snapshot plus the SHA-256 of the full referenced object at link time).

Nothing is copied into case tables except that snapshot; the record stays where it is and is resolved again on
demand (`verify`) to report whether it is still `current`, has `changed`, is `stale` (a reconciliation run
whose inputs changed, e.g. after an OCR correction) or is `missing`. Identifiers are matched by strict
patterns and looked up as values (no file paths, no SQL built from input).
"""

import json
import re
from typing import Any

from ledgerspy.config import EffectiveConfig
from ledgerspy.errors import LedgerSpyError
from ledgerspy.hashing import canonical_json, sha256_hex
from ledgerspy.investigations.model import LINK_TYPES

PATTERNS = {
    "risk_entity": r"^E-[0-9a-f]{16}$",
    "signal": r"^S-[0-9a-f]{20}$",
    "entity": r"^CE-[0-9a-f]{16}$",
    "entity_pair": r"^P-[0-9a-f]{16}$",
    "graph_node": r"^CE-[0-9a-f]{16}$",
    "graph_edge": r"^G-[0-9a-f]{16}$",
    "graph_cycle": r"^C-[0-9a-f]{16}$",
    "graph_signal": r"^S-[0-9a-f]{20}$",
    "reconciliation_run": r"^RR-[0-9a-f]{16}$",
    "reconciliation_result": r"^RM-[0-9a-f]{16}$",
    "reconciliation_finding": r"^S-[0-9a-f]{20}$",
    "duplicate_group": r"^RD-[0-9a-f]{16}$",
    "evidence_file": r"^[0-9a-f]{64}$",
    "pdf_page": r"^[1-9][0-9]{0,5}$",
    "pdf_item": r"^PX-[0-9a-f]{16}$",
}
TXN_ID = re.compile(r"^[^\x00-\x1f/\\]{1,128}$")  # source IDs are free text: printable, no path separators
DATASET_ID = re.compile(r"^D-[0-9A-HJKMNP-TV-Z]{26}$")
RUN_ID = re.compile(r"^RR-[0-9a-f]{16}$")


def _err(code: str, message: str, status: int = 422, **details: Any) -> LedgerSpyError:
    return LedgerSpyError(code, message, details, http_status=status)


def _sha(obj: Any) -> str:
    return sha256_hex(canonical_json(obj))


def _pick(obj: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: obj.get(k) for k in keys if k in obj}


def _dataset(conn, case_id: str, dataset_id: str | None) -> Any:
    if not dataset_id or not DATASET_ID.match(dataset_id):
        raise _err("invalid_reference", "a valid dataset_id (D-…) is required for this link type")
    row = conn.execute("SELECT * FROM datasets WHERE dataset_id = ?", (dataset_id,)).fetchone()
    if row is None:
        raise _err("not_found", f"dataset {dataset_id} not found", 404)
    if row["case_id"] != case_id:
        raise _err(
            "cross_scope_reference",
            "the dataset belongs to another engagement case",
            422,
            dataset_case_id=row["case_id"],
            investigation_case_id=case_id,
        )
    return row


def _run_row(conn, case_id: str, run_id: str | None) -> Any:
    if not run_id or not RUN_ID.match(run_id):
        raise _err("invalid_reference", "a valid run_id (RR-…) is required for this link type")
    row = conn.execute("SELECT * FROM reconciliation_runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise _err("not_found", f"reconciliation run {run_id} not found", 404)
    if row["case_id"] != case_id:
        raise _err("cross_scope_reference", "the reconciliation run belongs to another engagement case")
    return row


def _canonical_ds(row) -> None:
    if row["status"] != "canonicalised":
        raise _err("not_canonicalised", "the dataset's mapping has not been confirmed yet", 409)


def resolve(
    conn,
    cfg: EffectiveConfig,
    case_id: str,
    link_type: str,
    dataset_id: str | None,
    run_id: str | None,
    ref_id: str,
) -> dict[str, Any]:
    """{scope_key, dataset_id, run_id, ref_id, snapshot, snapshot_sha256} or an explicit error."""
    if link_type not in LINK_TYPES:
        raise _err("invalid_link_type", f"unknown link type {link_type!r}", 422, allowed=sorted(LINK_TYPES))
    spec = LINK_TYPES[link_type]
    pat = PATTERNS.get(link_type)
    if (
        not isinstance(ref_id, str)
        or (pat and not re.match(pat, ref_id))
        or (not pat and not TXN_ID.match(ref_id))
    ):
        raise _err("invalid_reference", f"ref_id is not a valid {link_type} identifier")
    if spec["dataset"]:
        ds = _dataset(conn, case_id, dataset_id)
    elif dataset_id is not None:
        raise _err("invalid_reference", f"{link_type} links take no dataset_id")
    if spec["run"]:
        _run_row(conn, case_id, run_id)
    elif run_id is not None:
        raise _err("invalid_reference", f"{link_type} links take no run_id")
    full, snap = _RESOLVERS[link_type](conn, cfg, case_id, ds if spec["dataset"] else None, run_id, ref_id)
    scope_key = "|".join([link_type, dataset_id or "", run_id or "", ref_id])
    return {
        "scope_key": scope_key,
        "dataset_id": dataset_id if spec["dataset"] else None,
        "run_id": run_id if spec["run"] else None,
        "ref_id": ref_id,
        "snapshot": snap,
        "snapshot_sha256": _sha(full),
    }


# ------------------------------------------------------------------------------------------- resolvers


def _transaction(conn, cfg, case_id, ds, run_id, ref):
    _canonical_ds(ds)
    r = conn.execute(
        "SELECT * FROM transactions WHERE dataset_id = ? AND txn_id = ?", (ds["dataset_id"], ref)
    ).fetchone()
    if r is None:
        raise _err("not_found", f"transaction {ref!r} not found in {ds['dataset_id']}", 404)
    full = {k: r[k] for k in r.keys()}  # noqa: SIM118
    snap = _pick(
        full,
        (
            "txn_id",
            "source_row",
            "posted_at",
            "amount_minor",
            "currency",
            "direction",
            "reference",
            "payer_raw",
            "payee_raw",
        ),
    ) | {
        "description": (full.get("description") or "")[:200] or None,
        "dataset_kind": ds["kind"],
        "evidence_sha256": ds["evidence_sha256"],
    }
    return full, snap


def _risk(conn, cfg, ds):
    _canonical_ds(ds)
    if ds["kind"] != "ledger":
        raise _err("wrong_dataset_kind", "P9 risk findings exist for ledger datasets only")
    from ledgerspy.risk import service as risk_service

    return risk_service.run_risk(conn, cfg, ds["dataset_id"])


def _risk_transaction(conn, cfg, case_id, ds, run_id, ref):
    from ledgerspy.risk import service as risk_service

    res = _risk(conn, cfg, ds)
    d = risk_service.transaction_detail(res, ref, cfg.settings)
    if d is None:
        raise _err("not_found", f"no P9 risk row for transaction {ref!r}", 404)
    t, e = d["transaction"], d["explanation"]
    full = {"transaction": t, "contributors": e["all_contributors"], "tier_reason": e["tier_reason"]}
    snap = {
        "txn_id": ref,
        "status": t.get("status"),
        "score": t.get("score"),
        "tier": t.get("tier"),
        "independent_sources": t.get("independent_sources"),
        "headline": e["headline"],
        "note": "automated review-risk score (P9): a lead for review, not proof",
    }
    return full, snap


def _risk_entity(conn, cfg, case_id, ds, run_id, ref):
    from ledgerspy.risk import service as risk_service

    d = risk_service.entity_detail(_risk(conn, cfg, ds), ref)
    if d is None:
        raise _err("not_found", f"no P9 entity {ref!r}", 404)
    e = d["entity"]
    return d, _pick(e, ("entity_id", "name", "flow", "score", "tier", "transactions"))


def _signal(conn, cfg, case_id, ds, run_id, ref):
    s = _risk(conn, cfg, ds).signals.get(ref)
    if s is None:
        raise _err("not_found", f"no detector signal {ref!r} in this dataset's current analysis", 404)
    snap = _pick(
        s,
        (
            "signal_id",
            "rule_id",
            "code",
            "family",
            "subject_type",
            "subject_id",
            "severity",
            "strength",
            "txn_ids",
        ),
    )
    if isinstance(snap.get("txn_ids"), list):
        snap["txn_ids"] = snap["txn_ids"][:20]
    return s, snap


def _resolution(conn, cfg, ds):
    _canonical_ds(ds)
    from ledgerspy.resolution import service as resolution_service

    return resolution_service.run(conn, cfg, ds["dataset_id"])


def _entity(conn, cfg, case_id, ds, run_id, ref):
    from ledgerspy.resolution import service as resolution_service

    d = resolution_service.entity_detail(_resolution(conn, cfg, ds), ref)
    return d, _pick(
        d, ("canonical_entity_id", "display_name", "entity_class", "resolution_status", "entity_types")
    )


def _entity_pair(conn, cfg, case_id, ds, run_id, ref):
    from ledgerspy.resolution import service as resolution_service

    d = resolution_service.pair_detail(_resolution(conn, cfg, ds), ref)
    snap = _pick(d, ("pair_id", "status", "evidence_score", "suggested_action")) | {"name": d.get("name")}
    return d, snap


def _graph(conn, cfg, ds):
    _canonical_ds(ds)
    from ledgerspy.graph import service as graph_service

    return graph_service.run(conn, cfg, ds["dataset_id"])


def _graph_node(conn, cfg, case_id, ds, run_id, ref):
    r = _graph(conn, cfg, ds)
    n = r.graph.nodes.get(ref)
    if n is None:
        raise _err("not_found", f"no graph node {ref!r}", 404)
    return n, _pick(n, ("label", "node_type")) | {"node_id": ref, "graph_sha256": r.graph.sha256}


def _graph_edge(conn, cfg, case_id, ds, run_id, ref):
    e = _graph(conn, cfg, ds).graph.by_id.get(ref)
    if e is None:
        raise _err("not_found", f"no graph edge {ref!r}", 404)
    return e, _pick(e, ("edge_id", "type", "source", "target", "currency", "amount_minor", "transactions"))


def _graph_cycle(conn, cfg, case_id, ds, run_id, ref):
    c = next((c for c in _graph(conn, cfg, ds).cycles["cycles"] if c["cycle_id"] == ref), None)
    if c is None:
        raise _err("not_found", f"no graph cycle {ref!r}", 404)
    return c, _pick(c, ("cycle_id", "status", "length", "span_days", "value_retention"))


def _graph_signal(conn, cfg, case_id, ds, run_id, ref):
    s = next((s for s in _graph(conn, cfg, ds).signals if s["signal_id"] == ref), None)
    if s is None:
        raise _err("not_found", f"no graph signal {ref!r}", 404)
    return s, _pick(s, ("signal_id", "rule_id", "severity", "explanation"))


def _run(conn, cfg, run_id):
    from ledgerspy.reconciliation import service as recon

    try:
        return recon.get_run(conn, cfg, run_id)
    except LedgerSpyError as exc:
        if exc.code == "inputs_changed":
            raise _err(
                "stale_reconciliation_run",
                "the run's inputs changed after it was recorded (e.g. an OCR "
                "correction): start a new reconciliation run and link its results",
                409,
            ) from exc
        raise


def _reconciliation_run(conn, cfg, case_id, ds, run_id, ref):
    if ref != run_id:
        raise _err("invalid_reference", "for a reconciliation_run link, ref_id must equal run_id")
    row = conn.execute("SELECT * FROM reconciliation_runs WHERE run_id = ?", (run_id,)).fetchone()
    full = {k: row[k] for k in row.keys()}  # noqa: SIM118
    snap = {
        "run_id": run_id,
        "ledger_dataset_id": row["ledger_dataset_id"],
        "bank_dataset_id": row["bank_dataset_id"],
        "result_sha256": row["result_sha256"],
        "summary": json.loads(row["summary"]).get("results_by_status"),
    }
    return full, snap


def _reconciliation_result(conn, cfg, case_id, ds, run_id, ref):
    r = _run(conn, cfg, run_id).by_id.get(ref)
    if r is None:
        raise _err("not_found", f"no result {ref!r} in run {run_id}", 404)
    snap = _pick(
        r,
        (
            "result_id",
            "status",
            "ledger_txn_ids",
            "bank_txn_ids",
            "amount_difference_minor",
            "date_difference_days",
            "evidence_score",
            "discrepancies",
        ),
    ) | {"candidates": len(r.get("candidates") or [])}
    return r, snap


def _reconciliation_finding(conn, cfg, case_id, ds, run_id, ref):
    s = next((s for s in _run(conn, cfg, run_id).findings if s["signal_id"] == ref), None)
    if s is None:
        raise _err("not_found", f"no reconciliation finding {ref!r} in run {run_id}", 404)
    return s, _pick(s, ("signal_id", "rule_id", "severity", "ledger_txn_ids", "bank_txn_ids", "result_id"))


def _duplicate_group(conn, cfg, case_id, ds, run_id, ref):
    g = next((g for g in _run(conn, cfg, run_id).duplicates if g["group_id"] == ref), None)
    if g is None:
        raise _err("not_found", f"no duplicate group {ref!r} in run {run_id}", 404)
    return g, _pick(g, ("group_id", "side", "type", "txn_ids", "amount_minor", "currency", "status"))


def _evidence_file(conn, cfg, case_id, ds, run_id, ref):
    ev = conn.execute("SELECT * FROM evidence_files WHERE sha256 = ?", (ref,)).fetchone()
    if ev is None:
        raise _err("not_found", "no evidence file with this SHA-256", 404)
    names = [
        r[0]
        for r in conn.execute(
            "SELECT original_filename FROM datasets WHERE evidence_sha256 = ? AND case_id = ? UNION "
            "SELECT original_filename FROM master_files WHERE evidence_sha256 = ? AND case_id = ? ORDER BY 1",
            (ref, case_id, ref, case_id),
        )
    ]
    if not names:
        raise _err(
            "cross_scope_reference", "this evidence file is not part of the investigation's engagement case"
        )
    full = {"sha256": ref, "size_bytes": ev["size_bytes"], "stored_at": ev["stored_at"], "names": names}
    return full, full


def _pdf(conn, ds):
    if ds["format"] != "pdf":
        raise _err("not_a_pdf_dataset", "this dataset was not imported from a PDF", 409)


def _pdf_page(conn, cfg, case_id, ds, run_id, ref):
    _pdf(conn, ds)
    from ledgerspy.pdfstatements import service as pdf_service

    s = pdf_service.summary(conn, ds["dataset_id"])
    pages = s.get("pages") or []
    page = int(ref)
    p = next((p for p in pages if p["page"] == page), None)
    if p is None:
        raise _err("not_found", f"page {page} does not exist in this PDF", 404)
    full = {
        "page": page,
        "evidence_sha256": ds["evidence_sha256"],
        "method": p.get("method"),
        "extraction_id": s["extraction"]["extraction_id"],
    }
    return full, full


def _pdf_item(conn, cfg, case_id, ds, run_id, ref):
    _pdf(conn, ds)
    from ledgerspy.pdfstatements import service as pdf_service

    d = pdf_service.item_detail(conn, ds["dataset_id"], ref)
    it = d["item"]
    snap = _pick(it, ("item_id", "page", "kind", "method", "text", "fields", "reasons", "bbox")) | {
        "review_state": d["review_state"],
        "extraction_id": d["extraction_id"],
        "evidence_sha256": ds["evidence_sha256"],
    }
    return it, snap


_RESOLVERS = {
    "transaction": _transaction,
    "risk_transaction": _risk_transaction,
    "risk_entity": _risk_entity,
    "signal": _signal,
    "entity": _entity,
    "entity_pair": _entity_pair,
    "graph_node": _graph_node,
    "graph_edge": _graph_edge,
    "graph_cycle": _graph_cycle,
    "graph_signal": _graph_signal,
    "reconciliation_run": _reconciliation_run,
    "reconciliation_result": _reconciliation_result,
    "reconciliation_finding": _reconciliation_finding,
    "duplicate_group": _duplicate_group,
    "evidence_file": _evidence_file,
    "pdf_page": _pdf_page,
    "pdf_item": _pdf_item,
}
assert set(_RESOLVERS) == set(LINK_TYPES)  # noqa: S101 — every link type has a resolver


def verify(conn, cfg: EffectiveConfig, case_id: str, link: dict[str, Any]) -> dict[str, Any]:
    """Resolve a stored link again: current / changed / stale / missing (never raises for these states)."""
    try:
        now = resolve(
            conn, cfg, case_id, link["link_type"], link["dataset_id"], link["run_id"], link["ref_id"]
        )
    except LedgerSpyError as exc:
        state = (
            "stale"
            if exc.code == "stale_reconciliation_run"
            else "missing"
            if exc.http_status == 404
            else "unavailable"
        )
        return {"state": state, "reason": exc.message}
    if now["snapshot_sha256"] == link["snapshot_sha256"]:
        return {"state": "current", "reason": None}
    return {
        "state": "changed",
        "reason": "the referenced record differs from what was linked",
        "current_snapshot": now["snapshot"],
    }
