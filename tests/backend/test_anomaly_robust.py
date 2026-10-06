"""P8 robust peer z-scores, tail percentiles, populations (warm-up, chronological split), signal contract and
evaluation metrics."""

import math
import random

import pytest

from ledgerspy.anomaly import evaluation
from ledgerspy.anomaly.robust_stats import MAD_TO_SIGMA, MEAN_AD_TO_SIGMA, center_scale, mid_rank_percentile
from ledgerspy.anomaly.robust_stats import tail_cutoff as cutoff
from ledgerspy.canonical import Signal, SignalFamily
from tests.backend.anomaly_support import detector, flagged, ledger, report, score_of, signals
from tests.backend.features_support import ALL_FIELDS, rec

ROBUST, TAIL, IF = "robust_peer", "tail_percentile", "isolation_forest"
AMOUNT_ONLY = {
    "robust_peer": {
        "features": [
            {
                "name": "log_amount",
                "transform": "none",
                "direction": "both",
                "peers": ["counterparty", "account"],
                "min_scale": 0.1,
            }
        ]
    },
    "isolation_forest": {"enabled": False},
    "tail": {"enabled": False},
}


def peers_ledger() -> list[dict]:
    """Vendor A: 30 ordinary rows + one at 10x. Vendor B: 10 rows (too few on its own) in A's account.
    Vendor C: 5 rows alone in its own account."""
    rng = random.Random(5)  # noqa: S311 — test data
    rows, n = [], 0
    for k in range(30):
        n += 1
        rows.append(rec(n, at=k, amount=int(1000_00 * rng.uniform(0.9, 1.1)), payee="Vendor A"))
    n += 1
    rows.append(rec(n, at=40, amount=10_000_00, payee="Vendor A"))
    for k in range(10):
        n += 1
        rows.append(rec(n, at=k + 0.5, amount=int(1100_00 * rng.uniform(0.9, 1.1)), payee="Vendor B"))
    n += 1
    rows.append(rec(n, at=41, amount=9_000_00, payee="Vendor B"))
    for k in range(5):
        n += 1
        rows.append(rec(n, at=k + 0.7, amount=500_00, payee="Vendor C", account="5999"))
    return rows


# --------------------------------------------------------------------------------------------- robust z


def test_hand_computed_median_mad_and_z():
    m, scale, method = center_scale([1.0, 2.0, 3.0, 4.0, 100.0])
    assert (m, method) == (3.0, "mad") and scale == pytest.approx(MAD_TO_SIGMA * 1.0)
    assert (100.0 - m) / scale == pytest.approx(97 / 1.4826)


def test_zero_mad_uses_mean_absolute_deviation_and_all_equal_is_undefined():
    m, scale, method = center_scale([5.0] * 10 + [6.0, 7.0])
    assert (m, method) == (5.0, "mean_ad") and scale == pytest.approx(MEAN_AD_TO_SIGMA * 3 / 12)
    assert center_scale([5.0] * 12) == (5.0, None, "zero_dispersion")


def test_obvious_outlier_is_flagged_against_its_own_counterparty():
    rep = report(peers_ledger(), anomaly=AMOUNT_ONLY)
    (s,) = [s for s in signals(rep, ROBUST) if s["subject_id"] == "T00031"]
    dev = s["evidence"]["deviations"][0]
    assert dev["peer"] == "counterparty" and dev["n"] == 31 and dev["z"] > 3.5
    assert dev["shown_feature"] == "amount_minor" and dev["shown_value"] == 10_000_00
    assert "robust SDs above the median" in s["explanation"] and "not a finding of fraud" in s["explanation"]
    assert s["population"]["kind"] == "peer_group" and s["family"] == "model_anomaly"


def test_small_counterparty_is_judged_by_its_account_and_the_basis_is_reported():
    rep = report(peers_ledger(), anomaly=AMOUNT_ONLY)
    det = detector(rep, ROBUST)["details"]["by_feature"]["log_amount"]
    assert det["basis"] == {"counterparty": 31, "account": 11}
    (s,) = [s for s in signals(rep, ROBUST) if s["subject_id"] == "T00042"]  # Vendor B's 9,000
    assert s["evidence"]["deviations"][0]["peer"] == "account"


