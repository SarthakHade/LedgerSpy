"""What features may read.

`FeatureRow` holds only canonical fields of the normalised view (P3 rows + logged P4 transformations),
reduced to the forms the definitions need. P12 adds the case's imported master records (`masters`: raw
mapped values of vendor / customer / employee / group-company masters, stored as evidence), read only by the
entity-resolution features. It has no `extra`, no labels, and no access to any file: label columns are split
off at ingestion (P3), unmapped columns stay in `extra`, and P5 ground truth is never an input. So no
feature can read a label even by accident; the tests prove the matrix is unchanged when labels or extra
columns change.

Dataset capabilities decide which features apply. A feature whose requirements are not met is reported
`not_available` for that dataset with the reason, and gets no column.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from ledgerspy.config.schema import FeaturesConfig

_ZERO_WIDTH = re.compile("[​‌‍⁠﻿]")
_SPACE = re.compile(r"\s+")


def party_key(name: str | None) -> str | None:
    """A light, deterministic name key: NFKC, zero-width characters removed, whitespace collapsed, casefolded.

    This is not entity resolution (P12): "Javrel  Office Supplies" and "Javrel Office Supplies" share a key,
    "Javrel Office Supplies Pvt Ltd" and "Javrel Office Supplies Private Limited" do not.
    """
    if name is None:
        return None
    key = _SPACE.sub(" ", _ZERO_WIDTH.sub("", unicodedata.normalize("NFKC", name))).strip().casefold()
    return key or None


@dataclass(frozen=True)
class FeatureRow:
    i: int  # position in the input (matrix row order = input order)
    txn_id: str
    source_row: int
    ts: datetime | None  # posting timestamp; orders history (simulation steps order too)
    amount: int | None  # minor units
    flow: str | None  # "out" (debit/out) or "in" (credit/in)
    payer: str | None  # party keys
    payee: str | None
    cp: str | None  # counterparty key (adapter rule, see config features.adapters)
    account: str | None
    txn_type: str | None
    document_date: date | None
    reference: str | None
    normalized: bool  # at least one field comes from a logged P4 transformation


# Capabilities a feature can require. Each is decided from the dataset and configuration, never assumed.
CAPABILITIES = {
    "amount": "an amount field is mapped",
    "direction": "a direction is known (column, debit/credit pair, declaration or adapter)",
    "timestamp": "posting timestamps exist (real or simulated); they order the point-in-time history",
    "calendar_time": "posting timestamps are real calendar dates (not a simulation clock)",
    "time_of_day": "posting timestamps carry a real time of day",
    "counterparty": "the counterparty of each row is known (payer/payee names)",
    "payer_payee": "both payer and payee are mapped",
    "account_category": "the account field is a GL/category code (a peer group), not an account holder",
    "txn_type": "a transaction type is mapped",
    "document_date": "a document (invoice) date is mapped",
    "reference": "a reference (invoice number) is mapped",
    "holidays": "a holiday calendar is configured for the case",
    "approval_thresholds": "approval thresholds are configured and apply (corporate ledger)",
    "p4_current": "a P4 integrity run exists for the current data version",
    "running_balance": "the current P4 run checked a running-balance column (balance_continuity ran)",
    "master_data": "master records (vendor / customer / employee / group company) are imported for the case",
}


@dataclass
class FeatureInput:
    dataset_id: str
    adapter: str
    time_basis: str
    data_version: str
    rows: list[FeatureRow]
    capabilities: dict[str, str | None]  # capability → None if available, else the reason it is not
    cfg: FeaturesConfig
    # P4 issues of the current data version: txn_id → list of check codes (one entry per issue).
    p4_issues: dict[str, list[str]] = field(default_factory=dict)
    p4_run_id: str | None = None
    # Status of each check in that run: check → {"status": ok|issues|not_available, "reason": …}.
    p4_checks: dict[str, dict[str, str]] = field(default_factory=dict)
    # P12: the case's imported master records (ledgerspy.resolution.records.MasterRecord), in a fixed order.
    masters: tuple = ()
    cache: dict[str, Any] = field(default_factory=dict)

    def available(self, requires: tuple[str, ...]) -> str | None:
        """None if every requirement holds, else the first reason it does not."""
        for cap in requires:
            reason = self.capabilities.get(cap, f"unknown capability {cap}")
            if reason is not None:
                return reason
        return None

    def memo(self, key: str, build) -> Any:
        if key not in self.cache:
            self.cache[key] = build()
        return self.cache[key]


def _flow(direction: str | None) -> str | None:
    if direction in ("debit", "out"):
        return "out"
    if direction in ("credit", "in"):
        return "in"
    return None


def _ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value)
    return (
        datetime.fromisoformat(text)
        if "T" in text or " " in text
        else datetime.fromisoformat(text + "T00:00")
    )


def _date(value: Any) -> date | None:
    if value is None:
        return None
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def rows_from_records(records: list[dict[str, Any]], counterparty_rule: str) -> list[FeatureRow]:
    """Canonical records (normalised view rows) → feature rows. Only canonical fields are read."""
    out = []
    for i, r in enumerate(records):
        flow = _flow(r.get("direction"))
        payer, payee = party_key(r.get("payer_raw")), party_key(r.get("payee_raw"))
        if counterparty_rule == "payee":
            cp = payee
        else:
            cp = payee if flow == "out" else payer if flow == "in" else None
        out.append(
            FeatureRow(
                i=i,
                txn_id=r["txn_id"],
                source_row=r["source_row"],
                ts=_ts(r.get("posted_at")),
                amount=r.get("amount_minor"),
                flow=flow,
                payer=payer,
                payee=payee,
                cp=cp,
                account=r.get("account"),
                txn_type=r.get("txn_type"),
                document_date=_date(r.get("document_date")),
                reference=r.get("reference"),
                normalized=bool(r.get("normalized_fields")),
            )
        )
    return out


def capabilities(
    *,
    adapter: str,
    time_basis: str,
    mapped: set[str],
    rows: list[FeatureRow],
    cfg: FeaturesConfig,
    p4_current: bool,
    p4_checks: dict[str, dict[str, str]] | None = None,
    masters: bool = False,
) -> dict[str, str | None]:
    profile = cfg.adapters.get(adapter)
    caps: dict[str, str | None] = {}

    def need(cap: str, ok: bool, reason: str) -> None:
        caps[cap] = None if ok else reason

    need("amount", "amount_minor" in mapped, "no amount field is mapped")
    need("direction", any(r.flow for r in rows), "no row has a known direction")
    need("timestamp", "posted_at" in mapped and any(r.ts for r in rows), "no posting timestamps")
    real = time_basis in ("date", "datetime")
    need(
        "calendar_time",
        real and caps["timestamp"] is None,
        f"time basis is '{time_basis}': steps of a simulation are not calendar time"
        if not real
        else "no posting timestamps",
    )
    need(
        "time_of_day",
        time_basis == "datetime" and caps["timestamp"] is None,
        "posting timestamps have no real time of day"
        + (f" (time basis '{time_basis}')" if time_basis else ""),
    )
    need("counterparty", any(r.cp for r in rows), "no payer/payee names to identify counterparties")
    need("payer_payee", {"payer_raw", "payee_raw"} <= mapped, "payer and payee are not both mapped")
    need(
        "account_category",
        profile is not None and profile.account_is_category and "account" in mapped,
        "the account field is not a GL/category code for this adapter"
        if "account" in mapped
        else "no account field is mapped",
    )
    need("txn_type", "txn_type" in mapped, "no transaction type is mapped")
    need("document_date", "document_date" in mapped, "no document date is mapped")
    need("reference", "reference" in mapped, "no reference (invoice number) is mapped")
    need("holidays", bool(cfg.calendar.holidays), "no holiday calendar is configured for this case")
    need(
        "approval_thresholds",
        bool(cfg.approval_thresholds) and profile is not None and profile.corporate,
        "approval thresholds do not apply to this adapter (not a corporate ledger)"
        if profile is not None and not profile.corporate
        else "no approval thresholds are configured for this case",
    )
    need("p4_current", p4_current, "no P4 integrity run exists for the current data version")
    balance = (p4_checks or {}).get("balance_continuity")
    need(
        "running_balance",
        p4_current and balance is not None and balance["status"] in ("ok", "issues"),
        "no P4 integrity run exists for the current data version"
        if not p4_current
        else f"P4 balance_continuity: {balance['reason']}"
        if balance
        else "the P4 run did not check balances",
    )
    need(
        "master_data",
        masters and adapter != "paysim",
        "PaySim party names are simulator account codes; master data does not apply"
        if adapter == "paysim"
        else "no master data (vendor / customer / employee master) is imported for the case",
    )
    if profile is None:
        need("counterparty", False, f"no feature profile for adapter '{adapter}' in config features.adapters")
    return caps
