"""P7 rules on hand-crafted data: each rule fires on its pattern, stays quiet on look-alikes, and says
`not_available` / `not_applicable` / `insufficient` instead of guessing."""

import random
from datetime import datetime, timedelta

import pytest

from ledgerspy.canonical import Signal, SubjectType
from ledgerspy.detectors.rules import RULES, binomial_tail, poisson_tail
from ledgerspy.forensics.models import ForensicSignal
from tests.backend.features_support import ALL_FIELDS, T0, rec
from tests.backend.forensics_support import flagged, outcome, report, signals

LIMITS = [
    {"name": "expense_claim", "amount_minor": 5_000_00},
    {"name": "vendor_payment", "amount_minor": 2_00_000_00},
]


def at(day: float, hour: int = 11, minute: int = 0) -> datetime:
    return T0.replace(hour=hour, minute=minute) + timedelta(days=day)


def background(n: int = 120, start: int = 0, seed: int = 1, **kw) -> list[dict]:
    """Ordinary payments: several vendors in one account, daytime, non-round amounts, varied references."""
    r = random.Random(seed)
    out = []
    for i in range(n):
        day = (start + i * 2) % 360 + r.random() * 0.2
        out.append(
            rec(
                9000 + i,
                at=at(day, hour=r.randint(10, 16)),
                amount=r.randint(80_000, 140_000) * 100 + r.randint(1, 99),
                payee=f"Vendor {i % 6}",
                reference=f"BG/{9000 + i}",
                **kw,
            )
        )
    return out


# ---------------------------------------------------------------------------------------------- registry


def test_every_required_rule_exists_with_a_version_and_documented_inputs():
    expected = {
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
    }
    assert set(RULES) == expected
    for spec in RULES.values():
        assert spec.version >= 1 and len(spec.description) > 60 and spec.features
        assert spec.semantics in ("point_in_time", "post_period", "dataset")


# ----------------------------------------------------------------------------------------- A. exact duplicate


def test_exact_duplicate_flags_the_copy_with_the_group_as_evidence():
    rows = [
        rec(1, at=at(1), amount=50_000_00, reference="INV/0001"),
        rec(2, at=at(1, minute=5), amount=50_000_00, reference="inv-0001"),  # same day, same normalised ref
        rec(3, at=at(1), amount=50_000_00, reference="INV/0002"),  # another invoice: not a duplicate
        rec(4, at=at(2), amount=50_000_00, reference="INV/0001"),  # another day: near-duplicate territory
    ]
    rep = report(rows)
    (s,) = signals(rep, "duplicate_exact")
    assert s["subject_id"] == "T00002" and s["evidence"]["original"] == "T00001"
    assert s["evidence"]["group"] == ["T00001", "T00002"] and "reference" in s["evidence"]["matched"]
    assert s["family"] == "duplication" and s["severity"] == "high" and "twice" in s["explanation"]


def test_recurring_fixed_payments_are_not_exact_duplicates():
    rows = [rec(i, at=at(30 * i), amount=40_000_00, reference=f"RET/{i:03d}") for i in range(1, 13)]
    assert flagged(report(rows), "duplicate_exact") == []


def test_exact_duplicate_without_references_compares_the_exact_simulated_time():
    rows = [rec(1, at=at(0), amount=10_00, reference=None), rec(2, at=at(0), amount=10_00, reference=None)]
    rows.append(
        rec(3, at=at(0, hour=12), amount=10_00, reference=None)
    )  # same "date", another simulated step
    rep = report(rows, time_basis="simulation_step", mapped=ALL_FIELDS - {"reference"})
    assert flagged(rep, "duplicate_exact") == ["T00002"]


# ------------------------------------------------------------------------------------------ B. near duplicate


def test_near_duplicates_match_typo_style_variants_and_say_how():
    rows = [
        rec(1, at=at(0), amount=1_234_56, reference="GMP6/00195"),
        rec(2, at=at(1), amount=1_234_56, reference="GMP6/00195A"),  # letter suffix
        rec(3, at=at(5), amount=3_281_31_18, reference="GMP6/00412"),
        rec(4, at=at(6), amount=3_283_11_18, reference="GMP6/00421"),  # amount and reference transposed
        rec(5, at=at(10), amount=7_000_00, reference="GMP6/00500"),
        rec(6, at=at(10.2), amount=7_000_00, reference="GMP6/00501"),  # consecutive invoices: different bills
        rec(7, at=at(40), amount=1_234_56, reference="GMP6/00195"),  # same bill but 39 days later (window 14)
    ]
    rep = report(rows)
    assert flagged(rep, "duplicate_near") == ["T00002", "T00004"]
    by = {s["subject_id"]: s["evidence"]["earlier"][0] for s in signals(rep, "duplicate_near")}
    assert by["T00002"]["reference_match"] == "suffix_letter" and by["T00002"]["amount_match"] == "equal"
    assert by["T00004"]["reference_match"] == "transposed" and by["T00004"]["amount_match"] == "transposed"


