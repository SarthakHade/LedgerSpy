"""Reconciliation runs: setup (accounts, scope and direction proposals), run, persistence and views.

Inputs: the P4 normalised views of one ledger dataset and one bank-statement dataset of the SAME case, the
analyst-confirmed scopes (bank account ↔ ledger filter) and ledger debit semantics, the bank dataset's
confirmed mapping, and — when `counterparty.use_entity_resolution` — the ledger's P12 entity resolution (the
same memoised run P6/P9/P13 use). Labels, ground truth, supervised outputs and P9 scores are never read.

A run's identity (dataset IDs and data versions, mappings, scopes, semantics, configuration, engine version)
gives its ID (`RR-…`): the same inputs always give the same run and the same result hash. Each run is recorded
once in `reconciliation_runs` (append-only, audit-logged) with its parameters and result hash; results are
recomputed from those parameters when not cached and verified against the stored hash.
"""

import json
import threading
import time
from collections import Counter, OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ledgerspy.cases import audit
from ledgerspy.cases.db import transaction
from ledgerspy.config.loader import EffectiveConfig
from ledgerspy.errors import LedgerSpyError
from ledgerspy.features.data import party_key
from ledgerspy.hashing import canonical_json, sha256_hex, utc_now_iso
from ledgerspy.normalize.view import DatasetView, data_version, load_view, overlay_rows
from ledgerspy.reconciliation import balances, duplicates, engine, findings
from ledgerspy.reconciliation.records import (
    TEXT_FIELDS,
    NameIndex,
    Scope,
    account_key,
    account_label,
    alnum,
    bank_rows,
    fallback_entity,
    ledger_rows,
    ledger_value,
)

NOTE = (
    "Reconciliation findings are evidence for review, not proof of fraud. They are a separate evidence "
    "family and do not change P9 review-risk scores. Evidence scores rank candidate matches; they are not "
    "probabilities."
)
FORMATS_NOTE = "CSV and XLSX statements are supported. PDF statements (text and scanned / OCR) come in P14b."
DATASET_ID = r"^D-[0-9A-HJKMNP-TV-Z]{26}$"
ACCOUNT_KEY = r"^A-([0-9a-f]{12}|unidentified)$"


class ScopeIn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    account_key: str = Field(pattern=ACCOUNT_KEY)
    ledger_column: str | None = Field(default=None, min_length=1, max_length=255)
    ledger_value: str | None = Field(default=None, min_length=1, max_length=200)


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ledger_dataset_id: str = Field(pattern=DATASET_ID)
    bank_dataset_id: str = Field(pattern=DATASET_ID)
    scopes: list[ScopeIn] = Field(default_factory=list, max_length=50)
    ledger_debit_means: Literal["outflow", "inflow"] | None = None


@dataclass
class Run:
    run_id: str
    identity: dict[str, Any]
    request: dict[str, Any]
    scopes: list[dict[str, Any]]
    results: list[dict[str, Any]]
    by_id: dict[str, dict[str, Any]]
    duplicates: list[dict[str, Any]]
    balance: dict[str, Any]
    findings: list[dict[str, Any]]
    summary: dict[str, Any]
    truncation: dict[str, Any]
    limitations: list[str]
    readiness: dict[str, Any]
    result_sha256: str
    timings: dict[str, float]
    datasets: dict[str, Any]
    record: dict[str, Any] | None = None


_CACHE: "OrderedDict[str, Run]" = OrderedDict()
_LOCK = threading.Lock()


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()


# ------------------------------------------------------------------------------------------- inputs


def _dataset(conn, dataset_id: str, kind: str) -> tuple[DatasetView, Any]:
    view = load_view(conn, dataset_id)  # 404 / 409
    row = conn.execute("SELECT * FROM datasets WHERE dataset_id = ?", (dataset_id,)).fetchone()
    if row["kind"] != kind:
        raise LedgerSpyError(
            "wrong_dataset_kind",
            f"dataset {dataset_id} is a {row['kind']!r} dataset; a {kind!r} dataset is required",
            {"dataset_id": dataset_id, "kind": row["kind"], "required": kind},
            http_status=422,
        )
    return view, row


def _pair(conn, ledger_id: str, bank_id: str):
    lv, lrow = _dataset(conn, ledger_id, "ledger")
    bv, brow = _dataset(conn, bank_id, "bank_statement")
    if lrow["case_id"] != brow["case_id"]:
        raise LedgerSpyError(
            "different_cases",
            "the ledger and the bank statement must belong to the same case",
            {"ledger_case_id": lrow["case_id"], "bank_case_id": brow["case_id"]},
            http_status=422,
        )
    return lv, lrow, bv, brow


