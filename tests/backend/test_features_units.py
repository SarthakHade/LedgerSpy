"""P6: one unit test per registered feature (test_<feature name>), with hand-computed values.

`test_features_registry.py` checks that every implemented feature has its test here.
"""

import math
from datetime import date, datetime, timedelta
from statistics import median

import pytest

from tests.backend.features_support import make, rec, values


def at(day: float, hour: int = 10, minute: int = 0) -> datetime:
    return datetime(2024, 1, 1, hour, minute) + timedelta(days=day)


# ---------------------------------------------------------------------------------------------- amount


def test_amount_minor():
    inp = make([rec(1, amount=12345), rec(2, amount=None)])
    assert values(inp, "amount_minor") == [12345, None]  # null stays null, never 0


def test_log_amount():
    inp = make([rec(1, amount=100), rec(2, amount=-100), rec(3, amount=None), rec(4, amount=0)])
    assert values(inp, "log_amount") == [math.log(2), math.log(2), None, 0.0]


def test_round_level():
    amounts = [1_00_000_00, 2_500_00, 2_550_50, 30_00, 10_00_000_00, 7_00, 0, None]
    inp = make([rec(i, amount=a) for i, a in enumerate(amounts, 1)])
    assert values(inp, "round_level") == [5, 2, 0, 1, 5, 0, None, None]


def test_is_outflow():
    inp = make([rec(i, direction=d) for i, d in enumerate(["debit", "credit", "out", "in", None], 1)])
    assert values(inp, "is_outflow") == [True, False, True, False, None]


def test_txn_type():
    inp = make([rec(1, txn_type="TRANSFER"), rec(2, txn_type=None)], adapter="paysim")
    assert values(inp, "txn_type") == ["TRANSFER", None]


LIMITS = [{"name": "claim", "amount_minor": 5_000_00}, {"name": "cfo", "amount_minor": 2_00_000_00}]


def test_threshold_proximity():
    inp = make(
        [rec(1, amount=4_850_00), rec(2, amount=5_000_00), rec(3, amount=3_00_000_00), rec(4, amount=None)],
        approval_thresholds=LIMITS,
    )
    assert values(inp, "threshold_proximity") == [0.97, 0.025, None, None]
    assert (
        make([rec(1)]).available(("approval_thresholds",))
        == "no approval thresholds are configured for this case"
    )
    paysim = make([rec(1)], adapter="paysim", approval_thresholds=LIMITS)
    assert "not a corporate ledger" in paysim.available(("approval_thresholds",))


def _robust_z(x: int, prior: list[int], floor: float = 0.1) -> float:
    logs = [math.log1p(a / 100) for a in prior]
    m = median(logs)
    scale = max(1.4826 * median([abs(v - m) for v in logs]), floor)
    return (math.log1p(x / 100) - m) / scale


def test_amount_robust_z():
    amounts = [100_00, 110_00, 90_00, 105_00, 95_00, 1000_00]
    inp = make([rec(i, at=i, amount=a) for i, a in enumerate(amounts, 1)], min_entity_history=5)
    z = values(inp, "amount_robust_z")
    assert z[:5] == [None] * 5  # global level needs min_peer_history (20) and the entity 5 prior rows
    assert z[5] == pytest.approx(_robust_z(1000_00, amounts[:5]))


def test_amount_pctl_prior():
    inp = make([rec(i, at=i, amount=a) for i, a in enumerate([100, 200, 300, 200], 1)], min_peer_history=3)
    assert values(inp, "amount_pctl_prior") == [None, None, None, pytest.approx(0.5)]  # (1 + 0.5) / 3


def test_amount_to_cp_median_ratio():
    rows = [rec(i, at=i, amount=a) for i, a in enumerate([100, 200, 300, 400], 1)]
    rows.append(rec(5, at=5, amount=50, payee="Vendor B"))  # another counterparty: no own history
    inp = make(rows, min_entity_history=3)
    assert values(inp, "amount_to_cp_median_ratio") == [None, None, None, 2.0, None]