def test_the_near_duplicate_window_and_tolerance_come_from_configuration():
    rows = [
        rec(1, at=at(0), amount=1_000_00, reference="A/1"),
        rec(2, at=at(20), amount=1_000_50, reference="A/1"),
    ]
    assert flagged(report(rows), "duplicate_near") == []
    wide = report(rows, rules={"duplicate_near": {"window_days": 30, "amount_relative_tolerance": 0.001}})
    assert flagged(wide, "duplicate_near") == ["T00002"]


# -------------------------------------------------------------------------------------------- C. round amount


def test_round_amounts_are_judged_against_the_population_and_need_variety():
    rows = background(120)
    fabricated = [49_000, 75_000, 1_00_000, 50_000, 1_25_000, 60_000, 90_000, 80_000]
    rows += [
        rec(100 + i, at=at(5 + 9 * i), amount=a * 100, payee="Round Co") for i, a in enumerate(fabricated)
    ]
    rows += [rec(200 + i, at=at(3 + 30 * i), amount=40_000_00, payee="Retainer LLP") for i in range(10)]
    rows += [rec(300 + i, at=at(4 + 30 * i), amount=5_000_00, payee="Few Rows") for i in range(4)]
    rep = report(rows)
    (s,) = signals(rep, "round_amount")
    assert s["subject_id"] == "out:round co" and s["subject_type"] == "entity"
    ev = s["evidence"]
    assert ev["round_rows"] == ev["rows"] == 8 and ev["distinct_round_amounts"] == 8
    assert ev["population_round_rate"] < 0.2 and ev["binomial_p_value"] < 0.001
    o = outcome(rep, "round_amount")
    assert o["insufficient"] >= 1  # "Few Rows" (4 < min_rows 6) is not judged; the retainer has one amount


# --------------------------------------------------------------------------------------------- D. structuring


def test_structuring_needs_parts_just_below_one_threshold_that_together_exceed_it():
    rows = [
        rec(i, at=at(0, 11 + i), amount=a, payee="Asha Rao")
        for i, a in enumerate([4_900_00, 4_950_00, 4_980_00], 1)
    ]
    rows += [rec(10, at=at(0), amount=4_950_00, payee="Single Claim")]
    rows += [
        rec(20, at=at(0), amount=4_900_00, payee="Far Apart"),
        rec(21, at=at(40), amount=4_900_00, payee="Far Apart"),
    ]
    rows += [rec(30 + i, at=at(1, 12 + i), amount=4_000_00, payee="Eighty Percent") for i in range(3)]
    rep = report(rows, features={"approval_thresholds": LIMITS})
    assert flagged(rep, "structuring") == ["T00001", "T00002", "T00003"]
    ev = signals(rep, "structuring")[0]["evidence"]
    assert ev["threshold"] == "expense_claim" and ev["aggregate_minor"] == 14_830_00
    assert [p["txn_id"] for p in ev["parts"]] == ["T00001", "T00002", "T00003"]


def test_structuring_is_unavailable_without_configured_thresholds():
    o = outcome(report([rec(1)]), "structuring")
    assert o["status"] == "not_available" and "approval thresholds" in o["reason"]


# ---------------------------------------------------------------------------------------------- E. temporal


def test_off_hours_postings_are_flagged_unless_they_are_an_established_routine():
    weekdays = [d for d in range(1, 26) if at(d).weekday() != 6][:19]  # Monday–Saturday, office hours
    rows = [rec(i, at=at(d), payee="Day Vendor") for i, d in enumerate(weekdays, 1)]
    rows.append(rec(50, at=at(21, hour=23, minute=40), payee="Day Vendor"))  # one-off night posting
    routine = [at(31 * m + 29, hour=20) for m in range(4)]  # month-end overtime, every month
    rows += [rec(60 + k, at=t, payee="Advisers") for k, t in enumerate(routine)]
    rows.append(rec(70, at=datetime(2024, 1, 7, 11), payee="Day Vendor"))  # Sunday
    rep = report(rows)
    assert set(flagged(rep, "temporal")) == {"T00050", "T00060", "T00061", "T00070"}  # 3rd+ month: routine
    by = {s["subject_id"]: s for s in signals(rep, "temporal")}
    assert by["T00050"]["evidence"]["conditions"] == ["after business hours"]
    assert by["T00070"]["evidence"]["conditions"] == ["on a non-working weekday"]


