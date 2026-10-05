"""P5: internal coherence (ledger, bank, cash flow), every scenario present and resolvable, semantics,
normal controls, and ground-truth separation."""

import difflib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import date, datetime, time

import pytest

from ledgerspy.config.schema import normalize_header
from tests.backend.test_synth_generation import CFG, INPUTS, REFERENCE, read

GT = json.loads((REFERENCE / "ground_truth" / "SYNTHETIC_ground_truth.json").read_text(encoding="utf-8"))
SC = {s["scenario_id"]: s for s in GT["scenarios"]}
LEDGER = {r["Txn ID"]: r for r in read("SYNTHETIC_ledger.csv")}
BANK = read("SYNTHETIC_bank_statement.csv")
VENDORS = {r["vendor_id"]: r for r in read("SYNTHETIC_vendor_master.csv")}
EMPLOYEES = {r["employee_id"]: r for r in read("SYNTHETIC_employee_master.csv")}
CUSTOMERS = {r["customer_id"]: r for r in read("SYNTHETIC_customer_master.csv")}
START, END = date(2023, 4, 1), date(2026, 3, 31)


def minor(text: str) -> int:
    return round(float(text) * 100) if text else 0


def rows(sid: str) -> list[dict]:
    return [LEDGER[t] for t in SC[sid]["affected"]["ledger_txn_ids"]]


def posted(r: dict) -> datetime:
    return datetime.fromisoformat(r["Posting Date"])


# ---------- coherence ----------


MANIFEST = json.loads((REFERENCE / "SYNTHETIC_manifest.json").read_text())


def test_ledger_is_coherent():
    assert len(LEDGER) == MANIFEST["files"]["SYNTHETIC_ledger.csv"]["rows"] > 5000
    for r in LEDGER.values():
        assert minor(r["Amount"]) > 0 and r["Dr/Cr"] in ("Dr", "Cr") and r["Currency"] == "INR"
        assert START <= posted(r).date() <= END
        assert date.fromisoformat(r["Document Date"]) <= posted(r).date()
        assert r["Payer"] and r["Payee"]
    span = {posted(r).year for r in LEDGER.values()}
    assert span == {2023, 2024, 2025, 2026}  # three April–March years


def test_bank_balances_are_continuous_per_account():
    by_account = defaultdict(list)
    for b in BANK:
        by_account[b["Account No"]].append(b)
    assert len(by_account) == 2
    for items in by_account.values():
        for prev, cur in zip(items, items[1:], strict=False):
            assert cur["Txn Date"] >= prev["Txn Date"]
            delta = minor(cur["Deposit"]) - minor(cur["Withdrawal"])
            assert minor(cur["Balance"]) == minor(prev["Balance"]) + delta
            assert bool(cur["Deposit"]) != bool(cur["Withdrawal"])
        assert min(minor(b["Balance"]) for b in items) > 0


def test_bank_statement_reconciles_with_the_ledger_by_construction():
    """bank rows = ledger rows − ledger-only − (batched − 1) − (one-to-many − 1) + bank-only."""
    s = {k: SC[k]["affected"] for k in ("S-S-01", "S-S-02", "S-S-04", "S-S-05")}
    expected = (
        len(LEDGER)
        - len(s["S-S-01"]["ledger_txn_ids"])
        - (len(s["S-S-04"]["ledger_txn_ids"]) - 1)
        - (len(s["S-S-05"]["ledger_txn_ids"]) - 1)
        + len(s["S-S-02"]["bank_txn_ids"])
    )
    assert len(BANK) == expected
    narrations = Counter(b["Narration"].split("/")[0] for b in BANK)
    assert narrations["ACH DR"] == 2 and any(n.startswith("SMS ALERT") for n in narrations)


