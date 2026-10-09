"""Column profiling, mapping suggestions and mapping validation.

The mapping step is explicit: an upload only produces a *proposal*. Nothing is canonicalised until the user
sends a mapping back (PUT /api/datasets/{id}/mapping), and that mapping is validated here first.

Suggestions are conservative. A field is proposed only when exactly one column matches one of its configured
aliases *and* that column's values fit the field. Several candidates, ill-fitting values or an ambiguous
convention (date format, decimal separator) are reported for confirmation and left unset.

Bank statements (P14, dataset kind `bank_statement`, adapter `bank_statement`) map into the same canonical
Transaction with direction `in` / `out` from the ACCOUNT HOLDER's view. Sign conventions differ between banks,
so the direction rule is part of the confirmed mapping (`bank`): what a debit column / debit indicator means
(`debit_means`), or what a positive signed amount means (`amount_sign`). It is proposed only when the headers
state it unambiguously (Withdrawal / Deposit); otherwise it must be confirmed. The running-balance and
statement-identifier columns stay verbatim in `extra`; `bank` names them.
"""

import contextlib
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ledgerspy.config.schema import (
    MAPPABLE_FIELDS,
    DirectionValue,
    LedgerSpyConfig,
    MappableField,
    normalize_header,
)
from ledgerspy.ingest.readers import TableSource, iter_records
from ledgerspy.ingest.values import Cell, cell_text, fits_format, is_blank, parse_decimal

AdapterName = Literal["paysim", "generic_ledger", "bank_statement"]
DatasetKind = Literal["ledger", "bank_statement"]
AMOUNT_FIELDS = ("amount", "debit_amount", "credit_amount")
DATE_FIELDS = ("posted_at", "document_date")
REQUIRED_GROUPS = ("posted_at", "amount source", "direction source")
ISO_CURRENCY = re.compile(r"^[A-Z]{3}$")
_SAMPLE_CHARS = 80

PAYSIM_SIGNATURE = (
    "step",
    "type",
    "amount",
    "nameOrig",
    "oldbalanceOrg",
    "newbalanceOrig",
    "nameDest",
    "oldbalanceDest",
    "newbalanceDest",
)
# Headers that state the direction from the account holder's view (normalised with normalize_header).
OUTFLOW_HEADERS = frozenset(
    {"withdrawal", "withdrawals", "withdrawalamount", "withdrawalamt", "withdrawalinr", "paidout", "moneyout"}
)
INFLOW_HEADERS = frozenset(
    {"deposit", "deposits", "depositamount", "depositamt", "depositinr", "paidin", "moneyin"}
)
STATEMENT_HEADERS = frozenset(
    {"statementid", "statementno", "statementnumber", "stmtid", "stmtno", "statement"}
)

PAYSIM_FIELDS: dict[str, str] = {
    "posted_at": "step",
    "amount": "amount",
    "txn_type": "type",
    "payer_raw": "nameOrig",
    "account": "nameOrig",
    "payee_raw": "nameDest",
}


# ----------------------------------------------------------------------------------------------- models


class MappingOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decimal_separator: Literal[".", ","] | None = None
    date_format: str | None = Field(default=None, min_length=2, max_length=64)
    document_date_format: str | None = Field(default=None, min_length=2, max_length=64)
    currency: str | None = Field(
        default=None, pattern=r"^[A-Z]{3}$", description="declared for the whole dataset"
    )
    direction: DirectionValue | None = Field(default=None, description="declared for the whole dataset")


class BankOptions(BaseModel):
    """Bank-statement semantics (adapter `bank_statement` only), from the account holder's view."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    debit_means: Literal["outflow", "inflow"] | None = Field(
        default=None, description="what a debit column / debit indicator means for the account holder"
    )
    amount_sign: Literal["positive_inflow", "positive_outflow"] | None = Field(
        default=None, description="for one signed amount column without a direction column"
    )
    balance_column: str | None = Field(default=None, min_length=1, max_length=255)
    statement_column: str | None = Field(default=None, min_length=1, max_length=255)


class Mapping(BaseModel):
    """A confirmed (or proposed) mapping from source columns to canonical transaction fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    adapter: AdapterName
    fields: dict[MappableField, str] = Field(default_factory=dict)
    options: MappingOptions = Field(default_factory=MappingOptions)
    label_columns: list[str] = Field(default_factory=list)
    bank: BankOptions | None = None