def _accounts(bv: DatasetView) -> dict[str, dict[str, Any]]:
    mapped = "account" in bv.mapping["fields"]
    acc: dict[str, dict[str, Any]] = {}
    for r in bv.rows:
        norm = alnum(r.get("account"))
        if mapped and norm is None:
            continue
        k = account_key(norm)
        a = acc.setdefault(
            k,
            {
                "account_key": k,
                "account": account_label(norm),
                "rows": 0,
                "currencies": Counter(),
                "first_date": None,
                "last_date": None,
            },
        )
        a["rows"] += 1
        a["currencies"][r.get("currency") or "unknown"] += 1
        d = (r.get("posted_at") or "")[:10] or None
        if d:
            a["first_date"] = min(a["first_date"] or d, d)
            a["last_date"] = max(a["last_date"] or d, d)
    for a in acc.values():
        a["currencies"] = dict(sorted(a["currencies"].items()))
    return dict(sorted(acc.items()))


def _ledger_columns(lv: DatasetView) -> list[str]:
    mapped = {f: c for f, c in lv.mapping["fields"].items()}
    canonical = [f for f in TEXT_FIELDS if f in mapped]
    extra = [
        h for h in lv.header if h not in set(mapped.values()) and h not in lv.mapping.get("label_columns", [])
    ]
    return canonical + extra


def _norm_name(s: str | None) -> str | None:
    return party_key(s)


def _direction_proposal(rows: list[dict[str, Any]], min_share: float) -> dict[str, Any] | None:
    """Who holds the account? The party paying on debit rows and receiving on credit rows (or vice versa)."""
    deb = [r for r in rows if r.get("direction") == "debit"]
    cred = [r for r in rows if r.get("direction") == "credit"]
    if not deb or not cred:
        return None

    def top(rs, f):
        c = Counter(_norm_name(r.get(f)) for r in rs if r.get(f))
        if not c:
            return None, 0.0
        k, n = sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        return k, n / len(rs)

    for means, (df, cf) in (("outflow", ("payer_raw", "payee_raw")), ("inflow", ("payee_raw", "payer_raw"))):
        a, sa = top(deb, df)
        b, sb = top(cred, cf)
        if a and a == b and min(sa, sb) >= min_share:
            return {
                "value": means,
                "account_holder": a,
                "basis": f"{a!r} is the {df.split('_')[0]} on {sa:.0%} of debit rows and the "
                f"{cf.split('_')[0]} on {sb:.0%} of credit rows: a debit is money "
                f"{'out of' if means == 'outflow' else 'into'} its bank account",
                "shares": {"debit_rows": round(sa, 6), "credit_rows": round(sb, 6)},
            }
    return None