def test_temporal_rule_is_unavailable_without_a_real_time_of_day():
    o = outcome(report([rec(1)], time_basis="date"), "temporal")
    assert o["status"] == "not_available" and "time of day" in o["reason"]


# ---------------------------------------------------------------------------------------------- F. new vendor


def test_new_vendor_with_a_large_amount_against_its_peers():
    rows = background(120)
    rows.append(rec(500, at=at(150), amount=9_00_000_00, payee="Brand New Ltd"))  # ~8x the account's level
    rows.append(rec(501, at=at(151), amount=1_10_000_00, payee="Ordinary New Ltd"))
    rows.append(rec(502, at=at(10), amount=9_00_000_00, payee="Too Early Ltd"))  # first 90 days of the data
    rows.append(
        rec(
            503,
            at=at(152),
            amount=9_00_000_00,
            payee="Acme Pvt Ltd",
            payer="Big Customer",
            direction="credit",
        )
    )  # money in: not a vendor payment
    rep = report(rows)
    (s,) = signals(rep, "new_vendor_large")
    assert s["subject_id"] == "T00500" and s["features"]["amount_robust_z__basis"] == "account"
    assert s["features"]["cp_prior_count"] == 0 and s["features"]["amount_robust_z"] >= 3
    assert outcome(rep, "new_vendor_large")["insufficient"] >= 1  # the too-early row


# ---------------------------------------------------------------------------------------------- G. balance


BAL_OK = {"balance_continuity": {"status": "issues", "reason": ""}}


def test_balance_discontinuities_come_from_p4_and_known_artefacts_are_ignored():
    details = {
        "balance_continuity": {
            "T00002": [
                {
                    "observed": {"kind": "other_mismatch", "before": 100, "after": 50},
                    "message": "jump",
                    "severity": "warning",
                }
            ],
            "T00003": [{"observed": {"kind": "balance_zeroed"}, "message": "zeroed", "severity": "info"}],
        }
    }
    p4 = {"T00002": ["balance_continuity"], "T00003": ["balance_continuity"]}
    rep = report(
        [rec(1, at=at(0)), rec(2, at=at(1)), rec(3, at=at(2))], p4=p4, p4_checks=BAL_OK, details=details
    )
    (s,) = signals(rep, "balance_continuity")
    assert (
        s["subject_id"] == "T00002" and s["evidence"]["p4_issues"][0]["observed"]["kind"] == "other_mismatch"
    )


def test_balance_rule_is_not_applicable_without_a_running_balance_and_unavailable_without_p4():
    na = {
        "balance_continuity": {"status": "not_available", "reason": "no running-balance column in the source"}
    }
    o = outcome(report([rec(1)], p4={}, p4_checks=na), "balance_continuity")
    assert o["status"] == "not_applicable" and "no running-balance column" in o["reason"]
    o = outcome(report([rec(1)]), "balance_continuity")
    assert o["status"] == "not_available" and "P4" in o["reason"]


# ---------------------------------------------------------------------------------------------- H. dormancy


def test_dormant_counterparty_resuming_at_an_unusual_amount():
    rows = [rec(i, at=at(30 * i), amount=10_000_00 + i * 100, payee="Sleeper") for i in range(6)]
    rows.append(rec(20, at=at(150 + 220), amount=31_000_00, payee="Sleeper"))  # 220 days later, ~3x
    rows += [rec(30 + i, at=at(30 * i), amount=10_000_00 + i, payee="Quiet Normal") for i in range(6)]
    rows.append(
        rec(40, at=at(150 + 220), amount=10_100_00, payee="Quiet Normal")
    )  # long gap, ordinary amount
    rows += [rec(50 + i, at=at(30 * i), amount=10_000_00, payee="Short Gap") for i in range(6)]
    rows.append(rec(60, at=at(150 + 100), amount=40_000_00, payee="Short Gap"))  # large but only 100 days
    rep = report(rows)
    (s,) = signals(rep, "dormancy")
    assert s["subject_id"] == "T00020" and s["evidence"]["dormant_days"] == 220


# ---------------------------------------------------------------------------------------------- I. period end


