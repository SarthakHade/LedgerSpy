"""Entity-level features: one value per counterparty stream (counterparty key + flow) over the whole dataset.

These are POST-PERIOD analytical summaries ("over the audit period, this vendor …"). They use every row of the
period, so they must never be attached to a transaction as if they were known when it was posted.
Point-in-time counterparts exist at transaction level (cp_prior_count, cp_days_since_prev, …).

Subject ID: "<flow>:<counterparty key>", e.g. "out:javrel office supplies private limited".
"""

from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any

from ledgerspy.features.data import FeatureInput, FeatureRow
from ledgerspy.features.history import days, log_amount, median, robust_center_scale
from ledgerspy.features.spec import Kind, Level, Missing, Semantics, Unusual, feature
from ledgerspy.features.transaction import ols_slope_per_30d, reference_number

STREAM = "counterparty stream over the whole dataset (all rows of the period)"


def ent(**meta: Any):
    return feature(level=Level.ENTITY, semantics=Semantics.POST_PERIOD, scope=STREAM, **meta)


def streams(inp: FeatureInput) -> dict[str, list[FeatureRow]]:
    """Subject ID → its rows in time order (ties and rows without a timestamp: by source_row, txn_id)."""

    def build() -> dict[str, list[FeatureRow]]:
        out: dict[str, list[FeatureRow]] = defaultdict(list)
        for r in inp.rows:
            if r.cp and r.flow:
                out[f"{r.flow}:{r.cp}"].append(r)
        for rows in out.values():
            rows.sort(key=lambda r: (r.ts is None, r.ts or datetime.min, r.source_row, r.txn_id))
        return dict(sorted(out.items()))

    return inp.memo("streams", build)


def subjects(inp: FeatureInput) -> list[str]:
    return list(streams(inp))


def per_stream(inp: FeatureInput, fn) -> list[Any]:
    return [fn(rows) for rows in streams(inp).values()]


def _amounts(rows: list[FeatureRow]) -> list[int]:
    return [r.amount for r in rows if r.amount is not None]


def _dated(rows: list[FeatureRow]) -> list[FeatureRow]:
    return [r for r in rows if r.ts is not None]


def _day(dated: list[FeatureRow], i: int) -> str | None:
    return dated[i].ts.date().isoformat() if dated else None  # type: ignore[union-attr]


@ent(
    name="ent_txn_count",
    version=1,
    family="profile",
    description="Number of transactions of the stream in the dataset.",
    inputs=("direction", "payer_raw/payee_raw"),
    kind=Kind.INT,
    units="transactions",
    missing=Missing.NULL_IF_NOT_APPLICABLE,
    missing_detail="never null (a stream exists only if it has rows)",
    unusual=Unusual.CONTEXT,
    requires=("direction", "counterparty"),
)
def ent_txn_count(inp: FeatureInput) -> list[Any]:
    return per_stream(inp, len)


@ent(
    name="ent_total_amount_minor",
    version=1,
    family="profile",
    description="Sum of the stream's amounts (rows with a null amount are skipped).",
    inputs=("amount_minor", "direction", "payer_raw/payee_raw"),
    kind=Kind.INT,
    units="minor currency units",
    missing=Missing.NULL_IF_INPUT_MISSING,
    missing_detail="null when no row of the stream has an amount",
    unusual=Unusual.HIGHER,
    requires=("amount", "direction", "counterparty"),
)
def ent_total_amount_minor(inp: FeatureInput) -> list[Any]:
    return per_stream(inp, lambda rows: sum(_amounts(rows)) if _amounts(rows) else None)


@ent(
    name="ent_mean_amount_minor",
    version=1,
    family="profile",
    description="Mean of the stream's amounts.",
    inputs=("amount_minor", "direction", "payer_raw/payee_raw"),
    kind=Kind.FLOAT,
    units="minor currency units",
    missing=Missing.NULL_IF_INPUT_MISSING,
    missing_detail="null when no row of the stream has an amount",
    unusual=Unusual.HIGHER,
    requires=("amount", "direction", "counterparty"),
)
def ent_mean_amount_minor(inp: FeatureInput) -> list[Any]:
    return per_stream(inp, lambda rows: sum(a := _amounts(rows)) / len(a) if _amounts(rows) else None)