def mapping_json(mapping: Mapping) -> dict[str, Any]:
    """The stored / hashed form. `bank` is omitted when absent: ledger mappings keep their P3 hash."""
    out = mapping.model_dump(mode="json")
    if out.get("bank") is None:
        out.pop("bank", None)
    return out


# ----------------------------------------------------------------------------------------------- profile


@dataclass
class ColumnProfile:
    index: int
    name: str
    non_empty: int = 0
    kinds: Counter = field(default_factory=Counter)
    distinct_text: dict[str, None] = field(default_factory=dict)
    distinct_truncated: bool = False
    samples: list[str] = field(default_factory=list)
    track_unique: bool = False
    seen: set[str] = field(default_factory=set)
    duplicates: int = 0
    blanks: int = 0

    def to_dict(self) -> dict[str, Any]:
        out = {
            "index": self.index,
            "name": self.name,
            "non_empty": self.non_empty,
            "value_kinds": dict(sorted(self.kinds.items())),
            "distinct_values": len(self.distinct_text) if not self.distinct_truncated else None,
            "samples": self.samples,
        }
        if self.track_unique:
            out["unique"] = {"blanks": self.blanks, "duplicates": self.duplicates}
        return out


def _kind(value: Cell) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, datetime | date):
        return "date"
    return "text"


@dataclass
class Profile:
    header: list[str]
    columns: list[ColumnProfile]
    rows: int
    records: int
    blank_records: int
    label_columns: list[str]

    def column(self, name: str) -> ColumnProfile:
        return self.columns[self.header.index(name)]


def detect_label_columns(header: list[str], settings: LedgerSpyConfig) -> list[str]:
    labels = {normalize_header(n) for n in settings.mapping.label_columns}
    return [h for h in header if normalize_header(h) in labels]


def _alias_index(settings: LedgerSpyConfig) -> dict[str, str]:
    return {normalize_header(a): f for f, names in settings.mapping.aliases.items() for a in names}


def profile_table(src: TableSource, settings: LedgerSpyConfig) -> Profile:
    """One streaming pass: per-column counts, value kinds, capped distinct values, ID-candidate uniqueness."""
    cap = settings.mapping.profile_distinct_cap
    aliases = _alias_index(settings)
    cols = [ColumnProfile(i, name) for i, name in enumerate(src.header)]
    for col in cols:
        col.track_unique = aliases.get(normalize_header(col.name)) == "txn_id"
    rows = 0
    for _row, cells in iter_records(src, settings.ingest):
        rows += 1
        for col, value in zip(cols, cells, strict=True):
            if is_blank(value):
                if col.track_unique:
                    col.blanks += 1
                continue
            col.non_empty += 1
            col.kinds[_kind(value)] += 1
            text = cell_text(value) or ""
            if col.track_unique:
                if text in col.seen:
                    col.duplicates += 1
                col.seen.add(text)
            if text not in col.distinct_text:
                if len(col.distinct_text) < cap:
                    col.distinct_text[text] = None
                    if len(col.samples) < settings.mapping.sample_values:
                        col.samples.append(text[:_SAMPLE_CHARS])
                else:
                    col.distinct_truncated = True
    for col in cols:
        col.seen = set()  # only the counts are needed after the pass
    return Profile(
        header=src.header,
        columns=cols,
        rows=rows,
        records=src.stats["records"],
        blank_records=src.stats["blank_records"],
        label_columns=detect_label_columns(src.header, settings),
    )


# ----------------------------------------------------------------------------------------------- value fit


def _string_values(col: ColumnProfile) -> list[str]:
    """Distinct values of text cells only (numbers/dates from XLSX need no convention)."""
    return list(col.distinct_text) if col.kinds.get("text") else []


