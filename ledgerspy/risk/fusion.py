"""Deterministic signal fusion with family and group caps, and its exact contribution breakdown.

Inputs are signals already attached to one subject. Each has evidence e = strength × weight in [0, 1].

    family f:  F_f = min(cap_f, 1 − Π_k (1 − e_(k) · d^k))   strongest first (ties: signal ID); d = decay
    group g:   G_g = min(cap_g, 1 − Π_{f in g} (1 − F_f))    an independent evidence source
    score:     S = 100 · (1 − Π_g (1 − G_g))                  bounded in [0, 100)

Why this shape: within a family, correlated signals add with diminishing returns and never beyond the cap;
across independent sources, evidence combines like independent reasons to look (noisy-OR), so the score only
rises when evidence is added (monotone) and never reaches 100.

Contributions are exact. In log-survival space L = −ln(1 − G) the score is S = 100 · (1 − e^(−Σ L)), so
group g's contribution is S · L_g / Σ L. A group's contribution is split across its families by l_f = −ln(1
− F_f) and a family's across its signals by −ln(1 − e_(k) d^k); a cap scales all members of what it caps
alike. Displayed values are rounded to hundredths with largest remainders, so the displayed contributions
add up exactly to the displayed score.

Unscored families (data quality) are computed for display and contribute nothing.
"""

import math
from dataclasses import dataclass, field
from typing import Any

from ledgerspy.config.schema import RiskConfig

_MAX_E = 1.0 - 1e-12  # keeps ln(1 − e) finite for e = 1


@dataclass(frozen=True)
class SignalInput:
    signal_id: str
    detector: str
    evidence: float  # strength × weight, in [0, 1]


@dataclass
class SignalPart:
    signal_id: str
    detector: str
    evidence: float
    rank: int  # 0 = strongest in its family
    effective: float  # evidence × decay^rank
    contribution: float = 0.0  # score points (exact)
    contribution_display: float = 0.0  # hundredths, largest-remainder rounded


@dataclass
class FamilyPart:
    family: str
    group: str
    label: str
    scored: bool
    cap: float
    raw: float  # uncapped family evidence
    capped: float  # after the family cap
    cap_applied: bool
    signals: list[SignalPart] = field(default_factory=list)
    contribution: float = 0.0
    contribution_display: float = 0.0
    standalone_points: float = 0.0  # 100 × capped: what this family alone would score


@dataclass
class GroupPart:
    group: str
    cap: float | None
    raw: float
    capped: float
    cap_applied: bool
    families: list[str]
    contribution: float = 0.0
    contribution_display: float = 0.0


@dataclass
class Fused:
    score: float  # exact, 0–100
    score_display: float  # rounded to hundredths
    families: list[FamilyPart]
    groups: list[GroupPart]

    @property
    def sources(self) -> int:
        """Independent evidence sources (scored groups with evidence)."""
        return sum(1 for g in self.groups if g.capped > 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score_display,
            "independent_sources": self.sources,
            "groups": [
                {
                    "group": g.group,
                    "cap": g.cap,
                    "evidence_uncapped": round(g.raw, 6),
                    "evidence": round(g.capped, 6),
                    "cap_applied": g.cap_applied,
                    "families": g.families,
                    "contribution": g.contribution_display,
                }
                for g in self.groups
            ],
            "families": [
                {
                    "family": f.family,
                    "label": f.label,
                    "group": f.group,
                    "scored": f.scored,
                    "cap": f.cap,
                    "evidence_uncapped": round(f.raw, 6),
                    "evidence": round(f.capped, 6),
                    "cap_applied": f.cap_applied,
                    "base_points": round(100 * f.raw, 2),
                    "family_points": round(f.standalone_points, 2),
                    "contribution": f.contribution_display,
                    "signals": [
                        {
                            "signal_id": s.signal_id,
                            "detector": s.detector,
                            "evidence": round(s.evidence, 6),
                            "rank_in_family": s.rank,
                            "effective_evidence": round(s.effective, 6),
                            "contribution": s.contribution_display,
                        }
                        for s in f.signals
                    ],
                }
                for f in self.families
            ],
        }


