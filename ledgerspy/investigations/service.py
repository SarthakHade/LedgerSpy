"""Investigation cases: lifecycle, evidence links, notes, decisions, activity, summary and evidence manifest.

Every write runs in one transaction with its audit-log entry and an `investigation_events` row that points at
that entry (`audit_seq`). Links, removals, notes, decisions and events are append-only (database triggers);
only an investigation's own status / texts change, and each change is an event. Automated outputs (P7–P14
findings, scores, matches, extractions) are referenced and assessed, never modified. The actor is the local
session identity the server runs as (`app.state.actor`, the OS user); no other identity is invented.
"""

import json
from typing import Any

from ledgerspy.canonical.ids import new_ulid
from ledgerspy.cases import audit, evidence
from ledgerspy.cases.db import transaction
from ledgerspy.config import EffectiveConfig
from ledgerspy.errors import LedgerSpyError
from ledgerspy.hashing import canonical_json, sha256_hex, utc_now_iso
from ledgerspy.investigations import model, refs

MAX_TEXT = 10_000
NOTE = (
    "Automated findings are indicators that require review, not proof of fraud. Analyst decisions are "
    "recorded assessments; they never change automated outputs, P9 scores, detectors, models or source data."
)


def _err(code: str, message: str, status: int = 422, **details: Any) -> LedgerSpyError:
    return LedgerSpyError(code, message, details, http_status=status)


def _text(value: str | None, name: str, minimum: int = 0, maximum: int = MAX_TEXT) -> str | None:
    if value is None:
        if minimum:
            raise _err(
                "rationale_required" if name in ("rationale", "reason") else "invalid_request",
                f"{name} is required",
            )
        return None
    v = value.strip()
    if len(v) < minimum:
        raise _err(
            "rationale_required" if name in ("rationale", "reason") else "invalid_request",
            f"{name} must have at least {minimum} characters",
        )
    if len(v) > maximum:
        raise _err("invalid_request", f"{name} must have at most {maximum} characters")
    return v


def _inv(conn, inv_id: str):
    row = conn.execute("SELECT * FROM investigations WHERE investigation_id = ?", (inv_id,)).fetchone()
    if row is None:
        raise _err("investigation_not_found", "investigation not found", 404)
    return row


def _writable(row) -> None:
    if row["status"] == "closed":
        raise _err(
            "investigation_closed",
            "the investigation is closed: re-open it (closed → in_review, with a reason) before changing it",
            409,
        )


def _event(
    conn,
    inv_id: str,
    event: str,
    actor: str,
    details: dict[str, Any],
    *,
    from_status=None,
    to_status=None,
    subject_id=None,
) -> dict[str, Any]:
    a = audit.append(
        conn,
        actor,
        f"investigation.{event}",
        f"investigation:{inv_id}",
        details | {"investigation_id": inv_id},
    )
    conn.execute(
        "INSERT INTO investigation_events (investigation_id, event, from_status, to_status, subject_id, "
        "details, audit_seq, actor, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            inv_id,
            event,
            from_status,
            to_status,
            subject_id,
            canonical_json(details).decode(),
            a.seq,
            actor,
            a.ts,
        ),
    )
    conn.execute(
        "UPDATE investigations SET updated_by = ?, updated_at = ? WHERE investigation_id = ?",
        (actor, a.ts, inv_id),
    )
    return {"audit_seq": a.seq, "ts": a.ts}


def _row(row) -> dict[str, Any]:
    d = dict(row)
    d["allowed_transitions"] = list(model.TRANSITIONS[d["status"]])
    return d


# ------------------------------------------------------------------------------------------- cases


