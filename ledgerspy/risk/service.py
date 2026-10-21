"""P9 dataset risk analysis: one coherent result per dataset state, read by every risk endpoint.

One analysis builds the P6 feature matrix once, runs the P7 forensic report and the P8 anomaly report on it,
fuses their signals per transaction, aggregates entities and builds the dataset summary. Summary,
transaction list, transaction detail, entity views, detector view and scatter all read that single result,
so they never disagree.

The result is cached in memory by its identity: dataset, normalised data version, latest P4 run, effective
configuration hash, feature-registry hash and the risk / detector versions. Any change to one of them is a
different result. Nothing is written to the database.

Inputs are canonical data only: labels, `extra` columns and ground truth are never read.
"""

import json
import math
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from ledgerspy.anomaly import service as anomaly_service
from ledgerspy.cache import NamespacedLRU, namespace
from ledgerspy.config.loader import ConfigError, EffectiveConfig
from ledgerspy.config.schema import LedgerSpyConfig
from ledgerspy.features import matrix
from ledgerspy.features import service as feature_service
from ledgerspy.features.data import FeatureInput
from ledgerspy.features.matrix import FeatureRun, registry_sha256
from ledgerspy.features.resolution import resolution_of
from ledgerspy.forensics import service as forensic_service
from ledgerspy.hashing import canonical_json, sha256_hex
from ledgerspy.normalize.view import DatasetView, data_version, load_view, overlay_rows
from ledgerspy.resolution.masters import records_sha256
from ledgerspy.risk import calibration, entities, explain
from ledgerspy.risk.fusion import Fused, SignalInput, fuse
from ledgerspy.risk.models import INSUFFICIENT, NOT_PROBABILITY, RISK_VERSION, SCORED, TIER_RANK, TIERS

SCORE_BINS = 10
SCATTER_MAX_POINTS = 2500
ANOMALY_STATUS = ("isolation_forest", "robust_peer", "tail_percentile")


@dataclass
class RiskResult:
    identity: dict[str, Any]
    summary: dict[str, Any]
    rows: list[dict[str, Any]]  # every transaction, in source-row order
    by_txn: dict[str, dict[str, Any]]
    fused: dict[str, Fused]
    signals: dict[str, dict[str, Any]]
    row_insufficient: dict[str, list[str]]
    unavailable: list[dict[str, str]]
    entities: list[dict[str, Any]]  # review order
    by_entity: dict[str, dict[str, Any]]
    detectors: dict[str, Any]
    scatter: dict[str, Any]
    result_sha256: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------------------------------------ helpers


def _amount_text(minor: int | None, currency: str | None, settings: LedgerSpyConfig) -> str | None:
    """Exact decimal text of a minor-unit amount (no float rounding)."""
    if minor is None:
        return None
    mu = settings.ingest.minor_units
    digits = mu.by_currency.get(currency or "", mu.default_digits)
    sign = "-" if minor < 0 else ""
    q, r = divmod(abs(minor), 10**digits) if digits else (abs(minor), 0)
    return f"{sign}{q}.{r:0{digits}d}" if digits else f"{sign}{q}"


def _family_of(settings: LedgerSpyConfig) -> dict[str, str]:
    return {d: name for name, fam in settings.risk.families.items() for d in fam.detectors}


def _attached(signal: dict[str, Any]) -> list[str]:
    """Transactions a signal raises: a transaction signal raises its subject (other rows it cites are context,
    e.g. the original of a duplicate); an entity or group signal raises every row it cites."""
    return [signal["subject_id"]] if signal["subject_type"] == "transaction" else list(signal["txn_ids"])


