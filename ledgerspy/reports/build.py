"""Report content (P16): built only from persisted records through the existing services.

Nothing here recomputes an analysis of its own: investigations come from P15, reconciliation from the P14 run
register (and the run's own row-level results), extraction values from P14b, evidence from the P2 store. A
report never writes: the stored artefact is written by `store.generate`, which only appends a report row and
an audit entry.

Content is deterministic for the same source state and configuration: it contains no generation time, and
every list has a fixed order. Each claim keeps its origin (automated / analyst / computed), the records it
references with their verification state, the evidence files behind them with their hashes and verification,
the page / row location where one exists, and its limitations and contradicting evidence.
"""

import json
import re
from collections import defaultdict
from typing import Any

from ledgerspy.cases import evidence, store
from ledgerspy.cases.db import SCHEMA_VERSION
from ledgerspy.config.loader import EffectiveConfig
from ledgerspy.errors import LedgerSpyError, require
from ledgerspy.hashing import canonical_json, sha256_hex
from ledgerspy.investigations import refs
from ledgerspy.investigations import service as inv_service
from ledgerspy.reconciliation import engine as recon_engine
from ledgerspy.reconciliation import service as recon
from ledgerspy.reports import model

ALL = 10**9
TRUSTED_EXTRACTION = ("auto_accepted", "reviewer_confirmed", "reviewer_corrected")


def _err(code: str, message: str, status: int = 422, **details: Any) -> LedgerSpyError:
    return LedgerSpyError(code, message, details or None, http_status=status)


# ------------------------------------------------------------------------------------------- context


class _Ctx:
    """Per-report caches: evidence verification and dataset metadata are looked up once per report."""

    def __init__(self, conn, cfg: EffectiveConfig, workspace) -> None:
        self.conn, self.cfg, self.workspace = conn, cfg, workspace
        self._evidence: dict[str, str] = {}
        self._datasets: dict[str, dict[str, Any]] = {}

    def evidence_state(self, sha: str) -> str:
        if sha not in self._evidence:
            try:
                evidence.verify_one(self.conn, sha, self.workspace)
                self._evidence[sha] = "verified"
            except LedgerSpyError as exc:
                self._evidence[sha] = exc.code
        return self._evidence[sha]

    def dataset(self, dataset_id: str) -> dict[str, Any]:
        if dataset_id not in self._datasets:
            r = self.conn.execute(
                "SELECT dataset_id, case_id, kind, format, original_filename, row_count, evidence_sha256, "
                "status, synthetic FROM datasets WHERE dataset_id = ?",
                (dataset_id,),
            ).fetchone()
            if r is None:
                raise _err("dataset_not_found", "dataset not found", 404)
            d = dict(r)
            # P24: NULL = the source declared nothing; never report that as "not synthetic"
            d["synthetic"] = None if d["synthetic"] is None else bool(d["synthetic"])
            d["evidence_verification"] = self.evidence_state(d["evidence_sha256"])
            self._datasets[dataset_id] = d
        return self._datasets[dataset_id]

    def money(self, minor: int | None, currency: str | None) -> dict[str, Any] | None:
        if minor is None:
            return None
        mu = self.cfg.settings.ingest.minor_units
        digits = mu.by_currency.get(currency or "", mu.default_digits)
        sign = "-" if minor < 0 else ""
        q, r = divmod(abs(minor), 10**digits) if digits else (abs(minor), 0)
        text = f"{sign}{q:,}.{r:0{digits}d}" if digits else f"{sign}{q:,}"
        return {"minor": minor, "currency": currency, "text": f"{text} {currency or '(currency unknown)'}"}


def _claim_id(*parts: Any) -> str:
    return "CL-" + sha256_hex(canonical_json(list(parts)))[:16]


# ------------------------------------------------------------------------------------------- transactions


def _txn(ctx: _Ctx, dataset_id: str, txn_id: str) -> dict[str, Any] | None:
    """A canonical row with its location, any logged P4 override, and its PDF provenance (if from a PDF)."""
    r = ctx.conn.execute(
        "SELECT txn_id, source_row, posted_at, amount_minor, currency, direction, reference, payer_raw, "
        "payee_raw, extra FROM transactions WHERE dataset_id = ? AND txn_id = ?",
        (dataset_id, txn_id),
    ).fetchone()
    if r is None:
        return None
    d = dict(r)
    extra = json.loads(d.pop("extra") or "{}")
    overrides = {
        f: {"value": json.loads(v), "transformation_id": t}
        for f, v, t in ctx.conn.execute(
            "SELECT field, value, transformation_id FROM normalized_values WHERE dataset_id = ? AND txn_id = "
            "? ORDER BY field",
            (dataset_id, txn_id),
        )
    }
    ds = ctx.dataset(dataset_id)
    out = {
        "dataset_id": dataset_id,
        "txn_id": txn_id,
        "canonical": d,
        "overrides": overrides,
        "amount_minor": overrides["amount_minor"]["value"]
        if "amount_minor" in overrides
        else d["amount_minor"],
        "currency": d["currency"],
        "posted_at": overrides["posted_at"]["value"] if "posted_at" in overrides else d["posted_at"],
        "location": {
            "dataset_id": dataset_id,
            "filename": ds["original_filename"],
            "source_row": d["source_row"],
            "evidence_sha256": ds["evidence_sha256"],
        },
    }
    if "pdf_review" in extra:
        out["location"] |= {
            "pdf_page": extra.get("pdf_page"),
            "pdf_item_id": extra.get("pdf_item_id"),
            "extraction_method": extra.get("pdf_method"),
            "review_state": extra.get("pdf_review"),
        }
    out["amount"] = ctx.money(out["amount_minor"], out["currency"])
    return out


def _single_txn(link: dict[str, Any]) -> tuple[str, str] | None:
    if link["link_type"] in ("transaction", "risk_transaction"):
        return link["dataset_id"], link["ref_id"]
    return None


# ------------------------------------------------------------------------------------------- links