def test_period_end_cluster_must_be_dense_and_unusual():
    rows = [rec(i, at=at(2 + 7 * i), amount=10_000_00 + i * 37, payee="Supplier") for i in range(40)]
    fy_end = datetime(2025, 3, 31, 21, 30)
    rows += [
        rec(100 + k, at=fy_end + timedelta(minutes=5 * k), amount=40_000_00 + k, payee="Supplier")
        for k in range(4)
    ]
    routine = [datetime(y, 3, 31, 19) for y in (2024, 2025)]  # one posting per year end: not a cluster
    rows += [rec(200 + i, at=at(2 + 7 * i), amount=5_000_00 + i * 11, payee="Routine") for i in range(40)]
    rows += [rec(300 + k, at=t, amount=5_000_00, payee="Routine") for k, t in enumerate(routine)]
    rows += [rec(400 + i, at=at(2 + 7 * i), amount=8_000_00 + i, payee="Busy Normal") for i in range(40)]
    rows += [
        rec(500 + k, at=fy_end + timedelta(minutes=k), amount=8_000_00 + k, payee="Busy Normal")
        for k in range(4)
    ]
    rep = report(rows)
    (s,) = signals(rep, "period_end")
    assert s["subject_type"] == "group" and s["subject_id"] == "out:supplier@2025-03-31"
    assert s["txn_ids"] == ["T00100", "T00101", "T00102", "T00103"] and s["evidence"]["rows_in_window"] == 4


# ---------------------------------------------------------------------------------------------- J. velocity


def test_velocity_spike_against_the_counterparty_s_own_pace():
    rows = [rec(i, at=at(30 * i), payee="Monthly") for i in range(8)]
    rows += [rec(100 + k, at=at(250 + k), payee="Monthly") for k in range(8)]  # 8 in 8 days after ~1 a month
    rows += [rec(200 + i, at=at(15 * i), payee="Fortnightly") for i in range(20)]  # steady, never a burst
    rows += [rec(300 + k, at=at(200 + k), payee="Brand New") for k in range(8)]  # burst but no history
    rep = report(rows)
    hits = flagged(rep, "velocity")
    assert hits and set(hits) <= {f"T{100 + k:05d}" for k in range(8)}
    assert all(s["evidence"]["poisson_p_value"] <= 0.0001 for s in signals(rep, "velocity"))
    assert outcome(rep, "velocity")["insufficient"] >= 8  # Brand New has no baseline: not judged


def test_statistical_helpers():
    assert binomial_tail(0, 5, 0.3) == 1.0 and binomial_tail(5, 5, 0.5) == pytest.approx(1 / 32)
    assert binomial_tail(3, 4, 0.5) == pytest.approx(5 / 16)
    assert poisson_tail(0, 2.0) == 1.0 and poisson_tail(1, 2.0) == pytest.approx(1 - 0.1353352832366127)
    assert poisson_tail(3, 1.0) == pytest.approx(1 - (1 + 1 + 0.5) * 0.36787944117144233)


# -------------------------------------------------------------------------------------------- signal contract


def test_signals_are_complete_deterministic_and_convert_to_the_canonical_model():
    rows = [
        rec(1, at=at(1), amount=50_000_00, reference="X/1"),
        rec(2, at=at(1), amount=50_000_00, reference="X/1"),
    ]
    a, b = report(rows), report(rows)
    assert a["report_sha256"] == b["report_sha256"] and a["signals"] == b["signals"]
    s = a["signals"][0]
    for key in (
        "signal_id",
        "rule_id",
        "rule_version",
        "family",
        "subject_type",
        "subject_id",
        "txn_ids",
        "triggered",
        "severity",
        "strength",
        "weight",
        "evidence",
        "features",
        "explanation",
    ):
        assert key in s, key
    sig = ForensicSignal(
        rule_id=s["rule_id"],
        rule_version=s["rule_version"],
        family=RULES["duplicate_exact"].family,
        subject_type=s["subject_type"],
        subject_id=s["subject_id"],
        txn_ids=tuple(s["txn_ids"]),
        severity=s["severity"],
        strength=s["strength"],
        weight=s["weight"],
        evidence=s["evidence"],
        features=s["features"],
        explanation=s["explanation"],
    )
    assert sig.signal_id == s["signal_id"]
    canonical = sig.to_canonical()
    assert isinstance(canonical, Signal) and canonical.subject == SubjectType.TXN
    assert (
        canonical.code == "duplication.duplicate_exact" and canonical.template_id == "p7.duplicate_exact.v1"
    )


def test_disabled_rules_say_so():
    o = outcome(report([rec(1)], rules={"velocity": {"enabled": False}}), "velocity")
    assert o["status"] == "disabled" and o["triggered"] == 0


def test_shuffled_input_gives_the_identical_report():
    from tests.backend.test_features_pit import random_records

    records = random_records(21, n=500)
    records += [rec(900 + k, at=at(150, 11 + k), amount=4_950_00, payee="V1") for k in range(3)]  # a split
    shuffled = records[:]
    random.Random(4).shuffle(shuffled)
    kw = {"features": {"approval_thresholds": LIMITS}, "mapped": ALL_FIELDS - {"txn_type"}}
    a, b = report(records, **kw), report(shuffled, **kw)
    assert a["signals"] and a["report_sha256"] == b["report_sha256"]
