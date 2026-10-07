"""Platt scaling, fitted on the VALIDATION split only and judged on the test split.

p_cal = sigmoid(a · logit(s) + b), fitted by Newton's method on validation scores with Platt's smoothed
targets (y⁺ = (N⁺ + 1)/(N⁺ + 2), y⁻ = 1/(N⁻ + 2)). Isotonic regression is not offered: it needs on the order
of 1,000 positives (architecture §5.2).

Status:
- `not_attempted`: fewer than `min_validation_positives` validation positives; the model output is an
  uncalibrated ranking score.
- `calibrated`: the calibrated test ECE ≤ `max_ece`; calibrated values may be read as estimated rates in data
  like this split, never beyond it.
- `not_validated`: calibration was fitted but its test ECE exceeds the limit; outputs remain ranking scores.
Class-weighted models systematically over-estimate the positive rate before calibration (by design of the
weights); calibration corrects only what the validation data can support.
"""

import math
from typing import Any

from ledgerspy.config.schema import SupervisedCalibration
from ledgerspy.supervised import metrics
from ledgerspy.supervised.models import sigmoid


def _logit(s: float) -> float:
    s = min(max(s, 1e-9), 1 - 1e-9)
    return math.log(s / (1 - s))


def fit_platt(scores: list[float], y: list[int], iters: int = 100) -> tuple[float, float]:
    npos = sum(y)
    nneg = len(y) - npos
    t = [(npos + 1) / (npos + 2) if yi else 1 / (nneg + 2) for yi in y]
    x = [_logit(s) for s in scores]
    a, b = 1.0, 0.0
    for _ in range(iters):
        p = [sigmoid(a * xi + b) for xi in x]
        ga = sum((pi - ti) * xi for pi, ti, xi in zip(p, t, x, strict=True))
        gb = sum(pi - ti for pi, ti in zip(p, t, strict=True))
        w = [pi * (1 - pi) + 1e-12 for pi in p]
        haa = sum(wi * xi * xi for wi, xi in zip(w, x, strict=True))
        hab = sum(wi * xi for wi, xi in zip(w, x, strict=True))
        hbb = sum(w)
        det = haa * hbb - hab * hab
        if det <= 0:
            break
        da = (hbb * ga - hab * gb) / det
        db = (haa * gb - hab * ga) / det
        a, b = a - da, b - db
        if max(abs(da), abs(db)) < 1e-10:
            break
    return a, b


def apply(scores: list[float], a: float, b: float) -> list[float]:
    return [sigmoid(a * _logit(s) + b) for s in scores]


def assess(
    val_scores: list[float],
    val_y: list[int],
    test_scores: list[float],
    test_y: list[int],
    cfg: SupervisedCalibration,
    bins: int,
) -> dict[str, Any]:
    raw_ece, raw_table = metrics.ece(test_scores, test_y, bins)
    out: dict[str, Any] = {
        "method": cfg.method,
        "fitted_on": "validation split",
        "uncalibrated_test": {
            "brier": round(metrics.brier(test_scores, test_y), 6),
            "ece": round(raw_ece, 6),
            "reliability": raw_table,
        },
    }
    if sum(val_y) < cfg.min_validation_positives:
        out["status"] = "not_attempted"
        out["reason"] = (
            f"{sum(val_y)} validation positives < {cfg.min_validation_positives}: too few to fit a "
            "calibration map; the output is an uncalibrated ranking score"
        )
        out["output_label"] = "model ranking score (uncalibrated)"
        return out
    a, b = fit_platt(val_scores, val_y)
    cal = apply(test_scores, a, b)
    cal_ece, cal_table = metrics.ece(cal, test_y, bins)
    out["parameters"] = {"a": round(a, 8), "b": round(b, 8)}
    out["calibrated_test"] = {
        "brier": round(metrics.brier(cal, test_y), 6),
        "ece": round(cal_ece, 6),
        "reliability": cal_table,
    }
    if cal_ece <= cfg.max_ece:
        out["status"] = "calibrated"
        out["output_label"] = "calibrated score (estimated positive rate in data like the test split)"
    else:
        out["status"] = "not_validated"
        out["reason"] = f"calibrated test ECE {cal_ece:.3f} > {cfg.max_ece}: not trustworthy"
        out["output_label"] = "model ranking score (calibration not validated)"
    return out
