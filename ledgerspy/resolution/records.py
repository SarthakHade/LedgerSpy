"""Party records: the inputs of entity resolution.

Two sources, kept explicit:
- **master records**: rows of an imported master file (vendor, customer, employee or group-company master;
  `ledgerspy.resolution.masters`), with the fields the user mapped (name, address, city, phone, e-mail, bank
  account, bank code, tax ID). Unmapped columns are never read.
- **ledger parties**: every distinct counterparty name key of a canonical ledger (P6 `party_key` of
  `payer_raw` / `payee_raw`), with its roles (payer, payee) and transaction behaviour. A ledger carries no
  identifiers, so a ledger party has a name only.

Entity class decides what may be merged: organisations (vendor, customer, group company, a ledger name with a
legal form) never merge with persons (employees); a ledger name without a legal form is `unknown` and may link
to either. PaySim party names are simulator account codes (`account_code`): compared exactly, never fuzzily.
"""

from dataclasses import dataclass, field
from typing import Any

from ledgerspy.config.schema import ResolutionConfig
from ledgerspy.resolution import normalize

MASTER_TYPES = ("vendor", "customer", "employee", "group_company")
MASTER_FIELDS = (
    "record_id",
    "name",
    "address",
    "city",
    "phone",
    "email",
    "bank_account",
    "bank_code",
    "tax_id",
)
IDENTIFIERS = ("tax_id", "bank_account", "email", "phone")


@dataclass(frozen=True)
class MasterRecord:
    """One imported master row: raw mapped values only (never normalised in storage)."""

    master_file_id: str
    entity_type: str
    source_row: int
    values: tuple[tuple[str, str | None], ...]  # (field, raw value) for the mapped fields

    def get(self, name: str) -> str | None:
        return dict(self.values).get(name)


@dataclass
class PartyRecord:
    key: str  # stable record key (see `master_key` / `ledger_key`)
    source: str  # "master" | "ledger"
    entity_type: str  # vendor | customer | employee | group_company | ledger_party
    entity_class: str  # organisation | person | unknown | account_code
    name: normalize.NormalizedName
    raw: dict[str, str | None]
    norm: dict[str, Any] = field(default_factory=dict)  # normalised comparison values
    roles: tuple[str, ...] = ()  # ledger roles
    behaviour: dict[str, Any] = field(default_factory=dict)  # ledger: supporting evidence inputs
    origin: dict[str, Any] = field(default_factory=dict)  # master file / row, or ledger counts

    @property
    def identifiers(self) -> dict[str, str]:
        return {k: self.norm[k] for k in IDENTIFIERS if self.norm.get(k)}


def master_key(rec: MasterRecord) -> str:
    rid = rec.get("record_id")
    ident = f"id:{rid.strip()}" if rid and rid.strip() else f"row:{rec.source_row}"
    return f"m:{rec.entity_type}:{rec.master_file_id}:{ident}"


def ledger_key(party_key: str) -> str:
    return f"l:{party_key}"


def from_master(rec: MasterRecord, cfg: ResolutionConfig) -> PartyRecord:
    raw = {f: rec.get(f) for f in MASTER_FIELDS}
    nm = normalize.name(raw["name"])
    addr, pin, words = normalize.address(raw["address"])
    city = normalize._text(raw["city"], [])  # noqa: SLF001 — same text rule
    tax, pan = normalize.tax_id(raw["tax_id"])
    norm = {
        "phone": normalize.phone(raw["phone"], cfg.normalization.phone_country_code).value,
        "email": normalize.email(raw["email"]).value,
        "bank_account": normalize.bank_account(raw["bank_account"]).value,
        "bank_code": normalize.bank_code(raw["bank_code"]).value,
        "tax_id": tax.value,
        "pan": pan,
        "address": addr.value,
        "address_words": words,
        "postal_code": pin,
        "city": city,
    }
    cls = "person" if rec.entity_type == "employee" else "organisation"
    return PartyRecord(
        key=master_key(rec),
        source="master",
        entity_type=rec.entity_type,
        entity_class=cls,
        name=nm,
        raw=raw,
        norm=norm,
        origin={"master_file_id": rec.master_file_id, "source_row": rec.source_row},
    )


def ledger_party(
    party_key: str,
    roles: tuple[str, ...],
    behaviour: dict[str, Any],
    variants: dict[str, int],
    account_codes: bool,
) -> PartyRecord:
    nm = normalize.name(party_key) if not account_codes else _code_name(party_key)
    if account_codes:
        cls = "account_code"
    elif nm.legal_form:
        cls = "organisation"
    else:
        cls = "unknown"
    return PartyRecord(
        key=ledger_key(party_key),
        source="ledger",
        entity_type="ledger_party",
        entity_class=cls,
        name=nm,
        raw={"name": party_key},
        norm={},
        roles=roles,
        behaviour=behaviour,
        origin={"name_variants": variants, "transactions": behaviour.get("transactions", 0)},
    )


def _code_name(code: str) -> normalize.NormalizedName:
    c = normalize.code(code)
    return normalize.NormalizedName(
        c.raw, c.value, c.rules, core=c.value, legal_form=None, tokens=(c.value,) if c.value else ()
    )
