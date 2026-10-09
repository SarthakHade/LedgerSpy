"""Duplicate CANDIDATES on each side: reported, never excluded or deleted; confirmation is an analyst's.

Only rows that can be reconciled (in a scope, with date, amount, currency and direction) are compared, inside
the same scope, currency, flow and amount:

- bank `exact`: the same account, date, description and reference — an identical statement line repeated;
- bank `near`: the same counterparty signature (the known parties named in the description, else the
  description without tokens that contain digits) on dates chained within `duplicates.near_window_days`;
- ledger `same_reference`: the same counterparty and the same non-empty reference;
- ledger `near`: the same counterparty on dates chained within `duplicates.near_window_days`.

A group that equals another group's rows is reported once (the stricter type wins).
"""

from collections import defaultdict
from typing import Any

from ledgerspy.config.schema import ReconciliationConfig
from ledgerspy.hashing import canonical_json, sha256_hex
from ledgerspy.reconciliation.records import RRow, alnum, segments

BASIS = {
    (
        "bank",
        "exact",
    ): "identical statement lines: same account, date, amount, direction, description and reference",
    (
        "bank",
        "near",
    ): "same account, amount, direction and counterparty on dates within the near-duplicate window",
    ("ledger", "same_reference"): "same counterparty, amount, direction and reference",
    ("ledger", "near"): "same counterparty, amount and direction on dates within the near-duplicate window",
}


def group_id(side: str, kind: str, ids: list[str]) -> str:
    return "RD-" + sha256_hex(canonical_json([side, kind, sorted(ids)]))[:16]


def _eligible(rows: list[RRow]) -> list[RRow]:
    return sorted(
        (r for r in rows if r.scope is not None and not r.na and not r.reasons), key=lambda r: r.txn_id
    )


def _chains(rows: list[RRow], window: int) -> list[list[RRow]]:
    rows = sorted(rows, key=lambda r: (r.date, r.txn_id))
    out, cur = [], [rows[0]]
    for r in rows[1:]:
        if (r.date - cur[-1].date).days <= window:
            cur.append(r)
        else:
            out.append(cur)
            cur = [r]
    out.append(cur)
    return [c for c in out if len(c) >= 2]


def _signature(b: RRow) -> str:
    if b.named:
        return "named:" + "|".join(sorted(b.named))
    toks = [t for seg in segments(b.description) for t in seg if not any(ch.isdigit() for ch in t)]
    return "text:" + " ".join(toks)


def _group(side: str, kind: str, rows: list[RRow]) -> dict[str, Any]:
    rows = sorted(rows, key=lambda r: (r.date, r.txn_id))
    ids = [r.txn_id for r in rows]
    return {
        "group_id": group_id(side, kind, ids),
        "side": side,
        "type": kind,
        "status": "candidate",
        "confirmed": False,
        "txn_ids": sorted(ids),
        "rows": [r.summary() for r in rows],
        "scope_id": rows[0].scope,
        "currency": rows[0].currency,
        "flow": rows[0].flow,
        "amount_minor": rows[0].amount,
        "first_date": rows[0].date.isoformat(),
        "last_date": rows[-1].date.isoformat(),
        "basis": BASIS[(side, kind)],
    }


def find(ledger: list[RRow], bank: list[RRow], cfg: ReconciliationConfig) -> list[dict[str, Any]]:
    window = cfg.duplicates.near_window_days
    groups: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()

    def add(side: str, kind: str, rows: list[RRow]) -> None:
        key = (side, tuple(sorted(r.txn_id for r in rows)))
        if key not in seen:
            seen.add(key)
            groups.append(_group(side, kind, rows))

    exact: dict[tuple, list[RRow]] = defaultdict(list)
    near: dict[tuple, list[RRow]] = defaultdict(list)
    for b in _eligible(bank):
        base = (b.scope, b.currency, b.flow, b.amount)
        exact[(*base, b.account_norm, b.date, alnum(b.description), alnum(b.reference))].append(b)
        near[(*base, _signature(b))].append(b)
    for k in sorted(exact, key=repr):
        if len(exact[k]) >= 2:
            add("bank", "exact", exact[k])
    for k in sorted(near, key=repr):
        if len(near[k]) >= 2:
            for chain in _chains(near[k], window):
                add("bank", "near", chain)

    by_ref: dict[tuple, list[RRow]] = defaultdict(list)
    by_cp: dict[tuple, list[RRow]] = defaultdict(list)
    for r in _eligible(ledger):
        cp = r.cp_entity or alnum(r.counterparty)
        if cp is None:
            continue
        base = (r.scope, r.currency, r.flow, r.amount, cp)
        ref = alnum(r.reference)
        if ref:
            by_ref[(*base, ref)].append(r)
        by_cp[base].append(r)
    for k in sorted(by_ref, key=repr):
        if len(by_ref[k]) >= 2:
            add("ledger", "same_reference", by_ref[k])
    for k in sorted(by_cp, key=repr):
        if len(by_cp[k]) >= 2:
            for chain in _chains(by_cp[k], window):
                add("ledger", "near", chain)
    return sorted(groups, key=lambda g: g["group_id"])
