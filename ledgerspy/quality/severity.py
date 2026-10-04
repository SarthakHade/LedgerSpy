"""Severity assignment (config `quality.severity`), one severity per check.

    severity(check) = synthetic_overrides[check]   if the dataset is synthetic and an override exists (final)
                      base[check], raised to the highest `escalation` rule for that check whose
                      min_share <= affected rows / total rows        otherwise

Escalation only ever raises severity. A synthetic override is final (no escalation): it exists for checks that
measure simulator behaviour (e.g. PaySim balances), where prevalence says nothing about ledger quality.
All issues of a check share its severity.
"""

from ledgerspy.config.schema import SeverityConfig

LEVELS = ("info", "warning", "error", "critical")
RANK = {s: i for i, s in enumerate(LEVELS)}


def at_least(severity: str, threshold: str) -> bool:
    return RANK[severity] >= RANK[threshold]


def assign(check: str, affected_rows: int, total_rows: int, synthetic: bool, cfg: SeverityConfig) -> dict:
    """Return {severity, base, share, rule} — `rule` names the escalation that applied, for explainability."""
    share = affected_rows / total_rows if total_rows else 0.0
    if synthetic and check in cfg.synthetic_overrides:
        override = cfg.synthetic_overrides[check]
        return {
            "severity": override,
            "base": override,
            "share": round(share, 6),
            "rule": "synthetic dataset override (final, no escalation)",
        }
    base = cfg.base[check]
    severity, rule = base, None
    for r in cfg.escalation:
        if r.check == check and share >= r.min_share and RANK[r.severity] > RANK[severity]:
            severity, rule = r.severity, f"affected share {share:.4f} >= {r.min_share} → {r.severity}"
    return {"severity": severity, "base": base, "share": round(share, 6), "rule": rule}