def test_amount_to_account_median_ratio():
    rows = [rec(i, at=i, amount=a, payee=f"V{i}") for i, a in enumerate([100, 200, 300], 1)]
    rows += [rec(4, at=4, amount=600, payee="V9"), rec(5, at=5, amount=600, account="9999")]
    inp = make(rows, min_peer_history=3)
    assert values(inp, "amount_to_account_median_ratio") == [None, None, None, 3.0, None]
    paysim = make(rows, adapter="paysim")
    assert (
        paysim.available(("account_category",)) is not None
    )  # PaySim's account is the sender, not a category


def test_cp_prev_amount_ratio():
    inp = make(
        [rec(1, at=0, amount=100), rec(2, at=1, amount=300), rec(3, at=2, amount=0), rec(4, at=3, amount=5)]
    )
    assert values(inp, "cp_prev_amount_ratio") == [None, 3.0, 0.0, None]  # a 0 previous amount → null


# ---------------------------------------------------------------------------------------------- timing


def test_hour_of_day():
    inp = make([rec(1, at=datetime(2024, 1, 2, 10, 30, 36)), rec(2, at=None)])
    assert values(inp, "hour_of_day") == [pytest.approx(10.51), None]
    assert make([rec(1)], time_basis="simulation_step").available(("time_of_day",)) is not None
    assert make([rec(1)], time_basis="date").available(("time_of_day",)) is not None


def test_day_of_week():
    inp = make([rec(1, at=datetime(2024, 1, 1)), rec(2, at=datetime(2024, 1, 7)), rec(3, at=None)])
    assert values(inp, "day_of_week") == [0, 6, None]


def test_is_non_working_weekday():
    inp = make([rec(1, at=datetime(2024, 1, 7)), rec(2, at=datetime(2024, 1, 6)), rec(3, at=None)])
    assert values(inp, "is_non_working_weekday") == [True, False, None]  # Sunday off, Saturday working


def test_is_holiday():
    rows = [rec(1, at=datetime(2024, 1, 26, 11)), rec(2, at=datetime(2024, 1, 25, 11)), rec(3, at=None)]
    assert values(make(rows, calendar={"holidays": ["2024-01-26"]}), "is_holiday") == [True, False, None]
    assert make(rows).available(("holidays",)) == "no holiday calendar is configured for this case"


def test_is_after_hours():
    times = [(9, 29), (9, 30), (18, 30), (18, 31), (23, 0)]
    inp = make([rec(i, at=datetime(2024, 1, 2, h, m)) for i, (h, m) in enumerate(times, 1)])
    assert values(inp, "is_after_hours") == [True, False, False, True, True]


def test_days_to_month_end():
    inp = make([rec(1, at=datetime(2024, 1, 31)), rec(2, at=datetime(2024, 2, 28)), rec(3, at=None)])
    assert values(inp, "days_to_month_end") == [0, 1, None]  # 2024 is a leap year


def test_days_to_fiscal_year_end():
    days = [datetime(2024, 3, 31), datetime(2024, 4, 1), datetime(2024, 12, 31)]
    inp = make([rec(i, at=d) for i, d in enumerate(days, 1)])
    assert values(inp, "days_to_fiscal_year_end") == [0, 364, 90]
    assert values(
        make([rec(1, at=datetime(2024, 12, 31))], calendar={"fiscal_year_start_month": 1}),
        "days_to_fiscal_year_end",
    ) == [0]


def test_posting_lag_days():
    inp = make(
        [rec(1, at=datetime(2024, 1, 10), document_date=date(2024, 1, 1)), rec(2, at=datetime(2024, 1, 10))]
    )
    assert values(inp, "posting_lag_days") == [9, None]
    assert make([rec(1)], mapped={"posted_at", "amount_minor"}).available(("document_date",)) is not None


def test_hour_deviation():
    rows = [rec(i, at=at(i, hour=h)) for i, h in enumerate([10, 11, 12], 1)] + [rec(4, at=at(4, hour=23))]
    assert values(make(rows, min_entity_history=3), "hour_deviation") == [None, None, None, 12.0]


