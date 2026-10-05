"""Normal entities of the fictional group: two companies, employees, vendors and customers.

IDs (E…, V…, C…) are assigned after all entities, including the ones scenarios need, exist: employees by
joining
date and vendors/customers by onboarding date. An ID therefore says nothing about why an entity exists.
"""

from datetime import date, timedelta

from ledgerspy.synth.context import Gen
from ledgerspy.synth.model import Customer, Employee, GroupEntity, Vendor
from ledgerspy.synth.names import core
from ledgerspy.synth.output import GeneratorError

# category → (GL account, median ₹, sigma, payments per month, seasonal month factors, payment terms days)
PROFILES = {
    "raw_materials": (
        ("5100", "Raw Materials"),
        180_000,
        0.45,
        2.6,
        {10: 1.35, 11: 1.35, 12: 1.3, 3: 1.15},
        45,
    ),
    "packaging": (("5110", "Packing Materials"), 45_000, 0.40, 2.0, {10: 1.25, 11: 1.25, 12: 1.2}, 30),
    "logistics": (("5200", "Freight & Logistics"), 28_000, 0.55, 3.5, {10: 1.3, 11: 1.3, 12: 1.2}, 15),
    "utilities": (("5300", "Power & Utilities"), 95_000, 0.18, 1.0, {4: 1.3, 5: 1.35, 6: 1.25}, 10),
    "it_services": (("5400", "IT Services"), 65_000, 0.30, 1.0, {}, 30),
    "facilities": (("5500", "Facility Services"), 38_000, 0.45, 1.2, {}, 30),
    "office_supplies": (("5600", "Office Supplies"), 9_000, 0.55, 1.5, {}, 15),
    "professional": (("5700", "Professional Fees"), 120_000, 0.50, 0.35, {3: 2.0}, 30),
    "staffing": (("5800", "Contract Staffing"), 240_000, 0.12, 1.0, {}, 30),
    "maintenance": (("5510", "Repairs & Maintenance"), 52_000, 0.65, 0.8, {}, 30),
}
GL_CAPEX = ("1500", "Capital Work in Progress")
GL_RENT = ("6200", "Rent")
GL_SALES = ("4000", "Sales")
GL_EXPORT = ("4010", "Export Sales")
GL_TRAVEL = ("6100", "Travel & Conveyance")
GL_WELFARE = ("6110", "Staff Welfare")
GL_PAYROLL = ("7000", "Salaries & Wages")
GL_STATUTORY = ("2400", "Statutory Dues Payable")
GL_BANK = ("8100", "Bank Charges")
GL_INTERCO = ("1800", "Intercompany Account")
GL_ADVANCE = ("2300", "Customer Advances")

# (role key, department, title, reports to role key, approval limit ₹)
KEY_STAFF = [
    ("cfo", "Finance", "Chief Financial Officer", None, 5_000_000),
    ("ap_manager", "Finance", "Accounts Payable Manager", "cfo", 200_000),
    ("ap_clerk_1", "Finance", "Accounts Payable Executive", "ap_manager", 0),
    ("ap_clerk_2", "Finance", "Accounts Payable Executive", "ap_manager", 0),
    ("finance_clerk", "Finance", "Finance Executive", "cfo", 0),
    ("accountant", "Finance", "Accountant", "cfo", 0),
    ("procurement_head", "Procurement", "Head of Procurement", "cfo", 500_000),
    ("buyer_1", "Procurement", "Buyer", "procurement_head", 0),
    ("buyer_2", "Procurement", "Buyer", "procurement_head", 0),
    ("ops_manager", "Operations", "Plant Operations Manager", "cfo", 100_000),
    ("sales_head", "Sales", "Head of Sales", "cfo", 100_000),
    ("hr_head", "HR", "HR Manager", "cfo", 50_000),
    ("it_manager", "IT", "IT Manager", "cfo", 100_000),
    ("admin_exec", "Admin", "Administration Executive", "hr_head", 0),
]
STAFF_POOLS = [
    ("Operations", "Production Engineer", "ops_manager", 10),
    ("Sales", "Sales Executive", "sales_head", 7),
    ("Operations", "Shift Supervisor", "ops_manager", 4),
    ("HR", "HR Executive", "hr_head", 1),
    ("IT", "Systems Engineer", "it_manager", 1),
]


def build_group(g: Gen) -> None:
    n = g.names
    hq = ("Veltara", "MH")
    for code, legal, role in (
        ("LVC", "Larkspur Vale Components Pvt Ltd", "parent"),
        ("LVE", "Larkspur Vale Exports Pvt Ltd", "subsidiary"),
    ):
        n.take(legal)
        g.group[code] = GroupEntity(
            code, legal, role, n.business_address(hq), n.bank_account(), n.bank_code(), n.tax_id()
        )