def test_cash_flow_is_coherent_with_the_bank():
    cash = read("SYNTHETIC_cashflow_monthly.csv")
    assert len(cash) == 72
    for company in ("LVC", "LVE"):
        months = [c for c in cash if c["company_code"] == company]
        assert [c["month"] for c in months] == sorted(c["month"] for c in months)
        for prev, cur in zip(months, months[1:], strict=False):
            assert cur["opening_balance"] == prev["closing_balance"]
        for c in months:
            assert minor(c["closing_balance"]) == minor(c["opening_balance"]) + minor(c["inflows"]) - minor(
                c["outflows"]
            )
        account = next(b["Account No"] for b in BANK if b["Txn ID"] and _company_of(b) == company)
        bank_rows = [b for b in BANK if b["Account No"] == account]
        assert sum(minor(c["inflows"]) for c in months) == sum(minor(b["Deposit"]) for b in bank_rows)
        assert sum(minor(c["outflows"]) for c in months) == sum(minor(b["Withdrawal"]) for b in bank_rows)
        assert months[-1]["closing_balance"] == bank_rows[-1]["Balance"]


def _company_of(bank_row: dict) -> str:
    groups = {g["bank_account"]: g["entity_code"] for g in read("SYNTHETIC_group_entities.csv")}
    return groups[bank_row["Account No"]]


def test_normal_population_follows_company_routine():
    """Rows in no injected scenario post inside business hours on working days; Tue/Fri dominate AP runs."""
    injected = {t for s in GT["scenarios"] if not s["is_control"] for t in s["affected"]["ledger_txn_ids"]}
    controls = set(SC["S-U-08"]["affected"]["ledger_txn_ids"])  # documented quarter-end overtime
    holidays = {date.fromisoformat(h["date"]) for h in read("SYNTHETIC_holiday_calendar.csv")}
    weekdays = Counter()
    for t, r in LEDGER.items():
        if t in injected or t in controls:
            continue
        p = posted(r)
        assert p.weekday() != 6 and p.date() not in holidays, t
        assert time(9, 30) <= p.time() <= time(18, 30), t
        if r["Counterparty Type"] == "vendor":
            weekdays[p.weekday()] += 1
    assert (weekdays[1] + weekdays[4]) / sum(weekdays.values()) > 0.7