# ---------------------------------------------------------------------------------------------- frequency


def test_cp_prior_count():
    rows = [
        rec(1, at=0),
        rec(2, at=1),
        rec(3, at=1),
        rec(4, at=2),
        rec(5, at=None),
        rec(6, at=3, payee="New"),
    ]
    assert values(make(rows), "cp_prior_count") == [0, 1, 1, 3, None, 0]  # ties never see each other


def test_cp_days_since_first():
    rows = [rec(1, at=0), rec(2, at=2.5), rec(3, at=10)]
    assert values(make(rows), "cp_days_since_first") == [None, 2.5, 10.0]


def test_cp_days_since_prev():
    rows = [rec(1, at=0), rec(2, at=2.5), rec(3, at=10)]
    assert values(make(rows), "cp_days_since_prev") == [None, 2.5, 7.5]


def test_cp_count_7d():
    rows = [rec(i, at=d) for i, d in enumerate([0, 3, 6, 10], 1)]
    assert values(make(rows), "cp_count_7d") == [0, 1, 2, 2]  # [t − 7d, t): day 3 counts at t = 10


def test_cp_count_30d():
    rows = [rec(i, at=d) for i, d in enumerate([0, 10, 29, 31, 60], 1)]
    assert values(make(rows), "cp_count_30d") == [0, 1, 2, 2, 1]  # at day 31: [1, 31) holds days 10 and 29


def test_cp_amount_sum_30d():
    rows = [
        rec(1, at=0, amount=100),
        rec(2, at=10, amount=None),
        rec(3, at=20, amount=50),
        rec(4, at=35, amount=1),
    ]
    assert values(make(rows), "cp_amount_sum_30d") == [0, 100, 100, 50]


def test_cp_velocity_ratio():
    days = [0, 30, 60, 90, 120, 140, 142, 144, 145]
    inp = make(
        [rec(i, at=d) for i, d in enumerate(days, 1)], min_entity_history=3, velocity_min_history_days=90
    )
    v = values(inp, "cp_velocity_ratio")
    assert v[-1] == pytest.approx((4 / 30) / (4 / 115))  # 4 rows in [115, 145); 4 rows before, over 115 days
    assert v[:4] == [None] * 4  # fewer than 3 rows before t − 30d, or under 90 days of history


def test_cp_count_30d_peer_ratio():
    rows = [rec(i, at=i, payee="A") for i in range(1, 5)]
    rows += [rec(5, at=2.5, payee="B"), rec(6, at=3.5, payee="C"), rec(7, at=6, payee="A")]
    v = values(make(rows, min_peers=2), "cp_count_30d_peer_ratio")
    assert v[-1] == 4.0  # A: 4 rows in the window; B and C: 1 each → 4 / median(1, 1)
    assert v[0] is None  # no other active counterparty yet


def test_cp_log_amount_trend():
    amounts, days = [1000_00, 2000_00, 4000_00], [0, 30, 60]
    rows = [rec(i, at=d, amount=a) for i, (d, a) in enumerate(zip(days, amounts, strict=True), 1)]
    rows.append(rec(4, at=61, amount=1))
    v = values(make(rows, trend_min_points=3), "cp_log_amount_trend")
    ys = [math.log1p(a / 100) for a in amounts]
    assert v[3] == pytest.approx(30 * (ys[2] - ys[0]) / 60)  # OLS slope of 3 evenly spaced points
    assert v[:3] == [None, None, None]


def test_cp_reference_gap():
    refs = ["INV/0005", "INV/0006", "INV/0010A", "IC/2024/X", "INV/0011", None]
    rows = [rec(i, at=i, reference=r) for i, r in enumerate(refs, 1)]
    assert values(make(rows), "cp_reference_gap") == [None, 1, 4, None, 1, None]


# ---------------------------------------------------------------------------------------------- relationship


def test_pair_prior_count():
    rows = [rec(1, at=0), rec(2, at=1), rec(3, at=2, payer="Other Co"), rec(4, at=3, payer=None)]
    assert values(make(rows), "pair_prior_count") == [0, 1, 0, None]


