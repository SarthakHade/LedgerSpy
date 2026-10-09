"""Reconciliation normalisation: ledger and bank rows → comparable `RRow`s (deterministic, read-only).

Inputs are the rows of each dataset's P4 normalised view (P3 canonical values plus logged, reversible P4
transformations); nothing here writes to, or mutates, a canonical row. Every normalised value sits next to the
effective value it came from, and the row keeps the list of fields P4 changed (`normalized_fields`).

Rules (docs/reconciliation.md §3):
- date: the calendar date of `posted_at` (time of day is not compared).
- amount: `amount_minor` (integer minor units, never floating point); must be positive. A negative amount with
  an explicit direction is not reinterpreted (insufficient data), a zero amount is not applicable.
- currency: the ISO code exactly as ingested; a missing currency is never assumed (insufficient data).
- flow: money `in` / `out` of the bank account. Bank lines already carry in/out (confirmed bank mapping). A
  ledger debit/credit becomes in/out only through the run's confirmed `ledger_debit_means`.
- account: letters and digits, upper-cased; shown masked (last 4). Scopes map an account to a ledger filter.
- reference: letters and digits, upper-cased; its `shape` (runs of letters / digits with lengths, e.g. A4N12)
  tells two identifiers of the same kind apart from identifiers of different kinds.
- counterparty: the ledger party on the other side of the money (payee of an outflow, payer of an inflow),
  identified by its P12 canonical entity when entity resolution is used; on a bank line, the known parties
  whose names appear in the description (`named`, see NameIndex).
"""

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from ledgerspy.features.data import party_key
from ledgerspy.hashing import sha256_hex
from ledgerspy.resolution import normalize

_ALNUM = re.compile(r"[^0-9A-Za-z]")
_SEGMENTS = re.compile(r"[/|;\\]")
_RUNS = re.compile(r"[A-Z]+|[0-9]+")
TEXT_FIELDS = ("account", "payer_raw", "payee_raw", "txn_type", "description", "reference")


def alnum(value: str | None) -> str | None:
    if value is None:
        return None
    s = _ALNUM.sub("", unicodedata.normalize("NFKC", str(value))).upper()
    return s or None


def shape(ref: str | None) -> str | None:
    """'SYNR230403000003' → 'A4N12'; 'IC202304FLVC' → 'A2N6A4'."""
    if not ref:
        return None
    return "".join(
        ("A" if m.group()[0].isalpha() else "N") + str(len(m.group())) for m in _RUNS.finditer(ref)
    )


def account_key(norm: str | None) -> str:
    return "A-unidentified" if norm is None else "A-" + sha256_hex(norm)[:12]


def account_label(norm: str | None) -> str:
    return "(unidentified account)" if norm is None else "…" + norm[-4:]


def segments(text: str | None) -> list[list[str]]:
    """Description → token segments ('/'-separated parts), each normalised with the P12 name rules (so
    "QUOR-VEN CARTON L.L.P." gives the same tokens as the ledger name "Quor-ven Carton L.L.P.")."""
    if not text:
        return []
    out = []
    for part in _SEGMENTS.split(text):
        n = normalize.name(part)
        if n.value:
            out.append(n.value.split())
    return out


def _date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


# --------------------------------------------------------------------------------------------- name index


@dataclass(frozen=True)
class _Name:
    entity: str
    tokens: tuple[str, ...]
    text: str