def _lsurv(x: float) -> float:
    return -math.log1p(-min(x, _MAX_E))


def fuse(by_family: dict[str, list[SignalInput]], cfg: RiskConfig) -> Fused:
    """Fuse one subject's signals, grouped by risk family (families not in config are an error upstream)."""
    families: list[FamilyPart] = []
    for name in sorted(by_family):
        items = by_family[name]
        if not items:
            continue
        spec = cfg.families[name]
        ordered = sorted(items, key=lambda s: (-s.evidence, s.signal_id))
        parts = []
        survival = 1.0
        for k, s in enumerate(ordered):
            e = min(max(s.evidence, 0.0), 1.0)
            eff = e * cfg.within_family_decay**k
            parts.append(SignalPart(s.signal_id, s.detector, e, k, eff))
            survival *= 1.0 - eff
        raw = 1.0 - survival
        capped = min(raw, spec.cap) if spec.scored else 0.0
        families.append(
            FamilyPart(
                family=name,
                group=spec.group,
                label=spec.label,
                scored=spec.scored,
                cap=spec.cap,
                raw=raw,
                capped=capped,
                cap_applied=spec.scored and raw > spec.cap,
                signals=parts,
                standalone_points=100 * capped,
            )
        )
    groups: list[GroupPart] = []
    for gname in sorted({f.group for f in families if f.scored and f.capped > 0}):
        members = [f for f in families if f.group == gname and f.scored and f.capped > 0]
        raw = 1.0 - math.prod(1.0 - f.capped for f in members)
        cap = cfg.groups[gname].cap if gname in cfg.groups else None
        capped = min(raw, cap) if cap is not None else raw
        groups.append(
            GroupPart(gname, cap, raw, capped, cap is not None and raw > cap, [f.family for f in members])
        )
    total_l = sum(_lsurv(g.capped) for g in groups)
    score = 100.0 * -math.expm1(-total_l) if total_l > 0 else 0.0
    # exact contributions: score → groups → families → signals, each by log-survival share
    for g in groups:
        g.contribution = score * _lsurv(g.capped) / total_l if total_l else 0.0
        members = [f for f in families if f.family in g.families]
        lf = {f.family: _lsurv(f.capped) for f in members}
        lsum = sum(lf.values())
        for f in members:
            f.contribution = g.contribution * lf[f.family] / lsum if lsum else 0.0
            ls = [_lsurv(s.effective) for s in f.signals]
            ssum = sum(ls)
            for s, part in zip(f.signals, ls, strict=True):
                s.contribution = f.contribution * part / ssum if ssum else 0.0
    _display(score, families, groups)
    return Fused(score, round(score, 2), families, groups)


def apportion(parts: list[float], total: float) -> list[float]:
    """Round `parts` (which sum to `total`) to hundredths so that they sum exactly to round(total, 2): largest
    remainders, ties by position (deterministic)."""
    target = round(total * 100)
    raw = [x * 100 for x in parts]
    base = [math.floor(x) for x in raw]
    left = target - sum(base)
    order = sorted(range(len(raw)), key=lambda i: (-(raw[i] - base[i]), i))
    for i in order[: max(left, 0)]:
        base[i] += 1
    return [b / 100 for b in base]


def _display(score: float, families: list[FamilyPart], groups: list[GroupPart]) -> None:
    """Hundredths with largest remainders at signal level; families and groups are sums of their members."""
    signals = [s for f in families if f.scored for s in f.signals]
    for s, v in zip(signals, apportion([s.contribution for s in signals], score), strict=True):
        s.contribution_display = v
    for f in families:
        f.contribution_display = sum(round(s.contribution_display * 100) for s in f.signals) / 100
    for g in groups:
        g.contribution_display = (
            sum(round(f.contribution_display * 100) for f in families if f.family in g.families) / 100
        )
