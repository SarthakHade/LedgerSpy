"""Generation context: parameters, calendar, random streams, entities, rows and scenario records."""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from ledgerspy.synth.calendar import Calendar
from ledgerspy.synth.model import (
    BankRow,
    Customer,
    Employee,
    GroupEntity,
    LedgerRow,
    Relationship,
    Scenario,
    Vendor,
)
from ledgerspy.synth.names import NameBook
from ledgerspy.synth.rng import stream


@dataclass(frozen=True)
class Params:
    seed: int
    start: date
    years: int
    scale: float
    currency: str
    expense_limit: int
    vendor_limit: int
    business_hours: tuple[str, str]

    @property
    def end(self) -> date:
        return date(self.start.year + self.years, self.start.month, self.start.day) - timedelta(days=1)

    def as_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "start_date": self.start.isoformat(),
            "end_date": self.end.isoformat(),
            "years": self.years,
            "scale": self.scale,
            "currency": self.currency,
            "approval_limits_minor": {
                "expense_claim": self.expense_limit,
                "vendor_payment": self.vendor_limit,
            },
            "business_hours": list(self.business_hours),
        }


@dataclass
class Gen:
    p: Params
    cal: Calendar = field(init=False)
    names: NameBook = field(init=False)
    group: dict[str, GroupEntity] = field(default_factory=dict)
    vendors: list[Vendor] = field(default_factory=list)
    employees: list[Employee] = field(default_factory=list)
    customers: list[Customer] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)
    rows: list[LedgerRow] = field(default_factory=list)
    bank_rows: list[BankRow] = field(default_factory=list)
    scenarios: list[Scenario] = field(default_factory=list)
    roles: dict[str, Employee] = field(default_factory=dict)
    special: dict[str, Any] = field(default_factory=dict)  # scenario entities, by internal name
    vendor_profile: dict[str, dict[str, Any]] = field(default_factory=dict)  # by vendor name (pre-ID)
    invoice_seq: dict[str, int] = field(default_factory=dict)
    sales_seq: dict[str, int] = field(default_factory=dict)
    expected_p4: list[tuple[str, str, int, str]] = field(
        default_factory=list
    )  # (dataset, check, key, scenario)
    allowed_pairs: set[frozenset[int]] = field(default_factory=set)  # intended same-payee/amount pairs
    _key: int = 0

    def __post_init__(self) -> None:
        self.cal = Calendar(self.p.start, self.p.end, list(self.p.business_hours))
        self.names = NameBook(self.rng("names"))

    def rng(self, name: str):
        return stream(self.p.seed, name)

    def next_key(self) -> int:
        self._key += 1
        return self._key

    # ------------------------------------------------------------------ entity lookup

    def vendor(self, name: str) -> Vendor:
        return next(v for v in self.vendors if v.name == name)

    def employee(self, role: str) -> Employee:
        return self.roles[role]

    # ------------------------------------------------------------------ rows

    def payment(
        self,
        *,
        company: str,
        vendor: Vendor,
        posted: datetime,
        amount: int,
        gl: tuple[str, str],
        description: str,
        reference: str | None = None,
        doc_date: date | None = None,
        mode: str | None = None,
        approved_by: str | None = None,
        posted_by: str | None = None,
        normal: bool = True,
    ) -> LedgerRow:
        """A vendor payment booked in `company`'s books."""
        reference = reference or self.next_invoice(vendor)
        doc_date = doc_date or (posted.date() - timedelta(days=max(1, vendor.payment_terms_days)))
        if approved_by is None:
            approved_by = (
                self.roles["cfo"] if amount >= self.p.vendor_limit else self.roles["ap_manager"]
            ).employee_id
        row = LedgerRow(
            key=self.next_key(),
            posted=posted,
            doc_date=doc_date,
            company=company,
            gl=gl[0],
            gl_name=gl[1],
            payer=self.group[company].legal_name,
            payee=vendor.name,
            cp_id=vendor.vendor_id,
            cp_type="vendor",
            description=description,
            reference=reference,
            amount=amount,
            drcr="Dr",
            mode=mode or ("RTGS" if amount >= 20_000_000 else "NEFT"),
            approved_by=approved_by,
            posted_by=posted_by or self.roles["ap_clerk_1"].employee_id,
            normal=normal,
        )
        self.rows.append(row)
        return row

    def receipt(
        self,
        *,
        company: str,
        payer_name: str,
        payer_id: str,
        payer_type: str,
        posted: datetime,
        amount: int,
        gl: tuple[str, str],
        description: str,
        reference: str | None = None,
        doc_date: date | None = None,
        mode: str = "NEFT",
        normal: bool = True,
    ) -> LedgerRow:
        reference = reference or self.next_sales_invoice(company, posted.date())
        row = LedgerRow(
            key=self.next_key(),
            posted=posted,
            doc_date=doc_date or posted.date() - timedelta(days=30),
            company=company,
            gl=gl[0],
            gl_name=gl[1],
            payer=payer_name,
            payee=self.group[company].legal_name,
            cp_id=payer_id,
            cp_type=payer_type,
            description=description,
            reference=reference,
            amount=amount,
            drcr="Cr",
            mode=mode,
            approved_by="",
            posted_by=self.roles["finance_clerk"].employee_id,
            normal=normal,
        )
        self.rows.append(row)
        return row

    def claim(
        self,
        *,
        employee: Employee,
        posted: datetime,
        amount: int,
        description: str,
        gl: tuple[str, str],
        approver: Employee | None = None,
        normal: bool = True,
    ) -> LedgerRow:
        seq = self.invoice_seq.get(employee.employee_id, 0) + 1
        self.invoice_seq[employee.employee_id] = seq
        if approver is None:
            approver = self.roles["cfo"] if amount >= self.p.expense_limit else self.manager_of(employee)
        row = LedgerRow(
            key=self.next_key(),
            posted=posted,
            doc_date=posted.date() - timedelta(days=3),
            company="LVC",
            gl=gl[0],
            gl_name=gl[1],
            payer=self.group["LVC"].legal_name,
            payee=employee.name,
            cp_id=employee.employee_id,
            cp_type="employee",
            description=description,
            reference=f"EXP/{employee.employee_id}/{seq:04d}",
            amount=amount,
            drcr="Dr",
            mode="NEFT",
            approved_by=approver.employee_id,
            posted_by=self.roles["finance_clerk"].employee_id,
            normal=normal,
        )
        self.rows.append(row)
        return row

    def manager_of(self, e: Employee) -> Employee:
        return next((m for m in self.employees if m.employee_id == e.manager_id), self.roles["cfo"])

    def next_invoice(self, vendor: Vendor) -> str:
        step_rng = self.rng(f"invseq:{vendor.name}:{self.invoice_seq.get(vendor.name, 0)}")
        seq = self.invoice_seq.get(vendor.name, step_rng.randint(100, 900)) + step_rng.randint(1, 6)
        self.invoice_seq[vendor.name] = seq
        return f"{vendor.invoice_prefix}/{seq:05d}"

    def next_sales_invoice(self, company: str, d: date) -> str:
        seq = self.sales_seq.get(company, 0) + 1
        self.sales_seq[company] = seq
        fy = d.year if d.month >= 4 else d.year - 1
        return f"{company}/SI/{fy % 100:02d}{(fy + 1) % 100:02d}/{seq:05d}"

    def scenario(self, **kw: Any) -> Scenario:
        s = Scenario(**kw)
        self.scenarios.append(s)
        return s

    def expect(self, dataset: str, check: str, row: LedgerRow | BankRow, scenario: Scenario) -> None:
        """Record a P4 integrity issue that this injection is intended to cause (asserted by the tests)."""
        self.expected_p4.append((dataset, check, row.key, scenario.scenario_id))