def test_is_reciprocal_pair():
    rows = [
        rec(1, at=0, payer="A", payee="B"),
        rec(2, at=1, payer="B", payee="A", direction="credit"),
        rec(3, at=2, payer="C", payee="D"),
        rec(4, at=2, payer="D", payee="C"),  # same instant: not "earlier"
    ]
    assert values(make(rows), "is_reciprocal_pair") == [False, True, False, False]


def test_payee_distinct_payers_prior():
    rows = [
        rec(1, at=0, payer="X", payee="Z"),
        rec(2, at=1, payer="Y", payee="Z"),
        rec(3, at=2, payer="X", payee="Z"),
    ]
    assert values(make(rows), "payee_distinct_payers_prior") == [0, 1, 2]


def test_payer_distinct_payees_prior():
    rows = [
        rec(1, at=0, payee="A"),
        rec(2, at=1, payee="B"),
        rec(3, at=2, payee="A"),
        rec(4, at=3, payee="C"),
    ]
    assert values(make(rows), "payer_distinct_payees_prior") == [0, 1, 2, 2]


def test_payer_new_payees_7d():
    rows = [
        rec(i, at=d, payee=p)
        for i, (d, p) in enumerate([(0, "A"), (2, "B"), (3, "A"), (5, "D"), (20, "C")], 1)
    ]
    assert values(make(rows), "payer_new_payees_7d") == [
        0,
        1,
        2,
        2,
        0,
    ]  # at day 20 nobody is new since day 13


def test_pair_share_of_payer_outflow():
    rows = [rec(1, at=0, payee="A", amount=100), rec(2, at=1, payee="B", amount=300), rec(3, at=2, payee="A")]
    assert values(make(rows), "pair_share_of_payer_outflow") == [None, 0.0, 0.25]


# ---------------------------------------------------------------------------------------------- data quality

P4 = {"T00001": ["exact_duplicate", "duplicate_source_id"], "T00002": ["missing_optional"]}


def test_dq_issue_count():
    rows = [rec(1), rec(2), rec(3)]
    assert values(make(rows, p4=P4), "dq_issue_count") == [2, 1, 0]
    assert make(rows).available(("p4_current",)) == "no P4 integrity run exists for the current data version"


def test_dq_has_duplicate_issue():
    assert values(make([rec(1), rec(2), rec(3)], p4=P4), "dq_has_duplicate_issue") == [True, False, False]


def test_dq_has_missing_field():
    assert values(make([rec(1), rec(2), rec(3)], p4=P4), "dq_has_missing_field") == [False, True, False]


def test_dq_is_normalized():
    assert values(make([rec(1, normalized=True), rec(2)]), "dq_is_normalized") == [True, False]


# ---------------------------------------------------------------------------------------------- entity level

# Streams in subject order: "in:customer c" then "out:vendor a", "out:vendor b".
ENTITY_ROWS = [
    rec(1, at=at(0), amount=1000_00, payee="Vendor A", reference="A/0001"),
    rec(2, at=at(10, hour=20), amount=2000_00, payee="Vendor A", reference="A/0003"),
    rec(3, at=at(6), amount=4000_00, payee="Vendor A", reference="A/0002"),  # Sunday 7 Jan
    rec(4, at=at(100), amount=3000_00, payee="Vendor A", reference="A/0007"),
    rec(5, at=at(5), amount=55_00, payee="Vendor B", account="5200"),
    rec(6, at=at(50), amount=None, payee="Vendor B", account="5200"),
    rec(
        7,
        at=at(3),
        amount=500_00,
        direction="credit",
        payer="Customer C",
        payee="Acme Pvt Ltd",
        account="4000",
    ),
]


def ent(name: str, **cfg):
    return dict(
        zip(
            ["in:customer c", "out:vendor a", "out:vendor b"],
            values(make(ENTITY_ROWS, **cfg), name),
            strict=True,
        )
    )


