"""P7 dataset forensic report: rule signals and Benford analysis for one canonicalised dataset.

Inputs: the P6 feature build of the dataset (normalised view, current P4 run) and P4 issue details for rules
that consume P4 results. Nothing else is read: not labels, not `extra`, not files, not ground truth. Nothing
is written.

The report is reproducible from (dataset data version, effective configuration, feature registry, rule and
Benford versions); `report_sha256` identifies it. P9 consumes `signals` for risk fusion; this module does not
fuse.
"""

import json
import sqlite3
from collections import defaultdict
from typing import Any

from ledgerspy.canonical import SignalFamily
from ledgerspy.config.loader import EffectiveConfig
from ledgerspy.config.schema import LedgerSpyConfig
from ledgerspy.detectors.rules import RULES, RuleContext, run_rules
from ledgerspy.features import matrix
from ledgerspy.features import service as feature_service
from ledgerspy.features.data import FeatureInput, FeatureRow
from ledgerspy.features.matrix import FeatureRun
from ledgerspy.forensics import benford
from ledgerspy.forensics.models import DISABLED, EVALUATED, NOT_APPLICABLE, NOT_AVAILABLE, ForensicSignal
from ledgerspy.hashing import canonical_json, sha256_hex
from ledgerspy.normalize.view import load_view

REPORT_VERSION = 1
BENFORD_RULE = "benford_subgroup"
NOTE = (
    "Signals are reasons to review, not findings: each says what was observed and why it merits a look. "
    "Benford deviations in particular are investigative leads, never evidence of fraud. Risk fusion and "
    "tiers are P9."
)


def p4_details(conn: sqlite3.Connection, run_id: str | None, checks: tuple[str, ...]) -> dict:
    """check → txn_id → [{observed, message, severity}] for the given P4 run."""
    out: dict[str, dict[str, list[dict[str, Any]]]] = {c: defaultdict(list) for c in checks}
    if run_id is None:
        return out
    marks = ",".join("?" * len(checks))
    for check, txn, observed, message, severity in conn.execute(
        f"SELECT check_code, txn_id, observed, message, severity FROM integrity_issues "  # noqa: S608 — placeholders
        f"WHERE run_id = ? AND check_code IN ({marks}) AND txn_id IS NOT NULL ORDER BY txn_id, issue_id",
        (run_id, *checks),
    ):
        out[check][txn].append({"observed": json.loads(observed), "message": message, "severity": severity})
    return out


# ------------------------------------------------------------------------------------------------- Benford


def _fiscal_year(r: FeatureRow, start_month: int) -> str | None:
    if r.ts is None:
        return None
    first = r.ts.year if r.ts.month >= start_month else r.ts.year - 1
    return f"FY{first}-{(first + 1) % 100:02d}" if start_month != 1 else f"FY{first}"


def _dimension_keys(inp: FeatureInput, dim: str):
    """(key function, None) or (None, reason the dimension does not apply)."""
    caps = inp.capabilities
    if dim == "counterparty":
        return (lambda r: f"{r.flow}:{r.cp}" if r.cp and r.flow else None), caps.get("counterparty")
    if dim == "account":
        return (lambda r: r.account), caps.get("account_category")
    if dim == "txn_type":
        return (lambda r: r.txn_type), caps.get("txn_type")
    start = inp.cfg.calendar.fiscal_year_start_month
    return (lambda r: _fiscal_year(r, start)), caps.get("calendar_time")


def benford_section(inp: FeatureInput, run: FeatureRun, settings: LedgerSpyConfig) -> tuple[dict, list]:
    cfg = settings.benford
    if run.transactions.status["amount_minor"]["status"] != "computed":
        reason = run.transactions.status["amount_minor"]["reason"]
        na = {t: {"test": t, "status": benford.NOT_APPLICABLE, "reason": reason} for t in benford.DIGITS}
        return {"version": benford.VERSION, "dataset": na, "subgroups": {}}, []
    amounts = run.transactions.columns["amount_minor"]
    section: dict[str, Any] = {
        "version": benford.VERSION,
        "dataset": {t: benford.analyze(amounts, t, cfg) for t in benford.DIGITS},
        "subgroups": {},
    }
    signals: list[ForensicSignal] = []
    for dim in cfg.subgroups.dimensions:
        key, why = _dimension_keys(inp, dim)
        if why is not None:
            section["subgroups"][dim] = {"status": benford.NOT_APPLICABLE, "reason": why}
            continue
        groups: dict[str, list[FeatureRow]] = defaultdict(list)
        for r in inp.rows:
            k = key(r)
            if k is not None:
                groups[k].append(r)
        per_test = {}
        for test in cfg.subgroups.tests:
            minimum = max(cfg.subgroups.min_n, cfg.min_n[test])
            analyzed, skipped, skipped_rows = [], 0, 0
            for k in sorted(groups):
                rows = groups[k]
                n = sum(1 for r in rows if benford.digit_of(amounts[r.i], test, cfg)[1] is None)
                if n < minimum:
                    skipped += 1
                    skipped_rows += len(rows)
                    continue
                res = benford.analyze([amounts[r.i] for r in rows], test, cfg, population=f"{dim} = {k}")
                summary = {
                    "group": k,
                    "status": res["status"],
                    "n": res["n"],
                    **{
                        f: res.get(f)
                        for f in ("mad", "band", "chi2", "chi2_p_value", "over_represented", "reason")
                    },
                }
                analyzed.append(summary)
                if res["status"] == benford.ANALYZED and res["band"] in cfg.subgroups.signal_bands:
                    signals.append(_benford_signal(dim, k, test, res, rows, amounts, cfg))
            analyzed.sort(key=lambda g: (-(g["mad"] or 0), g["group"]))
            per_test[test] = {
                "min_n": minimum,
                "groups_total": len(groups),
                "groups_analyzed": len(analyzed),
                "groups_skipped_small": skipped,
                "rows_in_skipped_groups": skipped_rows,
                "results": analyzed,
            }
        section["subgroups"][dim] = {"status": "analyzed", "tests": per_test}
    return section, signals