def setup(conn, cfg: EffectiveConfig, ledger_id: str, bank_id: str) -> dict[str, Any]:
    """What the analyst must confirm before a run, with evidence-backed proposals (never applied silently)."""
    lv, lrow, bv, brow = _pair(conn, ledger_id, bank_id)
    rc = cfg.settings.reconciliation
    accounts = _accounts(bv)
    columns = _ledger_columns(lv)
    proposals = _scope_proposals(conn, lv, accounts, columns)
    dirs = Counter(r.get("direction") or "missing" for r in lv.rows)
    needs_semantics = bool(dirs.get("debit") or dirs.get("credit"))
    direction = None
    if needs_semantics:
        subsets = [
            [r for r in lv.rows if ledger_value(r, p["ledger_column"]) == p["ledger_value"]]
            for p in proposals
        ]
        found = [
            _direction_proposal(rows, rc.direction_proposal_min_share) for rows in (subsets or [lv.rows])
        ]
        if found and all(f is not None for f in found) and len({f["value"] for f in found}) == 1:
            direction = {
                "value": found[0]["value"],
                "basis": "; ".join(f["basis"] for f in found),
                "per_scope": found,
            }
    bank_map = bv.mapping.get("bank") or {}
    requires = []
    if len(accounts) > 1 and not proposals:
        requires.append({"item": "scopes", "reason": "several bank accounts: map each to its ledger rows"})
    elif len(accounts) > 1:
        requires.append({"item": "scopes", "reason": "confirm the proposed account ↔ ledger scopes"})
    else:
        requires.append(
            {
                "item": "scopes",
                "reason": "confirm that the ledger rows belong to this bank account "
                "(optionally restrict them with a ledger filter)",
            }
        )
    if needs_semantics:
        requires.append(
            {
                "item": "ledger_debit_means",
                "reason": "the ledger uses debit/credit: confirm whether a "
                "debit is money out of ('outflow') or into ('inflow') the bank account",
            }
        )
    limitations = []
    if not bank_map.get("balance_column"):
        limitations.append("No running-balance column: balance checks are unavailable.")
    missing_cur = {
        "ledger": sum(1 for r in lv.rows if not r.get("currency")),
        "bank": sum(1 for r in bv.rows if not r.get("currency")),
    }
    if any(missing_cur.values()):
        limitations.append(f"Rows without a currency are not reconciled: {missing_cur}.")
    return {
        "ledger": {
            "dataset_id": ledger_id,
            "filename": lrow["original_filename"],
            "rows": len(lv.rows),
            "adapter": lv.adapter,
            "directions": dict(sorted(dirs.items())),
            "scope_columns": columns,
        },
        "bank": {
            "dataset_id": bank_id,
            "filename": brow["original_filename"],
            "rows": len(bv.rows),
            "accounts": list(accounts.values()),
            "account_mapped": "account" in bv.mapping["fields"],
            "direction_semantics": {k: bank_map.get(k) for k in ("debit_means", "amount_sign")},
            "balance_column": bank_map.get("balance_column"),
            "statement_column": bank_map.get("statement_column"),
        },
        "proposals": {"scopes": proposals, "ledger_debit_means": direction},
        "requires_confirmation": requires,
        "missing_currency_rows": missing_cur,
        "limitations": limitations,
        "readiness": _readiness(conn, lv, bv),
        "formats": FORMATS_NOTE,
        "note": NOTE,
    }


def _scope_proposals(conn, lv: DatasetView, accounts: dict, columns: list[str]) -> list[dict[str, Any]]:
    """From the case's group-company master (P12 import): its bank account ↔ the ledger column of its code."""
    from ledgerspy.features.service import masters_for

    out = []
    for m in masters_for(conn, lv.dataset_id):
        if m.entity_type != "group_company":
            continue
        k = account_key(alnum(m.get("bank_account"))) if m.get("bank_account") else None
        code, name = (m.get("record_id") or "").strip(), (m.get("name") or "").strip()
        if k not in accounts or not code:
            continue
        best = None
        for col in columns:
            n = sum(1 for r in lv.rows if ledger_value(r, col) == code)
            if n and (best is None or n > best[1]):
                best = (col, n)
        if best:
            out.append(
                {
                    "account_key": k,
                    "account": accounts[k]["account"],
                    "ledger_column": best[0],
                    "ledger_value": code,
                    "ledger_rows": best[1],
                    "basis": f"group-company master record {code} ({name}) lists this bank account; ledger "
                    f"column {best[0]!r} has the value {code!r} on {best[1]} rows",
                }
            )
    return sorted(out, key=lambda p: p["account_key"])


def _readiness(conn, lv: DatasetView, bv: DatasetView) -> dict[str, Any]:
    out = {}
    for side, v in (("ledger", lv), ("bank", bv)):
        r = conn.execute(
            "SELECT run_id, status, readiness, data_version FROM integrity_runs WHERE dataset_id = ? "
            "ORDER BY created_at DESC, run_id DESC LIMIT 1",
            (v.dataset_id,),
        ).fetchone()
        out[side] = (
            None
            if r is None
            else {
                "integrity_run_id": r["run_id"],
                "status": r["status"],
                "readiness": r["readiness"],
                "current": r["data_version"] == v.data_version,
            }
        )
    return out


# ------------------------------------------------------------------------------------------- run


def _names(conn, cfg: EffectiveConfig, lv: DatasetView):
    rc = cfg.settings.reconciliation
    raw = sorted({n for r in lv.rows for n in (r.get("payer_raw"), r.get("payee_raw")) if n})
    if rc.counterparty.use_entity_resolution and lv.adapter != "paysim":
        from ledgerspy.features import service as feature_service
        from ledgerspy.features.resolution import resolution_of
        from ledgerspy.resolution.records import ledger_key

        res = resolution_of(feature_service.input_for(conn, cfg, lv))

        def entity_of(name: str | None) -> str | None:
            k = party_key(name)
            return None if k is None else res.entity_of.get(ledger_key(k), "n:" + k)

        names = [(entity_of(n), n) for n in raw]
        names += [
            (res.entity_of[k], rec.name.raw)
            for k, rec in sorted(res.records.items())
            if rec.source == "master" and rec.name.raw and k in res.entity_of
        ]
        return entity_of, names, res.sha256
    return fallback_entity, [(fallback_entity(n), n) for n in raw], None


