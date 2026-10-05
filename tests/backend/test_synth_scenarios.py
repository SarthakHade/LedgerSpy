"""P5: every scenario in the ground truth is observable in the generated files themselves.

`test_synth_dataset.py` checks that ground-truth IDs resolve. Here, one check per scenario ID looks at the
ledger, bank statement and masters and confirms that the behaviour the ground truth describes is really there.
The registry must cover exactly the scenario IDs in the ground truth, so a new scenario cannot be added
without a data-level check.
"""

from collections import Counter
from datetime import date, datetime, time, timedelta
from statistics import median

import pytest

from tests.backend.test_synth_dataset import (
    BANK,
    CUSTOMERS,
    EMPLOYEES,
    GT,
    LEDGER,
    SC,
    VENDORS,
    minor,
    posted,
    rows,
)
from tests.backend.test_synth_generation import CFG, read

GROUP = {g["entity_code"]: g for g in read("SYNTHETIC_group_entities.csv")}
HOLIDAYS = {h["date"] for h in read("SYNTHETIC_holiday_calendar.csv")}
DECLARED = {frozenset((r["entity_a"], r["entity_b"])) for r in read("SYNTHETIC_relationships.csv")}
BANK_BY_ID = {b["Txn ID"]: b for b in BANK}
BANK_ACCOUNT_OF = {code: g["bank_account"] for code, g in GROUP.items()}
LIMIT = CFG.approval_limits.vendor_payment_minor
PERIOD = (
    date.fromisoformat(GT["parameters"]["start_date"]),
    date.fromisoformat(GT["parameters"]["end_date"]),
)
MONTHS = (PERIOD[1].year - PERIOD[0].year) * 12 + PERIOD[1].month - PERIOD[0].month + 1
CLAIM_LIMIT = CFG.approval_limits.expense_claim_minor


def paid_to(cp_id: str) -> list[dict]:
    """Every payment (Dr) to a counterparty in the whole ledger, in time order."""
    return sorted(
        (r for r in LEDGER.values() if r["Counterparty ID"] == cp_id and r["Dr/Cr"] == "Dr"), key=posted
    )


def amounts(rs: list[dict]) -> list[int]:
    return [minor(r["Amount"]) for r in rs]


def vendors(sid: str) -> list[dict]:
    return [VENDORS[v] for v in SC[sid]["affected"]["vendor_ids"]]


def employees(sid: str) -> list[dict]:
    return [EMPLOYEES[e] for e in SC[sid]["affected"]["employee_ids"]]


def bank_lines(sid: str) -> list[dict]:
    return [BANK_BY_ID[b] for b in SC[sid]["affected"]["bank_txn_ids"]]


def bank_amount(b: dict) -> int:
    return minor(b["Withdrawal"]) or minor(b["Deposit"])


def working_day(d: date) -> bool:
    return d.weekday() != 6 and d.isoformat() not in HOLIDAYS


def last_working_day(d: date) -> date:
    while not working_day(d):
        d -= timedelta(days=1)
    return d


def in_business_hours(r: dict) -> bool:
    p = posted(r)
    return working_day(p.date()) and time(9, 30) <= p.time() <= time(18, 30)


def others(sid: str, cp_id: str) -> list[int]:
    """The counterparty's payments that are not part of this scenario."""
    mine = set(SC[sid]["affected"]["ledger_txn_ids"])
    return amounts([r for r in paid_to(cp_id) if r["Txn ID"] not in mine])


def span_days(rs: list[dict]) -> int:
    ps = sorted(posted(r) for r in rs)
    return (ps[-1] - ps[0]).days


def max_in_window(rs: list[dict], days: int) -> int:
    ps = sorted(posted(r) for r in rs)
    return max(sum(1 for q in ps if p <= q < p + timedelta(days=days)) for p in ps)


def onboarded(v: dict) -> date:
    return date.fromisoformat(v["onboarding_date"])


# ---------- one check per scenario ----------