def test_vendors_have_distinct_amount_profiles():
    """Normal vendors differ in level and spread (peer comparison needs real structure)."""
    by_vendor = defaultdict(list)
    for r in LEDGER.values():
        if r["Counterparty Type"] == "vendor":
            by_vendor[r["Counterparty ID"]].append(math.log(minor(r["Amount"])))
    medians = sorted(sorted(v)[len(v) // 2] for v in by_vendor.values() if len(v) >= 10)
    assert math.exp(medians[-1] - medians[0]) > 50  # median spend differs by > 50x across vendors


# ---------- ground truth: completeness and resolution ----------


def test_every_required_scenario_letter_is_present():
    letters = Counter(s["category"] for s in GT["scenarios"])
    assert set("ABCDEFGHIJKLMNOPQRSTUVWX") <= set(letters)
    assert GT["scenario_count"] == 63 and GT["control_scenarios"] == 18 and GT["injected_scenarios"] == 45
    assert all(s["is_control"] for s in GT["scenarios"] if s["category"] == "U")
    for s in GT["scenarios"]:
        assert s["description"] and s["expected_detection"]["targets"] and s["expected_detection"]["phases"]
        a = s["affected"]
        assert any(a[k] for k in a), s["scenario_id"]


def test_every_ground_truth_reference_resolves():
    bank_ids = {b["Txn ID"] for b in BANK}
    groups = {g["entity_code"] for g in read("SYNTHETIC_group_entities.csv")}
    for s in GT["scenarios"]:
        a = s["affected"]
        assert set(a["ledger_txn_ids"]) <= set(LEDGER), s["scenario_id"]
        assert set(a["bank_txn_ids"]) <= bank_ids, s["scenario_id"]
        assert set(a["vendor_ids"]) <= set(VENDORS), s["scenario_id"]
        assert set(a["employee_ids"]) <= set(EMPLOYEES), s["scenario_id"]
        assert set(a["customer_ids"]) <= set(CUSTOMERS), s["scenario_id"]
        assert set(a["group_entities"]) <= groups, s["scenario_id"]
        for ids in s["stages"].values():
            assert set(ids) <= set(a["ledger_txn_ids"])
        for other in s["also_part_of"]:
            assert other in SC
    for kind in GT["expected_p4_row_issues"].values():
        for items in kind.values():
            assert all(x["txn_id"] in LEDGER and x["scenario_id"] in SC for x in items)


def test_ground_truth_csv_matches_json():
    flat = read("ground_truth/SYNTHETIC_ground_truth.csv")
    total = sum(len(ids) for s in GT["scenarios"] for ids in s["affected"].values())
    assert len(flat) == total
    assert {r["scenario_id"] for r in flat} == {
        s["scenario_id"] for s in GT["scenarios"] if any(s["affected"].values())
    }


def test_transactions_take_part_in_several_scenarios():
    membership = Counter(t for s in GT["scenarios"] for t in s["affected"]["ledger_txn_ids"])
    shared = [t for t, n in membership.items() if n >= 2]
    assert len(shared) >= 20
    combined = SC["S-X-01"]
    assert set(combined["also_part_of"]) == {"S-G-02", "S-H-02", "S-A-02", "S-N-05"}
    assert set(SC["S-A-02"]["affected"]["ledger_txn_ids"]) <= set(combined["affected"]["ledger_txn_ids"])


# ---------- scenario semantics (the evidence is really in the data) ----------


def test_ghost_vendor_evidence():
    v = VENDORS[SC["S-F-01"]["affected"]["vendor_ids"][0]]
    e = EMPLOYEES[SC["S-F-01"]["affected"]["employee_ids"][0]]
    assert v["tax_id"] == v["phone"] == v["email"] == ""
    assert v["address"] == e["home_address"] and v["bank_account"] == e["bank_account"]
    assert v["created_by"] == e["employee_id"]
    refs = [r["Invoice No"].split("/")[-1] for r in sorted(rows("S-F-01"), key=posted)]
    assert refs == [f"{i:03d}" for i in range(1, 7)]  # sequential invoices
    limit = CFG.approval_limits.vendor_payment_minor
    assert all(
        minor(r["Amount"]) % 100_000 == 0 and 0.9 * limit < minor(r["Amount"]) < limit for r in rows("S-F-01")
    )


def test_lookalike_vendors_resemble_their_originals():
    for sid in ("S-E-01", "S-E-02"):
        orig, twin = (VENDORS[v] for v in SC[sid]["affected"]["vendor_ids"])
        a, b = normalize_header(orig["vendor_name"]), normalize_header(twin["vendor_name"])
        ratio = difflib.SequenceMatcher(None, a, b).ratio()
        assert orig["vendor_name"] != twin["vendor_name"] and ratio > 0.75, ratio
        assert (
            orig["bank_account"] != twin["bank_account"] and twin["onboarding_date"] > orig["onboarding_date"]
        )


def test_threshold_splitting_uses_the_configured_limits():
    lim = CFG.approval_limits.expense_claim_minor
    amounts = [minor(r["Amount"]) for r in rows("S-J-01")]
    assert all(0.97 * lim < a < lim for a in amounts) and sum(amounts) > lim
    assert len({posted(r).date() for r in rows("S-J-01")}) == 1
    vlim = CFG.approval_limits.vendor_payment_minor
    assert all(0.97 * vlim < minor(r["Amount"]) < vlim for r in rows("S-J-02"))
    policy = read("SYNTHETIC_group_entities.csv")[0]
    assert minor(policy["expense_claim_approval_limit"]) == lim  # the policy is in the data, not in code


def test_circular_rings_are_temporal_and_value_retaining():
    for sid, k in (("S-O-01", 3), ("S-O-02", 4)):
        legs = sorted(rows(sid), key=posted)
        assert SC[sid]["injection_parameters"]["k"] == k
        amounts = [minor(r["Amount"]) for r in legs]
        assert amounts[-1] / amounts[0] > 0.9 and (posted(legs[-1]) - posted(legs[0])).days <= 31
    v, c = (
        VENDORS[SC["S-O-01"]["affected"]["vendor_ids"][0]],
        CUSTOMERS[SC["S-O-01"]["affected"]["customer_ids"][0]],
    )
    assert v["vendor_name"] == c["customer_name"] and v["tax_id"] == c["tax_id"]


def test_benford_subgroup_is_unnatural_but_unlabelled():
    rs = rows("S-T-01")
    assert len(rs) >= 300
    assert {r["Amount"][0] for r in rs} <= {"7", "8", "9"}
    assert SC["S-T-01"]["scenario_type"] == "digit_distribution_anomaly"


def test_temporal_anomalies_fall_outside_the_routine():
    holidays = {h["date"] for h in read("SYNTHETIC_holiday_calendar.csv")}
    for r in rows("S-K-01"):
        p = posted(r)
        assert (
            p.weekday() == 6
            or r["Posting Date"][:10] in holidays
            or not time(9, 30) <= p.time() <= time(18, 30)
        )


def test_shared_attribute_scenarios_and_controls():
    v, e = (
        VENDORS[SC["S-N-01"]["affected"]["vendor_ids"][0]],
        EMPLOYEES[SC["S-N-01"]["affected"]["employee_ids"][0]],
    )
    assert v["phone"] == e["phone"]
    v, e = (
        VENDORS[SC["S-M-01"]["affected"]["vendor_ids"][0]],
        EMPLOYEES[SC["S-M-01"]["affected"]["employee_ids"][0]],
    )
    assert v["bank_account"] == e["bank_account"]
    assert all(r["Posted By"] == e["employee_id"] for r in rows("S-M-01"))
    rel = read("SYNTHETIC_relationships.csv")
    declared = {(r["entity_a"], r["entity_b"]) for r in rel}
    g1, g2 = SC["S-N-04"]["affected"]["vendor_ids"]
    assert (g1, g2) in declared or (g2, g1) in declared  # the legitimate sharing is declared
    ok_vendor, head = SC["S-M-02"]["affected"]["vendor_ids"][0], SC["S-M-02"]["affected"]["employee_ids"][0]
    assert (ok_vendor, head) in declared and all(r["Approved By"] != head for r in rows("S-M-02"))
    bad_pairs = {(SC["S-M-01"]["affected"]["vendor_ids"][0], SC["S-M-01"]["affected"]["employee_ids"][0])}
    assert not (bad_pairs & declared)  # suspicious links are never declared


def test_controls_resemble_suspicious_patterns():
    assert all(minor(r["Amount"]) % 500_00 == 0 for r in rows("S-U-01"))  # round, legitimate
    assert (
        minor(max(rows("S-U-05"), key=lambda r: minor(r["Amount"]))["Amount"])
        > 10 * minor(rows("S-A-01")[0]["Amount"]) // 9
    )
    a, b = rows("S-U-13")
    assert a["Payee"] == b["Payee"] and a["Amount"] == b["Amount"] and a["Invoice No"] != b["Invoice No"]
    assert len({r["Invoice No"] for r in rows("S-U-05")}) == 1


# ---------- ground-truth leakage ----------

FORBIDDEN = re.compile(
    r"fraud|ghost|scenario|suspicious|inject|anomal|ground.?truth|is_control|shell|fake", re.IGNORECASE
)


@pytest.mark.parametrize("name", INPUTS)
def test_inputs_carry_no_ground_truth(name):
    text = (REFERENCE / name).read_text(encoding="utf-8")
    assert not FORBIDDEN.search(text), FORBIDDEN.search(text)
    header = text.splitlines()[0].split(",")
    assert not {"scenario_id", "scenario_type", "is_fraud", "label", "target"} & {h.lower() for h in header}
