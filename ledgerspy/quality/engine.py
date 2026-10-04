"""Integrity run: checks → severities → issues (with row links) → readiness → persisted report.

Determinism: the same dataset state (canonical rows + applied transformations) and the same config always
give the same issues, issue IDs, fix IDs and `report_sha256` (computed over the report without the run ID
and timestamp).
"""

import json
import sqlite3
from collections import Counter, defaultdict
from typing import Any

from ledgerspy.canonical.ids import new_ulid
from ledgerspy.cases import audit
from ledgerspy.cases.db import transaction
from ledgerspy.config import EffectiveConfig
from ledgerspy.config.schema import QUALITY_CHECKS
from ledgerspy.errors import LedgerSpyError
from ledgerspy.hashing import canonical_json, sha256_hex, utc_now_iso
from ledgerspy.normalize import proposals as proposal_mod
from ledgerspy.normalize.service import save_proposals
from ledgerspy.normalize.view import load_view
from ledgerspy.quality import checks as c
from ledgerspy.quality import readiness, severity

RUN_KEYS = ("run_id", "created_at", "created_by")


def _issue_id(dataset_id: str, check: str, raw: c.RawIssue) -> str:
    ident = [dataset_id, check, raw.field, raw.txn_id, raw.source_row, raw.key]
    return "I-" + sha256_hex(canonical_json(ident))[:16]


def _run_checks(ctx: c.Context) -> list[c.CheckResult]:
    exact = c.exact_duplicate(ctx)
    results = [
        c.missing_required(ctx),
        c.missing_optional(ctx),
        c.invalid_value(ctx),
        c.unparseable_date(ctx),
        c.implausible_date(ctx),
        c.malformed_identifier(ctx),
        exact,
        c.near_duplicate(ctx, exact),
        c.negative_amount(ctx),
        c.zero_amount(ctx),
        c.out_of_period(ctx),
        c.balance_continuity(ctx),
        c.duplicate_source_id(ctx),
        c.unmapped_columns(ctx),
        c.field_coverage(ctx),
        c.needs_normalization(ctx),  # after the checks that may claim a fix
    ]
    surfaced: dict[str, int] = Counter()
    for r in results:
        if r.check in ("invalid_value", "unparseable_date"):
            surfaced[r.check] += len(r.issues)
    for row in ctx.rows:
        for f in ctx.settings.quality.required_fields:
            for i in ctx.view.p3(row["source_row"], f):
                if i["code"] == "missing":
                    surfaced["missing_required" if row[f] is None else "resolved_by_normalization"] += 1
                elif i["code"] == "zero_amount_no_direction":
                    surfaced["zero_amount"] += 1
    results.append(c.ingestion_issues(ctx, {k: n for k, n in sorted(surfaced.items()) if n}))
    order = {name: i for i, name in enumerate(QUALITY_CHECKS)}
    return sorted(results, key=lambda r: order[r.check])