def create(
    conn,
    cfg: EffectiveConfig,
    case_id: str,
    title: str,
    description: str | None,
    priority: str,
    actor: str,
    links: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if conn.execute("SELECT 1 FROM cases WHERE case_id = ?", (case_id,)).fetchone() is None:
        raise _err("case_not_found", "engagement case not found", 404)
    if priority not in model.PRIORITIES:
        raise _err("invalid_request", f"priority must be one of {list(model.PRIORITIES)}")
    title = _text(title, "title", 1, 200)
    description = _text(description or "", "description", 0) or ""
    resolved = [_prepare_link(conn, cfg, case_id, spec) for spec in (links or [])]
    keys = [r["scope_key"] for r, _ in resolved]
    if len(set(keys)) != len(keys):
        raise _err("duplicate_link", "the same record is listed twice")
    inv_id = "IV-" + new_ulid()
    now = utc_now_iso()
    with transaction(conn):
        conn.execute(
            "INSERT INTO investigations (investigation_id, case_id, title, description, priority, status, "
            "created_by, created_at, updated_by, updated_at) VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)",
            (inv_id, case_id, title, description, priority, actor, now, actor, now),
        )
        _event(
            conn,
            inv_id,
            "created",
            actor,
            {"case_id": case_id, "title": title, "priority": priority},
            to_status="open",
        )
        for r, spec in resolved:
            _insert_link(conn, inv_id, r, spec, actor)
    return get(conn, inv_id)


def get(conn, inv_id: str) -> dict[str, Any]:
    row = _inv(conn, inv_id)
    counts = {
        "active_links": conn.execute(
            "SELECT COUNT(*) FROM investigation_links l WHERE investigation_id = ? AND NOT EXISTS "
            "(SELECT 1 FROM investigation_link_removals r WHERE r.link_id = l.link_id)",
            (inv_id,),
        ).fetchone()[0],
        "notes": conn.execute(
            "SELECT COUNT(*) FROM investigation_notes WHERE investigation_id = ?", (inv_id,)
        ).fetchone()[0],
        "decisions": conn.execute(
            "SELECT COUNT(*) FROM investigation_decisions WHERE investigation_id = ?", (inv_id,)
        ).fetchone()[0],
        "events": conn.execute(
            "SELECT COUNT(*) FROM investigation_events WHERE investigation_id = ?", (inv_id,)
        ).fetchone()[0],
    }
    return _row(row) | {"counts": counts, "note": NOTE}


def list_page(
    conn, case_id: str | None, status: str | None, priority: str | None, page: int, size: int
) -> dict[str, Any]:
    where, args = [], []
    for col, val in (("case_id", case_id), ("status", status), ("priority", priority)):
        if val:
            where.append(f"{col} = ?")
            args.append(val)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute("SELECT COUNT(*) FROM investigations" + clause, args).fetchone()[0]  # noqa: S608
    rows = conn.execute(
        "SELECT * FROM investigations"  # noqa: S608 — fixed fragments, values bound
        + clause
        + " ORDER BY created_at DESC, investigation_id LIMIT ? OFFSET ?",  # noqa: S608
        [*args, size, (page - 1) * size],
    ).fetchall()
    return {"page": page, "size": size, "total": total, "items": [_row(r) for r in rows]}


def update(conn, inv_id: str, changes: dict[str, Any], actor: str) -> dict[str, Any]:
    row = _inv(conn, inv_id)
    _writable(row)
    new: dict[str, Any] = {}
    if changes.get("title") is not None:
        new["title"] = _text(changes["title"], "title", 1, 200)
    if changes.get("description") is not None:
        new["description"] = _text(changes["description"], "description", 0)
    if changes.get("priority") is not None:
        if changes["priority"] not in model.PRIORITIES:
            raise _err("invalid_request", f"priority must be one of {list(model.PRIORITIES)}")
        new["priority"] = changes["priority"]
    new = {k: v for k, v in new.items() if v != row[k]}
    if not new:
        raise _err("no_change", "nothing to change")
    with transaction(conn):
        for k, v in new.items():
            conn.execute(f"UPDATE investigations SET {k} = ? WHERE investigation_id = ?", (v, inv_id))  # noqa: S608 — allow-listed column
        _event(conn, inv_id, "updated", actor, {"before": {k: row[k] for k in new}, "after": new})
    return get(conn, inv_id)


def transition(
    conn,
    inv_id: str,
    to_status: str,
    actor: str,
    reason: str | None = None,
    outcome: str | None = None,
    conclusion: str | None = None,
) -> dict[str, Any]:
    row = _inv(conn, inv_id)
    frm = row["status"]
    if to_status not in model.STATUSES:
        raise _err("invalid_request", f"status must be one of {list(model.STATUSES)}")
    if to_status not in model.TRANSITIONS[frm]:
        raise _err(
            "invalid_transition",
            f"{frm} → {to_status} is not a valid transition",
            409,
            allowed=list(model.TRANSITIONS[frm]),
        )
    sets: dict[str, Any] = {"status": to_status}
    details: dict[str, Any] = {}
    if to_status == "concluded":
        if outcome not in model.OUTCOMES:
            raise _err("outcome_required", f"concluding needs an outcome: one of {list(model.OUTCOMES)}")
        sets["outcome"] = outcome
        sets["conclusion"] = _text(conclusion, "rationale", 10)
        details |= {"outcome": outcome, "conclusion": sets["conclusion"]}
    elif outcome is not None or conclusion is not None:
        raise _err("invalid_request", "an outcome and conclusion are given only when concluding")
    if (frm, to_status) in model.NEEDS_RATIONALE or to_status == "closed":
        reason = _text(reason, "reason", 10)
    else:
        reason = _text(reason, "reason", 0)
    if to_status == "closed":
        sets["closure_rationale"] = reason
    if reason:
        details["reason"] = reason
    with transaction(conn):
        for k, v in sets.items():
            conn.execute(f"UPDATE investigations SET {k} = ? WHERE investigation_id = ?", (v, inv_id))  # noqa: S608 — fixed columns
        _event(
            conn,
            inv_id,
            "status_changed",
            actor,
            details | {"from": frm, "to": to_status},
            from_status=frm,
            to_status=to_status,
        )
    return get(conn, inv_id)


# ------------------------------------------------------------------------------------------- links


def _prepare_link(conn, cfg, case_id: str, spec: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    r = refs.resolve(
        conn,
        cfg,
        case_id,
        spec.get("link_type"),
        spec.get("dataset_id"),
        spec.get("run_id"),
        spec.get("ref_id"),
    )
    spec = dict(spec)
    spec["reason"] = _text(spec.get("reason"), "reason", 3, 2000)
    spec["annotation"] = _text(spec.get("annotation"), "annotation", 0, 2000)
    return r, spec


def _active_link(conn, inv_id: str, scope_key: str):
    return conn.execute(
        "SELECT link_id FROM investigation_links l WHERE investigation_id = ? AND scope_key = ? AND NOT "
        "EXISTS (SELECT 1 FROM investigation_link_removals r WHERE r.link_id = l.link_id)",
        (inv_id, scope_key),
    ).fetchone()


def _insert_link(conn, inv_id: str, r: dict[str, Any], spec: dict[str, Any], actor: str) -> str:
    if _active_link(conn, inv_id, r["scope_key"]) is not None:
        raise _err("duplicate_link", "this record is already linked to the investigation", 409)
    link_id = "LK-" + new_ulid()
    conn.execute(
        "INSERT INTO investigation_links VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            link_id,
            inv_id,
            spec["link_type"],
            r["dataset_id"],
            r["run_id"],
            r["ref_id"],
            r["scope_key"],
            canonical_json(r["snapshot"]).decode(),
            r["snapshot_sha256"],
            spec["reason"],
            spec.get("annotation"),
            actor,
            utc_now_iso(),
        ),
    )
    _event(
        conn,
        inv_id,
        "linked",
        actor,
        {
            "link_id": link_id,
            "link_type": spec["link_type"],
            "dataset_id": r["dataset_id"],
            "run_id": r["run_id"],
            "ref_id": r["ref_id"],
            "snapshot_sha256": r["snapshot_sha256"],
            "reason": spec["reason"],
        },
        subject_id=link_id,
    )
    return link_id


def link(conn, cfg: EffectiveConfig, inv_id: str, spec: dict[str, Any], actor: str) -> dict[str, Any]:
    row = _inv(conn, inv_id)
    _writable(row)
    r, spec = _prepare_link(conn, cfg, row["case_id"], spec)
    with transaction(conn):
        link_id = _insert_link(conn, inv_id, r, spec, actor)
    return link_detail(conn, cfg, inv_id, link_id, verify=False)


def unlink(conn, inv_id: str, link_id: str, reason: str, actor: str) -> dict[str, Any]:
    row = _inv(conn, inv_id)
    _writable(row)
    lk = _link_row(conn, inv_id, link_id)
    if conn.execute("SELECT 1 FROM investigation_link_removals WHERE link_id = ?", (link_id,)).fetchone():
        raise _err("already_unlinked", "this link was already removed", 409)
    reason = _text(reason, "reason", 3, 2000)
    with transaction(conn):
        conn.execute(
            "INSERT INTO investigation_link_removals VALUES (?, ?, ?, ?)",
            (link_id, reason, actor, utc_now_iso()),
        )
        _event(
            conn,
            inv_id,
            "unlinked",
            actor,
            {"link_id": link_id, "link_type": lk["link_type"], "ref_id": lk["ref_id"], "reason": reason},
            subject_id=link_id,
        )
    return {
        "link_id": link_id,
        "removed": True,
        "note": "only the case link was removed; the referenced record and its evidence are unchanged",
    }


def _link_row(conn, inv_id: str, link_id: str):
    r = conn.execute(
        "SELECT * FROM investigation_links WHERE link_id = ? AND investigation_id = ?", (link_id, inv_id)
    ).fetchone()
    if r is None:
        raise _err("link_not_found", "link not found in this investigation", 404)
    return r


def _link_dict(conn, r) -> dict[str, Any]:
    d = dict(r)
    d["snapshot"] = json.loads(d["snapshot"])
    rem = conn.execute(
        "SELECT reason, removed_by, removed_at FROM investigation_link_removals WHERE link_id = ?",
        (r["link_id"],),
    ).fetchone()
    d["active"] = rem is None
    d["removal"] = None if rem is None else dict(rem)
    d["kind"] = model.LINK_TYPES[r["link_type"]]["kind"]
    last = conn.execute(
        "SELECT action, created_at FROM investigation_decisions WHERE link_id = ? ORDER BY created_at DESC, "
        "decision_id DESC LIMIT 1",
        (r["link_id"],),
    ).fetchone()
    d["latest_decision"] = None if last is None else dict(last)
    d["finding_status"] = None
    if d["kind"] == "finding":
        d["finding_status"] = (
            "new" if last is None else model.FINDING_STATUS.get(last["action"], "under_review")
        )
    d["allowed_actions"] = list(model.ACTIONS_BY_KIND[d["kind"]])
    return d


def links_page(
    conn, inv_id: str, active: bool | None, link_type: str | None, page: int, size: int
) -> dict[str, Any]:
    _inv(conn, inv_id)
    rows = conn.execute(
        "SELECT * FROM investigation_links WHERE investigation_id = ? ORDER BY created_at, link_id", (inv_id,)
    ).fetchall()
    items = [_link_dict(conn, r) for r in rows]
    if active is not None:
        items = [i for i in items if i["active"] == active]
    if link_type:
        items = [i for i in items if i["link_type"] == link_type]
    return {"page": page, "size": size, "total": len(items), "items": items[(page - 1) * size : page * size]}


def link_detail(conn, cfg: EffectiveConfig, inv_id: str, link_id: str, verify: bool = True) -> dict[str, Any]:
    row = _inv(conn, inv_id)
    d = _link_dict(conn, _link_row(conn, inv_id, link_id))
    d["decisions"] = [
        _decision_dict(x)
        for x in conn.execute(
            "SELECT * FROM investigation_decisions WHERE link_id = ? ORDER BY created_at, decision_id",
            (link_id,),
        )
    ]
    d["notes"] = [
        dict(x)
        for x in conn.execute(
            "SELECT * FROM investigation_notes WHERE link_id = ? ORDER BY created_at, note_id", (link_id,)
        )
    ]
    if verify:
        d["verification"] = refs.verify(conn, cfg, row["case_id"], dict(_link_row(conn, inv_id, link_id)))
    return d


# ------------------------------------------------------------------------------------------- notes


def add_note(conn, inv_id: str, body: str, link_id: str | None, actor: str) -> dict[str, Any]:
    row = _inv(conn, inv_id)
    _writable(row)
    body = _text(body, "body", 1, MAX_TEXT)
    if link_id is not None:
        _link_row(conn, inv_id, link_id)
    note_id = "NT-" + new_ulid()
    with transaction(conn):
        conn.execute(
            "INSERT INTO investigation_notes VALUES (?, ?, ?, ?, ?, ?)",
            (note_id, inv_id, link_id, body, actor, utc_now_iso()),
        )
        _event(
            conn,
            inv_id,
            "note_added",
            actor,
            {"note_id": note_id, "link_id": link_id, "body_sha256": sha256_hex(body)},
            subject_id=link_id,
        )
    return dict(conn.execute("SELECT * FROM investigation_notes WHERE note_id = ?", (note_id,)).fetchone())


def notes_page(conn, inv_id: str, page: int, size: int) -> dict[str, Any]:
    _inv(conn, inv_id)
    total = conn.execute(
        "SELECT COUNT(*) FROM investigation_notes WHERE investigation_id = ?", (inv_id,)
    ).fetchone()[0]
    rows = conn.execute(
        "SELECT * FROM investigation_notes WHERE investigation_id = ? ORDER BY created_at, note_id "
        "LIMIT ? OFFSET ?",
        (inv_id, size, (page - 1) * size),
    ).fetchall()
    return {"page": page, "size": size, "total": total, "items": [dict(r) for r in rows]}


# ------------------------------------------------------------------------------------------- decisions


def _decision_dict(r) -> dict[str, Any]:
    d = dict(r)
    for k in ("payload", "supporting", "contradicting"):
        d[k] = json.loads(d[k])
    d["unresolved"] = bool(d["unresolved"])
    d["action_text"] = model.ACTION_TEXT.get(d["action"], d["action"])
    return d


def _check_payload(conn, cfg, row, lk, action: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    """Action-specific validation (and, for pre-confirmation OCR review, the P14b decision it implies)."""
    if action in ("match_accepted", "match_rejected", "ambiguity_resolved"):
        r = refs._run(conn, cfg, lk["run_id"]).by_id[lk["ref_id"]]
        if action == "ambiguity_resolved":
            if r["status"] != "ambiguous":
                raise _err("not_ambiguous", "only an ambiguous reconciliation result can be resolved")
            chosen = payload.get("chosen")
            options = []
            for c in r["candidates"]:
                if c.get("type") == "pair":
                    options.append(
                        {"ledger_txn_ids": [c["ledger_txn_id"]], "bank_txn_ids": [c["bank_txn_id"]]}
                    )
                else:
                    side_s = "bank" if c["settlement"] in r["bank_txn_ids"] else "ledger"
                    other = "ledger" if side_s == "bank" else "bank"
                    options.append(
                        {f"{side_s}_txn_ids": [c["settlement"]], f"{other}_txn_ids": sorted(c["components"])}
                    )
            norm = (
                None
                if not isinstance(chosen, dict)
                else {
                    "ledger_txn_ids": sorted(chosen.get("ledger_txn_ids") or []),
                    "bank_txn_ids": sorted(chosen.get("bank_txn_ids") or []),
                }
            )
            if norm not in [{k: sorted(v) for k, v in o.items()} for o in options]:
                raise _err(
                    "invalid_resolution",
                    "`payload.chosen` must be one of the result's competing candidates",
                    candidates=options,
                )
            return {
                "chosen": norm,
                "result_status": r["status"],
                "note": "recorded resolution: the reconciliation run itself is unchanged",
            }
        if r["status"] not in ("exact_match", "probable_match", "grouped_match", "partial_match"):
            raise _err("not_a_match", f"a {r['status']} result is not a match to accept or reject")
        return {"result_status": r["status"]}
    if action in ("ocr_row_confirmed", "ocr_row_excluded"):
        from ledgerspy.pdfstatements import service as pdf_service

        ds = conn.execute("SELECT status FROM datasets WHERE dataset_id = ?", (lk["dataset_id"],)).fetchone()
        item = pdf_service.item_detail(conn, lk["dataset_id"], lk["ref_id"])
        if ds["status"] == "canonicalised":
            if action == "ocr_row_excluded":
                raise _err(
                    "already_confirmed",
                    "the statement is confirmed: a row can no longer be excluded; record "
                    "a finding assessment or correct its value instead",
                    409,
                )
            return {"stage": "after_confirmation", "p14b_decision": None}
        if item["item"].get("review_required"):
            pdf_service.review(
                conn,
                lk["dataset_id"],
                [
                    {
                        "item_id": lk["ref_id"],
                        "decision": "confirm" if action == "ocr_row_confirmed" else "exclude",
                        "reason": "investigation decision",
                    }
                ],
                actor,
            )
            return {"stage": "before_confirmation", "p14b_decision": action.split("_")[-1]}
        return {
            "stage": "before_confirmation",
            "p14b_decision": None,
            "note": "the row did not require review",
        }
    if action == "ocr_row_corrected":
        cid = payload.get("correction_id")
        ok = (
            cid
            and conn.execute(
                "SELECT 1 FROM ocr_corrections WHERE correction_id = ? AND item_id = ?", (cid, lk["ref_id"])
            ).fetchone()
        )
        if not ok:
            raise _err(
                "invalid_request",
                "ocr_row_corrected needs payload.correction_id of a correction of this row "
                "(use the OCR correction endpoint)",
            )
        return {"correction_id": cid}
    return {}


def decide(
    conn,
    cfg: EffectiveConfig,
    inv_id: str,
    link_id: str | None,
    action: str,
    rationale: str,
    actor: str,
    payload: dict[str, Any] | None = None,
    supporting: list[str] | None = None,
    contradicting: list[str] | None = None,
    unresolved: bool | None = None,
) -> dict[str, Any]:
    row = _inv(conn, inv_id)
    _writable(row)
    lk = None
    kind = "case"
    if link_id is not None:
        lk = _link_row(conn, inv_id, link_id)
        if conn.execute("SELECT 1 FROM investigation_link_removals WHERE link_id = ?", (link_id,)).fetchone():
            raise _err("link_removed", "this link was removed from the investigation", 409)
        kind = model.LINK_TYPES[lk["link_type"]]["kind"]
    if action not in model.ACTIONS_BY_KIND[kind]:
        raise _err(
            "invalid_action",
            f"{action!r} is not a valid decision on a {kind} subject",
            422,
            allowed=list(model.ACTIONS_BY_KIND[kind]),
        )
    rationale = _text(rationale, "rationale", 3, MAX_TEXT)
    ev_ids = {
        r[0]
        for r in conn.execute(
            "SELECT link_id FROM investigation_links l WHERE investigation_id = ? AND NOT EXISTS "
            "(SELECT 1 FROM investigation_link_removals r WHERE r.link_id = l.link_id)",
            (inv_id,),
        )
    }
    for name, ids in (("supporting", supporting or []), ("contradicting", contradicting or [])):
        bad = [i for i in ids if i not in ev_ids]
        if bad:
            raise _err(
                "invalid_request", f"{name} evidence must be active links of this investigation", bad=bad
            )
    payload = dict(payload or {})
    effect = _check_payload(conn, cfg, row, lk, action, payload, actor) if lk is not None else {}
    if unresolved is None:
        unresolved = action in (
            "further_investigation_required",
            "evidence_insufficient",
            "finding_under_review",
        )
    did = "DC-" + new_ulid()
    stored_payload = payload | ({"effect": effect} if effect else {})
    with transaction(conn):
        conn.execute(
            "INSERT INTO investigation_decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                did,
                inv_id,
                link_id,
                action,
                rationale,
                canonical_json(stored_payload).decode(),
                canonical_json(sorted(supporting or [])).decode(),
                canonical_json(sorted(contradicting or [])).decode(),
                int(bool(unresolved)),
                actor,
                utc_now_iso(),
            ),
        )
        _event(
            conn,
            inv_id,
            "decision_recorded",
            actor,
            {
                "decision_id": did,
                "link_id": link_id,
                "action": action,
                "rationale_sha256": sha256_hex(rationale),
                "unresolved": bool(unresolved),
                "payload": stored_payload,
            },
            subject_id=link_id,
        )
    return _decision_dict(
        conn.execute("SELECT * FROM investigation_decisions WHERE decision_id = ?", (did,)).fetchone()
    )


def decisions_page(conn, inv_id: str, link_id: str | None, page: int, size: int) -> dict[str, Any]:
    _inv(conn, inv_id)
    sql, args = "SELECT * FROM investigation_decisions WHERE investigation_id = ?", [inv_id]
    if link_id:
        sql, args = sql + " AND link_id = ?", [*args, link_id]
    total = conn.execute(sql.replace("SELECT *", "SELECT COUNT(*)"), args).fetchone()[0]
    rows = conn.execute(
        sql + " ORDER BY created_at, decision_id LIMIT ? OFFSET ?", [*args, size, (page - 1) * size]
    ).fetchall()
    return {"page": page, "size": size, "total": total, "items": [_decision_dict(r) for r in rows]}


def decisions_on(conn, subject: str, ref_id: str) -> list[dict[str, Any]]:
    """Analyst decisions on a reconciliation result / duplicate group (read-only overlay for P14 views)."""
    rows = conn.execute(
        "SELECT d.*, l.link_type, l.ref_id, l.run_id FROM investigation_decisions d JOIN investigation_links "
        "l ON l.link_id = d.link_id WHERE l.link_type = ? AND l.ref_id = ? "
        "ORDER BY d.created_at, d.decision_id",
        (subject, ref_id),
    ).fetchall()
    return [_decision_dict(r) | {"run_id": r["run_id"]} for r in rows]


# ------------------------------------------------------------------------------------------- activity


def activity_page(conn, inv_id: str, page: int, size: int) -> dict[str, Any]:
    _inv(conn, inv_id)
    total = conn.execute(
        "SELECT COUNT(*) FROM investigation_events WHERE investigation_id = ?", (inv_id,)
    ).fetchone()[0]
    rows = conn.execute(
        "SELECT e.*, a.hash AS audit_hash FROM investigation_events e JOIN audit_log a ON a.seq = "
        "e.audit_seq WHERE e.investigation_id = ? ORDER BY e.seq LIMIT ? OFFSET ?",
        (inv_id, size, (page - 1) * size),
    ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["details"] = json.loads(d["details"])
        items.append(d)
    return {"page": page, "size": size, "total": total, "items": items}


def summary(conn, inv_id: str) -> dict[str, Any]:
    """Deterministic summary from stored records only (no automated output is recomputed here)."""
    inv = get(conn, inv_id)
    links = links_page(conn, inv_id, None, None, 1, 1_000_000)["items"]
    active = [x for x in links if x["active"]]
    by_type: dict[str, int] = {}
    for x in active:
        by_type[x["link_type"]] = by_type.get(x["link_type"], 0) + 1
    finding_status: dict[str, int] = {}
    for x in active:
        if x["finding_status"]:
            finding_status[x["finding_status"]] = finding_status.get(x["finding_status"], 0) + 1
    decisions = decisions_page(conn, inv_id, None, 1, 1_000_000)["items"]
    latest: dict[Any, dict[str, Any]] = {}
    for d in decisions:
        latest[d["link_id"]] = d
    unresolved = sorted(k or "case" for k, d in latest.items() if d["unresolved"])
    corrections = conn.execute(
        "SELECT COUNT(*) FROM ocr_corrections WHERE investigation_id = ?", (inv_id,)
    ).fetchone()[0]
    out = {
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
            )
        },
        "links": {
            "active": len(active),
            "removed": len(links) - len(active),
            "by_type": dict(sorted(by_type.items())),
        },
        "findings": {
            "by_analyst_status": dict(sorted(finding_status.items())),
            "note": "analyst assessment of linked automated findings; the findings themselves are unchanged",
        },
        "decisions": {
            "total": len(decisions),
            "unresolved_subjects": unresolved,
            "by_action": dict(
                sorted(
                    {
                        a: sum(1 for d in decisions if d["action"] == a)
                        for a in {d["action"] for d in decisions}
                    }.items()
                )
            ),
        },
        "notes": inv["counts"]["notes"],
        "ocr_corrections": corrections,
        "note": NOTE,
    }
    out["summary_sha256"] = sha256_hex(canonical_json(out))
    return out


