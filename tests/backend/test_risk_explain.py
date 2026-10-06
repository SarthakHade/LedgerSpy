"""P9 end-to-end on in-memory records: explanation consistency, entity aggregation, coverage and isolation."""

import copy
import random

import pytest

from ledgerspy.canonical import Explanation, Score
from ledgerspy.risk import service
from tests.backend.anomaly_support import ledger
from tests.backend.features_support import rec
from tests.backend.risk_support import result

DUPLICATE = "T00402"


def planted() -> list[dict]:
    rows = ledger()  # 10 vendors x 40 ordinary payments
    n = len(rows) + 1
    big = rec(n, at=130.0, amount=500_000_00, payee="Vendor 03", account="5103", reference="INV-9001")
    dup = dict(big, txn_id=f"T{n + 1:05d}", source_row=n + 1)  # the same payment booked twice
    return rows + [big, dup]


@pytest.fixture(scope="module")
def res():
    return result(planted())


def detail(res, txn_id):
    r, s = res
    return service.transaction_detail(r, txn_id, s)


def test_the_planted_duplicate_of_an_outlier_is_the_top_priority(res):
    r, _ = res
    row = r.by_txn[DUPLICATE]
    assert row["review_rank"] == 1 and row["tier"] in ("high", "critical")
    assert row["independent_sources"] >= 2
    labels = [k["detector"] for k in row["key_findings"]]
    assert "duplicate_exact" in labels


def test_contributors_are_the_real_signals_with_their_own_values(res):
    r, s = res
    d = detail(res, DUPLICATE)
    ex = d["explanation"]
    assert ex["score"] == d["transaction"]["score"] == d["breakdown"]["score"]
    for c in ex["all_contributors"]:
        sig = r.signals[c["signal_id"]]
        assert c["evidence"] == sig["evidence"] and c["features"] == sig["features"]
        assert c["explanation"] == sig["explanation"] and c["detector"] == sig["rule_id"]
        assert c["affected_transactions"] == sig["txn_ids"]
        assert c["signal_evidence"] == pytest.approx(sig["strength"] * sig["weight"])


def test_displayed_contributions_match_the_breakdown_and_sum_to_the_score(res):
    d = detail(res, DUPLICATE)
    ex, br = d["explanation"], d["breakdown"]
    by_signal = {sg["signal_id"]: sg["contribution"] for f in br["families"] for sg in f["signals"]}
    for c in ex["all_contributors"]:
        assert c["contribution"] == by_signal[c["signal_id"]]
    assert round(sum(c["contribution"] for c in ex["all_contributors"]) * 100) == round(ex["score"] * 100)
    assert round(sum(f["contribution"] for f in br["families"]) * 100) == round(ex["score"] * 100)


def test_top_contributors_are_the_largest(res):
    ex = detail(res, DUPLICATE)["explanation"]
    contribs = [c["contribution"] for c in ex["all_contributors"]]
    assert contribs == sorted(contribs, reverse=True)
    assert ex["contributors"] == ex["all_contributors"][: len(ex["contributors"])]
    assert ex["headline"].startswith(f"Review-risk score {ex['score']:.2f} of 100 ({ex['tier']}")


def test_explanations_state_limitations_and_never_claim_probability(res):
    r, _ = res
    ex = detail(res, DUPLICATE)["explanation"]
    text = " ".join(ex["limitations"])
    assert "not a probability" in text
    for u in r.summary["detectors_unavailable"]:
        assert u["detector"] in text
    blob = str(ex)
    for word in ("isFraud", "is_fraud", "ground truth", "scenario", "probability of fraud is", "AI detected"):
        assert word not in blob


def test_canonical_score_and_explanation_validate(res):
    r, _ = res
    f = r.fused[DUPLICATE]
    from ledgerspy.risk import explain

    out = explain.canonical(DUPLICATE, f, r.by_txn[DUPLICATE]["tier"], r.signals)
    score = Score.model_validate(out["score"])  # validates: shares sum to 1
    assert score.risk == pytest.approx(f.score / 100)
    expl = Explanation.model_validate(out["explanation"])
    assert len(expl.contributions) == sum(len(x.signals) for x in f.families if x.scored)


def test_changing_the_evidence_changes_the_explanation():
    rows = planted()[:-1]  # the duplicate is not booked
    r, s = result(rows)
    big = rows[-1]["txn_id"]
    d = service.transaction_detail(r, big, s)
    assert "duplicate_exact" not in [c["detector"] for c in d["explanation"]["all_contributors"]]
    assert DUPLICATE not in r.by_txn