def a01(sid):
    (r,) = rows(sid)
    assert minor(r["Amount"]) > 5 * median(others(sid, r["Counterparty ID"]))
    assert in_business_hours(r)  # otherwise routine


def a02(sid):
    (r,) = rows(sid)
    assert minor(r["Amount"]) > 5 * median(others(sid, r["Counterparty ID"]))


def b01(sid):
    (v,) = vendors(sid)
    mine = amounts(paid_to(v["vendor_id"]))
    assert len(mine) == SC[sid]["injection_parameters"]["payments"] and all(a % 1000_00 == 0 for a in mine)
    peers = [
        a
        for p in VENDORS.values()
        if p["category"] == v["category"] and p["vendor_id"] != v["vendor_id"]
        for a in amounts(paid_to(p["vendor_id"]))
    ]
    assert sum(a % 1000_00 == 0 for a in peers) / len(peers) < 0.1  # peers pay paise-level amounts


def b02(sid):
    assert all(a % 1000_00 == 0 for a in amounts(rows(sid)))
    stage3 = set(SC["S-Q-01"]["stages"]["stage_3_round_offhours"])
    assert set(SC[sid]["affected"]["ledger_txn_ids"]) <= stage3


def c01(sid):
    a, b = rows(sid)
    assert {k: v for k, v in a.items() if k != "Txn ID"} == {k: v for k, v in b.items() if k != "Txn ID"}


def c02(sid):
    a, b = sorted(rows(sid), key=posted)
    assert (a["Payee"], a["Invoice No"], a["Amount"]) == (b["Payee"], b["Invoice No"], b["Amount"])
    assert (posted(b).date() - posted(a).date()).days == SC[sid]["injection_parameters"]["days_later"]


def d01(sid):
    a, b = sorted(rows(sid), key=posted)
    assert a["Payee"] == b["Payee"] and a["Amount"] == b["Amount"]
    assert b["Invoice No"] == a["Invoice No"] + "A" and (posted(b).date() - posted(a).date()).days == 1


def d02(sid):
    a, b = sorted(rows(sid), key=posted)
    assert a["Payee"] == b["Payee"] and a["Amount"] != b["Amount"] and a["Invoice No"] != b["Invoice No"]
    assert sorted(a["Amount"]) == sorted(b["Amount"])  # transposed digits
    assert sorted(a["Invoice No"]) == sorted(b["Invoice No"])
    assert (posted(b).date() - posted(a).date()).days <= 3


def e0x(sid):
    orig, twin = vendors(sid)
    assert orig["city"] == twin["city"] and orig["category"] == twin["category"]
    assert orig["bank_account"] != twin["bank_account"] and onboarded(twin) > onboarded(orig)
    assert len(paid_to(twin["vendor_id"])) == len(rows(sid)) == 5
    assert all(r["Counterparty ID"] == twin["vendor_id"] for r in rows(sid))


def f01(sid):
    (v,) = vendors(sid)
    (e,) = employees(sid)
    assert v["created_by"] == e["employee_id"] and v["bank_account"] == e["bank_account"]
    assert all(0.9 * LIMIT < a < LIMIT and a % 1000_00 == 0 for a in amounts(rows(sid)))


def g01(sid):
    (v,) = vendors(sid)
    rs = rows(sid)
    assert rs == paid_to(v["vendor_id"]) and len(rs) == 3
    assert (posted(rs[0]).date() - onboarded(v)).days <= 10 and span_days(rs) <= 25
    peers = [
        a
        for p in VENDORS.values()
        if p["category"] == v["category"] and p["vendor_id"] != v["vendor_id"]
        for a in amounts(paid_to(p["vendor_id"]))
    ]
    assert min(amounts(rs)) > 3 * median(peers)  # ~4x the category median


def g02(sid):
    (v,) = vendors(sid)
    assert (posted(rows(sid)[0]).date() - onboarded(v)).days <= 30


