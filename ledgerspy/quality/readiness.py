"""Data-readiness score (config `quality.readiness`). Deterministic and fully decomposed.

For every component c (completeness, validity, uniqueness, consistency, …):

    affected_c = rows with >= 1 issue of severity >= min_severity raised by one of c's checks
    clean_c    = 1 - affected_c / total_rows
    assessed_c = at least one of c's checks was able to run (status != not_available)

    raw   = sum_c(w_c * clean_c) / sum_c(w_c)            over assessed components only
    score = floor(1000 * raw) / 10                       (one decimal, never rounded up to 100)
    if any issue has severity >= blocking_severity:
        score = min(score, max_score_when_blocking), status = not_ready
    elif any issue has severity >= min_severity: status = ready_with_warnings
    else: status = ready

Dataset-level issues (no row) never change clean_c, but they do count for status and blocking.
"""

import math
from dataclasses import dataclass

from ledgerspy.config.schema import ReadinessConfig
from ledgerspy.quality.severity import at_least


@dataclass(frozen=True)
class ScoredIssue:
    check: str
    severity: str
    source_row: int | None


def compute(
    total_rows: int, available: dict[str, bool], issues: list[ScoredIssue], cfg: ReadinessConfig
) -> dict:
    if total_rows <= 0:
        raise ValueError("readiness needs at least one row")
    components = []
    weighted = weight_sum = 0.0
    for name, comp in cfg.components.items():
        assessed = any(available.get(c, False) for c in comp.checks)
        rows = {
            i.source_row
            for i in issues
            if i.check in comp.checks and i.source_row is not None and at_least(i.severity, cfg.min_severity)
        }
        clean = 1 - len(rows) / total_rows
        entry = {
            "component": name,
            "weight": comp.weight,
            "checks": list(comp.checks),
            "assessed": assessed,
            "affected_rows": len(rows),
            "clean_share": round(clean, 6),
        }
        if assessed:
            weighted += comp.weight * clean
            weight_sum += comp.weight
        components.append(entry)
    raw = weighted / weight_sum if weight_sum else 0.0
    score = math.floor(1000 * raw + 1e-9) / 10
    blocking = sum(1 for i in issues if at_least(i.severity, cfg.blocking_severity))
    warnings = sum(1 for i in issues if at_least(i.severity, cfg.min_severity))
    capped = False
    if blocking:
        status = "not_ready"
        if score > cfg.max_score_when_blocking:
            score, capped = cfg.max_score_when_blocking, True
    else:
        status = "ready_with_warnings" if warnings else "ready"
    for entry in components:
        share = entry["weight"] / weight_sum if entry["assessed"] and weight_sum else 0.0
        entry["normalised_weight"] = round(share, 6)
        entry["points"] = round(100 * share * entry["clean_share"], 4) if entry["assessed"] else None
        entry["points_lost"] = (
            round(100 * share * (1 - entry["clean_share"]), 4) if entry["assessed"] else None
        )
    return {
        "score": score,
        "status": status,
        "raw": round(raw, 9),
        "capped_by_blocking_issues": capped,
        "blocking_issues": blocking,
        "issues_at_or_above_min_severity": warnings,
        "min_severity": cfg.min_severity,
        "blocking_severity": cfg.blocking_severity,
        "assessed_weight": round(weight_sum, 6),
        "components": components,
        "formula": (
            "score = floor1dp(100 × Σ w·clean / Σ w) over assessed components; "
            "clean = 1 − rows with an issue "
            f"≥ {cfg.min_severity} / total rows; capped at {cfg.max_score_when_blocking} while any issue "
            f"≥ {cfg.blocking_severity} remains"
        ),
    }
