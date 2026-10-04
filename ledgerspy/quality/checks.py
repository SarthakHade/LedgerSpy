"""Integrity checks over the normalised view of a canonical dataset.

Each check returns a `CheckResult`: status `ok`, `issues` or `not_available` (with the reason it could not
run),
its basis, details and the raw issues it found. Severities are assigned afterwards by `severity.assign`.
A check never runs on a field the dataset does not have; that is `not_available`, not a pass.
"""

import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from ledgerspy.config.schema import LedgerSpyConfig, normalize_header
from ledgerspy.ingest.adapters import PaySimConverter
from ledgerspy.ingest.values import parse_decimal, to_minor_units
from ledgerspy.normalize.proposals import Proposal
from ledgerspy.normalize.transforms import collapse_whitespace, has_control_chars
from ledgerspy.normalize.view import FIELDS, DatasetView

INVALID_CODES = ("unparseable", "unknown_value", "too_many_decimals", "debit_and_credit_both_set")
DUPLICATE_KEY = (
    "posted_at",
    "amount_minor",
    "currency",
    "direction",
    "account",
    "payer_raw",
    "payee_raw",
    "reference",
    "description",
    "txn_type",
)
SIMULATED = "simulation_step"


@dataclass
class RawIssue:
    field: str | None
    txn_id: str | None
    source_row: int | None
    observed: dict[str, Any]
    message: str
    fix_id: str | None = None
    key: str = ""  # disambiguates issues of one check on the same row/field


@dataclass
class CheckResult:
    check: str
    status: str  # ok | issues | not_available
    basis: str
    details: dict[str, Any] = field(default_factory=dict)
    issues: list[RawIssue] = field(default_factory=list)

    @property
    def affected_rows(self) -> int:
        return len({i.source_row for i in self.issues if i.source_row is not None})


@dataclass
class Context:
    view: DatasetView
    settings: LedgerSpyConfig
    fixes: dict[tuple[str, str], Proposal]
    used_fixes: set[str] = field(default_factory=set)

    @property
    def rows(self) -> list[dict[str, Any]]:
        return self.view.rows

    def fix_for(self, txn_id: str, field_name: str, transforms: tuple[str, ...] | None = None) -> str | None:
        p = self.fixes.get((txn_id, field_name))
        if p is None or (transforms and p.transform.split("+")[0] not in transforms):
            return None
        self.used_fixes.add(p.fix_id)
        return p.fix_id


def _result(check: str, basis: str, issues: list[RawIssue], details: dict | None = None) -> CheckResult:
    return CheckResult(check, "issues" if issues else "ok", basis, details or {}, issues)


def _na(check: str, reason: str) -> CheckResult:
    return CheckResult(check, "not_available", reason)


def _pct(n: int, total: int) -> float:
    return round(100 * n / total, 3) if total else 0.0


# ----------------------------------------------------------------------------------------------- completeness


def missing_required(ctx: Context) -> CheckResult:
    v, req = ctx.view, ctx.settings.quality.required_fields
    issues, counts = [], dict.fromkeys(req, 0)
    debit_credit = "debit_amount" in v.mapping["fields"]
    for row in ctx.rows:
        for f in req:
            if row[f] is not None:
                continue
            codes = {i["code"] for i in v.p3(row["source_row"], f)}
            if codes - {"missing"}:
                continue  # present but not convertible: reported by invalid_value / unparseable_date
            if f == "direction" and (
                row["amount_minor"] == 0 or (debit_credit and v.p3(row["source_row"], "amount_minor"))
            ):
                continue  # same root cause as a zero or invalid amount line, reported there
            counts[f] += 1
            issues.append(
                RawIssue(
                    f,
                    row["txn_id"],
                    row["source_row"],
                    {"value": None},
                    f"required field {f} is blank in the source",
                )
            )
    total = len(ctx.rows)
    details = {f: {"missing": n, "percent": _pct(n, total)} for f, n in counts.items()}
    return _result("missing_required", f"required fields: {', '.join(req)}", issues, details)