def _scopes(req: RunRequest, accounts: dict, lv: DatasetView) -> tuple[list[Scope], list[str]]:
    lim = []
    if not req.scopes:
        if len(accounts) != 1:
            raise LedgerSpyError(
                "scopes_required",
                "the statement has several bank accounts: confirm which ledger rows belong to each",
                {"accounts": list(accounts.values())},
                http_status=422,
            )
        only = next(iter(accounts))
        lim.append(
            "Scope not confirmed: every ledger row is assumed to belong to the statement's only account."
        )
        items = [ScopeIn(account_key=only)]
    else:
        items = req.scopes
    keys = [s.account_key for s in items]
    if len(set(keys)) != len(keys):
        raise LedgerSpyError(
            "invalid_scopes", "each bank account may appear in one scope only", http_status=422
        )
    cols = set(_ledger_columns(lv))
    for s in items:
        if s.account_key not in accounts:
            raise LedgerSpyError(
                "invalid_scopes",
                f"unknown bank account {s.account_key}",
                {"accounts": list(accounts)},
                http_status=422,
            )
        if (s.ledger_column is None) != (s.ledger_value is None):
            raise LedgerSpyError(
                "invalid_scopes", "ledger_column and ledger_value go together", http_status=422
            )
        if s.ledger_column is not None and s.ledger_column not in cols:
            raise LedgerSpyError(
                "invalid_scopes",
                f"unknown ledger column {s.ledger_column!r}",
                {"columns": sorted(cols)},
                http_status=422,
            )
        if s.ledger_column is None and len(items) > 1:
            raise LedgerSpyError(
                "invalid_scopes", "with several scopes every scope needs a ledger filter", http_status=422
            )
    scopes = [
        Scope(
            "SC-" + sha256_hex(canonical_json([s.account_key, s.ledger_column, s.ledger_value]))[:10],
            s.account_key,
            s.ledger_column,
            s.ledger_value,
        )
        for s in sorted(items, key=lambda s: s.account_key)
    ]
    return scopes, lim


def _identity(lv, lrow, bv, brow, req: RunRequest, scopes, cfg: EffectiveConfig, res_sha) -> dict[str, Any]:
    st = cfg.settings
    return {
        "engine_version": engine.VERSION,
        "ledger": {
            "dataset_id": lv.dataset_id,
            "data_version": lv.data_version,
            "mapping_sha256": lrow["mapping_sha256"],
        },
        "bank": {
            "dataset_id": bv.dataset_id,
            "data_version": bv.data_version,
            "mapping_sha256": brow["mapping_sha256"],
        },
        "scopes": [s.to_dict("") | {"account": None} for s in scopes],
        "ledger_debit_means": req.ledger_debit_means,
        "reconciliation_config_sha256": sha256_hex(canonical_json(st.reconciliation.model_dump(mode="json"))),
        "minor_units_sha256": sha256_hex(canonical_json(st.ingest.minor_units.model_dump(mode="json"))),
        "resolution_sha256": res_sha,
    }


