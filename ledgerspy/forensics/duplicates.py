"""Exact and near-duplicate grouping over P6 feature rows (canonical fields of the normalised view).

No all-pairs comparison: rows are grouped by key (exact) or by payee and flow, sorted by time and compared
only inside a sliding window (near). Every match says which attributes matched and how.
"""

import re
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta

from ledgerspy.features.data import FeatureRow

_NON_ALNUM = re.compile(r"[^0-9a-z]")


def norm_reference(ref: str | None) -> str | None:
    """Casefolded, letters and digits only ('GMP6/00195' → 'gmp600195'); None when nothing is left."""
    if ref is None:
        return None
    out = _NON_ALNUM.sub("", ref.casefold())
    return out or None


def _adjacent_transposition(a: str, b: str) -> bool:
    if len(a) != len(b) or a == b:
        return False
    diff = [i for i, (x, y) in enumerate(zip(a, b, strict=True)) if x != y]
    return len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]


def reference_match(a: str | None, b: str | None) -> str | None:
    """How two references match: equal | suffix_letter | transposed. None = they differ (or one is missing).

    A changed digit is *not* a match: consecutive invoice numbers are different invoices.
    """
    x, y = norm_reference(a), norm_reference(b)
    if x is None or y is None:
        return None
    if x == y:
        return "equal"
    short, long_ = sorted((x, y), key=len)
    if len(long_) == len(short) + 1 and long_.startswith(short) and long_[-1].isalpha():
        return "suffix_letter"
    if _adjacent_transposition(x, y):
        return "transposed"
    return None


def amount_match(a: int, b: int, tolerance: float) -> str | None:
    """equal | transposed (same digits, two adjacent swapped) | within_tolerance; None otherwise."""
    if a == b:
        return "equal"
    if _adjacent_transposition(str(abs(a)), str(abs(b))) and (a >= 0) == (b >= 0):
        return "transposed"
    if abs(a - b) <= tolerance * max(abs(a), abs(b)):
        return "within_tolerance"
    return None


def exact_key(r: FeatureRow, use_reference: bool, by_date: bool = True):
    """(payer, payee, amount, flow, posting date — or the exact timestamp of a simulated clock, reference)."""
    if r.amount is None or r.ts is None or r.flow is None or not (r.payee or r.payer):
        return None
    when = r.ts.date() if by_date else r.ts
    return (r.payer, r.payee, r.amount, r.flow, when, norm_reference(r.reference) if use_reference else None)


def exact_groups(
    rows: list[FeatureRow], use_reference: bool, by_date: bool = True
) -> tuple[list[list[FeatureRow]], int]:
    """Groups of ≥ 2 rows with the same key, each ordered by (timestamp, source_row), and the count of rows
    without a key."""
    groups: dict = defaultdict(list)
    skipped = 0
    for r in rows:
        k = exact_key(r, use_reference, by_date)
        if k is None:
            skipped += 1
        else:
            groups[k].append(r)
    out = [sorted(g, key=lambda r: (r.ts, r.source_row, r.txn_id)) for g in groups.values() if len(g) > 1]
    return sorted(out, key=lambda g: (g[0].ts, g[0].source_row)), skipped


@dataclass(frozen=True)
class NearMatch:
    earlier: FeatureRow
    later: FeatureRow
    amount: str
    reference: str  # equal | suffix_letter | transposed | not_compared
    days_apart: float


def near_matches(
    rows: list[FeatureRow], window_days: int, tolerance: float, use_reference: bool
) -> tuple[list[NearMatch], int]:
    """Pairs (earlier, later): same payee and flow, ≤ window_days apart, matching amount and reference.

    Exact duplicates (same exact key) are left to the exact rule. Without references (not mapped, or missing
    on either row) the amount must be exactly equal.
    """
    streams: dict = defaultdict(list)
    skipped = 0
    for r in rows:
        if r.amount is None or r.ts is None or r.flow is None or r.payee is None:
            skipped += 1
        else:
            streams[(r.payee, r.flow)].append(r)
    window = timedelta(days=window_days)
    out = []
    for key in sorted(streams, key=lambda k: (k[0], k[1])):
        s = sorted(streams[key], key=lambda r: (r.ts, r.source_row, r.txn_id))
        times = [r.ts for r in s]
        for j, later in enumerate(s):
            for earlier in s[bisect_left(times, later.ts - window) : j]:
                if exact_key(earlier, use_reference) == exact_key(later, use_reference):
                    continue
                ref = reference_match(earlier.reference, later.reference) if use_reference else None
                amt = amount_match(earlier.amount, later.amount, tolerance)  # type: ignore[arg-type]
                if amt is None:
                    continue
                if ref is None:
                    both = use_reference and earlier.reference and later.reference
                    if both or amt != "equal":
                        continue  # references differ, or no reference to back a non-identical amount
                    ref = "not_compared"
                days = (later.ts - earlier.ts).total_seconds() / 86400  # type: ignore[operator]
                out.append(NearMatch(earlier, later, amt, ref, days))
    return out, skipped