def test_insufficient_peer_group_is_reported_never_replaced_by_a_global_baseline():
    rep = report(peers_ledger(), anomaly=AMOUNT_ONLY)
    d = detector(rep, ROBUST)
    assert d["details"]["by_feature"]["log_amount"]["rows_insufficient"] == {"no_sufficient_peer_group": 5}
    status = score_of(rep, ROBUST, "status")
    assert [status[f"T{n:05d}"] for n in range(43, 48)] == ["insufficient_data"] * 5
    assert all(score_of(rep, ROBUST, "max_abs_z")[f"T{n:05d}"] is None for n in range(43, 48))


def test_constant_group_uses_the_resolution_floor():
    rows = [rec(n, at=n, amount=500_00, payee="Fixed") for n in range(1, 31)]
    rows.append(rec(31, at=40, amount=520_00, payee="Fixed"))  # 4% off: within resolution
    rows.append(rec(32, at=41, amount=900_00, payee="Fixed"))  # 80% off
    rep = report(rows, anomaly=AMOUNT_ONLY)
    assert flagged(rep, ROBUST) == ["T00032"]
    assert signals(rep, ROBUST)[0]["evidence"]["deviations"][0]["scale_method"] == "min_scale"


def test_missing_values_are_counted_not_scored():
    rows = peers_ledger() + [rec(48, at=42, amount=None, payee="Vendor A")]
    rep = report(rows, anomaly=AMOUNT_ONLY)
    assert (
        detector(rep, ROBUST)["details"]["by_feature"]["log_amount"]["rows_insufficient"]["missing_value"]
        == 1
    )
    assert score_of(rep, ROBUST, "status")["T00048"] == "insufficient_data"


def test_direction_is_respected():
    cfg = {
        **AMOUNT_ONLY,
        "robust_peer": {"features": [AMOUNT_ONLY["robust_peer"]["features"][0] | {"direction": "lower"}]},
    }
    rep = report(peers_ledger(), anomaly=cfg)
    assert "T00031" not in flagged(rep, ROBUST)  # a high amount is not "unusually low"


def test_unsupported_peer_dimension_is_not_available_with_its_reason():
    rep = report(
        peers_ledger(),
        mapped=ALL_FIELDS - {"txn_type"},
        anomaly={
            **AMOUNT_ONLY,
            "robust_peer": {
                "features": [
                    AMOUNT_ONLY["robust_peer"]["features"][0] | {"peers": ["counterparty", "txn_type"]}
                ]
            },
        },
    )
    peer = detector(rep, ROBUST)["details"]["by_feature"]["log_amount"]["peers"]["txn_type"]
    assert peer["status"] == "not_available" and peer["reason"]


def test_robust_result_is_independent_of_row_order():
    rows = peers_ledger()
    base = report(rows, anomaly=AMOUNT_ONLY)
    random.Random(9).shuffle(rows)  # noqa: S311 — test data
    assert report(rows, anomaly=AMOUNT_ONLY)["report_sha256"] == base["report_sha256"]


# ------------------------------------------------------------------------------------------- tail percentile

TAIL_ONLY = {
    "tail": {"features": [{"name": "amount_minor", "direction": "higher"}]},
    "isolation_forest": {"enabled": False},
    "robust_peer": {"enabled": False},
}


def test_known_percentiles_and_tail_boundary():
    values = [float(v) for v in range(1, 201)]
    assert mid_rank_percentile(values, 200.0) == 0.9975
    assert mid_rank_percentile(values, 199.0) == 0.9925
    assert mid_rank_percentile(values, 198.0) == 0.9875  # just outside the top 1%
    assert cutoff(values, 0.01, "higher") == 199.0 and cutoff(values, 0.005, "higher") == 200.0
    assert cutoff(values, 0.01, "lower") == 2.0


def test_ties_never_inflate_a_tail():
    values = sorted([float(v) for v in range(1, 191)] + [500.0] * 10)
    assert mid_rank_percentile(values, 500.0) == pytest.approx(0.975)
    assert cutoff(values, 0.01, "higher") is None


