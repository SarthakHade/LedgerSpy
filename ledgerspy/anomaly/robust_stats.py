"""Robust peer-group z-scores and tail percentiles over P6 feature values.

Robust z. Within a peer group of the reference population: z = (x − median) / scale, with scale = 1.4826·MAD
(consistent with σ for normal data). When MAD = 0 but values differ, the scale is 1.2533·mean absolute
deviation from the median (Iglewicz & Hoaglin's modified z). The scale never falls below the feature's
configured resolution `min_scale` (so equal values never divide by 0 and a one-day wobble in a very regular
cadence is not "unusual"). Peer dimensions are tried most specific first (the counterparty's own history, then
its GL account or transaction type); the first group with `min_group_size` reference values is used and
reported as the basis. A row without one is `insufficient_data` — no silent fallback to a global baseline.

Tail percentile. Within each flow (money out / money in) of the reference population, the mid-rank percentile
p = (values below + ½·values equal) / n. A row is in the top-q tail when p ≥ 1 − q (bottom: p ≤ q). Tied
values share one percentile, so a block of ties never inflates a tail; the reported cutoff is the smallest
(largest, for "lower") population value inside the tail.
"""

from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Callable, Hashable
from typing import Any

from ledgerspy.anomaly.eligibility import Population, check_feature, transformed_column
from ledgerspy.anomaly.models import (
    DISABLED,
    EVALUATED,
    INSUFFICIENT_DATA,
    REFERENCE_ONLY,
    ROW_INSUFFICIENT,
    SCORED,
    AnomalyResult,
    DetectorOutcome,
)
from ledgerspy.config.schema import AnomalyConfig
from ledgerspy.detectors.rules import money
from ledgerspy.features.data import FeatureInput, FeatureRow
from ledgerspy.features.matrix import FeatureRun

ROBUST_ID, ROBUST_VERSION = "robust_peer", 1
TAIL_ID, TAIL_VERSION = "tail_percentile", 1
MAD_TO_SIGMA = 1.4826  # MAD → σ for normal data
MEAN_AD_TO_SIGMA = 1.2533  # √(π/2): mean absolute deviation → σ for normal data
# Peer groups always compare like with like: the same flow (out / in) as well as the named dimension.
PEER_DESCRIPTION = {
    "counterparty": "same counterparty and flow",
    "account": "same GL/category account and flow",
    "txn_type": "same transaction type and flow",
    "flow": "every row of the same flow",
}
# For explanations: the amount behind log_amount is shown in money.
DISPLAY = {"log_amount": "amount_minor"}
MONEY = {"amount_minor"}


def median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def center_scale(values: list[float]) -> tuple[float, float | None, str]:
    """(median, robust scale or None, method): mad, mean_ad (MAD = 0) or zero_dispersion (all equal)."""
    m = median(values)
    mad = median([abs(v - m) for v in values])
    if mad > 0:
        return m, MAD_TO_SIGMA * mad, "mad"
    mean_ad = sum(abs(v - m) for v in values) / len(values)
    if mean_ad > 0:
        return m, MEAN_AD_TO_SIGMA * mean_ad, "mean_ad"
    return m, None, "zero_dispersion"


def mid_rank_percentile(sorted_values: list[float], x: float) -> float:
    lo, hi = bisect_left(sorted_values, x), bisect_right(sorted_values, x)
    return (lo + 0.5 * (hi - lo)) / len(sorted_values)


def tail_cutoff(sorted_values: list[float], q: float, direction: str) -> float | None:
    """The population value at the tail boundary (smallest value with p ≥ 1 − q), or None if the tail is
    empty."""
    distinct = sorted(set(sorted_values), reverse=direction == "higher")
    best = None
    for v in distinct:
        p = mid_rank_percentile(sorted_values, v)
        if (p >= 1 - q) if direction == "higher" else (p <= q):
            best = v
        else:
            break
    return best


def _fmt(name: str, value: Any) -> str:
    if value is None:
        return "null"
    if name in MONEY:
        return money(value)
    return f"{value:,.4g}" if isinstance(value, float) else f"{value:,}"


def _peer_keys(inp: FeatureInput) -> dict[str, Callable[[FeatureRow], Hashable | None]]:
    """Peer dimensions this dataset supports. As in P6's peer levels, an account is a peer group only where
    it is a GL/category code and a type only where one is mapped; every group is within one flow."""
    keys: dict[str, Callable[[FeatureRow], Hashable | None]] = {
        "counterparty": lambda r: (r.cp, r.flow) if r.cp and r.flow else None,
        "flow": lambda r: r.flow,
    }
    if inp.capabilities.get("account_category") is None:
        keys["account"] = lambda r: (r.account, r.flow) if r.account and r.flow else None
    if inp.capabilities.get("txn_type") is None:
        keys["txn_type"] = lambda r: (r.txn_type, r.flow) if r.txn_type and r.flow else None
    if inp.capabilities.get("counterparty") is not None:
        keys.pop("counterparty")
    return keys