def missing_optional(ctx: Context) -> CheckResult:
    v = ctx.view
    fields = [f for f in ctx.settings.quality.optional_fields if v.mapped(f)]
    if not fields:
        return _na("missing_optional", "no optional field is mapped")
    issues, counts = [], dict.fromkeys(fields, 0)
    for row in ctx.rows:
        for f in fields:
            if row[f] is None and not v.p3(row["source_row"], f):
                counts[f] += 1
                issues.append(
                    RawIssue(
                        f, row["txn_id"], row["source_row"], {"value": None}, f"optional field {f} is blank"
                    )
                )
    total = len(ctx.rows)
    details = {f: {"missing": n, "percent": _pct(n, total)} for f, n in counts.items()}
    return _result("missing_optional", f"mapped optional fields: {', '.join(fields)}", issues, details)


# ----------------------------------------------------------------------------------------------- validity


def _p3_based(
    ctx: Context, check: str, fields: tuple[str, ...], codes: tuple[str, ...], fix: str
) -> CheckResult:
    issues = []
    by_code: dict[str, int] = defaultdict(int)
    for row in ctx.rows:
        for f in fields:
            if row[f] is not None:
                continue  # fixed by an applied transformation
            for i in ctx.view.p3(row["source_row"], f):
                if i["code"] not in codes:
                    continue
                by_code[f"{i['field']}.{i['code']}"] += 1
                issues.append(
                    RawIssue(
                        f,
                        row["txn_id"],
                        row["source_row"],
                        {
                            "column": i["source_column"],
                            "raw_value": i["raw_value"],
                            "ingestion_code": i["code"],
                            "source_field": i["field"],
                        },
                        f"{i['source_column']!r} value {i['raw_value']!r} "
                        f"could not be converted ({i['code']})",
                        ctx.fix_for(row["txn_id"], f, (fix,)),
                        key=f"{i['field']}.{i['code']}",
                    )
                )
    return _result(
        check,
        "conversion failures recorded at ingestion (P3), still unresolved",
        issues,
        {"by_field_and_code": dict(sorted(by_code.items()))},
    )


def invalid_value(ctx: Context) -> CheckResult:
    return _p3_based(
        ctx, "invalid_value", ("amount_minor", "direction", "currency"), INVALID_CODES, "reinterpret_amount"
    )


def unparseable_date(ctx: Context) -> CheckResult:
    return _p3_based(
        ctx, "unparseable_date", ("posted_at", "document_date"), ("unparseable",), "reinterpret_date"
    )


def implausible_date(ctx: Context) -> CheckResult:
    v = ctx.view
    if v.time_basis == SIMULATED:
        return _na("implausible_date", "the dataset uses a simulated clock (PaySim step), not real dates")
    b = ctx.settings.quality.date_bounds
    lo, hi = date.fromisoformat(b.earliest), date.fromisoformat(b.latest)
    issues = []
    for row in ctx.rows:
        for f in ("posted_at", "document_date"):
            if row[f] is None:
                continue
            d = datetime.fromisoformat(row[f]).date()
            if not lo <= d <= hi:
                issues.append(
                    RawIssue(
                        f,
                        row["txn_id"],
                        row["source_row"],
                        {"value": row[f]},
                        f"{f} {d} is outside {lo}..{hi}",
                    )
                )
    return _result("implausible_date", f"dates must lie within {lo}..{hi}", issues)


def malformed_identifier(ctx: Context) -> CheckResult:
    q = ctx.settings.quality
    fields = [f for f in q.identifier_fields if ctx.view.mapped(f)]
    if not fields:
        return _na(
            "malformed_identifier", "no identifier field (" + ", ".join(q.identifier_fields) + ") is mapped"
        )
    issues = []
    for row in ctx.rows:
        for f in fields:
            value = row[f]
            if not isinstance(value, str):
                continue
            problems = []
            if has_control_chars(value):
                hidden = sorted({f"U+{ord(c):04X}" for c in value if unicodedata.category(c) in ("Cc", "Cf")})
                problems.append("contains control/invisible characters " + ", ".join(hidden))
            if len(value) > q.identifier_max_length:
                problems.append(f"longer than {q.identifier_max_length} characters")
            if problems:
                issues.append(
                    RawIssue(
                        f,
                        row["txn_id"],
                        row["source_row"],
                        {"value": value},
                        f"{f} {'; '.join(problems)}",
                        ctx.fix_for(row["txn_id"], f),
                    )
                )
    return _result("malformed_identifier", f"identifier fields: {', '.join(fields)}", issues)


