"""Point-in-time machinery and the reusable peer-group hierarchy.

History rule: the history of transaction T is every row whose posting timestamp is strictly earlier than T's.
Rows are processed in timestamp groups: every row of a group is evaluated against the state *before* the
group, and only then is the group added. So rows that share a timestamp never see each other, input order
never matters, and nothing from T's own timestamp or later can reach T. Rows without a timestamp get no
history-based value and are never part of anyone's history.

Peer hierarchy (one definition, used by every peer-relative feature):

    counterparty  same counterparty + flow       ≥ min_entity_history prior rows
    → account     same GL/category account + flow ≥ min_peer_history   (only if `account` is a category)
    → txn_type    same transaction type + flow    ≥ min_peer_history   (only if a type is mapped)
    → global      same flow                       ≥ min_peer_history
    → unavailable the value is null; nothing is fabricated

Levels whose capability the dataset lacks are skipped. The level used is reported per row as the feature's
basis. Baselines use at most the `features.baseline_window` most recent prior observations of the group.

Versioning: a feature's definition hash covers its own function, not these shared helpers. A change to the
behaviour of anything here must bump the version of every feature that uses it (and regenerate the lock).
"""

import math
from bisect import bisect_left
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ledgerspy.features.data import FeatureInput, FeatureRow

MAD_TO_SIGMA = 1.4826


def timeline(inp: FeatureInput) -> list[list[FeatureRow]]:
    """Rows with a timestamp, grouped by identical timestamp, in time order; ties by (source_row, txn_id),
    so the result never depends on the order rows are passed in."""

    def build() -> list[list[FeatureRow]]:
        dated = sorted(
            (r for r in inp.rows if r.ts is not None), key=lambda r: (r.ts, r.source_row, r.txn_id)
        )
        groups: list[list[FeatureRow]] = []
        for r in dated:
            if groups and groups[-1][0].ts == r.ts:
                groups[-1].append(r)
            else:
                groups.append([r])
        return groups

    return inp.memo("timeline", build)


@dataclass
class History:
    """The prior rows of one key, in time order."""

    rows: list[FeatureRow] = field(default_factory=list)
    times: list[datetime] = field(default_factory=list)

    def add(self, r: FeatureRow) -> None:
        self.rows.append(r)
        self.times.append(r.ts)  # type: ignore[arg-type]

    def __len__(self) -> int:
        return len(self.rows)

    def since(self, start: datetime) -> list[FeatureRow]:
        """Prior rows with timestamp ≥ start."""
        return self.rows[bisect_left(self.times, start) :]

    def before(self, end: datetime) -> list[FeatureRow]:
        return self.rows[: bisect_left(self.times, end)]


def scan(
    inp: FeatureInput,
    key: Callable[[FeatureRow], Hashable | None],
    compute: Callable[[FeatureRow, History], Any],
) -> list[Any]:
    """`compute(row, prior history of its key)` for every row, point in time; null without key/timestamp."""
    hist: dict[Hashable, History] = {}
    out: list[Any] = [None] * len(inp.rows)
    empty = History()
    for group in timeline(inp):
        for r in group:
            k = key(r)
            if k is not None:
                out[r.i] = compute(r, hist.get(k, empty))
        for r in group:
            k = key(r)
            if k is not None:
                hist.setdefault(k, History()).add(r)
    return out


def scan_groups(inp: FeatureInput, compute: Callable[[FeatureRow, dict[str, Any]], Any], add) -> list[Any]:
    """Like `scan`, with a free-form state: `compute(row, state)` then `add(row, state)` after each group."""
    state: dict[str, Any] = {}
    out: list[Any] = [None] * len(inp.rows)
    for group in timeline(inp):
        for r in group:
            out[r.i] = compute(r, state)
        for r in group:
            add(r, state)
    return out


def median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def robust_center_scale(values: list[float], floor: float) -> tuple[float, float]:
    """Median and robust scale 1.4826·MAD, never below `floor`."""
    m = median(values)
    mad = median([abs(v - m) for v in values])
    return m, max(MAD_TO_SIGMA * mad, floor)


def log_amount(amount: int | None) -> float | None:
    """ln(1 + |amount| in major units). Null for a null amount."""
    return None if amount is None else math.log1p(abs(amount) / 100)


def days(delta: timedelta) -> float:
    return delta.total_seconds() / 86400


# ------------------------------------------------------------------------------------------- peer hierarchy


@dataclass(frozen=True)
class PeerLevel:
    name: str
    description: str
    key: Callable[[FeatureRow], Hashable | None]
    min_n: int


def peer_levels(inp: FeatureInput) -> list[PeerLevel]:
    """The fallback hierarchy for this dataset (levels the data cannot support are left out)."""
    cfg = inp.cfg
    levels = [
        PeerLevel(
            "counterparty",
            "same counterparty and flow",
            lambda r: (r.cp, r.flow) if r.cp and r.flow else None,
            cfg.min_entity_history,
        )
    ]
    if inp.capabilities.get("account_category") is None:
        levels.append(
            PeerLevel(
                "account",
                "same GL/category account and flow",
                lambda r: (r.account, r.flow) if r.account and r.flow else None,
                cfg.min_peer_history,
            )
        )
    if inp.capabilities.get("txn_type") is None:
        levels.append(
            PeerLevel(
                "txn_type",
                "same transaction type and flow",
                lambda r: (r.txn_type, r.flow) if r.txn_type and r.flow else None,
                cfg.min_peer_history,
            )
        )
    levels.append(PeerLevel("global", "every row of the same flow", lambda r: r.flow, cfg.min_peer_history))
    return levels


PEER_HIERARCHY_DOC = (
    "counterparty (same counterparty + flow, ≥ min_entity_history) → account (same GL/category + flow, ≥ "
    "min_peer_history; only where `account` is a category) → txn_type (same type + flow, ≥ min_peer_history; "
    "only where a type is mapped) → global (same flow, ≥ min_peer_history) → unavailable"
)


def peer_baseline(
    inp: FeatureInput,
    value: Callable[[FeatureRow], float | None],
    compute: Callable[[FeatureRow, list[float]], Any],
    levels: list[PeerLevel] | None = None,
) -> tuple[list[Any], list[str | None]]:
    """For every row: the first hierarchy level with enough prior values → `compute(row, recent prior
    values)`.

    Returns (values, basis). Basis is the level name, "unavailable" when no level has enough history, or None
    when the row has no timestamp or no value of its own.
    """
    levels = levels if levels is not None else peer_levels(inp)
    window = inp.cfg.baseline_window
    hist: list[dict[Hashable, list[float]]] = [{} for _ in levels]
    out: list[Any] = [None] * len(inp.rows)
    basis: list[str | None] = [None] * len(inp.rows)
    for group in timeline(inp):
        for r in group:
            if value(r) is None:
                continue
            basis[r.i] = "unavailable"
            for level, h in zip(levels, hist, strict=True):
                k = level.key(r)
                prior = h.get(k) if k is not None else None
                if prior is not None and len(prior) >= level.min_n:
                    out[r.i] = compute(r, prior[-window:])
                    basis[r.i] = level.name
                    break
        for r in group:
            v = value(r)
            if v is None:
                continue
            for level, h in zip(levels, hist, strict=True):
                k = level.key(r)
                if k is not None:
                    h.setdefault(k, []).append(v)
    return out, basis
