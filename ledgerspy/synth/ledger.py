"""Normal business of the fictional group (the population every scenario must stand out from).

Structure on purpose: each vendor has its own cadence and log-normal amount profile; categories have seasonal
peaks; AP payment runs fall mostly on Tuesdays/Fridays inside business hours; customers pay on their own
terms;
employees file claims at individual rates. Nothing here is uniformly random.
"""

from datetime import date, datetime, time, timedelta

from ledgerspy.synth import entities as E
from ledgerspy.synth.context import Gen
from ledgerspy.synth.rng import lognormal_minor, poisson

DESCRIPTIONS = {
    "raw_materials": [
        "Supply of steel coils",
        "Aluminium ingots",
        "Copper wire rods",
        "Polymer granules",
        "MS sheets and plates",
        "Brass castings",
    ],
    "packaging": ["Corrugated cartons", "Stretch wrap rolls", "Wooden pallets", "Printed labels"],
    "logistics": ["Freight charges", "Container haulage", "Inbound transport", "Courier consignments"],
    "utilities": ["Electricity charges", "Water charges", "Utility charges"],
    "it_services": ["Software subscription", "Network support", "Cloud hosting", "IT support contract"],
    "facilities": ["Housekeeping services", "Security services", "Pest control", "Garden maintenance"],
    "office_supplies": ["Stationery", "Printer consumables", "Office pantry supplies"],
    "professional": ["Tax advisory fees", "Legal fees", "Internal audit fees", "Consulting fees"],
    "staffing": ["Contract manpower", "Temporary staffing", "Loading crew services"],
    "maintenance": ["Machine repair", "Electrical maintenance", "Tool room services", "Compressor servicing"],
}
CLAIMS = [
    ("Local conveyance", E.GL_TRAVEL),
    ("Client visit travel", E.GL_TRAVEL),
    ("Fuel reimbursement", E.GL_TRAVEL),
    ("Team lunch", E.GL_WELFARE),
    ("Hotel stay", E.GL_TRAVEL),
    ("Mobile and internet", E.GL_WELFARE),
]
CLAIMANT_ROLES = (
    "Sales Executive",
    "Production Engineer",
    "Buyer",
    "Head of Sales",
    "Plant Operations Manager",
    "Shift Supervisor",
    "Head of Procurement",
    "Systems Engineer",
)


def vendor_payments(g: Gen) -> None:
    for v in g.vendors:
        prof = g.vendor_profile.get(v.name)
        if prof is None:
            continue  # scenario/control vendors are paid by their own code
        r = g.rng(f"payments:{v.name}")
        gl = E.PROFILES[prof["category"]][0]
        company = prof.setdefault(
            "company",
            "LVE" if prof["category"] in ("logistics", "packaging") and r.random() < 0.25 else "LVC",
        )
        for month in g.cal.months():
            if month > v.onboarding_date and prof["churn"] and month > prof["churn"]:
                break
            season = prof["season"].get(month.month, 1.0)
            for _ in range(poisson(r, prof["rate"] * season)):
                day = g.cal.payment_day(r, month)
                if day < v.onboarding_date + timedelta(days=20):
                    continue
                amount = lognormal_minor(r, round(prof["median"] * season**0.5), prof["sigma"])
                g.payment(
                    company=company,
                    vendor=v,
                    posted=g.cal.business_time(r, day),
                    amount=amount,
                    gl=gl,
                    description=r.choice(DESCRIPTIONS[prof["category"]]),
                )


def expense_claims(g: Gen) -> None:
    claimants = [e for e in g.employees if e.role in CLAIMANT_ROLES]
    for e in claimants:
        r = g.rng(f"claims:{e.name}")
        rate, median = r.uniform(0.5, 1.3), r.randint(1_200, 2_600) * 100
        for month in g.cal.months():
            for _ in range(poisson(r, rate)):
                day = r.choice(g.cal.working_days(month))
                text, gl = r.choice(CLAIMS)
                amount = lognormal_minor(r, median, 0.55)
                if r.random() < 0.04:  # occasional legitimate larger claim, approved by Finance
                    amount = r.randint(6_500, 16_000) * 100 + r.randint(0, 99)
                    text, gl = "Outstation travel", E.GL_TRAVEL
                g.claim(
                    employee=e, posted=g.cal.business_time(r, day), amount=amount, description=text, gl=gl
                )


def customer_receipts(g: Gen) -> None:
    for c in g.special["normal_customers"]:
        r = g.rng(f"receipts:{c.name}")
        rate = r.uniform(1.1, 1.8)
        median = (r.randint(6, 12) if c.entity == "LVE" else r.randint(3, 6)) * 100_000 * 100
        for month in g.cal.months():
            for _ in range(poisson(r, rate)):
                day = r.choice(g.cal.working_days(month))
                if day < c.onboarding_date:
                    continue
                amount = lognormal_minor(r, median, 0.5)
                gl = E.GL_EXPORT if c.entity == "LVE" else E.GL_SALES
                doc = day - timedelta(days=c.credit_terms_days + r.randint(-5, 10))
                g.receipt(
                    company=c.entity,
                    payer_name=c.name,
                    payer_id=c.customer_id,
                    payer_type="customer",
                    posted=g.cal.business_time(r, day),
                    amount=amount,
                    gl=gl,
                    doc_date=doc,
                    description="Receipt against sales invoice",
                    mode="RTGS" if amount >= 20_000_000 else "NEFT",
                )