# ----------------------------------------------------------------------------------------------- uniqueness


def exact_duplicate(ctx: Context) -> CheckResult:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in ctx.rows:
        if row["posted_at"] is None or row["amount_minor"] is None:
            continue
        groups[tuple(row[f] for f in DUPLICATE_KEY)].append(row)
    issues = []
    dup_groups = [g for g in groups.values() if len(g) > 1]
    for g in dup_groups:
        first = g[0]
        for row in g[1:]:
            issues.append(
                RawIssue(
                    None,
                    row["txn_id"],
                    row["source_row"],
                    {
                        "duplicate_of_txn": first["txn_id"],
                        "duplicate_of_row": first["source_row"],
                        "group_size": len(g),
                    },
                    f"identical to row {first['source_row']} in every transaction field",
                )
            )
    return _result(
        "exact_duplicate",
        "same " + ", ".join(DUPLICATE_KEY) + " (rows without date or amount excluded); the first row of each "
        "group is kept as the reference, later rows are flagged",
        issues,
        {
            "groups": len(dup_groups),
            "rows_in_groups": sum(len(g) for g in dup_groups),
            "redundant_rows": len(issues),
        },
    )


def near_duplicate(ctx: Context, exact: CheckResult) -> CheckResult:
    v, days = ctx.view, ctx.settings.quality.near_duplicate_window_days
    if not v.mapped("payee_raw"):
        return _na("near_duplicate", "no payee column is mapped")
    redundant = {i.txn_id for i in exact.issues}
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in ctx.rows:
        if row["txn_id"] in redundant or None in (row["payee_raw"], row["posted_at"], row["amount_minor"]):
            continue
        payee = collapse_whitespace(row["payee_raw"]).casefold()
        groups[(payee, row["amount_minor"], row["direction"])].append(row)
    issues = []
    for g in groups.values():
        g.sort(key=lambda r: (r["posted_at"], r["source_row"]))
        for prev, row in zip(g, g[1:], strict=False):
            gap = datetime.fromisoformat(row["posted_at"]) - datetime.fromisoformat(prev["posted_at"])
            if gap.total_seconds() <= days * 86400:
                issues.append(
                    RawIssue(
                        None,
                        row["txn_id"],
                        row["source_row"],
                        {
                            "near_txn": prev["txn_id"],
                            "near_row": prev["source_row"],
                            "hours_apart": round(gap.total_seconds() / 3600, 2),
                        },
                        f"same payee, amount and direction as row {prev['source_row']} within {days} days",
                    )
                )
    basis = (
        f"same payee (case/space-insensitive), amount and direction within {days} days, "
        "not an exact duplicate"
    )
    if v.time_basis == SIMULATED:
        basis += "; measured on the simulated clock"
    return _result("near_duplicate", basis, issues)


