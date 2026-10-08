"""Supervised experiments: split → fit on train → threshold and calibration on validation → test once.

For every configured split strategy:
1. Split the labelled rows (nothing has been fitted yet).
2. Baselines, scored on the same test rows: prevalence (a constant score), amount-only (log_amount), the P8
   Isolation Forest score, the P9 review-risk score (both label-free) and, when a transaction type exists,
   the per-type positive rate learnt on the training split.
3. For every model × imbalance setting (× ablation): fit the preprocessor and the model on the training split
   (class weights from training counts only; no oversampling anywhere); score validation and test.
4. Operating threshold = the score at the top `review_share` of VALIDATION rows (review capacity). Calibration
   (Platt) is fitted on validation. The test split is then evaluated once: PR-AUC, ROC-AUC,
   precision/recall@k, the confusion matrix at the validation threshold, calibration metrics, and bootstrap
   intervals.
5. Interpretation: logistic-regression coefficients on standardised inputs; tree gain importance; permutation
   importance (PR-AUC drop) on the VALIDATION split. None of these are causal explanations.

Every run records its identity (dataset, data version, evidence and label hashes, feature contract, config
hash, split, seed, model and hyperparameters, model hash), its results, warnings and limitations.
`result_sha256` covers everything except timestamps and timings, so a rerun with the same inputs reproduces
it exactly.
"""

import math
import platform
import random
import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from ledgerspy import __version__
from ledgerspy.config.schema import LedgerSpyConfig
from ledgerspy.hashing import canonical_json, sha256_hex
from ledgerspy.supervised import calibration, metrics, models, splits
from ledgerspy.supervised.dataset import Preprocessor, TrainingData

REPORT_VERSION = 1
SUPERVISED_VERSION = 1
NOTE = (
    "Experimental evaluation of supervised ranking models. Model outputs are ranking scores unless the "
    "calibration status says 'calibrated', and even then they are estimated rates in data like the test "
    "split — never a probability that a transaction is fraudulent. Nothing here changes the P9 review-risk "
    "score."
)
LIMITATIONS = [
    "Labels are a development proxy (PaySim simulation) or injected synthetic scenarios, not confirmed "
    "audit outcomes; results do not transfer to a real ledger without labelled outcomes from that domain.",
    "Few positives per split: point estimates move a lot between splits; read the bootstrap intervals.",
    "The models are pure-Python implementations of standard algorithms (scikit-learn is not installed).",
]