def _rate(ok: int, total: int) -> float:
    return ok / total if total else 1.0


def decimal_fit(cols: list[ColumnProfile], min_rate: float) -> tuple[str | None, dict[str, Any]]:
    """Choose the decimal separator for the amount columns, or None when it is ambiguous / nothing fits."""
    values = list(dict.fromkeys(v for c in cols for v in _string_values(c)))  # distinct across columns
    if not values:
        return ".", {"basis": "no text amounts (numeric cells)"}
    rates, parsed = {}, {}
    for sep in (".", ","):
        ok = {}
        for v in values:
            with contextlib.suppress(ValueError):
                ok[v] = parse_decimal(v, sep)
        rates[sep], parsed[sep] = _rate(len(ok), len(values)), ok
    fitting = [s for s in (".", ",") if rates[s] >= min_rate]
    info = {"parse_rate": {s: round(r, 4) for s, r in rates.items()}}
    if len(fitting) == 1:
        return fitting[0], info
    if not fitting:
        return None, {**info, "problem": "amount values do not parse as numbers"}
    disagree = [
        v for v in values if v in parsed["."] and v in parsed[","] and parsed["."][v] != parsed[","][v]
    ]
    if disagree:
        return None, {**info, "problem": "ambiguous decimal separator", "examples": disagree[:5]}
    return ".", info


def date_fit(col: ColumnProfile, formats: list[str], min_rate: float) -> tuple[str | None, dict[str, Any]]:
    values = _string_values(col)
    if not values:
        return None, {"basis": "date cells (no text format needed)"}
    candidates = []
    for fmt in formats:
        ok = {v: d for v in values if (d := fits_format(v, fmt)) is not None}
        if _rate(len(ok), len(values)) >= min_rate:
            candidates.append((fmt, ok))
    if not candidates:
        return None, {"problem": "no configured date format fits these values", "examples": values[:5]}
    if len(candidates) == 1:
        return candidates[0][0], {"basis": f"only format that fits ({candidates[0][0]})"}
    first_fmt, first = candidates[0]
    conflicting = [
        fmt for fmt, ok in candidates[1:] if any(ok.get(v) not in (None, d) for v, d in first.items())
    ]
    if conflicting:
        return None, {
            "problem": "ambiguous date format",
            "candidates": [first_fmt, *conflicting],
            "examples": values[:5],
        }
    return first_fmt, {"basis": f"formats that fit agree on every value ({first_fmt})"}


def _values_fit(fieldname: str, col: ColumnProfile, settings: LedgerSpyConfig) -> tuple[bool, str]:
    m = settings.mapping
    values = list(col.distinct_text)
    if col.non_empty == 0:
        return False, "column is empty"
    if fieldname == "txn_id":
        if col.blanks or col.duplicates:
            return False, f"not usable as a unique ID ({col.blanks} blank, {col.duplicates} duplicate values)"
        return True, "values are present and unique"
    if fieldname in AMOUNT_FIELDS:
        sep, info = decimal_fit([col], m.min_parse_rate)
        if sep is None and info.get("problem") != "ambiguous decimal separator":
            return False, info["problem"]
        return True, "values are numbers"
    if fieldname in DATE_FIELDS:
        if col.kinds.get("number") and not col.kinds.get("date"):
            return False, "values are plain numbers, not dates"
        fmt, info = date_fit(col, m.date_formats, m.min_parse_rate)
        if fmt is None and "no configured" in info.get("problem", ""):
            return False, info["problem"]
        return True, "values are dates"
    if fieldname == "direction":
        known = {normalize_header(v) for vs in m.direction_values.values() for v in vs}
        ok = sum(1 for v in values if normalize_header(v) in known)
        if _rate(ok, len(values)) < m.min_parse_rate:
            return False, "values are not recognised debit/credit indicators"
        return True, "values are debit/credit indicators"
    if fieldname == "currency":
        ok = sum(1 for v in values if ISO_CURRENCY.match(v.strip().upper()))
        if _rate(ok, len(values)) < m.min_parse_rate:
            return False, "values are not ISO 4217 currency codes"
        return True, "values are currency codes"
    return True, "text field"


