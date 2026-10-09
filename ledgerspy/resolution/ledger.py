"""Ledger parties: one party record per distinct counterparty name key of a canonical ledger.

Reads only canonical fields already reduced by P6 (`FeatureRow`: party keys, flow, reference, account,
amount). Behaviour summaries are supporting evidence for name pairs (shared reference patterns,
counterparties, accounts, a similar typical amount); they never decide a match.
"""

import re
from collections import Counter, defaultdict
from statistics import median
from typing import Any

from ledgerspy.config.schema import ResolutionConfig
from ledgerspy.resolution import engine, records
from ledgerspy.resolution.records import MasterRecord, PartyRecord

_DIGITS = re.compile(r"\d+")


def parties(rows, account_codes: bool) -> list[PartyRecord]:
    roles: dict[str, set[str]] = defaultdict(set)
    txns: Counter = Counter()
    refs: dict[str, Counter] = defaultdict(Counter)
    opposite: dict[str, Counter] = defaultdict(Counter)
    accounts: dict[str, Counter] = defaultdict(Counter)
    amounts: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        for role, me, other in (("payer", r.payer, r.payee), ("payee", r.payee, r.payer)):
            if not me:
                continue
            roles[me].add(role)
            txns[me] += 1
            if other:
                opposite[me][other] += 1
            if r.reference:
                pat = _DIGITS.sub("#", r.reference).strip()
                if sum(c.isalpha() for c in pat) >= 3:
                    refs[me][pat] += 1
            if r.account:
                accounts[me][r.account] += 1
            if r.amount is not None:
                amounts[me].append(abs(r.amount))

    def top(c: Counter, n: int) -> list[str]:
        return [k for k, _ in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]

    out = []
    for key in sorted(roles):
        behaviour: dict[str, Any] = {
            "transactions": txns[key],
            "reference_patterns": top(refs[key], 5),
            "counterparties": top(opposite[key], 10),
            "accounts": top(accounts[key], 10),
            "median_amount_minor": median(amounts[key]) if amounts[key] else None,
        }
        out.append(records.ledger_party(key, tuple(sorted(roles[key])), behaviour, {}, account_codes))
    return out


def resolve_dataset(
    rows, masters: tuple[MasterRecord, ...], cfg: ResolutionConfig, account_codes: bool
) -> engine.Resolution:
    recs = parties(rows, account_codes)
    if not account_codes:  # master data describes legal entities; it never applies to simulator account codes
        recs += [records.from_master(m, cfg) for m in masters]
    return engine.resolve(recs, cfg)
