"""Explanation objects (architecture §5.5, layer 1 + 3): exact score breakdown plus the evidence behind it.

Every contributor is a real P7/P8 signal: its contribution is the fused value, its evidence, features and
sentence are the signal's own (the detector already phrased them from the data). This module only orders
them, adds the score/tier headline and states the limitations. It never invents values; a value that is not
in a signal or the transaction record is not shown.
"""

from typing import Any

from ledgerspy.canonical import Contribution, Explanation, Score, SignalFamily, SubjectType
from ledgerspy.risk.fusion import Fused
from ledgerspy.risk.models import NOT_PROBABILITY, detector_label


def contributors(fused: Fused, signals: dict[str, dict[str, Any]], limit: int | None = None) -> list[dict]:
    """Scored signals, largest contribution first (ties: signal ID)."""
    out = []
    for fam in fused.families:
        if not fam.scored:
            continue
        for part in fam.signals:
            s = signals[part.signal_id]
            out.append(
                {
                    "signal_id": part.signal_id,
                    "detector": part.detector,
                    "detector_label": detector_label(part.detector),
                    "family": fam.family,
                    "family_label": fam.label,
                    "contribution": part.contribution_display,
                    "share_of_score": round(part.contribution / fused.score, 6) if fused.score else 0.0,
                    "signal_evidence": round(part.evidence, 6),
                    "rank_in_family": part.rank,
                    "family_cap_applied": fam.cap_applied,
                    "severity": s["severity"],
                    "subject_type": s["subject_type"],
                    "subject_id": s["subject_id"],
                    "affected_transactions": s["txn_ids"],
                    "evidence": s["evidence"],
                    "features": s["features"],
                    "explanation": s["explanation"],
                }
            )
    out.sort(key=lambda c: (-c["contribution"], c["signal_id"]))
    return out[:limit] if limit else out


def data_quality_flags(fused: Fused, signals: dict[str, dict[str, Any]]) -> list[dict]:
    """Unscored (data-quality) signals: shown beside the risk, never part of it."""
    return [
        {
            "signal_id": p.signal_id,
            "detector": p.detector,
            "detector_label": detector_label(p.detector),
            "evidence": signals[p.signal_id]["evidence"],
            "explanation": signals[p.signal_id]["explanation"],
        }
        for fam in fused.families
        if not fam.scored
        for p in fam.signals
    ]


def headline(score: float, tier: str, sources: int, fused: Fused) -> str:
    fams = sorted(
        (f for f in fused.families if f.scored and f.contribution_display > 0),
        key=lambda f: (-f.contribution_display, f.family),
    )
    if not fams:
        return f"Review-risk score {score:.2f} ({tier}): no detector raised a signal for this row."
    parts = "; ".join(f"{f.label} {f.contribution_display:.2f} points" for f in fams)
    src = f"{sources} independent source" + ("" if sources == 1 else "s")
    return f"Review-risk score {score:.2f} of 100 ({tier}, {src}): {parts}."


def limitations(
    detectors_unavailable: list[dict[str, str]],
    row_insufficient: list[str],
    fused: Fused,
    tier_reason: str,
) -> list[str]:
    out = [NOT_PROBABILITY]
    out.append(f"Tier: {tier_reason}.")
    for d in detectors_unavailable:
        out.append(
            f"{detector_label(d['detector'])} ({d['detector']}) could not run on this dataset: {d['reason']}"
        )
    for d in row_insufficient:
        out.append(
            f"{detector_label(d)} ({d}) had insufficient data to assess this row; it contributes nothing."
        )
    for f in fused.families:
        if f.scored and f.cap_applied:
            out.append(
                f"{f.label}: evidence capped at {f.cap:g} (uncapped {f.raw:.3f}) so correlated signals do "
                "not "
                "dominate."
            )
    for g in fused.groups:
        if g.cap_applied:
            out.append(f"Group '{g.group}': evidence capped at {g.cap:g} (uncapped {g.raw:.3f}).")
    if any(p.detector == "benford_subgroup" for f in fused.families for p in f.signals):
        out.append(
            "A Benford deviation describes the subgroup's digit distribution; this row is one of its "
            "sample rows, not individually proven unusual."
        )
    return out


def canonical(subject_id: str, fused: Fused, tier: str, signals: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The same breakdown as the canonical Score / Explanation models (validated: shares sum to 1)."""
    risk = fused.score / 100
    by_family: dict[SignalFamily, float] = {}
    contribs = []
    for fam in fused.families:
        if not fam.scored:
            continue
        for p in fam.signals:
            share = p.contribution / fused.score if fused.score else 0.0
            sig_family = SignalFamily(signals[p.signal_id]["family"])
            by_family[sig_family] = by_family.get(sig_family, 0.0) + share
            contribs.append(
                Contribution(
                    family=sig_family,
                    signal_code=signals[p.signal_id]["code"],
                    share=min(max(share, 0.0), 1.0),
                    sentence=signals[p.signal_id]["explanation"],
                )
            )
    if by_family:  # absorb float residue so the shares sum to exactly 1 for validation
        top = max(by_family, key=lambda k: by_family[k])
        by_family[top] += 1.0 - sum(by_family.values())
    score = Score(
        subject=SubjectType.TXN, subject_id=subject_id, risk=risk, tier=tier, contributions=by_family
    )
    expl = Explanation(
        subject=SubjectType.TXN, subject_id=subject_id, risk=risk, tier=tier, contributions=contribs
    )
    return {"score": score.model_dump(mode="json"), "explanation": expl.model_dump(mode="json")}