# ----------------------------------------------------------------------------------------------- suggest


def _is_paysim(header: list[str]) -> bool:
    keys = {normalize_header(h) for h in header}
    return all(normalize_header(s) in keys for s in PAYSIM_SIGNATURE)


def _paysim_header_names(header: list[str]) -> dict[str, str]:
    by_key = {normalize_header(h): h for h in header}
    return {f: by_key[normalize_header(col)] for f, col in PAYSIM_FIELDS.items()}


def suggest(profile: Profile, settings: LedgerSpyConfig, kind: str = "ledger") -> dict[str, Any]:
    """Build the inspectable mapping proposal returned by the upload endpoint."""
    if kind == "bank_statement":
        return _suggest_bank(profile, settings)
    if _is_paysim(profile.header):
        return _suggest_paysim(profile, settings)
    out = _suggest_generic(profile, settings)
    if _looks_like_bank_statement(profile, settings):
        out["notes"].append(
            "This file looks like a bank statement (withdrawal/deposit or running-balance columns). "
            "To reconcile it against a ledger, upload it with kind=bank_statement; as a ledger it is "
            "analysed as postings."
        )
    return out


def _balance_columns(profile: Profile, settings: LedgerSpyConfig, used: set[str]) -> list[str]:
    aliases = {normalize_header(a) for a in settings.quality.balance.running_balance_aliases}
    return [h for h in profile.header if h not in used and normalize_header(h) in aliases]


def _looks_like_bank_statement(profile: Profile, settings: LedgerSpyConfig) -> bool:
    keys = {normalize_header(h) for h in profile.header}
    return bool(keys & OUTFLOW_HEADERS and keys & INFLOW_HEADERS) or bool(
        _balance_columns(profile, settings, set())
    )


def _direction_values_seen(col: ColumnProfile, settings: LedgerSpyConfig) -> set[str]:
    """Canonical direction values ('debit', 'credit', 'in', 'out') the column's distinct values map to."""
    known = {normalize_header(v): d for d, vs in settings.mapping.direction_values.items() for v in vs}
    return {known[k] for v in col.distinct_text if (k := normalize_header(v)) in known}