@ent(
    name="ent_median_amount_minor",
    version=1,
    family="profile",
    description="Median of the stream's amounts (historical baseline over the period).",
    inputs=("amount_minor", "direction", "payer_raw/payee_raw"),
    kind=Kind.FLOAT,
    units="minor currency units",
    missing=Missing.NULL_IF_INPUT_MISSING,
    missing_detail="null when no row of the stream has an amount",
    unusual=Unusual.HIGHER,
    requires=("amount", "direction", "counterparty"),
)
def ent_median_amount_minor(inp: FeatureInput) -> list[Any]:
    return per_stream(inp, lambda rows: median(_amounts(rows)) if _amounts(rows) else None)


@ent(
    name="ent_log_amount_dispersion",
    version=1,
    family="profile",
    description="Robust dispersion of the stream's amounts: 1.4826·MAD of log_amount (0 = every amount "
    "equal).",
    inputs=("amount_minor", "direction", "payer_raw/payee_raw"),
    kind=Kind.FLOAT,
    units="log-amount units",
    missing=Missing.NULL_IF_INSUFFICIENT_HISTORY,
    missing_detail="null with fewer than 2 amounts",
    unusual=Unusual.LOWER,
    requires=("amount", "direction", "counterparty"),
)
def ent_log_amount_dispersion(inp: FeatureInput) -> list[Any]:
    def disp(rows: list[FeatureRow]) -> float | None:
        logs = [log_amount(a) for a in _amounts(rows)]
        return robust_center_scale(logs, 0.0)[1] if len(logs) >= 2 else None  # type: ignore[arg-type]

    return per_stream(inp, disp)


@ent(
    name="ent_first_seen",
    version=1,
    family="profile",
    description="Posting date of the stream's first transaction in the dataset.",
    inputs=("posted_at", "direction", "payer_raw/payee_raw"),
    kind=Kind.DATE,
    units="date",
    missing=Missing.NULL_IF_INPUT_MISSING,
    missing_detail="null when no row of the stream has a timestamp",
    unusual=Unusual.CONTEXT,
    requires=("calendar_time", "direction", "counterparty"),
)
def ent_first_seen(inp: FeatureInput) -> list[Any]:
    return per_stream(inp, lambda rows: _day(_dated(rows), 0))


@ent(
    name="ent_last_seen",
    version=1,
    family="profile",
    description="Posting date of the stream's last transaction in the dataset.",
    inputs=("posted_at", "direction", "payer_raw/payee_raw"),
    kind=Kind.DATE,
    units="date",
    missing=Missing.NULL_IF_INPUT_MISSING,
    missing_detail="null when no row of the stream has a timestamp",
    unusual=Unusual.CONTEXT,
    requires=("calendar_time", "direction", "counterparty"),
)
def ent_last_seen(inp: FeatureInput) -> list[Any]:
    return per_stream(inp, lambda rows: _day(_dated(rows), -1))


@ent(
    name="ent_max_gap_days",
    version=1,
    family="frequency",
    description="Longest gap between consecutive transactions of the stream (dormant-period length).",
    inputs=("posted_at", "direction", "payer_raw/payee_raw"),
    kind=Kind.FLOAT,
    units="days",
    missing=Missing.NULL_IF_INSUFFICIENT_HISTORY,
    missing_detail="null with fewer than 2 dated rows",
    unusual=Unusual.HIGHER,
    requires=("calendar_time", "direction", "counterparty"),
)
def ent_max_gap_days(inp: FeatureInput) -> list[Any]:
    def gap(rows: list[FeatureRow]) -> float | None:
        d = _dated(rows)
        if len(d) < 2:
            return None
        return max(days(b.ts - a.ts) for a, b in zip(d, d[1:], strict=False))  # type: ignore[operator]

    return per_stream(inp, gap)


