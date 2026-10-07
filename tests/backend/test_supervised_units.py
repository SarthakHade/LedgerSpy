"""P10 units: metrics, models, splits, preprocessing, imbalance, threshold and calibration."""

import math
import random
from datetime import datetime, timedelta

import pytest

from ledgerspy.config import load_config
from ledgerspy.supervised import calibration, experiment, metrics, splits
from ledgerspy.supervised.dataset import Preprocessor, TrainingData
from ledgerspy.supervised.models import GradientBoosting, LogisticRegression

CFG = load_config().settings.supervised


def toy(n: int = 600, seed: int = 1, pos_rate: float = 0.1, entities: int = 60) -> TrainingData:
    """Rows with a signal in x1 (positives shifted), noise in x2, a categorical, nulls and timestamps."""
    rng = random.Random(seed)  # noqa: S311 — test data
    y = [int(rng.random() < pos_rate) for _ in range(n)]
    x1 = [rng.gauss(2.0 if v else 0.0, 1.0) for v in y]
    x2 = [rng.gauss(0, 1) if i % 7 else None for i in range(n)]
    cat = ["A" if rng.random() < 0.5 else "B" for _ in range(n)]
    t0 = datetime(2024, 1, 1)
    return TrainingData(
        txn_ids=[f"T{i:05d}" for i in range(n)],
        y=y,
        timestamps=[t0 + timedelta(hours=i) for i in range(n)],
        entity_groups=[f"out:e{i % entities}" for i in range(n)],
        scenario_groups=[["S-1"] if v and i % 3 == 0 else [] for i, v in enumerate(y)],
        numeric={"x1": x1, "x2": x2},
        categorical={"txn_type": cat},
        contract={"contract_sha256": "x"},
    )


def fcfg(**kw):
    return CFG.features.model_copy(
        update={"numeric": ["x1", "x2"], "log_transform": [], "ablations": {}} | kw
    )


# --------------------------------------------------------------------------------------------- metrics


def test_average_precision_and_roc_auc_hand_examples():
    s, y = [0.9, 0.8, 0.7, 0.1], [1, 0, 1, 0]
    assert metrics.average_precision(s, y) == pytest.approx(0.5 * 1 + 0.5 * 2 / 3)
    assert metrics.roc_auc(s, y) == pytest.approx(0.75)  # positives beat 3 of 4 (pos, neg) pairs
    assert metrics.roc_auc([0.5, 0.5], [1, 0]) == 0.5  # ties count half
    assert metrics.average_precision([0.5, 0.5], [1, 0]) == 0.5  # no optimistic tie order
    assert metrics.roc_auc([0.1, 0.2], [0, 0]) is None and metrics.average_precision([0.1], [0]) is None


def test_precision_recall_f1_and_confusion_at_threshold():
    s, y = [0.9, 0.8, 0.7, 0.2, 0.1], [1, 0, 1, 1, 0]
    r = metrics.at_threshold(s, y, 0.7)
    assert r["confusion"] == {"tp": 2, "fp": 1, "fn": 1, "tn": 1}
    assert r["precision"] == pytest.approx(2 / 3) and r["recall"] == pytest.approx(2 / 3)
    assert r["f1"] == pytest.approx(2 / 3)
    k = metrics.at_k(s, y, [f"t{i}" for i in range(5)], 2)
    assert (k["precision"], k["recall"]) == (0.5, pytest.approx(1 / 3))


def test_prevalence_baseline_pr_auc_equals_the_base_rate():
    y = [1, 0, 0, 0, 1, 0, 0, 0, 0, 0]
    assert metrics.average_precision([0.0] * 10, y) == pytest.approx(0.2)


def test_brier_and_ece():
    p, y = [0.9, 0.1, 0.8, 0.2], [1, 0, 1, 0]
    assert metrics.brier(p, y) == pytest.approx((0.01 + 0.01 + 0.04 + 0.04) / 4)
    e, table = metrics.ece([0.95, 0.95, 0.05, 0.05], [1, 1, 0, 0], 10)
    assert e == pytest.approx(0.05) and sum(t["rows"] for t in table) == 4


def test_bootstrap_is_deterministic_and_cluster_aware():
    d = toy()
    ids, s = d.txn_ids, d.numeric["x1"]
    a = metrics.bootstrap(s, d.y, ids, None, [10], 100, 3, 0.95)
    b = metrics.bootstrap(s, d.y, ids, None, [10], 100, 3, 0.95)
    c = metrics.bootstrap(s, d.y, ids, d.entity_groups, [10], 100, 3, 0.95)
    assert (
        a == b and a["method"].startswith("row") and c["method"].startswith("cluster") and c["clusters"] == 60
    )
    lo, hi = a["intervals"]["pr_auc"]["low"], a["intervals"]["pr_auc"]["high"]
    assert lo <= metrics.average_precision(s, d.y) <= hi


