from datetime import datetime

import pytest
from pydantic import ValidationError

from ledgerspy.canonical import (
    Contribution,
    Direction,
    Entity,
    Explanation,
    Finding,
    FindingStatus,
    Score,
    Signal,
    SignalFamily,
    SubjectType,
    Tier,
    Transaction,
    new_entity_id,
)

TXN = dict(
    txn_id="T-0123456789abcdef",
    dataset_id="D-1",
    source_row=1,
    posted_at=datetime(2026, 4, 1, 10, 30),
    amount_minor=4_98_000_00,
    currency="INR",
    direction=Direction.DEBIT,
)


def test_transaction_valid_and_frozen():
    t = Transaction(**TXN, payee_raw="Stationery Ltd", extra={"memo": "x"})
    assert t.amount_minor == 49_800_000
    with pytest.raises(ValidationError):
        t.amount_minor = 1


def test_transaction_has_no_place_for_label_columns():
    with pytest.raises(ValidationError):
        Transaction(**TXN, isFraud=1)


@pytest.mark.parametrize(
    ("field", "value"),
    [("source_row", 0), ("currency", "inr"), ("direction", "sideways"), ("amount_minor", 12.5)],
)
def test_transaction_rejects_invalid_fields(field, value):
    with pytest.raises(ValidationError):
        Transaction(**{**TXN, field: value})


def test_entity_id_format_enforced():
    Entity(entity_id=new_entity_id(), kind="vendor", canonical_name="A")
    with pytest.raises(ValidationError):
        Entity(entity_id="E-1", kind="vendor", canonical_name="A")


def signal(**kw):
    base = dict(
        subject=SubjectType.TXN,
        subject_id="T-1",
        code="amount.peer_robust_z",
        family="amount",
        strength=0.5,
        weight=0.6,
        template_id="amount.peer_robust_z.v1",
    )
    return Signal(**{**base, **kw})


def test_signal_bounds_and_code_format():
    signal()
    for bad in (
        {"strength": 1.1},
        {"strength": -0.1},
        {"weight": 0.0},
        {"weight": 1.5},
        {"code": "NoFamily"},
        {"family": "astrology"},
    ):
        with pytest.raises(ValidationError):
            signal(**bad)


def test_score_contributions_must_decompose_risk():
    Score(
        subject="txn",
        subject_id="T-1",
        risk=0.8,
        tier=Tier.HIGH,
        contributions={SignalFamily.AMOUNT: 0.65, SignalFamily.TIMING: 0.35},
    )
    Score(subject="txn", subject_id="T-1", risk=0.0, tier=Tier.LOW)
    with pytest.raises(ValidationError):
        Score(subject="txn", subject_id="T-1", risk=0.8, tier="high", contributions={"amount": 0.5})
    with pytest.raises(ValidationError):
        Score(subject="txn", subject_id="T-1", risk=0.8, tier="high")
    with pytest.raises(ValidationError):
        Score(subject="txn", subject_id="T-1", risk=0.0, tier="low", contributions={"amount": 1.0})


def test_explanation_attribution_methods_are_named_honestly():
    e = Explanation(
        subject="txn",
        subject_id="T-1",
        risk=0.5,
        tier="review",
        contributions=[Contribution(family="amount", signal_code="amount.x", share=1.0, sentence="s")],
        attributions=[{"method": "shap", "feature": "log_amount", "value": 0.2}],
    )
    assert e.attributions[0].method == "shap"
    with pytest.raises(ValidationError):
        Explanation(
            subject="txn",
            subject_id="T-1",
            risk=0.5,
            tier="review",
            attributions=[{"method": "shap-style", "feature": "f", "value": 1}],
        )


def test_finding_dismissal_requires_reason_and_stable_key():
    base = dict(
        finding_id="F-1",
        case_id="C-1",
        run_id="R-1",
        finding_type="high_risk_txn",
        subject="txn",
        subject_id="T-1",
    )
    f = Finding(**base)
    assert f.status is FindingStatus.NEW
    assert f.stable_key == "high_risk_txn:txn:T-1"
    assert Finding(**{**base, "run_id": "R-2"}).stable_key == f.stable_key
    Finding(**base, status="dismissed", dismissal_reason="Approved by CFO")
    with pytest.raises(ValidationError):
        Finding(**base, status="dismissed")
    with pytest.raises(ValidationError):
        Finding(**base, status="dismissed", dismissal_reason="  ")
    with pytest.raises(ValidationError):
        Finding(**base, status="confirmed", dismissal_reason="x")