def _period_end(inp: FeatureInput):
    return inp.memo("period_end", lambda: max((r.ts for r in inp.rows if r.ts), default=None))


def _recent_split(inp: FeatureInput, rows: list[FeatureRow]):
    end = _period_end(inp)
    cut = end - timedelta(days=inp.cfg.recent_days)
    d = _dated(rows)
    return [r for r in d if r.ts < cut], [r for r in d if r.ts >= cut], cut


@ent(
    name="ent_recent_count_ratio",
    version=1,
    family="velocity",
    description="Recent vs historical activity: rate of rows in the last recent_days of the dataset ÷ the "
    "stream's rate before that (from its first row).",
    inputs=("posted_at", "direction", "payer_raw/payee_raw"),
    kind=Kind.FLOAT,
    units="ratio of rates",
    missing=Missing.NULL_IF_INSUFFICIENT_HISTORY,
    missing_detail="null when the stream's history before the recent window spans fewer than recent_days",
    unusual=Unusual.BOTH,
    requires=("calendar_time", "direction", "counterparty"),
    params=("recent_days",),
)
def ent_recent_count_ratio(inp: FeatureInput) -> list[Any]:
    def ratio(rows: list[FeatureRow]) -> float | None:
        before, recent, cut = _recent_split(inp, rows)
        if not before:
            return None
        span = days(cut - before[0].ts)  # type: ignore[operator]
        if span < inp.cfg.recent_days:
            return None
        return (len(recent) / inp.cfg.recent_days) / (len(before) / span)

    return per_stream(inp, ratio)


@ent(
    name="ent_recent_amount_ratio",
    version=1,
    family="behaviour",
    description="Recent vs historical amount: median amount in the last recent_days of the dataset ÷ the "
    "stream's median amount before that.",
    inputs=("amount_minor", "posted_at", "direction", "payer_raw/payee_raw"),
    kind=Kind.FLOAT,
    units="ratio",
    missing=Missing.NULL_IF_INSUFFICIENT_HISTORY,
    missing_detail="null when either window has no amount or the earlier median is 0",
    unusual=Unusual.BOTH,
    requires=("amount", "calendar_time", "direction", "counterparty"),
    params=("recent_days",),
)
def ent_recent_amount_ratio(inp: FeatureInput) -> list[Any]:
    def ratio(rows: list[FeatureRow]) -> float | None:
        before, recent, _ = _recent_split(inp, rows)
        a, b = _amounts(before), _amounts(recent)
        if not a or not b or median(a) == 0:
            return None
        return median(b) / median(a)

    return per_stream(inp, ratio)


@ent(
    name="ent_round_share",
    version=1,
    family="amount",
    description="Share of the stream's non-zero amounts that are multiples of 10^round_level_min major "
    "units.",
    inputs=("amount_minor", "direction", "payer_raw/payee_raw"),
    kind=Kind.FLOAT,
    units="fraction",
    missing=Missing.NULL_IF_INPUT_MISSING,
    missing_detail="null when the stream has no non-zero amount",
    unusual=Unusual.HIGHER,
    requires=("amount", "direction", "counterparty"),
    params=("round_level_min",),
)
def ent_round_share(inp: FeatureInput) -> list[Any]:
    unit = 100 * 10**inp.cfg.round_level_min

    def share(rows: list[FeatureRow]) -> float | None:
        a = [x for x in _amounts(rows) if x]
        return sum(1 for x in a if x % unit == 0) / len(a) if a else None

    return per_stream(inp, share)


@ent(
    name="ent_share_of_flow",
    version=1,
    family="relationship",
    description="Concentration: the stream's total amount ÷ the total amount of all rows of the same flow.",
    inputs=("amount_minor", "direction", "payer_raw/payee_raw"),
    kind=Kind.FLOAT,
    units="fraction",
    missing=Missing.NULL_IF_INPUT_MISSING,
    missing_detail="null when the stream or its flow has no amount",
    unusual=Unusual.HIGHER,
    requires=("amount", "direction", "counterparty"),
)
def ent_share_of_flow(inp: FeatureInput) -> list[Any]:
    totals: Counter = Counter()
    for r in inp.rows:
        if r.flow and r.amount is not None:
            totals[r.flow] += abs(r.amount)
    out = []
    for sid, rows in streams(inp).items():
        a = _amounts(rows)
        flow_total = totals[sid.split(":", 1)[0]]
        out.append(sum(abs(x) for x in a) / flow_total if a and flow_total else None)
    return out