def _group_label(dim: str, key: Hashable) -> str:
    if isinstance(key, tuple):
        return f"{dim}={key[0]} ({key[1]})"
    return f"{dim}={key}"


def _features_section(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "used": [e for e in entries if e["status"] == "used"],
        "excluded": [e for e in entries if e["status"] == "excluded"],
    }


def _row_status(outcome: DetectorOutcome, pop: Population, judged: dict[int, bool]) -> list[str | None]:
    """Per row (matrix order): scored / insufficient_data / reference_only. Population exclusions (warm-up,
    no timestamp) are insufficient_data with their reason."""
    status: list[str | None] = [None] * len(pop.order)
    scoring = set(pop.scoring)
    reasons: dict[str, int] = defaultdict(int)
    for i in pop.order:
        if i in pop.excluded:
            status[i] = ROW_INSUFFICIENT
            reasons[pop.excluded[i]] += 1
        elif i not in scoring:
            status[i] = REFERENCE_ONLY
        elif judged.get(i):
            status[i] = SCORED
        else:
            status[i] = ROW_INSUFFICIENT
            reasons["no_sufficient_population"] += 1
    outcome.rows_scored = sum(1 for s in status if s == SCORED)
    outcome.rows_insufficient = sum(1 for s in status if s == ROW_INSUFFICIENT)
    outcome.rows_reference_only = sum(1 for s in status if s == REFERENCE_ONLY)
    outcome.insufficient_reasons = dict(reasons)
    return status


# ------------------------------------------------------------------------------------------- robust peer


def robust_peer(inp: FeatureInput, run: FeatureRun, pop: Population, cfg: AnomalyConfig) -> DetectorOutcome:
    rc = cfg.robust_peer
    out = DetectorOutcome(ROBUST_ID, ROBUST_VERSION, EVALUATED, rows_total=len(pop.order))
    out.parameters = {
        "min_group_size": rc.min_group_size,
        "z_threshold": rc.z_threshold,
        "scale": "max(1.4826·MAD, or 1.2533·mean absolute deviation when MAD = 0, min_scale)",
        "peer_choice": "the most specific configured peer group with ≥ min_group_size reference values",
        "reference": pop.mode,
    }
    if not rc.enabled:
        out.status = DISABLED
        return out
    if pop.status != "ok":
        out.status, out.reason = pop.status, pop.reason
        return out
    tx = run.transactions
    rows = {r.i: r for r in inp.rows}
    keys = _peer_keys(inp)
    entries, details = [], {}
    judged: dict[int, bool] = {}
    best_z: dict[int, float] = {}
    hits: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for fs in rc.features:
        entry = check_feature(fs.name, run, pop.reference, cfg.eligibility)
        entry |= {
            "transform": fs.transform,
            "direction": fs.direction,
            "peers": fs.peers,
            "min_scale": fs.min_scale,
        }
        entries.append(entry)
        if entry["status"] != "used":
            continue
        column = tx.columns[fs.name]
        tcol = transformed_column(inp, run, fs.name, fs.transform)
        shown = DISPLAY.get(fs.name, fs.name)
        shown_col = tx.columns.get(shown, column)
        peers, levels = {}, []
        for dim in fs.peers:
            if dim not in keys:
                need = "account_category" if dim == "account" else dim
                peers[dim] = {
                    "status": "not_available",
                    "reason": inp.capabilities.get(need) or "unsupported",
                }
                continue
            key = keys[dim]
            groups: dict[Hashable, list[int]] = defaultdict(list)
            for i in pop.reference:
                k = key(rows[i])
                if k is not None and column[i] is not None:
                    groups[k].append(i)
            stats = {}
            for k, members in groups.items():
                if len(members) >= rc.min_group_size:
                    m, scale, method = center_scale([tcol[i] for i in members])
                    if scale is None or scale < fs.min_scale:
                        scale, method = fs.min_scale, "min_scale"
                    shown_vals = [shown_col[i] for i in members if shown_col[i] is not None]
                    stats[k] = (len(members), m, scale, method, median(shown_vals) if shown_vals else None)
            peers[dim] = {
                "status": "evaluated",
                "description": PEER_DESCRIPTION[dim],
                "groups_total": len(groups),
                "groups_sufficient": len(stats),
                "groups_at_min_scale": sum(1 for s in stats.values() if s[3] == "min_scale"),
            }
            levels.append((dim, key, stats))
        reasons: dict[str, int] = defaultdict(int)
        basis: dict[str, int] = defaultdict(int)
        triggered = 0
        for i in pop.scoring:
            if column[i] is None:
                reasons["missing_value"] += 1
                continue
            chosen = None
            for dim, key, stats in levels:
                k = key(rows[i])
                if k is not None and k in stats:
                    chosen = (dim, k, stats[k])
                    break
            if chosen is None:
                reasons["no_sufficient_peer_group"] += 1
                continue
            dim, k, (n, m, scale, method, shown_median) = chosen
            basis[dim] += 1
            judged[i] = True
            z = (tcol[i] - m) / scale
            best_z[i] = max(best_z.get(i, 0.0), abs(z))
            fired = {"higher": z >= rc.z_threshold, "lower": z <= -rc.z_threshold}.get(
                fs.direction, abs(z) >= rc.z_threshold
            )
            if fired:
                triggered += 1
                hits[i].append(
                    {
                        "feature": fs.name,
                        "peer": dim,
                        "group": _group_label(dim, k),
                        "n": n,
                        "value": column[i],
                        "z": round(z, 4),
                        "median_transformed": round(m, 6),
                        "scale": round(scale, 6),
                        "scale_method": method,
                        "transform": fs.transform,
                        "shown_feature": shown,
                        "shown_value": shown_col[i],
                        "shown_group_median": shown_median,
                    }
                )
        details[fs.name] = {
            "peers": peers,
            "basis": dict(basis),
            "rows_insufficient": dict(sorted(reasons.items())),
            "triggered": triggered,
        }
    out.features = _features_section(entries)
    out.details = {"by_feature": details}
    if not out.features["used"]:
        out.status, out.reason = INSUFFICIENT_DATA, "no eligible feature"
    status = _row_status(out, pop, judged)
    subjects = tx.subjects
    out.scores = {
        "status": status,
        "max_abs_z": [round(best_z[i], 4) if i in best_z else None for i in range(len(subjects))],
    }
    for i in sorted(hits, key=lambda i: subjects[i]):
        hs = sorted(hits[i], key=lambda h: (-abs(h["z"]), h["feature"], h["peer"]))
        top = hs[0]
        out.results.append(
            AnomalyResult(
                detector_id=ROBUST_ID,
                detector_version=ROBUST_VERSION,
                subject_type="transaction",
                subject_id=subjects[i],
                txn_ids=(subjects[i],),
                anomaly_score=abs(top["z"]),
                percentile=None,
                tail=None,
                population={
                    "kind": "peer_group",
                    "peer": top["peer"],
                    "group": top["group"],
                    "n": top["n"],
                    "reference": pop.mode,
                },
                features={h["feature"]: h["value"] for h in hs}
                | {h["shown_feature"]: h["shown_value"] for h in hs},
                evidence={"deviations": hs, "z_threshold": rc.z_threshold},
                explanation=_robust_text(hs, rc.z_threshold),
                severity=rc.severity,
                strength=1.0,
                weight=rc.weight,
                metadata={"detector_version": ROBUST_VERSION, "reference": pop.mode},
            )
        )
    return out