def duplicate_source_id(ctx: Context) -> CheckResult:
    v = ctx.view
    id_aliases = {normalize_header(a) for a in ctx.settings.mapping.aliases["txn_id"]}
    mapped_cols = set(v.mapping["fields"].values()) | set(v.mapping.get("label_columns", []))
    extra_cols = [h for h in v.header if h not in mapped_cols and normalize_header(h) in id_aliases]
    sources: list[tuple[str, str]] = [("field", "reference")] if v.mapped("reference") else []
    sources += [("extra", c) for c in extra_cols]
    enforced = v.id_source == "source_column"
    if not sources:
        if enforced:
            return CheckResult(
                "duplicate_source_id",
                "ok",
                "transaction IDs come from a source column whose uniqueness was enforced at ingestion (P3)",
            )
        return _na("duplicate_source_id", "no source transaction or document ID column exists")
    issues, details = [], {}
    for kind, name in sources:
        first: dict[str, dict] = {}
        dups = 0
        for row in ctx.rows:
            value = row["reference"] if kind == "field" else row["extra"].get(name)
            if value in (None, ""):
                continue
            value = str(value)
            if value in first:
                dups += 1
                f0 = first[value]
                issues.append(
                    RawIssue(
                        "reference" if kind == "field" else None,
                        row["txn_id"],
                        row["source_row"],
                        {
                            "column": v.mapping["fields"]["reference"] if kind == "field" else name,
                            "value": value,
                            "first_txn": f0["txn_id"],
                            "first_row": f0["source_row"],
                        },
                        f"ID {value!r} already used by row {f0['source_row']}",
                        key=name,
                    )
                )
            else:
                first[value] = row
        details[name] = {"duplicate_rows": dups}
    basis = "document/transaction ID columns: " + ", ".join(n for _, n in sources)
    if enforced:
        basis += "; the transaction ID column itself was enforced unique at ingestion (P3)"
    return _result("duplicate_source_id", basis, issues, details)


# ----------------------------------------------------------------------------------------------- consistency


def negative_amount(ctx: Context) -> CheckResult:
    issues = [
        RawIssue(
            "amount_minor",
            r["txn_id"],
            r["source_row"],
            {"amount_minor": r["amount_minor"]},
            "negative amount (reversal or credit note?)",
        )
        for r in ctx.rows
        if r["amount_minor"] is not None and r["amount_minor"] < 0
    ]
    return _result("negative_amount", "amount_minor < 0", issues)


def zero_amount(ctx: Context) -> CheckResult:
    issues = [
        RawIssue("amount_minor", r["txn_id"], r["source_row"], {"amount_minor": 0}, "zero amount")
        for r in ctx.rows
        if r["amount_minor"] == 0
    ]
    return _result("zero_amount", "amount_minor == 0", issues)


def out_of_period(ctx: Context) -> CheckResult:
    v = ctx.view
    if v.time_basis == SIMULATED:
        return _na(
            "out_of_period", "the dataset uses a simulated clock (PaySim step); it has no accounting period"
        )
    start, end = v.period
    if start is None and end is None:
        return _na("out_of_period", "the case has no audit period (period_start/period_end) declared")
    issues = []
    for row in ctx.rows:
        if row["posted_at"] is None:
            continue
        d = datetime.fromisoformat(row["posted_at"]).date()
        if (start and d < start) or (end and d > end):
            issues.append(
                RawIssue(
                    "posted_at",
                    row["txn_id"],
                    row["source_row"],
                    {"posted_at": row["posted_at"]},
                    f"posted {d}, outside the case period {start or '…'}..{end or '…'}",
                )
            )
    return _result("out_of_period", f"case period {start or '…'}..{end or '…'}", issues)


def _minor(value: Any, sep: str, digits: int) -> int | None:
    try:
        number = parse_decimal(value, sep)
        return None if number is None else to_minor_units(Decimal(number), digits)
    except ValueError:
        return None