def evaluate(
    conn: sqlite3.Connection, cfg: EffectiveConfig, dataset_id: str
) -> tuple[dict, list[dict], list]:
    """Pure evaluation (no writes): returns (report without run metadata, issues, proposals)."""
    s = cfg.settings
    view = load_view(conn, dataset_id)
    props = proposal_mod.propose(view, s)
    ctx = c.Context(view, s, {(p.txn_id, p.field): p for p in props})
    results = _run_checks(ctx)
    total = len(view.rows)
    synthetic = bool(view.synthetic)

    issues: list[dict[str, Any]] = []
    checks_out, available = [], {}
    for r in results:
        available[r.check] = r.status != "not_available"
        sev = severity.assign(r.check, r.affected_rows, total, synthetic, s.quality.severity)
        entry = {
            "check": r.check,
            "status": r.status,
            "basis": r.basis,
            "issue_count": len(r.issues),
            "affected_rows": r.affected_rows,
            "severity": sev["severity"] if r.issues else None,
            "severity_basis": sev if r.issues else None,
            "details": r.details,
        }
        checks_out.append(entry)
        for raw in r.issues:
            issues.append(
                {
                    "issue_id": _issue_id(dataset_id, r.check, raw),
                    "check": r.check,
                    "severity": sev["severity"],
                    "field": raw.field,
                    "txn_id": raw.txn_id,
                    "source_row": raw.source_row,
                    "observed": raw.observed,
                    "message": raw.message,
                    "fix_id": raw.fix_id,
                }
            )
    ids = [i["issue_id"] for i in issues]
    if len(ids) != len(set(ids)):  # pragma: no cover — guards the ID scheme
        raise LedgerSpyError("issue_id_collision", "two integrity issues received the same ID")

    scored = [readiness.ScoredIssue(i["check"], i["severity"], i["source_row"]) for i in issues]
    ready = readiness.compute(total, available, scored, s.quality.readiness)
    min_sev = s.quality.readiness.min_severity
    counted_rows = {
        i["source_row"] for i in issues if i["source_row"] and severity.at_least(i["severity"], min_sev)
    }
    any_rows = {i["source_row"] for i in issues if i["source_row"]}
    by_sev = Counter(i["severity"] for i in issues)
    rows_by_sev: dict[str, set] = defaultdict(set)
    for i in issues:
        if i["source_row"]:
            rows_by_sev[i["severity"]].add(i["source_row"])
    check_map = {e["check"]: e for e in checks_out}
    limitations = [f"{e['check']}: {e['basis']}" for e in checks_out if e["status"] == "not_available"]
    if view.synthetic:
        limitations.insert(
            0,
            "SYNTHETIC source (PaySim simulation): not a corporate ledger. Fields marked "
            "not_applicable do not exist in the simulation; they are not missing data.",
        )
    if view.time_basis == "simulation_step":
        limitations.insert(
            1, "posted_at is a simulated clock (1970-01-01 + step hours), not an accounting date."
        )
    na_fields = {
        f: d for f, d in check_map["field_coverage"]["details"].items() if d["status"] == "not_applicable"
    }
    report = {
        "dataset": {
            "dataset_id": dataset_id,
            "case_id": view.case_id,
            "evidence_sha256": view.evidence_sha256,
            "adapter": view.adapter,
            "time_basis": view.time_basis,
            "synthetic": view.synthetic,
            "case_period": [d.isoformat() if d else None for d in view.period],
        },
        "data_version": {
            "overlay_sha256": view.data_version,
            "normalized_values": len(view.overlay),
        },
        "config_sha256": cfg.sha256,
        "summary": {
            "total_rows": total,
            "clean_rows": total - len(counted_rows),
            "rows_with_issues": len(counted_rows),
            "rows_with_info_only": len(any_rows - counted_rows),
            "clean_rows_definition": f"rows without any issue of severity >= {min_sev}",
            "issues_total": len(issues),
            "issues_by_severity": {lvl: by_sev.get(lvl, 0) for lvl in severity.LEVELS},
            "rows_by_severity": {lvl: len(rows_by_sev.get(lvl, ())) for lvl in severity.LEVELS},
            "issues_by_check": {e["check"]: e["issue_count"] for e in checks_out},
            "checks_available": [e["check"] for e in checks_out if e["status"] != "not_available"],
            "checks_not_available": {
                e["check"]: e["basis"] for e in checks_out if e["status"] == "not_available"
            },
        },
        "readiness": ready,
        "missing_values": {
            "required": check_map["missing_required"]["details"],
            "optional": check_map["missing_optional"]["details"],
        },
        "duplicates": {
            "exact": check_map["exact_duplicate"]["details"],
            "near": check_map["near_duplicate"]["issue_count"],
            "source_ids": check_map["duplicate_source_id"]["issue_count"],
        },
        "format_issues": {
            "invalid_value": check_map["invalid_value"]["issue_count"],
            "unparseable_date": check_map["unparseable_date"]["issue_count"],
            "implausible_date": check_map["implausible_date"]["issue_count"],
            "malformed_identifier": check_map["malformed_identifier"]["issue_count"],
        },
        "unmapped_columns": check_map["unmapped_columns"]["details"]["columns"],
        "field_coverage": check_map["field_coverage"]["details"],
        "not_applicable_fields": na_fields,
        "checks": checks_out,
        "fixes": {
            "proposed_this_run": len(props),
            "safe": sum(1 for p in props if not p.requires_confirmation),
            "require_confirmation": sum(1 for p in props if p.requires_confirmation),
        },
        "limitations": limitations,
    }
    return report, issues, props