def _suggest_bank(profile: Profile, settings: LedgerSpyConfig) -> dict[str, Any]:
    """Generic column proposal + bank semantics. Nothing about direction or currency is assumed."""
    out = _suggest_generic(profile, settings)
    fields = dict(out["proposed_mapping"]["fields"])
    for f in ("payer_raw", "payee_raw"):
        if f in fields:
            out["fields"][f]["basis"] += (
                " (bank statements usually name the counterparty only in the description)"
            )
    confirm = [i for i in out["requires_confirmation"] if i["item"] != "direction source"]
    bank: dict[str, Any] = {}
    semantics: dict[str, Any] = {}
    if "debit_amount" in fields and "credit_amount" in fields:
        dk, ck = normalize_header(fields["debit_amount"]), normalize_header(fields["credit_amount"])
        if dk in OUTFLOW_HEADERS and ck in INFLOW_HEADERS:
            bank["debit_means"] = "outflow"
            semantics = {
                "status": "proposed",
                "debit_means": "outflow",
                "basis": f"the headers state the direction: {fields['debit_amount']!r} is money out of the "
                f"account, {fields['credit_amount']!r} money in",
            }
        else:
            semantics = {"status": "requires_confirmation"}
            confirm.append(
                {
                    "item": "bank.debit_means",
                    "reason": f"columns {fields['debit_amount']!r} / {fields['credit_amount']!r}: on most "
                    "bank statements a debit is money out of the account holder's account, but the "
                    "convention is not universal; confirm 'outflow' or 'inflow'",
                }
            )
    elif "amount" in fields and "direction" in fields:
        seen = _direction_values_seen(profile.column(fields["direction"]), settings)
        if seen & {"debit", "credit"}:
            semantics = {"status": "requires_confirmation", "indicator_values": sorted(seen)}
            confirm.append(
                {
                    "item": "bank.debit_means",
                    "reason": f"direction column {fields['direction']!r} uses debit/credit indicators; "
                    "confirm whether a debit is money out ('outflow') or in ('inflow') for the account "
                    "holder",
                }
            )
        else:
            semantics = {"status": "proposed", "basis": "indicator values are in/out (no debit/credit)"}
    elif "amount" in fields:
        semantics = {"status": "requires_confirmation"}
        confirm.append(
            {
                "item": "bank.amount_sign",
                "reason": "one signed amount column and no direction column: confirm whether a positive "
                "amount is money in ('positive_inflow') or money out ('positive_outflow'); it is never "
                "assumed",
            }
        )
    used = set(fields.values()) | set(profile.label_columns)
    balances = _balance_columns(profile, settings, used)
    if len(balances) == 1:
        bank["balance_column"] = balances[0]
        balance = {"column": balances[0], "status": "proposed", "basis": "the only running-balance column"}
    elif balances:
        balance = {"column": None, "status": "ambiguous", "candidates": balances}
        confirm.append(
            {"item": "bank.balance_column", "reason": f"several running-balance columns: {balances}"}
        )
    else:
        balance = {
            "column": None,
            "status": "not_in_source",
            "basis": "no running-balance column: balance checks will be reported unavailable",
        }
    statements = [h for h in profile.header if h not in used and normalize_header(h) in STATEMENT_HEADERS]
    if len(statements) == 1:
        bank["statement_column"] = statements[0]
    if "currency" not in fields:
        confirm.append(
            {
                "item": "currency",
                "reason": "no currency column: declare options.currency only if the statement states it; "
                "rows without a currency are reported as insufficient data and never reconciled",
            }
        )
    if "account" not in fields:
        out["notes"].append(
            "No account-number column: the statement is treated as ONE unidentified account; every "
            "reconciliation scope must then be confirmed explicitly."
        )
    proposal = Mapping(
        adapter="bank_statement",
        fields=fields,
        options=MappingOptions(**out["proposed_mapping"]["options"]),
        label_columns=profile.label_columns,
        bank=BankOptions(**bank),
    )
    out["adapter"] = {
        "suggested": "bank_statement",
        "basis": "uploaded as a bank statement (kind=bank_statement)",
        "available": ["bank_statement"],
    }
    out["bank"] = {
        "direction_semantics": semantics,
        "balance": balance,
        "statement_column": bank.get("statement_column"),
        "formats": {
            "supported": ["csv", "xlsx", "pdf"],
            "pdf": "PDF statements (text and scanned, local OCR) are extracted and reviewed first (P14b)",
        },
    }
    out["requires_confirmation"] = confirm
    out["notes"] = [
        "Bank lines become canonical transactions with direction in/out from the account holder's view.",
        *out["notes"],
    ]
    out["proposed_mapping"] = mapping_json(proposal)
    return out


def _unmapped(profile: Profile, fields: dict[str, str]) -> list[str]:
    used = set(fields.values()) | set(profile.label_columns)
    return [h for h in profile.header if h not in used]