def _compute(conn, cfg: EffectiveConfig, req: RunRequest) -> Run:
    t0 = time.perf_counter()
    lv, lrow, bv, brow = _pair(conn, req.ledger_dataset_id, req.bank_dataset_id)
    rc = cfg.settings.reconciliation
    if req.ledger_debit_means is None and any(r.get("direction") in ("debit", "credit") for r in lv.rows):
        raise LedgerSpyError(
            "ledger_semantics_required",
            "the ledger uses debit/credit: confirm ledger_debit_means ('outflow' or 'inflow')",
            {"proposal": _direction_proposal(lv.rows, rc.direction_proposal_min_share)},
            http_status=422,
        )
    accounts = _accounts(bv)
    scopes, limitations = _scopes(req, accounts, lv)
    entity_of, names, res_sha = _names(conn, cfg, lv)
    t1 = time.perf_counter()
    index = NameIndex(
        [(e, n) for e, n in names if e], rc.counterparty.min_name_chars, rc.counterparty.min_prefix_chars
    )
    L = ledger_rows(lv.rows, scopes, req.ledger_debit_means, entity_of)
    B = bank_rows(bv.rows, "account" in bv.mapping["fields"], scopes, index)
    t2 = time.perf_counter()
    dups = duplicates.find(L, B, rc)
    out = engine.reconcile(L, B, rc, dups)
    t3 = time.perf_counter()
    bal = balances.check(bv.rows, bv.mapping, cfg.settings)
    status_of = {
        (side, t): r["status"]
        for r in out.results
        for side in ("ledger", "bank")
        for t in r[side + "_txn_ids"]
    }
    result_of = {
        (side, t): r["result_id"]
        for r in out.results
        for side in ("ledger", "bank")
        for t in r[side + "_txn_ids"]
    }
    groups_of = defaultdict(set)
    for g in dups:
        g["member_status"] = {t: status_of[(g["side"], t)] for t in g["txn_ids"]}
        g["member_results"] = {t: result_of[(g["side"], t)] for t in g["txn_ids"]}
        for t in g["txn_ids"]:
            groups_of[(g["side"], t)].add(g["group_id"])
    scope_info = {s.scope_id: s for s in scopes}
    for r in out.results:
        ids = set(r["duplicate_group_ids"])
        for side in ("ledger", "bank"):
            for t in r[side + "_txn_ids"]:
                ids |= groups_of.get((side, t), set())
        r["duplicate_group_ids"] = sorted(ids)
        sc = scope_info.get(r["scope"]["scope_id"])
        r["scope"]["account_key"] = sc.account_key if sc else None
        if sc and r["scope"]["account"] is None:
            r["scope"]["account"] = accounts[sc.account_key]["account"]
    sig = findings.compute(out.results, dups, bal)
    summary = engine.summarize(out.results, L, B)
    summary["duplicates"] = {
        "groups": len(dups),
        "by_type": dict(sorted(Counter(f"{g['side']}_{g['type']}" for g in dups).items())),
        "rows_in_groups": {
            s: sum(len(g["txn_ids"]) for g in dups if g["side"] == s) for s in ("ledger", "bank")
        },
        "note": "candidates only; nothing is excluded or deleted",
    }
    summary["balance_check"] = {
        "status": bal["status"],
        "accounts": [
            {k: a.get(k) for k in ("account_key", "account", "currency", "status", "reason")}
            | {"discontinuities": len(a.get("discontinuities", []))}
            for a in bal["accounts"]
        ],
    }
    if bal["status"] == "unavailable" and bal.get("reason"):
        summary["balance_check"]["reason"] = bal["reason"]
    summary["findings"] = dict(sorted(Counter(s["rule_id"] for s in sig).items()))
    if out.truncation:
        limitations.append(
            "Candidate limits were reached (see truncation): those rows' searches are incomplete."
        )
    if any(v is None or not v["current"] for v in (_readiness(conn, lv, bv)).values()):
        limitations.append("No current P4 integrity run for one of the datasets: data quality is unchecked.")
    if res_sha is None:
        limitations.append("Entity resolution not used: counterparty names are compared by spelling only.")
    for r in out.results:  # P14b: say where a bank line's values came from when OCR read them
        ocr = [b["txn_id"] for b in r["bank_rows"] if (b.get("extraction") or {}).get("method") == "ocr"]
        if ocr:
            r["limitations"].append(
                f"Bank line(s) {', '.join(ocr)} were read by OCR from the PDF and confirmed by a reviewer; "
                "OCR confidence is not financial correctness — check the page image before relying on this "
                "result."
            )
    identity = _identity(lv, lrow, bv, brow, req, scopes, cfg, res_sha)
    run_id = "RR-" + sha256_hex(canonical_json(identity))[:16]
    content = {
        "results": out.results,
        "duplicates": dups,
        "balance": bal,
        "findings": [s["signal_id"] for s in sig],
        "truncation": out.truncation,
    }
    result_sha = sha256_hex(canonical_json(content))
    scope_list = []
    for s in scopes:
        d = s.to_dict(accounts[s.account_key]["account"])
        d["ledger_rows"] = sum(1 for r in L if r.scope == s.scope_id)
        d["bank_rows"] = sum(1 for r in B if r.scope == s.scope_id)
        d["confirmed"] = bool(req.scopes)
        scope_list.append(d)
    return Run(
        run_id=run_id,
        identity=identity,
        request=req.model_dump(mode="json"),
        scopes=scope_list,
        results=out.results,
        by_id={r["result_id"]: r for r in out.results},
        duplicates=dups,
        balance=bal,
        findings=sig,
        summary=summary,
        truncation={
            "limits_reached": bool(out.truncation),
            "rows": out.truncation,
            "blocks": out.blocks,
            "candidate_pairs_evaluated": out.candidate_pairs,
        },
        limitations=limitations,
        readiness=_readiness(conn, lv, bv),
        result_sha256=result_sha,
        timings={
            "inputs_and_resolution": round(t1 - t0, 4),
            "normalisation": round(t2 - t1, 4),
            "matching": round(t3 - t2, 4),
            "balances_and_findings": round(time.perf_counter() - t3, 4),
        },
        datasets={
            "ledger": {
                "dataset_id": lv.dataset_id,
                "filename": lrow["original_filename"],
                "evidence_sha256": lrow["evidence_sha256"],
                "data_version": lv.data_version,
            },
            "bank": {
                "dataset_id": bv.dataset_id,
                "filename": brow["original_filename"],
                "evidence_sha256": brow["evidence_sha256"],
                "data_version": bv.data_version,
            },
        },
    )


