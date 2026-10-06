"""Isolation Forest (Liu, Ting & Zhou, 2008) in pure Python, and the transaction-level detector built on it.

Algorithm. Each of `n_estimators` trees is grown on a sub-sample of ψ = min(max_samples, n) reference rows
drawn without replacement, to a height limit ⌈log₂ ψ⌉. A node picks a feature uniformly among those not
constant in the node and a split uniformly in [min, max); rows with x < split go left. A row's path length
h(x) is the depth of the leaf it reaches plus c(leaf size), the expected depth of an unbuilt subtree, where
c(n) = 2·H(n − 1) − 2(n − 1)/n and H(i) = ln i + γ. The anomaly score is s(x) = 2^(−E[h(x)] / c(ψ)) in (0, 1]:
**higher = more unusual** (short average path = easy to isolate). s ≈ 0.5 for every row means there is no
distinct anomaly. (scikit-learn's `score_samples` is −s; this API never negates.)

Determinism. One `random.Random(random_state)` draws every sub-sample and split in a fixed order from the
reference rows sorted by their model inputs, so the same feature values and configuration give the same forest
(`model_sha256`) and scores — whatever the input order, transaction IDs or unrelated columns (e.g. labels).

Threshold policy. Not scikit-learn's `contamination`: a scored row is flagged when its score is in the top
`tail_fraction` of the reference scores (mid-rank percentile ≥ 1 − tail_fraction), it is above `min_score`,
and it is among the `max_flagged` highest scores (review capacity). So a dataset with nothing unusual flags
little or nothing, and a large dataset never flags more rows than can be reviewed.

Inputs. Configured P6 features that pass eligibility, after their monotone transform. Isolation Forest splits
are invariant to per-feature affine scaling, so no standardisation is needed; heavy-tailed counts, days and
ratios are log-transformed so splits are not spent on the far tail. A null value is replaced by the feature's
reference median and recorded in a missing-indicator column (`missing:<feature>`); evidence always shows the
original null. Rows with too few present features are not scored (`insufficient_data`).

Explanations are robust per-feature deviations of the flagged row from the reference rows, labelled "feature
deviation (not SHAP)" (architecture §5.5).
"""

import math
import random
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field

from ledgerspy.anomaly.eligibility import Population, check_feature, transformed_column
from ledgerspy.anomaly.models import (
    DISABLED,
    EVALUATED,
    INSUFFICIENT_DATA,
    REFERENCE_ONLY,
    ROW_INSUFFICIENT,
    SCORED,
    AnomalyResult,
    DetectorOutcome,
)
from ledgerspy.anomaly.robust_stats import center_scale, median
from ledgerspy.config.schema import AnomalyConfig
from ledgerspy.features.data import FeatureInput
from ledgerspy.features.matrix import FeatureRun
from ledgerspy.hashing import canonical_json, sha256_hex

DETECTOR_ID, VERSION = "isolation_forest", 1
EULER_GAMMA = 0.5772156649015329


def c_factor(n: int) -> float:
    """Average path length of an unsuccessful binary-search-tree search among n points."""
    if n <= 1:
        return 0.0
    if n == 2:
        return 1.0
    return 2.0 * (math.log(n - 1) + EULER_GAMMA) - 2.0 * (n - 1) / n


@dataclass
class Tree:
    feature: list[int] = field(default_factory=list)  # -1 for a leaf
    split: list[float] = field(default_factory=list)
    left: list[int] = field(default_factory=list)
    right: list[int] = field(default_factory=list)
    leaf_depth: list[float] = field(default_factory=list)  # c(size) for a leaf (added to its depth)

    def node(self, feature: int, split: float, leaf: float) -> int:
        self.feature.append(feature)
        self.split.append(split)
        self.left.append(-1)
        self.right.append(-1)
        self.leaf_depth.append(leaf)
        return len(self.feature) - 1