def _suggest_paysim(profile: Profile, settings: LedgerSpyConfig) -> dict[str, Any]:
    p = settings.adapters.paysim
    fields = _paysim_header_names(profile.header)
    basis = {
        "posted_at": f"PaySim `step` (simulated hour) → {p.step_epoch} + (step − 1) × {p.step_hours} h; "
        "an artificial clock, NOT an accounting date",
        "amount": "PaySim `amount` (decimal point); stored in minor units; no currency in the source",
        "txn_type": "PaySim `type` (PAYMENT, TRANSFER, CASH_OUT, CASH_IN, DEBIT)",
        "payer_raw": "PaySim `nameOrig` (originating account code)",
        "account": "PaySim `nameOrig`: the account whose balance columns describe the line",
        "payee_raw": "PaySim `nameDest` (destination account code)",
    }
    proposal = Mapping(
        adapter="paysim",
        fields=fields,
        options=MappingOptions(decimal_separator="."),
        label_columns=profile.label_columns,
    )
    return {
        "adapter": {
            "suggested": "paysim",
            "basis": "all PaySim signature columns are present: " + ", ".join(PAYSIM_SIGNATURE),
            "available": ["paysim", "generic_ledger"],
        },
        "fields": {
            f: {
                "column": fields.get(f),
                "status": "proposed" if f in fields else "not_in_source",
                "basis": basis.get(f, "PaySim has no such column"),
            }
            for f in MAPPABLE_FIELDS
            if f not in ("debit_amount", "credit_amount")
        }
        | {
            "direction": {
                "column": fields["txn_type"],
                "status": "derived",
                "basis": "from `type`: "
                + ", ".join(f"{k}→{v}" for k, v in p.direction_by_type.items())
                + " (relative to the nameOrig account)",
            }
        },
        "label_columns": profile.label_columns,
        "unmapped_columns": _unmapped(profile, fields),
        "requires_confirmation": [],
        "notes": [
            "PaySim is a synthetic simulation, not an accounting ledger. The dataset is marked synthetic and "
            "its time basis is 'simulation_step'.",
            "Unmapped columns are preserved verbatim in each transaction's `extra`; the appended columns "
            "(e.g. 'Date of transaction') have unknown provenance and are not used as dates.",
            "Label columns are stored separately and never become transaction fields.",
        ],
        "proposed_mapping": mapping_json(proposal),
    }


def _suggest_generic(profile: Profile, settings: LedgerSpyConfig) -> dict[str, Any]:
    m = settings.mapping
    aliases = _alias_index(settings)
    labels = set(profile.label_columns)
    candidates: dict[str, list[str]] = {f: [] for f in MAPPABLE_FIELDS}
    for h in profile.header:
        f = aliases.get(normalize_header(h))
        if f and h not in labels:
            candidates[f].append(h)

    report: dict[str, dict[str, Any]] = {}
    fields: dict[str, str] = {}
    for f in MAPPABLE_FIELDS:
        cands = candidates[f]
        if not cands:
            report[f] = {"column": None, "status": "unmapped", "candidates": []}
        elif len(cands) > 1:
            report[f] = {
                "column": None,
                "status": "ambiguous",
                "candidates": cands,
                "basis": "several columns match this field's aliases",
            }
        else:
            ok, why = _values_fit(f, profile.column(cands[0]), settings)
            if ok:
                fields[f] = cands[0]
                report[f] = {"column": cands[0], "status": "proposed", "basis": f"exact alias match; {why}"}
            else:
                report[f] = {
                    "column": None,
                    "status": "low_confidence",
                    "candidates": cands,
                    "basis": f"alias match but {why}",
                }

    confirm: list[dict[str, Any]] = []
    # amount source: either `amount` or the debit/credit pair — never both
    has_dc = "debit_amount" in fields and "credit_amount" in fields
    if candidates["amount"] and (candidates["debit_amount"] or candidates["credit_amount"]):
        for f in ("amount", "debit_amount", "credit_amount"):
            fields.pop(f, None)
            report[f] = {
                **report[f],
                "status": "ambiguous",
                "column": None,
                "basis": "both an amount column and debit/credit columns exist",
            }
        has_dc = False
        confirm.append(
            {"item": "amount source", "reason": "both an amount column and debit/credit columns exist"}
        )
    elif not has_dc:
        for f in ("debit_amount", "credit_amount"):
            if f in fields:
                fields.pop(f)
                report[f] = {
                    **report[f],
                    "column": None,
                    "status": "low_confidence",
                    "basis": "debit and credit columns must be mapped together",
                }
    if "amount" not in fields and not has_dc and not any(i["item"] == "amount source" for i in confirm):
        confirm.append(
            {"item": "amount source", "reason": "map an amount column, or a debit and a credit column"}
        )

    options: dict[str, Any] = {}
    amount_cols = [profile.column(fields[f]) for f in AMOUNT_FIELDS if f in fields]
    if amount_cols:
        sep, info = decimal_fit(amount_cols, m.min_parse_rate)
        options["decimal_separator"] = sep
        if sep is None:
            confirm.append({"item": "options.decimal_separator", "reason": info.pop("problem", None), **info})
    for f, opt in (("posted_at", "date_format"), ("document_date", "document_date_format")):
        if f in fields:
            fmt, info = date_fit(profile.column(fields[f]), m.date_formats, m.min_parse_rate)
            if fmt is None and info.get("problem"):
                confirm.append({"item": f"options.{opt}", "reason": info.pop("problem"), **info})
            options[opt] = fmt
            report[f]["format"] = info
    if "posted_at" not in fields:
        confirm.append({"item": "posted_at", "reason": "no single date column could be identified"})
    if "direction" not in fields and not has_dc:
        confirm.append(
            {
                "item": "direction source",
                "reason": "no debit/credit indicator column; map one, or declare options.direction "
                "for the whole file",
            }
        )
    if "currency" not in fields:
        report["currency"]["basis"] = "no currency column: amounts are stored without currency unless "
        report["currency"]["basis"] += "options.currency is declared"

    proposal = Mapping(
        adapter="generic_ledger",
        fields=fields,
        options=MappingOptions(**{k: v for k, v in options.items() if v is not None}),
        label_columns=profile.label_columns,
    )
    return {
        "adapter": {
            "suggested": "generic_ledger",
            "basis": "no specific adapter signature found",
            "available": ["paysim", "generic_ledger"],
        },
        "fields": report,
        "label_columns": profile.label_columns,
        "unmapped_columns": _unmapped(profile, fields),
        "requires_confirmation": confirm,
        "notes": [
            "Only exact alias matches whose values fit are proposed; everything else needs confirmation.",
            "Unmapped columns are preserved verbatim in each transaction's `extra`.",
            "Label columns are stored separately and never become transaction fields.",
        ],
        "proposed_mapping": mapping_json(proposal),
    }