def balance_continuity(ctx: Context) -> CheckResult:
    v, s = ctx.view, ctx.settings
    sep = v.mapping["options"].get("decimal_separator") or "."
    digits = s.ingest.minor_units.default_digits
    if v.adapter == "paysim":
        cols = s.adapters.paysim.balance_columns
        if cols.before not in v.header or cols.after not in v.header:
            return _na("balance_continuity", "the PaySim balance columns are absent")
        issues, kinds = [], defaultdict(int)
        for row in ctx.rows:
            before, after = (
                _minor(row["extra"].get(cols.before), sep, digits),
                _minor(row["extra"].get(cols.after), sep, digits),
            )
            if None in (before, after, row["amount_minor"]) or row["direction"] not in ("in", "out"):
                kinds["not_checkable"] += 1
                continue
            sign = 1 if row["direction"] == "in" else -1
            expected = before + sign * row["amount_minor"]
            if after == expected:
                kinds["consistent"] += 1
                continue
            kind = (
                "balance_unchanged"
                if after == before
                else "balance_zeroed"
                if after == 0
                else "other_mismatch"
            )
            kinds[kind] += 1
            issues.append(
                RawIssue(
                    None,
                    row["txn_id"],
                    row["source_row"],
                    {
                        "before": before,
                        "after": after,
                        "amount_minor": row["amount_minor"],
                        "direction": row["direction"],
                        "expected_after": expected,
                        "kind": kind,
                    },
                    f"{cols.after} {after} != {cols.before} {before} "
                    f"{'+' if sign > 0 else '-'} amount {row['amount_minor']} ({kind})",
                    key=kind,
                )
            )
        return _result(
            "balance_continuity",
            f"per-row origin balance arithmetic: {cols.after} = {cols.before} ± amount (simulator output; "
            "known PaySim artefacts such as unchanged or zeroed balances are counted, not corrected)",
            issues,
            dict(sorted(kinds.items())),
        )
    aliases = {normalize_header(a) for a in s.quality.balance.running_balance_aliases}
    mapped = set(v.mapping["fields"].values())
    cols = [h for h in v.header if h not in mapped and normalize_header(h) in aliases]
    if not cols:
        return _na("balance_continuity", "no running-balance column in the source")
    if len(cols) > 1:
        return _na("balance_continuity", f"several running-balance columns ({', '.join(cols)}); none chosen")
    col = cols[0]
    group_by_account = v.mapped("account")
    groups: dict[Any, list[dict]] = defaultdict(list)
    for row in ctx.rows:
        groups[row["account"] if group_by_account else None].append(row)
    transitions = []
    for g in groups.values():
        for prev, row in zip(g, g[1:], strict=False):
            b0, b1 = _minor(prev["extra"].get(col), sep, digits), _minor(row["extra"].get(col), sep, digits)
            if None in (b0, b1, row["amount_minor"], row["direction"]):
                continue
            transitions.append((prev, row, b0, b1))

    def signed(row: dict, debit_raises: bool) -> int:
        d = row["direction"]
        up = d == "in" or (d == "debit" and debit_raises) or (d == "credit" and not debit_raises)
        return row["amount_minor"] if up else -row["amount_minor"]

    if not transitions:
        return _na("balance_continuity", f"running balance {col!r} has no checkable consecutive rows")
    fits = {c: sum(1 for _, r, b0, b1 in transitions if b1 - b0 == signed(r, c)) for c in (True, False)}
    convention = max(fits, key=lambda c: (fits[c], c))
    share = fits[convention] / len(transitions)
    if fits[True] == fits[False] and any(r["direction"] in ("debit", "credit") for _, r, _, _ in transitions):
        return _na(
            "balance_continuity",
            f"running balance {col!r}: debit-raises and credit-raises conventions "
            "explain the data equally; declare it before checking",
        )
    if share < s.quality.balance.min_convention_share:
        return _na(
            "balance_continuity",
            f"running balance {col!r}: no sign convention explains at least "
            f"{s.quality.balance.min_convention_share:.0%} of transitions ({share:.0%})",
        )
    issues = []
    for prev, row, b0, b1 in transitions:
        expected = b0 + signed(row, convention)
        if b1 != expected:
            issues.append(
                RawIssue(
                    None,
                    row["txn_id"],
                    row["source_row"],
                    {
                        "column": col,
                        "previous_row": prev["source_row"],
                        "previous_balance": b0,
                        "balance": b1,
                        "expected_balance": expected,
                        "amount_minor": row["amount_minor"],
                        "direction": row["direction"],
                    },
                    f"{col} {b1} != previous {b0} {'+' if signed(row, convention) >= 0 else '-'} "
                    f"{row['amount_minor']} (row {prev['source_row']} → {row['source_row']})",
                )
            )
    conv = "debit raises the balance" if convention else "credit raises the balance"
    return _result(
        "balance_continuity",
        f"running balance {col!r} in file order{' per account' if group_by_account else ''}; "
        "inferred convention: "
        f"{conv} (explains {fits[convention]} of {len(transitions)} transitions)",
        issues,
        {"column": col, "transitions": len(transitions), "convention": conv},
    )


