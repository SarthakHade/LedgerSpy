"""Injected forensic scenarios (A–W, X) and normal controls (U), each with machine-readable ground truth.

Two phases: `register_entities(g)` creates the vendors/customers/relationships the scenarios need *before*
IDs are assigned (so IDs reveal nothing), and `inject(g)` adds or marks ledger rows after the normal
population exists.
Scenario membership is recorded only in `g.scenarios`; nothing about it is written into input files.

Timing uses fractions of the period (`_month(g, 0.55)`), so every scenario fits any 2–5 year range.
"""

import random
from datetime import date, datetime, time, timedelta

from ledgerspy.synth import entities as E
from ledgerspy.synth.context import Gen
from ledgerspy.synth.ledger import transfer
from ledgerspy.synth.model import LedgerRow, Relationship, Vendor
from ledgerspy.synth.names import CITIES, REMOTE_CITY, STEMS, core
from ledgerspy.synth.rng import lognormal_minor

P_RULES, P_ANOM, P_RISK, P_ER, P_GRAPH, P_RECON, P_INV = "P7", "P8", "P9", "P12", "P13", "P14", "P15"


def _month(g: Gen, frac: float) -> date:
    months = g.cal.months()
    return months[min(len(months) - 1, max(0, round(frac * (len(months) - 1))))]


def _day(g: Gen, r: random.Random, frac: float, run_day: bool = True) -> date:
    month = _month(g, frac)
    return g.cal.payment_day(r, month) if run_day else r.choice(g.cal.working_days(month))


class AmountIndex:
    """Keeps injected rows from accidentally sharing payee+amount within a week (that would be an unplanned
    P4 near-duplicate). Normal rows that collide are nudged later by the guard instead."""

    def __init__(self) -> None:
        self.seen: dict[tuple[str, int, str], list[date]] = {}

    def fresh(self, payee: str, amount: int, d: date, drcr: str = "Dr", step: int = 100) -> int:
        while any(
            abs((d - other).days) <= 7 for other in self.seen.get((payee.casefold(), amount, drcr), [])
        ):
            amount += step
        self.seen.setdefault((payee.casefold(), amount, drcr), []).append(d)
        return amount


def _claim_vendor(g: Gen, category: str, *, company: str | None = "LVC", min_rows: int = 6) -> Vendor:
    """A normal vendor of `category` not yet used by another scenario (keeps scenarios isolated)."""
    used = g.special.setdefault("claimed_vendors", set())
    for v in sorted(g.vendors, key=lambda v: v.vendor_id):
        prof = g.vendor_profile.get(v.name)
        if not prof or prof.get("special") or prof["category"] != category or v.name in used or prof["churn"]:
            continue
        if company and prof.get("company", "LVC") != company:
            continue
        rows = [row for row in g.rows if row.payee == v.name]
        if len(rows) >= min_rows and all(row.normal for row in rows):  # untouched by earlier scenarios
            used.add(v.name)
            return v
    raise RuntimeError(f"no free normal vendor in {category}")  # pragma: no cover


def _background(g: Gen):
    """Filter: normal rows whose counterparty no scenario or control uses (keeps late injections isolated)."""
    taken = {e for s in g.scenarios for e in (*s.vendor_ids, *s.employee_ids, *s.customer_ids)}
    reserved = g.special.setdefault("claimed_vendors", set()) | {
        name for name, prof in g.vendor_profile.items() if prof.get("special")
    }
    keys = {k for s in g.scenarios for k in s.ledger_keys}
    return lambda x: (
        x.normal
        and x.key not in keys
        and x.cp_id not in taken
        and x.payee not in reserved
        and x.payer not in reserved
    )


def _rows_of(g: Gen, payee: str) -> list[LedgerRow]:
    return sorted((r for r in g.rows if r.payee == payee and r.drcr == "Dr"), key=lambda r: (r.posted, r.key))


def _take(rows: list[LedgerRow], r: random.Random, lo: float = 0.2, hi: float = 0.8) -> LedgerRow:
    row = rows[int(len(rows) * r.uniform(lo, hi))]
    row.normal = False
    return row


