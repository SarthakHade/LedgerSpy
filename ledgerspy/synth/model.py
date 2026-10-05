"""In-memory model of the synthetic company. Rows carry no scenario information: which rows belong to which
injected scenario is held only in `Scenario` records, which are written to the separate ground-truth
folder."""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass
class GroupEntity:
    code: str
    legal_name: str
    role: str
    address: str
    bank_account: str
    bank_code: str
    tax_id: str


@dataclass
class Vendor:
    vendor_id: str
    name: str
    category: str
    address: str
    city: str
    phone: str
    email: str
    bank_account: str
    bank_code: str
    tax_id: str
    onboarding_date: date
    status: str
    payment_terms_days: int
    created_by: str
    invoice_prefix: str
    created_by_ref: "Employee | None" = field(default=None, repr=False, compare=False)


@dataclass
class Employee:
    employee_id: str
    name: str
    department: str
    role: str
    location: str
    manager_id: str
    approval_limit_minor: int
    joined_on: date
    status: str
    phone: str
    email: str
    home_address: str
    bank_account: str
    bank_code: str
    tax_id: str
    manager_ref: "Employee | None" = field(default=None, repr=False, compare=False)


@dataclass
class Customer:
    customer_id: str
    name: str
    entity: str  # group entity that sells to it
    address: str
    city: str
    phone: str
    tax_id: str
    credit_terms_days: int
    onboarding_date: date


@dataclass
class Relationship:
    relationship_id: str
    entity_a: str
    entity_b: str
    relationship_type: str
    declared_on: date
    notes: str


@dataclass
class LedgerRow:
    key: int
    posted: datetime
    doc_date: date
    company: str
    gl: str
    gl_name: str
    payer: str
    payee: str
    cp_id: str
    cp_type: str
    description: str
    reference: str
    amount: int  # minor units, > 0
    drcr: str  # "Dr" payment/expense, "Cr" receipt/income
    mode: str
    approved_by: str
    posted_by: str
    normal: bool = True  # normal rows may be nudged by the collision guard; injected rows never
    bank: str = "match"  # how the bank statement settles it (see bank.py)
    bank_info: dict[str, Any] = field(default_factory=dict)
    txn_id: str = ""


@dataclass
class BankRow:
    key: int
    account: str
    company: str
    date: date
    value_date: date
    narration: str
    reference: str
    withdrawal: int | None
    deposit: int | None
    balance: int = 0
    txn_id: str = ""


@dataclass
class Scenario:
    scenario_id: str
    scenario_type: str
    letter: str
    description: str
    severity: str  # high | medium | low | none (controls)
    is_control: bool
    targets: list[str]
    phases: list[str]
    ledger_keys: list[int] = field(default_factory=list)
    bank_keys: list[int] = field(default_factory=list)
    vendor_ids: list[str] = field(default_factory=list)
    employee_ids: list[str] = field(default_factory=list)
    customer_ids: list[str] = field(default_factory=list)
    group_codes: list[str] = field(default_factory=list)
    parameters: dict[str, Any] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    stages: dict[str, list[int]] = field(default_factory=dict)  # optional named subsets of ledger_keys
    also_part_of: list[str] = field(default_factory=list)
