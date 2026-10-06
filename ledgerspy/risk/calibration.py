"""Tiers and the review-volume guard.

No probability calibration happens here (that needs confirmed outcomes: P10/P11). "Calibration" in this
module
means keeping the review workload honest:

Tier. `critical` needs score ≥ tiers.critical AND evidence from ≥ min_sources_critical independent sources;
`high` needs score ≥ tiers.high AND ≥ min_sources_high sources; `medium` needs score ≥ tiers.medium; else
`low`. Thresholds compare the displayed (hundredths) score, so what is shown and the tier always agree.

Review priority. Rows with a positive score are ranked by (tier, score, independent sources, |amount|,
txn_id), highest first. The review queue is the first `review.top_k` ranked rows of tier medium or above.
The volume guard compares the raw high + critical count with `max_high_share` (and critical with
`max_critical_share`) of scored rows; exceeding either raises `calibration_warning`. Raw scores and tiers
are never changed by the guard.
"""

import math
from typing import Any

from ledgerspy.config.schema import RiskReview, RiskTiers
from ledgerspy.risk.models import TIER_RANK


def tier(score: float, sources: int, cfg: RiskTiers) -> tuple[str, str]:
    """(tier, reason) for a displayed score and its number of independent sources."""
    if score >= cfg.critical and sources >= cfg.min_sources_critical:
        return "critical", f"score ≥ {cfg.critical:g} with {sources} independent sources"
    if score >= cfg.high and sources >= cfg.min_sources_high:
        note = (
            f"; critical needs {cfg.min_sources_critical} independent sources"
            if score >= cfg.critical
            else ""
        )
        return "high", f"score ≥ {cfg.high:g} with {sources} independent sources{note}"
    if score >= cfg.medium:
        note = (
            f"; high needs corroboration from {cfg.min_sources_high} independent sources, this has {sources}"
            if score >= cfg.high
            else ""
        )
        return "medium", f"score ≥ {cfg.medium:g}{note}"
    return "low", f"score below {cfg.medium:g}"


def priority_key(row: dict[str, Any]) -> tuple:
    return (
        -TIER_RANK[row["tier"]],
        -(row["score"] or 0.0),
        -row["independent_sources"],
        -abs(row["amount_minor"] or 0),
        row["txn_id"],
    )


def review(rows: list[dict[str, Any]], cfg: RiskReview) -> dict[str, Any]:
    """Assign review_rank / review_priority in place; return the volume-guard report."""
    scored = [r for r in rows if r["status"] == "scored"]
    ranked = sorted((r for r in scored if r["score"]), key=priority_key)
    queue = 0
    for rank, r in enumerate(ranked, start=1):
        r["review_rank"] = rank
        if TIER_RANK[r["tier"]] >= TIER_RANK["medium"] and queue < cfg.top_k:
            queue += 1
            r["review_priority"] = "review_queue"
        elif TIER_RANK[r["tier"]] >= TIER_RANK["high"]:
            r["review_priority"] = "over_capacity"
        else:
            r["review_priority"] = "standard"
    for r in rows:
        r.setdefault("review_rank", None)
        r.setdefault("review_priority", "none")
    n = len(scored)
    counts = {t: sum(1 for r in scored if r["tier"] == t) for t in TIER_RANK}
    high_budget = math.floor(cfg.max_high_share * n)
    critical_budget = math.floor(cfg.max_critical_share * n)
    high_plus = counts["high"] + counts["critical"]
    exceeded_high = high_plus > high_budget
    exceeded_critical = counts["critical"] > critical_budget
    warning = exceeded_high or exceeded_critical
    message = (
        f"{high_plus} high/critical rows exceed the review budget of {high_budget} "
        f"({cfg.max_high_share:.1%} of {n} scored rows)"
        if exceeded_high
        else f"{high_plus} high/critical rows are within the review budget of {high_budget}"
    )
    if exceeded_critical:
        message += f"; {counts['critical']} critical rows exceed the critical budget of {critical_budget}"
    if warning:
        message += (
            ". Raw scores and tiers are unchanged; the review queue holds the highest priorities. Check "
            "the risk configuration (weights, caps, tiers)."
        )
    last = [r for r in ranked if r["review_priority"] == "review_queue"]
    return {
        "scored_rows": n,
        "raw_tier_counts": counts,
        "high_or_critical": high_plus,
        "high_or_critical_share": round(high_plus / n, 6) if n else None,
        "high_budget": high_budget,
        "critical_budget": critical_budget,
        "max_high_share": cfg.max_high_share,
        "max_critical_share": cfg.max_critical_share,
        "exceeded_high_budget": exceeded_high,
        "exceeded_critical_budget": exceeded_critical,
        "calibration_warning": warning,
        "message": message,
        "review_queue_size": len(last),
        "review_queue_capacity": cfg.top_k,
        "review_queue_min_score": last[-1]["score"] if last else None,
        "over_capacity": sum(1 for r in ranked if r["review_priority"] == "over_capacity"),
        "ranking": "tier, score, independent sources, |amount|, transaction ID (highest first)",
    }