class IsolationForest:
    def __init__(self, n_estimators: int, max_samples: int, random_state: int) -> None:
        self.n_estimators = n_estimators
        self.max_samples = max_samples
        self.random_state = random_state
        self.trees: list[Tree] = []
        self.psi = 0

    def fit(self, columns: list[list[float]], rows: list[int]) -> "IsolationForest":
        """Grow the forest on `rows` (indices into every column). Columns must have no nulls on `rows`."""
        rng = random.Random(self.random_state)  # noqa: S311 — reproducible model randomness, not security
        self.psi = min(self.max_samples, len(rows))
        limit = math.ceil(math.log2(self.psi)) if self.psi > 1 else 0
        self.trees = []
        for _ in range(self.n_estimators):
            sample = rng.sample(rows, self.psi)
            tree = Tree()
            self._grow(tree, columns, sample, 0, limit, rng)
            self.trees.append(tree)
        return self

    def _grow(self, tree: Tree, columns, idx: list[int], depth: int, limit: int, rng: random.Random) -> int:
        if depth >= limit or len(idx) <= 1:
            return tree.node(-1, 0.0, c_factor(len(idx)))
        candidates = list(range(len(columns)))
        while candidates:  # uniform among the features not constant in this node
            f = candidates.pop(rng.randrange(len(candidates)))
            col = columns[f]
            lo = hi = col[idx[0]]
            for i in idx:
                v = col[i]
                if v < lo:
                    lo = v
                elif v > hi:
                    hi = v
            if lo < hi:
                break
        else:
            return tree.node(-1, 0.0, c_factor(len(idx)))
        split = rng.uniform(lo, hi)
        while split <= lo:  # uniform() may return the lower bound itself; both sides must be non-empty
            split = rng.uniform(lo, hi)
        me = tree.node(f, split, 0.0)
        left = [i for i in idx if col[i] < split]
        right = [i for i in idx if col[i] >= split]
        tree.left[me] = self._grow(tree, columns, left, depth + 1, limit, rng)
        tree.right[me] = self._grow(tree, columns, right, depth + 1, limit, rng)
        return me

    def score(self, columns: list[list[float]], rows: list[int]) -> dict[int, float]:
        """Anomaly score s(x) ∈ (0, 1] for each row index; higher = more unusual."""
        if not rows:
            return {}
        total = [0.0] * (max(rows) + 1)
        for tree in self.trees:
            stack = [(0, rows, 0)]
            while stack:
                node, idx, depth = stack.pop()
                f = tree.feature[node]
                if f < 0:
                    h = depth + tree.leaf_depth[node]
                    for i in idx:
                        total[i] += h
                    continue
                split = tree.split[node]
                col = columns[f]
                left = [i for i in idx if col[i] < split]
                right = [i for i in idx if col[i] >= split]
                if left:
                    stack.append((tree.left[node], left, depth + 1))
                if right:
                    stack.append((tree.right[node], right, depth + 1))
        norm = c_factor(self.psi) * len(self.trees)
        return {i: 2.0 ** (-total[i] / norm) if norm else 0.5 for i in rows}

    def sha256(self) -> str:
        """Identifies the fitted forest (structure and splits): the deterministic serialisation."""
        return sha256_hex(
            canonical_json(
                {
                    "psi": self.psi,
                    "trees": [
                        [
                            t.feature,
                            [repr(s) for s in t.split],
                            t.left,
                            t.right,
                            [repr(d) for d in t.leaf_depth],
                        ]
                        for t in self.trees
                    ],
                }
            )
        )


# ------------------------------------------------------------------------------------------------ detector