def _party_sides(inp: FeatureInput) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    def build():
        payers_of: dict[str, set[str]] = defaultdict(set)
        payees_of: dict[str, set[str]] = defaultdict(set)
        for r in inp.rows:
            if r.payer and r.payee:
                payers_of[r.payee].add(r.payer)
                payees_of[r.payer].add(r.payee)
        return payers_of, payees_of

    return inp.memo("party_sides", build)


@ent(
    name="ent_fan_in",
    version=1,
    family="relationship",
    description="Distinct payers that paid this counterparty anywhere in the dataset (fan-in of the party).",
    inputs=("payer_raw", "payee_raw"),
    kind=Kind.INT,
    units="parties",
    missing=Missing.NULL_IF_NOT_APPLICABLE,
    missing_detail="never null when available (0 = nobody paid this party)",
    unusual=Unusual.HIGHER,
    requires=("direction", "counterparty", "payer_payee"),
)
def ent_fan_in(inp: FeatureInput) -> list[Any]:
    payers_of, _ = _party_sides(inp)
    return [len(payers_of.get(sid.split(":", 1)[1], ())) for sid in subjects(inp)]


@ent(
    name="ent_fan_out",
    version=1,
    family="relationship",
    description="Distinct payees this counterparty paid anywhere in the dataset (fan-out of the party).",
    inputs=("payer_raw", "payee_raw"),
    kind=Kind.INT,
    units="parties",
    missing=Missing.NULL_IF_NOT_APPLICABLE,
    missing_detail="never null when available (0 = this party paid nobody)",
    unusual=Unusual.HIGHER,
    requires=("direction", "counterparty", "payer_payee"),
)
def ent_fan_out(inp: FeatureInput) -> list[Any]:
    _, payees_of = _party_sides(inp)
    return [len(payees_of.get(sid.split(":", 1)[1], ())) for sid in subjects(inp)]


@ent(
    name="ent_after_hours_share",
    version=1,
    family="timing",
    description="Share of the stream's rows posted outside configured business hours.",
    inputs=("posted_at", "config: features.calendar.business_hours"),
    kind=Kind.FLOAT,
    units="fraction",
    missing=Missing.NULL_IF_INPUT_MISSING,
    missing_detail="null when no row of the stream has a timestamp",
    unusual=Unusual.HIGHER,
    requires=("time_of_day", "direction", "counterparty"),
    params=("calendar.business_hours",),
)
def ent_after_hours_share(inp: FeatureInput) -> list[Any]:
    start, end = (time(*map(int, t.split(":"))) for t in inp.cfg.calendar.business_hours)

    def share(rows: list[FeatureRow]) -> float | None:
        d = _dated(rows)
        if not d:
            return None
        return sum(1 for r in d if not start <= r.ts.time() <= end) / len(d)  # type: ignore[union-attr]

    return per_stream(inp, share)


@ent(
    name="ent_non_working_share",
    version=1,
    family="timing",
    description="Share of the stream's rows posted on a configured non-working weekday or configured "
    "holiday.",
    inputs=(
        "posted_at",
        "config: features.calendar.non_working_weekdays",
        "config: features.calendar.holidays",
    ),
    kind=Kind.FLOAT,
    units="fraction",
    missing=Missing.NULL_IF_INPUT_MISSING,
    missing_detail="null when no row of the stream has a timestamp",
    unusual=Unusual.HIGHER,
    requires=("calendar_time", "direction", "counterparty"),
    params=("calendar.non_working_weekdays", "calendar.holidays"),
)
def ent_non_working_share(inp: FeatureInput) -> list[Any]:
    off = set(inp.cfg.calendar.non_working_weekdays)
    holidays = {date.fromisoformat(d) for d in inp.cfg.calendar.holidays}

    def share(rows: list[FeatureRow]) -> float | None:
        d = _dated(rows)
        if not d:
            return None
        off_days = [r for r in d if r.ts.weekday() in off or r.ts.date() in holidays]  # type: ignore[union-attr]
        return len(off_days) / len(d)

    return per_stream(inp, share)