def test_ent_txn_count():
    assert ent("ent_txn_count") == {"in:customer c": 1, "out:vendor a": 4, "out:vendor b": 2}


def test_ent_total_amount_minor():
    assert ent("ent_total_amount_minor") == {
        "in:customer c": 500_00,
        "out:vendor a": 10000_00,
        "out:vendor b": 55_00,
    }


def test_ent_mean_amount_minor():
    assert ent("ent_mean_amount_minor")["out:vendor a"] == 2500_00


def test_ent_median_amount_minor():
    assert ent("ent_median_amount_minor")["out:vendor a"] == 2500_00


def test_ent_log_amount_dispersion():
    d = ent("ent_log_amount_dispersion")
    logs = sorted(math.log1p(a) for a in (1000, 2000, 4000, 3000))
    m = median(logs)
    assert d["out:vendor a"] == pytest.approx(1.4826 * median([abs(v - m) for v in logs]))
    assert d["in:customer c"] is None and d["out:vendor b"] is None  # fewer than 2 amounts


def test_ent_first_seen():
    assert ent("ent_first_seen")["out:vendor a"] == "2024-01-01"


def test_ent_last_seen():
    assert ent("ent_last_seen")["out:vendor a"] == "2024-04-10"


def test_ent_max_gap_days():
    assert ent("ent_max_gap_days") == {
        "in:customer c": None,
        "out:vendor a": pytest.approx(90 - 10 / 24),
        "out:vendor b": 45.0,
    }


def test_ent_recent_count_ratio():
    # dataset ends at day 100; recent = [day 70, day 100]; vendor A: 1 recent row, 3 rows in 70 days before
    r = ent("ent_recent_count_ratio", recent_days=30)
    assert r["out:vendor a"] == pytest.approx((1 / 30) / (3 / 70))
    assert r["in:customer c"] == 0.0  # one row at day 3: 67 days of history, nothing recent


def test_ent_recent_amount_ratio():
    r = ent("ent_recent_amount_ratio", recent_days=30)
    assert r["out:vendor a"] == pytest.approx(3000 / 2000)  # recent median 3000 / earlier median 2000
    assert r["out:vendor b"] is None  # no recent amount


def test_ent_round_share():
    assert ent("ent_round_share") == {
        "in:customer c": 0.0,
        "out:vendor a": 1.0,
        "out:vendor b": 0.0,
    }  # ₹1,000s


def test_ent_share_of_flow():
    s = ent("ent_share_of_flow")
    assert s["out:vendor a"] == pytest.approx(10000 / 10055) and s["in:customer c"] == 1.0


def test_ent_fan_in():
    assert ent("ent_fan_in") == {"in:customer c": 0, "out:vendor a": 1, "out:vendor b": 1}


def test_ent_fan_out():
    assert ent("ent_fan_out") == {"in:customer c": 1, "out:vendor a": 0, "out:vendor b": 0}


def test_ent_after_hours_share():
    assert ent("ent_after_hours_share")["out:vendor a"] == 0.25  # one of four rows at 20:00


def test_ent_non_working_share():
    assert ent("ent_non_working_share")["out:vendor a"] == 0.25  # one Sunday
    assert ent("ent_non_working_share", calendar={"holidays": ["2024-01-01"]})["out:vendor a"] == 0.5


def test_ent_log_amount_trend():
    t = ent("ent_log_amount_trend", trend_min_points=3)
    assert t["out:vendor a"] is not None and t["out:vendor b"] is None  # B has one dated amount


def test_ent_reference_sequential_share():
    assert ent("ent_reference_sequential_share")["out:vendor a"] == pytest.approx(
        2 / 3
    )  # 1→2→3→7 in time order


def test_ent_median_to_peer_ratio():
    rows = [rec(i, at=i, payee=f"V{i}", amount=100_00 * i) for i in range(1, 5)]
    r = values(make(rows, min_peers=3), "ent_median_to_peer_ratio")
    assert r[3] == pytest.approx(400 / 200)  # V4: 400 vs median(100, 200, 300) of the other three