def run_integrity(conn: sqlite3.Connection, cfg: EffectiveConfig, dataset_id: str, actor: str) -> dict:
    report, issues, props = evaluate(conn, cfg, dataset_id)
    content_sha = sha256_hex(canonical_json(report))
    run_id = "Q-" + new_ulid()
    now = utc_now_iso()
    with transaction(conn):
        save_proposals(conn, dataset_id, props, run_id)
        status_counts = Counter(
            r[0] for r in conn.execute("SELECT status FROM fix_proposals WHERE dataset_id = ?", (dataset_id,))
        )
        report["fixes"]["all_by_status"] = {
            k: status_counts.get(k, 0) for k in ("proposed", "accepted", "rejected", "applied", "reverted")
        }
        full = {
            "run_id": run_id,
            "created_at": now,
            "created_by": actor,
            "report_sha256": content_sha,
            **report,
        }
        conn.execute(
            "INSERT INTO integrity_runs (run_id, dataset_id, created_at, created_by, config_sha256, "
            "data_version, "
            "report_sha256, readiness, status, issue_count, report) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                dataset_id,
                now,
                actor,
                cfg.sha256,
                report["data_version"]["overlay_sha256"],
                content_sha,
                report["readiness"]["score"],
                report["readiness"]["status"],
                len(issues),
                canonical_json(full).decode("utf-8"),
            ),
        )
        conn.executemany(
            "INSERT INTO integrity_issues (run_id, issue_id, dataset_id, check_code, severity, field, "
            "txn_id, "
            "source_row, observed, message, fix_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id,
                    i["issue_id"],
                    dataset_id,
                    i["check"],
                    i["severity"],
                    i["field"],
                    i["txn_id"],
                    i["source_row"],
                    canonical_json(i["observed"]).decode("utf-8"),
                    i["message"],
                    i["fix_id"],
                )
                for i in issues
            ],
        )
        audit.append(
            conn,
            actor,
            "integrity.run",
            f"dataset:{dataset_id}",
            {
                "run_id": run_id,
                "report_sha256": content_sha,
                "readiness": report["readiness"]["score"],
                "status": report["readiness"]["status"],
                "issues": len(issues),
                "data_version": report["data_version"]["overlay_sha256"],
            },
        )
    return full


def latest_report(conn: sqlite3.Connection, dataset_id: str) -> dict:
    row = conn.execute(
        "SELECT report FROM integrity_runs WHERE dataset_id = ? "
        "ORDER BY created_at DESC, run_id DESC LIMIT 1",
        (dataset_id,),
    ).fetchone()
    if row is None:
        raise LedgerSpyError(
            "not_run", "no integrity check has been run on this dataset yet", http_status=404
        )
    return json.loads(row[0])


def list_issues(
    conn: sqlite3.Connection,
    dataset_id: str,
    run_id: str | None,
    check: str | None,
    severity_: str | None,
    page: int,
    size: int,
) -> dict:
    run_id = run_id or latest_report(conn, dataset_id)["run_id"]
    where, args = ["i.run_id = ?", "i.dataset_id = ?"], [run_id, dataset_id]
    if check:
        where, args = [*where, "i.check_code = ?"], [*args, check]
    if severity_:
        where, args = [*where, "i.severity = ?"], [*args, severity_]
    clause = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) FROM integrity_issues i WHERE {clause}", args).fetchone()[0]  # noqa: S608
    rows = conn.execute(
        f"SELECT i.*, f.status AS fix_status FROM integrity_issues i LEFT JOIN fix_proposals f "  # noqa: S608
        f"ON f.fix_id = i.fix_id WHERE {clause} ORDER BY i.source_row IS NULL, i.source_row, i.check_code, "
        "i.issue_id LIMIT ? OFFSET ?",
        [*args, size, (page - 1) * size],
    ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["observed"] = json.loads(r["observed"])
        d["check"] = d.pop("check_code")
        items.append(d)
    return {"run_id": run_id, "page": page, "size": size, "total": total, "items": items}