def test_tail_detector_flags_exactly_the_top_one_percent():
    rows = [rec(n, at=n * 0.5, amount=n * 100_00, payee=f"V{n % 7}") for n in range(1, 201)]
    rep = report(rows, anomaly=TAIL_ONLY)
    assert flagged(rep, TAIL) == ["T00199", "T00200"]
    top = next(s for s in signals(rep, TAIL) if s["subject_id"] == "T00200")
    t = top["evidence"]["tails"][0]
    assert (t["n"], t["percentile"], t["tail"], t["cutoff"]) == (200, 0.9975, 0.005, 200_00_00)
    assert top["population"] == {"kind": "flow", "group": "flow=out", "n": 200, "reference": "dataset"}
    assert "top 0.5%" in top["explanation"] and "not a finding of fraud" in top["explanation"]
    assert next(s for s in signals(rep, TAIL) if s["subject_id"] == "T00199")["tail"] == 0.01
    assert [s["subject_id"] for s in rep["signals"]] == sorted(s["subject_id"] for s in rep["signals"])


def test_tail_population_below_minimum_is_insufficient():
    rows = [rec(n, at=n, amount=n * 100_00) for n in range(1, 151)]
    rep = report(rows, anomaly=TAIL_ONLY)
    d = detector(rep, TAIL)
    assert flagged(rep, TAIL) == [] and d["rows_scored"] == 0
    assert d["details"]["by_feature"]["amount_minor"]["rows_insufficient"] == {"population_too_small": 150}
    assert d["details"]["by_feature"]["amount_minor"]["populations"]["out"]["status"] == "insufficient_data"


def test_flows_are_separate_populations():
    rows = [rec(n, at=n, amount=n * 100_00) for n in range(1, 201)]
    rows += [rec(200 + n, at=n, amount=10**9, direction="credit", payer=f"C{n}") for n in range(1, 11)]
    rep = report(rows, anomaly=TAIL_ONLY)
    assert flagged(rep, TAIL) == ["T00199", "T00200"]  # 10 huge receipts are not a 200-row population


# -------------------------------------------------------------------------------------------- populations


def test_warm_up_rows_are_insufficient_and_outside_the_reference():
    rows = ledger()  # 10 vendors, ~120 days
    rep = report(rows, anomaly={"reference": {"warmup_days": 30}})
    warm = rep["population"]["warm_up"]
    assert warm["applied"] and warm["rows"] > 0
    assert rep["population"]["reference_rows"] == len(rows) - warm["rows"]
    for det in rep["detectors"]:
        assert det["insufficient_reasons"].get("warm_up") == warm["rows"], det["detector_id"]


def test_chronological_mode_never_lets_future_rows_into_the_reference():
    rows = ledger(rows_per_vendor=60)
    cfg = {"reference": {"mode": "chronological", "chronological_fraction": 0.6}}
    rep = report(rows, anomaly=cfg)
    pop = rep["population"]
    assert pop["mode"] == "chronological" and pop["reference_rows"] + pop["scoring_rows"] == len(rows)
    status = score_of(rep, IF, "status")
    split = pop["split_timestamp"]
    for r in rows:
        expected = "reference_only" if r["posted_at"] < split else None
        if expected:
            assert status[r["txn_id"]] == expected
    # Rewriting every row after the split changes neither the forest nor the reference statistics.
    future = [r | {"amount_minor": r["amount_minor"] * 50} if r["posted_at"] >= split else r for r in rows]
    again = report(future, anomaly=cfg)
    model = detector(rep, IF)["details"]["model"]
    assert detector(again, IF)["details"]["model"]["model_sha256"] == model["model_sha256"]
    assert (
        detector(again, TAIL)["details"]["by_feature"]["amount_minor"]["populations"]
        == detector(rep, TAIL)["details"]["by_feature"]["amount_minor"]["populations"]
    )
    assert len(flagged(again, TAIL)) > len(flagged(rep, TAIL))  # the future is scored against the past