def _readiness(conn: sqlite3.Connection, dataset_id: str, version: str) -> dict[str, Any]:
    run = conn.execute(
        "SELECT run_id, created_at, readiness, status, issue_count FROM integrity_runs WHERE dataset_id = "
        "? AND data_version = ? ORDER BY created_at DESC, run_id DESC LIMIT 1",
        (dataset_id, version),
    ).fetchone()
    if run is None:
        return {
            "status": "not_run",
            "note": "No P4 integrity check has been run on the current data version; data-quality "
            "detectors that need it are not available.",
        }
    return {
        "status": run["status"],
        "score": run["readiness"],
        "issue_count": run["issue_count"],
        "run_id": run["run_id"],
        "created_at": run["created_at"],
        "note": "Data readiness is reported separately from risk: poor data quality is never scored as "
        "fraud risk.",
    }


# ------------------------------------------------------------------------------------------------ analysis


def analyze(
    view: DatasetView,
    inp: FeatureInput,
    run: FeatureRun,
    forensic: dict[str, Any],
    anomaly: dict[str, Any],
    settings: LedgerSpyConfig,
    readiness: dict[str, Any],
) -> RiskResult:
    """Fuse one dataset's P7 and P8 reports. Pure: the same inputs give the same result."""
    rc = settings.risk
    family_of = _family_of(settings)
    signals: dict[str, dict[str, Any]] = {}
    for s in [*forensic["signals"], *anomaly["signals"]]:
        if s["rule_id"] not in family_of:
            raise ConfigError(
                f"detector {s['rule_id']} is not assigned to a risk family (config risk.families)"
            )
        signals[s["signal_id"]] = s
    by_txn_family: dict[str, dict[str, list[SignalInput]]] = defaultdict(lambda: defaultdict(list))
    for sid in sorted(signals):
        s = signals[sid]
        item = SignalInput(sid, s["rule_id"], float(s["strength"]) * float(s["weight"]))
        for t in _attached(s):
            by_txn_family[t][family_of[s["rule_id"]]].append(item)

    # detector coverage: dataset level (P7 rules, P8 detectors) and row level (P8 statuses)
    unavailable = [
        {"detector": r["rule_id"], "status": r["status"], "reason": r["reason"] or r["status"]}
        for r in forensic["rules"]
        if r["status"] != "evaluated"
    ] + [
        {"detector": d["detector_id"], "status": d["status"], "reason": d["reason"] or d["status"]}
        for d in anomaly["detectors"]
        if d["status"] != "evaluated"
    ]
    benford_ok = any(v.get("status") == "analyzed" for v in forensic["benford"]["dataset"].values())
    p7_evaluated = any(r["status"] == "evaluated" for r in forensic["rules"]) or benford_ok
    a_scores = anomaly["scores"]
    a_index = {t: i for i, t in enumerate(a_scores["txn_ids"])}
    row_insufficient: dict[str, list[str]] = {}
    row_scored_by_anomaly: dict[str, bool] = {}
    for t, i in a_index.items():
        bad, good = [], False
        for d in ANOMALY_STATUS:
            col = (a_scores.get(d) or {}).get("status")
            st = col[i] if col else None
            if st == "scored":
                good = True
            elif st == "insufficient_data":
                bad.append(d)
        row_insufficient[t] = bad
        row_scored_by_anomaly[t] = good

    rows: list[dict[str, Any]] = []
    fused_by: dict[str, Fused] = {}
    sources: dict[str, set[str]] = {}
    for vr in view.rows:
        t = vr["txn_id"]
        status = SCORED if (p7_evaluated or row_scored_by_anomaly.get(t)) else INSUFFICIENT
        fam = by_txn_family.get(t)
        if fam:
            f = fuse(fam, rc)
            fused_by[t] = f
            score, n_src = f.score_display, f.sources
            sources[t] = {g.group for g in f.groups if g.capped > 0}
        else:
            score, n_src = 0.0, 0
        tier, reason = (
            calibration.tier(score, n_src, rc.tiers) if status == SCORED else ("low", "not assessed")
        )
        contribs = explain.contributors(f, signals) if fam else []
        flow = (
            "out"
            if vr.get("direction") in ("debit", "out")
            else "in"
            if vr.get("direction") in ("credit", "in")
            else None
        )
        rows.append(
            {
                "txn_id": t,
                "source_row": vr["source_row"],
                "posted_at": vr.get("posted_at"),
                "amount_minor": vr.get("amount_minor"),
                "amount": _amount_text(vr.get("amount_minor"), vr.get("currency"), settings),
                "currency": vr.get("currency"),
                "direction": vr.get("direction"),
                "flow": flow,
                "payer": vr.get("payer_raw"),
                "payee": vr.get("payee_raw"),
                "reference": vr.get("reference"),
                "account": vr.get("account"),
                "txn_type": vr.get("txn_type"),
                "status": status,
                "score": score if status == SCORED else None,
                "tier": tier,
                "tier_reason": reason,
                "independent_sources": n_src,
                "key_findings": [
                    {
                        "detector": c["detector"],
                        "label": c["detector_label"],
                        "contribution": c["contribution"],
                    }
                    for c in contribs[:3]
                ],
                "families": sorted(  # largest contribution first
                    (
                        {"family": x.family, "label": x.label, "contribution": x.contribution_display}
                        for x in (f.families if fam else [])
                        if x.scored and x.contribution_display > 0
                    ),
                    key=lambda x: (-x["contribution"], x["family"]),
                ),
                "data_quality_flags": len(explain.data_quality_flags(f, signals)) if fam else 0,
            }
        )
    review = calibration.review(rows, rc.review)
    by_txn = {r["txn_id"]: r for r in rows}

    # entities
    names: dict[str, Counter] = defaultdict(Counter)
    for fr, vr in zip(inp.rows, view.rows, strict=True):
        if fr.cp and fr.flow:
            raw = vr.get("payee_raw") if fr.cp == fr.payee else vr.get("payer_raw")
            if raw:
                names[f"{fr.flow}:{fr.cp}"][raw] += 1
    best_names = {k: sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] for k, c in names.items()}
    res_er = resolution_of(inp) if inp.available(("counterparty",)) is None else None
    grouping = entities.stream_groups(inp, res_er if rc.entities.grouping == "resolved" else None)
    if rc.entities.grouping == "name" and res_er is not None:  # annotate, never merge
        grouping = ({s: s for s in grouping[0]}, entities.stream_groups(inp, res_er)[1])
    ent = entities.aggregate(inp, run, by_txn, sources, best_names, list(signals.values()), rc, grouping)
    currencies = sorted({r["currency"] for r in rows if r["currency"]})
    currency = currencies[0] if len(currencies) == 1 else None
    for e in ent:  # exact amount text for display (single-currency datasets only)
        e["currency"] = currency
        e["exposure"] = _amount_text(e["exposure_amount_minor"], currency, settings) if currency else None
        e["total_amount"] = _amount_text(e["total_amount_minor"], currency, settings) if currency else None
    by_entity = {e["entity_id"]: e for e in ent}
    stream_to_entity = {s: e["entity_id"] for e in ent for s in e["member_streams"]}
    for fr in inp.rows:
        by_txn[fr.txn_id]["entity_id"] = (
            stream_to_entity.get(f"{fr.flow}:{fr.cp}") if fr.cp and fr.flow else None
        )

    detectors = _detectors(forensic, anomaly, signals, rows, family_of, settings)
    summary = _summary(view, inp, run, rows, ent, detectors, review, readiness, forensic, anomaly, settings)
    merged = [e for e in ent if len(e["member_streams"]) > 1]
    summary["entity_grouping"] = {
        "method": rc.entities.grouping,
        "description": "streams whose names resolve to one canonical entity through confirmed matches are "
        "aggregated together (P12); probable, ambiguous and conflicting candidates are never merged"
        if rc.entities.grouping == "resolved"
        else "one entity per counterparty stream (P6 party key); resolution shown, never applied",
        "entities": len(ent),
        "streams": sum(len(e["member_streams"]) for e in ent),
        "merged_entities": len(merged),
        "streams_in_merged_entities": sum(len(e["member_streams"]) for e in merged),
        "resolution_sha256": res_er.sha256 if res_er is not None else None,
        "master_records": res_er.stats["records_by_source"].get("master", 0) if res_er is not None else 0,
    }
    scatter = _scatter(rows, anomaly)
    res = RiskResult(
        identity={},
        summary=summary,
        rows=rows,
        by_txn=by_txn,
        fused=fused_by,
        signals=signals,
        row_insufficient=row_insufficient,
        unavailable=unavailable,
        entities=ent,
        by_entity=by_entity,
        detectors=detectors,
        scatter=scatter,
    )
    res.result_sha256 = sha256_hex(canonical_json({"summary": summary, "rows": rows, "entities": ent}))
    summary["result_sha256"] = res.result_sha256
    return res