def manifest(conn, workspace, inv_id: str) -> dict[str, Any]:
    """Every link (active and removed) with its reference and snapshot hash, and every evidence file behind
    them, re-hashed now. The manifest hash covers the stored references only (not the verification time)."""
    inv = _inv(conn, inv_id)
    links = links_page(conn, inv_id, None, None, 1, 1_000_000)["items"]
    entries = [
        {
            k: x[k]
            for k in (
                "link_id",
                "link_type",
                "dataset_id",
                "run_id",
                "ref_id",
                "snapshot_sha256",
                "reason",
                "created_by",
                "created_at",
                "active",
            )
        }
        for x in links
    ]
    shas: set[str] = set()
    for x in links:
        if x["dataset_id"]:
            shas.add(
                conn.execute(
                    "SELECT evidence_sha256 FROM datasets WHERE dataset_id = ?", (x["dataset_id"],)
                ).fetchone()[0]
            )
        if x["run_id"]:
            run = conn.execute(
                "SELECT ledger_dataset_id, bank_dataset_id FROM reconciliation_runs WHERE run_id = ?",
                (x["run_id"],),
            ).fetchone()
            for ds in run:
                shas.add(
                    conn.execute(
                        "SELECT evidence_sha256 FROM datasets WHERE dataset_id = ?", (ds,)
                    ).fetchone()[0]
                )
        if x["link_type"] == "evidence_file":
            shas.add(x["ref_id"])
    files = []
    for sha in sorted(shas):
        try:
            evidence.verify_one(conn, sha, workspace)
            state = "verified"
        except LedgerSpyError as exc:
            state = exc.code
        files.append({"sha256": sha, "verification": state})
    body = {
        "investigation_id": inv_id,
        "case_id": inv["case_id"],
        "links": entries,
        "evidence_files": [f["sha256"] for f in files],
    }
    return body | {
        "manifest_sha256": sha256_hex(canonical_json(body)),
        "evidence_verification": files,
        "verified_at": utc_now_iso(),
    }