@ent(
    name="ent_log_amount_trend",
    version=1,
    family="behaviour",
    description="Escalation over the period: least-squares slope of log_amount against time, per 30 days.",
    inputs=("amount_minor", "posted_at", "direction", "payer_raw/payee_raw"),
    kind=Kind.FLOAT,
    units="log-amount change per 30 days",
    missing=Missing.NULL_IF_INSUFFICIENT_HISTORY,
    missing_detail="null with fewer than trend_min_points dated rows with amounts, or all at one instant",
    unusual=Unusual.HIGHER,
    requires=("amount", "calendar_time", "direction", "counterparty"),
    params=("trend_min_points",),
)
def ent_log_amount_trend(inp: FeatureInput) -> list[Any]:
    return per_stream(
        inp,
        lambda rows: ols_slope_per_30d(
            [r for r in _dated(rows) if r.amount is not None], inp.cfg.trend_min_points
        ),
    )


@ent(
    name="ent_reference_sequential_share",
    version=1,
    family="sequence",
    description="Share of consecutive (time-ordered) numbered references of the stream that differ by "
    "exactly 1 (sequential invoices).",
    inputs=("reference", "posted_at", "direction", "payer_raw/payee_raw"),
    kind=Kind.FLOAT,
    units="fraction",
    missing=Missing.NULL_IF_INSUFFICIENT_HISTORY,
    missing_detail="null with fewer than 3 numbered references",
    unusual=Unusual.HIGHER,
    requires=("reference", "timestamp", "direction", "counterparty"),
)
def ent_reference_sequential_share(inp: FeatureInput) -> list[Any]:
    def share(rows: list[FeatureRow]) -> float | None:
        nums = [n for r in _dated(rows) if (n := reference_number(r.reference)) is not None]
        if len(nums) < 3:
            return None
        return sum(1 for a, b in zip(nums, nums[1:], strict=False) if b - a == 1) / (len(nums) - 1)

    return per_stream(inp, share)


@ent(
    name="ent_median_to_peer_ratio",
    version=1,
    family="behaviour",
    description="The stream's median amount ÷ the median of the median amounts of the other streams with the "
    "same flow and the same most frequent GL/category account (peer-relative amount profile).",
    inputs=("amount_minor", "account", "direction", "payer_raw/payee_raw"),
    kind=Kind.FLOAT,
    units="ratio",
    peer_group="other streams of the same flow whose most frequent account is the same",
    missing=Missing.NULL_IF_INSUFFICIENT_HISTORY,
    missing_detail="null with fewer than min_peers peer streams, no amount, or a zero peer median",
    unusual=Unusual.BOTH,
    requires=("amount", "direction", "counterparty", "account_category"),
    params=("min_peers",),
)
def ent_median_to_peer_ratio(inp: FeatureInput) -> list[Any]:
    info = {}
    for sid, rows in streams(inp).items():
        accounts = Counter(r.account for r in rows if r.account)
        account = min(accounts, key=lambda a: (-accounts[a], a)) if accounts else None
        a = _amounts(rows)
        info[sid] = (sid.split(":", 1)[0], account, median(a) if a else None)
    out = []
    for sid, (flow, account, m) in info.items():
        peers = [
            pm
            for o, (f, acc, pm) in info.items()
            if o != sid and f == flow and acc == account and pm is not None
        ]
        if m is None or account is None or len(peers) < inp.cfg.min_peers or median(peers) == 0:
            out.append(None)
        else:
            out.append(m / median(peers))
    return out
