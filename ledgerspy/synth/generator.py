"""`generate(params)` builds the whole synthetic company in memory; `generate_to(...)` also writes it."""

from datetime import date
from pathlib import Path
from typing import Any

from ledgerspy.config.schema import SynthConfig
from ledgerspy.synth import bank, entities, ledger, output, scenarios
from ledgerspy.synth.context import Gen, Params
from ledgerspy.synth.model import Relationship


def params_from(cfg: SynthConfig, **overrides: Any) -> Params:
    values = {
        "seed": cfg.seed,
        "start": date.fromisoformat(cfg.start_date),
        "years": cfg.years,
        "scale": cfg.scale,
        "currency": cfg.currency,
        "expense_limit": cfg.approval_limits.expense_claim_minor,
        "vendor_limit": cfg.approval_limits.vendor_payment_minor,
        "business_hours": tuple(cfg.business_hours),
    }
    values.update({k: v for k, v in overrides.items() if v is not None})
    if not 2 <= values["years"] <= 5:
        raise ValueError("years must be between 2 and 5")
    if values["scale"] <= 0:
        raise ValueError("scale must be positive")
    return Params(**values)


def generate(p: Params) -> tuple[Gen, list[dict]]:
    g = Gen(p)
    entities.build_group(g)
    entities.build_employees(g)
    entities.build_vendors(g)
    entities.build_customers(g)
    scenarios.register_entities(g)
    entities.assign_ids(g)
    g.relationships.insert(
        0,
        Relationship(
            "",
            g.group["LVC"].legal_name,
            g.group["LVE"].legal_name,
            "parent_subsidiary_100pct",
            date(p.start.year - 6, 4, 1),
            "Wholly owned subsidiary",
        ),
    )
    for i, rel in enumerate(g.relationships, start=1):
        rel.relationship_id = f"R{i:03d}"
    ledger.normal_flows(g)
    scenarios.inject(g)
    g.special["guard_nudges"] = output.guard(g)
    output.assign_ledger_ids(g)
    bank.build_bank(g)
    bank.clamp_to_period(g)
    openings = bank.finalize_bank(g)
    return g, bank.cashflow(g, openings)


def generate_to(p: Params, out: Path, force: bool = False) -> dict[str, Any]:
    g, cash = generate(p)
    return output.write(g, cash, out, force)