def test_rows_without_signals_have_zero_risk_and_say_so(res):
    r, s = res
    quiet = next(t for t, row in r.by_txn.items() if row["score"] == 0)
    d = service.transaction_detail(r, quiet, s)
    assert d["transaction"]["tier"] == "low" and d["explanation"]["contributors"] == []
    assert "no detector raised a signal" in d["explanation"]["headline"]
    assert d["breakdown"]["families"] == []


def test_labels_and_extra_columns_cannot_change_the_result():
    rows = planted()
    base, _ = result(copy.deepcopy(rows))
    for i, x in enumerate(rows):
        x["extra"] = {"isFraud": str(i % 2), "label": "fraud", "scenario_id": "S-A-01"}
    again, _ = result(rows)
    assert again.result_sha256 == base.result_sha256


def test_row_order_does_not_change_the_result():
    rows = planted()
    base, _ = result(copy.deepcopy(rows))
    random.Random(4).shuffle(rows)  # noqa: S311 — test data
    again, _ = result(rows)
    assert {t: (x["score"], x["tier"], x["review_rank"]) for t, x in again.by_txn.items()} == {
        t: (x["score"], x["tier"], x["review_rank"]) for t, x in base.by_txn.items()
    }
    assert again.entities == base.entities


def test_entity_risk_aggregates_its_transactions_once(res):
    r, s = res
    ent = r.by_entity[r.by_txn[DUPLICATE]["entity_id"]]
    tops = ent["top_transactions"]
    assert tops[0]["txn_id"] in (DUPLICATE, f"T{int(DUPLICATE[1:]) - 1:05d}")
    expected = 1.0
    for j, t in enumerate(tops):
        expected *= 1 - t["score"] / 100 * s.risk.entities.decay**j
    assert ent["score"] == pytest.approx(round(100 * (1 - expected), 2), abs=0.02)
    assert len({t["txn_id"] for t in tops}) == len(tops) <= s.risk.entities.top_k
    assert round(sum(t["contribution"] for t in tops) * 100) == round(ent["score"] * 100)
    assert ent["exposure_amount_minor"] >= 500_000_00
    assert ent["transactions"] == 41 + 1  # vendor 03's payments incl. the planted pair


def test_insufficient_rows_score_nothing_and_are_counted():
    rules_off = {
        k: {"enabled": False}
        for k in (
            "duplicate_exact",
            "duplicate_near",
            "round_amount",
            "structuring",
            "temporal",
            "new_vendor_large",
            "balance_continuity",
            "dormancy",
            "period_end",
            "velocity",
        )
    }
    anomaly_off = {d: {"enabled": False} for d in ("isolation_forest", "robust_peer", "tail")}
    rows = [rec(n, at=float(n)) for n in range(1, 30)]
    r, s = result(rows, rules=rules_off, anomaly=anomaly_off)
    assert r.summary["counts"]["insufficient_data"] == 29 and r.summary["counts"]["scored"] == 0
    row = r.rows[0]
    assert row["score"] is None and row["status"] == "insufficient_data" and row["review_priority"] == "none"
    assert "Not assessed" in service.transaction_detail(r, row["txn_id"], s)["explanation"]["headline"]
    assert sum(b["count"] for b in r.summary["score_distribution"]) == 0


def test_summary_numbers_are_computed_from_the_rows(res):
    r, _ = res
    sm = r.summary
    tiers = sm["counts"]["tiers"]
    assert sum(tiers.values()) == sm["counts"]["scored"] == len(r.rows)
    high = [x for x in r.rows if x["tier"] in ("high", "critical")]
    assert sm["counts"]["high_or_critical"] == len(high)
    assert sm["amounts"]["high_or_critical_minor"] == sum(abs(x["amount_minor"]) for x in high)
    assert sm["amounts"]["total_minor"] == sum(abs(x["amount_minor"]) for x in r.rows)
    assert sum(b["count"] for b in sm["score_distribution"]) == sm["counts"]["scored"]
    assert sm["volume_guard"]["raw_tier_counts"] == tiers
    assert sm["top_findings"][0]["txn_id"] == DUPLICATE


def test_row_families_and_key_findings_are_ordered_by_contribution(res):
    r, _ = res
    for row in r.rows:
        fam = [x["contribution"] for x in row["families"]]
        kf = [x["contribution"] for x in row["key_findings"]]
        assert fam == sorted(fam, reverse=True) and kf == sorted(kf, reverse=True)
    led = {f["family"]: f["transactions_led"] for f in r.summary["method"]["families"]}
    assert sum(led.values()) == sum(1 for x in r.rows if x["families"])
