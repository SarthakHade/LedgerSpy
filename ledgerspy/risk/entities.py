"""Entity (counterparty) risk: transaction risk aggregated to counterparty entities.

Identity. By default an entity is P6's stream (`out:<party key>` / `in:<party key>`). With P12 resolution
(`risk.entities.grouping: resolved`), streams of the same flow whose names resolve to ONE canonical entity
through confirmed matches are aggregated together. Their entity ID then derives from `<flow>:<canonical
entity ID>`, while every other entity keeps its stream-based ID. Probable, ambiguous or conflicting
candidates are never merged. The score formula is unchanged: merging only decides which transactions belong
to the entity.

    entity score = 100 · (1 − Π_{j < top_k} (1 − r_(j) · decay^j)) r = transaction score / 100, highest
    first

Each transaction counts once and only through its own fused score — which already includes any
counterparty-level signal (round amounts, Benford subgroup) that cites it — so nothing is counted twice.
Contributions per transaction are exact in log-survival space, as in fusion. Tiers use the same thresholds;
independent sources are the union of the groups with evidence in those top transactions (the evidence behind
the score). Monetary exposure (|amount| of the entity's medium-or-higher rows) ranks entities for review but
does not inflate the score: a large but ordinary counterparty is not riskier for being large.
"""

import math
from collections import defaultdict
from typing import Any

from ledgerspy.config.schema import RiskConfig
from ledgerspy.features.data import FeatureInput
from ledgerspy.features.matrix import FeatureRun
from ledgerspy.hashing import sha256_hex
from ledgerspy.risk import calibration
from ledgerspy.risk.fusion import apportion
from ledgerspy.risk.models import TIER_RANK

# P6 entity features shown with an entity (computed values only; absent when P6 could not compute them).
ENTITY_FEATURES = (
    "ent_txn_count",
    "ent_total_amount_minor",
    "ent_median_amount_minor",
    "ent_share_of_flow",
    "ent_round_share",
    "ent_max_gap_days",
    "ent_after_hours_share",
    "ent_non_working_share",
    "ent_recent_count_ratio",
    "ent_recent_amount_ratio",
    "ent_median_to_peer_ratio",
    "ent_first_seen",
    "ent_last_seen",
)


def entity_id(stream: str) -> str:
    return "E-" + sha256_hex(stream)[:16]


def _name(mstreams, names, members, inp, group_of) -> str:
    """A merged entity's name: the name of its stream with the most transactions (ties: stream order)."""
    count = defaultdict(int)
    for r in inp.rows:
        if r.cp and r.flow:
            count[f"{r.flow}:{r.cp}"] += 1
    best = sorted(mstreams, key=lambda s: (-count[s], s))[0]
    return names.get(best, best.split(":", 1)[1])


def _resolution(mstreams: list[str], info: dict[str, dict[str, Any]], merged: bool) -> dict[str, Any] | None:
    if not info:
        return None
    first = info[mstreams[0]]
    statuses = {info[s]["resolution_status"] for s in mstreams}
    for st in ("conflicting_identity", "needs_review", "resolved", "unique"):
        if st in statuses:
            status = st
            break
    return {
        "canonical_entity_id": first["canonical_entity_id"],
        "resolution_status": status,
        "linked_records": first["linked_records"],
        "master_records": first["master_records"],
        "confidence": first["confidence"],
        "merged_streams": len(mstreams) if merged else 1,
    }


def stream_groups(inp: FeatureInput, res: Any | None) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    """stream → group key, and per stream its resolution facts (P12; identity when `res` is None)."""
    streams = sorted({f"{r.flow}:{r.cp}" for r in inp.rows if r.cp and r.flow})
    if res is None:
        return {s: s for s in streams}, {}
    info, by_ce = {}, defaultdict(list)
    for s in streams:
        flow, cp = s.split(":", 1)
        key = "l:" + cp
        ce = res.entity_of[key]
        e = res.entity(ce)
        info[s] = {
            "canonical_entity_id": ce,
            "resolution_status": res.record_status[key],
            "linked_records": e["member_count"],
            "master_records": e["master_records"],
            "confidence": e["confidence"],
        }
        by_ce[(flow, ce)].append(s)
    out = {}
    for (flow, ce), ss in by_ce.items():
        for s in ss:
            out[s] = f"{flow}:{ce}" if len(ss) > 1 else s
    return out, info