def _locations(ctx: _Ctx, link: dict[str, Any], snap: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Source locations a link points at: rows, PDF pages / items, or the run's datasets."""
    lt, snap = link["link_type"], snap or link["snapshot"]
    pair = _single_txn(link)
    if pair:
        t = _txn(ctx, *pair)
        return [t["location"]] if t else []
    if lt == "pdf_item":
        ds = ctx.dataset(link["dataset_id"])
        return [
            {
                "dataset_id": link["dataset_id"],
                "filename": ds["original_filename"],
                "pdf_page": snap.get("page"),
                "pdf_item_id": link["ref_id"],
                "bbox": snap.get("bbox"),
                "evidence_sha256": ds["evidence_sha256"],
            }
        ]
    if lt == "pdf_page":
        ds = ctx.dataset(link["dataset_id"])
        return [
            {
                "dataset_id": link["dataset_id"],
                "filename": ds["original_filename"],
                "pdf_page": int(link["ref_id"]),
                "evidence_sha256": ds["evidence_sha256"],
            }
        ]
    if link["run_id"]:
        row = ctx.conn.execute(
            "SELECT ledger_dataset_id, bank_dataset_id FROM reconciliation_runs WHERE run_id = ?",
            (link["run_id"],),
        ).fetchone()
        out = []
        ids = {"ledger": snap.get("ledger_txn_ids") or [], "bank": snap.get("bank_txn_ids") or []}
        if lt == "duplicate_group":
            ids = {snap.get("side") or "bank": snap.get("txn_ids") or []}
        for side, ds_id in (("ledger", row[0]), ("bank", row[1])):
            for t in sorted(ids.get(side, []))[:20]:
                tx = _txn(ctx, ds_id, t)
                if tx:
                    out.append(tx["location"] | {"side": side})
        if not out:
            for ds_id in row:
                ds = ctx.dataset(ds_id)
                out.append(
                    {
                        "dataset_id": ds_id,
                        "filename": ds["original_filename"],
                        "evidence_sha256": ds["evidence_sha256"],
                    }
                )
        return out
    if lt == "evidence_file":
        return [{"evidence_sha256": link["ref_id"]}]
    ds = ctx.dataset(link["dataset_id"])
    return [
        {
            "dataset_id": ds["dataset_id"],
            "filename": ds["original_filename"],
            "evidence_sha256": ds["evidence_sha256"],
        }
    ]


def _reference(ctx: _Ctx, case_id: str, link: dict[str, Any]) -> dict[str, Any]:
    ver, now = refs.check(ctx.conn, ctx.cfg, case_id, link)
    locs = _locations(ctx, link, now) if ver["state"] != "missing" else []
    shas = sorted({x["evidence_sha256"] for x in locs if x.get("evidence_sha256")})
    if not locs and link["dataset_id"]:
        shas = [ctx.dataset(link["dataset_id"])["evidence_sha256"]]
    return {
        "link_id": link["link_id"],
        "link_type": link["link_type"],
        "dataset_id": link["dataset_id"],
        "run_id": link["run_id"],
        "ref_id": link["ref_id"],
        "active": link["active"],
        "snapshot_sha256": link["snapshot_sha256"],
        "snapshot_at_link": link["snapshot"],
        "current": now,
        "verification": ver["state"],
        "verification_reason": ver["reason"],
        "locations": locs,
        "evidence": [{"sha256": s, "verification": ctx.evidence_state(s)} for s in shas],
    }


def _traceability(references: list[dict[str, Any]]) -> str:
    if not references:
        return "no_reference"
    ok = all(
        r["verification"] == "current" and all(e["verification"] == "verified" for e in r["evidence"])
        for r in references
    )
    return "traceable" if ok else "unverified"


def _ref_text(link: dict[str, Any]) -> str:
    return f"{link['link_type']} {link['ref_id']}"


def _finding_text(ref: dict[str, Any]) -> str:
    s = ref["current"] or ref["snapshot_at_link"]
    lt = ref["link_type"]
    if lt == "risk_transaction":
        return (
            f"P9 review-risk row {ref['ref_id']}: score {s.get('score')} (0–100 review priority, not a "
            f"probability), tier {s.get('tier')}, {s.get('independent_sources')} independent source(s). "
            f"{s.get('headline') or ''}".strip()
        )
    if lt == "risk_entity":
        return (
            f"P9 counterparty {s.get('name') or ref['ref_id']}: score {s.get('score')} (review priority), "
            f"tier {s.get('tier')}."
        )
    if lt == "signal":
        rule = s.get("rule_id") or s.get("code")
        return f"Detector signal {ref['ref_id']} ({rule}, severity {s.get('severity')})."
    if lt == "graph_cycle":
        return (
            f"Relationship-graph cycle {ref['ref_id']} (length {s.get('length')}, status {s.get('status')})."
        )
    if lt == "graph_signal":
        return (
            f"Relationship-graph signal {ref['ref_id']} ({s.get('rule_id')}, severity {s.get('severity')})."
        )
    if lt == "reconciliation_finding":
        return f"Reconciliation finding {ref['ref_id']} ({s.get('rule_id')}, severity {s.get('severity')})."
    return f"{lt} {ref['ref_id']}."


def _record_text(ctx: _Ctx, ref: dict[str, Any]) -> str:
    s = ref["current"] or ref["snapshot_at_link"]
    lt = ref["link_type"]
    if lt == "transaction":
        amt = ctx.money(s.get("amount_minor"), s.get("currency"))
        return (
            f"Transaction {ref['ref_id']} posted {s.get('posted_at') or 'date unavailable'}, "
            f"{amt['text'] if amt else 'amount unavailable'}, payer {s.get('payer_raw') or 'unavailable'}, "
            f"payee {s.get('payee_raw') or 'unavailable'}."
        )
    if lt == "reconciliation_result":
        diff = s.get("amount_difference_minor")
        return (
            f"Reconciliation result {ref['ref_id']} in run {ref['run_id']}: status {s.get('status')}; ledger "
            f"{', '.join(s.get('ledger_txn_ids') or []) or 'none'}; "
            f"bank {', '.join(s.get('bank_txn_ids') or []) or 'none'}"
            + (f"; amount difference {diff} minor units" if diff not in (None, 0) else "")
            + "."
        )
    if lt == "duplicate_group":
        return (
            f"Duplicate candidate group {ref['ref_id']} ({s.get('side')}, {s.get('type')}): "
            f"{', '.join(s.get('txn_ids') or [])} — a candidate, nothing was excluded."
        )
    if lt == "reconciliation_run":
        return (
            f"Reconciliation run {ref['ref_id']} (ledger {s.get('ledger_dataset_id')}, "
            f"bank {s.get('bank_dataset_id')})."
        )
    if lt == "pdf_item":
        return (
            f"PDF extraction row {ref['ref_id']} on page {s.get('page')}: review state "
            f"{s.get('review_state')}; extracted text {s.get('text')!r}."
        )
    if lt == "pdf_page":
        return f"PDF page {ref['ref_id']} ({s.get('method')})."
    if lt == "evidence_file":
        return f"Evidence file {', '.join(s.get('names') or [])} (SHA-256 {ref['ref_id']})."
    if lt in ("entity", "entity_pair"):
        name = s.get("display_name") or s.get("name") or ref["ref_id"]
        status = s.get("resolution_status") or s.get("status")
        return f"Entity-resolution {lt.replace('_', ' ')} {ref['ref_id']} ({name}; status {status})."
    if lt in ("graph_node", "graph_edge"):
        return f"Relationship-graph {lt.split('_')[1]} {ref['ref_id']}."
    return f"{lt} {ref['ref_id']}."


# ------------------------------------------------------------------------------------------- case report


def _investigation(conn, inv_id: str) -> dict[str, Any]:
    return inv_service.get(conn, inv_id)


def case_report(ctx: _Ctx, inv_id: str) -> dict[str, Any]:
    conn = ctx.conn
    inv = _investigation(conn, inv_id)
    case = store.get_case(conn, inv["case_id"])
    links = inv_service.links_page(conn, inv_id, None, None, 1, ALL)["items"]
    decisions = inv_service.decisions_page(conn, inv_id, None, 1, ALL)["items"]
    notes = inv_service.notes_page(conn, inv_id, 1, ALL)["items"]
    activity = inv_service.activity_page(conn, inv_id, 1, model.MAX_ACTIVITY)
    by_link = {x["link_id"]: x for x in links}
    references = {x["link_id"]: _reference(ctx, inv["case_id"], x) for x in links}
    dec_by_link: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for d in decisions:
        dec_by_link[d["link_id"]].append(d)

    findings, records, removed = [], [], []
    for lk in links:
        ref = references[lk["link_id"]]
        if not lk["active"]:
            removed.append(
                {
                    "link_id": lk["link_id"],
                    "link_type": lk["link_type"],
                    "ref_id": lk["ref_id"],
                    "linked_reason": lk["reason"],
                    "removal": lk["removal"],
                }
            )
            continue
        limitations = []
        if ref["verification"] != "current":
            limitations.append(f"Record is {ref['verification']}: {ref['verification_reason']}")
        if any(e["verification"] != "verified" for e in ref["evidence"]):
            limitations.append("An evidence file behind this record does not verify (see evidence).")
        latest = (dec_by_link.get(lk["link_id"]) or [None])[-1]
        if lk["kind"] == model.FINDING_KIND:
            text, ctype, origin = _finding_text(ref), "automated_finding", "automated"
        elif lk["link_type"] == "pdf_item":
            text, ctype, origin = _record_text(ctx, ref), "extraction_value", "automated"
            state = (ref["current"] or ref["snapshot_at_link"]).get("review_state")
            if state not in TRUSTED_EXTRACTION:
                limitations.append(
                    f"Extraction row not confirmed by a reviewer (state {state}): its values are not "
                    "confirmed financial facts."
                )
        elif lk["link_type"] in ("reconciliation_result", "duplicate_group", "reconciliation_run"):
            text, ctype, origin = _record_text(ctx, ref), "reconciliation_result", "automated"
        else:
            text, ctype, origin = _record_text(ctx, ref), "linked_record", "automated"
        claim = {
            "claim_id": _claim_id(ctype, inv_id, lk["link_id"]),
            "claim_type": ctype,
            "origin": origin,
            "text": text,
            "link_reason": lk["reason"],
            "annotation": lk["annotation"],
            "references": [ref],
            "traceability": _traceability([ref]),
            "limitations": limitations,
            "analyst_status": lk["finding_status"],
            "latest_decision": None
            if latest is None
            else {k: latest[k] for k in ("decision_id", "action", "action_text", "unresolved", "created_at")},
        }
        (findings if ctype == "automated_finding" else records).append(claim)

    def link_refs(ids: list[str]) -> list[dict[str, Any]]:
        return [references[i] for i in ids if i in references]

    assessments = []
    for d in decisions:
        subject = by_link.get(d["link_id"]) if d["link_id"] else None
        subj_refs = [references[d["link_id"]]] if subject else []
        support = link_refs(d["supporting"])
        contra = link_refs(d["contradicting"])
        assessments.append(
            {
                "claim_id": _claim_id("analyst_decision", d["decision_id"]),
                "claim_type": "analyst_decision",
                "origin": "analyst",
                "decision_id": d["decision_id"],
                "subject": "case" if subject is None else _ref_text(subject),
                "action": d["action"],
                "action_text": d["action_text"],
                "text": d["rationale"],
                "unresolved": d["unresolved"],
                "payload": d["payload"],
                "created_by": d["created_by"],
                "created_at": d["created_at"],
                "references": subj_refs + support,
                "contradicting": contra,
                "traceability": _traceability(subj_refs + support),
                "limitations": (
                    ["Contradicting evidence was recorded with this decision (listed)."] if contra else []
                ),
            }
        )

    latest_by_subject: dict[Any, dict[str, Any]] = {}
    for d in decisions:
        latest_by_subject[d["link_id"]] = d
    unresolved = [
        {
            "subject": "case" if k is None else _ref_text(by_link[k]),
            "link_id": k,
            "decision_id": d["decision_id"],
            "action_text": d["action_text"],
            "rationale": d["rationale"],
        }
        for k, d in sorted(latest_by_subject.items(), key=lambda kv: (kv[0] or "", kv[1]["decision_id"]))
        if d["unresolved"]
    ]

    conclusion = None
    if inv["outcome"]:
        conclusion = {
            "claim_id": _claim_id("case_conclusion", inv_id, inv["outcome"], inv["conclusion"]),
            "claim_type": "case_conclusion",
            "origin": "analyst",
            "outcome": inv["outcome"],
            "text": inv["conclusion"],
            "closure_rationale": inv["closure_rationale"],
            "status_at_report": inv["status"],
            "note": "Recorded by the analyst in the investigation workflow; the report does not assess it.",
        }

    values = linked_values(ctx, [lk for lk in links if lk["active"]], references)
    datasets = sorted(
        {lk["dataset_id"] for lk in links if lk["dataset_id"]}
        | {
            ds
            for lk in links
            if lk["run_id"]
            for ds in conn.execute(
                "SELECT ledger_dataset_id, bank_dataset_id FROM reconciliation_runs WHERE run_id = ?",
                (lk["run_id"],),
            ).fetchone()
        }
    )
    runs = sorted({lk["run_id"] for lk in links if lk["run_id"]})
    stale_runs = recon.stale_run_ids(conn) if runs else set()
    quality = defaultdict(list)
    for lk in links:
        if lk["active"] and references[lk["link_id"]]["verification"] != "current":
            quality[references[lk["link_id"]]["verification"]].append(lk["link_id"])
    for c in findings + records:
        if c["claim_type"] == "extraction_value" and any("not confirmed" in x for x in c["limitations"]):
            quality["unconfirmed_extraction"].append(c["references"][0]["link_id"])
    return {
        "report_type": "case_investigation",
        "title": f"Case investigation report — {inv['title']}",
        "engagement": _engagement(case),
        "investigation": {
            k: inv[k]
            for k in (
                "investigation_id",
                "case_id",
                "title",
                "description",
                "priority",
                "status",
                "outcome",
                "conclusion",
                "closure_rationale",
                "created_by",
                "created_at",
                "updated_at",
            )
        },
        "scope": {
            "datasets": [ctx.dataset(d) for d in datasets],
            "reconciliation_runs": [{"run_id": r, "stale": r in stale_runs} for r in runs],
        },
        "automated_findings": findings,
        "linked_records": records,
        "analyst_assessments": assessments,
        "analyst_notes": [
            {
                "claim_id": _claim_id("analyst_note", n["note_id"]),
                "claim_type": "analyst_note",
                "origin": "analyst",
                "note_id": n["note_id"],
                "subject": "case" if n["link_id"] is None else _ref_text(by_link[n["link_id"]]),
                "text": n["body"],
                "created_by": n["created_by"],
                "created_at": n["created_at"],
            }
            for n in notes
        ],
        "unresolved": unresolved,
        "conclusion": conclusion,
        "conclusion_status": "recorded" if conclusion else "not_recorded",
        "linked_values": values,
        "removed_links": removed,
        "data_quality": {k: sorted(v) for k, v in sorted(quality.items())},
        "activity": {
            "total": activity["total"],
            "listed": len(activity["items"]),
            "truncated": activity["total"] > len(activity["items"]),
            "items": [
                {
                    k: e[k]
                    for k in (
                        "seq",
                        "event",
                        "actor",
                        "created_at",
                        "from_status",
                        "to_status",
                        "audit_seq",
                        "audit_hash",
                    )
                    if k in e
                }
                for e in activity["items"]
            ],
        },
        "limitations": _case_limitations(),
        "terminology": model.TERMINOLOGY,
    }


def _engagement(case: dict[str, Any] | None) -> dict[str, Any]:
    if case is None:
        return {"case_id": None, "note": "engagement case unavailable"}
    return {k: case.get(k) for k in ("case_id", "name", "client", "period_start", "period_end", "status")}


def _case_limitations() -> list[str]:
    return [
        "Automated findings are indicators requiring review; none is proof of fraud.",
        "Review-risk scores are 0–100 review priorities, not probabilities, and not amounts.",
        "Values are the stored records' values; a logged P4 transformation (e.g. an OCR correction) is shown "
        "as an override next to the original.",
        "Records are re-resolved when the report is built; records that changed, became stale or are missing "
        "since they were linked are marked and their claims are 'unverified'.",
        "Analyst rationales and notes are reproduced verbatim; the report does not verify their content.",
    ]


def linked_values(
    ctx: _Ctx, links: list[dict[str, Any]], references: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Distinct transactions referenced directly by transaction / risk-row links, with totals per dataset and
    currency. A transaction linked several times (e.g. as a transaction and as a risk row) counts once."""
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for lk in links:
        pair = _single_txn(lk)
        if not pair:
            continue
        row = seen.get(pair)
        if row is None:
            t = _txn(ctx, *pair)
            row = seen[pair] = {
                "dataset_id": pair[0],
                "txn_id": pair[1],
                "available": t is not None,
                "amount": t["amount"] if t else None,
                "posted_at": t["posted_at"] if t else None,
                "overrides": t["overrides"] if t else {},
                "location": t["location"] if t else None,
                "link_ids": [],
            }
        row["link_ids"].append(lk["link_id"])
    rows = [seen[k] for k in sorted(seen)]
    totals: dict[tuple[str, str], dict[str, int]] = {}
    unavailable = 0
    for r in rows:
        if r["amount"] is None:
            unavailable += 1
            continue
        key = (r["dataset_id"], r["amount"]["currency"] or "unknown")
        t = totals.setdefault(key, {"rows": 0, "minor": 0})
        t["rows"] += 1
        t["minor"] += r["amount"]["minor"]
    total_list = [
        {
            "dataset_id": ds,
            "currency": cur,
            "transactions": t["rows"],
            "gross_value": ctx.money(t["minor"], None if cur == "unknown" else cur),
        }
        for (ds, cur), t in sorted(totals.items())
    ]
    require(  # each linked transaction is counted exactly once (explicit, so it also holds under python -O)
        sum(t["transactions"] for t in total_list) + unavailable == len(rows),
        "report totals do not count every linked transaction exactly once",
    )
    return {
        "transactions": rows,
        "distinct_transactions": len(rows),
        "without_amount": unavailable,
        "totals": total_list,
        "currencies": sorted({cur for _, cur in totals}),
        "note": "Gross value of the linked transactions (each counted once), per dataset and currency. It is "
        "not a loss and not a confirmed exposure; currencies and datasets are never added together.",
    }


# ------------------------------------------------------------------------------------------- reconciliation


def reconciliation_report(ctx: _Ctx, run_id: str) -> dict[str, Any]:
    conn = ctx.conn
    rec = conn.execute("SELECT * FROM reconciliation_runs WHERE run_id = ?", (run_id,)).fetchone()
    if rec is None:
        raise _err("run_not_found", "reconciliation run not found", 404)
    case = store.get_case(conn, rec["case_id"])
    sources = {
        "ledger": ctx.dataset(rec["ledger_dataset_id"]),
        "bank": ctx.dataset(rec["bank_dataset_id"]),
    }
    recorded = json.loads(rec["summary"])
    base = {
        "report_type": "bank_reconciliation",
        "title": f"Bank reconciliation report — run {run_id}",
        "engagement": _engagement(case),
        "run": {
            "run_id": run_id,
            "result_sha256": rec["result_sha256"],
            "parameters": json.loads(rec["parameters"]),
            "recorded_by": rec["created_by"],
            "recorded_at": rec["created_at"],
        },
        "sources": sources,
        "terminology": model.TERMINOLOGY,
    }
    stale = run_id in recon.stale_run_ids(conn, rec["ledger_dataset_id"]) | recon.stale_run_ids(
        conn, rec["bank_dataset_id"]
    )
    if stale:
        return base | {
            "run_state": "stale",
            "run_state_reason": "The ledger or bank data changed after this run was recorded (a logged P4 "
            "transformation such as an OCR correction). Its results are superseded and are NOT presented as "
            "a current reconciliation; start a new run.",
            "recorded_summary_superseded": recorded,
            "results": None,
            "limitations": [
                "Superseded run: only the summary recorded at the time is shown, for the record."
            ],
            "recommendations": ["Start a new reconciliation run on the current data and report that run."],
        }
    try:
        run = recon.get_run(conn, ctx.cfg, run_id)
    except LedgerSpyError as exc:
        return base | {
            "run_state": "not_reproducible",
            "run_state_reason": exc.message,
            "recorded_summary_superseded": recorded,
            "results": None,
            "limitations": [
                "The run cannot be reproduced with the current configuration; nothing is shown as current."
            ],
            "recommendations": ["Start a new reconciliation run with the current configuration."],
        }
    return base | {"run_state": "current", "run_state_reason": None} | _run_content(ctx, conn, run)


def _row_out(ctx: _Ctx, row: dict[str, Any]) -> dict[str, Any]:
    out = {
        "side": row["side"],
        "txn_id": row["txn_id"],
        "source_row": row["source_row"],
        "date": row["date"],
        "amount": ctx.money(row["amount_minor"], row["currency"]),
        "account": row.get("account"),
        "reference": row.get("reference"),
        "counterparty": row.get("counterparty"),
    }
    if row.get("extraction"):
        out["extraction"] = row["extraction"]
    return out


def _run_content(ctx: _Ctx, conn, run) -> dict[str, Any]:
    s = run.summary
    # account × currency × status breakdown from the row-level results; it must add up to the engine's
    # per-currency totals (every row belongs to exactly one result, so nothing is double-counted).
    cells: dict[tuple[str, str, str, str], dict[str, int]] = {}
    no_amount = {"ledger": 0, "bank": 0}
    for r in run.results:
        account = r["scope"].get("account") or "unscoped"
        for side in ("ledger", "bank"):
            for row in r[side + "_rows"]:
                cur = row["currency"] or "unknown"
                c = cells.setdefault((side, account, cur, r["status"]), {"rows": 0, "minor": 0})
                c["rows"] += 1
                c["minor"] += row["amount_minor"] or 0
                if row["amount_minor"] is None:
                    no_amount[side] += 1
    check: dict[str, dict[str, dict[str, int]]] = {"ledger": {}, "bank": {}}
    for (side, _acc, cur, status), c in cells.items():
        check[side].setdefault(cur, {}).setdefault(status, 0)
        check[side][cur][status] += c["minor"]
    engine_totals = s["amounts_by_status_minor"]
    reconciled = all(
        {st: v for st, v in check[side].get(cur, {}).items() if v} == {st: v for st, v in by.items() if v}
        for side in ("ledger", "bank")
        for cur, by in engine_totals[side].items()
    ) and all(cur in engine_totals[side] for side in ("ledger", "bank") for cur in check[side])
    rows_ok = all(
        sum(c["rows"] for (sd, *_rest), c in cells.items() if sd == side) == s[side + "_rows"]
        for side in ("ledger", "bank")
    )
    if not (reconciled and rows_ok):  # the engine asserts the same invariant; this guards the report
        raise _err("totals_mismatch", "report totals do not reconcile with the run's results", 500)
    breakdown = [
        {
            "side": side,
            "account": acc,
            "currency": cur,
            "status": st,
            "rows": c["rows"],
            "value": ctx.money(c["minor"], None if cur == "unknown" else cur),
        }
        for (side, acc, cur, st), c in sorted(cells.items())
    ]
    order = {st: i for i, st in enumerate(recon_engine.STATUSES)}
    exceptions = sorted(
        (r for r in run.results if r["status"] not in recon_engine.MATCHED),
        key=lambda r: (order[r["status"]], r["first_date"] or "", r["result_id"]),
    )
    listed = exceptions[: model.MAX_LISTED_ROWS]
    unconfirmed = [r for r in run.results if "unconfirmed_extraction" in r["reasons"]]
    ocr_rows = sum(
        1 for r in run.results for b in r["bank_rows"] if (b.get("extraction") or {}).get("method") == "ocr"
    )
    groups = run.duplicates[: model.MAX_LISTED_ROWS]
    return {
        "scopes": run.scopes,
        "status_counts": {
            "results": s["results_by_status"],
            "rows": s["rows_by_status"],
            "ledger_rows": s["ledger_rows"],
            "bank_rows": s["bank_rows"],
            "matched_row_share": s["matched_row_share"],
            "matched_row_share_note": s["matched_row_share_note"],
        },
        "totals": {
            "breakdown": breakdown,
            "matched": {
                side: _money_map(ctx, s["matched_amount_minor"][side]) for side in ("ledger", "bank")
            },
            "partial": {
                side: _money_map(ctx, s["partial_amount_minor"][side]) for side in ("ledger", "bank")
            },
            "ambiguous": {
                side: _money_map(ctx, s["ambiguous_amount_minor"][side]) for side in ("ledger", "bank")
            },
            "unmatched": {
                side: _money_map(ctx, s["unmatched_amount_minor"][side]) for side in ("ledger", "bank")
            },
            "rows_without_amount": no_amount,
            "check": "The account × currency × status breakdown adds up to the run's per-currency totals, "
            "and "
            "every row is counted exactly once.",
            "note": "Unmatched and partial values are amounts that the matching could not pair; they are not "
            "losses. Currencies and accounts are never added together. Grouped matches count each row once.",
        },
        "exceptions": {
            "total": len(exceptions),
            "listed": len(listed),
            "truncated": len(exceptions) > len(listed),
            "items": [
                {
                    "result_id": r["result_id"],
                    "status": r["status"],
                    "account": r["scope"].get("account"),
                    "currency": r["scope"].get("currency"),
                    "first_date": r["first_date"],
                    "last_date": r["last_date"],
                    "ledger_rows": [_row_out(ctx, x) for x in r["ledger_rows"]],
                    "bank_rows": [_row_out(ctx, x) for x in r["bank_rows"]],
                    "amount_difference": ctx.money(r["amount_difference_minor"], r["scope"].get("currency")),
                    "date_difference_days": r["date_difference_days"],
                    "discrepancies": r["discrepancies"],
                    "candidates": len(r["candidates"]),
                    "reasons": r["reasons"],
                    "explanation": r["explanation"],
                    "limitations": r["limitations"],
                    "analyst_decisions": [
                        {
                            k: d[k]
                            for k in (
                                "decision_id",
                                "action",
                                "action_text",
                                "rationale",
                                "created_by",
                                "created_at",
                            )
                        }
                        for d in inv_service.decisions_on(conn, "reconciliation_result", r["result_id"])
                        if d["run_id"] == run.run_id
                    ],
                }
                for r in listed
            ],
        },
        "duplicate_candidates": {
            "total": len(run.duplicates),
            "listed": len(groups),
            "truncated": len(run.duplicates) > len(groups),
            "items": [
                {
                    "group_id": g["group_id"],
                    "side": g["side"],
                    "type": g["type"],
                    "txn_ids": g["txn_ids"],
                    "amount": ctx.money(g.get("amount_minor"), g.get("currency")),
                    "member_status": g.get("member_status"),
                }
                for g in groups
            ],
            "note": "Candidates only: nothing is excluded or deleted, and their rows are counted once, in "
            "their own result.",
        },
        "balance_check": s["balance_check"],
        "extraction": {
            "bank_rows_read_by_ocr": ocr_rows,
            "results_with_unconfirmed_extraction": len(unconfirmed),
            "note": "Unconfirmed extracted lines never enter matching; they appear as not reconciled. OCR "
            "confidence is not financial correctness.",
        },
        "findings": {
            "total": len(run.findings),
            "by_rule": s.get("findings", {}),
        },
        "truncation": run.truncation,
        "readiness": run.readiness,
        "limitations": run.limitations
        + [
            "Opening and closing balances are reported only where the statement supports a balance check "
            "(see balance_check); otherwise they are unavailable, not assumed.",
        ],
        "recommendations": _recon_recommendations(s, exceptions, unconfirmed),
    }


def _money_map(ctx: _Ctx, by_cur: dict[str, int]) -> list[dict[str, Any]]:
    return [ctx.money(v, None if cur == "unknown" else cur) for cur, v in sorted(by_cur.items())]


def _recon_recommendations(s, exceptions, unconfirmed) -> list[str]:
    out = []
    by = s["results_by_status"]
    if by.get("ambiguous"):
        out.append("Resolve the ambiguous results with documentary evidence (recorded as analyst decisions).")
    if by.get("unmatched_ledger") or by.get("unmatched_bank"):
        out.append("Investigate the unmatched items, oldest first, against supporting documents.")
    if by.get("duplicate_candidate"):
        out.append("Review the duplicate candidates; a candidate is not a confirmed duplicate payment.")
    if unconfirmed:
        out.append("Complete the review of the unconfirmed extracted lines, then start a new run.")
    if s["balance_check"]["status"] != "checked":
        out.append("Obtain statement balances where the balance check is unavailable.")
    return out


# ------------------------------------------------------------------------------------------- selection


def _investigations(conn, inv_ids: list[str]) -> list[dict[str, Any]]:
    if not inv_ids:
        raise _err("investigation_required", "select at least one investigation")
    if len(inv_ids) > model.MAX_INVESTIGATIONS:
        raise _err("too_many_investigations", f"at most {model.MAX_INVESTIGATIONS} investigations per report")
    if len(set(inv_ids)) != len(inv_ids):
        raise _err("duplicate_investigation", "an investigation is selected twice")
    invs = [_investigation(conn, i) for i in sorted(inv_ids)]
    if len({i["case_id"] for i in invs}) != 1:
        raise _err(
            "cross_scope_reference",
            "the selected investigations belong to different engagement cases; report one case at a time",
        )
    return invs


# ------------------------------------------------------------------------------------- evidence manifest


def evidence_manifest(ctx: _Ctx, inv_ids: list[str]) -> dict[str, Any]:
    conn = ctx.conn
    invs = _investigations(conn, inv_ids)
    case_id = invs[0]["case_id"]
    entries, files = [], defaultdict(set)
    for inv in invs:
        for lk in inv_service.links_page(conn, inv["investigation_id"], None, None, 1, ALL)["items"]:
            ref = _reference(ctx, case_id, lk)
            for e in ref["evidence"]:
                files[e["sha256"]].add(lk["link_id"])
            entries.append(
                {
                    "investigation_id": inv["investigation_id"],
                    "link_id": lk["link_id"],
                    "link_type": lk["link_type"],
                    "dataset_id": lk["dataset_id"],
                    "run_id": lk["run_id"],
                    "ref_id": lk["ref_id"],
                    "active": lk["active"],
                    "linked_by": lk["created_by"],
                    "linked_at": lk["created_at"],
                    "reason": lk["reason"],
                    "snapshot_sha256": lk["snapshot_sha256"],
                    "verification": ref["verification"],
                    "verification_reason": ref["verification_reason"],
                    "locations": ref["locations"],
                    "evidence_sha256": [e["sha256"] for e in ref["evidence"]],
                }
            )
    file_rows = []
    for sha in sorted(files):
        ev = conn.execute(
            "SELECT size_bytes, stored_at FROM evidence_files WHERE sha256 = ?", (sha,)
        ).fetchone()
        names = [
            r[0]
            for r in conn.execute(
                "SELECT original_filename FROM datasets WHERE evidence_sha256 = ? AND case_id = ? UNION "
                "SELECT original_filename FROM master_files WHERE evidence_sha256 = ? AND case_id = ? "
                "ORDER BY 1",
                (sha, case_id, sha, case_id),
            )
        ]
        file_rows.append(
            {
                "sha256": sha,
                "original_filenames": names,
                "size_bytes": ev["size_bytes"] if ev else None,
                "stored_at": ev["stored_at"] if ev else None,
                "verification": ctx.evidence_state(sha),
                "referenced_by": sorted(files[sha]),
            }
        )
    by_state = defaultdict(int)
    for e in entries:
        by_state[e["verification"]] += 1
    return {
        "report_type": "evidence_manifest",
        "title": "Evidence manifest",
        "engagement": _engagement(store.get_case(conn, case_id)),
        "investigations": [{k: i[k] for k in ("investigation_id", "title", "status")} for i in invs],
        "references": entries,
        "evidence_files": file_rows,
        "counts": {
            "references": len(entries),
            "active_references": sum(1 for e in entries if e["active"]),
            "by_verification": dict(sorted(by_state.items())),
            "evidence_files": len(file_rows),
            "evidence_not_verified": sum(1 for f in file_rows if f["verification"] != "verified"),
        },
        "note": "Evidence files are referenced by SHA-256 and re-hashed when the manifest is built; the "
        "files themselves are never copied or changed. A reference shows where a record lives, not that a "
        "claim about it is true.",
    }


# ------------------------------------------------------------------------------------------- audit memo

# A number is a digit run not glued to letters, digits, "_" or "-" (so IDs such as P9, IV-01J…, SHA-256 or a
# hex digest are identifiers, not numbers); thousands separators are ignored.
_NUM = re.compile(r"(?<![A-Za-z0-9_\-])\d+(?:[.,]\d+)*(?![A-Za-z0-9_])")


def _numbers(text: str) -> set[str]:
    return {m.replace(",", "") for m in _NUM.findall(text)}


def audit_memo(ctx: _Ctx, inv_ids: list[str]) -> dict[str, Any]:
    """A draft memo composed from the case reports with fixed templates. Every number it states is checked
    against the case reports it was built from (`numbers_check`)."""
    conn = ctx.conn
    invs = _investigations(conn, inv_ids)
    cases = [case_report(ctx, i["investigation_id"]) for i in invs]
    eng = cases[0]["engagement"]
    findings = [(c["investigation"]["investigation_id"], f) for c in cases for f in c["automated_findings"]]
    traceable = [(i, f) for i, f in findings if f["traceability"] == "traceable"]
    material = [(i, f) for i, f in traceable if f["analyst_status"] != "dismissed"]
    dismissed = [(i, f) for i, f in traceable if f["analyst_status"] == "dismissed"]
    excluded = [(i, f) for i, f in findings if f["traceability"] != "traceable"]
    status_order = {"confirmed": 0, "escalated": 1, "under_review": 2, "new": 3}
    material.sort(key=lambda x: (status_order.get(x[1]["analyst_status"], 9), x[0], x[1]["claim_id"]))

    # values across the selected investigations, each transaction once
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for c in cases:
        for t in c["linked_values"]["transactions"]:
            seen.setdefault((t["dataset_id"], t["txn_id"]), t)
    totals: dict[tuple[str, str], dict[str, int]] = {}
    for t in seen.values():
        if t["amount"] is None:
            continue
        k = (t["dataset_id"], t["amount"]["currency"] or "unknown")
        x = totals.setdefault(k, {"rows": 0, "minor": 0})
        x["rows"] += 1
        x["minor"] += t["amount"]["minor"]
    value_totals = [
        {
            "dataset_id": ds,
            "currency": cur,
            "transactions": x["rows"],
            "gross_value": ctx.money(x["minor"], None if cur == "unknown" else cur),
        }
        for (ds, cur), x in sorted(totals.items())
    ]
    value_lines = [
        f"{v['transactions']} distinct linked transaction(s) in dataset {v['dataset_id']}: gross value "
        f"{v['gross_value']['text']}."
        for v in value_totals
    ]
    relationship = [
        (c["investigation"]["investigation_id"], r)
        for c in cases
        for r in c["automated_findings"] + c["linked_records"]
        if r["references"][0]["link_type"]
        in ("risk_entity", "entity", "entity_pair", "graph_node", "graph_edge", "graph_cycle", "graph_signal")
    ]
    recon_items = [
        (c["investigation"]["investigation_id"], r)
        for c in cases
        for r in c["automated_findings"] + c["linked_records"]
        if r["references"][0]["link_type"]
        in ("reconciliation_run", "reconciliation_result", "reconciliation_finding", "duplicate_group")
    ]

    def matched(r: dict[str, Any]) -> bool:
        ref = r["references"][0]
        snap = ref["current"] or ref["snapshot_at_link"]
        return ref["link_type"] == "reconciliation_run" or (
            ref["link_type"] == "reconciliation_result" and snap.get("status") in recon_engine.MATCHED
        )

    recon_exceptions = [(i, r) for i, r in recon_items if not matched(r)]
    recon_context = [(i, r) for i, r in recon_items if matched(r)]
    assessments = [
        (c["investigation"]["investigation_id"], a) for c in cases for a in c["analyst_assessments"]
    ]
    unresolved = [(c["investigation"]["investigation_id"], u) for c in cases for u in c["unresolved"]]
    conclusions = [
        (c["investigation"]["investigation_id"], c["conclusion"]) for c in cases if c["conclusion"]
    ]
    statuses = defaultdict(int)
    for c in cases:
        statuses[c["investigation"]["status"]] += 1
    facts = {
        "investigations": len(cases),
        "investigations_by_status": dict(sorted(statuses.items())),
        "automated_findings": len(findings),
        "findings_traceable": len(traceable),
        "findings_not_reverified": len(excluded),
        "findings_dismissed": len(dismissed),
        "material_findings": len(material),
        "analyst_assessments": len(assessments),
        "open_questions": len(unresolved),
        "conclusions_recorded": len(conclusions),
        "linked_value_totals": value_totals,
        "note": "counts and totals derived by the memo from the case reports below",
    }
    status_text = ", ".join(f"{n} {st.replace('_', ' ')}" for st, n in sorted(statuses.items()))
    datasets = {d["dataset_id"]: d for c in cases for d in c["scope"]["datasets"]}
    runs = {r["run_id"]: r for c in cases for r in c["scope"]["reconciliation_runs"]}

    def _assessed(f: dict[str, Any]) -> str:
        if f["analyst_status"]:  # an automated finding: its finding status
            return f"Analyst status: {f['analyst_status'].replace('_', ' ')}."
        d = f.get("latest_decision")
        return f"Latest analyst decision: {d['action_text']}." if d else "No analyst decision recorded."

    def fline(inv_id: str, f: dict[str, Any]) -> dict[str, Any]:
        ref = f["references"][0]
        return {
            "text": f"[{inv_id}] {f['text']} {_assessed(f)}",
            "claim_id": f["claim_id"],
            "origin": f["origin"],
            "evidence": [e["sha256"] for e in ref["evidence"]],
            "locations": ref["locations"],
        }

    sections = [
        {
            "id": "scope",
            "title": "Engagement and investigation scope",
            "paragraphs": [
                f"Engagement case {eng.get('name') or eng.get('case_id')} (client "
                f"{eng.get('client') or 'not recorded'}, period {eng.get('period_start') or 'not recorded'} "
                f"to {eng.get('period_end') or 'not recorded'}).",
                f"Investigations covered: {len(cases)} ({status_text}).",
            ]
            + [
                f"{x['investigation_id']}: {x['title']} — status {x['status'].replace('_', ' ')}, "
                f"priority {x['priority']}."
                for x in (c["investigation"] for c in cases)
            ],
        },
        {
            "id": "executive_summary",
            "title": "Executive summary",
            "paragraphs": [
                f"The investigations link {len(findings)} automated finding(s) (indicators requiring review, "
                f"not proof of fraud). {len(traceable)} resolve to current records with verified evidence; "
                f"{len(excluded)} could not be re-verified and are excluded from the material findings.",
                f"Material findings (current and not dismissed): {len(material)}. "
                f"Analyst assessments recorded: {len(assessments)}. Findings dismissed by the analyst: "
                f"{len(dismissed)}. Open questions: {len(unresolved)}.",
                f"Conclusions recorded: {len(conclusions)} of {len(cases)} investigation(s)."
                if conclusions
                else "No investigation has a recorded conclusion; this memo states no conclusion.",
            ],
        },
        {
            "id": "methodology",
            "title": "Methodology and data sources",
            "paragraphs": [
                "Automated findings come from the LedgerSpy engines (P7 rules, P8 anomaly detection, P9 "
                "review-risk fusion, P12 entity resolution, P13 relationship graph, P14 reconciliation). "
                "Each was re-resolved when this memo was built and compared with the hash recorded when it "
                "was linked.",
                "Analyst assessments, notes and conclusions are reproduced from the investigation record.",
            ],
            "items": [
                f"Dataset {d['dataset_id']}: {d['original_filename']} ({d['kind']}, {d['format']}, "
                f"{d['row_count']} rows{', flagged synthetic' if d['synthetic'] else ''}); evidence SHA-256 "
                f"{d['evidence_sha256']} — {d['evidence_verification']}."
                for d in sorted(datasets.values(), key=lambda d: d["dataset_id"])
            ]
            + [
                f"Reconciliation run {r}: "
                + ("STALE — inputs changed after the run." if v["stale"] else "current.")
                for r, v in sorted(runs.items())
            ],
        },
        {
            "id": "material_findings",
            "title": "Material findings and evidence",
            "paragraphs": [] if material else ["No current, undismissed automated finding is linked."],
            "findings": [fline(i, f) for i, f in material],
        },
        {
            "id": "amounts",
            "title": "Amounts and transaction counts",
            "paragraphs": (value_lines or ["No transaction with an amount is linked directly."])
            + [model.TERMINOLOGY["gross_value"]],
        },
        {
            "id": "relationships",
            "title": "Counterparty and relationship indicators",
            "paragraphs": [] if relationship else ["None linked."],
            "findings": [fline(i, f) for i, f in relationship],
        },
        {
            "id": "reconciliation",
            "title": "Reconciliation exceptions",
            "paragraphs": ([] if recon_exceptions else ["No reconciliation exception is linked."])
            + (
                [
                    "Matched results and runs linked for context (not exceptions) are listed below the "
                    "exceptions."
                ]
                if recon_context
                else []
            ),
            "findings": [fline(i, f) for i, f in recon_exceptions] + [fline(i, f) for i, f in recon_context],
        },
        {
            "id": "evidence_balance",
            "title": "Supporting and contradictory evidence",
            "paragraphs": [] if assessments else ["No analyst assessment recorded."],
            "items": [
                f"[{i}] {a['action_text']} on {a['subject']}: {a['text']} Supporting: "
                f"{_ref_list(a['references'][1 if a['subject'] != 'case' else 0 :])}. "
                f"Contradicting: {_ref_list(a['contradicting'])}."
                for i, a in assessments
            ],
        },
        {
            "id": "limitations",
            "title": "Limitations, assumptions and open questions",
            "paragraphs": _case_limitations(),
            "items": [
                f"[{i}] Open: {u['subject']} — {u['action_text']}: {u['rationale']}" for i, u in unresolved
            ]
            + [
                f"[{i}] Not re-verified ({f['references'][0]['verification']}): {f['text']}"
                for i, f in excluded
            ],
        },
        {
            "id": "decisions_and_conclusion",
            "title": "Analyst decisions and conclusion",
            "paragraphs": [
                f"[{i}] Recorded conclusion ({c['outcome'].replace('_', ' ')}): {c['text']}"
                for i, c in conclusions
            ]
            or ["No conclusion has been recorded. This draft does not draw one."],
            "items": [f"[{i}] Dismissed by the analyst: {f['text']}" for i, f in dismissed],
        },
        {
            "id": "recommendations",
            "title": "Recommended next steps (recommendations, not findings)",
            "paragraphs": [model.RECOMMENDATION_NOTICE],
            "items": _memo_recommendations(cases, excluded, unresolved, runs),
        },
    ]
    memo = {
        "report_type": "audit_memo",
        "title": f"Audit memo (draft) — {eng.get('name') or eng.get('case_id')}",
        "status": "draft",
        "approval": {"status": "not_reviewed", "supported": False, "note": model.DRAFT_NOTICE},
        "engagement": eng,
        "investigation_ids": [c["investigation"]["investigation_id"] for c in cases],
        "facts": facts,
        "sections": sections,
        "case_reports": cases,
    }
    memo["numbers_check"] = check_numbers(memo)
    return memo


def _ref_list(refs: list[dict[str, Any]]) -> str:
    return ", ".join(r["link_type"] + " " + r["ref_id"] for r in refs) or "none recorded"


def _memo_recommendations(cases, excluded, unresolved, runs) -> list[str]:
    out = []
    for c in cases:
        inv = c["investigation"]
        if inv["status"] == "awaiting_evidence":
            out.append(f"{inv['investigation_id']}: obtain the evidence the investigation is waiting for.")
        if not c["conclusion"] and inv["status"] in ("open", "in_review", "awaiting_evidence"):
            out.append(
                f"{inv['investigation_id']}: record a conclusion (or 'inconclusive') when the review ends."
            )
        for f in c["automated_findings"]:
            if f["analyst_status"] == "new":
                out.append(
                    f"{inv['investigation_id']}: assess the unreviewed finding "
                    f"{f['references'][0]['ref_id']}."
                )
    for i, u in unresolved:
        subject = "the investigation as a whole" if u["subject"] == "case" else u["subject"]
        out.append(f"{i}: resolve the open question on {subject}.")
    for _i, f in excluded:
        r = f["references"][0]
        out.append(f"Re-check {r['link_type']} {r['ref_id']} ({r['verification']}) before relying on it.")
    for r, v in sorted(runs.items()):
        if v["stale"]:
            out.append(f"Reconciliation run {r} is stale: start a new run and link its results.")
    return out


def memo_texts(memo: dict[str, Any]) -> list[str]:
    out = [memo["title"]]
    for s in memo["sections"]:
        out.append(s["title"])
        out += s.get("paragraphs", []) + s.get("items", [])
        out += [f["text"] for f in s.get("findings", [])]
    return out


def check_numbers(memo: dict[str, Any]) -> dict[str, Any]:
    """Every number in the memo's narrative must occur in its report model: the case reports (stored records
    only), the engagement, or the memo's own `facts` (counts and totals derived from those case reports)."""
    known = _numbers(
        " ".join(canonical_json(memo[k]).decode() for k in ("case_reports", "engagement", "facts"))
    )
    stated = set().union(*(_numbers(t) for t in memo_texts(memo)))
    missing = sorted(stated - known)
    if missing:
        raise _err(
            "memo_number_unsupported",
            "the memo states numbers not found in its sources",
            500,
            numbers=missing,
        )
    return {
        "numbers_stated": len(stated),
        "unsupported": [],
        "rule": "every number in the memo text occurs in the case reports, the engagement or the memo facts",
    }


# ------------------------------------------------------------------------------------------- dispatch


def build(conn, cfg: EffectiveConfig, workspace, report_type: str, params: dict[str, Any]) -> dict[str, Any]:
    if report_type not in model.REPORT_TYPES:
        raise _err("invalid_report_type", "unknown report type", allowed=sorted(model.REPORT_TYPES))
    ctx = _Ctx(conn, cfg, workspace)
    inv_ids = params.get("investigation_ids") or []
    if report_type == "bank_reconciliation":
        if not params.get("run_id") or inv_ids:
            raise _err("invalid_parameters", "a reconciliation report takes run_id only")
        content = reconciliation_report(ctx, params["run_id"])
    else:
        if params.get("run_id"):
            raise _err("invalid_parameters", f"a {report_type} report takes investigation_ids only")
        if report_type == "case_investigation":
            if len(inv_ids) != 1:
                raise _err(
                    "invalid_parameters", "a case investigation report covers exactly one investigation"
                )
            content = case_report(ctx, inv_ids[0])
        elif report_type == "evidence_manifest":
            content = evidence_manifest(ctx, inv_ids)
        else:
            content = audit_memo(ctx, inv_ids)
    content["format_version"] = model.FORMAT_VERSION
    content["schema_version"] = SCHEMA_VERSION
    content["config_sha256"] = cfg.sha256
    content["provenance"] = provenance(content)
    content["content_sha256"] = sha256_hex(canonical_json(content))
    return content


def provenance(content: dict[str, Any]) -> dict[str, Any]:
    """Selected cases, source record IDs and evidence hashes, collected from the content itself."""
    text = canonical_json(content).decode()
    shas = sorted(set(re.findall(r'"(?:evidence_sha256|sha256)":"([0-9a-f]{64})"', text)))
    return {
        "case_id": (content.get("engagement") or {}).get("case_id"),
        "investigation_ids": sorted(set(re.findall(r'"investigation_id":"(IV-[0-9A-Z]{26})"', text))),
        "run_ids": sorted(set(re.findall(r'"run_id":"(RR-[0-9a-f]{16})"', text))),
        "dataset_ids": sorted(set(re.findall(r'"dataset_id":"(D-[0-9A-Z]{26})"', text))),
        "link_ids": sorted(set(re.findall(r'"link_id":"(LK-[0-9A-Z]{26})"', text))),
        "evidence_sha256": shas,
    }