# ----------------------------------------------------------------------------------------------- validate


def validate_mapping(
    mapping: Mapping, profile: Profile, settings: LedgerSpyConfig, kind: str = "ledger"
) -> list[dict[str, str]]:
    """Structural checks before conversion. Returns a list of errors (empty = valid)."""
    errors: list[dict[str, str]] = []

    def err(code: str, message: str, fieldname: str = "") -> None:
        errors.append({"code": code, "field": fieldname, "message": message})

    is_bank = mapping.adapter == "bank_statement"
    if kind == "bank_statement" and not is_bank:
        err("adapter", "a bank statement dataset must use the bank_statement adapter")
    if kind != "bank_statement" and is_bank:
        err("adapter", "the bank_statement adapter is only for datasets uploaded with kind=bank_statement")
    if mapping.bank is not None and not is_bank:
        err("bank", "`bank` options apply to the bank_statement adapter only")
    if errors:
        return errors

    header = set(profile.header)
    missing_labels = [c for c in profile.label_columns if c not in mapping.label_columns]
    if missing_labels:
        err("label_columns", f"detected label columns must stay labels: {missing_labels}")
    for c in mapping.label_columns:
        if c not in header:
            err("unknown_column", f"label column {c!r} is not in the file")
    labels = set(mapping.label_columns) | set(profile.label_columns)
    for f, col in mapping.fields.items():
        if col not in header:
            err("unknown_column", f"column {col!r} is not in the file", f)
        elif col in labels:
            err("label_column_mapped", f"label column {col!r} can never be a transaction field", f)
    if errors:
        return errors

    o, fields = mapping.options, mapping.fields
    if mapping.adapter == "paysim":
        if not _is_paysim(profile.header):
            err("adapter", "the file does not have the PaySim columns")
        elif dict(fields) != _paysim_header_names(profile.header):
            err("adapter", f"the PaySim adapter uses a fixed mapping: {_paysim_header_names(profile.header)}")
        if o != MappingOptions(decimal_separator="."):
            err("options", "the PaySim adapter takes no options except decimal_separator '.'")
        return errors

    if "posted_at" not in fields:
        err("required", "posted_at must be mapped", "posted_at")
    dc = [f for f in ("debit_amount", "credit_amount") if f in fields]
    if "amount" in fields and dc:
        err("amount_source", "map either amount or debit_amount + credit_amount, not both", "amount")
    elif "amount" not in fields and len(dc) != 2:
        err("amount_source", "map amount, or both debit_amount and credit_amount", "amount")
    if is_bank:
        _validate_bank(mapping, profile, settings, err)
    elif "amount" in fields:
        if "direction" in fields and o.direction:
            err(
                "direction_source",
                "map a direction column or declare options.direction, not both",
                "direction",
            )
        if "direction" not in fields and not o.direction:
            err("direction_source", "map a direction column or declare options.direction", "direction")
    elif dc and ("direction" in fields or o.direction):
        err("direction_source", "debit/credit columns already give the direction", "direction")
    if o.decimal_separator is None:
        err("options", "options.decimal_separator must be confirmed ('.' or ',')", "amount")
    if "currency" in fields and o.currency:
        err("currency_source", "map a currency column or declare options.currency, not both", "currency")
    for f, opt in (("posted_at", "date_format"), ("document_date", "document_date_format")):
        fmt = getattr(o, opt)
        if f in fields and profile.column(fields[f]).kinds.get("text") and not fmt:
            err("options", f"options.{opt} is required: {fields[f]!r} contains text dates", f)
        if fmt is not None and "%" not in fmt:
            err("options", f"options.{opt} must be a strptime format such as %d/%m/%Y", f)
    return errors


