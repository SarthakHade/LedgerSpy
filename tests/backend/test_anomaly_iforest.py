"""P8 Isolation Forest: the algorithm (pure Python) and the transaction-level detector."""

import math
import random

import pytest

from ledgerspy.anomaly.eligibility import FORBIDDEN
from ledgerspy.anomaly.isolation_forest import IsolationForest, c_factor
from ledgerspy.config import ConfigError
from tests.backend.anomaly_support import detector, flagged, ledger, report, score_of, signals
from tests.backend.features_support import rec

IF = "isolation_forest"
FEATURES = [
    {"name": "log_amount", "transform": "none"},
    {"name": "amount_robust_z", "transform": "none"},
    {"name": "amount_to_cp_median_ratio", "transform": "signed_log1p"},
    {"name": "cp_prior_count", "transform": "signed_log1p"},
    {"name": "cp_days_since_prev", "transform": "signed_log1p"},
]


def cluster(n: int, seed: int = 1) -> list[list[float]]:
    rng = random.Random(seed)  # noqa: S311 — test data
    return [[rng.gauss(0, 1) for _ in range(n)], [rng.gauss(0, 1) for _ in range(n)]]


def outlier_ledger() -> tuple[list[dict], str]:
    rows = ledger()
    n = len(rows) + 1
    rows.append(rec(n, at=130.0, amount=500_000_00, payee="Vendor 03", account="5103"))
    return rows, f"T{n:05d}"


def if_cfg(**extra) -> dict:
    return {"isolation_forest": {"features": FEATURES, **extra}}


# ------------------------------------------------------------------------------------------- algorithm


def test_c_factor_matches_the_paper():
    assert c_factor(1) == 0.0 and c_factor(2) == 1.0
    assert c_factor(256) == pytest.approx(2 * (math.log(255) + 0.5772156649) - 2 * 255 / 256)