def h01(sid):
    (v,) = vendors(sid)
    rs = rows(sid)
    assert len(rs) == 9 and span_days(rs) <= 16
    mine = set(SC[sid]["affected"]["ledger_txn_ids"])
    rest = [r for r in paid_to(v["vendor_id"]) if r["Txn ID"] not in mine]
    assert len(rest) >= 12 and max_in_window(rest, 16) <= 5  # otherwise about monthly: 9 is a spike


def h02(sid):
    rs = rows(sid)
    assert len(rs) == 6 and span_days(rs) <= 18


def h03(sid):
    (v,) = vendors(sid)
    st = SC[sid]["stages"]
    active = sorted((LEDGER[t] for t in st["active"]), key=posted)
    burst = sorted((LEDGER[t] for t in st["reactivated"]), key=posted)
    assert paid_to(v["vendor_id"]) == sorted(active + burst, key=posted)  # nothing else, ever
    assert len(active) >= 6 and len(burst) == 6
    silent = (posted(burst[0]) - posted(active[-1])).days
    assert silent == SC[sid]["injection_parameters"]["silent_days"] > 0.35 * (PERIOD[1] - PERIOD[0]).days
    assert span_days(burst) <= 21
    assert median(amounts(burst)) > 2.5 * median(amounts(active))


def i01(sid):
    (r,) = rows(sid)
    (e,) = employees(sid)
    assert minor(r["Amount"]) == SC[sid]["injection_parameters"]["amount_minor"] > CLAIM_LIMIT
    past = others(sid, e["employee_id"])
    assert 1000_00 <= median(past) <= 3000_00  # usually ₹1–3k
    assert minor(r["Amount"]) > 15 * median(past) and minor(r["Amount"]) > 3 * max(past)


def j01(sid):
    rs = rows(sid)
    assert len(rs) == 3 and len({posted(r).date() for r in rs}) == 1
    assert all(0.97 * CLAIM_LIMIT < a < CLAIM_LIMIT for a in amounts(rs)) and sum(amounts(rs)) > CLAIM_LIMIT


def j02(sid):
    rs = sorted(rows(sid), key=posted)
    assert all(0.97 * LIMIT < a < LIMIT for a in amounts(rs)) and sum(amounts(rs)) > LIMIT
    assert {r["Approved By"] for r in rs} == {SC[sid]["affected"]["employee_ids"][0]}
    assert span_days(rs) <= 6


def k01(sid):
    rs = rows(sid)
    assert len(rs) == 6 and not any(in_business_hours(r) for r in rs)
    assert {r["Posted By"] for r in rs} <= set(SC[sid]["affected"]["employee_ids"])


def k02(sid):
    assert all(posted(r).weekday() == 6 for r in rows(sid))
    assert set(SC[sid]["affected"]["ledger_txn_ids"]) <= set(SC["S-Q-01"]["affected"]["ledger_txn_ids"])


def k03(sid):
    rs = rows(sid)
    day = date.fromisoformat(SC[sid]["injection_parameters"]["date"])
    year_end = last_working_day(date(day.year, 3, 31))  # the last working day of a financial year
    assert len(rs) == 5 and {posted(r).date() for r in rs} == {year_end}
    assert all(time(21, 30) < posted(r).time() < time(22, 0) for r in rs)
    poster = SC[sid]["affected"]["employee_ids"][0]
    assert {r["Posted By"] for r in rs} == {poster}
    assert poster != rows("S-U-08")[0]["Posted By"]  # not the routine quarter-end Finance poster
    v = rs[0]["Counterparty ID"]
    assert min(amounts(rs)) > 3 * median(others(sid, v))