# -------------------------------------------------------------------------------------------- dataset level


def unmapped_columns(ctx: Context) -> CheckResult:
    v = ctx.view
    used = set(v.mapping["fields"].values()) | set(v.mapping.get("label_columns", []))
    cols = [h for h in v.header if h not in used]
    issues = [
        RawIssue(
            None,
            None,
            None,
            {"column": c},
            f"column {c!r} is not mapped to a canonical field; it is kept "
            "verbatim in `extra` and not validated",
            key=c,
        )
        for c in cols
    ]
    return _result("unmapped_columns", "source columns not mapped and not labels", issues, {"columns": cols})


def field_coverage(ctx: Context) -> CheckResult:
    v, q = ctx.view, ctx.settings.quality
    not_applicable = PaySimConverter.NOT_APPLICABLE if v.adapter == "paysim" else {}
    fields = v.mapping["fields"]
    coverage, issues = {}, []
    for f in FIELDS:
        source = "amount" if f == "amount_minor" else f
        if f in not_applicable:
            coverage[f] = {"status": "not_applicable", "reason": not_applicable[f]}
        elif source in fields:
            coverage[f] = {"status": "mapped", "column": fields[source]}
        elif f == "amount_minor" and "debit_amount" in fields:
            coverage[f] = {"status": "derived", "from": [fields["debit_amount"], fields["credit_amount"]]}
        elif f == "direction":
            basis = (
                "debit/credit columns"
                if "debit_amount" in fields
                else f"declared: {v.mapping['options'].get('direction')}"
                if v.mapping["options"].get("direction")
                else f"adapter {v.adapter} from {fields.get('txn_type')!r}"
            )
            coverage[f] = {"status": "derived", "from": basis}
        elif f == "currency" and v.mapping["options"].get("currency"):
            coverage[f] = {"status": "declared", "value": v.mapping["options"]["currency"]}
        else:
            coverage[f] = {"status": "not_in_source"}
            if f in q.expected_ledger_fields:
                issues.append(
                    RawIssue(
                        f,
                        None,
                        None,
                        {"field": f},
                        f"a corporate ledger normally has {f}, but this source has no such column; "
                        "checks and analyses that need it are unavailable",
                        key=f,
                    )
                )
    return _result(
        "field_coverage",
        "which canonical fields the source provides, derives, or cannot have",
        issues,
        coverage,
    )


def needs_normalization(ctx: Context) -> CheckResult:
    issues = []
    for p in ctx.fixes.values():
        if p.requires_confirmation or p.fix_id in ctx.used_fixes:
            continue
        issues.append(
            RawIssue(
                p.field,
                p.txn_id,
                p.source_row,
                {"value": p.original_value, "proposed": p.proposed_value, "transform": p.transform},
                f"{p.field} can be normalised ({p.transform})",
                p.fix_id,
            )
        )
    return _result(
        "needs_normalization",
        "safe, deterministic normalisations available (applied only after an analyst accepts them)",
        issues,
    )


def ingestion_issues(ctx: Context, surfaced: dict[str, int]) -> CheckResult:
    total: dict[str, int] = defaultdict(int)
    for items in ctx.view.p3_issues.values():
        for i in items:
            total[f"{i['field']}.{i['code']}"] += 1
    count = sum(total.values())
    where = ", ".join(f"{check} ({n})" for check, n in surfaced.items()) or "none"
    # Status `ok`: this check raises no issues of its own; each P3 record is reported exactly once by the
    # check named in `surfaced_by` (or was resolved by an applied transformation).
    return CheckResult(
        "ingestion_issues",
        "ok",
        f"{count} conversion problem(s) recorded by P3 ingestion, each reported once by: {where}",
        {"recorded": count, "by_field_and_code": dict(sorted(total.items())), "surfaced_by": surfaced},
    )
