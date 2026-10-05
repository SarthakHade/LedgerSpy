"""Benford analysis (architecture §7): first digit, first two digits, last two digits.

A deviation from Benford's law is an investigative signal for sampling, never evidence of fraud: many
legitimate populations do not follow the law (assigned numbers, prices, caps, narrow ranges), and large
samples reject the exact law for trivial differences. MAD with Nigrini's conformity bands is the primary
measure; χ² and per-digit z are reported with that caveat.

Extraction rule (documented, configurable in `benford`):
- missing and non-numeric amounts are excluded (`missing`);
- zero amounts are excluded (`zero`);
- negative amounts are excluded (`negative`) unless `negative_amounts: absolute`, which uses |amount|;
- first-digit / first-two tests use amounts ≥ `min_value_major` currency units (`below_minimum`): leading
  digits are read from the minor-unit integer, identical to the major-unit digits;
- the last-two test uses whole currency units ≥ `last_two_min_major` (`below_minimum`), so the two digits are
  trailing digits, not leading ones.
Source values are never modified.
"""

import math
from collections import Counter
from typing import Any

from ledgerspy.config.schema import BenfordConfig

VERSION = 1
ANALYZED = "analyzed"
INSUFFICIENT = "insufficient_data"
NOT_APPLICABLE = "not_applicable"
INVALID = "invalid_population"

DIGITS = {"first": range(1, 10), "first_two": range(10, 100), "last_two": range(0, 100)}


def expected(test: str) -> dict[int, float]:
    if test == "last_two":
        return {d: 0.01 for d in DIGITS[test]}
    return {d: math.log10(1 + 1 / d) for d in DIGITS[test]}


def digit_of(amount_minor: int | None, test: str, cfg: BenfordConfig) -> tuple[int | None, str | None]:
    """(digit value, None) or (None, exclusion reason) for one amount."""
    if amount_minor is None:
        return None, "missing"
    if amount_minor == 0:
        return None, "zero"
    if amount_minor < 0 and cfg.negative_amounts == "exclude":
        return None, "negative"
    v = abs(amount_minor)
    if test == "last_two":
        whole = v // 100
        if whole < cfg.last_two_min_major:
            return None, "below_minimum"
        return whole % 100, None
    if v < cfg.min_value_major * 100:
        return None, "below_minimum"
    text = str(v)
    return int(text[0]) if test == "first" else int(text[:2]), None


def gamma_q(a: float, x: float) -> float:
    """Regularized upper incomplete gamma Q(a, x): series for x < a + 1, else continued fraction (Numerical
    Recipes); used for the χ² p-value."""
    if x <= 0:
        return 1.0
    gln = math.lgamma(a)
    if x < a + 1:
        term = total = 1.0 / a
        ap = a
        for _ in range(10_000):
            ap += 1
            term *= x / ap
            total += term
            if abs(term) < abs(total) * 1e-15:
                break
        return max(0.0, 1.0 - total * math.exp(-x + a * math.log(x) - gln))
    b, c, d = x + 1 - a, 1e300, 1 / (x + 1 - a)
    h = d
    for i in range(1, 10_000):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = 1e-300 if abs(d) < 1e-300 else d
        c = b + an / c
        c = 1e-300 if abs(c) < 1e-300 else c
        d = 1 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-15:
            break
    return min(1.0, math.exp(-x + a * math.log(x) - gln) * h)


def band(test: str, mad: float, cfg: BenfordConfig) -> str | None:
    """Nigrini's conformity band for first / first-two digits; None for last-two (no standard bands)."""
    if test not in cfg.mad_bands:
        return None
    close, acceptable, marginal = cfg.mad_bands[test]
    return (
        "close"
        if mad <= close
        else "acceptable"
        if mad <= acceptable
        else "marginal"
        if mad <= marginal
        else ("nonconformity")
    )


def analyze(
    amounts: list[int | None], test: str, cfg: BenfordConfig, population: str = "all rows"
) -> dict[str, Any]:
    """One Benford test on a list of amounts (minor units). Deterministic; the input is not modified."""
    digits: list[int] = []
    excluded: Counter = Counter()
    used: list[int] = []
    for a in amounts:
        d, why = digit_of(a, test, cfg)
        if why:
            excluded[why] += 1
        else:
            digits.append(d)  # type: ignore[arg-type]
            used.append(abs(a))  # type: ignore[arg-type]
    n = len(digits)
    out: dict[str, Any] = {
        "test": test,
        "version": VERSION,
        "population": population,
        "rows": len(amounts),
        "n": n,
        "excluded": dict(sorted(excluded.items())),
        "min_n": cfg.min_n[test],
        "warnings": [],
    }
    if not amounts or excluded.get("missing", 0) == len(amounts):
        return {**out, "status": NOT_APPLICABLE, "reason": "no amounts"}
    if n < cfg.min_n[test]:
        return {**out, "status": INSUFFICIENT, "reason": f"n = {n} < minimum {cfg.min_n[test]}"}
    distinct = len(set(used))
    if distinct < cfg.min_distinct_values:
        return {
            **out,
            "status": INVALID,
            "reason": f"only {distinct} distinct amounts (< {cfg.min_distinct_values}): degenerate",
        }
    if test != "last_two":
        ratio = max(used) / min(used)
        out["range_ratio"] = round(ratio, 3)
        if ratio < cfg.warn_range_ratio:
            out["warnings"].append(
                f"narrow range (max/min {ratio:.2f} < {cfg.warn_range_ratio:g}): such a population is not "
                "expected to follow Benford; a deviation may only reflect the range, which may itself be the "
                "finding"
            )
    if n < cfg.warn_n[test]:
        out["warnings"].append(
            f"small sample (n = {n} < {cfg.warn_n[test]}): wide confidence in every measure"
        )
    exp = expected(test)
    counts = Counter(digits)
    observed = {d: counts.get(d, 0) / n for d in exp}
    z = {}
    for d, p in exp.items():
        dev = abs(observed[d] - p) - 1 / (2 * n)
        z[d] = round(max(dev, 0.0) / math.sqrt(p * (1 - p) / n), 4)
    mad = sum(abs(observed[d] - p) for d, p in exp.items()) / len(exp)
    chi2 = sum((counts.get(d, 0) - n * p) ** 2 / (n * p) for d, p in exp.items())
    dof = len(exp) - 1
    significant = [d for d in exp if z[d] > cfg.z_critical]
    return {
        **out,
        "status": ANALYZED,
        "reason": "",
        "expected": {str(d): round(p, 6) for d, p in exp.items()},
        "observed": {str(d): round(observed[d], 6) for d in exp},
        "counts": {str(d): counts.get(d, 0) for d in exp},
        "z": {str(d): z[d] for d in exp},
        "mad": round(mad, 6),
        "band": band(test, mad, cfg),
        "chi2": round(chi2, 4),
        "dof": dof,
        "chi2_p_value": round(gamma_q(dof / 2, chi2 / 2), 6),
        "over_represented": [d for d in significant if observed[d] > exp[d]],
        "under_represented": [d for d in significant if observed[d] < exp[d]],
        "max_abs_z_digit": max(exp, key=lambda d: (z[d], -d)),
        "note": "A Benford deviation is a reason to sample these rows, not evidence of fraud. "
        "χ² rejects almost any large sample; MAD and its band are the primary measure.",
    }
