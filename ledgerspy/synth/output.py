"""Collision guard, ID assignment, and writing the dataset, ground truth and manifest.

Layout of an output directory:

    SYNTHETIC_manifest.json            parameters, file hashes, dataset hash (no ground truth content)
    SYNTHETIC_*.csv                    the inputs a detector may read (every row has is_synthetic=true)
    ground_truth/SYNTHETIC_ground_truth.json / .csv   scenario answers — for evaluation only
"""

import csv
import io
import json
import platform
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from ledgerspy.hashing import canonical_json, sha256_hex
from ledgerspy.normalize.transforms import collapse_whitespace
from ledgerspy.synth.context import Gen

GENERATOR_VERSION = "1"
GUARD_WINDOW_DAYS = 7  # wider than P4's near-duplicate window (3), so accidental pairs never appear
GROUND_TRUTH_DIR = "ground_truth"

LEDGER_COLUMNS = [
    "Txn ID",
    "Posting Date",
    "Document Date",
    "Company Code",
    "GL Account",
    "GL Account Name",
    "Payer",
    "Payee",
    "Counterparty ID",
    "Counterparty Type",
    "Description",
    "Invoice No",
    "Amount",
    "Dr/Cr",
    "Currency",
    "Payment Mode",
    "Approved By",
    "Posted By",
    "is_synthetic",
]
BANK_COLUMNS = [
    "Txn ID",
    "Account No",
    "Txn Date",
    "Value Dt",
    "Narration",
    "Ref No",
    "Withdrawal",
    "Deposit",
    "Balance",
    "Currency",
    "is_synthetic",
]


class GeneratorError(RuntimeError):
    pass


def money(minor: int | None) -> str:
    if minor is None:
        return ""
    sign = "-" if minor < 0 else ""
    minor = abs(minor)
    return f"{sign}{minor // 100}.{minor % 100:02d}"


# ----------------------------------------------------------------------------------------------- guard


def guard(g: Gen) -> int:
    """Remove accidental same-payee/amount/direction pairs within a week by nudging NORMAL rows by 37 paise.
    Two injected rows colliding without being an intended pair is a generator bug and raises."""
    nudges = 0
    for _ in range(50):
        groups: dict[tuple, list] = defaultdict(list)
        for row in g.rows:
            groups[(collapse_whitespace(row.payee).casefold(), row.amount, row.drcr)].append(row)
        changed = False
        for rows in groups.values():
            rows.sort(key=lambda x: (x.posted, x.key))
            for a, b in zip(rows, rows[1:], strict=False):
                close = abs((b.posted - a.posted).total_seconds()) <= GUARD_WINDOW_DAYS * 86400
                if not close or frozenset((a.key, b.key)) in g.allowed_pairs:
                    continue
                target = b if b.normal else a if a.normal else None
                if target is None:
                    raise GeneratorError(f"injected rows {a.key} and {b.key} collide unintentionally")
                target.amount += 37
                nudges, changed = nudges + 1, True
        if not changed:
            break
    refs = Counter(row.reference for row in g.rows)
    expected = {k for (_, check, k, _) in g.expected_p4 if check == "duplicate_source_id"}
    expected_refs = {row.reference for row in g.rows if row.key in expected}
    accidental = {ref for ref, n in refs.items() if n > 1 and ref not in expected_refs}
    if accidental:
        raise GeneratorError(f"unplanned duplicate references: {sorted(accidental)[:5]}")
    return nudges


def assign_ledger_ids(g: Gen) -> None:
    counters: Counter = Counter()
    for row in sorted(g.rows, key=lambda x: (x.posted, x.key)):
        counters[row.company] += 1
        row.txn_id = f"{row.company}-JV-{counters[row.company]:06d}"


# ----------------------------------------------------------------------------------------------- write