def _internal_row(
    g: Gen,
    *,
    company: str,
    posted: datetime,
    payee: str,
    payee_id: str,
    cp_type: str,
    gl: tuple[str, str],
    amount: int,
    reference: str,
    description: str,
    mode: str = "NEFT",
):
    from ledgerspy.synth.model import LedgerRow

    row = LedgerRow(
        key=g.next_key(),
        posted=posted,
        doc_date=posted.date(),
        company=company,
        gl=gl[0],
        gl_name=gl[1],
        payer=g.group[company].legal_name,
        payee=payee,
        cp_id=payee_id,
        cp_type=cp_type,
        description=description,
        reference=reference,
        amount=amount,
        drcr="Dr",
        mode=mode,
        approved_by=g.roles["cfo"].employee_id,
        posted_by=g.roles["finance_clerk"].employee_id,
    )
    g.rows.append(row)
    return row


def payroll_statutory_charges(g: Gen) -> None:
    r = g.rng("payroll")
    for i, month in enumerate(g.cal.months()):
        year_idx = (month - g.p.start).days // 365
        for company, base, stat, charge in (
            ("LVC", 2_840_000, 650_000, 23_600),
            ("LVE", 630_000, 120_000, 17_700),
        ):
            pay_day = g.cal.last_working_day(month)
            amount = round(base * 100 * 1.08**year_idx * r.uniform(0.985, 1.015))
            row = _internal_row(
                g,
                company=company,
                posted=datetime.combine(pay_day, time(16, r.randint(0, 40))),
                payee="Payroll Clearing A/c",
                payee_id="PAYROLL",
                cp_type="internal",
                gl=E.GL_PAYROLL,
                amount=amount,
                reference=f"{company}/PAY/{month:%Y-%m}",
                description=f"Salaries {month:%b %Y}",
                mode="BULK",
            )
            row.bank = "payroll_batch"
            stat_day = g.cal.next_working(month.replace(day=7))
            if stat_day <= g.p.end:
                _internal_row(
                    g,
                    company=company,
                    posted=g.cal.business_time(r, stat_day),
                    payee="Central Tax Payment Gateway",
                    payee_id="TAXGW",
                    cp_type="statutory",
                    gl=E.GL_STATUTORY,
                    amount=lognormal_minor(r, stat * 100, 0.2),
                    reference=f"{company}/GST/{month:%Y-%m}",
                    description="GST and TDS remittance",
                )
            charge_day = g.cal.last_working_day(month)
            row = _internal_row(
                g,
                company=company,
                posted=datetime.combine(charge_day, time(17, 30 + i % 20)),
                payee="Synbank Ltd",
                payee_id="BANK",
                cp_type="bank",
                gl=E.GL_BANK,
                amount=charge,
                reference=f"{company}/BC/{month:%Y-%m}",
                description="Bank charges incl. GST",
                mode="AUTO-DEBIT",
            )
            row.bank = "bank_charge"


def intercompany(g: Gen) -> None:
    """Quarterly funding LVC → LVE and half-yearly repayment LVE → LVC (both companies book each transfer)."""
    r = g.rng("interco")
    for month in g.cal.months():
        if month.month in (4, 7, 10, 1):
            day = g.cal.next_working(month.replace(day=3))
            transfer(g, "LVC", "LVE", day, 2_500_000 * 100, f"IC/{month:%Y%m}/F", "Intercompany funding", r)
        if month.month in (9, 3):
            day = g.cal.next_working(month.replace(day=20))
            transfer(g, "LVE", "LVC", day, 4_000_000 * 100, f"IC/{month:%Y%m}/R", "Intercompany repayment", r)


def transfer(g: Gen, src: str, dst: str, day: date, amount: int, ref: str, text: str, r, normal: bool = True):
    """One transfer between group companies: a payment in the sender's books, a receipt in the receiver's."""
    from ledgerspy.synth.model import LedgerRow

    posted = g.cal.business_time(r, day)
    out = LedgerRow(
        key=g.next_key(),
        posted=posted,
        doc_date=day,
        company=src,
        gl=E.GL_INTERCO[0],
        gl_name=E.GL_INTERCO[1],
        payer=g.group[src].legal_name,
        payee=g.group[dst].legal_name,
        cp_id=dst,
        cp_type="group",
        description=text,
        reference=f"{ref}/{src}",
        amount=amount,
        drcr="Dr",
        mode="RTGS",
        approved_by=g.roles["cfo"].employee_id,
        posted_by=g.roles["accountant"].employee_id,
        normal=normal,
    )
    inc = LedgerRow(
        key=g.next_key(),
        posted=posted + timedelta(minutes=12),
        doc_date=day,
        company=dst,
        gl=E.GL_INTERCO[0],
        gl_name=E.GL_INTERCO[1],
        payer=g.group[src].legal_name,
        payee=g.group[dst].legal_name,
        cp_id=src,
        cp_type="group",
        description=text,
        reference=f"{ref}/{dst}",
        amount=amount,
        drcr="Cr",
        mode="RTGS",
        approved_by="",
        posted_by=g.roles["accountant"].employee_id,
        normal=normal,
    )
    out.bank, inc.bank = "transfer_out", "transfer_in"
    g.rows += [out, inc]
    return out, inc


def normal_flows(g: Gen) -> None:
    vendor_payments(g)
    expense_claims(g)
    customer_receipts(g)
    payroll_statutory_charges(g)
    intercompany(g)
