"""P7 Benford analysis: conformity, deviation, gating, exclusions, determinism, subgroups."""

import copy
import math
import random

import pytest

from ledgerspy.config import load_config
from ledgerspy.forensics import benford
from tests.backend.features_support import rec
from tests.backend.forensics_support import report

CFG = load_config().settings.benford


def benford_sample(n: int, decades: int = 5) -> list[int]:
    """Deterministic Benford-distributed amounts: mantissas evenly spaced on a log scale (≥ ₹10)."""
    return [round(10 ** (3 + decades * (k + 0.5) / n)) for k in range(n)]


def uniform_sample(n: int, low: int = 10_00, high: int = 10_000_00) -> list[int]:
    step = (high - low) / n
    return [round(low + step * (k + 0.5)) for k in range(n)]


# ---------------------------------------------------------------------------------------------- conformity


@pytest.mark.parametrize("test", ["first", "first_two"])
def test_benford_distributed_data_conforms(test):
    r = benford.analyze(benford_sample(20_000), test, CFG)
    assert r["status"] == "analyzed" and r["band"] == "close"
    assert r["mad"] <= (0.006 if test == "first" else 0.0012)
    assert r["over_represented"] == [] and r["chi2_p_value"] > 0.05


def test_uniform_data_does_not_conform():
    r = benford.analyze(uniform_sample(5_000), "first", CFG)
    assert r["status"] == "analyzed" and r["band"] == "nonconformity"
    assert r["observed"]["1"] < r["expected"]["1"] and {7, 8, 9} <= set(r["over_represented"])


def test_deliberately_non_benford_data_is_named_digit_by_digit():
    amounts = [r for r in benford_sample(4_000) if str(r)[0] in "56789"] * 3
    r = benford.analyze(amounts, "first", CFG)
    assert r["band"] == "nonconformity" and set(r["over_represented"]) == {5, 6, 7, 8, 9}
    assert set(r["under_represented"]) == {1, 2, 3, 4} and r["chi2_p_value"] < 1e-6
    assert "not evidence of fraud" in r["note"]


def test_random_looking_amounts_with_a_narrow_range_are_analysed_with_a_warning():
    rng = random.Random(3)
    r = benford.analyze([rng.randint(7_000_00, 9_999_99) for _ in range(400)], "first", CFG)
    assert r["status"] == "analyzed" and r["band"] == "nonconformity"
    assert any("narrow range" in w for w in r["warnings"]) and r["range_ratio"] < 2


def test_last_two_digits():
    even = benford.analyze(
        list(range(100_00, 100_00 + 2_000 * 100, 100)), "last_two", CFG
    )  # every 00..99 equally
    assert even["status"] == "analyzed" and even["mad"] == 0 and even["band"] is None
    rounded = benford.analyze([k * 1_000_00 for k in range(1, 1_201)], "last_two", CFG)  # all end in 00
    assert rounded["over_represented"] == [0] and rounded["counts"]["0"] == 1_200


# -------------------------------------------------------------------------------------- gating and exclusions


def test_below_the_minimum_sample_is_insufficient_data():
    r = benford.analyze(benford_sample(299), "first", CFG)
    assert r["status"] == "insufficient_data" and r["n"] == 299 and "mad" not in r
    assert benford.analyze(benford_sample(999), "first_two", CFG)["status"] == "insufficient_data"


def test_zeros_negatives_missing_and_small_values_are_excluded_and_counted():
    amounts = benford_sample(400) + [0, 0, None, -5_000_00, 9_99]
    r = benford.analyze(amounts, "first", CFG)
    assert r["excluded"] == {"below_minimum": 1, "missing": 1, "negative": 1, "zero": 2} and r["n"] == 400
    absolute = CFG.model_copy(update={"negative_amounts": "absolute"})
    assert benford.analyze(amounts, "first", absolute)["n"] == 401
    assert benford.digit_of(-5_000_00, "first", absolute) == (5, None)
    assert benford.digit_of(99_99, "last_two", CFG) == (None, "below_minimum")  # ₹99.99: whole part < 100
    assert benford.digit_of(123_45, "last_two", CFG) == (23, None)  # ₹123.45 → whole 123 → 23
    assert benford.digit_of(12_345_67, "last_two", CFG) == (45, None)  # ₹12,345.67 → whole 12,345 → 45