# ------------------------------------------------------------------------------------------- OCR corrections


def correct_ocr(
    conn,
    cfg: EffectiveConfig,
    inv_id: str,
    dataset_id: str,
    item_id: str,
    field: str,
    value: str,
    rationale: str,
    actor: str,
) -> dict[str, Any]:
    """Correct one extracted field as part of an investigation: the row is linked (if not yet), the correction
    overlay is recorded (pdfstatements.corrections) and an `ocr_row_corrected` decision references it."""
    from ledgerspy.pdfstatements import corrections

    row = _inv(conn, inv_id)
    _writable(row)
    r = refs.resolve(conn, cfg, row["case_id"], "pdf_item", dataset_id, None, item_id)
    existing = _active_link(conn, inv_id, r["scope_key"])
    if existing is None:
        with transaction(conn):
            link_id = _insert_link(
                conn,
                inv_id,
                r,
                {"link_type": "pdf_item", "reason": "OCR row reviewed for correction", "annotation": None},
                actor,
            )
    else:
        link_id = existing[0]
    fix = corrections.correct(conn, cfg, dataset_id, item_id, field, value, rationale, actor, inv_id)
    dec = decide(
        conn,
        cfg,
        inv_id,
        link_id,
        "ocr_row_corrected",
        rationale,
        actor,
        payload={"correction_id": fix["correction_id"], "field": field},
        unresolved=False,
    )
    return {"correction": fix, "decision": dec, "link_id": link_id}
