"""P6: point-in-time correctness, peer fallback, missing-data policy and determinism on in-memory data."""

import random
from datetime import timedelta

import pytest

from ledgerspy.features import matrix
from ledgerspy.features.history import peer_levels
from ledgerspy.features.spec import REGISTRY, Level, Missing, Semantics, implemented
from tests.backend.features_support import ALL_FIELDS, T0, make, rec

LIMITS = [{"name": "claim", "amount_minor": 5_000_00}, {"name": "cfo", "amount_minor": 2_00_000_00}]
FULL = {"approval_thresholds": LIMITS, "calendar": {"holidays": ["2024-01-26", "2024-03-25"]}}
P4 = {}  # a current P4 run with no issues


def random_records(seed: int, n: int = 400, *, date_only: bool = False) -> list[dict]:
    """A seeded random ledger: several payers/payees, accounts, flows, many identical timestamps."""
    r = random.Random(seed)
    out = []
    for i in range(1, n + 1):
        day = r.randint(0, 200)
        ts = (
            T0 + timedelta(days=day)
            if date_only
            else T0 + timedelta(days=day, hours=r.choice([0, 0, 3, 9, 14]))
        )
        payee = r.choice(["V1", "V2", "V3", "V4", "V5", "Acme Pvt Ltd"])
        payer = "Acme Pvt Ltd" if payee != "Acme Pvt Ltd" else r.choice(["C1", "C2", "V2"])
        out.append(
            rec(
                i,
                at=ts,
                amount=r.choice([None] + [r.randint(1, 500_000) * 100 + r.randint(0, 99)] * 20),
                direction="debit" if payee != "Acme Pvt Ltd" else "credit",
                payer=payer,
                payee=payee,
                account=r.choice(["5100", "5200", "4000"]),
                reference=f"INV/{r.randint(1, 999):04d}",
                document_date=(ts - timedelta(days=r.randint(0, 40))).date(),
            )
        )
    return out


def tx_matrix(records, **kw):
    kw = {**FULL, "min_entity_history": 3, "min_peer_history": 5, "mapped": ALL_FIELDS - {"txn_type"}, **kw}
    return matrix.build(make(records, p4=P4, **kw)).transactions


def by_txn(m):
    return {
        t: {n: c[i] for n, c in m.columns.items()} | {f"{n}__basis": b[i] for n, b in m.basis.items()}
        for i, t in enumerate(m.subjects)
    }


HISTORICAL = [
    s.name for s in implemented(Level.TRANSACTION) if s.semantics in (Semantics.ROW, Semantics.POINT_IN_TIME)
]


# ---------------------------------------------------------------------------------------------- point in time


def test_a_dramatic_future_does_not_change_the_past():
    """Huge later transactions would move every whole-dataset statistic; earlier features stay put."""
    past = [rec(i, at=i * 7, amount=10_000_00 + i * 1_00, reference=f"V/{i:04d}") for i in range(1, 30)]
    future = [rec(100 + i, at=300 + i, amount=90_00_000_00, reference=f"V/{900 + i}") for i in range(1, 40)]
    future += [rec(200, at=300.5, amount=5_00, payee="Elsewhere", payer="Vendor A", direction="debit")]
    before = by_txn(tx_matrix(past))
    after = by_txn(tx_matrix(past + future))
    for txn, feats in before.items():
        assert {k: after[txn][k] for k in feats} == feats, txn
    # the future really is dramatic: the later rows see the earlier ones and a 900x jump
    assert after["T00101"]["cp_prior_count"] == 29 and after["T00101"]["amount_to_cp_median_ratio"] > 500


