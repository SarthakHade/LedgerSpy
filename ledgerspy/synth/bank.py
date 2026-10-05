"""Bank statements (one account per group company) derived from the ledger, and the monthly cash-flow history.

Normal settlement: the bank date is the posting date plus 0–2 working days (receipts: same day), same amount.
Injected complications are driven by each ledger row's `bank` mode (set by scenario S and the normal flows):
ledger_only, delayed, batched, one_to_many, fee_adjusted, payee_mismatch; bank-only debits come from
`g.special["bank_only"]`. Balances are computed per account in statement order, so they are coherent by
construction; the opening balance is chosen so the account never goes negative.
"""

from collections import defaultdict
from datetime import date, timedelta

from ledgerspy.synth.context import Gen
from ledgerspy.synth.model import BankRow, LedgerRow

OPENING_FLOOR = {"LVC": 2_50_00_000_00, "LVE": 60_00_000_00}


def _utr(g: Gen, d: date) -> str:
    g.special["utr"] = g.special.get("utr", 0) + 1
    return f"SYNR{d:%y%m%d}{g.special['utr']:06d}"


def _narration(row: LedgerRow, utr: str, counterparty: str, suffix: str = "") -> str:
    name = " ".join(counterparty.upper().split())[:32]
    if row.mode in ("BULK",):
        return f"SALARY BULK UPLOAD {row.posted:%b%Y}".upper()
    if row.mode == "AUTO-DEBIT":
        return f"CHARGES INCL GST {row.posted:%b%Y}".upper()
    direction = "CR" if row.drcr == "Cr" else "DR"
    return f"{row.mode} {direction}/{utr}/{name}{suffix}"


def build_bank(g: Gen) -> None:
    r = g.rng("bank")
    bank_of: dict[int, list[int]] = defaultdict(list)
    groups: dict[str, list[LedgerRow]] = defaultdict(list)

    def add(
        company: str,
        d: date,
        narration: str,
        utr: str,
        withdrawal: int | None,
        deposit: int | None,
        sources: list[LedgerRow],
        value: date | None = None,
    ) -> BankRow:
        b = BankRow(
            g.next_key(),
            g.group[company].bank_account,
            company,
            d,
            value or d,
            narration,
            utr,
            withdrawal,
            deposit,
        )
        g.bank_rows.append(b)
        for s in sources:
            bank_of[s.key].append(b.key)
        return b

    for row in sorted(g.rows, key=lambda x: (x.posted, x.key)):
        mode = row.bank
        posted = row.posted.date()
        outflow = row.drcr == "Dr"
        own = row.company
        counterparty = row.payee if outflow else row.payer
        if mode == "ledger_only":
            continue
        if mode in ("batched", "one_to_many"):
            groups[row.bank_info["group"]].append(row)
            continue
        if mode == "delayed":
            d = g.cal.next_working(posted, row.bank_info["delay"])
        elif outflow and mode in ("match", "payee_mismatch", "fee_adjusted"):
            d = g.cal.next_working(posted, r.choices((0, 1, 2), (70, 25, 5))[0])
        else:
            d = g.cal.next_working(posted)
        amount = row.amount + row.bank_info.get("fee", 0) if mode == "fee_adjusted" else row.amount
        utr = _utr(g, d)
        suffix = ""
        if mode == "payee_mismatch":
            employee = row.bank_info["beneficiary"]
            counterparty, suffix = employee.name, f"/A-C XX{employee.bank_account[-4:]}"
        elif outflow and row.cp_type == "vendor":
            vendor = next((v for v in g.vendors if v.vendor_id == row.cp_id), None)
            suffix = f"/A-C XX{vendor.bank_account[-4:]}" if vendor else ""
        add(
            own,
            d,
            _narration(row, utr, counterparty, suffix),
            utr,
            amount if outflow else None,
            None if outflow else amount,
            [row],
        )

    for _gid, members in sorted(groups.items()):
        first = members[0]
        d = g.cal.next_working(max(m.posted.date() for m in members), 1)
        utr = _utr(g, d)
        total = sum(m.amount for m in members)
        outflow = first.drcr == "Dr"
        counterparty = first.payee if outflow else first.payer
        add(
            first.company,
            d,
            f"{'NEFT DR' if outflow else 'RTGS CR'}/{utr}/{' '.join(counterparty.upper().split())[:32]}"
            f"/{len(members)} INVOICES",
            utr,
            total if outflow else None,
            None if outflow else total,
            members,
        )

    only = []
    for kind, d in g.special.get("bank_only", []):
        utr = _utr(g, d)
        if kind == "unexplained_debit":
            only.append(
                add(
                    "LVC",
                    d,
                    f"ACH DR/{utr}/SYN PAYMENT SERVICES MANDATE",
                    utr,
                    48_750_00 + r.randint(0, 9_999),
                    None,
                    [],
                )
            )
        else:
            only.append(add("LVC", d, f"SMS ALERT CHARGES Q/{utr}", utr, 1_770_00, None, []))
    g.special["bank_only_keys"] = [b.key for b in only]
    g.special["bank_of"] = bank_of


def finalize_bank(g: Gen) -> dict[str, int]:
    """Order rows per account, assign IDs and running balances. Returns the opening balance per company."""
    openings = {}
    for company in ("LVC", "LVE"):
        account = g.group[company].bank_account
        rows = sorted((b for b in g.bank_rows if b.account == account), key=lambda b: (b.date, b.key))
        running, low = 0, 0
        for b in rows:
            running += (b.deposit or 0) - (b.withdrawal or 0)
            low = min(low, running)
        opening = max(OPENING_FLOOR[company], ((-low) // 1_00_000_00 + 5) * 1_00_000_00)
        balance = opening
        for i, b in enumerate(rows, start=1):
            balance += (b.deposit or 0) - (b.withdrawal or 0)
            b.balance = balance
            b.txn_id = f"SB-{account[-4:]}-{i:06d}"
        openings[company] = opening
    return openings


def cashflow(g: Gen, openings: dict[str, int]) -> list[dict]:
    """Monthly opening, inflows, outflows and closing per company, straight from the bank statement."""
    out = []
    for company in ("LVC", "LVE"):
        account = g.group[company].bank_account
        balance = openings[company]
        for month in g.cal.months():
            end = g.cal.month_end(month)
            rows = [b for b in g.bank_rows if b.account == account and month <= b.date <= end]
            inflow = sum(b.deposit or 0 for b in rows)
            outflow = sum(b.withdrawal or 0 for b in rows)
            out.append(
                {
                    "month": month.strftime("%Y-%m"),
                    "company": company,
                    "opening": balance,
                    "inflows": inflow,
                    "outflows": outflow,
                    "closing": balance + inflow - outflow,
                    "transactions": len(rows),
                }
            )
            balance += inflow - outflow
    return out


def late_rows(g: Gen) -> list[BankRow]:
    return [b for b in g.bank_rows if b.date > g.p.end]


def clamp_to_period(g: Gen) -> None:
    """Statements stop at the period end: rows clearing later move to the last working day."""
    last = g.p.end
    while not g.cal.is_working(last):
        last -= timedelta(days=1)
    for b in late_rows(g):
        b.date = b.value_date = last