def _robust_text(hs: list[dict[str, Any]], threshold: float) -> str:
    parts = []
    for h in hs[:3]:
        side = "above" if h["z"] > 0 else "below"
        parts.append(
            f"{h['shown_feature']} {_fmt(h['shown_feature'], h['shown_value'])} is {abs(h['z']):.1f} robust "
            "SDs "
            f"{side} the median of its peers ({h['group']}, n = {h['n']}, median "
            f"{_fmt(h['shown_feature'], h['shown_group_median'])})"
        )
    more = f"; {len(hs) - 3} more deviation(s) in the evidence" if len(hs) > 3 else ""
    return (
        "; ".join(parts)
        + more
        + f". Flagged at |z| ≥ {threshold:g}. Unusual relative to these peers — a reason "
        "to review, not a finding of fraud."
    )


# ------------------------------------------------------------------------------------------- tail percentile


def tail_percentile(
    inp: FeatureInput, run: FeatureRun, pop: Population, cfg: AnomalyConfig
) -> DetectorOutcome:
    tc = cfg.tail
    fractions = sorted(tc.fractions)
    out = DetectorOutcome(TAIL_ID, TAIL_VERSION, EVALUATED, rows_total=len(pop.order))
    out.parameters = {
        "fractions": fractions,
        "min_population": tc.min_population,
        "population": "flow (money out / money in) of the reference rows",
        "percentile": "mid-rank: (values below + ½·values equal) / n",
        "reference": pop.mode,
    }
    if not tc.enabled:
        out.status = DISABLED
        return out
    if pop.status != "ok":
        out.status, out.reason = pop.status, pop.reason
        return out
    tx = run.transactions
    rows = {r.i: r for r in inp.rows}
    flow = _peer_keys(inp)["flow"]
    entries, details = [], {}
    judged: dict[int, bool] = {}
    best: dict[int, float] = {}
    hits: dict[int, list[dict[str, Any]]] = defaultdict(list)
    p0 = 1 - fractions[-1]
    for fs in tc.features:
        entry = check_feature(fs.name, run, pop.reference, cfg.eligibility)
        entry["direction"] = fs.direction
        entries.append(entry)
        if entry["status"] != "used":
            continue
        column = tx.columns[fs.name]
        pops: dict[Hashable, list[float]] = defaultdict(list)
        for i in pop.reference:
            k = flow(rows[i])
            if k is not None and column[i] is not None:
                pops[k].append(float(column[i]))
        for v in pops.values():
            v.sort()
        populations = {}
        for k in sorted(pops):
            n = len(pops[k])
            populations[k] = {
                "n": n,
                "status": "evaluated" if n >= tc.min_population else INSUFFICIENT_DATA,
                "cutoffs": {str(q): tail_cutoff(pops[k], q, fs.direction) for q in fractions}
                if n >= tc.min_population
                else {},
            }
        reasons: dict[str, int] = defaultdict(int)
        triggered = 0
        for i in pop.scoring:
            k = flow(rows[i])
            if column[i] is None:
                reasons["missing_value"] += 1
                continue
            if k is None:
                reasons["no_flow"] += 1
                continue
            if k not in pops or len(pops[k]) < tc.min_population:
                reasons["population_too_small"] += 1
                continue
            judged[i] = True
            p = mid_rank_percentile(pops[k], float(column[i]))
            depth = p if fs.direction == "higher" else 1 - p
            best[i] = max(best.get(i, 0.0), depth)
            inside = [q for q in fractions if depth >= 1 - q]
            if inside:
                q = inside[0]  # the smallest tail the row is in
                triggered += 1
                hits[i].append(
                    {
                        "feature": fs.name,
                        "direction": fs.direction,
                        "population": f"flow={k}",
                        "n": len(pops[k]),
                        "value": column[i],
                        "percentile": round(p, 6),
                        "tail": q,
                        "cutoff": populations[k]["cutoffs"][str(q)],
                    }
                )
        details[fs.name] = {
            "populations": {str(k): v for k, v in populations.items()},
            "rows_insufficient": dict(sorted(reasons.items())),
            "triggered": triggered,
        }
    out.features = _features_section(entries)
    out.details = {"by_feature": details}
    if not out.features["used"]:
        out.status, out.reason = INSUFFICIENT_DATA, "no eligible feature"
    status = _row_status(out, pop, judged)
    subjects = tx.subjects
    out.scores = {
        "status": status,
        "max_tail_percentile": [round(best[i], 6) if i in best else None for i in range(len(subjects))],
    }
    for i in sorted(hits, key=lambda i: subjects[i]):
        hs = sorted(hits[i], key=lambda h: (h["tail"], -_depth(h), h["feature"]))
        top = hs[0]
        score = max(_depth(h) for h in hs)
        out.results.append(
            AnomalyResult(
                detector_id=TAIL_ID,
                detector_version=TAIL_VERSION,
                subject_type="transaction",
                subject_id=subjects[i],
                txn_ids=(subjects[i],),
                anomaly_score=round(score, 6),
                percentile=top["percentile"],
                tail=top["tail"],
                population={"kind": "flow", "group": top["population"], "n": top["n"], "reference": pop.mode},
                features={h["feature"]: h["value"] for h in hs},
                evidence={"tails": hs},
                explanation=_tail_text(hs),
                severity=tc.severity,
                strength=round(min(max((score - p0) / (1 - p0), 0.0), 1.0), 6),
                weight=tc.weight,
                metadata={"detector_version": TAIL_VERSION, "reference": pop.mode},
            )
        )
    return out


def _depth(h: dict[str, Any]) -> float:
    return h["percentile"] if h["direction"] == "higher" else 1 - h["percentile"]


def _tail_text(hs: list[dict[str, Any]]) -> str:
    parts = []
    for h in hs[:3]:
        side = "top" if h["direction"] == "higher" else "bottom"
        parts.append(
            f"{h['feature']} {_fmt(h['feature'], h['value'])} is in the {side} {h['tail']:.1%} of "
            f"{h['n']:,} rows of {h['population']} (percentile {100 * h['percentile']:.2f}; cutoff "
            f"{_fmt(h['feature'], h['cutoff'])})"
        )
    more = f"; {len(hs) - 3} more in the evidence" if len(hs) > 3 else ""
    return (
        "; ".join(parts)
        + more
        + ". Unusual relative to this population — a reason to review, not a finding of "
        "fraud."
    )


__all__ = ["center_scale", "mid_rank_percentile", "robust_peer", "tail_cutoff", "tail_percentile"]