# --------------------------------------------------------------------------------------------- models


def test_logistic_regression_recovers_a_known_direction_and_is_deterministic():
    d = toy()
    rows = list(range(d.n))
    pre = Preprocessor.fit(d, rows, fcfg(), set())
    x = pre.transform(d, rows, True)
    m1 = LogisticRegression(1.0, 50, 1e-10).fit(x, d.y, [1.0] * d.n)
    m2 = LogisticRegression(1.0, 50, 1e-10).fit(x, d.y, [1.0] * d.n)
    assert m1.converged and m1.sha256() == m2.sha256()
    coef = dict(zip(pre.names, m1.coef, strict=True))
    assert coef["x1"] > 1.0 and abs(coef["x2"]) < 0.5  # signal vs noise
    assert metrics.roc_auc(m1.score(x), d.y) > 0.85


def test_logistic_regression_gradient_is_zero_at_the_optimum():
    d = toy(n=300)
    rows = list(range(d.n))
    x = Preprocessor.fit(d, rows, fcfg(), set()).transform(d, rows, True)
    m = LogisticRegression(2.0, 100, 1e-12).fit(x, d.y, [1.0] * d.n)
    p = m.score(x)
    for j, col in enumerate(x):
        g = sum((yi - pi) * xi for yi, pi, xi in zip(d.y, p, col, strict=True)) - 2.0 * m.coef[j]
        assert abs(g) < 1e-6


def test_gradient_boosting_learns_an_interaction_and_is_deterministic():
    rng = random.Random(5)  # noqa: S311 — test data
    a = [rng.random() for _ in range(800)]
    b = [rng.random() for _ in range(800)]
    y = [int((ai > 0.5) != (bi > 0.5)) for ai, bi in zip(a, b, strict=True)]  # XOR: no linear model can
    gb = GradientBoosting(60, 0.3, 3, 16, 10, 1.0).fit([a, b], y, [1.0] * 800)
    gb2 = GradientBoosting(60, 0.3, 3, 16, 10, 1.0).fit([a, b], y, [1.0] * 800)
    assert gb.sha256() == gb2.sha256()
    assert metrics.roc_auc(gb.score([a, b]), y) > 0.95
    lr = LogisticRegression(1.0, 50, 1e-8).fit([a, b], y, [1.0] * 800)
    assert metrics.roc_auc(lr.score([a, b]), y) < 0.65
    assert all(nd[0] in ("leaf", "split") for t in gb.trees for nd in t)


def test_gradient_boosting_respects_min_samples_leaf():
    d = toy()
    rows = list(range(d.n))
    x = Preprocessor.fit(d, rows, fcfg(), set()).transform(d, rows, False)
    gb = GradientBoosting(5, 0.1, 3, 32, 100, 1.0).fit(x, d.y, [1.0] * d.n)
    binned_rows = []
    for t in gb.trees:
        leaves = sum(1 for nd in t if nd[0] == "leaf")
        binned_rows.append(leaves)
    assert max(binned_rows) <= d.n // 100  # every leaf holds ≥ 100 rows


# --------------------------------------------------------------------------------------------- splits


@pytest.mark.parametrize("strategy", ["random_stratified", "chronological", "group"])
def test_splits_are_deterministic_disjoint_and_complete(strategy):
    d = toy()
    a = splits.make(strategy, d, CFG.splits, 42)
    b = splits.make(strategy, d, CFG.splits, 42)
    assert a.parts == b.parts
    sets = [set(a.parts[p]) for p in splits.PARTS]
    assert not (sets[0] & sets[1]) and not (sets[0] & sets[2]) and not (sets[1] & sets[2])
    assert len(set().union(*sets)) + a.excluded == d.n
    if strategy != "chronological":
        assert splits.make(strategy, d, CFG.splits, 43).parts != a.parts  # the seed matters


def test_chronological_split_orders_time():
    d = toy()
    s = splits.make("chronological", d, CFG.splits, 42)
    tmax = {p: max(d.timestamps[i] for i in s.parts[p]) for p in splits.PARTS}
    tmin = {p: min(d.timestamps[i] for i in s.parts[p]) for p in splits.PARTS}
    assert tmax["train"] < tmin["validation"] and tmax["validation"] < tmin["test"]