def test_degenerate_or_absent_populations():
    assert benford.analyze([5_000_00] * 500, "first", CFG)["status"] == "invalid_population"
    assert benford.analyze([], "first", CFG)["status"] == "not_applicable"
    assert benford.analyze([None] * 10, "first", CFG)["status"] == "not_applicable"


def test_hand_computed_example():
    small = CFG.model_copy(
        update={"min_n": {"first": 4, "first_two": 4, "last_two": 4}, "min_distinct_values": 2}
    )
    amounts = [1_000_00, 1_500_00, 2_000_00, 3_000_00]  # first digits 1, 1, 2, 3
    r = benford.analyze(amounts, "first", small)
    exp = {d: math.log10(1 + 1 / d) for d in range(1, 10)}
    obs = {1: 0.5, 2: 0.25, 3: 0.25} | {d: 0.0 for d in range(4, 10)}
    assert r["mad"] == pytest.approx(sum(abs(obs[d] - exp[d]) for d in exp) / 9, abs=1e-6)
    chi2 = sum((4 * obs[d] - 4 * exp[d]) ** 2 / (4 * exp[d]) for d in exp)
    assert r["chi2"] == pytest.approx(chi2, abs=1e-4) and r["dof"] == 8
    z1 = (abs(0.5 - exp[1]) - 1 / 8) / math.sqrt(exp[1] * (1 - exp[1]) / 4)
    assert r["z"]["1"] == pytest.approx(max(z1, 0), abs=1e-4)


def test_chi_square_p_value():
    assert benford.gamma_q(4.0, 4.0) == pytest.approx(0.43347, abs=1e-5)  # χ²=8, 8 dof
    assert benford.gamma_q(44.0, 44.0) == pytest.approx(0.48, abs=0.01)  # χ²≈dof → p≈0.5
    assert benford.gamma_q(4.0, 50.0) < 1e-15


def test_deterministic_and_never_mutates_its_input():
    amounts = benford_sample(1_500) + [None, 0, -1]
    before = copy.deepcopy(amounts)
    assert benford.analyze(amounts, "first_two", CFG) == benford.analyze(amounts, "first_two", CFG)
    assert amounts == before


# ---------------------------------------------------------------------------------------------- subgroups


def _ledger(groups: dict[str, list[int]]) -> list[dict]:
    rows, n = [], 0
    for payee, amounts in groups.items():
        for a in amounts:
            n += 1
            rows.append(rec(n, at=n * 0.01, amount=a, payee=payee, account=payee[:1]))
    return rows


def test_subgroups_are_gated_by_minimum_n_and_counted():
    rng = random.Random(1)
    groups = {
        "Benford Co": benford_sample(320),
        "Seven Nine Co": [rng.randint(7_000_00, 9_999_99) for _ in range(320)],
        "Tiny Co": benford_sample(40),
    }
    rep = report(_ledger(groups))
    cp = rep["benford"]["subgroups"]["counterparty"]["tests"]["first"]
    assert (cp["groups_total"], cp["groups_analyzed"], cp["groups_skipped_small"]) == (3, 2, 1)
    assert cp["rows_in_skipped_groups"] == 40 and cp["min_n"] == 300
    results = {g["group"]: g for g in cp["results"]}
    assert results["out:seven nine co"]["band"] == "nonconformity"
    assert results["out:benford co"]["band"] in ("close", "acceptable")
    sig = [s for s in rep["signals"] if s["subject_id"] == "benford:first:counterparty=out:seven nine co"]
    assert len(sig) == 1 and sig[0]["family"] == "digit" and sig[0]["severity"] == "low"
    assert (
        set(sig[0]["evidence"]["over_represented"]) <= {7, 8, 9}
        and "not evidence of fraud" in sig[0]["explanation"]
    )
    assert len(sig[0]["txn_ids"]) == CFG.subgroups.max_example_rows


def test_subgroup_dimensions_the_data_cannot_support_are_not_applicable():
    rep = report(_ledger({"A": benford_sample(50)}), adapter="paysim")
    sub = rep["benford"]["subgroups"]
    assert sub["account"]["status"] == "not_applicable"  # PaySim's account is the sender
    assert sub["txn_type"]["status"] == "analyzed"  # mapped (all null here → no groups)
    assert sub["txn_type"]["tests"]["first"]["groups_total"] == 0