def _csv(header: list[str], rows: list[list[Any]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _files(g: Gen, cash: list[dict]) -> dict[str, bytes]:
    yes = "true"
    p = g.p
    name_to_id = {v.name: v.vendor_id for v in g.vendors} | {e.name: e.employee_id for e in g.employees}
    name_to_id |= {ge.legal_name: code for code, ge in g.group.items()}
    files = {
        "SYNTHETIC_group_entities.csv": _csv(
            [
                "entity_code",
                "legal_name",
                "role",
                "address",
                "bank_account",
                "bank_code",
                "tax_id",
                "expense_claim_approval_limit",
                "vendor_payment_approval_limit",
                "business_hours_start",
                "business_hours_end",
                "working_days",
                "currency",
                "is_synthetic",
            ],
            [
                [
                    ge.code,
                    ge.legal_name,
                    ge.role,
                    ge.address,
                    ge.bank_account,
                    ge.bank_code,
                    ge.tax_id,
                    money(p.expense_limit),
                    money(p.vendor_limit),
                    p.business_hours[0],
                    p.business_hours[1],
                    "Mon-Sat",
                    p.currency,
                    yes,
                ]
                for ge in g.group.values()
            ],
        ),
        "SYNTHETIC_vendor_master.csv": _csv(
            [
                "vendor_id",
                "vendor_name",
                "category",
                "address",
                "city",
                "phone",
                "email",
                "bank_account",
                "bank_code",
                "tax_id",
                "onboarding_date",
                "status",
                "payment_terms_days",
                "created_by",
                "is_synthetic",
            ],
            [
                [
                    v.vendor_id,
                    v.name,
                    v.category,
                    v.address,
                    v.city,
                    v.phone,
                    v.email,
                    v.bank_account,
                    v.bank_code,
                    v.tax_id,
                    v.onboarding_date.isoformat(),
                    v.status,
                    v.payment_terms_days,
                    v.created_by,
                    yes,
                ]
                for v in sorted(g.vendors, key=lambda v: v.vendor_id)
            ],
        ),
        "SYNTHETIC_employee_master.csv": _csv(
            [
                "employee_id",
                "name",
                "department",
                "role",
                "location",
                "manager_id",
                "approval_limit",
                "joined_on",
                "status",
                "phone",
                "email",
                "home_address",
                "bank_account",
                "bank_code",
                "tax_id",
                "is_synthetic",
            ],
            [
                [
                    e.employee_id,
                    e.name,
                    e.department,
                    e.role,
                    e.location,
                    e.manager_id,
                    money(e.approval_limit_minor),
                    e.joined_on.isoformat(),
                    e.status,
                    e.phone,
                    e.email,
                    e.home_address,
                    e.bank_account,
                    e.bank_code,
                    e.tax_id,
                    yes,
                ]
                for e in sorted(g.employees, key=lambda e: e.employee_id)
            ],
        ),
        "SYNTHETIC_customer_master.csv": _csv(
            [
                "customer_id",
                "customer_name",
                "sells_to_entity",
                "address",
                "city",
                "phone",
                "tax_id",
                "credit_terms_days",
                "onboarding_date",
                "is_synthetic",
            ],
            [
                [
                    c.customer_id,
                    c.name,
                    c.entity,
                    c.address,
                    c.city,
                    c.phone,
                    c.tax_id,
                    c.credit_terms_days,
                    c.onboarding_date.isoformat(),
                    yes,
                ]
                for c in sorted(g.customers, key=lambda c: c.customer_id)
            ],
        ),
        "SYNTHETIC_relationships.csv": _csv(
            [
                "relationship_id",
                "entity_a",
                "entity_b",
                "relationship_type",
                "declared_on",
                "notes",
                "is_synthetic",
            ],
            [
                [
                    rel.relationship_id,
                    name_to_id.get(rel.entity_a, rel.entity_a),
                    name_to_id.get(rel.entity_b, rel.entity_b),
                    rel.relationship_type,
                    rel.declared_on.isoformat(),
                    rel.notes,
                    yes,
                ]
                for rel in g.relationships
            ],
        ),
        "SYNTHETIC_holiday_calendar.csv": _csv(
            ["date", "holiday", "is_synthetic"],
            [[d.isoformat(), n, yes] for d, n in sorted(g.cal.holidays.items())],
        ),
        "SYNTHETIC_ledger.csv": _csv(
            LEDGER_COLUMNS,
            [
                [
                    x.txn_id,
                    x.posted.strftime("%Y-%m-%d %H:%M:%S"),
                    x.doc_date.isoformat(),
                    x.company,
                    x.gl,
                    x.gl_name,
                    x.payer,
                    x.payee,
                    x.cp_id,
                    x.cp_type,
                    x.description,
                    x.reference,
                    money(x.amount),
                    x.drcr,
                    p.currency,
                    x.mode,
                    x.approved_by,
                    x.posted_by,
                    yes,
                ]
                for x in sorted(g.rows, key=lambda x: (x.posted, x.key))
            ],
        ),
        "SYNTHETIC_bank_statement.csv": _csv(
            BANK_COLUMNS,
            [
                [
                    b.txn_id,
                    b.account,
                    b.date.isoformat(),
                    b.value_date.isoformat(),
                    b.narration,
                    b.reference,
                    money(b.withdrawal),
                    money(b.deposit),
                    money(b.balance),
                    p.currency,
                    yes,
                ]
                for b in sorted(g.bank_rows, key=lambda b: (b.account, b.date, b.key))
            ],
        ),
        "SYNTHETIC_cashflow_monthly.csv": _csv(
            [
                "month",
                "company_code",
                "opening_balance",
                "inflows",
                "outflows",
                "closing_balance",
                "transactions",
                "is_synthetic",
            ],
            [
                [
                    c["month"],
                    c["company"],
                    money(c["opening"]),
                    money(c["inflows"]),
                    money(c["outflows"]),
                    money(c["closing"]),
                    c["transactions"],
                    yes,
                ]
                for c in cash
            ],
        ),
    }
    return files


def ground_truth(g: Gen) -> dict[str, Any]:
    by_key = {x.key: x for x in g.rows}
    bank_by_key = {b.key: b for b in g.bank_rows}
    bank_of = g.special.get("bank_of", {})
    scenarios = []
    for s in sorted(g.scenarios, key=lambda s: s.scenario_id):
        ledger_ids = sorted({by_key[k].txn_id for k in s.ledger_keys})
        bank_keys = list(s.bank_keys)
        if s.letter == "S":
            bank_keys += [b for k in s.ledger_keys for b in bank_of.get(k, [])]
        if s.scenario_id == "S-S-02":
            bank_keys += g.special.get("bank_only_keys", [])
        dates = sorted(by_key[k].posted for k in s.ledger_keys)
        # every counterparty of an affected row is an affected entity, besides the ones the scenario names
        parties: dict[str, set[str]] = {"vendor": set(), "employee": set(), "customer": set(), "group": set()}
        for k in s.ledger_keys:
            if by_key[k].cp_type in parties:
                parties[by_key[k].cp_type].add(by_key[k].cp_id)
        scenarios.append(
            {
                "scenario_id": s.scenario_id,
                "scenario_type": s.scenario_type,
                "category": s.letter,
                "is_control": s.is_control,
                "severity": s.severity,
                "description": s.description,
                "expected_detection": {"targets": s.targets, "phases": s.phases},
                "affected": {
                    "ledger_txn_ids": ledger_ids,
                    "bank_txn_ids": sorted({bank_by_key[k].txn_id for k in bank_keys}),
                    "vendor_ids": sorted(set(s.vendor_ids) | parties["vendor"]),
                    "employee_ids": sorted(set(s.employee_ids) | parties["employee"]),
                    "customer_ids": sorted(set(s.customer_ids) | parties["customer"]),
                    "group_entities": sorted(set(s.group_codes) | parties["group"]),
                },
                "stages": {name: sorted(by_key[k].txn_id for k in keys) for name, keys in s.stages.items()},
                "injection_parameters": s.parameters,
                "start_date": dates[0].date().isoformat() if dates else None,
                "end_date": dates[-1].date().isoformat() if dates else None,
                "explanation_evidence": s.evidence,
                "also_part_of": s.also_part_of,
            }
        )
    expected: dict[str, dict[str, list[dict[str, str]]]] = {"ledger": {}, "bank": {}}
    for dataset, check, key, sid in sorted(g.expected_p4, key=lambda t: (t[0], t[1], by_key[t[2]].txn_id)):
        expected[dataset].setdefault(check, []).append({"txn_id": by_key[key].txn_id, "scenario_id": sid})
    return {
        "synthetic": True,
        "purpose": "Evaluation only. Detectors must never read this file.",
        "generator_version": GENERATOR_VERSION,
        "parameters": g.p.as_dict(),
        "scenario_count": len(scenarios),
        "injected_scenarios": sum(1 for s in scenarios if not s["is_control"]),
        "control_scenarios": sum(1 for s in scenarios if s["is_control"]),
        "scenarios": scenarios,
        "expected_p4_row_issues": expected,
    }


def ground_truth_csv(gt: dict[str, Any]) -> bytes:
    rows = []
    for s in gt["scenarios"]:
        aff = s["affected"]
        for kind, key in (
            ("ledger_txn", "ledger_txn_ids"),
            ("bank_txn", "bank_txn_ids"),
            ("vendor", "vendor_ids"),
            ("employee", "employee_ids"),
            ("customer", "customer_ids"),
            ("group_entity", "group_entities"),
        ):
            rows += [
                [s["scenario_id"], s["scenario_type"], str(s["is_control"]).lower(), kind, ident]
                for ident in aff[key]
            ]
    return _csv(["scenario_id", "scenario_type", "is_control", "record_kind", "record_id"], rows)


def write(g: Gen, cash: list[dict], out: Path, force: bool = False) -> dict[str, Any]:
    out = Path(out)
    existing = [p for p in out.glob("SYNTHETIC_*")] + list((out / GROUND_TRUTH_DIR).glob("SYNTHETIC_*"))
    if existing and not force:
        raise GeneratorError(f"{out} already contains a synthetic dataset; use --force to replace it")
    out.mkdir(parents=True, exist_ok=True)
    (out / GROUND_TRUTH_DIR).mkdir(exist_ok=True)
    files = _files(g, cash)
    gt = ground_truth(g)
    gt_files = {
        f"{GROUND_TRUTH_DIR}/SYNTHETIC_ground_truth.json": (
            json.dumps(gt, indent=2, ensure_ascii=False) + "\n"
        ).encode(),
        f"{GROUND_TRUTH_DIR}/SYNTHETIC_ground_truth.csv": ground_truth_csv(gt),
    }
    every = files | gt_files
    for name, data in every.items():
        (out / name).write_bytes(data)
    hashes = {name: sha256_hex(data) for name, data in sorted(every.items())}
    manifest = {
        "synthetic": True,
        "notice": "SYNTHETIC: a fictional company. No real person, company, address, phone or account "
        "is used.",
        "generator": "ledgerspy.synth",
        "generator_version": GENERATOR_VERSION,
        "parameters": g.p.as_dict(),
        "python": platform.python_version(),
        "files": {
            name: {
                "sha256": hashes[name],
                "bytes": len(every[name]),
                "rows": every[name].count(b"\n") - 1 if name.endswith(".csv") else None,
            }
            for name in sorted(every)
        },
        "inputs": sorted(files),
        "ground_truth": sorted(gt_files),
        "dataset_sha256": sha256_hex(canonical_json(hashes)),
        "scenario_count": gt["scenario_count"],
    }
    (out / "SYNTHETIC_manifest.json").write_bytes((json.dumps(manifest, indent=2) + "\n").encode())
    return manifest