def test_chronological_mode_needs_timestamps():
    rows = [rec(n, at=None) for n in range(1, 50)]
    rep = report(rows, anomaly={"reference": {"mode": "chronological"}})
    assert {d["status"] for d in rep["detectors"]} == {"not_available"}


# ----------------------------------------------------------------------------------------- signal contract


def test_signals_convert_to_the_canonical_contract_deterministically():
    rep = report(peers_ledger(), anomaly=AMOUNT_ONLY)
    s = signals(rep, ROBUST)[0]
    assert s["signal_id"].startswith("S-") and s["code"] == "model_anomaly.robust_peer"
    for key in (
        "detector_id",
        "detector_version",
        "subject_type",
        "subject_id",
        "status",
        "anomaly_score",
        "percentile",
        "tail",
        "population",
        "features",
        "evidence",
        "explanation",
        "metadata",
    ):
        assert key in s, key
    again = report(peers_ledger(), anomaly=AMOUNT_ONLY)
    assert signals(again, ROBUST)[0]["signal_id"] == s["signal_id"]


def test_anomaly_result_to_canonical():
    from ledgerspy.anomaly.models import AnomalyResult

    r = AnomalyResult(
        "tail_percentile",
        1,
        "transaction",
        "T1",
        ("T1",),
        0.998,
        0.998,
        0.005,
        {"kind": "flow"},
        {"amount_minor": 5},
        {"tails": []},
        "x",
        "low",
        0.6,
        0.2,
    )
    sig = r.to_canonical()
    assert isinstance(sig, Signal) and sig.family == SignalFamily.MODEL_ANOMALY and sig.raw_value == 0.998
    assert sig.evidence["population"] == {"kind": "flow"} and sig.template_id == "p7.tail_percentile.v1"


# --------------------------------------------------------------------------------------------- evaluation


def test_precision_recall_at_k():
    ranked = ["a", "b", "c", "d"]
    assert evaluation.precision_at_k(ranked, {"a", "c"}, 2) == 0.5
    assert evaluation.recall_at_k(ranked, {"a", "c", "z"}, 3) == pytest.approx(2 / 3)  # unscored = missed
    assert (
        evaluation.recall_at_k(ranked, set(), 3) is None and evaluation.precision_at_k([], {"a"}, 3) is None
    )


def test_average_precision_hand_example_and_ties():
    assert evaluation.average_precision({"a": 0.9, "b": 0.8, "c": 0.7}, {"a", "c"}) == pytest.approx(
        0.5 * 1 + 0.5 * 2 / 3
    )
    assert evaluation.average_precision({"a": 0.9, "b": 0.9}, {"a"}) == 0.5  # no optimistic tie order
    assert evaluation.average_precision({"a": 0.9, "b": None}, {"b"}) is None


def test_ranking_coverage_and_distribution_are_deterministic():
    scores = {"b": 0.5, "a": 0.5, "c": 0.9, "d": None}
    assert evaluation.ranking(scores) == ["c", "a", "b"]
    cov = evaluation.coverage_at_k(["c", "a", "b"], {"S1": {"a"}, "S2": {"z"}}, 2)
    assert cov["covered"] == ["S1"] and cov["share"] == 0.5
    dist = evaluation.score_distribution([0.1, 0.2, 0.3, None, 1.0])
    assert (
        dist["n"] == 4 and dist["min"] == 0.1 and dist["max"] == 1.0 and sum(dist["histogram"]["counts"]) == 4
    )
    out = evaluation.evaluate({"a": 0.9, "b": 0.1}, {"a"}, [1])
    assert out["at_k"]["1"] == {"precision": 1.0, "recall": 1.0} and out["prevalence"] == 0.5


def test_report_carries_no_label_metrics():
    rep = report(ledger())
    ev = rep["evaluation"]
    assert ev["labels_used"] is False and rep["inputs"]["labels_read"] is False
    assert "precision" not in str({k: v for k, v in ev.items() if k != "diagnostics_available"})
    assert set(ev["top_k_isolation_forest"]) == {"50", "100", "250"}
    assert math.isclose(
        sum(ev["score_distribution"]["isolation_forest"]["histogram"]["counts"]),
        detector(rep, IF)["rows_scored"],
    )