def _validate_bank(mapping: Mapping, profile: Profile, settings: LedgerSpyConfig, err) -> None:
    """Direction semantics of a bank statement must be stated, never assumed (see module docstring)."""
    o, fields = mapping.options, mapping.fields
    b = mapping.bank or BankOptions()
    if o.direction is not None and o.direction not in ("in", "out"):
        err(
            "direction_source",
            "a bank statement can only declare options.direction 'in' or 'out'",
            "direction",
        )
    if "amount" in fields:
        sources = ["direction" in fields, o.direction is not None, b.amount_sign is not None]
        if sum(sources) != 1:
            err(
                "direction_source",
                "a signed amount column needs exactly one direction source: a direction column, "
                "options.direction ('in' / 'out'), or bank.amount_sign",
                "direction",
            )
        if "direction" in fields and b.debit_means is None:
            seen = _direction_values_seen(profile.column(fields["direction"]), settings)
            if seen & {"debit", "credit"}:
                err(
                    "bank_semantics",
                    "the direction column uses debit/credit indicators: confirm bank.debit_means",
                    "direction",
                )
    elif "debit_amount" in fields and "credit_amount" in fields:
        if "direction" in fields or o.direction:
            err("direction_source", "debit/credit columns already give the direction", "direction")
        if b.debit_means is None:
            err(
                "bank_semantics",
                "confirm bank.debit_means: is the debit column money out of the account ('outflow') or in?",
                "debit_amount",
            )
        if b.amount_sign is not None:
            err("bank_semantics", "bank.amount_sign applies to one signed amount column only", "amount")
    used = set(fields.values()) | set(mapping.label_columns) | set(profile.label_columns)
    for opt in ("balance_column", "statement_column"):
        col = getattr(b, opt)
        if col is None:
            continue
        if col not in profile.header:
            err("unknown_column", f"bank.{opt} {col!r} is not in the file", opt)
        elif col in used:
            err("bank_column_mapped", f"bank.{opt} {col!r} is already mapped or a label column", opt)
    if b.balance_column and b.balance_column == b.statement_column:
        err("bank_column_mapped", "the balance and statement columns must differ", "balance_column")