def _detectors(forensic, anomaly, signals, rows, family_of, settings) -> dict[str, Any]:
    rc = settings.risk
    per_detector_rows: dict[str, set[str]] = defaultdict(set)
    per_detector_signals: Counter = Counter()
    for s in signals.values():
        per_detector_signals[s["rule_id"]] += 1
        per_detector_rows[s["rule_id"]].update(_attached(s))
    items = []
    for r in forensic["rules"]:
        items.append(
            {
                "detector": r["rule_id"],
                "source": "P7 forensic rule",
                "version": r["version"],
                "status": r["status"],
                "reason": r["reason"],
                "description": r["description"],
                "family": family_of[r["rule_id"]],
                "family_label": rc.families[family_of[r["rule_id"]]].label,
                "scored": rc.families[family_of[r["rule_id"]]].scored,
                "evaluated": r["evaluated"],
                "insufficient": r["insufficient"],
                "signals": per_detector_signals[r["rule_id"]],
                "transactions_raised": len(per_detector_rows[r["rule_id"]]),
            }
        )
    items.append(
        {
            "detector": "benford_subgroup",
            "source": "P7 Benford analysis",
            "version": forensic["benford"]["version"],
            "status": "evaluated"
            if any(v.get("status") == "analyzed" for v in forensic["benford"]["dataset"].values())
            else "not_available",
            "reason": "",
            "description": "First / first-two / last-two digit tests; subgroup deviations are sampling "
            "leads, not proof.",
            "family": family_of["benford_subgroup"],
            "family_label": rc.families[family_of["benford_subgroup"]].label,
            "scored": rc.families[family_of["benford_subgroup"]].scored,
            "signals": per_detector_signals["benford_subgroup"],
            "transactions_raised": len(per_detector_rows["benford_subgroup"]),
        }
    )
    for d in anomaly["detectors"]:
        items.append(
            {
                "detector": d["detector_id"],
                "source": "P8 anomaly detector",
                "version": d["version"],
                "status": d["status"],
                "reason": d["reason"],
                "description": d["parameters"].get("score_direction")
                or d["parameters"].get("percentile")
                or d["parameters"].get("scale", ""),
                "family": family_of[d["detector_id"]],
                "family_label": rc.families[family_of[d["detector_id"]]].label,
                "scored": rc.families[family_of[d["detector_id"]]].scored,
                "rows_scored": d["rows_scored"],
                "rows_insufficient": d["rows_insufficient"],
                "insufficient_reasons": d["insufficient_reasons"],
                "features_used": [f["feature"] for f in d["features"].get("used", [])],
                "features_excluded": [
                    {"feature": f["feature"], "reason": f["reason"]}
                    for f in d["features"].get("excluded", [])
                ],
                "signals": per_detector_signals[d["detector_id"]],
                "transactions_raised": len(per_detector_rows[d["detector_id"]]),
            }
        )
    families = []
    top_counter = Counter(r["families"][0]["family"] for r in rows if r["families"])
    for name, fam in rc.families.items():
        dets = [i for i in items if i["family"] == name]
        families.append(
            {
                "family": name,
                "label": fam.label,
                "group": fam.group,
                "cap": fam.cap,
                "group_cap": rc.groups[fam.group].cap if fam.group in rc.groups else None,
                "scored": fam.scored,
                "detectors": [i["detector"] for i in dets],
                "signals": sum(i["signals"] for i in dets),
                "transactions_raised": len(set().union(*(per_detector_rows[i["detector"]] for i in dets))),
                "transactions_led": top_counter[name],
            }
        )
    benford = forensic["benford"]
    return {
        "items": items,
        "families": families,
        "benford": {
            "version": benford["version"],
            "dataset": benford["dataset"],
            "subgroups": benford["subgroups"],
            "note": "A Benford deviation is an investigative lead for sampling, never evidence of fraud.",
        },
        "population": anomaly["population"],
    }