def _rnd(x: Any) -> Any:
    if isinstance(x, float):
        return round(x, 6)
    if isinstance(x, dict):
        return {k: _rnd(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_rnd(v) for v in x]
    return x


def threshold_from_validation(scores: list[float], ids: list[str], share: float) -> dict[str, Any]:
    """The score of the last row inside the top `share` of validation rows (review capacity)."""
    k = max(1, math.ceil(share * len(scores)))
    ranked = metrics.order(scores, ids)
    t = scores[ranked[k - 1]]
    return {
        "value": t,
        "rule": f"score of the top {share:.1%} of validation rows (review capacity)",
        "validation_rows": len(scores),
        "validation_top_k": k,
    }


def _sub(xs: list, rows: list[int]) -> list:
    return [xs[i] for i in rows]


def _weights(y: list[int], mode: str) -> list[float]:
    if mode == "none":
        return [1.0] * len(y)
    pos = sum(y)
    neg = len(y) - pos
    wp = len(y) / (2 * pos) if pos else 1.0
    wn = len(y) / (2 * neg) if neg else 1.0
    return [wp if v else wn for v in y]


def _score_block(scores, y, ids, clusters, threshold, ev_cfg) -> dict[str, Any]:
    out = metrics.evaluate(scores, y, ids, ev_cfg.top_k, threshold)
    b = ev_cfg.bootstrap
    out["confidence_intervals"] = metrics.bootstrap(
        scores, y, ids, clusters, ev_cfg.top_k, b.resamples, b.seed, b.confidence
    )
    return out


def _baselines(
    data: TrainingData,
    split: splits.Split,
    label_free: dict[str, list[float | None]],
    settings: LedgerSpyConfig,
) -> list[dict[str, Any]]:
    ev = settings.supervised.evaluation
    tr, va, te = split.parts["train"], split.parts["validation"], split.parts["test"]
    ids = data.txn_ids
    out = []
    cands: dict[str, tuple[list[float], str]] = {
        "prevalence": ([0.0] * data.n, "a constant score: PR-AUC equals the test prevalence"),
    }
    if "log_amount" in data.numeric:
        la = data.numeric["log_amount"]
        cands["amount_only"] = ([-1.0 if v is None else v for v in la], "rank by log amount (no training)")
    for name, col in label_free.items():
        cands[name] = (
            [-1.0 if v is None else float(v) for v in col],
            "label-free score from the forensic pipeline (no training)",
        )
    if "txn_type" in data.categorical:
        tt = data.categorical["txn_type"]
        n_by, p_by = defaultdict(int), defaultdict(int)
        for i in tr:
            n_by[tt[i]] += 1
            p_by[tt[i]] += data.y[i]
        base = sum(data.y[i] for i in tr) / len(tr)
        cands["type_rate"] = (
            [p_by[tt[i]] / n_by[tt[i]] if n_by[tt[i]] else base for i in range(data.n)],
            "positive rate of the transaction type in the training split",
        )
    for name, (scores, desc) in cands.items():
        thr = threshold_from_validation(_sub(scores, va), _sub(ids, va), ev.review_share) if va else None
        out.append(
            {
                "baseline": name,
                "description": desc,
                "threshold": thr,
                "test": _score_block(
                    _sub(scores, te),
                    _sub(data.y, te),
                    _sub(ids, te),
                    _sub(split.clusters, te) if split.clusters else None,
                    thr["value"] if thr else None,
                    ev,
                ),
            }
        )
    return out


def _interpret(model, pre: Preprocessor, cols_val, y_val, ids_val, base_ap, repeats, seed) -> dict[str, Any]:
    names = pre.names
    out: dict[str, Any] = {"caveat": "Associations learnt from these labels, not causal explanations."}
    if model.name == "logistic_regression":
        coef = sorted(zip(names, model.coef, strict=True), key=lambda t: (-abs(t[1]), t[0]))
        out["coefficients"] = [
            {
                "feature": n,
                "coefficient": round(c, 6),
                "direction": "raises score" if c > 0 else "lowers score",
                "odds_ratio_per_sd": round(math.exp(c), 4),
            }
            for n, c in coef
        ]
        out["coefficients_note"] = (
            "Inputs are standardised on the training split: a coefficient is the change in log-odds per "
            "training standard deviation (indicators and one-hots: per unit)."
        )
    else:
        agg = defaultdict(float)
        for n, g in zip(names, model.gain, strict=True):
            agg[n.split("=")[0].replace("missing:", "")] += g
        total = sum(agg.values()) or 1.0
        out["gain_importance"] = [
            {"feature": n, "share": round(g / total, 6)}
            for n, g in sorted(agg.items(), key=lambda t: (-t[1], t[0]))
        ]
    if base_ap is None:
        return out
    rng = random.Random(seed)  # noqa: S311 — reproducible permutation
    perm = []
    for j, n in enumerate(names):
        drops = []
        for _ in range(repeats):
            shuffled = cols_val[j][:]
            rng.shuffle(shuffled)
            cols = cols_val[:j] + [shuffled] + cols_val[j + 1 :]
            ap = metrics.average_precision(model.score(cols), y_val)
            drops.append(base_ap - (ap or 0.0))
        perm.append({"feature": n, "pr_auc_drop": round(sum(drops) / len(drops), 6)})
    out["permutation_importance"] = sorted(perm, key=lambda d: (-d["pr_auc_drop"], d["feature"]))
    out["permutation_note"] = (
        f"mean PR-AUC drop over {repeats} shuffles of each column, on the validation split"
    )
    return out


def _run_model(
    data, split, model_name, imbalance, ablation, removed, settings, identity, interpret, fitted=False
):
    """One experiment record; with `fitted`, also the fitted preprocessor and model (P11 persistence)."""
    sc = settings.supervised
    ev = sc.evaluation
    tr, va, te = split.parts["train"], split.parts["validation"], split.parts["test"]
    started = time.perf_counter()
    pre = Preprocessor.fit(data, tr, sc.features, removed)
    std = model_name == "logistic_regression"
    xt = pre.transform(data, tr, std)
    xv = pre.transform(data, va, std)
    xs = pre.transform(data, te, std)
    yt, yv, ys = _sub(data.y, tr), _sub(data.y, va), _sub(data.y, te)
    w = _weights(yt, imbalance)
    model = models.make(model_name, sc.models).fit(xt, yt, w)
    sv, st = model.score(xv), model.score(xs)
    iv, its = _sub(data.txn_ids, va), _sub(data.txn_ids, te)
    thr = threshold_from_validation(sv, iv, ev.review_share)
    val = metrics.evaluate(sv, yv, iv, ev.top_k, thr["value"])
    test = _score_block(st, ys, its, _sub(split.clusters, te) if split.clusters else None, thr["value"], ev)
    cal = calibration.assess(sv, yv, st, ys, sc.calibration, ev.calibration_bins)
    hyper = (sc.models.logistic_regression if std else sc.models.gradient_boosting).model_dump()
    ident = identity | {
        "split": split.strategy,
        "model": model_name,
        "imbalance": imbalance,
        "ablation": ablation,
    }
    rec = {
        "experiment_id": "E-" + sha256_hex(canonical_json(ident))[:16],
        "model": model_name,
        "imbalance": imbalance if imbalance == "none" else "class weights (balanced, from training counts)",
        "ablation": ablation,
        "features_removed": sorted(removed),
        "split": split.strategy,
        "hyperparameters": hyper,
        "fit": model.describe(),
        "model_sha256": model.sha256(),
        "preprocessing": pre.describe(),
        "training_rows": len(tr),
        "training_positives": sum(yt),
        "class_weights": {
            "positive": w[yt.index(1)] if 1 in yt else None,
            "negative": w[yt.index(0)] if 0 in yt else None,
        },
        "threshold": thr,
        "validation": val,
        "test": test,
        "calibration": cal,
        "output_label": cal["output_label"],
    }
    if interpret:
        rec["interpretation"] = _interpret(
            model, pre, xv, yv, iv, val["pr_auc"], sc.permutation_repeats, sc.seed
        )
    rec["_seconds"] = round(time.perf_counter() - started, 2)
    return (rec, pre, model) if fitted else rec


def run(
    data: TrainingData,
    label_free: dict[str, list[float | None]],
    settings: LedgerSpyConfig,
    identity: dict[str, Any],
    label_info: dict[str, Any],
) -> dict[str, Any]:
    sc = settings.supervised
    warnings: list[str] = []
    split_reports, runs, baselines = [], [], []
    ablations = {
        name: set(rm)
        for name, rm in sc.features.ablations.items()
        if set(rm) <= (set(data.numeric) | set(data.categorical))  # only where every removed feature exists
    }
    for strategy in sc.splits.strategies:
        split = splits.make(strategy, data, sc.splits, sc.seed)
        desc = split.describe(data)
        split_reports.append(desc)
        if not all(desc["positives"][p] for p in splits.PARTS):
            warnings.append(
                f"{strategy}: a split has no positives ({desc['positives']}); no model is evaluated on it"
            )
            continue
        baselines.append({"split": strategy, "baselines": _baselines(data, split, label_free, settings)})
        for model_name in ("logistic_regression", "gradient_boosting"):
            for imbalance in sc.imbalance:
                for ablation, removed in [(None, set()), *ablations.items()]:
                    interpret = imbalance == sc.imbalance[-1] and ablation is None
                    runs.append(
                        _run_model(
                            data,
                            split,
                            model_name,
                            imbalance,
                            ablation,
                            removed,
                            settings,
                            identity,
                            interpret,
                        )
                    )
    for r in runs:
        if r["test"]["positives"] < 20:
            warnings.append(
                f"{r['split']}/{r['model']}: only {r['test']['positives']} test positives — "
                "metrics are highly uncertain"
            )
            break
    report = {
        "report_version": REPORT_VERSION,
        "kind": "supervised_evaluation (experiment)",
        "note": NOTE,
        "identity": identity,
        "labels": label_info,
        "contract": data.contract,
        "rows": {"labelled": data.n, "positives": sum(data.y), **data.excluded_rows},
        "splits": split_reports,
        "baselines": baselines,
        "runs": runs,
        "comparison": comparison(baselines, runs, sc.evaluation.top_k[-1]),
        "warnings": warnings,
        "limitations": LIMITATIONS,
        "software": {
            "ledgerspy": __version__,
            "supervised_version": SUPERVISED_VERSION,
            "python": platform.python_version(),
            "implementation": "pure Python (no scikit-learn)",
        },
    }
    timings = {r["experiment_id"]: r.pop("_seconds") for r in runs}
    report = _rnd(report)
    report["result_sha256"] = sha256_hex(canonical_json(report))
    report["run_at"] = datetime.now(UTC).isoformat()
    report["timings_seconds"] = timings
    return report


def comparison(baselines: list[dict], runs: list[dict], k: int) -> list[dict[str, Any]]:
    """Model | PR-AUC | ROC-AUC | Precision@K | Recall@K | F1 | Calibration | Notes, per split."""
    rows = []

    def row(split, name, test, cal, notes):
        ci = test["confidence_intervals"]["intervals"]
        rows.append(
            {
                "split": split,
                "model": name,
                "prevalence": test["prevalence"],
                "pr_auc": test["pr_auc"],
                "pr_auc_ci": [ci.get("pr_auc", {}).get("low"), ci.get("pr_auc", {}).get("high")],
                "roc_auc": test["roc_auc"],
                f"precision@{k}": test["at_k"][str(k)]["precision"],
                f"recall@{k}": test["at_k"][str(k)]["recall"],
                "f1_at_threshold": test["at_threshold"]["f1"] if test.get("at_threshold") else None,
                "calibration": cal,
                "notes": notes,
            }
        )

    for b in baselines:
        for x in b["baselines"]:
            row(b["split"], "baseline: " + x["baseline"], x["test"], "n/a (ranking)", x["description"])
    for r in runs:
        name = (
            r["model"]
            + (" + class weights" if r["imbalance"] != "none" else "")
            + (f" [{r['ablation']}]" if r["ablation"] else "")
        )
        row(r["split"], name, r["test"], r["calibration"]["status"], r["output_label"])
    return rows