class NameIndex:
    """Known party names (ledger names and, with P12, master names / aliases) found in bank descriptions.

    A name is found when its core tokens (legal form removed) appear consecutively in one description segment,
    or when the segment ends inside the name after at least `min_prefix_chars` matching characters (banks
    truncate narrations; the last token may be cut). Names shorter than `min_name_chars` are never searched.
    When one found name's span lies strictly inside another's, only the longer one counts.
    """

    def __init__(self, names: list[tuple[str, str]], min_name_chars: int, min_prefix_chars: int) -> None:
        self.min_prefix = min_prefix_chars
        self.by_first: dict[str, list[_Name]] = {}  # every name, by its first token
        self.by_two: dict[
            tuple[str, ...], list[_Name]
        ] = {}  # by its first two tokens (one for one-token names)
        seen = set()
        for entity, raw in sorted(set(names)):
            toks = normalize.name(raw).tokens
            text = " ".join(toks)
            if not toks or len(text) < min_name_chars or (entity, toks) in seen:
                continue
            seen.add((entity, toks))
            n = _Name(entity, toks, text)
            self.by_first.setdefault(toks[0], []).append(n)
            self.by_two.setdefault(toks[:2], []).append(n)

    def _match(self, seg: list[str], i: int, n: _Name) -> int:
        """Matched characters of `n` starting at seg[i], or 0."""
        chars = 0
        for k, tok in enumerate(n.tokens):
            j = i + k
            if j >= len(seg):  # the segment ends inside the name: truncated narration
                return chars if k >= 1 and chars >= self.min_prefix else 0
            if seg[j] == tok:
                chars += len(tok) + (1 if k else 0)
                continue
            last = j == len(seg) - 1
            if last and k >= 1 and tok.startswith(seg[j]):  # cut inside the token
                chars += len(seg[j]) + 1
                return chars if chars >= self.min_prefix else 0
            return 0
        return chars

    def find(self, segs: list[list[str]]) -> tuple[frozenset[str], list[dict[str, Any]]]:
        spans = []
        for si, seg in enumerate(segs):
            for i, tok in enumerate(seg):
                # exact lookup by the first two tokens; the full first-token list only where the segment ends
                # within two tokens (a truncated name can then stop inside its second token)
                if i + 2 >= len(seg):
                    cands = self.by_first.get(tok, ())
                else:
                    cands = self.by_two.get((tok,), []) + self.by_two.get((tok, seg[i + 1]), [])
                for n in cands:
                    c = self._match(seg, i, n)
                    if c:
                        width = min(len(n.tokens), len(seg) - i)
                        spans.append((si, i, i + width, n.entity, n.text, c < len(n.text)))
        keep = []
        for s in spans:
            inner = any(
                o is not s
                and o[0] == s[0]
                and o[1] <= s[1]
                and s[2] <= o[2]
                and (o[2] - o[1]) > (s[2] - s[1])
                for o in spans
            )
            if not inner:
                keep.append(s)
        found = sorted({(s[3], s[4], s[5]) for s in keep})
        return frozenset(e for e, _, _ in found), [
            {"entity": e, "name": t, "truncated": tr} for e, t, tr in found
        ]


# --------------------------------------------------------------------------------------------- rows


@dataclass
class RRow:
    side: str  # "ledger" | "bank"
    txn_id: str
    source_row: int
    date: date | None
    amount: int | None
    currency: str | None
    flow: str | None  # "in" | "out" (bank account view)
    scope: str | None  # scope ID, None = outside every scope
    account_norm: str | None
    reference: str | None
    ref_shape: str | None
    text_alnum: str  # description + reference, letters/digits only (reference containment)
    counterparty: str | None  # ledger: raw counterparty name
    cp_entity: str | None  # ledger: counterparty entity; None if unknown
    own_entity: str | None  # ledger: the account-holder side entity (payer of an outflow, payee of an inflow)
    named: frozenset[str] = frozenset()  # bank: known parties named in the description
    named_detail: tuple = ()
    description: str | None = None
    normalized_fields: tuple[str, ...] = ()
    reasons: list[str] = field(default_factory=list)  # insufficient / not-applicable reasons
    na: bool = False  # not applicable (outside scope, zero amount)

    def summary(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "txn_id": self.txn_id,
            "source_row": self.source_row,
            "date": self.date.isoformat() if self.date else None,
            "amount_minor": self.amount,
            "currency": self.currency,
            "flow": self.flow,
            "account": account_label(self.account_norm) if self.side == "bank" else None,
            "reference": self.reference,
            "counterparty": self.counterparty,
            "description": self.description,
            "normalized_fields": list(self.normalized_fields),
        }


@dataclass(frozen=True)
class Scope:
    scope_id: str
    account_key: str
    ledger_column: str | None
    ledger_value: str | None

    def to_dict(self, label: str) -> dict[str, Any]:
        return {
            "scope_id": self.scope_id,
            "account_key": self.account_key,
            "account": label,
            "ledger_filter": None
            if self.ledger_column is None
            else {"column": self.ledger_column, "value": self.ledger_value},
        }


def ledger_value(row: dict[str, Any], column: str) -> str | None:
    v = row.get(column) if column in TEXT_FIELDS else row["extra"].get(column)
    return None if v is None else str(v).strip()