def _median(values: list[int]) -> int:
    s = sorted(values)
    return s[len(s) // 2]


def lookalike(name: str, style: int) -> str:
    """A naturally confusable variant of a vendor name.

    style 0: doubled final letter of the first word ("Varnel" → "Varnell"); style 1: hyphenated first word
    ("Teravin" → "Tera-vin"). Both also flip the first plural trade word to singular ("Supplies" → "Supply",
    "Metals" → "Metal") and restyle the legal suffix ("Pvt Ltd" → "Pvt. Ltd.").
    """
    words = name.split()
    stem = words[0]
    if style == 0:
        words[0] = stem + stem[-1]
    else:
        cut = (len(stem) + 1) // 2
        words[0] = stem[:cut] + "-" + stem[cut:].lower()
    for i in range(1, len(words)):
        w = words[i]
        if w in ("Pvt", "Ltd", "LLP", "Private", "Limited", "Co", "&"):
            continue
        if w.endswith("ies"):
            words[i] = w[:-3] + "y"
            break
        if w.endswith("s") and len(w) > 4:
            words[i] = w[:-1]
            break
    text = " ".join(words)
    for a, b in (
        (" Pvt Ltd", " Pvt. Ltd."),
        (" Private Limited", " Pvt Ltd"),
        (" LLP", " L.L.P."),
        (" & Co", " and Co"),
    ):
        if text.endswith(a):
            return text[: -len(a)] + b
    return text


def _city_tuple(city: str) -> tuple[str, str]:
    return next((c for c in CITIES if c[0] == city), ("Veltara", "MH"))


# =============================================================================================== entities


def register_entities(g: Gen) -> None:
    r = g.rng("scenario-entities")
    n = g.names
    s, end = g.p.start, g.p.end
    span = (end - s).days

    def at(frac: float) -> date:
        return s + timedelta(days=round(frac * span))

    sp = g.special
    # B: a facilities vendor paid only round amounts
    sp["round_vendor"] = E.new_vendor(g, "facilities", onboarded=s - timedelta(days=420))
    # E: look-alikes of two long-standing vendors (created now so their IDs follow onboarding order)
    used = sp.setdefault("claimed_vendors", set())
    sp["lookalikes"] = []
    for sid, category, style, frac in (
        ("S-E-01", "raw_materials", 0, 0.52),
        ("S-E-02", "packaging", 1, 0.64),
    ):
        orig = next(
            v
            for v in sorted(g.vendors, key=lambda v: (v.onboarding_date, v.name))
            if g.vendor_profile.get(v.name, {}).get("category") == category
            and not g.vendor_profile[v.name]["churn"]
            and v.name not in used
            and core(lookalike(v.name, style)) not in n.cores  # the twin must differ beyond punctuation
        )
        used.add(orig.name)
        twin = E.new_vendor(
            g,
            category,
            name=lookalike(orig.name, style),
            onboarded=at(frac - 0.03),
            created_by=g.roles["buyer_1"],
            city=_city_tuple(orig.city),
        )
        sp["lookalikes"].append((sid, orig, twin, frac))
    # F: ghost vendor, created by a buyer, with the buyer's home address and bank account, little master data
    buyer = g.roles["buyer_2"]
    ghost = E.new_vendor(g, "professional", onboarded=at(0.28), created_by=buyer, terms=7)
    ghost.address, ghost.bank_account, ghost.bank_code = (
        buyer.home_address,
        buyer.bank_account,
        buyer.bank_code,
    )
    ghost.tax_id = ghost.phone = ghost.email = ""
    ghost.city = buyer.location
    sp["ghost"] = ghost
    # G: sudden new vendor with large payments; U: legitimate new vendors
    sp["sudden_vendor"] = E.new_vendor(g, "raw_materials", onboarded=at(0.61), created_by=g.roles["buyer_1"])
    sp["legit_new_vendor"] = E.new_vendor(g, "logistics", onboarded=at(0.38))
    sp["capex_vendor"] = E.new_vendor(
        g,
        "maintenance",
        name=n.free_stem([r.choice(["Arkholm", "Celdon"])], "{} Machine Tools Pvt Ltd")
        + " Machine Tools Pvt Ltd",
        onboarded=at(0.42),
        terms=30,
    )
    # H: a vendor that goes dormant for months and then suddenly spikes
    sp["dormant_vendor"] = E.new_vendor(g, "maintenance", onboarded=s - timedelta(days=900))
    # L: test-then-exploit vendor
    sp["sequence_vendor"] = E.new_vendor(g, "maintenance", onboarded=at(0.70), created_by=g.roles["buyer_1"])
    # M: suspicious — AP executive's own bank account on a vendor; legitimate — declared relative's firm
    clerk = g.roles["ap_clerk_1"]
    m_bad = E.new_vendor(g, "it_services", onboarded=at(0.22), created_by=g.roles["buyer_1"])
    m_bad.bank_account, m_bad.bank_code = clerk.bank_account, clerk.bank_code
    sp["m_bad"] = m_bad
    head = g.roles["procurement_head"]
    m_ok = E.new_vendor(g, "maintenance", onboarded=s - timedelta(days=1500), created_by=g.roles["cfo"])
    m_ok.address = head.home_address
    sp["m_ok"] = m_ok
    g.vendor_profile[m_ok.name] = {
        "median": 4_200_000,
        "sigma": 0.4,
        "rate": 0.7,
        "season": {},
        "category": "maintenance",
        "churn": None,
        "special": True,
    }
    g.relationships.append(
        Relationship(
            "",
            m_ok.name,
            head.name,
            "declared_conflict_of_interest",
            s - timedelta(days=1400),
            "Proprietor is the spouse of the employee; employee excluded from approving this vendor",
        )
    )
    # N: suspicious shared phone; legitimate group vendors sharing phone/address/bank (declared)
    supervisor = next(e for e in g.employees if e.role == "Shift Supervisor")
    n_bad = E.new_vendor(g, "facilities", onboarded=at(0.46))
    n_bad.phone = supervisor.phone
    sp["n_bad"], sp["n_bad_employee"] = n_bad, supervisor
    stem = n.free_stem(["Kesrin"], "{} Freight Carriers Pvt Ltd", "{} Roadways LLP")
    g1 = E.new_vendor(
        g, "logistics", name=f"{stem} Freight Carriers Pvt Ltd", onboarded=s - timedelta(days=2000)
    )
    g2 = E.new_vendor(g, "logistics", name=f"{stem} Roadways LLP", onboarded=s - timedelta(days=1700))
    g2.phone, g2.address, g2.bank_account, g2.bank_code = g1.phone, g1.address, g1.bank_account, g1.bank_code
    for v in (g1, g2):
        g.vendor_profile[v.name] = {
            "median": 3_100_000,
            "sigma": 0.5,
            "rate": 2.0,
            "season": {10: 1.2},
            "category": "logistics",
            "churn": None,
            "company": "LVC",
            "special": True,
        }
    g.relationships.append(
        Relationship(
            "",
            g1.name,
            g2.name,
            "same_corporate_group",
            s - timedelta(days=1690),
            "Sister concerns sharing office and collection account",
        )
    )
    sp["group_vendors"] = (g1, g2)
    # U: legitimately similar names, unrelated businesses in different trades
    sim_stem = n.free_stem(["Delvane"], "{} Metals Pvt Ltd", "{} Electricals LLP")
    u1 = E.new_vendor(
        g, "raw_materials", name=f"{sim_stem} Metals Pvt Ltd", onboarded=s - timedelta(days=2400)
    )
    u2 = E.new_vendor(
        g,
        "maintenance",
        name=f"{sim_stem} Electricals LLP",
        onboarded=s - timedelta(days=900),
        city=next(c for c in CITIES if c[0] != u1.city and c != REMOTE_CITY),
    )
    for v, cat, med in ((u1, "raw_materials", 15_000_000), (u2, "maintenance", 3_800_000)):
        g.vendor_profile[v.name] = {
            "median": med,
            "sigma": 0.4,
            "rate": 1.2,
            "season": {},
            "category": cat,
            "churn": None,
            "company": "LVC",
            "special": True,
        }
    sp["similar_names"] = (u1, u2)
    # O: circular trading counterparties (each is both a vendor and a customer of the group)
    for key, frac, entity in (("ring1", 0.40, "LVE"), ("ring2a", 0.70, "LVE"), ("ring2b", 0.70, "LVC")):
        v = E.new_vendor(g, "trading", onboarded=at(frac) - timedelta(days=60))
        c = E.new_customer(g, entity, name=v.name, onboarded=at(frac) - timedelta(days=55), city=None)
        c.address, c.tax_id, c.phone = v.address, v.tax_id, v.phone
        sp[key] = (v, c)
    # U: legitimate dual role — a maintenance vendor that also buys scrap
    scrap_v = E.new_vendor(
        g,
        "maintenance",
        name=f"{n.free_stem(['Brennick'], '{} Scrap & Metals Pvt Ltd')} Scrap & Metals Pvt Ltd",
        onboarded=s - timedelta(days=1300),
    )
    scrap_c = E.new_customer(g, "LVC", name=scrap_v.name, onboarded=s - timedelta(days=1200))
    scrap_c.address, scrap_c.tax_id, scrap_c.phone = scrap_v.address, scrap_v.tax_id, scrap_v.phone
    g.vendor_profile[scrap_v.name] = {
        "median": 2_600_000,
        "sigma": 0.45,
        "rate": 0.8,
        "season": {},
        "category": "maintenance",
        "churn": None,
        "company": "LVC",
        "special": True,
    }
    sp["dual_role"] = (scrap_v, scrap_c)

    # P: shell vendors sharing one bank account; burst of new vendors paid by LVE
    def shell_set(stem: str) -> list[str]:  # one firm behind three variants of a name
        return [f"{stem} Supply Co", f"{stem[:-1]} Supplies", f"{stem[0]}{stem[-4].upper()} Supply House"]

    shell_names = next(
        names
        for names in map(shell_set, ["Brightwell", *STEMS])
        if not {core(x) for x in names} & n.cores and len({core(x) for x in names}) == 3
    )
    shells = [
        E.new_vendor(
            g,
            "office_supplies",
            name=nm,
            onboarded=at(0.74) + timedelta(days=9 * i),
            created_by=g.roles["buyer_1"],
        )
        for i, nm in enumerate(shell_names)
    ]
    for v in shells[1:]:
        v.bank_account, v.bank_code = shells[0].bank_account, shells[0].bank_code
    sp["shells"] = shells
    burst_day = at(0.83)
    sp["burst"] = [
        E.new_vendor(
            g,
            r.choice(["logistics", "packaging", "maintenance"]),
            onboarded=burst_day,
            created_by=g.roles["buyer_1"],
        )
        for _ in range(7)
    ]
    # Q: gradual escalation; T: digit-distribution subgroup; U: rent, retainers, canteen, two-meter utility
    sp["escalation_vendor"] = E.new_vendor(g, "maintenance", onboarded=s - timedelta(days=700))
    sp["benford_vendor"] = E.new_vendor(g, "facilities", onboarded=s - timedelta(days=300))
    sp["landlord"] = E.new_vendor(
        g,
        "facilities",
        name=f"{n.free_stem([r.choice(['Glenmora', 'Hessarin'])], '{} Estates Pvt Ltd')} Estates Pvt Ltd",
        onboarded=s - timedelta(days=3000),
        terms=0,
    )
    sp["retainers"] = [E.new_vendor(g, "professional", onboarded=s - timedelta(days=1800)) for _ in range(2)]
    sp["canteen"] = E.new_vendor(
        g,
        "facilities",
        name=f"{n.free_stem([r.choice(['Falcrest', 'Dunmore'])], '{} Canteen Services')} Canteen Services",
        onboarded=s - timedelta(days=1000),
    )
    sp["two_meter_utility"] = E.new_vendor(g, "utilities", onboarded=s - timedelta(days=2500), terms=10)
    sp["seasonal_vendor"] = E.new_vendor(g, "raw_materials", onboarded=s - timedelta(days=2600))
    # X: combined case — new local-services vendor in a city with no other suppliers, shares an employee phone
    sales = next(e for e in g.employees if e.role == "Sales Executive")
    combined = E.new_vendor(
        g, "facilities", onboarded=at(0.88), created_by=g.roles["buyer_1"], city=REMOTE_CITY
    )
    combined.phone = sales.phone
    sp["combined"], sp["combined_employee"] = combined, sales
    others = [x for x in (*g.vendors, *g.customers, *g.employees) if x is not combined]
    if any(REMOTE_CITY[0] in (getattr(x, "city", ""), getattr(x, "location", "")) for x in others):
        raise RuntimeError("the remote city must hold only the S-X-01 vendor")  # pragma: no cover


# =============================================================================================== inject


def inject(g: Gen) -> None:
    idx = AmountIndex()
    r = g.rng("scenarios")
    # vendors for the two late injectors are reserved first, before any background row is touched
    g.special["late_vendors"] = {
        "it_gl": _claim_vendor(g, "it_services"),
        "batch": _claim_vendor(g, "logistics", min_rows=10),
    }
    for fn in (
        _a_amount,
        _b_round,
        _c_duplicate,
        _d_near_duplicate,
        _e_lookalike,
        _f_ghost,
        _g_sudden,
        _h_velocity,
        _i_first_high,
        _j_splitting,
        _k_temporal,
        _h_dormant,
        _k_period_end,
        _l_sequence,
        _m_relationship,
        _n_shared,
        _o_circular,
        _p_concentration,
        _q_escalation,
        _r_silent,
        _t_benford,
        _v_data_quality,
        _x_combined,
        _u_controls,
        _w_inconsistent,  # these two pick arbitrary background rows, so they run last and skip
        _s_bank,  # every row and entity another scenario or control already uses
    ):
        fn(g, r, idx)


def _pay(g: Gen, idx: AmountIndex, v: Vendor, when: datetime, amount: int, text: str, **kw) -> LedgerRow:
    gl = kw.pop("gl", E.PROFILES.get(v.category, (("5900", "Trading Purchases"),))[0])
    amount = idx.fresh(v.name, amount, when.date())
    return g.payment(
        company=kw.pop("company", "LVC"),
        vendor=v,
        posted=when,
        amount=amount,
        gl=gl,
        description=text,
        normal=False,
        **kw,
    )


def _a_amount(g, r, idx):
    v = _claim_vendor(g, "packaging")
    hist = _rows_of(g, v.name)
    base = _median([x.amount for x in hist])
    day = _day(g, r, 0.55)
    row = _pay(g, idx, v, g.cal.business_time(r, day), round(base * 9.2), "Corrugated cartons")
    g.scenario(
        scenario_id="S-A-01",
        scenario_type="amount_anomaly",
        letter="A",
        severity="high",
        is_control=False,
        description="One payment about 9x the vendor's own median, otherwise routine (same description, "
        "normal posting time and approver).",
        targets=["robust z of amount within vendor (peer/behavioural deviation)", "risk fusion"],
        phases=[P_ANOM, P_RISK],
        ledger_keys=[row.key],
        vendor_ids=[v.vendor_id],
        parameters={"multiplier": 9.2, "vendor_median_minor": base, "history_rows": len(hist)},
        evidence=[f"amount {row.amount} vs vendor median {base}"],
    )


def _b_round(g, r, idx):
    v = g.special["round_vendor"]
    amounts = [49_000, 75_000, 1_00_000, 50_000, 1_25_000, 60_000, 90_000, 80_000, 1_10_000]
    rows, day = [], _day(g, r, 0.05)
    for i in range(18):
        rows.append(
            _pay(
                g,
                idx,
                v,
                g.cal.business_time(r, day),
                amounts[i % len(amounts)] * 100,
                r.choice(["Facility upkeep", "Annual maintenance contract", "Site services"]),
            )
        )
        day = g.cal.next_working(day + timedelta(days=r.randint(22, 48)))
        if day > g.p.end - timedelta(days=5):
            break
    g.scenario(
        scenario_id="S-B-01",
        scenario_type="round_number",
        letter="B",
        severity="medium",
        is_control=False,
        description="Every payment to this vendor is an exact multiple of ₹1,000; its category peers pay "
        "paise-level amounts.",
        targets=[
            "round-amount proportion per payee vs population (binomial test)",
            "Benford last-two-digits",
        ],
        phases=[P_RULES, P_RISK],
        ledger_keys=[x.key for x in rows],
        vendor_ids=[v.vendor_id],
        parameters={"amounts_rupees": amounts, "payments": len(rows)},
        evidence=["all amounts end in 000.00"],
    )


def _c_duplicate(g, r, idx):
    v1 = _claim_vendor(g, "logistics")
    orig = _take(_rows_of(g, v1.name), r)
    copy = LedgerRow(**{**orig.__dict__, "key": g.next_key(), "normal": False, "bank_info": {}})
    g.rows.append(copy)
    g.allowed_pairs.add(frozenset((orig.key, copy.key)))
    s1 = g.scenario(
        scenario_id="S-C-01",
        scenario_type="duplicate_transaction",
        letter="C",
        severity="high",
        is_control=False,
        description="The same payment posted twice at the same moment (every field identical).",
        targets=["exact duplicate (P4 integrity, P7 duplicates rule)"],
        phases=["P4", P_RULES],
        ledger_keys=[orig.key, copy.key],
        vendor_ids=[v1.vendor_id],
        stages={"duplicate": [copy.key]},
        parameters={"mode": "double posting"},
        evidence=["identical posting time, amount, invoice"],
    )
    g.expect("ledger", "exact_duplicate", copy, s1)
    g.expect("ledger", "duplicate_source_id", copy, s1)
    v2 = _claim_vendor(g, "raw_materials")
    orig2 = _take(_rows_of(g, v2.name), r)
    later = g.cal.next_working(orig2.posted.date() + timedelta(days=9))
    dup = g.payment(
        company=orig2.company,
        vendor=v2,
        posted=g.cal.business_time(r, later),
        amount=orig2.amount,
        gl=(orig2.gl, orig2.gl_name),
        description=orig2.description,
        reference=orig2.reference,
        doc_date=orig2.doc_date,
        normal=False,
    )
    days = (later - orig2.posted.date()).days  # 9 calendar days, moved to a working day
    s2 = g.scenario(
        scenario_id="S-C-02",
        scenario_type="duplicate_transaction",
        letter="C",
        severity="high",
        is_control=False,
        description=f"The same invoice paid again {days} days later (same vendor, invoice number, amount).",
        targets=["duplicate invoice payment (P7 duplicates rule)", "duplicate document id (P4)"],
        phases=["P4", P_RULES],
        ledger_keys=[orig2.key, dup.key],
        vendor_ids=[v2.vendor_id],
        stages={"duplicate": [dup.key]},
        parameters={"days_later": days},
        evidence=[f"invoice {orig2.reference} paid twice"],
    )
    g.expect("ledger", "duplicate_source_id", dup, s2)


def _d_near_duplicate(g, r, idx):
    v = _claim_vendor(g, "raw_materials")
    rows = _rows_of(g, v.name)
    # an original whose next calendar day is a working day, so the copy follows within ~24 hours
    candidates = [x for x in rows if x.normal and g.cal.is_working(x.posted.date() + timedelta(days=1))]
    orig = _take(candidates, r, 0.2, 0.45)
    later = orig.posted.date() + timedelta(days=1)
    near = g.payment(
        company=orig.company,
        vendor=v,
        posted=g.cal.business_time(r, later),
        amount=orig.amount,
        gl=(orig.gl, orig.gl_name),
        description=orig.description,
        reference=orig.reference + "A",
        doc_date=orig.doc_date,
        normal=False,
    )
    g.allowed_pairs.add(frozenset((orig.key, near.key)))
    s1 = g.scenario(
        scenario_id="S-D-01",
        scenario_type="near_duplicate",
        letter="D",
        severity="high",
        is_control=False,
        description="Same vendor and amount the next day with the invoice number suffixed 'A'.",
        targets=["near duplicate (P4 integrity, P7 duplicates with reference similarity)"],
        phases=["P4", P_RULES],
        ledger_keys=[orig.key, near.key],
        vendor_ids=[v.vendor_id],
        stages={"duplicate": [near.key]},
        parameters={"days_later": 1, "reference_change": "+A"},
        evidence=[f"{orig.reference} vs {near.reference}"],
    )
    g.expect("ledger", "near_duplicate", near, s1)
    orig2 = _take(rows, r, 0.6, 0.9)
    digits = f"{orig2.amount:d}"
    swapped = orig2.amount + 9_000  # only if every rupee digit is the same
    for i in range(len(digits) - 4, 0, -1):  # transpose the lowest pair of different adjacent rupee digits
        if digits[i] != digits[i - 1]:
            swapped = int(digits[: i - 1] + digits[i] + digits[i - 1] + digits[i + 1 :])
            break
    existing = {x.reference for x in g.rows}
    ref = orig2.reference + "-1"
    base = orig2.reference
    for i in range(
        len(base) - 1, 1, -1
    ):  # typo-style: transpose two adjacent digits, never onto a real invoice
        cand = base[: i - 1] + base[i] + base[i - 1] + base[i + 1 :]
        if cand != base and cand not in existing and base[i].isdigit() and base[i - 1].isdigit():
            ref = cand
            break
    later2 = g.cal.next_working(orig2.posted.date() + timedelta(days=1))
    near2 = _pay(
        g,
        idx,
        v,
        g.cal.business_time(r, later2),
        swapped,
        orig2.description,
        reference=ref,
        doc_date=orig2.doc_date,
        company=orig2.company,
        gl=(orig2.gl, orig2.gl_name),
    )
    g.scenario(
        scenario_id="S-D-02",
        scenario_type="near_duplicate",
        letter="D",
        severity="medium",
        is_control=False,
        description="Next-day payment to the same vendor with transposed digits in both amount and invoice "
        "number (a typo-style duplicate that exact matching misses).",
        targets=["fuzzy duplicate (amount/reference edit distance, P7)"],
        phases=[P_RULES],
        ledger_keys=[orig2.key, near2.key],
        vendor_ids=[v.vendor_id],
        stages={"duplicate": [near2.key]},
        parameters={"original_amount_minor": orig2.amount, "duplicate_amount_minor": near2.amount},
        evidence=[f"{orig2.reference} vs {ref}"],
    )


def _e_lookalike(g, r, idx):
    for sid, orig, twin, frac in g.special["lookalikes"]:
        name = twin.name
        hist = _rows_of(g, orig.name)
        rows = []
        day = max(_day(g, r, frac), g.cal.next_working(twin.onboarding_date + timedelta(days=10)))
        for _ in range(5):
            src = r.choice(hist)
            rows.append(
                _pay(
                    g,
                    idx,
                    twin,
                    g.cal.business_time(r, day),
                    lognormal_minor(r, src.amount, 0.15),
                    src.description,
                    gl=(src.gl, src.gl_name),
                    company=src.company,
                )
            )
            day = g.cal.next_working(day + timedelta(days=r.randint(15, 30)))
        g.scenario(
            scenario_id=sid,
            scenario_type="lookalike_vendor",
            letter="E",
            severity="high",
            is_control=False,
            description=f"New vendor '{name}' imitates long-standing '{orig.name}' (same trade and city, "
            "different bank account), and is paid for the same kind of goods.",
            targets=["fuzzy entity matching / similarity matrix (P12)", "new-vendor rules (P7)"],
            phases=[P_ER, P_RULES, P_RISK],
            ledger_keys=[x.key for x in rows],
            vendor_ids=[orig.vendor_id, twin.vendor_id],
            parameters={"original": orig.name, "variant": name},
            evidence=["high name similarity", "different bank account", "onboarded recently"],
        )


def _f_ghost(g, r, idx):
    v, buyer = g.special["ghost"], g.roles["buyer_2"]
    near_limit = [0.925, 0.95, 0.975, 0.94, 0.965, 0.955]
    rows, day = [], g.cal.next_working(v.onboarding_date + timedelta(days=12))
    for i, f in enumerate(near_limit):
        amount = round(g.p.vendor_limit * f / 100_000) * 100_000  # round thousands just below the CFO limit
        rows.append(
            _pay(
                g,
                idx,
                v,
                g.cal.business_time(r, day),
                amount,
                "Business consultancy services",
                reference=f"{v.invoice_prefix}/{i + 1:03d}",
                gl=E.PROFILES["professional"][0],
            )
        )
        day = g.cal.next_working(day + timedelta(days=r.randint(30, 45)))
    g.scenario(
        scenario_id="S-F-01",
        scenario_type="ghost_vendor",
        letter="F",
        severity="critical",
        is_control=False,
        description="A consultancy with no tax ID, phone or e-mail, registered at the home address and paid "
        "into the bank account of the buyer who created it; sequential invoice numbers; round "
        "amounts just under the CFO approval limit.",
        targets=[
            "entity resolution vendor↔employee attributes (P12)",
            "shell/ghost indicators (P13)",
            "sequential invoices and round amounts (P7)",
            "threshold proximity (P7)",
        ],
        phases=[P_RULES, P_ER, P_GRAPH, P_RISK],
        ledger_keys=[x.key for x in rows],
        vendor_ids=[v.vendor_id],
        employee_ids=[buyer.employee_id],
        parameters={"fractions_of_limit": near_limit, "limit_minor": g.p.vendor_limit},
        evidence=[
            "missing tax ID/phone/e-mail",
            "address = employee home address",
            "bank account = employee salary account",
            "created by the same employee",
            "invoice numbers 001, 002, …",
            "amounts are round and just below the approval limit",
        ],
    )


def _g_sudden(g, r, idx):
    v = g.special["sudden_vendor"]
    day = g.cal.next_working(v.onboarding_date + timedelta(days=6))
    rows, multipliers = [], (3.8, 4.1, 3.5)
    category_median = _median(
        [x.amount for x in g.rows if g.vendor_profile.get(x.payee, {}).get("category") == "raw_materials"]
    )
    for m in multipliers:
        amount = round(category_median * m / 10_000_00) * 10_000_00 + r.randint(0, 99_99)
        rows.append(_pay(g, idx, v, g.cal.business_time(r, day), amount, "Supply of steel coils"))
        day = g.cal.next_working(day + timedelta(days=r.randint(6, 9)))
    g.scenario(
        scenario_id="S-G-01",
        scenario_type="sudden_vendor_appearance",
        letter="G",
        severity="high",
        is_control=False,
        description="Vendor onboarded and paid three amounts ~4x the category median within 25 days.",
        targets=["new vendor + large amount rule (P7)", "vendor age at transaction (P6)"],
        phases=[P_RULES, P_RISK],
        ledger_keys=[x.key for x in rows],
        vendor_ids=[v.vendor_id],
        parameters={
            "days_to_first_payment": (rows[0].posted.date() - v.onboarding_date).days,
            "category_median_minor": category_median,
            "multipliers": list(multipliers),
        },
        evidence=["first payment within days of onboarding", "amounts far above category median"],
    )


def _h_velocity(g, r, idx):
    def busiest_fortnight(vendor: Vendor) -> int:
        days = sorted(x.posted for x in _rows_of(g, vendor.name))
        return max((sum(1 for q in days if p <= q < p + timedelta(days=16)) for p in days), default=0)

    v = _claim_vendor(g, "it_services")
    while busiest_fortnight(v) > 4:  # its own history must never look like the burst
        v = _claim_vendor(g, "it_services")
    prof = g.vendor_profile[v.name]
    day = _day(g, r, 0.68)
    window = [d for d in (day + timedelta(days=i) for i in range(15)) if g.cal.is_working(d)]
    rows = []
    for d in sorted(r.sample(window, 9)):  # nine payments within two weeks
        rows.append(
            _pay(
                g,
                idx,
                v,
                g.cal.business_time(r, d),
                lognormal_minor(r, prof["median"], prof["sigma"]),
                r.choice(["Network support", "Cloud hosting", "Software subscription"]),
            )
        )
    g.scenario(
        scenario_id="S-H-01",
        scenario_type="velocity_spike",
        letter="H",
        severity="medium",
        is_control=False,
        description="A vendor paid about once a month receives nine payments in about two weeks, each of "
        "an ordinary amount.",
        targets=["rolling 7/30-day count vs own history (P6/P8)"],
        phases=["P6", P_ANOM, P_RISK],
        ledger_keys=[x.key for x in rows],
        vendor_ids=[v.vendor_id],
        parameters={"payments": 9, "usual_rate_per_month": round(prof["rate"], 2)},
        evidence=["burst of payments vs monthly cadence"],
    )


def _h_dormant(g, r, idx):
    v = g.special["dormant_vendor"]
    months = g.cal.months()
    stages: dict[str, list[int]] = {"active": [], "reactivated": []}
    for month in months[: round(0.3 * len(months))]:
        when = g.cal.business_time(r, g.cal.payment_day(r, month))
        row = _pay(g, idx, v, when, lognormal_minor(r, 38_000_00, 0.3), "Electrical maintenance")
        stages["active"].append(row.key)
    day = _day(g, r, 0.74)
    by_key = {x.key: x for x in g.rows}
    level = _median([by_key[k].amount for k in stages["active"]])
    for _ in range(6):
        row = _pay(
            g,
            idx,
            v,
            g.cal.business_time(r, day),
            round(level * r.uniform(2.8, 3.4)),
            "Electrical maintenance",
        )
        stages["reactivated"].append(row.key)
        day = g.cal.next_working(day + timedelta(days=r.randint(2, 4)))
    by_key = {x.key: x for x in g.rows}
    gap = (by_key[stages["reactivated"][0]].posted - by_key[stages["active"][-1]].posted).days
    g.scenario(
        scenario_id="S-H-03",
        scenario_type="dormant_vendor_reactivation",
        letter="H",
        severity="high",
        is_control=False,
        description=f"A vendor paid monthly for {len(stages['active'])} months, then silent for {gap // 30} "
        "months, suddenly receives six payments of about 3x its old level within three weeks.",
        targets=["dormant-then-active vendor (P7 suspicious sequences)", "velocity vs own history (P6/P8)"],
        phases=["P6", P_RULES, P_ANOM, P_RISK],
        ledger_keys=stages["active"] + stages["reactivated"],
        vendor_ids=[v.vendor_id],
        stages=stages,
        parameters={"active_until_fraction": 0.3, "reactivated_at_fraction": 0.74, "silent_days": gap},
        evidence=[f"no payments for {gap} days", "6 payments in ~3 weeks after reactivation"],
    )


def _k_period_end(g, r, idx):
    v = _claim_vendor(g, "office_supplies")
    poster = g.roles["ap_clerk_2"]
    year_ends = [m for m in g.cal.months() if m.month == 3]
    day = g.cal.last_working_day(year_ends[min(1, len(year_ends) - 1)])
    base = _median([x.amount for x in _rows_of(g, v.name)])
    rows = []
    for i in range(5):
        when = datetime.combine(day, time(21, 35 + 6 * i))
        rows.append(
            _pay(
                g,
                idx,
                v,
                when,
                round(base * r.uniform(3.5, 5.0)),
                "Stationery",
                posted_by=poster.employee_id,
                approved_by=g.roles["ap_manager"].employee_id,
            )
        )
    g.scenario(
        scenario_id="S-K-03",
        scenario_type="period_end_clustering",
        letter="K",
        severity="medium",
        is_control=False,
        description="Five unusually large payments to a small supplier posted after 21:30 on the last "
        "working day of "
        "the financial year by one AP user (contrast: S-U-08, routine quarter-end work by Finance).",
        targets=["period-end clustering and after-hours postings (P7)"],
        phases=[P_RULES, P_RISK],
        ledger_keys=[x.key for x in rows],
        vendor_ids=[v.vendor_id],
        employee_ids=[poster.employee_id],
        parameters={"date": day.isoformat(), "amount_multiplier_range": [3.5, 5.0]},
        evidence=["posted 21:35–21:59 on the year-end working day", "3.5–5x the supplier's median payment"],
    )


def _i_first_high(g, r, idx):
    e = next(
        x for x in g.employees if x.role == "Sales Executive" and x is not g.special["combined_employee"]
    )
    g.special["first_high_employee"] = e
    day = _day(g, r, 0.53, run_day=False)
    row = g.claim(
        employee=e,
        posted=g.cal.business_time(r, day),
        amount=46_500_00 + r.randint(0, 99),
        description="Conference registration and travel",
        gl=E.GL_TRAVEL,
        normal=False,
    )
    g.scenario(
        scenario_id="S-I-01",
        scenario_type="first_time_high_value",
        letter="I",
        severity="medium",
        is_control=False,
        description="An employee whose claims are usually ₹1–3k files a ₹46.5k claim (correctly approved).",
        targets=["first-time high value vs personal history (P6/P8)"],
        phases=["P6", P_ANOM, P_RISK],
        ledger_keys=[row.key],
        employee_ids=[e.employee_id],
        parameters={"amount_minor": row.amount},
        evidence=["largest claim by far for this employee"],
    )


def _j_splitting(g, r, idx):
    e = next(x for x in g.employees if x.role == "Production Engineer")
    day = _day(g, r, 0.44, run_day=False)
    lim = g.p.expense_limit
    rows = []
    for i, f in enumerate((0.98, 0.99, 0.996)):
        when = datetime.combine(day, time(11 + i, 10 + 7 * i))
        rows.append(
            g.claim(
                employee=e,
                posted=when,
                amount=round(lim * f),
                description="Purchase of safety gear",
                gl=E.GL_WELFARE,
                normal=False,
            )
        )
    g.scenario(
        scenario_id="S-J-01",
        scenario_type="threshold_splitting",
        letter="J",
        severity="high",
        is_control=False,
        description="One purchase claimed as three same-day claims just under the expense approval limit.",
        targets=["split payments under an approval limit (P7, limit from company policy/config)"],
        phases=[P_RULES, P_RISK],
        ledger_keys=[x.key for x in rows],
        employee_ids=[e.employee_id],
        parameters={"limit_minor": lim, "fractions": [0.98, 0.99, 0.996]},
        evidence=["3 claims in one day, each 98–99.6% of the limit, total above the limit"],
    )
    v = _claim_vendor(g, "maintenance")
    day = _day(g, r, 0.79)
    lim, rows = g.p.vendor_limit, []
    for f in (0.98, 0.9925, 0.996):
        rows.append(
            _pay(
                g,
                idx,
                v,
                g.cal.business_time(r, day),
                round(lim * f),
                "Machine repair",
                approved_by=g.roles["ap_manager"].employee_id,
            )
        )
        day = g.cal.next_working(day + timedelta(days=1))
    g.scenario(
        scenario_id="S-J-02",
        scenario_type="threshold_splitting",
        letter="J",
        severity="high",
        is_control=False,
        description="Three payments to one vendor on consecutive working days, each just below the CFO "
        "approval limit, all approved by the AP manager.",
        targets=["split payments under an approval limit (P7)"],
        phases=[P_RULES, P_RISK],
        ledger_keys=[x.key for x in rows],
        vendor_ids=[v.vendor_id],
        employee_ids=[g.roles["ap_manager"].employee_id],
        parameters={"limit_minor": lim, "fractions": [0.98, 0.9925, 0.996]},
        evidence=["sum exceeds the limit", "approver below CFO level"],
    )


def _k_temporal(g, r, idx):
    v = _claim_vendor(g, "facilities")
    poster = g.roles["ap_clerk_2"]
    prof = g.vendor_profile[v.name]
    base = _month(g, 0.58)
    sunday = base + timedelta(days=(6 - base.weekday()) % 7)
    holiday = next((d for d in sorted(g.cal.holidays) if d >= base), None) or sunday
    whens = [
        datetime.combine(base + timedelta(days=2), time(23, 42)),
        datetime.combine(base + timedelta(days=9), time(1, 15)),
        datetime.combine(sunday, time(11, 5)),
        datetime.combine(sunday + timedelta(days=7), time(16, 40)),
        datetime.combine(holiday, time(10, 30)),
        datetime.combine(base + timedelta(days=20), time(22, 55)),
    ]
    rows = [
        _pay(
            g,
            idx,
            v,
            w,
            lognormal_minor(r, prof["median"], prof["sigma"]),
            "Security services",
            posted_by=poster.employee_id,
        )
        for w in whens
    ]
    g.scenario(
        scenario_id="S-K-01",
        scenario_type="temporal_anomaly",
        letter="K",
        severity="medium",
        is_control=False,
        description="Six payments posted late at night, on Sundays and on a company holiday by one AP user.",
        targets=["after-hours / weekend / holiday postings vs company calendar (P7)"],
        phases=[P_RULES, P_RISK],
        ledger_keys=[x.key for x in rows],
        vendor_ids=[v.vendor_id],
        employee_ids=[poster.employee_id],
        parameters={"posting_times": [w.isoformat() for w in whens]},
        evidence=["outside 09:30–18:30 Mon–Sat or on a holiday"],
    )


def _l_sequence(g, r, idx):
    v = g.special["sequence_vendor"]
    day = g.cal.next_working(v.onboarding_date + timedelta(days=10))
    rows = []
    for rupees in (8_500, 9_200, 11_000, 3_85_000):
        rows.append(_pay(g, idx, v, g.cal.business_time(r, day), rupees * 100, "Compressor servicing"))
        day = g.cal.next_working(day + timedelta(days=14))
    g.scenario(
        scenario_id="S-L-01",
        scenario_type="sequence_pattern",
        letter="L",
        severity="high",
        is_control=False,
        description="New vendor: three small payments two weeks apart, then a large one (test-then-exploit).",
        targets=["escalating sequence for a new vendor (P7 suspicious sequences)"],
        phases=[P_RULES, P_RISK],
        ledger_keys=[x.key for x in rows],
        vendor_ids=[v.vendor_id],
        parameters={"amounts_rupees": [8_500, 9_200, 11_000, 3_85_000]},
        evidence=["small → small → small → large within 7 weeks of onboarding"],
    )


def _m_relationship(g, r, idx):
    v, clerk = g.special["m_bad"], g.roles["ap_clerk_1"]
    day = g.cal.next_working(v.onboarding_date + timedelta(days=20))
    rows = []
    for _ in range(7):
        rows.append(
            _pay(
                g,
                idx,
                v,
                g.cal.business_time(r, day),
                lognormal_minor(r, 1_15_000_00, 0.25),
                "IT support contract",
                posted_by=clerk.employee_id,
            )
        )
        day = g.cal.next_working(day + timedelta(days=r.randint(35, 60)))
    g.scenario(
        scenario_id="S-M-01",
        scenario_type="employee_vendor_relationship",
        letter="M",
        severity="critical",
        is_control=False,
        description="A vendor's bank account is the salary account of the AP executive who posts its "
        "payments; the link is not declared.",
        targets=["employee–vendor attribute link (P12)", "segregation of duties (P15)"],
        phases=[P_ER, P_GRAPH, P_RISK],
        ledger_keys=[x.key for x in rows],
        vendor_ids=[v.vendor_id],
        employee_ids=[clerk.employee_id],
        parameters={"shared": "bank_account", "declared": False},
        evidence=["vendor bank account = employee bank account", "employee posted every payment"],
    )
    ok, head = g.special["m_ok"], g.roles["procurement_head"]
    rows_ok = _rows_of(g, ok.name)
    for x in rows_ok:
        x.approved_by = (
            g.roles["ap_manager"].employee_id if x.amount < g.p.vendor_limit else g.roles["cfo"].employee_id
        )
    g.scenario(
        scenario_id="S-M-02",
        scenario_type="employee_vendor_relationship",
        letter="M",
        severity="none",
        is_control=True,
        description="Declared conflict of interest: the procurement head's spouse owns this vendor (same "
        "address); the head never approves its payments.",
        targets=["should rank low: declared in the relationship register, independent approvals"],
        phases=[P_ER, P_RISK],
        ledger_keys=[x.key for x in rows_ok],
        vendor_ids=[ok.vendor_id],
        employee_ids=[head.employee_id],
        parameters={"shared": "address", "declared": True},
    )


def _n_shared(g, r, idx):
    v, emp = g.special["n_bad"], g.special["n_bad_employee"]
    day = g.cal.next_working(v.onboarding_date + timedelta(days=15))
    rows = []
    for _ in range(4):
        rows.append(
            _pay(
                g,
                idx,
                v,
                g.cal.business_time(r, day),
                lognormal_minor(r, 72_000_00, 0.3),
                "Housekeeping services",
            )
        )
        day = g.cal.next_working(day + timedelta(days=r.randint(25, 40)))
    g.scenario(
        scenario_id="S-N-01",
        scenario_type="shared_attributes",
        letter="N",
        severity="high",
        is_control=False,
        description="Vendor phone number is a shift supervisor's phone (undeclared).",
        targets=["shared phone vendor↔employee (P12 attribute edges)"],
        phases=[P_ER, P_GRAPH, P_RISK],
        ledger_keys=[x.key for x in rows],
        vendor_ids=[v.vendor_id],
        employee_ids=[emp.employee_id],
        parameters={"shared": "phone", "declared": False},
    )
    ghost, buyer = g.special["ghost"], g.roles["buyer_2"]
    g.scenario(
        scenario_id="S-N-02",
        scenario_type="shared_attributes",
        letter="N",
        severity="critical",
        is_control=False,
        description="Ghost vendor shares address and bank account with an employee.",
        targets=["shared address/bank account vendor↔employee (P12)"],
        phases=[P_ER, P_GRAPH],
        vendor_ids=[ghost.vendor_id],
        employee_ids=[buyer.employee_id],
        parameters={"shared": ["address", "bank_account"], "declared": False},
        also_part_of=["S-F-01"],
    )
    clerk = g.roles["ap_clerk_1"]
    g.scenario(
        scenario_id="S-N-03",
        scenario_type="shared_attributes",
        letter="N",
        severity="critical",
        is_control=False,
        description="Vendor shares a bank account with an AP executive.",
        targets=["shared bank account vendor↔employee (P12)"],
        phases=[P_ER, P_GRAPH],
        vendor_ids=[g.special["m_bad"].vendor_id],
        employee_ids=[clerk.employee_id],
        parameters={"shared": "bank_account", "declared": False},
        also_part_of=["S-M-01"],
    )
    g1, g2 = g.special["group_vendors"]
    g.scenario(
        scenario_id="S-N-04",
        scenario_type="shared_attributes",
        letter="N",
        severity="none",
        is_control=True,
        description="Two sister concerns share phone, address and collection account; declared as one "
        "corporate group in the relationship register.",
        targets=["should rank low: declared group relationship"],
        phases=[P_ER, P_GRAPH],
        ledger_keys=[x.key for x in _rows_of(g, g1.name) + _rows_of(g, g2.name)],
        vendor_ids=[g1.vendor_id, g2.vendor_id],
        parameters={"shared": ["phone", "address", "bank_account"], "declared": True},
    )


def _o_circular(g, r, idx):
    (v1, c1) = g.special["ring1"]
    day = _day(g, r, 0.40)
    a = _pay(
        g,
        idx,
        v1,
        g.cal.business_time(r, day),
        12_40_000_00,
        "Purchase of trading goods",
        gl=("5900", "Trading Purchases"),
        approved_by=g.roles["cfo"].employee_id,
    )
    day2 = g.cal.next_working(day + timedelta(days=6))
    b = g.receipt(
        company="LVE",
        payer_name=c1.name,
        payer_id=c1.customer_id,
        payer_type="customer",
        posted=g.cal.business_time(r, day2),
        amount=12_15_000_00,
        gl=E.GL_ADVANCE,
        description="Advance against export order",
        mode="RTGS",
        normal=False,
    )
    day3 = g.cal.next_working(day2 + timedelta(days=7))
    c_out, c_in = transfer(
        g, "LVE", "LVC", day3, 11_90_000_00, f"IC/{day3:%Y%m%d}/X", "Intercompany repayment", r, normal=False
    )
    g.scenario(
        scenario_id="S-O-01",
        scenario_type="circular_trading",
        letter="O",
        severity="critical",
        is_control=False,
        description="Ring of length 3: LVC pays a trading firm, the same firm pays LVE an 'advance', LVE "
        "returns funds to LVC; temporally ordered within 13 days, 96% of value retained.",
        targets=["temporal cycle detection (P13)", "vendor-customer identity (P12)"],
        phases=[P_ER, P_GRAPH, P_RISK],
        ledger_keys=[a.key, b.key, c_out.key, c_in.key],
        vendor_ids=[v1.vendor_id],
        customer_ids=[c1.customer_id],
        group_codes=["LVC", "LVE"],
        parameters={
            "cycle": ["LVC", v1.vendor_id + "/" + c1.customer_id, "LVE", "LVC"],
            "k": 3,
            "value_retained": round(11_90_000 / 12_40_000, 4),
        },
        evidence=["the vendor and the customer share tax ID, address and phone"],
    )
    (v2, c2), (v3, c3) = g.special["ring2a"], g.special["ring2b"]
    day = _day(g, r, 0.71)
    legs = []
    legs.append(
        _pay(
            g,
            idx,
            v2,
            g.cal.business_time(r, day),
            8_60_000_00,
            "Purchase of trading goods",
            gl=("5900", "Trading Purchases"),
            approved_by=g.roles["cfo"].employee_id,
        )
    )
    day = g.cal.next_working(day + timedelta(days=5))
    legs.append(
        g.receipt(
            company="LVE",
            payer_name=c2.name,
            payer_id=c2.customer_id,
            payer_type="customer",
            posted=g.cal.business_time(r, day),
            amount=8_45_000_00,
            gl=E.GL_ADVANCE,
            description="Advance against export order",
            mode="RTGS",
            normal=False,
        )
    )
    day = g.cal.next_working(day + timedelta(days=6))
    legs.append(
        _pay(
            g,
            idx,
            v3,
            g.cal.business_time(r, day),
            8_30_000_00,
            "Purchase of trading goods",
            gl=("5900", "Trading Purchases"),
            company="LVE",
            approved_by=g.roles["cfo"].employee_id,
        )
    )
    day = g.cal.next_working(day + timedelta(days=7))
    legs.append(
        g.receipt(
            company="LVC",
            payer_name=c3.name,
            payer_id=c3.customer_id,
            payer_type="customer",
            posted=g.cal.business_time(r, day),
            amount=8_15_000_00,
            gl=E.GL_SALES,
            description="Receipt against sales invoice",
            mode="RTGS",
            normal=False,
        )
    )
    g.scenario(
        scenario_id="S-O-02",
        scenario_type="circular_trading",
        letter="O",
        severity="critical",
        is_control=False,
        description="Ring of length 4 through two trading firms that are both suppliers and customers.",
        targets=["temporal cycle detection (P13)"],
        phases=[P_ER, P_GRAPH, P_RISK],
        ledger_keys=[x.key for x in legs],
        vendor_ids=[v2.vendor_id, v3.vendor_id],
        customer_ids=[c2.customer_id, c3.customer_id],
        group_codes=["LVC", "LVE"],
        parameters={"k": 4, "value_retained": round(8_15_000 / 8_60_000, 4)},
    )


def _p_concentration(g, r, idx):
    shells = g.special["shells"]
    rows = []
    for i, v in enumerate(shells):
        day = _day(g, r, 0.76 + 0.02 * i)
        for _j in range(4):
            company = "LVE" if (i + _j) % 2 else "LVC"
            rows.append(
                _pay(
                    g,
                    idx,
                    v,
                    g.cal.business_time(r, day),
                    lognormal_minor(r, 1_35_000_00, 0.2),
                    "Office supplies",
                    company=company,
                )
            )
            day = g.cal.next_working(day + timedelta(days=r.randint(12, 20)))
    g.scenario(
        scenario_id="S-P-01",
        scenario_type="concentration_fan_in",
        letter="P",
        severity="high",
        is_control=False,
        description="Three similarly named new vendors share one bank account, which receives payments "
        "from both group companies under three names.",
        targets=["fan-in to a shared account (P13)", "look-alike names (P12)"],
        phases=[P_ER, P_GRAPH, P_RISK],
        ledger_keys=[x.key for x in rows],
        vendor_ids=[v.vendor_id for v in shells],
        parameters={"shared_bank_account": shells[0].bank_account, "names": [v.name for v in shells]},
    )
    burst = g.special["burst"]
    day = g.cal.next_working(burst[0].onboarding_date + timedelta(days=2))
    trigger = g.receipt(
        company="LVE",
        payer_name=g.special["normal_customers"][-1].name,
        payer_id=g.special["normal_customers"][-1].customer_id,
        payer_type="customer",
        posted=g.cal.business_time(r, day),
        amount=14_20_000_00,
        gl=E.GL_EXPORT,
        description="Receipt against sales invoice",
        mode="RTGS",
        normal=False,
    )
    rows = [trigger]
    for i, v in enumerate(burst):
        d = g.cal.next_working(day + timedelta(days=1 + i // 2))
        rows.append(
            _pay(
                g,
                idx,
                v,
                g.cal.business_time(r, d),
                lognormal_minor(r, 1_55_000_00, 0.15),
                "Advance for services",
                company="LVE",
                gl=("5900", "Trading Purchases"),
            )
        )
    g.scenario(
        scenario_id="S-P-02",
        scenario_type="concentration_fan_out",
        letter="P",
        severity="high",
        is_control=False,
        description="Right after a large receipt, LVE pays seven vendors created the same day by one buyer "
        "within four days (pass-through fan-out).",
        targets=["fan-out burst vs population percentile (P13)", "new vendors (P7)"],
        phases=[P_GRAPH, P_RISK],
        ledger_keys=[x.key for x in rows],
        vendor_ids=[v.vendor_id for v in burst],
        group_codes=["LVE"],
        stages={"trigger_receipt": [trigger.key]},
        parameters={"vendors": len(burst), "days": 4},
    )


def _q_escalation(g, r, idx):
    v, poster = g.special["escalation_vendor"], g.roles["ap_clerk_2"]
    stages: dict[str, list[int]] = {"stage_1_normal": [], "stage_2_rising": [], "stage_3_round_offhours": []}
    months = g.cal.months()
    lo, hi = round(0.15 * len(months)), round(0.85 * len(months))
    third = (hi - lo) // 3
    amount = 45_000_00
    round_rows: list[int] = []
    offhours_rows: list[int] = []
    for k, month in enumerate(months[lo:hi]):
        stage = (
            "stage_1_normal" if k < third else "stage_2_rising" if k < 2 * third else "stage_3_round_offhours"
        )
        per_month = (
            1 if stage == "stage_1_normal" else 2 + (k % 2) if stage == "stage_2_rising" else 3 + (k % 2)
        )
        if stage != "stage_1_normal":
            amount = round(amount * 1.12)
        for j in range(per_month):
            day = g.cal.payment_day(r, month)
            value = lognormal_minor(r, amount, 0.1)
            when, late, step = g.cal.business_time(r, day), stage == "stage_3_round_offhours", 100
            offhours = False
            if late:
                value, step = round(value / 1_000_00) * 1_000_00, 1_000_00
                sunday = day + timedelta(days=(6 - day.weekday()) % 7)
                if j == 0 and sunday <= g.p.end:
                    when, offhours = datetime.combine(sunday, time(15, 20 + k % 30)), True
            value = idx.fresh(v.name, value, when.date(), step=step)
            row = g.payment(
                company="LVC",
                vendor=v,
                posted=when,
                amount=value,
                gl=E.PROFILES["maintenance"][0],
                description="Tool room services",
                posted_by=poster.employee_id,
                normal=False,
            )
            stages[stage].append(row.key)
            if late:
                round_rows.append(row.key)
            if offhours:
                offhours_rows.append(row.key)
    keys = [k for s in stages.values() for k in s]
    g.scenario(
        scenario_id="S-Q-01",
        scenario_type="gradual_risk_escalation",
        letter="Q",
        severity="high",
        is_control=False,
        description="Over ~two years one vendor's payments grow 12% per month in size and frequency, then "
        "turn round-valued and partly weekend-posted by one AP user.",
        targets=["risk trend over time (P9/P15)", "behavioural deviation (P8)"],
        phases=[P_ANOM, P_RISK, P_INV],
        ledger_keys=keys,
        vendor_ids=[v.vendor_id],
        employee_ids=[poster.employee_id],
        stages=stages,
        parameters={"monthly_growth": 0.12},
        also_part_of=["S-B-02", "S-K-02"],
    )
    g.scenario(
        scenario_id="S-B-02",
        scenario_type="round_number",
        letter="B",
        severity="medium",
        is_control=False,
        description="Round amounts in the late stage of S-Q-01 (non-isolated).",
        targets=["round-amount proportion per payee (P7)"],
        phases=[P_RULES],
        ledger_keys=round_rows,
        vendor_ids=[v.vendor_id],
        also_part_of=["S-Q-01"],
    )
    g.scenario(
        scenario_id="S-K-02",
        scenario_type="temporal_anomaly",
        letter="K",
        severity="medium",
        is_control=False,
        description="Sunday postings in the late stage of S-Q-01 (non-isolated).",
        targets=["weekend postings (P7)"],
        phases=[P_RULES],
        ledger_keys=offhours_rows,
        vendor_ids=[v.vendor_id],
        employee_ids=[poster.employee_id],
        also_part_of=["S-Q-01"],
    )


def _r_silent(g, r, idx):
    e = [x for x in g.employees if x.role == "Shift Supervisor"][-1]
    rows = []
    months = g.cal.months()
    for month in months[2:-2]:
        for _ in range(9 + r.randint(0, 4)):
            day = r.choice(g.cal.working_days(month))
            amount = idx.fresh(e.name, r.randint(650, 1_950) * 100 + r.randint(0, 99), day)
            rows.append(
                g.claim(
                    employee=e,
                    posted=g.cal.business_time(r, day),
                    amount=amount,
                    description="Local conveyance",
                    gl=E.GL_TRAVEL,
                    normal=False,
                )
            )
    g.scenario(
        scenario_id="S-R-01",
        scenario_type="silent_small_fraud",
        letter="R",
        severity="medium",
        is_control=False,
        description="Many small 'local conveyance' claims (₹650–1,950), each unremarkable; together several "
        "times the department's per-person total.",
        targets=["peer comparison of totals/frequency within department (P6/P8)"],
        phases=["P6", P_ANOM, P_RISK],
        ledger_keys=[x.key for x in rows],
        employee_ids=[e.employee_id],
        parameters={"claims": len(rows), "total_minor": sum(x.amount for x in rows)},
    )


def _s_bank(g, r, idx):
    free = _background(g)
    pool = sorted(
        (x for x in g.rows if free(x) and x.cp_type == "vendor" and x.bank == "match"),
        key=lambda x: (x.posted, x.key),
    )
    rec = sorted(
        (x for x in g.rows if free(x) and x.cp_type == "customer" and x.bank == "match"),
        key=lambda x: (x.posted, x.key),
    )

    def pick(rows, frac):
        row = rows[int(frac * (len(rows) - 1))]
        while not row.normal:
            row = rows[rows.index(row) + 1]
        row.normal = False
        return row

    def record(sid, kind, desc, ledger, sev="medium", params=None):
        return g.scenario(
            scenario_id=sid,
            scenario_type="bank_mismatch",
            letter="S",
            severity=sev,
            is_control=False,
            description=desc,
            targets=[f"reconciliation: {kind} (P14)"],
            phases=[P_RECON, P_RISK],
            ledger_keys=[x.key for x in ledger],
            parameters={"kind": kind, **(params or {})},
        )

    lo = [pick(pool, f) for f in (0.31, 0.57, 0.86)]
    for x in lo:
        x.bank = "ledger_only"
    record("S-S-01", "ledger_only", "Three ledger payments never appear on the bank statement.", lo, "high")
    g.special["bank_only"] = [
        ("unexplained_debit", _day(g, r, 0.47)),
        ("unbooked_charge", _day(g, r, 0.62)),
        ("unexplained_debit", _day(g, r, 0.91)),
    ]
    record(
        "S-S-02",
        "bank_only",
        "Bank debits with no ledger entry (two unexplained ACH debits, one unbooked charge).",
        [],
        "high",
    )
    delayed = [pick(pool, f) for f in (0.12, 0.42, 0.69, 0.95)]
    for x in delayed:
        x.bank, x.bank_info["delay"] = "delayed", 8 + delayed.index(x)
    record(
        "S-S-03",
        "delayed",
        "Payments cleared 8–11 working days after posting (outside a normal tolerance).",
        delayed,
        "low",
    )
    v = g.special["late_vendors"]["batch"]
    day, batch = _day(g, r, 0.55), []
    for _ in range(3):  # three consignments in one week, settled by one bank debit
        batch.append(
            _pay(
                g, idx, v, g.cal.business_time(r, day), lognormal_minor(r, 30_000_00, 0.3), "Freight charges"
            )
        )
        day = g.cal.next_working(day + timedelta(days=2))
    for x in batch:
        x.normal, x.bank, x.bank_info["group"] = False, "batched", "B1"
    s4 = record(
        "S-S-04",
        "batched",
        "Three ledger payments settled by one bank debit (sum).",
        batch,
        "low",
        {"group_size": len(batch)},
    )
    s4.vendor_ids = [v.vendor_id]
    cust = g.special["normal_customers"][0]
    day = _day(g, r, 0.36, run_day=False)
    group = [
        g.receipt(
            company=cust.entity,
            payer_name=cust.name,
            payer_id=cust.customer_id,
            payer_type="customer",
            posted=g.cal.business_time(r, day),
            amount=lognormal_minor(r, 2_40_000_00, 0.3),
            gl=E.GL_SALES if cust.entity == "LVC" else E.GL_EXPORT,
            description="Receipt against sales invoice",
            normal=False,
        )
        for _ in range(3)
    ]
    for x in group:
        x.bank, x.bank_info["group"] = "one_to_many", "R1"
    record(
        "S-S-05",
        "one_to_many",
        "One customer deposit settles three sales invoices booked separately.",
        group,
        "low",
        {"invoices": 3},
    )
    fees = [pick(pool, 0.22), pick(rec, 0.5), pick(rec, 0.8)]
    fees[0].bank, fees[0].bank_info["fee"] = "fee_adjusted", 590_00
    for x in fees[1:]:
        x.bank, x.bank_info["fee"] = "fee_adjusted", -236_00
    record(
        "S-S-06",
        "fee_adjusted",
        "Bank amount differs from the ledger by a bank fee (+₹590 on a remittance, "
        "−₹236 deducted from two receipts).",
        fees,
        "low",
    )
    mismatch = pick(pool, 0.74)
    mismatch.bank, mismatch.bank_info["beneficiary"] = "payee_mismatch", g.roles["admin_exec"]
    s7 = record(
        "S-S-07",
        "payee_mismatch",
        "Ledger says vendor X; the bank shows the money went to an employee's account.",
        [mismatch],
        "critical",
        {"employee": g.roles["admin_exec"].employee_id},
    )
    s7.employee_ids = [g.roles["admin_exec"].employee_id]  # the beneficiary on the bank line


def _t_benford(g, r, idx):
    v = g.special["benford_vendor"]
    rows, months = [], g.cal.months()
    per_month = max(1, round(330 / len(months)))
    for month in months:
        for _ in range(per_month):
            day = g.cal.payment_day(r, month)
            amount = idx.fresh(v.name, r.randint(7_000_00, 9_999_99), day)
            rows.append(
                g.payment(
                    company="LVC",
                    vendor=v,
                    posted=g.cal.business_time(r, day),
                    amount=amount,
                    gl=E.PROFILES["facilities"][0],
                    description="Office interiors and fittings",
                    normal=False,
                )
            )
    g.scenario(
        scenario_id="S-T-01",
        scenario_type="digit_distribution_anomaly",
        letter="T",
        severity="medium",
        is_control=False,
        description="A subgroup whose amounts are spread evenly between ₹7,000 and ₹9,999 (leading digits "
        "7–9 only). Not labelled as fraud: the Benford module must find the deviation.",
        targets=["Benford first-digit subgroup test, MAD band (P7)"],
        phases=[P_RULES],
        ledger_keys=[x.key for x in rows],
        vendor_ids=[v.vendor_id],
        parameters={"n": len(rows), "range_rupees": [7_000, 9_999.99]},
    )


def _v_data_quality(g, r, idx):
    v = _claim_vendor(g, "office_supplies")
    rows = [_take(_rows_of(g, v.name), r, f, f + 0.05) for f in (0.2, 0.5, 0.8)]
    s = g.scenario(
        scenario_id="S-V-01",
        scenario_type="data_quality",
        letter="V",
        severity="low",
        is_control=False,
        description="Typing problems: three payee names with doubled spaces, one invoice number with a "
        "hidden zero-width character, one payment without a narration.",
        targets=["P4 integrity: needs_normalization, malformed_identifier, missing_optional"],
        phases=["P4"],
        vendor_ids=[v.vendor_id],
        parameters={},
    )
    for x in rows:
        x.payee = x.payee.replace(" ", "  ", 1)
        g.expect("ledger", "needs_normalization", x, s)
    pool = [x for x in _rows_of(g, _claim_vendor(g, "office_supplies").name)]
    zw = _take(pool, r, 0.3, 0.4)
    zw.reference = zw.reference[:3] + "​" + zw.reference[3:]
    g.expect("ledger", "malformed_identifier", zw, s)
    blank = _take(pool, r, 0.6, 0.7)
    blank.description = ""
    g.expect("ledger", "missing_optional", blank, s)
    s.ledger_keys = [x.key for x in [*rows, zw, blank]]


def _w_inconsistent(g, r, idx):
    v = g.special["late_vendors"]["it_gl"]
    rows = _rows_of(g, v.name)
    wrong_gl = [_take(rows, r, 0.3, 0.35), _take(rows, r, 0.7, 0.75)]
    for x in wrong_gl:
        x.gl, x.gl_name = E.PROFILES["raw_materials"][0]
    free = _background(g)
    small = [x for x in g.rows if free(x) and x.cp_type == "vendor" and 30_000_00 < x.amount < 1_50_000_00]
    rtgs = [small[len(small) // 3], small[2 * len(small) // 3]]
    for x in rtgs:
        x.mode, x.normal = "RTGS", False
    g.scenario(
        scenario_id="S-W-01",
        scenario_type="inconsistent_combination",
        letter="W",
        severity="low",
        is_control=False,
        description="IT services booked to Raw Materials (twice), and RTGS used for amounts below the "
        "₹2 lakh RTGS minimum (twice).",
        targets=["GL account vs vendor category, payment mode vs amount (P7)"],
        phases=[P_RULES],
        ledger_keys=[x.key for x in wrong_gl + rtgs],
        vendor_ids=[v.vendor_id],
        parameters={"gl_mismatch": 2, "rtgs_below_minimum": 2},
    )


def _x_combined(g, r, idx):
    v, emp = g.special["combined"], g.special["combined_employee"]
    day = g.cal.next_working(v.onboarding_date + timedelta(days=4))
    rows = []
    for i in range(6):
        amount = 4_60_000_00 + r.randint(0, 99_99) if i == 4 else lognormal_minor(r, 62_000_00, 0.2)
        rows.append(_pay(g, idx, v, g.cal.business_time(r, day), amount, "Housekeeping services"))
        day = g.cal.next_working(day + timedelta(days=r.randint(2, 4)))
    big = rows[4]
    g.scenario(
        scenario_id="S-X-01",
        scenario_type="composite_risk",
        letter="X",
        severity="critical",
        is_control=False,
        description="New housekeeping vendor in a city where the group has no other supplier, sharing an "
        "employee's phone, paid six times in ~18 days, once ~7x its other payments.",
        targets=["combination of weak signals > any single signal (P9 fusion)", "explanations (P9)"],
        phases=[P_RULES, P_ANOM, P_ER, P_RISK],
        ledger_keys=[x.key for x in rows],
        vendor_ids=[v.vendor_id],
        employee_ids=[emp.employee_id],
        parameters={"components": ["new vendor", "velocity", "amount spike", "shared phone", "remote city"]},
        evidence=[
            "vendor age < 30 days",
            "6 payments in 18 days",
            "one payment ~7x the others",
            "phone shared with an employee",
            "only supplier located in Morvik",
        ],
        also_part_of=["S-G-02", "S-H-02", "S-A-02", "S-N-05"],
    )
    for sid, typ, letter, keys, text in (
        (
            "S-G-02",
            "sudden_vendor_appearance",
            "G",
            [x.key for x in rows],
            "Part of S-X-01: payments soon after onboarding.",
        ),
        ("S-H-02", "velocity_spike", "H", [x.key for x in rows], "Part of S-X-01: six payments in 18 days."),
        ("S-A-02", "amount_anomaly", "A", [big.key], "Part of S-X-01: one payment ~7x the vendor's others."),
        ("S-N-05", "shared_attributes", "N", [], "Part of S-X-01: phone shared with an employee."),
    ):
        g.scenario(
            scenario_id=sid,
            scenario_type=typ,
            letter=letter,
            severity="medium",
            is_control=False,
            description=text,
            targets=["component of a composite case"],
            phases=[P_RISK],
            ledger_keys=keys,
            vendor_ids=[v.vendor_id],
            employee_ids=[emp.employee_id] if letter == "N" else [],
            also_part_of=["S-X-01"],
        )


# =============================================================================================== controls (U)


def _u_controls(g, r, idx):
    sp = g.special

    def control(sid, typ, desc, *, keys=(), vendors=(), employees=(), customers=(), params=None, p4=None):
        return g.scenario(
            scenario_id=sid,
            scenario_type=typ,
            letter="U",
            severity="none",
            is_control=True,
            description=desc,
            targets=["should NOT be flagged as suspicious (false-positive check)"],
            phases=[P_RULES, P_ANOM, P_ER, P_GRAPH, P_RISK],
            ledger_keys=list(keys),
            vendor_ids=list(vendors),
            employee_ids=list(employees),
            customer_ids=list(customers),
            parameters=params or {},
        )

    # U-01 rent: exact round monthly amount with 5% annual escalation
    land, rows = sp["landlord"], []
    for i, month in enumerate(g.cal.months()):
        amount = round(3_50_000_00 * 1.05 ** (i // 12) / 500_00) * 500_00  # lease rounds rent to ₹500
        day = g.cal.next_working(month.replace(day=5))
        rows.append(
            _pay(
                g,
                idx,
                land,
                g.cal.business_time(r, day),
                amount,
                "Factory rent",
                gl=E.GL_RENT,
                doc_date=month,
            )
        )
    control(
        "S-U-01",
        "legit_round_numbers",
        "Monthly factory rent: exact round amount per lease (5% annual escalation, rounded to ₹500).",
        keys=[x.key for x in rows],
        vendors=[land.vendor_id],
    )
    rows = []
    for v, fee in zip(sp["retainers"], (25_000, 40_000), strict=True):
        for month in g.cal.months():
            day = g.cal.next_working(month.replace(day=10))
            rows.append(_pay(g, idx, v, g.cal.business_time(r, day), fee * 100, "Monthly retainer fee"))
    control(
        "S-U-02",
        "legit_round_numbers",
        "Two advisers on fixed monthly retainers (round amounts by contract).",
        keys=[x.key for x in rows],
        vendors=[v.vendor_id for v in sp["retainers"]],
    )
    # U-03 similar names, unrelated businesses
    u1, u2 = sp["similar_names"]
    control(
        "S-U-03",
        "legit_similar_names",
        "Two unrelated vendors share a word in their names but differ in "
        "trade, city, tax ID and bank account, with long independent histories.",
        vendors=[u1.vendor_id, u2.vendor_id],
    )
    # U-04 legitimate new vendor with normal amounts
    nv = sp["legit_new_vendor"]
    day, rows = g.cal.next_working(nv.onboarding_date + timedelta(days=21)), []
    while day < g.p.end - timedelta(days=10):
        rows.append(
            _pay(
                g, idx, nv, g.cal.business_time(r, day), lognormal_minor(r, 24_000_00, 0.4), "Freight charges"
            )
        )
        day = g.cal.next_working(day + timedelta(days=r.randint(7, 16)))
    control(
        "S-U-04",
        "legit_new_vendor",
        "New logistics partner, onboarded mid-period, paid ordinary amounts at an ordinary cadence.",
        keys=[x.key for x in rows],
        vendors=[nv.vendor_id],
    )
    # U-05 legitimate large payment in installments of one invoice (also legit repeated invoice number)
    cap = sp["capex_vendor"]
    ref, rows = f"{cap.invoice_prefix}/00101", []
    day = g.cal.next_working(cap.onboarding_date + timedelta(days=30))
    for share in (0.4, 0.4, 0.2):
        rows.append(
            _pay(
                g,
                idx,
                cap,
                g.cal.business_time(r, day),
                round(48_00_000_00 * share),
                "CNC machine purchase - installment",
                reference=ref,
                gl=E.GL_CAPEX,
                approved_by=g.roles["cfo"].employee_id,
                doc_date=cap.onboarding_date + timedelta(days=25),
                mode="RTGS",
            )
        )
        day = g.cal.next_working(day + timedelta(days=45))
    s = control(
        "S-U-05",
        "legit_large_payment_installments",
        "₹48 lakh machine paid in three installments against "
        "one invoice, CFO-approved: large, round-ish and a repeated invoice number, all legitimate.",
        keys=[x.key for x in rows],
        vendors=[cap.vendor_id],
        params={"invoice": ref, "total_minor": 48_00_000_00},
    )
    for x in rows[1:]:
        g.expect("ledger", "duplicate_source_id", x, s)
    # U-06 legitimate seasonal velocity: raw materials every Oct–Dec
    raw = sp["seasonal_vendor"]  # paid here, not by the Poisson background: the season must show every year
    for month in g.cal.months():
        festive = month.month in (10, 11, 12)
        for _ in range((5 if festive else 2) + r.randint(0, 1)):
            day = g.cal.payment_day(r, month)
            amount = lognormal_minor(r, 2_10_000_00 if festive else 1_80_000_00, 0.3)
            _pay(
                g,
                idx,
                raw,
                g.cal.business_time(r, day),
                amount,
                r.choice(["Steel coils", "Aluminium ingots"]),
            )
    peak = [x.key for x in _rows_of(g, raw.name) if x.posted.month in (10, 11, 12)]
    control(
        "S-U-06",
        "legit_seasonal_velocity",
        "Raw-material purchases rise every October–December (festive "
        "production): a recurring, personal seasonal pattern.",
        keys=peak,
        vendors=[raw.vendor_id],
        params={"months": [10, 11, 12], "payments_per_month": {"festive": [5, 6], "other": [2, 3]}},
    )
    # U-07 legitimate single claims just under the limit (no pattern)
    busy = {sp["combined_employee"].employee_id, sp["first_high_employee"].employee_id}
    claimers = [e for e in g.employees if e.role == "Sales Executive" and e.employee_id not in busy][:3]
    rows = []
    for i, e in enumerate(claimers):
        day = _day(g, r, 0.2 + 0.25 * i, run_day=False)
        rows.append(
            g.claim(
                employee=e,
                posted=g.cal.business_time(r, day),
                amount=round(g.p.expense_limit * 0.97),
                description="Client visit travel",
                gl=E.GL_TRAVEL,
                normal=False,
            )
        )
    control(
        "S-U-07",
        "legit_near_threshold",
        "Three different employees each file one claim at 97% of the limit, "
        "months apart (no splitting pattern).",
        keys=[x.key for x in rows],
        employees=[e.employee_id for e in claimers],
    )
    # U-08 quarter-end late postings by Finance
    rows, adviser = [], sp["retainers"][0]
    for month in g.cal.months():
        if month.month in (3, 6, 9, 12):
            day = g.cal.last_working_day(month)
            rows.append(
                _pay(
                    g,
                    idx,
                    adviser,
                    datetime.combine(day, time(19 + len(rows) % 2, 15 + len(rows))),
                    lognormal_minor(r, 85_000_00, 0.2),
                    "Quarter-end advisory fees",
                    posted_by=g.roles["finance_clerk"].employee_id,
                )
            )
    control(
        "S-U-08",
        "legit_period_end_overtime",
        "Finance posts quarter-end fees between 19:00 and 21:00 on the last working day of each quarter.",
        keys=[x.key for x in rows],
        vendors=[adviser.vendor_id],
        employees=[g.roles["finance_clerk"].employee_id],
    )
    # U-09 normal non-circular chains; U-10 legitimate vendor/customer dual role
    chain = [
        x.key
        for x in g.rows
        if x.cp_type == "group" and x.description == "Intercompany repayment" and x.normal
    ]
    control(
        "S-U-09",
        "legit_multi_party_chain",
        "Customers pay LVE, LVE repays LVC, LVC pays vendors: multi-hop "
        "flows that never return to their origin.",
        keys=chain[:6],
    )
    sv, sc = sp["dual_role"]
    day, rows = _day(g, r, 0.05, run_day=False), []
    while day < g.p.end - timedelta(days=20):
        rows.append(
            g.receipt(
                company="LVC",
                payer_name=sc.name,
                payer_id=sc.customer_id,
                payer_type="customer",
                posted=g.cal.business_time(r, day),
                amount=lognormal_minor(r, 55_000_00, 0.3),
                gl=("4100", "Scrap Sales"),
                description="Sale of scrap",
                normal=False,
            )
        )
        day = g.cal.next_working(day + timedelta(days=r.randint(80, 100)))
    control(
        "S-U-10",
        "legit_reciprocal_flows",
        "A maintenance vendor also buys the plant's scrap: two-way flows with unrelated amounts and timing.",
        keys=[x.key for x in rows + _rows_of(g, sv.name)],
        vendors=[sv.vendor_id],
        customers=[sc.customer_id],
    )
    control(
        "S-U-11",
        "legit_fan_in",
        "LVC receives from many customers: high fan-in that is ordinary.",
        customers=[c.customer_id for c in sp["normal_customers"] if c.entity == "LVC"],
    )
    control(
        "S-U-12",
        "legit_batch_payments",
        "Monthly payroll is one bulk debit covering every employee.",
        keys=[x.key for x in g.rows if x.cp_id == "PAYROLL"],
    )
    # U-13 two meters billed the same amount on the same day (a genuine near-duplicate)
    util = sp["two_meter_utility"]
    day, rows = _day(g, r, 0.33), []
    amount = lognormal_minor(r, 88_000_00, 0.1)
    for i, meter in enumerate(("Meter A", "Meter B")):
        rows.append(
            g.payment(
                company="LVC",
                vendor=util,
                posted=datetime.combine(day, time(11, 5 + 20 * i)),
                amount=amount,
                gl=E.PROFILES["utilities"][0],
                description=f"Electricity charges {meter}",
                normal=False,
            )
        )
    rows.sort(key=lambda x: x.posted)
    g.allowed_pairs.add(frozenset(x.key for x in rows))
    s = control(
        "S-U-13",
        "legit_near_duplicate",
        "Two electricity meters billed the same amount on the same day "
        "(two invoices): a near-duplicate that is genuine.",
        keys=[x.key for x in rows],
        vendors=[util.vendor_id],
    )
    g.expect("ledger", "near_duplicate", rows[1], s)
    # U-14 legitimate sequential invoices from a single-client canteen contractor
    can, rows = sp["canteen"], []
    for i, month in enumerate(g.cal.months()):
        day = g.cal.next_working(month.replace(day=12))
        rows.append(
            _pay(
                g,
                idx,
                can,
                g.cal.business_time(r, day),
                lognormal_minor(r, 1_42_000_00, 0.06),
                "Canteen services",
                reference=f"{can.invoice_prefix}/{301 + i:04d}",
            )
        )
    control(
        "S-U-14",
        "legit_sequential_invoices",
        "Canteen contractor that serves only this plant: consecutive "
        "invoice numbers are normal for a single-client business.",
        keys=[x.key for x in rows],
        vendors=[can.vendor_id],
    )
    m_ok = sp["m_ok"]
    control(
        "S-U-15",
        "legit_employee_vendor_relationship",
        "See S-M-02: declared relationship with independent approvals.",
        vendors=[m_ok.vendor_id],
        employees=[g.roles["procurement_head"].employee_id],
    )
    g1, g2 = sp["group_vendors"]
    control(
        "S-U-16",
        "legit_shared_attributes",
        "See S-N-04: declared sister concerns sharing contact details and a collection account.",
        vendors=[g1.vendor_id, g2.vendor_id],
    )