def new_employee(
    g: Gen, department: str, role: str, manager: Employee | None, limit_rupees: int, joined: date
) -> Employee:
    n, r = g.names, g.rng(f"employee:{len(g.employees)}")
    city = ("Veltara", "MH") if r.random() < 0.85 else n.city()
    name = n.person()
    e = Employee(
        employee_id="",
        name=name,
        department=department,
        role=role,
        location=city[0],
        manager_id="",
        approval_limit_minor=limit_rupees * 100,
        joined_on=joined,
        status="active",
        phone=n.phone(),
        email=n.email(name, "larkspurvale"),
        home_address=n.home_address(city),
        bank_account=n.bank_account(),
        bank_code=n.bank_code(),
        tax_id=n.tax_id(),
        manager_ref=manager,
    )
    g.employees.append(e)
    return e


def build_employees(g: Gen) -> None:
    r = g.rng("employees")
    for key, dept, title, boss, limit in KEY_STAFF:
        joined = g.p.start - timedelta(days=r.randint(400, 3600))
        g.roles[key] = new_employee(g, dept, title, g.roles.get(boss) if boss else None, limit, joined)
    for dept, title, boss, count in STAFF_POOLS:
        # small scales keep up to 5 per pool: scenarios and controls need distinct sales executives
        for _ in range(max(min(count, 5), round(count * g.p.scale))):
            joined = g.p.start - timedelta(days=r.randint(60, 3000))
            new_employee(g, dept, title, g.roles[boss], 0, joined)


def new_vendor(
    g: Gen,
    category: str,
    *,
    name: str | None = None,
    onboarded: date,
    city=None,
    terms: int | None = None,
    created_by: Employee | None = None,
    status: str = "active",
) -> Vendor:
    n = g.names
    city = city or n.city()
    if name is not None and core(name) in n.cores:
        raise GeneratorError(f"vendor name already in use (up to its legal form): {name}")
    name = n.take(name) if name is not None else n.company(category)
    v = Vendor(
        vendor_id="",
        name=name,
        category=category,
        address=n.business_address(city),
        city=city[0],
        phone=n.phone(),
        email=n.email("accounts desk", name),
        bank_account=n.bank_account(),
        bank_code=n.bank_code(),
        tax_id=n.tax_id(),
        onboarding_date=onboarded,
        status=status,
        payment_terms_days=terms if terms is not None else PROFILES.get(category, (None,) * 6)[5] or 30,
        created_by="",
        invoice_prefix=n.invoice_prefix(name),
        created_by_ref=created_by or g.roles["procurement_head"],
    )
    g.vendors.append(v)
    return v


def build_vendors(g: Gen) -> None:
    r = g.rng("vendors")
    # At least 6 per category whatever the scale: isolated scenarios each need their own normal vendor.
    per = max(6, round(6 * g.p.scale))
    for category, (_, median, sigma, rate, season, _) in PROFILES.items():
        for i in range(per):
            onboarded = g.p.start - timedelta(days=r.randint(200, 2800))
            v = new_vendor(g, category, onboarded=onboarded)
            g.vendor_profile[v.name] = {
                "median": round(median * 100 * r.lognormvariate(0, 0.35)),
                "sigma": sigma * r.uniform(0.8, 1.2),
                "rate": rate * r.uniform(0.7, 1.3),
                "season": season,
                "category": category,
                "churn": (g.p.start + timedelta(days=r.randint(300, 900)))
                if r.random() < 0.08 and i >= 4
                else None,
            }


def new_customer(
    g: Gen, entity: str, *, name: str | None = None, onboarded: date, city=None, terms: int = 45
) -> Customer:
    n = g.names
    city = city or n.city()
    name = n.take(name) if name is not None else n.company("customer")  # a given name: the same firm's vendor
    c = Customer("", name, entity, n.business_address(city), city[0], n.phone(), n.tax_id(), terms, onboarded)
    g.customers.append(c)
    return c


def build_customers(g: Gen) -> None:
    r = g.rng("customers")
    for entity, count in (("LVC", 16), ("LVE", 6)):
        for _ in range(max(min(count, 12), round(count * g.p.scale))):  # fan-in control needs many payers
            c = new_customer(
                g,
                entity,
                onboarded=g.p.start - timedelta(days=r.randint(100, 2500)),
                terms=r.choice([30, 45, 60]),
            )
            g.special.setdefault("normal_customers", []).append(c)


def assign_ids(g: Gen) -> None:
    for i, e in enumerate(sorted(g.employees, key=lambda e: (e.joined_on, e.name)), start=1):
        e.employee_id = f"E{i:04d}"
    for e in g.employees:  # references were captured before IDs existed
        e.manager_id = e.manager_ref.employee_id if e.manager_ref else ""
    for i, v in enumerate(sorted(g.vendors, key=lambda v: (v.onboarding_date, v.name)), start=1):
        v.vendor_id = f"V{i:04d}"
        v.created_by = v.created_by_ref.employee_id if v.created_by_ref else ""
    for i, c in enumerate(sorted(g.customers, key=lambda c: (c.onboarding_date, c.name)), start=1):
        c.customer_id = f"C{i:04d}"