def _amount(row: dict[str, Any], r: RRow) -> None:
    a = row.get("amount_minor")
    if a is None:
        r.reasons.append("missing_amount")
    elif a < 0:
        r.reasons.append("negative_amount_with_direction")
    elif a == 0:
        r.na = True
        r.reasons.append("zero_amount")
    else:
        r.amount = a


def _common(row: dict[str, Any], r: RRow) -> None:
    if r.date is None:
        r.reasons.append("missing_date")
    _amount(row, r)
    if r.currency is None:
        r.reasons.append("missing_currency")
    if r.flow is None:
        r.reasons.append("missing_direction")


def bank_rows(
    view_rows: list[dict[str, Any]], account_mapped: bool, scopes: list[Scope], names: NameIndex
) -> list[RRow]:
    by_account = {s.account_key: s.scope_id for s in scopes}
    out = []
    for row in view_rows:
        acc = alnum(row.get("account"))
        ref = alnum(row.get("reference"))
        desc = row.get("description")
        named, detail = names.find(segments(desc))
        r = RRow(
            side="bank",
            txn_id=row["txn_id"],
            source_row=row["source_row"],
            date=_date(row.get("posted_at")),
            amount=None,
            currency=row.get("currency"),
            flow=row.get("direction") if row.get("direction") in ("in", "out") else None,
            scope=None,
            account_norm=acc,
            reference=row.get("reference"),
            ref_shape=shape(ref),
            text_alnum=(alnum(desc) or "") + "|" + (ref or ""),
            counterparty=None,
            cp_entity=None,
            own_entity=None,
            named=named,
            named_detail=tuple((d["entity"], d["name"], d["truncated"]) for d in detail),
            description=desc,
            normalized_fields=tuple(row.get("normalized_fields", ())),
        )
        if row.get("direction") in ("debit", "credit"):  # pragma: no cover — the bank adapter emits in/out
            r.reasons.append("bank_direction_not_in_out")
        _common(row, r)
        if account_mapped and acc is None:
            r.reasons.append("missing_account")
        else:
            r.scope = by_account.get(account_key(acc))
            if r.scope is None:
                r.na = True
                r.reasons.append("account_not_in_scope")
        out.append(r)
    return out


def ledger_rows(
    view_rows: list[dict[str, Any]],
    scopes: list[Scope],
    debit_means: str | None,
    entity_of_name,
) -> list[RRow]:
    """`entity_of_name(raw) -> entity id | None` (P12 canonical entity, or a party-key fallback)."""
    out = []
    for row in view_rows:
        d = row.get("direction")
        if d in ("in", "out"):
            flow = d
        elif d in ("debit", "credit") and debit_means in ("outflow", "inflow"):
            flow = "out" if (d == "debit") == (debit_means == "outflow") else "in"
        else:
            flow = None
        cp_raw = row.get("payee_raw") if flow == "out" else row.get("payer_raw") if flow == "in" else None
        own_raw = row.get("payer_raw") if flow == "out" else row.get("payee_raw") if flow == "in" else None
        ref = alnum(row.get("reference"))
        hits = [
            s.scope_id
            for s in scopes
            if s.ledger_column is None or ledger_value(row, s.ledger_column) == s.ledger_value
        ]
        r = RRow(
            side="ledger",
            txn_id=row["txn_id"],
            source_row=row["source_row"],
            date=_date(row.get("posted_at")),
            amount=None,
            currency=row.get("currency"),
            flow=flow,
            scope=hits[0] if len(hits) == 1 else None,
            account_norm=None,
            reference=row.get("reference"),
            ref_shape=shape(ref),
            text_alnum=(alnum(row.get("description")) or "") + "|" + (ref or ""),
            counterparty=cp_raw,
            cp_entity=entity_of_name(cp_raw) if cp_raw else None,
            own_entity=entity_of_name(own_raw) if own_raw else None,
            description=row.get("description"),
            normalized_fields=tuple(row.get("normalized_fields", ())),
        )
        _common(row, r)
        if len(hits) > 1:
            r.reasons.append("ambiguous_scope")
        elif not hits:
            r.na = True
            r.reasons.append("outside_scope")
        out.append(r)
    return out


def fallback_entity(raw: str | None) -> str | None:
    """Without P12: the light party key (spelling / whitespace only)."""
    k = party_key(raw)
    return None if k is None else "n:" + k