def _cache(run: Run, cfg: EffectiveConfig) -> Run:
    with _LOCK:
        _CACHE[run.run_id] = run
        _CACHE.move_to_end(run.run_id)
        while len(_CACHE) > cfg.settings.reconciliation.limits.max_runs_cached:
            _CACHE.popitem(last=False)
    return run


def _record(conn, run_id: str):
    return conn.execute("SELECT * FROM reconciliation_runs WHERE run_id = ?", (run_id,)).fetchone()


def start(conn, cfg: EffectiveConfig, req: RunRequest, actor: str) -> Run:
    run = _compute(conn, cfg, req)
    rec = _record(conn, run.run_id)
    if rec is None:
        lv_case = conn.execute(
            "SELECT case_id FROM datasets WHERE dataset_id = ?", (req.ledger_dataset_id,)
        ).fetchone()[0]
        with transaction(conn):
            conn.execute(
                "INSERT INTO reconciliation_runs (run_id, case_id, ledger_dataset_id, bank_dataset_id, "
                "parameters, identity, result_sha256, summary, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run.run_id,
                    lv_case,
                    req.ledger_dataset_id,
                    req.bank_dataset_id,
                    canonical_json(run.request).decode(),
                    canonical_json(run.identity).decode(),
                    run.result_sha256,
                    canonical_json(_brief(run)).decode(),
                    actor,
                    utc_now_iso(),
                ),
            )
            audit.append(
                conn,
                actor,
                "reconciliation.run",
                f"reconciliation:{run.run_id}",
                {
                    "ledger_dataset_id": req.ledger_dataset_id,
                    "bank_dataset_id": req.bank_dataset_id,
                    "result_sha256": run.result_sha256,
                    "scopes": run.identity["scopes"],
                    "ledger_debit_means": req.ledger_debit_means,
                },
            )
        rec = _record(conn, run.run_id)
    elif rec["result_sha256"] != run.result_sha256:  # pragma: no cover — identity covers every input
        raise LedgerSpyError(
            "result_mismatch",
            "the recomputed result differs from the recorded run",
            {"run_id": run.run_id},
            http_status=500,
        )
    run.record = {"created_by": rec["created_by"], "created_at": rec["created_at"]}
    return _cache(run, cfg)


def get_run(conn, cfg: EffectiveConfig, run_id: str) -> Run:
    with _LOCK:
        cached = _CACHE.get(run_id)
    if cached is not None:
        # a cached run is served only while its inputs are unchanged (P15: e.g. no OCR correction since)
        rec = _record(conn, run_id)
        ident = cached.identity
        for side in ("ledger", "bank"):
            ds = ident[side]["dataset_id"]
            if data_version(overlay_rows(conn, ds)) != ident[side]["data_version"]:
                with _LOCK:
                    _CACHE.pop(run_id, None)
                raise LedgerSpyError(
                    "inputs_changed",
                    f"the {side} dataset's normalised data changed after this run (P4 transformations, e.g. "
                    "an OCR correction); start a new run — the recorded run stays in the register",
                    {"run_id": run_id},
                    http_status=409,
                )
        if rec is not None:
            with _LOCK:
                _CACHE.move_to_end(run_id)
            return cached
    rec = _record(conn, run_id)
    if rec is None:
        raise LedgerSpyError("run_not_found", "reconciliation run not found", http_status=404)
    req = RunRequest(**json.loads(rec["parameters"]))
    identity = json.loads(rec["identity"])
    for side, ds in (("ledger", req.ledger_dataset_id), ("bank", req.bank_dataset_id)):
        if load_view(conn, ds).data_version != identity[side]["data_version"]:
            raise LedgerSpyError(
                "inputs_changed",
                f"the {side} dataset's normalised data changed after this run (P4 transformations); start a "
                "new run — the recorded run stays in the register",
                {"run_id": run_id},
                http_status=409,
            )
    run = _compute(conn, cfg, req)
    if run.run_id != run_id or run.result_sha256 != rec["result_sha256"]:
        raise LedgerSpyError(
            "inputs_changed",
            "the configuration or inputs differ from the recorded run; start a new run",
            {"run_id": run_id, "recomputed_run_id": run.run_id},
            http_status=409,
        )
    run.record = {"created_by": rec["created_by"], "created_at": rec["created_at"]}
    return _cache(run, cfg)