def _histogram(scores: list[float]) -> list[dict[str, Any]]:
    out = []
    for b in range(SCORE_BINS):
        lo, hi = b * 100 / SCORE_BINS, (b + 1) * 100 / SCORE_BINS
        n = sum(1 for s in scores if lo <= s < hi or (b == SCORE_BINS - 1 and s == 100))
        out.append({"from": lo, "to": hi, "count": n})
    return out


def _monthly(view: DatasetView, inp: FeatureInput, rows: list[dict[str, Any]]) -> dict[str, Any]:
    reason = inp.available(("calendar_time",))
    if reason is not None:
        return {"status": "not_available", "reason": reason, "months": []}
    by_month: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        if r["posted_at"]:
            by_month[str(r["posted_at"])[:7]][
                r["tier"] if r["status"] == SCORED else "insufficient_data"
            ] += 1
    return {
        "status": "available",
        "months": [
            {"month": m, **{t: c.get(t, 0) for t in (*TIERS, "insufficient_data")}}
            for m, c in sorted(by_month.items())
        ],
    }


def _summary(
    view, inp, run, rows, ent, detectors, review, readiness, forensic, anomaly, settings
) -> dict[str, Any]:
    scored = [r for r in rows if r["status"] == SCORED]
    tiers = {t: sum(1 for r in scored if r["tier"] == t) for t in TIERS}
    amount = lambda rs: sum(abs(r["amount_minor"] or 0) for r in rs)  # noqa: E731
    high = [r for r in scored if TIER_RANK[r["tier"]] >= TIER_RANK["high"]]
    medium_up = [r for r in scored if TIER_RANK[r["tier"]] >= TIER_RANK["medium"]]
    currencies = sorted({r["currency"] for r in rows if r["currency"]})
    currency = currencies[0] if len(currencies) == 1 else None
    queue = sorted(
        (r for r in rows if r["review_priority"] == "review_queue"), key=lambda r: r["review_rank"]
    )
    top_findings = []
    for r in queue[:10]:
        top_findings.append(
            {
                "kind": "transaction",
                "txn_id": r["txn_id"],
                "score": r["score"],
                "tier": r["tier"],
                "amount": r["amount"],
                "currency": r["currency"],
                "payee": r["payee"],
                "key_findings": r["key_findings"],
            }
        )
    group_findings = [
        {
            "kind": s["subject_type"],
            "detector": s["rule_id"],
            "subject_id": s["subject_id"],
            "rows_cited": len(s["txn_ids"]),
            "explanation": s["explanation"],
        }
        for s in sorted(
            (s for s in forensic["signals"] if s["subject_type"] in ("entity", "group")),
            key=lambda s: (s["rule_id"], s["subject_id"]),
        )
    ]
    unavailable_count = sum(1 for d in detectors["items"] if d["status"] != "evaluated")
    return {
        "risk_version": RISK_VERSION,
        "dataset": {
            "dataset_id": view.dataset_id,
            "adapter": view.adapter,
            "time_basis": view.time_basis,
            "data_version": view.data_version,
            "p4_run_id": inp.p4_run_id,
        },
        "method": {
            "score": "0–100 review-risk score: noisy-OR fusion of P7 rule and P8 anomaly signals with "
            "family and group caps (config risk.*). " + NOT_PROBABILITY,
            "tiers": {
                "medium": settings.risk.tiers.medium,
                "high": settings.risk.tiers.high,
                "critical": settings.risk.tiers.critical,
                "min_sources_high": settings.risk.tiers.min_sources_high,
                "min_sources_critical": settings.risk.tiers.min_sources_critical,
            },
            "families": detectors["families"],
        },
        "counts": {
            "transactions": len(rows),
            "scored": len(scored),
            "insufficient_data": len(rows) - len(scored),
            "with_signals": sum(1 for r in scored if r["score"]),
            "tiers": tiers,
            "medium_or_higher": len(medium_up),
            "high_or_critical": len(high),
            "detectors_unavailable": unavailable_count,
            "detectors_total": len(detectors["items"]),
        },
        "amounts": {
            "currency": currency,
            "currencies": currencies,
            "total_minor": amount(rows),
            "total": _amount_text(amount(rows), currency, settings) if currency else None,
            "high_or_critical_minor": amount(high),
            "high_or_critical": _amount_text(amount(high), currency, settings) if currency else None,
            "medium_or_higher_minor": amount(medium_up),
            "medium_or_higher": _amount_text(amount(medium_up), currency, settings) if currency else None,
            "high_or_critical_average": _amount_text(amount(high) // len(high), currency, settings)
            if currency and high
            else None,
        },
        "entities": {
            "total": len(ent),
            "medium_or_higher": sum(1 for e in ent if TIER_RANK[e["tier"]] >= TIER_RANK["medium"]),
            "high_or_critical": sum(1 for e in ent if TIER_RANK[e["tier"]] >= TIER_RANK["high"]),
            "with_high_or_critical_transactions": sum(1 for e in ent if e["transactions_high_or_critical"]),
            "top": [
                {
                    k: e[k]
                    for k in (
                        "entity_id",
                        "name",
                        "flow",
                        "score",
                        "tier",
                        "transactions",
                        "transactions_medium_or_higher",
                        "exposure_amount_minor",
                    )
                }
                | {
                    "exposure": _amount_text(e["exposure_amount_minor"], currency, settings)
                    if currency
                    else None
                }
                for e in ent[:10]
            ],
        },
        "volume_guard": review,
        "score_distribution": _histogram([r["score"] for r in scored]),
        "monthly": _monthly(view, inp, rows),
        "readiness": readiness,
        "top_findings": top_findings,
        "dataset_findings": group_findings,
        "detectors_unavailable": [
            {"detector": d["detector"], "status": d["status"], "reason": d["reason"]}
            for d in detectors["items"]
            if d["status"] != "evaluated"
        ],
        "inputs": {
            "forensic_report_sha256": forensic["report_sha256"],
            "anomaly_report_sha256": anomaly["report_sha256"],
            "feature_registry_sha256": run.manifest["registry_sha256"],
            "risk_config_sha256": sha256_hex(canonical_json(settings.risk.model_dump(mode="json"))),
            "labels_read": False,
        },
    }


def _scatter(rows: list[dict[str, Any]], anomaly: dict[str, Any]) -> dict[str, Any]:
    """Every row with a positive score, plus an even stride of the zero-score rows up to the point budget."""
    a = anomaly["scores"]
    if_col = (a.get("isolation_forest") or {}).get("score")
    if_score = dict(zip(a["txn_ids"], if_col, strict=True)) if if_col else {}
    pos = [r for r in rows if r["status"] == SCORED and r["score"]]
    zero = [r for r in rows if r["status"] == SCORED and not r["score"] and r["amount_minor"] is not None]
    room = max(SCATTER_MAX_POINTS - len(pos), 0)
    stride = math.ceil(len(zero) / room) if room and len(zero) > room else 1
    keep = zero[::stride] if room else []
    pts = [
        {
            "txn_id": r["txn_id"],
            "amount_minor": r["amount_minor"],
            "amount": r["amount"],
            "score": r["score"],
            "tier": r["tier"],
            "isolation_forest_score": if_score.get(r["txn_id"]),
        }
        for r in sorted(pos + keep, key=lambda r: r["source_row"])
        if r["amount_minor"] is not None
    ]
    return {
        "points": pts,
        "rows_with_score": len(pos),
        "zero_score_rows": len(zero),
        "zero_score_rows_shown": len(keep),
        "sampling": f"every row with a positive score; every {stride}th zero-score row (deterministic)",
    }


# ------------------------------------------------------------------------------------------------ cache + run

_CACHE_SIZE = 4
_CACHE = NamespacedLRU(_CACHE_SIZE)  # per database (ledgerspy.cache)


def identity(conn: sqlite3.Connection, cfg: EffectiveConfig, dataset_id: str) -> dict[str, Any]:
    version = data_version(overlay_rows(conn, dataset_id))
    run = conn.execute(
        "SELECT run_id FROM integrity_runs WHERE dataset_id = ? AND data_version = ? ORDER BY created_at "
        "DESC, run_id DESC LIMIT 1",
        (dataset_id, version),
    ).fetchone()
    return {
        "dataset_id": dataset_id,
        "data_version": version,
        "p4_run_id": run[0] if run else None,
        "config_sha256": cfg.sha256,
        "feature_registry_sha256": registry_sha256(),
        "risk_version": RISK_VERSION,
        "detector_versions": json.dumps(anomaly_service.DETECTOR_VERSIONS, sort_keys=True),
        "masters_sha256": records_sha256(feature_service.masters_for(conn, dataset_id)),
    }


def compute(conn: sqlite3.Connection, cfg: EffectiveConfig, dataset_id: str) -> RiskResult:
    view = load_view(conn, dataset_id)
    inp = feature_service.input_for(conn, cfg, view)
    run = matrix.build(inp)
    details = forensic_service.p4_details(conn, inp.p4_run_id, ("balance_continuity",))
    forensic = forensic_service.analyze(inp, run, cfg.settings, details)
    anomaly = anomaly_service.analyze(inp, run, cfg.settings)
    return analyze(
        view, inp, run, forensic, anomaly, cfg.settings, _readiness(conn, dataset_id, view.data_version)
    )


def run_risk(conn: sqlite3.Connection, cfg: EffectiveConfig, dataset_id: str) -> RiskResult:
    """The dataset's risk result for its current state (cached by identity; read-only)."""
    ident = identity(conn, cfg, dataset_id)
    key = sha256_hex(canonical_json(ident))
    ns = namespace(conn)
    hit = _CACHE.get(ns, key)
    if hit is not None:
        return hit
    res = compute(conn, cfg, dataset_id)
    res.identity = ident | {"cache_key": key}
    res.summary["identity"] = res.identity
    _CACHE.put(ns, key, res)
    return res


def clear_cache() -> None:
    _CACHE.clear()


# ------------------------------------------------------------------------------------------------ views


def transactions_page(
    res: RiskResult, page: int, size: int, tier: str | None, q: str | None, sort: str
) -> dict[str, Any]:
    rows = res.rows
    if tier == "insufficient_data":
        rows = [r for r in rows if r["status"] == INSUFFICIENT]
    elif tier:
        rows = [r for r in rows if r["status"] == SCORED and r["tier"] == tier]
    if q:
        needle = q.casefold()
        rows = [
            r
            for r in rows
            if any(needle in str(r[k]).casefold() for k in ("txn_id", "payer", "payee", "reference") if r[k])
        ]
    if sort == "priority":
        rows = sorted(rows, key=lambda r: (r["review_rank"] is None, r["review_rank"] or 0, r["source_row"]))
    elif sort == "amount":
        rows = sorted(rows, key=lambda r: (-(abs(r["amount_minor"] or 0)), r["source_row"]))
    elif sort == "score":
        rows = sorted(rows, key=lambda r: (-(r["score"] or 0), r["source_row"]))
    total = len(rows)
    start = (page - 1) * size
    return {
        "page": page,
        "size": size,
        "total": total,
        "pages": max(1, math.ceil(total / size)),
        "sort": sort,
        "tier": tier,
        "q": q,
        "items": rows[start : start + size],
        "result_sha256": res.result_sha256,
    }


def transaction_detail(res: RiskResult, txn_id: str, settings: LedgerSpyConfig) -> dict[str, Any] | None:
    row = res.by_txn.get(txn_id)
    if row is None:
        return None
    f = res.fused.get(txn_id)
    contribs = explain.contributors(f, res.signals) if f else []
    fused = f.to_dict() if f else {"score": 0.0, "independent_sources": 0, "groups": [], "families": []}
    insufficient = res.row_insufficient.get(txn_id, []) if row["status"] == SCORED else []
    entity = res.by_entity.get(row.get("entity_id") or "")
    return {
        "transaction": row,
        "explanation": {
            "subject_id": txn_id,
            "score": row["score"],
            "tier": row["tier"],
            "tier_reason": row["tier_reason"],
            "independent_sources": row["independent_sources"],
            "headline": explain.headline(row["score"] or 0.0, row["tier"], row["independent_sources"], f)
            if f
            else f"Review-risk score {row['score'] or 0:.2f} ({row['tier']}): no detector raised a signal "
            "for this row."
            if row["status"] == SCORED
            else "Not assessed: no detector could evaluate this row.",
            "contributors": contribs[: settings.risk.max_contributors],
            "contributors_total": len(contribs),
            "all_contributors": contribs,
            "data_quality_flags": explain.data_quality_flags(f, res.signals) if f else [],
            "limitations": explain.limitations(
                res.unavailable, insufficient, f or Fused(0.0, 0.0, [], []), row["tier_reason"]
            ),
        },
        "breakdown": fused,
        "entity": {k: entity[k] for k in ("entity_id", "name", "flow", "score", "tier", "transactions")}
        if entity
        else None,
        "result_sha256": res.result_sha256,
    }


def entities_page(res: RiskResult, page: int, size: int, tier: str | None, q: str | None) -> dict[str, Any]:
    items = res.entities
    if tier:
        items = [e for e in items if e["tier"] == tier]
    if q:
        needle = q.casefold()
        items = [e for e in items if needle in e["name"].casefold() or needle in e["stream"]]
    total = len(items)
    start = (page - 1) * size
    return {
        "page": page,
        "size": size,
        "total": total,
        "pages": max(1, math.ceil(total / size)),
        "items": [{k: v for k, v in e.items() if k != "features"} for e in items[start : start + size]],
        "result_sha256": res.result_sha256,
    }


def entity_detail(res: RiskResult, entity_id: str) -> dict[str, Any] | None:
    e = res.by_entity.get(entity_id)
    if e is None:
        return None
    txns = sorted(
        (r for r in res.rows if r.get("entity_id") == entity_id),
        key=lambda r: (r["review_rank"] is None, r["review_rank"] or 0, r["source_row"]),
    )
    return {
        "entity": e,
        "aggregation": "entity score = noisy-OR of its top transaction scores with diminishing returns "
        "(config risk.entities); each transaction counts once. " + NOT_PROBABILITY,
        "transactions": txns[:200],
        "transactions_total": len(txns),
        "result_sha256": res.result_sha256,
    }


__all__ = ["RiskResult", "analyze", "clear_cache", "compute", "run_risk"]