@pytest.mark.parametrize("seed", [1, 2, 3])
@pytest.mark.parametrize("date_only", [False, True])
def test_truncating_the_future_never_changes_a_row(seed, date_only):
    """Property: for any cut-off t, rows at or before t get identical features with and without later rows."""
    records = random_records(seed, date_only=date_only)
    basis = "date" if date_only else "datetime"
    full = by_txn(tx_matrix(records, time_basis=basis))
    cuts = sorted({r["posted_at"] for r in records if r["posted_at"]})
    for cut in (cuts[len(cuts) // 5], cuts[len(cuts) // 2], cuts[-3]):
        kept = [r for r in records if r["posted_at"] and r["posted_at"] <= cut]
        part = by_txn(tx_matrix(kept, time_basis=basis))
        for txn, feats in part.items():
            assert feats == {k: full[txn][k] for k in feats}, (cut, txn)


@pytest.mark.parametrize("seed", [4, 5])
def test_input_order_never_matters(seed):
    records = random_records(seed)
    shuffled = records[:]
    random.Random(99).shuffle(shuffled)
    assert by_txn(tx_matrix(records)) == by_txn(tx_matrix(shuffled))


def test_rows_sharing_a_timestamp_never_see_each_other():
    same = T0 + timedelta(days=5)
    rows = [rec(1, at=0, amount=100_00), rec(2, at=same, amount=900_00), rec(3, at=same, amount=1_00)]
    m = tx_matrix(rows)
    assert m.columns["cp_prior_count"] == [0, 1, 1]
    assert m.columns["cp_prev_amount_ratio"] == [None, 9.0, 0.01]  # both compare with row 1 only


def test_rows_without_a_timestamp_get_no_history_and_are_nobody_s_history():
    rows = [rec(1, at=0), rec(2, at=None, amount=99_000_00), rec(3, at=1)]
    m = tx_matrix(rows)
    assert m.columns["cp_prior_count"] == [0, None, 1]
    assert m.columns["cp_prev_amount_ratio"][2] == 1.0  # the undated 99,000 is not "previous"


def test_entity_features_are_post_period_and_never_transaction_features():
    for s in implemented(Level.ENTITY):
        assert s.semantics == Semantics.POST_PERIOD
    for s in implemented(Level.TRANSACTION):
        assert s.semantics != Semantics.POST_PERIOD


# ---------------------------------------------------------------------------------------------- peer fallback


def _z_basis(records, **kw):
    inp = make(records, **{"min_entity_history": 3, "min_peer_history": 4, **kw})
    out = REGISTRY["amount_robust_z"].fn(inp)
    return out[0], out[1]


def test_peer_fallback_entity_then_account_then_type_then_global_then_unavailable():
    rows = [rec(1, at=0, payee="X", account="5100", txn_type="T1")]  # nothing before it
    rows += [rec(10 + i, at=1 + i, payee=f"P{i}", account="5100", txn_type="T9") for i in range(4)]  # account
    rows += [rec(20 + i, at=10 + i, payee=f"Q{i}", account=f"7{i}", txn_type="T1") for i in range(4)]  # type
    rows += [rec(30 + i, at=20 + i, payee=f"R{i}", account=f"8{i}", txn_type=f"U{i}") for i in range(2)]
    rows += [rec(40 + i, at=30 + i, payee="X", account="5100", txn_type="T1") for i in range(4)]  # entity
    rows += [
        rec(50, at=40, payee="New1", account="5100", txn_type="T9"),  # account peers exist (5100: 9 rows)
        rec(51, at=41, payee="New2", account="9999", txn_type="T1"),  # type peers exist (T1)
        rec(52, at=42, payee="New3", account="9998", txn_type="U7"),  # global only
        rec(53, at=43, payee="X", account="5100", txn_type="T1"),  # its own history (4 earlier rows)
    ]
    values_, basis = _z_basis(rows)
    got = dict(zip([r["txn_id"] for r in rows], basis, strict=True))
    assert got["T00001"] == "unavailable" and values_[0] is None
    assert [got[t] for t in ("T00050", "T00051", "T00052", "T00053")] == [
        "account",
        "txn_type",
        "global",
        "counterparty",
    ]


def test_levels_without_data_support_are_skipped():
    names = [lv.name for lv in peer_levels(make([rec(1, txn_type="A")]))]
    assert names == ["counterparty", "account", "txn_type", "global"]
    untyped = make([rec(1)], mapped=ALL_FIELDS - {"txn_type"})
    assert [lv.name for lv in peer_levels(untyped)] == ["counterparty", "account", "global"]  # no type mapped
    paysim = make([rec(1, txn_type="TRANSFER")], adapter="paysim")
    assert [lv.name for lv in peer_levels(paysim)] == [
        "counterparty",
        "txn_type",
        "global",
    ]  # account = sender


def test_baseline_uses_only_the_recent_window():
    rows = [rec(i, at=i, amount=1_000_00) for i in range(1, 11)] + [
        rec(i, at=i, amount=2_000_00) for i in range(11, 16)
    ]
    rows.append(rec(99, at=50, amount=2_000_00))
    z, _ = _z_basis(rows, baseline_window=5)
    assert z[-1] == 0.0  # only the last 5 (all ₹2,000) count; the older ₹1,000s are outside the window


# ---------------------------------------------------------------------------------------------- missing data


def test_every_feature_follows_its_missing_data_policy():
    complete = random_records(7, n=200)
    blank = {
        "txn_id": "T99999",
        "source_row": 99999,
        "posted_at": None,
        "document_date": None,
        "amount_minor": None,
        "direction": None,
        "account": None,
        "payer_raw": None,
        "payee_raw": None,
        "reference": None,
        "txn_type": None,
        "normalized_fields": [],
        "extra": {},
    }
    m = tx_matrix(complete + [blank])
    assert set(m.columns) == {s.name for s in implemented(Level.TRANSACTION)} - {"txn_type"}
    for name, col in m.columns.items():
        spec = REGISTRY[name]
        if "never null" in spec.missing_detail:
            assert None not in col, name  # data-quality flags: 0/false is a real value
            continue
        assert col[-1] is None, name  # nothing is invented for a row without inputs (no silent zeros)
        assert any(v is not None for v in col), name  # and the feature does produce values when it can
        if spec.missing == Missing.NULL_IF_INSUFFICIENT_HISTORY:
            first = min(range(len(complete)), key=lambda i: complete[i]["posted_at"])
            assert col[first] is None, name  # the very first row has no history to compare with


# ---------------------------------------------------------------------------------------------- determinism


def test_same_input_same_config_same_values():
    records = random_records(11)
    a = matrix.build(make(records, p4=P4, **FULL))
    b = matrix.build(make(records, p4=P4, **FULL))
    assert a.transactions.values_sha256() == b.transactions.values_sha256()
    assert a.entities.values_sha256() == b.entities.values_sha256()
    assert a.manifest == b.manifest


def test_configuration_changes_are_visible_in_the_manifest():
    records = random_records(12)
    a = matrix.build(make(records, p4=P4, **FULL)).manifest
    b = matrix.build(make(records, p4=P4, **FULL, min_entity_history=9)).manifest
    assert a["features_config_sha256"] != b["features_config_sha256"]
    assert a["registry_sha256"] == b["registry_sha256"]


def test_float_values_are_finite():
    records = random_records(13)
    m = tx_matrix(records)
    floats = [v for c in m.columns.values() for v in c if isinstance(v, float)]
    assert floats and all(v == v and abs(v) != float("inf") for v in floats)