def list_runs(conn, ledger_id: str | None, bank_id: str | None) -> dict[str, Any]:
    where, args = [], []
    if ledger_id:
        where, args = [*where, "ledger_dataset_id = ?"], [*args, ledger_id]
    if bank_id:
        where, args = [*where, "bank_dataset_id = ?"], [*args, bank_id]
    clause = " WHERE " + " AND ".join(where) if where else ""
    sql = "SELECT * FROM reconciliation_runs" + clause  # noqa: S608 — fixed fragments only
    rows = conn.execute(sql + " ORDER BY created_at DESC, run_id", args).fetchall()
    stale = stale_run_ids(conn)
    return {
        "items": [
            {
                "run_id": r["run_id"],
                "stale": r["run_id"] in stale,
                "case_id": r["case_id"],
                "ledger_dataset_id": r["ledger_dataset_id"],
                "bank_dataset_id": r["bank_dataset_id"],
                "result_sha256": r["result_sha256"],
                "summary": json.loads(r["summary"]),
                "created_by": r["created_by"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    }


def stale_run_ids(conn, dataset_id: str | None = None) -> set[str]:
    """P15: recorded runs whose ledger or bank data changed since (e.g. an OCR correction applied as a P4
    transformation). Their recorded results are no longer valid: `get_run` refuses them (`inputs_changed`)."""
    versions: dict[str, str] = {}
    out = set()
    sql, args = "SELECT run_id, identity FROM reconciliation_runs", []
    if dataset_id:
        sql, args = sql + " WHERE ledger_dataset_id = ? OR bank_dataset_id = ?", [dataset_id, dataset_id]
    for r in conn.execute(sql, args):
        ident = json.loads(r["identity"])
        for side in ("ledger", "bank"):
            ds = ident[side]["dataset_id"]
            if ds not in versions:
                versions[ds] = data_version(overlay_rows(conn, ds))
            if ident[side]["data_version"] != versions[ds]:
                out.add(r["run_id"])
    return out


def _brief(run: Run) -> dict[str, Any]:
    s = run.summary
    return {
        k: s[k]
        for k in (
            "ledger_rows",
            "bank_rows",
            "results_by_status",
            "matched_amount_minor",
            "unmatched_amount_minor",
        )
    } | {"balance_check": s["balance_check"]["status"]}


# ------------------------------------------------------------------------------------------- views


def summary(run: Run) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "result_sha256": run.result_sha256,
        "datasets": run.datasets,
        "scopes": run.scopes,
        "request": run.request,
        "identity": run.identity,
        "summary": run.summary,
        "truncation": run.truncation,
        "limitations": run.limitations,
        "readiness": run.readiness,
        "record": run.record,
        "timings_seconds": run.timings,
        "detector": engine.DETECTOR,
        "formats": FORMATS_NOTE,
        "note": NOTE,
    }


def _page(items: list, page: int, size: int) -> dict[str, Any]:
    return {"page": page, "size": size, "total": len(items), "items": items[(page - 1) * size : page * size]}


def results_page(
    run: Run,
    status: str | None,
    account: str | None,
    currency: str | None,
    frm: date | None,
    to: date | None,
    q: str | None,
    page: int,
    size: int,
) -> dict[str, Any]:
    if frm and to and frm > to:
        raise LedgerSpyError("invalid_window", "`from` must not be after `to`")
    items = []
    for r in run.results:
        if status and r["status"] != status:
            continue
        if account and r["scope"].get("account_key") != account:
            continue
        if currency and r["scope"]["currency"] != currency:
            continue
        if frm and (r["last_date"] is None or r["last_date"] < frm.isoformat()):
            continue
        if to and (r["first_date"] is None or r["first_date"] > to.isoformat()):
            continue
        if q and not any(q in t for t in r["ledger_txn_ids"] + r["bank_txn_ids"]):
            continue
        items.append(r)
    items.sort(key=lambda r: (r["first_date"] or "", r["result_id"]))
    return {"run_id": run.run_id, **_page(items, page, size)}


def result_detail(conn, run: Run, result_id: str) -> dict[str, Any]:
    r = run.by_id.get(result_id)
    if r is None:
        raise LedgerSpyError("result_not_found", "reconciliation result not found", http_status=404)
    bank_map = (
        json.loads(
            conn.execute(
                "SELECT mapping FROM datasets WHERE dataset_id = ?", (run.datasets["bank"]["dataset_id"],)
            ).fetchone()[0]
        ).get("bank")
        or {}
    )
    bank_cols = {bank_map[k] for k in ("balance_column", "statement_column") if bank_map.get(k)}
    evidence = []
    for side in ("ledger", "bank"):
        ds = run.datasets[side]
        for t in r[side + "_txn_ids"]:
            row = conn.execute(
                "SELECT * FROM transactions WHERE dataset_id = ? AND txn_id = ?", (ds["dataset_id"], t)
            ).fetchone()
            canonical = {
                k: row[k]
                for k in (
                    "txn_id",
                    "source_row",
                    "posted_at",
                    "amount_minor",
                    "currency",
                    "direction",
                    "account",
                    "payer_raw",
                    "payee_raw",
                    "reference",
                    "description",
                )
            }
            if side == "bank" and canonical["account"]:
                canonical["account"] = account_label(alnum(canonical["account"]))
            extra = json.loads(row["extra"])
            normalized = {
                f: json.loads(v)
                for f, v in conn.execute(
                    "SELECT field, value FROM normalized_values WHERE dataset_id = ? AND txn_id = ? "
                    "ORDER BY field",
                    (ds["dataset_id"], t),
                )
            }
            evidence.append(
                {
                    "side": side,
                    "dataset_id": ds["dataset_id"],
                    "evidence_sha256": ds["evidence_sha256"],
                    "original_filename": ds["filename"],
                    "source_row": row["source_row"],
                    "canonical": canonical,
                    "statement_columns": {c: extra.get(c) for c in sorted(bank_cols) if c in extra}
                    if side == "bank"
                    else {},
                    "p4_normalized_values": normalized,
                }
                | (
                    {"pdf_provenance": {k: v for k, v in sorted(extra.items()) if k.startswith("pdf_")}}
                    if side == "bank" and "pdf_review" in extra
                    else {}
                )
            )
    related = [
        s
        for s in run.findings
        if s["result_id"] == result_id
        or set(s["ledger_txn_ids"]) & set(r["ledger_txn_ids"])
        or set(s["bank_txn_ids"]) & set(r["bank_txn_ids"])
    ]
    groups = [g for g in run.duplicates if g["group_id"] in set(r["duplicate_group_ids"])]
    from ledgerspy.investigations.service import decisions_on

    return {
        "run_id": run.run_id,
        "result": r,
        "source_evidence": evidence,
        "findings": related,
        "duplicate_groups": groups,
        # P15: recorded analyst decisions on this result (read-only; the computed result above is unchanged)
        "analyst_decisions": [
            d for d in decisions_on(conn, "reconciliation_result", result_id) if d["run_id"] == run.run_id
        ],
        "note": NOTE,
    }


def duplicates_page(run: Run, side: str | None, page: int, size: int, conn=None) -> dict[str, Any]:
    items = [g for g in run.duplicates if side is None or g["side"] == side]
    items = sorted(items, key=lambda g: (g["first_date"], g["group_id"]))
    # P15: analyst decisions on each group (candidates stay candidates in the run itself)
    if conn is not None:
        from ledgerspy.investigations.service import decisions_on

        items = [
            g
            | {
                "analyst_decisions": [
                    d
                    for d in decisions_on(conn, "duplicate_group", g["group_id"])
                    if d["run_id"] == run.run_id
                ]
            }
            for g in items
        ]
    return {
        "run_id": run.run_id,
        "note": "Duplicate CANDIDATES: nothing is excluded or deleted; confirming a "
        "duplicate is an analyst decision.",
        **_page(items, page, size),
    }


def findings_page(run: Run, rule: str | None, severity: str | None, page: int, size: int) -> dict[str, Any]:
    items = [
        s
        for s in run.findings
        if (rule is None or s["rule_id"] == rule) and (severity is None or s["severity"] == severity)
    ]
    return {"run_id": run.run_id, "rules": findings.RULES, "note": NOTE, **_page(items, page, size)}
