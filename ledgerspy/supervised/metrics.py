"""Evaluation metrics for imbalanced ranking problems, with deterministic bootstrap intervals.

Ranking: score descending, ties by txn_id. PR-AUC is average precision with tied scores as one threshold (no
optimistic tie order); ROC-AUC is the Mann–Whitney statistic with mid-ranks. precision@k / recall@k use the
ranking. At an operating threshold t (chosen on validation), a row is flagged when score ≥ t: confusion
matrix, precision, recall and F1. Calibration: Brier score and expected calibration error (equal-width
bins). Accuracy is never reported.

Bootstrap: the test split is resampled with replacement `resamples` times with a seeded generator. When the
split is grouped (group strategy: connected components; chronological: days, real or simulated) whole
clusters are resampled, because rows of one cluster are not independent observations; otherwise rows are.
The interval is the percentile interval at `confidence`. With few test positives the intervals are wide, and
they are reported as such.
"""

import math
import random
from collections import Counter, defaultdict
from typing import Any


def order(scores: list[float], ids: list[str]) -> list[int]:
    return sorted(range(len(scores)), key=lambda i: (-scores[i], ids[i]))


def average_precision(scores: list[float], y: list[int], weights: list[int] | None = None) -> float | None:
    w = weights or [1] * len(y)
    total_pos = sum(wi for wi, yi in zip(w, y, strict=True) if yi)
    if total_pos == 0:
        return None
    by: dict[float, list[int]] = defaultdict(list)
    for i, s in enumerate(scores):
        if w[i]:
            by[s].append(i)
    ap = tp = seen = 0.0
    prev = 0.0
    for s in sorted(by, reverse=True):
        block = by[s]
        seen += sum(w[i] for i in block)
        tp += sum(w[i] for i in block if y[i])
        rec = tp / total_pos
        ap += (rec - prev) * (tp / seen)
        prev = rec
    return ap


def roc_auc(scores: list[float], y: list[int]) -> float | None:
    n_pos = sum(y)
    n_neg = len(y) - n_pos
    if not n_pos or not n_neg:
        return None
    idx = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks = [0.0] * len(scores)
    k = 0
    while k < len(idx):
        j = k
        while j + 1 < len(idx) and scores[idx[j + 1]] == scores[idx[k]]:
            j += 1
        mid = (k + j) / 2 + 1
        for m in range(k, j + 1):
            ranks[idx[m]] = mid
        k = j + 1
    rsum = sum(r for r, yi in zip(ranks, y, strict=True) if yi)
    return (rsum - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def at_k(scores: list[float], y: list[int], ids: list[str], k: int) -> dict[str, float | None]:
    top = order(scores, ids)[:k]
    tp = sum(y[i] for i in top)
    pos = sum(y)
    return {
        "k": k,
        "precision": tp / len(top) if top else None,
        "recall": tp / pos if pos else None,
        "hits": tp,
    }


def at_threshold(scores: list[float], y: list[int], t: float) -> dict[str, Any]:
    tp = sum(1 for s, yi in zip(scores, y, strict=True) if s >= t and yi)
    fp = sum(1 for s, yi in zip(scores, y, strict=True) if s >= t and not yi)
    fn = sum(1 for s, yi in zip(scores, y, strict=True) if s < t and yi)
    tn = len(y) - tp - fp - fn
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision and recall
        else 0.0
        if precision is not None
        else None
    )
    return {
        "threshold": t,
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "flagged": tp + fp,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def brier(p: list[float], y: list[int]) -> float:
    return sum((pi - yi) ** 2 for pi, yi in zip(p, y, strict=True)) / len(y)


def ece(p: list[float], y: list[int], bins: int) -> tuple[float, list[dict[str, Any]]]:
    groups: dict[int, list[int]] = defaultdict(list)
    for i, pi in enumerate(p):
        groups[min(int(pi * bins), bins - 1)].append(i)
    total, table = 0.0, []
    for b in range(bins):
        rows = groups.get(b, [])
        if not rows:
            continue
        conf = sum(p[i] for i in rows) / len(rows)
        acc = sum(y[i] for i in rows) / len(rows)
        total += len(rows) / len(p) * abs(conf - acc)
        table.append(
            {
                "bin": [b / bins, (b + 1) / bins],
                "rows": len(rows),
                "mean_score": round(conf, 6),
                "observed_rate": round(acc, 6),
            }
        )
    return total, table


def bootstrap(
    scores: list[float],
    y: list[int],
    ids: list[str],
    clusters: list[str] | None,
    ks: list[int],
    resamples: int,
    seed: int,
    confidence: float,
) -> dict[str, Any]:
    """Percentile intervals for PR-AUC, precision@k and recall@k (weighted by resample multiplicity)."""
    rank = order(scores, ids)
    keys = clusters or [str(i) for i in range(len(y))]
    members: dict[str, list[int]] = defaultdict(list)
    for i, c in enumerate(keys):
        members[c].append(i)
    names = sorted(members)
    rng = random.Random(seed)  # noqa: S311 — reproducible resampling, not security
    stats: dict[str, list[float]] = defaultdict(list)
    for _ in range(resamples):
        counts = Counter(rng.choice(names) for _ in names)
        w = [0] * len(y)
        for c, m in counts.items():
            for i in members[c]:
                w[i] += m
        ap = average_precision(scores, y, w)
        if ap is not None:
            stats["pr_auc"].append(ap)
        pos = sum(wi for wi, yi in zip(w, y, strict=True) if yi)
        for k in ks:
            taken = tp = 0
            for i in rank:
                if not w[i]:
                    continue
                use = min(w[i], k - taken)
                taken += use
                tp += use * y[i]
                if taken >= k:
                    break
            stats[f"precision@{k}"].append(tp / taken if taken else 0.0)
            if pos:
                stats[f"recall@{k}"].append(tp / pos)
    lo, hi = (1 - confidence) / 2, 1 - (1 - confidence) / 2
    out = {}
    for name, vals in sorted(stats.items()):
        s = sorted(vals)
        out[name] = {
            "low": round(s[min(len(s) - 1, int(math.floor(lo * len(s))))], 6),
            "high": round(s[min(len(s) - 1, int(math.ceil(hi * len(s))) - 1)], 6),
            "valid_resamples": len(s),
        }
    return {
        "method": ("cluster" if clusters else "row") + " bootstrap (percentile)",
        "resamples": resamples,
        "seed": seed,
        "confidence": confidence,
        "clusters": len(names),
        "intervals": out,
    }


def evaluate(
    scores: list[float], y: list[int], ids: list[str], ks: list[int], threshold: float | None
) -> dict:
    out: dict[str, Any] = {
        "rows": len(y),
        "positives": sum(y),
        "prevalence": sum(y) / len(y) if y else None,
        "pr_auc": average_precision(scores, y),
        "roc_auc": roc_auc(scores, y),
        "at_k": {str(k): at_k(scores, y, ids, k) for k in ks},
    }
    if threshold is not None:
        out["at_threshold"] = at_threshold(scores, y, threshold)
    return out