def _benford_signal(dim, key, test, res, rows, amounts, cfg) -> ForensicSignal:
    digits = res["over_represented"] or [res["max_abs_z_digit"]]
    examples = [
        r.txn_id
        for r in sorted(rows, key=lambda r: (r.source_row, r.txn_id))
        if benford.digit_of(amounts[r.i], test, cfg)[0] in digits
    ][: cfg.subgroups.max_example_rows]
    return ForensicSignal(
        rule_id=BENFORD_RULE,
        rule_version=benford.VERSION,
        family=SignalFamily.DIGIT,
        subject_type="group",
        subject_id=f"benford:{test}:{dim}={key}",
        txn_ids=tuple(examples),
        severity=cfg.severity,
        strength=1.0,
        weight=cfg.weight,
        evidence={
            "dimension": dim,
            "group": key,
            "test": test,
            "n": res["n"],
            "mad": res["mad"],
            "band": res["band"],
            "chi2": res["chi2"],
            "chi2_p_value": res["chi2_p_value"],
            "over_represented": res["over_represented"],
            "under_represented": res["under_represented"],
            "sample_rows": examples,
            "value": res["mad"],
        },
        features={"amount_minor": "subgroup distribution (see Benford section)"},
        explanation=f"{test.replace('_', '-')}-digit distribution of {dim} '{key}' (n = {res['n']}) departs "
        "from "
        f"Benford's law: MAD {res['mad']:.4f} ({res['band']}); digits {', '.join(map(str, digits))} occur "
        "more often than expected. A deviation is a reason to sample these rows, not evidence of fraud.",
    )


# ------------------------------------------------------------------------------------------------- report


def analyze(
    inp: FeatureInput, run: FeatureRun, settings: LedgerSpyConfig, details: dict | None = None
) -> dict[str, Any]:
    """Run every rule and the Benford analysis on a feature build. Pure: same inputs, same report."""
    ctx = RuleContext(inp, run, settings.rules, settings.benford, details or {})
    outcomes = run_rules(ctx)
    benford_part, benford_signals = benford_section(inp, run, settings)
    signals = sorted(
        [s for o in outcomes for s in o.signals] + benford_signals,
        key=lambda s: (s.rule_id, s.subject_type, s.subject_id),
    )
    affected: dict[str, set[str]] = {"transactions": set(), "entities": set(), "groups": set()}
    for s in signals:
        affected["transactions"].update(s.txn_ids)
        if s.subject_type == "entity":
            affected["entities"].add(s.subject_id)
        elif s.subject_type == "group":
            affected["groups"].add(s.subject_id)
    by_status = defaultdict(int)
    for o in outcomes:
        by_status[o.status] += 1
    report: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "dataset": run.manifest["dataset"],
        "inputs": {
            "feature_registry_sha256": run.manifest["registry_sha256"],
            "features_config_sha256": run.manifest["features_config_sha256"],
            "transaction_matrix_sha256": run.manifest["transaction_matrix_sha256"],
            "entity_matrix_sha256": run.manifest["entity_matrix_sha256"],
            "rules_config_sha256": sha256_hex(canonical_json(settings.rules.model_dump(mode="json"))),
            "benford_config_sha256": sha256_hex(canonical_json(settings.benford.model_dump(mode="json"))),
            "rule_versions": {rid: RULES[rid].version for rid in sorted(RULES)},
            "benford_version": benford.VERSION,
        },
        "summary": {
            "rules": len(outcomes),
            "evaluated": by_status[EVALUATED],
            "not_available": by_status[NOT_AVAILABLE],
            "not_applicable": by_status[NOT_APPLICABLE],
            "disabled": by_status[DISABLED],
            "signals": len(signals),
            "signals_by_rule": dict(sorted(_count(signals).items())),
            "affected_transactions": len(affected["transactions"]),
            "affected_entities": len(affected["entities"]),
            "affected_groups": len(affected["groups"]),
            "benford_subgroup_signals": len(benford_signals),
        },
        "rules": [
            o.summary()
            | {"semantics": RULES[o.rule_id].semantics, "description": RULES[o.rule_id].description}
            for o in outcomes
        ],
        "benford": benford_part,
        "signals": [s.to_dict() for s in signals],
        "affected": {k: sorted(v) for k, v in affected.items()},
        "note": NOTE,
    }
    report["report_sha256"] = sha256_hex(canonical_json(report))
    return report


def _count(signals: list[ForensicSignal]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for s in signals:
        out[s.rule_id] += 1
    return out


def build_inputs(
    conn: sqlite3.Connection, cfg: EffectiveConfig, dataset_id: str
) -> tuple[FeatureInput, FeatureRun, dict]:
    view = load_view(conn, dataset_id)
    inp = feature_service.input_from_view(
        view, cfg, feature_service.p4_issues(conn, dataset_id, view.data_version)
    )
    run = matrix.build(inp)
    return inp, run, p4_details(conn, inp.p4_run_id, ("balance_continuity",))


def run_forensics(conn: sqlite3.Connection, cfg: EffectiveConfig, dataset_id: str) -> dict[str, Any]:
    """The dataset forensic report (read-only)."""
    inp, run, details = build_inputs(conn, cfg, dataset_id)
    return analyze(inp, run, cfg.settings, details)


__all__ = ["analyze", "build_inputs", "run_forensics"]