def test_obvious_outlier_scores_highest_and_the_cluster_scores_normal():
    cols = cluster(500)
    cols[0].append(12.0)
    cols[1].append(-11.0)
    rows = list(range(501))
    forest = IsolationForest(100, 256, 42).fit(cols, rows)
    s = forest.score(cols, rows)
    assert max(s, key=s.get) == 500 and s[500] > 0.7  # higher = more unusual
    normal = sorted(s[i] for i in range(500))
    assert normal[len(normal) // 2] < 0.5  # a typical cluster point is not anomalous
    assert all(0.0 < v <= 1.0 for v in s.values())


def test_fixed_seed_gives_an_identical_forest_and_scores():
    cols = cluster(400)
    rows = list(range(400))
    a = IsolationForest(50, 128, 42).fit(cols, rows)
    b = IsolationForest(50, 128, 42).fit(cols, rows)
    assert a.sha256() == b.sha256() and a.score(cols, rows) == b.score(cols, rows)
    c = IsolationForest(50, 128, 43).fit(cols, rows)
    assert c.sha256() != a.sha256()


def test_constant_features_are_never_split_on():
    cols = cluster(300) + [[5.0] * 300]
    forest = IsolationForest(30, 64, 1).fit(cols, list(range(300)))
    assert all(f != 2 for t in forest.trees for f in t.feature)


def test_all_constant_data_isolates_nothing():
    cols = [[1.0] * 300, [2.0] * 300]
    forest = IsolationForest(20, 64, 1).fit(cols, list(range(300)))
    s = forest.score(cols, list(range(300)))
    assert len(set(s.values())) == 1  # every row equally (un)remarkable


# ------------------------------------------------------------------------------------------- detector


def test_detector_flags_the_planted_outlier_first():
    rows, out = outlier_ledger()
    rep = report(rows, anomaly=if_cfg())
    d = detector(rep, IF)
    # each vendor's first row has no history: too few present features to score
    assert d["status"] == "evaluated" and d["rows_scored"] == len(rows) - 10
    assert d["insufficient_reasons"] == {"too_few_present_features": 10}
    sig = signals(rep, IF)
    top = min(sig, key=lambda s: s["evidence"]["rank"])
    assert top["subject_id"] == out
    assert top["anomaly_score"] == max(v for v in score_of(rep, IF, "score").values() if v is not None)
    assert top["family"] == "model_anomaly" and top["triggered"] is True
    assert top["evidence"]["feature_deviation_method"] == "robust z vs reference rows (not SHAP)"
    assert top["evidence"]["feature_deviation"][0]["robust_z"] > 3
    assert "not a finding of fraud" in top["explanation"]
    # threshold policy: never more than the tail share or the review capacity
    assert len(sig) <= math.ceil(0.01 * len(rows))
    assert all(s["percentile"] >= 0.99 and s["anomaly_score"] > 0.5 for s in sig)


def test_review_capacity_caps_flagged_rows():
    rows, _ = outlier_ledger()
    rep = report(rows, anomaly=if_cfg(tail_fraction=0.2, max_flagged=3))
    assert len(flagged(rep, IF)) == 3
    assert detector(rep, IF)["details"]["capped_by_review_capacity"] > 0


def test_min_score_means_nothing_is_flagged_without_a_distinct_anomaly():
    rows, _ = outlier_ledger()
    assert flagged(report(rows, anomaly=if_cfg(min_score=0.99)), IF) == []


def test_missing_values_are_imputed_explicitly_with_an_indicator():
    rows, out = outlier_ledger()
    rep = report(rows, anomaly=if_cfg())
    d = detector(rep, IF)
    model = d["details"]["model"]
    # the vendor-baseline ratio is null until a vendor has 5 prior rows → reference median + indicator
    entry = next(f for f in model["model_features"] if f["feature"] == "amount_to_cp_median_ratio")
    assert entry["reference_nulls"] == 40 and "imputed_with_reference_median" in entry
    assert "missing:amount_to_cp_median_ratio" in model["model_inputs"]
    # evidence keeps the original null, never the imputed number
    for s in signals(rep, IF):
        imputed = s["evidence"]["imputed_features"]
        assert imputed == [k for k, v in s["features"].items() if v is None]


def test_rows_with_too_few_present_features_are_insufficient():
    rows, _ = outlier_ledger()
    rows.append(rec(len(rows) + 1, at=131.0, amount=None, payee="Vendor 01"))
    rep = report(rows, anomaly=if_cfg())
    d = detector(rep, IF)
    assert d["insufficient_reasons"] == {"too_few_present_features": 11}  # + each vendor's first row
    assert score_of(rep, IF, "status")[f"T{len(rows):05d}"] == "insufficient_data"
    assert score_of(rep, IF, "score")[f"T{len(rows):05d}"] is None  # no fabricated score


def test_constant_and_excessively_missing_features_are_excluded_with_reasons():
    rows, _ = outlier_ledger()
    feats = FEATURES + [
        {"name": "is_holiday", "transform": "none"},  # not available: no holiday calendar
        {"name": "dq_is_normalized", "transform": "none"},  # constant: nothing was normalised
        {"name": "cp_reference_gap", "transform": "none"},  # no reference values
    ]
    d = detector(report(rows, anomaly={"isolation_forest": {"features": feats}}), IF)
    excluded = {e["feature"]: e["reason"] for e in d["features"]["excluded"]}
    assert excluded["dq_is_normalized"] == "constant"
    assert excluded["is_holiday"] == "not_available"  # no holiday calendar configured
    assert excluded["cp_reference_gap"] == "excessive_missing"  # mapped, but every reference is null
    assert [e["feature"] for e in d["features"]["used"]] == [f["name"] for f in FEATURES]
    strict = {"eligibility": {"max_missing_share": 0.0}, **if_cfg()}
    excessive = detector(report(rows, anomaly=strict), IF)
    assert {e["feature"]: e["reason"] for e in excessive["features"]["excluded"]}[
        "cp_days_since_prev"
    ] == "excessive_missing"


def test_non_numeric_post_period_and_dataset_features_are_never_model_inputs():
    rows, _ = outlier_ledger()
    feats = FEATURES + [
        {"name": "txn_type", "transform": "none"},  # category
        {"name": "ent_txn_count", "transform": "none"},  # entity level
        {"name": "dq_issue_count", "transform": "none"},  # dataset (P4) semantics
    ]
    d = detector(report(rows, anomaly={"isolation_forest": {"features": feats}}), IF)
    excluded = {e["feature"]: e["reason"] for e in d["features"]["excluded"]}
    assert excluded == {
        "txn_type": "not_numeric",
        "ent_txn_count": "not_transaction_level",
        "dq_issue_count": "not_point_in_time",
    }


def test_unknown_feature_is_a_configuration_error():
    rows, _ = outlier_ledger()
    with pytest.raises(ConfigError, match="not a P6 registry feature"):
        report(rows, anomaly={"isolation_forest": {"features": [{"name": "isFraud", "transform": "none"}]}})


@pytest.mark.parametrize("name", ["isfraud", "is_fraud", "label", "txn_id", "scenario_id", "ground_truth"])
def test_label_and_identifier_names_are_forbidden(name):
    assert FORBIDDEN.search(name)


def test_insufficient_population_returns_insufficient_data_not_a_model():
    rows = ledger(vendors=3, rows_per_vendor=30)  # 90 rows < min_rows 256
    rep = report(rows, anomaly=if_cfg())
    d = detector(rep, IF)
    assert d["status"] == "insufficient_data" and "too little information" in d["reason"]
    assert "model" not in d["details"] and flagged(rep, IF) == []
    assert set(score_of(rep, IF, "status").values()) == {"insufficient_data"}
    assert rep["scores"][IF]["score"] is None


def test_too_few_usable_features_returns_insufficient_data():
    rows, _ = outlier_ledger()
    rep = report(rows, anomaly=if_cfg(min_features=6))
    assert detector(rep, IF)["status"] == "insufficient_data"


def test_labels_and_extra_columns_cannot_affect_scores():
    rows, _ = outlier_ledger()
    base = report(rows, anomaly=if_cfg())
    for i, r in enumerate(rows):
        r["extra"] = {"isFraud": str(i % 2), "scenario_id": "S-A-01", "label": "fraud"}
    assert report(rows, anomaly=if_cfg())["report_sha256"] == base["report_sha256"]


def test_shuffled_rows_give_identical_subject_level_scores():
    rows, _ = outlier_ledger()
    base = report(rows, anomaly=if_cfg())
    shuffled = rows[:]
    random.Random(3).shuffle(shuffled)  # noqa: S311 — test data
    again = report(shuffled, anomaly=if_cfg())
    assert again["report_sha256"] == base["report_sha256"]
    assert (
        detector(again, IF)["details"]["model"]["model_sha256"]
        == detector(base, IF)["details"]["model"]["model_sha256"]
    )


def test_disabled_detector_reports_disabled():
    rows, _ = outlier_ledger()
    rep = report(rows, anomaly=if_cfg(enabled=False))
    assert detector(rep, IF)["status"] == "disabled" and flagged(rep, IF) == []