def aggregate(
    inp: FeatureInput,
    run: FeatureRun,
    rows: dict[str, dict[str, Any]],
    sources: dict[str, set[str]],
    names: dict[str, str],
    signals: list[dict[str, Any]],
    cfg: RiskConfig,
    grouping: tuple[dict[str, str], dict[str, dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    group_of, info = grouping if grouping is not None else stream_groups(inp, None)
    members: dict[str, list[str]] = defaultdict(list)
    member_streams: dict[str, list[str]] = defaultdict(list)
    for s, g in group_of.items():
        member_streams[g].append(s)
    for r in inp.rows:
        if r.cp and r.flow:
            members[group_of[f"{r.flow}:{r.cp}"]].append(r.txn_id)
    ent_matrix = run.entities
    feat_index = {s: i for i, s in enumerate(ent_matrix.subjects)}
    entity_signals: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in signals:
        if s["subject_type"] == "entity":
            entity_signals[s["subject_id"]].append(s)
        elif s["subject_type"] == "group" and s["evidence"].get("dimension") == "counterparty":
            entity_signals[s["evidence"]["group"]].append(s)
    out = []
    ec = cfg.entities
    for stream in sorted(members):
        mstreams = sorted(member_streams[stream])
        merged = len(mstreams) > 1
        txns = [rows[t] for t in members[stream]]
        ranked = sorted((t for t in txns if t["score"]), key=lambda t: (-t["score"], t["txn_id"]))[: ec.top_k]
        parts = [(t, t["score"] / 100 * ec.decay**j) for j, t in enumerate(ranked)]
        ls = [-math.log1p(-min(p, 1 - 1e-12)) for _, p in parts]
        total = sum(ls)
        score = 100 * -math.expm1(-total) if total else 0.0
        # independent sources behind the score: the groups with evidence in the transactions that form it
        groups = set().union(*(sources.get(t["txn_id"], set()) for t in ranked)) if ranked else set()
        display = round(score, 2)
        shares = apportion([score * x / total for x in ls], score) if total else []
        tier, reason = calibration.tier(display, len(groups), cfg.tiers)
        flagged = [t for t in txns if TIER_RANK[t["tier"]] >= TIER_RANK["medium"]]
        i = None if merged else feat_index.get(stream)  # P6 entity features describe single streams
        features = {}
        if i is not None:
            for f in ENTITY_FEATURES:
                if f in ent_matrix.columns and ent_matrix.columns[f][i] is not None:
                    features[f] = ent_matrix.columns[f][i]
        out.append(
            {
                "entity_id": entity_id(stream),
                "stream": stream,
                "name": _name(mstreams, names, members, inp, group_of)
                if merged
                else names.get(stream, stream.split(":", 1)[1]),
                "flow": stream.split(":", 1)[0],
                "transactions": len(txns),
                "scored_transactions": sum(1 for t in txns if t["status"] == "scored"),
                "transactions_medium_or_higher": len(flagged),
                "transactions_high_or_critical": sum(
                    1 for t in txns if TIER_RANK[t["tier"]] >= TIER_RANK["high"]
                ),
                "total_amount_minor": sum(abs(t["amount_minor"] or 0) for t in txns),
                "exposure_amount_minor": sum(abs(t["amount_minor"] or 0) for t in flagged),
                "score": display,
                "tier": tier,
                "tier_reason": reason,
                "independent_sources": len(groups),
                "source_groups": sorted(groups),
                "top_transactions": [
                    {
                        "txn_id": t["txn_id"],
                        "score": t["score"],
                        "tier": t["tier"],
                        "weight_in_entity": round(ec.decay**j, 6),
                        "contribution": c,
                    }
                    for j, ((t, _), c) in enumerate(zip(parts, shares, strict=True))
                ],
                "entity_signals": [
                    {"signal_id": s["signal_id"], "detector": s["rule_id"], "explanation": s["explanation"]}
                    for s in sorted(
                        (x for m in mstreams for x in entity_signals.get(m, [])), key=lambda s: s["signal_id"]
                    )
                ],
                "features": features,
                "member_streams": mstreams,
                "resolution": _resolution(mstreams, info, merged),
            }
        )
    ranked_entities = sorted(
        out, key=lambda e: (-TIER_RANK[e["tier"]], -e["score"], -e["exposure_amount_minor"], e["entity_id"])
    )
    for rank, e in enumerate(ranked_entities, start=1):
        e["review_rank"] = rank if e["score"] > 0 else None
    return ranked_entities
