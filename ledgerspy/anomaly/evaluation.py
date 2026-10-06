"""Diagnostic evaluation of anomaly scores: precision@k, recall@k, PR-AUC, top-k scenario coverage and score
distributions.

These functions take labels or scenario groups as *arguments*. Nothing in the detection path calls them with
labels: the anomaly report only carries label-free score distributions and review-capacity rankings. Tests and
the acceptance record load PaySim labels or P5 ground truth *after* detection and pass them in here. For a
real ledger without labels no precision or recall is ever computed or shown.

Ranking is deterministic: score descending, then subject ID. PR-AUC is average precision, the step-wise area
under the precision–recall curve with tied scores treated as one threshold (no optimistic tie ordering).
"""

from collections.abc import Iterable
from typing import Any


def ranking(scores: dict[str, float | None]) -> list[str]:
    """Subjects with a score, most unusual first (ties by subject ID)."""
    return sorted((s for s, v in scores.items() if v is not None), key=lambda s: (-scores[s], s))  # type: ignore[operator]


def precision_at_k(ranked: list[str], positives: set[str], k: int) -> float | None:
    top = ranked[:k]
    return sum(1 for s in top if s in positives) / len(top) if top else None


def recall_at_k(ranked: list[str], positives: set[str], k: int) -> float | None:
    if not positives:
        return None
    return sum(1 for s in ranked[:k] if s in positives) / len(positives)


def average_precision(scores: dict[str, float | None], positives: set[str]) -> float | None:
    """PR-AUC as average precision over the scored subjects; None when no scored subject is positive."""
    scored = {s: v for s, v in scores.items() if v is not None}
    total_pos = sum(1 for s in scored if s in positives)
    if total_pos == 0:
        return None
    by_score: dict[float, list[str]] = {}
    for s, v in scored.items():
        by_score.setdefault(v, []).append(s)  # type: ignore[arg-type]
    ap = tp = seen = 0.0
    prev_recall = 0.0
    for v in sorted(by_score, reverse=True):
        block = by_score[v]
        seen += len(block)
        tp += sum(1 for s in block if s in positives)
        recall = tp / total_pos
        ap += (recall - prev_recall) * (tp / seen)
        prev_recall = recall
    return ap


def coverage_at_k(ranked: list[str], groups: dict[str, set[str]], k: int) -> dict[str, Any]:
    """Which groups (e.g. injected scenarios) have at least one subject in the top k."""
    top = set(ranked[:k])
    covered = sorted(g for g, members in groups.items() if members & top)
    return {
        "k": k,
        "covered": covered,
        "covered_count": len(covered),
        "groups": len(groups),
        "share": len(covered) / len(groups) if groups else None,
    }


def score_distribution(values: Iterable[float | None], bins: int = 10) -> dict[str, Any]:
    """Count, quantiles and an equal-width histogram between min and max (label-free)."""
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return {"n": 0}

    def q(p: float) -> float:
        return xs[min(int(p * len(xs)), len(xs) - 1)]

    lo, hi = xs[0], xs[-1]
    width = (hi - lo) / bins if hi > lo else 1.0
    counts = [0] * bins
    for x in xs:
        counts[min(int((x - lo) / width), bins - 1)] += 1
    return {
        "n": len(xs),
        "min": round(lo, 6),
        "p50": round(q(0.5), 6),
        "p90": round(q(0.9), 6),
        "p99": round(q(0.99), 6),
        "p999": round(q(0.999), 6),
        "max": round(hi, 6),
        "histogram": {"edges": [round(lo + j * width, 6) for j in range(bins + 1)], "counts": counts},
    }


def evaluate(scores: dict[str, float | None], positives: set[str], ks: list[int]) -> dict[str, Any]:
    """precision@k / recall@k for each k, PR-AUC and the prevalence it should be compared with."""
    ranked = ranking(scores)
    scored_pos = sum(1 for s in ranked if s in positives)
    return {
        "scored": len(ranked),
        "positives_scored": scored_pos,
        "prevalence": scored_pos / len(ranked) if ranked else None,
        "pr_auc": average_precision(scores, positives),
        "at_k": {
            str(k): {
                "precision": precision_at_k(ranked, positives, k),
                "recall": recall_at_k(ranked, positives, k),
            }
            for k in ks
        },
    }