def l01(sid):
    (v,) = vendors(sid)
    rs = rows(sid)
    assert rs == paid_to(v["vendor_id"])
    assert [a // 100 for a in amounts(rs)] == [8500, 9200, 11000, 385000]  # small, small, small, large
    assert (posted(rs[-1]).date() - onboarded(v)).days <= 7 * 8


def m01(sid):
    (v,) = vendors(sid)
    (e,) = employees(sid)
    assert (
        v["bank_account"] == e["bank_account"]
        and frozenset((v["vendor_id"], e["employee_id"])) not in DECLARED
    )
    assert {r["Posted By"] for r in rows(sid)} == {e["employee_id"]}


def m02(sid):
    (v,) = vendors(sid)
    (e,) = employees(sid)
    assert v["address"] == e["home_address"] and frozenset((v["vendor_id"], e["employee_id"])) in DECLARED
    assert rows(sid) and all(r["Approved By"] != e["employee_id"] for r in rows(sid))


def shared(sid, fields, declared):
    (v,) = [x for x in vendors(sid)][:1]
    (e,) = employees(sid)
    pairs = {"address": "home_address", "phone": "phone", "bank_account": "bank_account"}
    for f in fields:
        assert v[f] and v[f] == e[pairs[f]], f
    assert (frozenset((v["vendor_id"], e["employee_id"])) in DECLARED) is declared


def n01(sid):
    shared(sid, ["phone"], False)


def n02(sid):
    shared(sid, ["address", "bank_account"], False)


def n03(sid):
    shared(sid, ["bank_account"], False)


def n04(sid):
    a, b = vendors(sid)
    assert a["vendor_name"] != b["vendor_name"]
    assert (a["phone"], a["address"], a["bank_account"]) == (b["phone"], b["address"], b["bank_account"])
    assert frozenset((a["vendor_id"], b["vendor_id"])) in DECLARED
    assert {r["Counterparty ID"] for r in rows(sid)} == {a["vendor_id"], b["vendor_id"]}


def n05(sid):
    shared(sid, ["phone"], False)


def ring(sid, k):
    legs = sorted(rows(sid), key=posted)
    assert len(legs) >= k and SC[sid]["injection_parameters"]["k"] == k
    assert {r["Company Code"] for r in legs} == {"LVC", "LVE"}
    customer_tax_ids = {CUSTOMERS[c]["tax_id"] for c in SC[sid]["affected"]["customer_ids"]}
    assert {VENDORS[v]["tax_id"] for v in SC[sid]["affected"]["vendor_ids"]} == customer_tax_ids  # both roles
    vals = amounts(legs)
    assert min(vals) / max(vals) > 0.9 and span_days(legs) <= 31


def o01(sid):
    ring(sid, 3)


def o02(sid):
    ring(sid, 4)


def p01(sid):
    vs = vendors(sid)
    assert (
        len(vs) == 3
        and len({v["bank_account"] for v in vs}) == 1
        and len({v["vendor_name"] for v in vs}) == 3
    )
    rs = rows(sid)
    assert {r["Company Code"] for r in rs} == {"LVC", "LVE"}
    assert {r["Counterparty ID"] for r in rs} == {v["vendor_id"] for v in vs}


def p02(sid):
    vs = [v for v in vendors(sid)]
    assert (
        len(vs) == 7
        and len({v["onboarding_date"] for v in vs}) == 1
        and len({v["created_by"] for v in vs}) == 1
    )
    (trigger,) = (LEDGER[t] for t in SC[sid]["stages"]["trigger_receipt"])
    pays = [r for r in rows(sid) if r["Dr/Cr"] == "Dr"]
    assert trigger["Dr/Cr"] == "Cr" and trigger["Company Code"] == "LVE"
    assert all(posted(p) > posted(trigger) for p in pays) and span_days([trigger, *pays]) <= 5
    assert {p["Counterparty ID"] for p in pays} == {v["vendor_id"] for v in vs}


def q01(sid):
    st = {k: sorted((LEDGER[t] for t in v), key=posted) for k, v in SC[sid]["stages"].items()}
    s1, s2, s3 = st["stage_1_normal"], st["stage_2_rising"], st["stage_3_round_offhours"]
    assert posted(s1[-1]) <= posted(s2[0]) and posted(s2[-1]) <= posted(s3[0])
    assert median(amounts(s1)) < median(amounts(s2)) < median(amounts(s3))
    assert sum(a % 1000_00 == 0 for a in amounts(s3)) / len(s3) > 0.8
    assert any(posted(r).weekday() == 6 for r in s3) and not any(posted(r).weekday() == 6 for r in s1 + s2)


def r01(sid):
    rs = rows(sid)
    (e,) = employees(sid)
    p = SC[sid]["injection_parameters"]
    assert len(rs) == p["claims"] and sum(amounts(rs)) == p["total_minor"]
    assert all(650_00 <= a <= 1_960_00 for a in amounts(rs))
    assert {r["Counterparty ID"] for r in rs} == {e["employee_id"]}
    dept = [x["employee_id"] for x in EMPLOYEES.values() if x["department"] == e["department"]]
    per_person = [sum(amounts(paid_to(x))) for x in dept if x != e["employee_id"]]
    assert sum(amounts(rs)) > 3 * median(per_person)


def bank_matches(r: dict) -> list[dict]:
    acct = BANK_ACCOUNT_OF[r["Company Code"]]
    return [b for b in BANK if b["Account No"] == acct and bank_amount(b) == minor(r["Amount"])]


def s01(sid):
    assert len(rows(sid)) == 3 and not bank_lines(sid)
    assert all(not bank_matches(r) for r in rows(sid))  # no line of that amount on the company account


def s02(sid):
    lines = bank_lines(sid)
    assert len(lines) == 3 and not rows(sid)
    ledger_amounts = {minor(r["Amount"]) for r in LEDGER.values()}
    assert all(bank_amount(b) not in ledger_amounts for b in lines)


def s03(sid):
    for r in rows(sid):
        (b,) = [b for b in bank_lines(sid) if bank_amount(b) == minor(r["Amount"])]
        gap = date.fromisoformat(b["Txn Date"]) - posted(r).date()
        assert gap.days >= 8


def s04(sid):
    (b,) = bank_lines(sid)
    assert len(rows(sid)) == 3 and bank_amount(b) == sum(amounts(rows(sid)))
    assert all(not bank_matches(r) for r in rows(sid))


def s05(sid):
    (b,) = bank_lines(sid)
    rs = rows(sid)
    assert len(rs) == 3 and all(r["Dr/Cr"] == "Cr" for r in rs) and minor(b["Deposit"]) == sum(amounts(rs))


def s06(sid):
    diffs = Counter()
    for r in rows(sid):
        (b,) = [
            b
            for b in bank_lines(sid)
            if abs(bank_amount(b) - minor(r["Amount"])) in (590_00, 236_00)
            and b["Account No"] == BANK_ACCOUNT_OF[r["Company Code"]]
        ]
        diffs[bank_amount(b) - minor(r["Amount"])] += 1
    assert diffs == {590_00: 1, -236_00: 2}


def s07(sid):
    (r,) = rows(sid)
    (b,) = bank_lines(sid)
    (e,) = employees(sid)
    assert bank_amount(b) == minor(r["Amount"])
    assert e["name"].upper() in b["Narration"] and r["Payee"].upper()[:12] not in b["Narration"]
    assert b["Narration"].endswith(e["bank_account"][-4:])


def t01(sid):
    rs = rows(sid)
    assert len(rs) >= 300 and {r["Amount"][0] for r in rs} <= {"7", "8", "9"}


def u01(sid):
    rs = rows(sid)
    assert (
        len(rs) == MONTHS
        and all(a % 500_00 == 0 for a in amounts(rs))
        and all(in_business_hours(r) for r in rs)
    )


def u02(sid):
    for v in vendors(sid):
        vals = amounts([r for r in rows(sid) if r["Counterparty ID"] == v["vendor_id"]])
        assert len(vals) == MONTHS and len(set(vals)) == 1 and vals[0] % 1000_00 == 0  # one retainer, monthly


def u03(sid):
    a, b = vendors(sid)
    assert a["vendor_name"].split()[0] == b["vendor_name"].split()[0]
    for f in ("category", "city", "tax_id", "bank_account", "phone", "address"):
        assert a[f] != b[f], f
    assert all(len(paid_to(v["vendor_id"])) >= 10 for v in (a, b))


def u04(sid):
    (v,) = vendors(sid)
    assert PERIOD[0] < onboarded(v) < PERIOD[1]
    rs = rows(sid)
    assert rs == paid_to(v["vendor_id"]) and len(rs) >= 20 and all(in_business_hours(r) for r in rs)
    peers = [
        a
        for p in VENDORS.values()
        if p["category"] == v["category"] and p["vendor_id"] != v["vendor_id"]
        for a in amounts(paid_to(p["vendor_id"]))
    ]
    assert median(amounts(rs)) < 2 * median(peers)


def u05(sid):
    rs = rows(sid)
    assert len({r["Invoice No"] for r in rs}) == 1 and sum(amounts(rs)) == 480000000
    assert all(
        r["Approved By"]
        == next(e for e in EMPLOYEES.values() if "Chief Financial" in e["role"])["employee_id"]
        for r in rs
    )


def u06(sid):
    (v,) = vendors(sid)
    by_month = Counter(posted(r).month for r in paid_to(v["vendor_id"]))
    festive = sum(by_month[m] for m in (10, 11, 12)) / 3
    rest = sum(n for m, n in by_month.items() if m not in (10, 11, 12)) / 9
    assert festive > 1.5 * rest
    for year in range(PERIOD[0].year, PERIOD[1].year):  # recurring: every festive season is busier
        fy = [r for r in paid_to(v["vendor_id"]) if PERIOD[0].replace(year=year) <= posted(r).date()]
        fy = [r for r in fy if posted(r).date() < PERIOD[0].replace(year=year + 1)]
        peak = sum(posted(r).month in (10, 11, 12) for r in fy) / 3
        assert peak > (len(fy) - 3 * peak) / 9
    assert all(posted(r).month in (10, 11, 12) for r in rows(sid))


def u07(sid):
    rs = sorted(rows(sid), key=posted)
    assert len({r["Counterparty ID"] for r in rs}) == 3
    assert all(a == round(0.97 * CLAIM_LIMIT) for a in amounts(rs))
    assert all((posted(b) - posted(a)).days > 60 for a, b in zip(rs, rs[1:], strict=False))


def u08(sid):
    rs = rows(sid)
    assert len(rs) == MONTHS // 3 and len({r["Posted By"] for r in rs}) == 1
    for r in rs:
        p = posted(r)
        assert time(19, 0) <= p.time() <= time(21, 0)
        quarter_end = date(p.year, ((p.month - 1) // 3 + 1) * 3, 1) + timedelta(days=31)
        assert p.date() == last_working_day(quarter_end.replace(day=1) - timedelta(days=1))


def u09(sid):
    rs = rows(sid)
    assert {r["Company Code"] for r in rs} == {"LVC", "LVE"}
    assert all(r["Counterparty Type"] == "group" for r in rs)
    # repayments only ever run LVE → LVC: no cycle back to the origin
    assert {(r["Payer"], r["Payee"]) for r in rs} == {
        (GROUP["LVE"]["legal_name"], GROUP["LVC"]["legal_name"])
    }


def u10(sid):
    (v,) = vendors(sid)
    (c,) = [CUSTOMERS[x] for x in SC[sid]["affected"]["customer_ids"]]
    assert v["vendor_name"] == c["customer_name"]
    kinds = Counter(r["Dr/Cr"] for r in rows(sid))
    assert kinds["Dr"] >= 5 and kinds["Cr"] >= 5
    assert not set(amounts([r for r in rows(sid) if r["Dr/Cr"] == "Dr"])) & set(
        amounts([r for r in rows(sid) if r["Dr/Cr"] == "Cr"])
    )


def u11(sid):
    cs = SC[sid]["affected"]["customer_ids"]
    assert len(cs) >= 10
    for c in cs:
        assert CUSTOMERS[c]["sells_to_entity"] == "LVC"
        assert any(r["Counterparty ID"] == c and r["Dr/Cr"] == "Cr" for r in LEDGER.values())


def u12(sid):
    rs = rows(sid)
    assert len(rs) == 2 * MONTHS and all(r["Payment Mode"] == "BULK" for r in rs)
    for r in rs:
        assert len(bank_matches(r)) == 1  # one bulk debit per company per month


def u13(sid):
    a, b = rows(sid)
    assert a["Payee"] == b["Payee"] and a["Amount"] == b["Amount"] and a["Invoice No"] != b["Invoice No"]
    assert posted(a).date() == posted(b).date()


def u14(sid):
    (v,) = vendors(sid)
    rs = sorted(rows(sid), key=posted)
    nums = [int(r["Invoice No"].split("/")[-1]) for r in rs]
    assert nums == list(range(nums[0], nums[0] + len(nums))) and rs == paid_to(v["vendor_id"])


def u15(sid):
    m02("S-M-02")
    assert SC[sid]["affected"]["vendor_ids"] == SC["S-M-02"]["affected"]["vendor_ids"]


def u16(sid):
    n04("S-N-04")
    assert SC[sid]["affected"]["vendor_ids"] == SC["S-N-04"]["affected"]["vendor_ids"]


def v01(sid):
    rs = rows(sid)
    assert sum("  " in r["Payee"] for r in rs) == 3
    assert sum("​" in r["Invoice No"] for r in rs) == 1
    assert sum(r["Description"] == "" for r in rs) == 1


def w01(sid):
    rs = rows(sid)
    raw_gl = Counter(
        r["GL Account"] for r in LEDGER.values() if r["Counterparty Type"] == "vendor"
    ).most_common()
    rtgs = [r for r in rs if r["Payment Mode"] == "RTGS" and minor(r["Amount"]) < 2_00_000_00]
    it = [r for r in rs if r not in rtgs and VENDORS[r["Counterparty ID"]]["category"] == "it_services"]
    assert len(it) == 2 and len(rtgs) == 2 and raw_gl
    it_gl = Counter(
        r["GL Account"]
        for r in paid_to(it[0]["Counterparty ID"])
        if r["Txn ID"] not in {x["Txn ID"] for x in it}
    )
    assert all(r["GL Account"] not in it_gl for r in it)  # booked to a GL the vendor otherwise never uses


def x01(sid):
    (v,) = vendors(sid)
    (e,) = employees(sid)
    rs = rows(sid)
    assert sum(1 for x in VENDORS.values() if x["city"] == v["city"]) == 1  # the only supplier in that city
    assert v["phone"] == e["phone"] and (posted(rs[0]).date() - onboarded(v)).days < 30
    assert len(rs) == 6 and span_days(rs) <= 18
    vals = sorted(amounts(rs))
    assert vals[-1] > 5 * median(vals[:-1])


CHECKS = {
    "S-A-01": a01,
    "S-A-02": a02,
    "S-B-01": b01,
    "S-B-02": b02,
    "S-C-01": c01,
    "S-C-02": c02,
    "S-D-01": d01,
    "S-D-02": d02,
    "S-E-01": e0x,
    "S-E-02": e0x,
    "S-F-01": f01,
    "S-G-01": g01,
    "S-G-02": g02,
    "S-H-01": h01,
    "S-H-02": h02,
    "S-H-03": h03,
    "S-I-01": i01,
    "S-J-01": j01,
    "S-J-02": j02,
    "S-K-01": k01,
    "S-K-02": k02,
    "S-K-03": k03,
    "S-L-01": l01,
    "S-M-01": m01,
    "S-M-02": m02,
    "S-N-01": n01,
    "S-N-02": n02,
    "S-N-03": n03,
    "S-N-04": n04,
    "S-N-05": n05,
    "S-O-01": o01,
    "S-O-02": o02,
    "S-P-01": p01,
    "S-P-02": p02,
    "S-Q-01": q01,
    "S-R-01": r01,
    "S-S-01": s01,
    "S-S-02": s02,
    "S-S-03": s03,
    "S-S-04": s04,
    "S-S-05": s05,
    "S-S-06": s06,
    "S-S-07": s07,
    "S-T-01": t01,
    "S-U-01": u01,
    "S-U-02": u02,
    "S-U-03": u03,
    "S-U-04": u04,
    "S-U-05": u05,
    "S-U-06": u06,
    "S-U-07": u07,
    "S-U-08": u08,
    "S-U-09": u09,
    "S-U-10": u10,
    "S-U-11": u11,
    "S-U-12": u12,
    "S-U-13": u13,
    "S-U-14": u14,
    "S-U-15": u15,
    "S-U-16": u16,
    "S-V-01": v01,
    "S-W-01": w01,
    "S-X-01": x01,
}


def test_every_ground_truth_scenario_has_a_data_check():
    assert set(CHECKS) == set(SC) and len(CHECKS) == GT["scenario_count"] == 63


@pytest.mark.parametrize("sid", sorted(SC))
def test_scenario_is_observable_in_the_generated_data(sid):
    s = SC[sid]
    a = s["affected"]
    assert sum(len(v) for v in a.values()) > 0, "a scenario must point at records"
    if s["start_date"]:
        dates = sorted(posted(r).date().isoformat() for r in rows(sid))
        assert (dates[0], dates[-1]) == (s["start_date"], s["end_date"])
    CHECKS[sid](sid)


def test_controls_never_share_rows_with_injected_scenarios():
    control_rows = {t for s in GT["scenarios"] if s["is_control"] for t in s["affected"]["ledger_txn_ids"]}
    for s in GT["scenarios"]:
        if not s["is_control"]:
            assert not control_rows & set(s["affected"]["ledger_txn_ids"]), s["scenario_id"]


def test_shared_rows_between_injected_scenarios_are_declared():
    """Non-isolated scenarios are declared in `also_part_of` (directly, or as siblings of one composite)."""
    groups = {s["scenario_id"]: {s["scenario_id"], *s["also_part_of"]} for s in GT["scenarios"]}
    for s in GT["scenarios"]:
        for other in s["also_part_of"]:
            groups[other] |= groups[s["scenario_id"]]
    owners: dict[str, set[str]] = {}
    for s in GT["scenarios"]:
        for t in s["affected"]["ledger_txn_ids"]:
            owners.setdefault(t, set()).add(s["scenario_id"])
    for t, ids in owners.items():
        for x in ids:
            assert ids <= groups[x], (t, ids)


def test_affected_entities_include_every_counterparty():
    kinds = {
        "vendor": "vendor_ids",
        "employee": "employee_ids",
        "customer": "customer_ids",
        "group": "group_entities",
    }
    for s in GT["scenarios"]:
        for r in rows(s["scenario_id"]):
            if r["Counterparty Type"] in kinds:
                assert r["Counterparty ID"] in s["affected"][kinds[r["Counterparty Type"]]], s["scenario_id"]


def test_master_attributes_are_shared_only_where_a_scenario_says_so():
    """Unique names and e-mails; a shared phone/address/bank account always belongs to a scenario."""
    names = Counter(v["vendor_name"] for v in VENDORS.values())
    assert max(names.values()) == 1
    emails = Counter(v["email"] for v in VENDORS.values() if v["email"])
    assert max(emails.values()) == 1
    explained = {frozenset(SC[s]["affected"]["vendor_ids"]) for s in ("S-N-04", "S-P-01")}
    for field in ("phone", "address", "bank_account"):
        groups: dict[str, set[str]] = {}
        for v in VENDORS.values():
            if v[field]:
                groups.setdefault(v[field], set()).add(v["vendor_id"])
        for ids in groups.values():
            if len(ids) > 1:
                assert frozenset(ids) in explained, (field, ids)


def test_posting_dates_are_within_the_period():
    first, last = datetime.combine(PERIOD[0], time()), datetime.combine(PERIOD[1] + timedelta(days=1), time())
    assert all(first <= posted(r) < last for r in LEDGER.values())