def detect(inp: FeatureInput, run: FeatureRun, pop: Population, cfg: AnomalyConfig) -> DetectorOutcome:
    ic = cfg.isolation_forest
    out = DetectorOutcome(DETECTOR_ID, VERSION, EVALUATED, rows_total=len(pop.order))
    out.parameters = {
        "random_state": ic.random_state,
        "n_estimators": ic.n_estimators,
        "max_samples": ic.max_samples,
        "min_rows": ic.min_rows,
        "min_features": ic.min_features,
        "threshold": {
            "tail_fraction": ic.tail_fraction,
            "min_score": ic.min_score,
            "max_flagged": ic.max_flagged,
            "rule": "percentile ≥ 1 − tail_fraction of reference scores, score > min_score, top max_flagged",
        },
        "score_direction": "higher = more unusual; s = 2^(−E[h(x)]/c(ψ)) in (0, 1]",
        "reference": pop.mode,
    }
    if not ic.enabled:
        out.status = DISABLED
        return out
    if pop.status != "ok":
        out.status, out.reason = pop.status, pop.reason
        return out
    tx = run.transactions
    subjects = tx.subjects
    entries = []
    for f in ic.features:
        e = check_feature(f.name, run, pop.reference, cfg.eligibility)
        entries.append(e | {"transform": f.transform})
    used = [(e["feature"], e["transform"]) for e in entries if e["status"] == "used"]
    out.features = {
        "used": [e for e in entries if e["status"] == "used"],
        "excluded": [e for e in entries if e["status"] == "excluded"],
    }
    values = {name: transformed_column(inp, run, name, how) for name, how in used}

    def present_share(i: int) -> float:
        return sum(1 for name, _ in used if values[name][i] is not None) / len(used) if used else 0.0

    candidates = pop.reference if pop.mode == "dataset" else pop.reference + pop.scoring
    ok = {i for i in candidates if present_share(i) >= cfg.eligibility.min_row_present_share}
    ref_rows = [i for i in pop.reference if i in ok]
    scoring_rows = [i for i in pop.scoring if i in ok]
    status: list[str | None] = [None] * len(subjects)
    scoring_set = set(pop.scoring)
    reasons: dict[str, int] = {}
    for i in pop.order:
        if i in pop.excluded:
            status[i] = ROW_INSUFFICIENT
            reasons[pop.excluded[i]] = reasons.get(pop.excluded[i], 0) + 1
        elif i not in scoring_set:
            status[i] = REFERENCE_ONLY
        elif i in ok:
            status[i] = SCORED
        else:
            status[i] = ROW_INSUFFICIENT
            reasons["too_few_present_features"] = reasons.get("too_few_present_features", 0) + 1
    out.rows_reference_only = sum(1 for s in status if s == REFERENCE_ONLY)
    if len(used) < ic.min_features or len(ref_rows) < ic.min_rows:
        out.status = INSUFFICIENT_DATA
        out.reason = (
            f"{len(used)} usable feature(s) (minimum {ic.min_features}) and {len(ref_rows)} usable reference "
            f"row(s) (minimum {ic.min_rows}): too little information for a meaningful model"
        )
        status = [ROW_INSUFFICIENT if s == SCORED else s for s in status]
        out.rows_insufficient = sum(1 for s in status if s == ROW_INSUFFICIENT)
        out.insufficient_reasons = reasons | {"detector_insufficient_data": len(scoring_rows)}
        out.scores = {"status": status}
        return out

    # Imputation (reference medians) and missing indicators: explicit and recorded.
    columns: list[list[float]] = []
    names: list[str] = []
    model_features = []
    for name, how in used:
        col = values[name]
        present = [col[i] for i in ref_rows if col[i] is not None]
        fill = median(present)
        nulls = len(ref_rows) - len(present)
        columns.append([fill if v is None else v for v in col])
        names.append(name)
        model_features.append(
            {
                "feature": name,
                "transform": how,
                "imputed_with_reference_median": round(fill, 6),
                "reference_nulls": nulls,
            }
        )
        if 0 < nulls < len(ref_rows):
            columns.append([1.0 if v is None else 0.0 for v in col])
            names.append(f"missing:{name}")
            model_features.append({"feature": f"missing:{name}", "indicator_of": name})

    # Sub-samples are drawn from the reference rows ordered by their model inputs (ties are identical inputs),
    # so the forest depends on the feature values and the seed only — not on IDs, file bytes or row order.
    train = sorted(ref_rows, key=lambda i: (tuple(c[i] for c in columns), subjects[i]))
    forest = IsolationForest(ic.n_estimators, ic.max_samples, ic.random_state).fit(columns, train)
    scores = forest.score(columns, sorted(set(ref_rows) | set(scoring_rows), key=lambda i: subjects[i]))
    ref_sorted = sorted(scores[i] for i in ref_rows)

    def pct(s: float) -> float:
        lo, hi = bisect_left(ref_sorted, s), bisect_right(ref_sorted, s)
        return (lo + 0.5 * (hi - lo)) / len(ref_sorted)

    percentile = {i: pct(scores[i]) for i in scoring_rows}
    eligible = [i for i in scoring_rows if percentile[i] >= 1 - ic.tail_fraction and scores[i] > ic.min_score]
    eligible.sort(key=lambda i: (-scores[i], subjects[i]))
    flagged = eligible[: ic.max_flagged]

    # Feature deviations for explanations: robust z of each model input against the reference rows.
    deviation = []
    for name, _how in used:
        col = values[name]
        m, scale, method = center_scale([col[i] for i in ref_rows if col[i] is not None])
        deviation.append((name, m, scale, method))

    out.rows_scored = len(scoring_rows)
    out.rows_insufficient = sum(1 for s in status if s == ROW_INSUFFICIENT)
    out.insufficient_reasons = reasons
    model_meta = {
        "algorithm": "isolation_forest (Liu, Ting & Zhou 2008), pure Python",
        "random_state": ic.random_state,
        "n_estimators": ic.n_estimators,
        "psi": forest.psi,
        "height_limit": math.ceil(math.log2(forest.psi)) if forest.psi > 1 else 0,
        "reference_rows": len(ref_rows),
        "model_inputs": names,
        "model_features": model_features,
        "model_sha256": forest.sha256(),
    }
    out.details = {
        "model": model_meta,
        "candidates_in_tail": len(eligible),
        "flagged": len(flagged),
        "capped_by_review_capacity": max(len(eligible) - len(flagged), 0),
        "score_cutoff": round(scores[flagged[-1]], 6) if flagged else None,
    }
    out.scores = {
        "status": status,
        "score": [
            round(scores[i], 6) if i in scores and status[i] == SCORED else None for i in range(len(subjects))
        ],
        "percentile": [round(percentile[i], 6) if i in percentile else None for i in range(len(subjects))],
    }
    p0 = 1 - ic.tail_fraction
    for rank, i in enumerate(flagged, start=1):
        devs = []
        for name, m, scale, method in deviation:
            v = values[name][i]
            if v is None or scale is None:
                continue
            devs.append(
                {
                    "feature": name,
                    "value": tx.columns[name][i],
                    "robust_z": round((v - m) / scale, 4),
                    "scale_method": method,
                }
            )
        devs.sort(key=lambda d: (-abs(d["robust_z"]), d["feature"]))
        devs = devs[: ic.max_explained_features]
        imputed = [name for name, _ in used if values[name][i] is None]
        out.results.append(
            AnomalyResult(
                detector_id=DETECTOR_ID,
                detector_version=VERSION,
                subject_type="transaction",
                subject_id=subjects[i],
                txn_ids=(subjects[i],),
                anomaly_score=round(scores[i], 6),
                percentile=round(percentile[i], 6),
                tail=ic.tail_fraction,
                population={"kind": "reference_rows", "n": len(ref_rows), "reference": pop.mode},
                features={name: tx.columns[name][i] for name, _ in used},
                evidence={
                    "rank": rank,
                    "feature_deviation": devs,
                    "feature_deviation_method": "robust z vs reference rows (not SHAP)",
                    "imputed_features": imputed,
                    "threshold": out.parameters["threshold"],
                },
                explanation=_text(scores[i], percentile[i], len(ref_rows), devs, imputed, ic),
                severity=ic.severity,
                strength=round(min(max((percentile[i] - p0) / (1 - p0), 0.0), 1.0), 6),
                weight=ic.weight,
                metadata={"model_sha256": model_meta["model_sha256"], "random_state": ic.random_state},
            )
        )
    return out


def _text(score: float, pct: float, n: int, devs: list[dict], imputed: list[str], ic) -> str:
    top = 1 - pct
    lead = (
        f"Isolation Forest score {score:.3f} (higher = more unusual) is in the top "
        f"{max(top, 0.0) * 100:.2f}% of "
        f"{n:,} reference transactions (flagged: top {ic.tail_fraction:.1%}, score > {ic.min_score:g})."
    )
    if devs:
        lead += " Most deviating inputs (feature deviation, not SHAP): " + ", ".join(
            f"{d['feature']} = {d['value']:,.4g} (robust z {d['robust_z']:+.1f})"
            if isinstance(d["value"], float)
            else f"{d['feature']} = {d['value']} (robust z {d['robust_z']:+.1f})"
            for d in devs
        )
        lead += "."
    if imputed:
        lead += f" Null inputs, scored at the reference median: {', '.join(imputed)}."
    return lead + " Unusual relative to this dataset — a reason to review, not a finding of fraud."