def test_chronological_split_keeps_equal_timestamps_together():
    d = toy(n=100)
    d.timestamps = [datetime(2024, 1, 1) + timedelta(days=i // 10) for i in range(100)]
    s = splits.make("chronological", d, CFG.splits, 42)
    days = {p: {d.timestamps[i] for i in s.parts[p]} for p in splits.PARTS}
    assert not (days["train"] & days["validation"]) and not (days["validation"] & days["test"])


def test_group_split_isolates_entities_and_scenarios():
    d = toy()
    s = splits.make("group", d, CFG.splits, 42)
    desc = s.describe(d)
    assert desc["counterparties_in_train_and_test"] == 0 and desc["scenarios_in_train_and_test"] == []
    ent = {p: {d.entity_groups[i] for i in s.parts[p]} for p in splits.PARTS}
    assert not (ent["train"] & ent["validation"]) and not (ent["validation"] & ent["test"])
    r = splits.make("random_stratified", d, CFG.splits, 42).describe(d)
    assert r["counterparties_in_train_and_test"] > 0  # a row-level split shares entities: reported


# --------------------------------------------------------------------------------------------- preprocessing


def test_preprocessor_uses_training_rows_only():
    d = toy()
    s = splits.make("random_stratified", d, CFG.splits, 42)
    p1 = Preprocessor.fit(d, s.parts["train"], fcfg(), set())
    for i in s.parts["test"]:
        d.numeric["x1"][i] = 1e9  # anything in the test rows
        d.categorical["txn_type"][i] = "UNSEEN"
    p2 = Preprocessor.fit(d, s.parts["train"], fcfg(), set())
    assert p1.describe() == p2.describe()
    x = p2.transform(d, s.parts["test"], True)
    one_hot = [c for n, c in zip(p2.names, x, strict=True) if n.startswith("txn_type=")]
    assert all(v == 0.0 for col in one_hot for v in col)  # an unseen category is all zeros


def test_preprocessor_imputes_with_indicator_and_drops_constant_or_missing():
    d = toy()
    d.numeric["flat"] = [3.0] * d.n
    d.numeric["empty"] = [None] * (d.n - 5) + [1.0] * 5
    p = Preprocessor.fit(d, list(range(d.n)), fcfg(numeric=["x1", "x2", "flat", "empty"]), set())
    assert {x["feature"]: x["reason"] for x in p.dropped} == {
        "flat": "constant in training rows",
        "empty": "99% null in training rows",
    }
    assert "missing:x2" in p.names and "missing:x1" not in p.names
    removed = Preprocessor.fit(d, list(range(d.n)), fcfg(), {"txn_type"})
    assert not any(n.startswith("txn_type=") for n in removed.names)


# --------------------------------------------------------------------------------------------- imbalance


def test_class_weights_come_from_training_counts_and_never_resample():
    y = [1, 0, 0, 0]
    w = experiment._weights(y, "balanced")
    assert w == [2.0, 4 / 6, 4 / 6, 4 / 6] and sum(w[i] for i in range(4) if y[i]) == pytest.approx(2.0)
    assert experiment._weights(y, "none") == [1.0] * 4
    assert len(w) == len(y)  # no oversampling: one weight per original row


# --------------------------------------------------------------------------------------------- threshold


def test_threshold_is_the_review_share_of_validation_rows():
    scores = [i / 100 for i in range(100)]
    ids = [f"t{i}" for i in range(100)]
    t = experiment.threshold_from_validation(scores, ids, 0.02)
    assert t["value"] == 0.98 and t["validation_top_k"] == 2


# --------------------------------------------------------------------------------------------- calibration


def test_platt_scaling_recovers_a_known_mapping():
    rng = random.Random(9)  # noqa: S311 — test data
    true_a, true_b = 0.5, -1.0
    s = [rng.random() for _ in range(4000)]
    y = [int(rng.random() < 1 / (1 + math.exp(-(true_a * math.log(v / (1 - v)) + true_b)))) for v in s]
    a, b = calibration.fit_platt(s, y)
    assert a == pytest.approx(true_a, abs=0.1) and b == pytest.approx(true_b, abs=0.15)


def test_calibration_is_not_attempted_with_few_validation_positives():
    cfg = CFG.calibration
    out = calibration.assess([0.1, 0.9], [0, 1], [0.2, 0.8], [0, 1], cfg, 10)
    assert out["status"] == "not_attempted" and "ranking score" in out["output_label"]
    assert "calibrated_test" not in out
