"""Transaction-level features (one value per transaction).

Every function here is the single implementation of one registered feature. It returns one value per input row
(in input order), or (values, basis) for features with a peer fallback. Shared machinery (history, peer
hierarchy) lives in `ledgerspy.features.history`; no feature re-implements another.
"""

import calendar as cal
import re
from collections import Counter
from datetime import date, time, timedelta
from typing import Any

from ledgerspy.features.data import FeatureInput, FeatureRow
from ledgerspy.features.history import (
    PEER_HIERARCHY_DOC,
    History,
    days,
    log_amount,
    median,
    peer_baseline,
    peer_levels,
    robust_center_scale,
    scan,
    scan_groups,
)
from ledgerspy.features.spec import Kind, Level, Missing, Semantics, Unusual, feature


def tx(**meta: Any):
    return feature(level=Level.TRANSACTION, **meta)


CP = "counterparty stream: same counterparty key and flow (see `party_key` and config features.adapters)"
NO_KEY = "null when the row has no timestamp, counterparty or direction"


def _cp_key(r: FeatureRow):
    return (r.cp, r.flow) if r.cp and r.flow else None


# =============================================================================================== amount


@tx(
    name="amount_minor",
    version=1,
    family="amount",
    description="The transaction amount as booked, in minor currency units (paise).",
    inputs=("amount_minor",),
    kind=Kind.INT,
    units="minor currency units",
    semantics=Semantics.ROW,
    scope="transaction",
    missing=Missing.NULL_IF_INPUT_MISSING,
    missing_detail="null when the amount is null",
    unusual=Unusual.HIGHER,
    requires=("amount",),
)
def amount_minor(inp: FeatureInput) -> list[Any]:
    return [r.amount for r in inp.rows]


@tx(
    name="log_amount",
    version=1,
    family="amount",
    description="ln(1 + |amount| in major units): compresses the heavy right tail of amounts.",
    inputs=("amount_minor",),
    kind=Kind.FLOAT,
    units="natural log of major currency units",
    semantics=Semantics.ROW,
    scope="transaction",
    missing=Missing.NULL_IF_INPUT_MISSING,
    missing_detail="null when the amount is null",
    unusual=Unusual.HIGHER,
    requires=("amount",),
)
def log_amount_feature(inp: FeatureInput) -> list[Any]:
    return [log_amount(r.amount) for r in inp.rows]


@tx(
    name="round_level",
    version=1,
    family="amount",
    description="Roundness: the largest k in 1..5 such that the amount is a whole multiple of 10^k major "
    "units (₹10 … ₹1,00,000); 0 when it is not a multiple of 10 major units (including any paise).",
    inputs=("amount_minor",),
    kind=Kind.INT,
    units="power of ten (0–5)",
    semantics=Semantics.ROW,
    scope="transaction",
    missing=Missing.NULL_IF_INPUT_MISSING,
    missing_detail="null when the amount is null or zero (zero is a multiple of everything)",
    unusual=Unusual.HIGHER,
    requires=("amount",),
)
def round_level(inp: FeatureInput) -> list[Any]:
    out: list[Any] = []
    for r in inp.rows:
        if not r.amount:
            out.append(None)
            continue
        a, k = abs(r.amount), 0
        while k < 5 and a % (100 * 10 ** (k + 1)) == 0:
            k += 1
        out.append(k)
    return out


@tx(
    name="is_outflow",
    version=1,
    family="amount",
    description="True for money leaving the books (debit / out), false for money coming in (credit / in).",
    inputs=("direction",),
    kind=Kind.BOOL,
    units="flag",
    semantics=Semantics.ROW,
    scope="transaction",
    missing=Missing.NULL_IF_INPUT_MISSING,
    missing_detail="null when the direction is null",
    unusual=Unusual.CONTEXT,
    requires=("direction",),
)
def is_outflow(inp: FeatureInput) -> list[Any]:
    return [None if r.flow is None else r.flow == "out" for r in inp.rows]


@tx(
    name="txn_type",
    version=1,
    family="amount",
    description="The source transaction type as a category (e.g. PaySim TRANSFER, CASH_OUT). An input "
    "encoding for models; models one-hot it themselves.",
    inputs=("txn_type",),
    kind=Kind.CATEGORY,
    units="category",
    semantics=Semantics.ROW,
    scope="transaction",
    missing=Missing.NULL_IF_INPUT_MISSING,
    missing_detail="null when the type is null",
    unusual=Unusual.CONTEXT,
    requires=("txn_type",),
)
def txn_type(inp: FeatureInput) -> list[Any]:
    return [r.txn_type for r in inp.rows]


@tx(
    name="threshold_proximity",
    version=1,
    family="amount",
    description="amount / the smallest configured approval threshold strictly above the amount, in (0, 1). "
    "Values just below 1 mean 'just under a limit'. Thresholds are case configuration; the canonical data "
    "does not say which limit governs a row, so the nearest one above is used.",
    inputs=("amount_minor", "config: features.approval_thresholds"),
    kind=Kind.FLOAT,
    units="ratio",
    semantics=Semantics.ROW,
    scope="transaction",
    missing=Missing.NULL_IF_NOT_APPLICABLE,
    missing_detail="null when the amount is null or ≤ 0, or at/above every configured threshold",
    unusual=Unusual.HIGHER,
    requires=("amount", "approval_thresholds"),
    params=("approval_thresholds",),
)
def threshold_proximity(inp: FeatureInput) -> list[Any]:
    limits = sorted(t.amount_minor for t in inp.cfg.approval_thresholds)
    out: list[Any] = []
    for r in inp.rows:
        above = [t for t in limits if r.amount is not None and 0 < r.amount < t]
        out.append(r.amount / above[0] if above else None)  # type: ignore[operator]
    return out


@tx(
    name="amount_robust_z",
    version=1,
    family="amount",
    description="Robust z-score of log_amount against prior amounts of the first peer level with enough "
    "history: (log x − median) / max(1.4826·MAD, mad_scale_floor).",
    inputs=("amount_minor", "direction", "posted_at", "payer_raw/payee_raw", "account", "txn_type"),
    kind=Kind.FLOAT,
    units="robust standard deviations (log scale)",
    semantics=Semantics.POINT_IN_TIME,
    scope="transaction vs its peer baseline",
    peer_group=PEER_HIERARCHY_DOC,
    fallback="hierarchy above; the level used is reported as the basis",
    missing=Missing.NULL_IF_INSUFFICIENT_HISTORY,
    missing_detail="null (basis 'unavailable') when no level has enough prior rows; null without "
    "amount/timestamp/direction",
    unusual=Unusual.BOTH,
    requires=("amount", "timestamp", "direction"),
    params=("baseline_window", "min_entity_history", "min_peer_history", "mad_scale_floor"),
)
def amount_robust_z(inp: FeatureInput) -> tuple[list[Any], list[Any]]:
    floor = inp.cfg.mad_scale_floor

    def z(r: FeatureRow, prior: list[float]) -> float:
        m, s = robust_center_scale(prior, floor)
        return (log_amount(r.amount) - m) / s  # type: ignore[operator]

    return peer_baseline(inp, lambda r: log_amount(r.amount) if r.flow else None, z)


@tx(
    name="amount_pctl_prior",
    version=1,
    family="amount",
    description="Mid-rank percentile of the amount among the most recent prior amounts of the same flow "
    "(global baseline), in [0, 1].",
    inputs=("amount_minor", "direction", "posted_at"),
    kind=Kind.FLOAT,
    units="fraction",
    semantics=Semantics.POINT_IN_TIME,
    scope="transaction vs all prior rows of the same flow",
    peer_group="global (same flow)",
    missing=Missing.NULL_IF_INSUFFICIENT_HISTORY,
    missing_detail="null with fewer than min_peer_history prior rows of the flow; null without "
    "amount/timestamp/direction",
    unusual=Unusual.HIGHER,
    requires=("amount", "timestamp", "direction"),
    params=("baseline_window", "min_peer_history"),
)
def amount_pctl_prior(inp: FeatureInput) -> list[Any]:
    level = [lv for lv in peer_levels(inp) if lv.name == "global"]

    def pctl(r: FeatureRow, prior: list[float]) -> float:
        below = sum(1 for v in prior if v < r.amount)  # type: ignore[operator]
        ties = sum(1 for v in prior if v == r.amount)
        return (below + 0.5 * ties) / len(prior)

    values, _ = peer_baseline(
        inp, lambda r: float(r.amount) if r.amount is not None and r.flow else None, pctl, level
    )
    return values


@tx(
    name="amount_to_cp_median_ratio",
    version=1,
    family="behaviour",
    description="amount / median of the counterparty stream's prior amounts (deviation from the entity's own "
    "baseline; no fallback, so it always means 'vs this counterparty').",
    inputs=("amount_minor", "direction", "posted_at", "payer_raw/payee_raw"),
    kind=Kind.FLOAT,
    units="ratio",
    semantics=Semantics.POINT_IN_TIME,
    scope=CP,
    peer_group="counterparty stream only",
    missing=Missing.NULL_IF_INSUFFICIENT_HISTORY,
    missing_detail="null with fewer than min_entity_history prior rows of the stream, or a zero median; "
    + NO_KEY,
    unusual=Unusual.BOTH,
    requires=("amount", "timestamp", "direction", "counterparty"),
    params=("baseline_window", "min_entity_history"),
)
def amount_to_cp_median_ratio(inp: FeatureInput) -> list[Any]:
    level = [lv for lv in peer_levels(inp) if lv.name == "counterparty"]

    def ratio(r: FeatureRow, prior: list[float]) -> float | None:
        m = median(prior)
        return r.amount / m if m else None  # type: ignore[operator]

    values, _ = peer_baseline(inp, lambda r: float(r.amount) if r.amount is not None else None, ratio, level)
    return values


@tx(
    name="amount_to_account_median_ratio",
    version=1,
    family="behaviour",
    description="amount / median prior amount of the same GL/category account and flow (peer-relative "
    "amount: e.g. a vendor paid far more than its category).",
    inputs=("amount_minor", "direction", "posted_at", "account"),
    kind=Kind.FLOAT,
    units="ratio",
    semantics=Semantics.POINT_IN_TIME,
    scope="transaction vs its account peer group",
    peer_group="account (same GL/category account and flow)",
    missing=Missing.NULL_IF_INSUFFICIENT_HISTORY,
    missing_detail="null with fewer than min_peer_history prior rows in the account peer group, or a zero "
    "median; null without amount/timestamp/direction/account",
    unusual=Unusual.BOTH,
    requires=("amount", "timestamp", "direction", "account_category"),
    params=("baseline_window", "min_peer_history"),
)
def amount_to_account_median_ratio(inp: FeatureInput) -> list[Any]:
    level = [lv for lv in peer_levels(inp) if lv.name == "account"]

    def ratio(r: FeatureRow, prior: list[float]) -> float | None:
        m = median(prior)
        return r.amount / m if m else None  # type: ignore[operator]

    values, _ = peer_baseline(inp, lambda r: float(r.amount) if r.amount is not None else None, ratio, level)
    return values


@tx(
    name="cp_prev_amount_ratio",
    version=1,
    family="sequence",
    description="amount / the amount of the counterparty stream's most recent prior row (sequence context: "
    "small → small → large).",
    inputs=("amount_minor", "direction", "posted_at", "payer_raw/payee_raw"),
    kind=Kind.FLOAT,
    units="ratio",
    semantics=Semantics.POINT_IN_TIME,
    scope=CP,
    missing=Missing.NULL_IF_INSUFFICIENT_HISTORY,
    missing_detail="null with no prior row (or a prior amount of 0 / null); " + NO_KEY,
    unusual=Unusual.BOTH,
    requires=("amount", "timestamp", "direction", "counterparty"),
)
def cp_prev_amount_ratio(inp: FeatureInput) -> list[Any]:
    def ratio(r: FeatureRow, h: History) -> float | None:
        if not h or r.amount is None:
            return None
        prev = h.rows[-1].amount  # latest earlier timestamp (ties: highest source_row)
        return r.amount / prev if prev else None

    return scan(inp, _cp_key, ratio)


# =============================================================================================== timing


@tx(
    name="hour_of_day",
    version=1,
    family="timing",
    description="Posting time as hours since midnight (hour + minute/60 + second/3600).",
    inputs=("posted_at",),
    kind=Kind.FLOAT,
    units="hours (0–24)",
    semantics=Semantics.ROW,
    scope="transaction",
    missing=Missing.NULL_IF_INPUT_MISSING,
    missing_detail="null when the posting timestamp is null",
    unusual=Unusual.CONTEXT,
    requires=("time_of_day",),
)
def hour_of_day(inp: FeatureInput) -> list[Any]:
    return [None if r.ts is None else r.ts.hour + r.ts.minute / 60 + r.ts.second / 3600 for r in inp.rows]


@tx(
    name="day_of_week",
    version=1,
    family="timing",
    description="Weekday of the posting date, Monday = 0 … Sunday = 6.",
    inputs=("posted_at",),
    kind=Kind.INT,
    units="weekday index",
    semantics=Semantics.ROW,
    scope="transaction",
    missing=Missing.NULL_IF_INPUT_MISSING,
    missing_detail="null when the posting timestamp is null",
    unusual=Unusual.CONTEXT,
    requires=("calendar_time",),
)
def day_of_week(inp: FeatureInput) -> list[Any]:
    return [None if r.ts is None else r.ts.weekday() for r in inp.rows]


@tx(
    name="is_non_working_weekday",
    version=1,
    family="timing",
    description="True when the posting date falls on a configured non-working weekday (e.g. Sunday).",
    inputs=("posted_at", "config: features.calendar.non_working_weekdays"),
    kind=Kind.BOOL,
    units="flag",
    semantics=Semantics.ROW,
    scope="transaction",
    missing=Missing.NULL_IF_INPUT_MISSING,
    missing_detail="null when the posting timestamp is null",
    unusual=Unusual.TRUE,
    requires=("calendar_time",),
    params=("calendar.non_working_weekdays",),
)
def is_non_working_weekday(inp: FeatureInput) -> list[Any]:
    off = set(inp.cfg.calendar.non_working_weekdays)
    return [None if r.ts is None else r.ts.weekday() in off for r in inp.rows]


@tx(
    name="is_holiday",
    version=1,
    family="timing",
    description="True when the posting date is in the case's configured holiday calendar.",
    inputs=("posted_at", "config: features.calendar.holidays"),
    kind=Kind.BOOL,
    units="flag",
    semantics=Semantics.ROW,
    scope="transaction",
    missing=Missing.NULL_IF_INPUT_MISSING,
    missing_detail="null when the posting timestamp is null",
    unusual=Unusual.TRUE,
    requires=("calendar_time", "holidays"),
    params=("calendar.holidays",),
)
def is_holiday(inp: FeatureInput) -> list[Any]:
    days_off = {date.fromisoformat(d) for d in inp.cfg.calendar.holidays}
    return [None if r.ts is None else r.ts.date() in days_off for r in inp.rows]


def _hhmm(text: str) -> time:
    h, m = text.split(":")
    return time(int(h), int(m))


@tx(
    name="is_after_hours",
    version=1,
    family="timing",
    description="True when the posting time is before the start or after the end of configured business "
    "hours.",
    inputs=("posted_at", "config: features.calendar.business_hours"),
    kind=Kind.BOOL,
    units="flag",
    semantics=Semantics.ROW,
    scope="transaction",
    missing=Missing.NULL_IF_INPUT_MISSING,
    missing_detail="null when the posting timestamp is null",
    unusual=Unusual.TRUE,
    requires=("time_of_day",),
    params=("calendar.business_hours",),
)
def is_after_hours(inp: FeatureInput) -> list[Any]:
    start, end = (_hhmm(t) for t in inp.cfg.calendar.business_hours)
    return [None if r.ts is None else not start <= r.ts.time() <= end for r in inp.rows]


def _off_hours(inp: FeatureInput):
    """True/False/None per row: after hours, on a non-working weekday, or on a configured holiday."""
    start, end = (_hhmm(t) for t in inp.cfg.calendar.business_hours)
    off = set(inp.cfg.calendar.non_working_weekdays)
    holidays = {date.fromisoformat(d) for d in inp.cfg.calendar.holidays}
    return lambda r: (
        None
        if r.ts is None
        else not start <= r.ts.time() <= end or r.ts.weekday() in off or r.ts.date() in holidays
    )


@tx(
    name="is_off_hours",
    version=1,
    family="timing",
    description="True when the posting falls outside the configured working time: after hours, on a "
    "non-working weekday, or on a configured holiday (holidays count only when a calendar is configured).",
    inputs=(
        "posted_at",
        "config: features.calendar.business_hours",
        "config: features.calendar.non_working_weekdays",
        "config: features.calendar.holidays",
    ),
    kind=Kind.BOOL,
    units="flag",
    semantics=Semantics.ROW,
    scope="transaction",
    missing=Missing.NULL_IF_INPUT_MISSING,
    missing_detail="null when the posting timestamp is null",
    unusual=Unusual.TRUE,
    requires=("time_of_day",),
    params=("calendar.business_hours", "calendar.non_working_weekdays", "calendar.holidays"),
)
def is_off_hours(inp: FeatureInput) -> list[Any]:
    off = _off_hours(inp)
    return [off(r) for r in inp.rows]


@tx(
    name="cp_prior_offhours_months",
    version=1,
    family="behaviour",
    description="Number of distinct calendar months in which the counterparty stream had earlier off-hours "
    "postings (see is_off_hours). ≥ 2 means off-hours work with this counterparty is a recurring routine, "
    "not a one-off.",
    inputs=("posted_at", "direction", "payer_raw/payee_raw", "config: features.calendar"),
    kind=Kind.INT,
    units="months",
    semantics=Semantics.POINT_IN_TIME,
    scope=CP,
    missing=Missing.NULL_IF_INPUT_MISSING,
    missing_detail=NO_KEY,
    unusual=Unusual.LOWER,
    requires=("time_of_day", "direction", "counterparty"),
    params=("calendar.business_hours", "calendar.non_working_weekdays", "calendar.holidays"),
)
def cp_prior_offhours_months(inp: FeatureInput) -> list[Any]:
    off = _off_hours(inp)
    return scan(inp, _cp_key, lambda r, h: len({(p.ts.year, p.ts.month) for p in h.rows if off(p)}))  # type: ignore[union-attr]


@tx(
    name="days_to_month_end",
    version=1,
    family="timing",
    description="Calendar days from the posting date to the last day of its month (0 on the last day).",
    inputs=("posted_at",),
    kind=Kind.INT,
    units="days",
    semantics=Semantics.ROW,
    scope="transaction",
    missing=Missing.NULL_IF_INPUT_MISSING,
    missing_detail="null when the posting timestamp is null",
    unusual=Unusual.LOWER,
    requires=("calendar_time",),
)
def days_to_month_end(inp: FeatureInput) -> list[Any]:
    return [None if r.ts is None else cal.monthrange(r.ts.year, r.ts.month)[1] - r.ts.day for r in inp.rows]


@tx(
    name="days_to_fiscal_year_end",
    version=1,
    family="timing",
    description="Calendar days from the posting date to the last day of its financial year (0 on that day).",
    inputs=("posted_at", "config: features.calendar.fiscal_year_start_month"),
    kind=Kind.INT,
    units="days",
    semantics=Semantics.ROW,
    scope="transaction",
    missing=Missing.NULL_IF_INPUT_MISSING,
    missing_detail="null when the posting timestamp is null",
    unusual=Unusual.LOWER,
    requires=("calendar_time",),
    params=("calendar.fiscal_year_start_month",),
)
def days_to_fiscal_year_end(inp: FeatureInput) -> list[Any]:
    start_month = inp.cfg.calendar.fiscal_year_start_month
    out: list[Any] = []
    for r in inp.rows:
        if r.ts is None:
            out.append(None)
            continue
        d = r.ts.date()
        next_start_year = d.year + 1 if d.month >= start_month else d.year
        fy_end = date(next_start_year, start_month, 1) - timedelta(days=1)
        out.append((fy_end - d).days)
    return out


@tx(
    name="posting_lag_days",
    version=1,
    family="timing",
    description="Posting date − document (invoice) date in days. Negative = posted before the document date.",
    inputs=("posted_at", "document_date"),
    kind=Kind.INT,
    units="days",
    semantics=Semantics.ROW,
    scope="transaction",
    missing=Missing.NULL_IF_INPUT_MISSING,
    missing_detail="null when the posting timestamp or the document date is null",
    unusual=Unusual.BOTH,
    requires=("calendar_time", "document_date"),
)
def posting_lag_days(inp: FeatureInput) -> list[Any]:
    return [
        None if r.ts is None or r.document_date is None else (r.ts.date() - r.document_date).days
        for r in inp.rows
    ]


@tx(
    name="hour_deviation",
    version=1,
    family="behaviour",
    description="|hour_of_day − median prior posting hour| of the first peer level with enough history "
    "(time-pattern deviation).",
    inputs=("posted_at", "direction", "payer_raw/payee_raw", "account", "txn_type"),
    kind=Kind.FLOAT,
    units="hours",
    semantics=Semantics.POINT_IN_TIME,
    scope="transaction vs its peer baseline",
    peer_group=PEER_HIERARCHY_DOC,
    fallback="hierarchy above; the level used is reported as the basis",
    missing=Missing.NULL_IF_INSUFFICIENT_HISTORY,
    missing_detail="null (basis 'unavailable') when no level has enough prior rows; null without "
    "timestamp/direction",
    unusual=Unusual.HIGHER,
    requires=("time_of_day", "direction"),
    params=("baseline_window", "min_entity_history", "min_peer_history"),
)
def hour_deviation(inp: FeatureInput) -> tuple[list[Any], list[Any]]:
    def hour(r: FeatureRow) -> float | None:
        if r.ts is None or r.flow is None:
            return None
        return r.ts.hour + r.ts.minute / 60 + r.ts.second / 3600

    return peer_baseline(inp, hour, lambda r, prior: abs(hour(r) - median(prior)))  # type: ignore[operator]


# =============================================================================================== frequency


@tx(
    name="cp_prior_count",
    version=1,
    family="frequency",
    description="Number of earlier transactions of the counterparty stream. 0 = first-time activity with "
    "this counterparty (0 is a true count, not a missing value).",
    inputs=("posted_at", "direction", "payer_raw/payee_raw"),
    kind=Kind.INT,
    units="transactions",
    semantics=Semantics.POINT_IN_TIME,
    scope=CP,
    missing=Missing.NULL_IF_INPUT_MISSING,
    missing_detail=NO_KEY,
    unusual=Unusual.LOWER,
    requires=("timestamp", "direction", "counterparty"),
)
def cp_prior_count(inp: FeatureInput) -> list[Any]:
    return scan(inp, _cp_key, lambda r, h: len(h))


@tx(
    name="cp_days_since_first",
    version=1,
    family="frequency",
    description="Days since the counterparty stream's first earlier transaction (relationship age as seen in "
    "the data; not the vendor-master onboarding date, which is not canonical data yet).",
    inputs=("posted_at", "direction", "payer_raw/payee_raw"),
    kind=Kind.FLOAT,
    units="days",
    semantics=Semantics.POINT_IN_TIME,
    scope=CP,
    missing=Missing.NULL_IF_NOT_APPLICABLE,
    missing_detail="null for first-time activity (no earlier row: see cp_prior_count = 0); " + NO_KEY,
    unusual=Unusual.LOWER,
    requires=("calendar_time", "direction", "counterparty"),
)
def cp_days_since_first(inp: FeatureInput) -> list[Any]:
    return scan(inp, _cp_key, lambda r, h: days(r.ts - h.times[0]) if h else None)


@tx(
    name="cp_days_since_prev",
    version=1,
    family="frequency",
    description="Days since the counterparty stream's most recent earlier transaction (time gap; long gaps = "
    "dormancy before this row).",
    inputs=("posted_at", "direction", "payer_raw/payee_raw"),
    kind=Kind.FLOAT,
    units="days",
    semantics=Semantics.POINT_IN_TIME,
    scope=CP,
    missing=Missing.NULL_IF_NOT_APPLICABLE,
    missing_detail="null for first-time activity; " + NO_KEY,
    unusual=Unusual.BOTH,
    requires=("calendar_time", "direction", "counterparty"),
)
def cp_days_since_prev(inp: FeatureInput) -> list[Any]:
    return scan(inp, _cp_key, lambda r, h: days(r.ts - h.times[-1]) if h else None)


def _count_window(inp: FeatureInput, window: int) -> list[Any]:
    return scan(inp, _cp_key, lambda r, h: len(h.since(r.ts - timedelta(days=window))))


@tx(
    name="cp_count_7d",
    version=1,
    family="velocity",
    description="Earlier transactions of the counterparty stream in the 7 days before this one [t − 7d, t).",
    inputs=("posted_at", "direction", "payer_raw/payee_raw"),
    kind=Kind.INT,
    units="transactions",
    semantics=Semantics.POINT_IN_TIME,
    scope=CP,
    missing=Missing.NULL_IF_INPUT_MISSING,
    missing_detail=NO_KEY,
    unusual=Unusual.HIGHER,
    requires=("calendar_time", "direction", "counterparty"),
)
def cp_count_7d(inp: FeatureInput) -> list[Any]:
    return _count_window(inp, 7)


@tx(
    name="cp_count_30d",
    version=1,
    family="velocity",
    description="Earlier transactions of the counterparty stream in the 30 days before this one [t − 30d, "
    "t).",
    inputs=("posted_at", "direction", "payer_raw/payee_raw"),
    kind=Kind.INT,
    units="transactions",
    semantics=Semantics.POINT_IN_TIME,
    scope=CP,
    missing=Missing.NULL_IF_INPUT_MISSING,
    missing_detail=NO_KEY,
    unusual=Unusual.HIGHER,
    requires=("calendar_time", "direction", "counterparty"),
)
def cp_count_30d(inp: FeatureInput) -> list[Any]:
    return _count_window(inp, 30)


@tx(
    name="cp_amount_sum_30d",
    version=1,
    family="velocity",
    description="Sum of the counterparty stream's earlier amounts in [t − 30d, t) (rows with a null amount "
    "are skipped).",
    inputs=("amount_minor", "posted_at", "direction", "payer_raw/payee_raw"),
    kind=Kind.INT,
    units="minor currency units",
    semantics=Semantics.POINT_IN_TIME,
    scope=CP,
    missing=Missing.NULL_IF_INPUT_MISSING,
    missing_detail=NO_KEY,
    unusual=Unusual.HIGHER,
    requires=("amount", "calendar_time", "direction", "counterparty"),
)
def cp_amount_sum_30d(inp: FeatureInput) -> list[Any]:
    return scan(
        inp,
        _cp_key,
        lambda r, h: sum(p.amount for p in h.since(r.ts - timedelta(days=30)) if p.amount is not None),
    )


@tx(
    name="cp_velocity_ratio",
    version=1,
    family="velocity",
    description="Recent rate / historical rate of the counterparty stream: (rows in [t−30d, t) / 30) ÷ (rows "
    "before t−30d / days from the first row to t−30d). 1 = usual pace; ≫ 1 = burst.",
    inputs=("posted_at", "direction", "payer_raw/payee_raw"),
    kind=Kind.FLOAT,
    units="ratio of rates",
    semantics=Semantics.POINT_IN_TIME,
    scope=CP,
    missing=Missing.NULL_IF_INSUFFICIENT_HISTORY,
    missing_detail="null when the history before t−30d spans fewer than velocity_min_history_days or has "
    "fewer "
    "than min_entity_history rows; " + NO_KEY,
    unusual=Unusual.HIGHER,
    requires=("calendar_time", "direction", "counterparty"),
    params=("velocity_min_history_days", "min_entity_history"),
)
def cp_velocity_ratio(inp: FeatureInput) -> list[Any]:
    cfg = inp.cfg

    def ratio(r: FeatureRow, h: History) -> float | None:
        cut = r.ts - timedelta(days=30)  # type: ignore[operator]
        before = h.before(cut)
        if len(before) < cfg.min_entity_history:
            return None
        span = days(cut - h.times[0])
        if span < cfg.velocity_min_history_days:
            return None
        recent = len(h) - len(before)
        return (recent / 30) / (len(before) / span)

    return scan(inp, _cp_key, ratio)


@tx(
    name="cp_count_30d_peer_ratio",
    version=1,
    family="behaviour",
    description="The counterparty stream's rows in [t−30d, t) ÷ the median of the same count for the other "
    "counterparties active in that window in the same account peer group (peer-relative frequency).",
    inputs=("posted_at", "direction", "payer_raw/payee_raw", "account"),
    kind=Kind.FLOAT,
    units="ratio",
    semantics=Semantics.POINT_IN_TIME,
    scope=CP,
    peer_group="account (same GL/category account and flow), other counterparties active in the window",
    missing=Missing.NULL_IF_INSUFFICIENT_HISTORY,
    missing_detail="null with fewer than min_peers other active counterparties in the peer group; null "
    "without timestamp/direction/counterparty/account",
    unusual=Unusual.HIGHER,
    requires=("calendar_time", "direction", "counterparty", "account_category"),
    params=("min_peers",),
)
def cp_count_30d_peer_ratio(inp: FeatureInput) -> list[Any]:
    def ratio(r: FeatureRow, h: History) -> float | None:
        if r.cp is None:
            return None
        counts = Counter(p.cp for p in h.since(r.ts - timedelta(days=30)))  # type: ignore[operator]
        own = counts.pop(r.cp, 0)
        if len(counts) < inp.cfg.min_peers:
            return None
        return own / median(list(counts.values()))

    return scan(inp, lambda r: (r.account, r.flow) if r.account and r.flow else None, ratio)


@tx(
    name="cp_log_amount_trend",
    version=1,
    family="behaviour",
    description="Escalation: least-squares slope of log_amount against time over the counterparty stream's "
    "earlier rows in [t − trend_window_days, t), per 30 days (0.11 ≈ +12% a month).",
    inputs=("amount_minor", "posted_at", "direction", "payer_raw/payee_raw"),
    kind=Kind.FLOAT,
    units="log-amount change per 30 days",
    semantics=Semantics.POINT_IN_TIME,
    scope=CP,
    missing=Missing.NULL_IF_INSUFFICIENT_HISTORY,
    missing_detail="null with fewer than trend_min_points earlier rows (with amounts) in the window, or all "
    "at "
    "one instant; " + NO_KEY,
    unusual=Unusual.HIGHER,
    requires=("amount", "calendar_time", "direction", "counterparty"),
    params=("trend_window_days", "trend_min_points"),
)
def cp_log_amount_trend(inp: FeatureInput) -> list[Any]:
    cfg = inp.cfg

    def slope(r: FeatureRow, h: History) -> float | None:
        start = r.ts - timedelta(days=cfg.trend_window_days)  # type: ignore[operator]
        pts = [p for p in h.since(start) if p.amount is not None]
        return ols_slope_per_30d(pts, cfg.trend_min_points)

    return scan(inp, _cp_key, slope)


def ols_slope_per_30d(rows: list[FeatureRow], min_points: int) -> float | None:
    """Least-squares slope of log_amount over time (days), × 30. None if too few points or no time spread."""
    if len(rows) < min_points:
        return None
    t0 = rows[0].ts
    xs = [days(p.ts - t0) for p in rows]  # type: ignore[operator]
    ys = [log_amount(p.amount) for p in rows]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)  # type: ignore[arg-type]
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    return 30 * sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx  # type: ignore[operator]


# =============================================================================================== sequence

_TRAILING_NUMBER = re.compile(r"(\d+)[A-Za-z]?$")


def reference_number(reference: str | None) -> int | None:
    """The number a reference ends with, allowing one letter suffix ('HST9/00970' → 970, 'GMP6/00195A' → 195).

    None when the reference does not end in digits ('IC/20240704/X/LVC'): an embedded date is not a sequence.
    """
    if not reference:
        return None
    m = _TRAILING_NUMBER.search(reference)
    return int(m.group(1)) if m else None


@tx(
    name="cp_reference_gap",
    version=1,
    family="sequence",
    description="Trailing number of the reference − that of the counterparty stream's most recent earlier "
    "row with a numbered reference (a reference ending in digits, optionally one letter). A vendor with "
    "many clients rarely sends consecutive numbers (gap 1) to one.",
    inputs=("reference", "posted_at", "direction", "payer_raw/payee_raw"),
    kind=Kind.INT,
    units="reference numbers",
    semantics=Semantics.POINT_IN_TIME,
    scope=CP,
    missing=Missing.NULL_IF_NOT_APPLICABLE,
    missing_detail="null when this reference has no number or no earlier row of the stream has one; "
    + NO_KEY,
    unusual=Unusual.CONTEXT,
    requires=("reference", "timestamp", "direction", "counterparty"),
)
def cp_reference_gap(inp: FeatureInput) -> list[Any]:
    def gap(r: FeatureRow, h: History) -> int | None:
        n = reference_number(r.reference)
        if n is None:
            return None
        for p in reversed(h.rows):
            prev = reference_number(p.reference)
            if prev is not None:
                return n - prev
        return None

    return scan(inp, _cp_key, gap)


# =============================================================================================== relationship


def _pair(r: FeatureRow):
    return (r.payer, r.payee) if r.payer and r.payee else None


@tx(
    name="pair_prior_count",
    version=1,
    family="relationship",
    description="Number of earlier transactions from the same payer to the same payee (directional pair).",
    inputs=("payer_raw", "payee_raw", "posted_at"),
    kind=Kind.INT,
    units="transactions",
    semantics=Semantics.POINT_IN_TIME,
    scope="payer → payee pair",
    missing=Missing.NULL_IF_INPUT_MISSING,
    missing_detail="null when the row has no timestamp, payer or payee",
    unusual=Unusual.LOWER,
    requires=("timestamp", "payer_payee"),
)
def pair_prior_count(inp: FeatureInput) -> list[Any]:
    return scan(inp, _pair, lambda r, h: len(h))


@tx(
    name="is_reciprocal_pair",
    version=1,
    family="relationship",
    description="True when an earlier transaction ran the other way: this row's payee paid this row's payer.",
    inputs=("payer_raw", "payee_raw", "posted_at"),
    kind=Kind.BOOL,
    units="flag",
    semantics=Semantics.POINT_IN_TIME,
    scope="payer ↔ payee pair",
    missing=Missing.NULL_IF_INPUT_MISSING,
    missing_detail="null when the row has no timestamp, payer or payee",
    unusual=Unusual.TRUE,
    requires=("timestamp", "payer_payee"),
)
def is_reciprocal_pair(inp: FeatureInput) -> list[Any]:
    def compute(r: FeatureRow, seen: dict) -> bool | None:
        if not (r.payer and r.payee):
            return None
        return (r.payee, r.payer) in seen.setdefault("pairs", set())

    def add(r: FeatureRow, seen: dict) -> None:
        if r.payer and r.payee:
            seen.setdefault("pairs", set()).add((r.payer, r.payee))

    return scan_groups(inp, compute, add)


@tx(
    name="payee_distinct_payers_prior",
    version=1,
    family="relationship",
    description="Number of distinct payers seen paying this row's payee before this row (fan-in to date).",
    inputs=("payer_raw", "payee_raw", "posted_at"),
    kind=Kind.INT,
    units="parties",
    semantics=Semantics.POINT_IN_TIME,
    scope="payee",
    missing=Missing.NULL_IF_INPUT_MISSING,
    missing_detail="null when the row has no timestamp or payee",
    unusual=Unusual.HIGHER,
    requires=("timestamp", "payer_payee"),
)
def payee_distinct_payers_prior(inp: FeatureInput) -> list[Any]:
    return scan(inp, lambda r: r.payee, lambda r, h: len({p.payer for p in h.rows if p.payer}))


@tx(
    name="payer_distinct_payees_prior",
    version=1,
    family="relationship",
    description="Number of distinct payees this row's payer paid before this row (fan-out to date).",
    inputs=("payer_raw", "payee_raw", "posted_at"),
    kind=Kind.INT,
    units="parties",
    semantics=Semantics.POINT_IN_TIME,
    scope="payer",
    missing=Missing.NULL_IF_INPUT_MISSING,
    missing_detail="null when the row has no timestamp or payer",
    unusual=Unusual.HIGHER,
    requires=("timestamp", "payer_payee"),
)
def payer_distinct_payees_prior(inp: FeatureInput) -> list[Any]:
    def compute(r: FeatureRow, s: dict) -> int | None:
        return None if not r.payer else len(s.setdefault(r.payer, set()))

    def add(r: FeatureRow, s: dict) -> None:
        if r.payer and r.payee:
            s.setdefault(r.payer, set()).add(r.payee)

    return scan_groups(inp, compute, add)


@tx(
    name="payer_new_payees_7d",
    version=1,
    family="relationship",
    description="Number of distinct payees this row's payer paid for the first time in [t − 7d, t) (a burst "
    "of brand-new counterparties: pass-through fan-out).",
    inputs=("payer_raw", "payee_raw", "posted_at"),
    kind=Kind.INT,
    units="parties",
    semantics=Semantics.POINT_IN_TIME,
    scope="payer",
    missing=Missing.NULL_IF_INPUT_MISSING,
    missing_detail="null when the row has no timestamp or payer",
    unusual=Unusual.HIGHER,
    requires=("calendar_time", "payer_payee"),
)
def payer_new_payees_7d(inp: FeatureInput) -> list[Any]:
    def compute(r: FeatureRow, s: dict) -> int | None:
        if not r.payer:
            return None
        start = r.ts - timedelta(days=7)  # type: ignore[operator]
        return sum(1 for first in s.setdefault(r.payer, {}).values() if first >= start)

    def add(r: FeatureRow, s: dict) -> None:
        if r.payer and r.payee:
            s.setdefault(r.payer, {}).setdefault(r.payee, r.ts)

    return scan_groups(inp, compute, add)


@tx(
    name="pair_share_of_payer_outflow",
    version=1,
    family="relationship",
    description="Share of the payer's earlier paid amount that went to this payee (flow concentration), in "
    "[0, 1].",
    inputs=("payer_raw", "payee_raw", "amount_minor", "posted_at"),
    kind=Kind.FLOAT,
    units="fraction",
    semantics=Semantics.POINT_IN_TIME,
    scope="payer → payee pair",
    missing=Missing.NULL_IF_INSUFFICIENT_HISTORY,
    missing_detail="null when the payer has no earlier amount; null without timestamp/payer/payee",
    unusual=Unusual.HIGHER,
    requires=("amount", "timestamp", "payer_payee"),
)
def pair_share_of_payer_outflow(inp: FeatureInput) -> list[Any]:
    def compute(r: FeatureRow, s: dict) -> float | None:
        if not (r.payer and r.payee):
            return None
        total = s.get(("total", r.payer), 0)
        return s.get(("pair", r.payer, r.payee), 0) / total if total else None

    def add(r: FeatureRow, s: dict) -> None:
        if r.payer and r.payee and r.amount is not None:
            s[("total", r.payer)] = s.get(("total", r.payer), 0) + abs(r.amount)
            s[("pair", r.payer, r.payee)] = s.get(("pair", r.payer, r.payee), 0) + abs(r.amount)

    return scan_groups(inp, compute, add)


# =============================================================================================== data quality

DUPLICATE_CHECKS = ("exact_duplicate", "near_duplicate", "duplicate_source_id")
MISSING_CHECKS = ("missing_required", "missing_optional")
P4_DOC = "the latest P4 integrity run of the current data version (integrity_issues)"


@tx(
    name="dq_issue_count",
    version=1,
    family="data_quality",
    description="Number of row-level P4 integrity issues on this transaction (any check, any severity).",
    inputs=(P4_DOC,),
    kind=Kind.INT,
    units="issues",
    semantics=Semantics.DATASET,
    scope="transaction",
    missing=Missing.NULL_IF_NOT_APPLICABLE,
    missing_detail="never null when available (0 = no issue); unavailable without a current P4 run",
    unusual=Unusual.HIGHER,
    requires=("p4_current",),
)
def dq_issue_count(inp: FeatureInput) -> list[Any]:
    return [len(inp.p4_issues.get(r.txn_id, [])) for r in inp.rows]


@tx(
    name="dq_has_duplicate_issue",
    version=1,
    family="data_quality",
    description="True when P4 reported an exact_duplicate, near_duplicate or duplicate_source_id issue on "
    "this transaction (P4 compares the whole dataset, so this is not point-in-time).",
    inputs=(P4_DOC,),
    kind=Kind.BOOL,
    units="flag",
    semantics=Semantics.DATASET,
    scope="transaction",
    missing=Missing.NULL_IF_NOT_APPLICABLE,
    missing_detail="never null when available; unavailable without a current P4 run",
    unusual=Unusual.TRUE,
    requires=("p4_current",),
)
def dq_has_duplicate_issue(inp: FeatureInput) -> list[Any]:
    return [any(c in DUPLICATE_CHECKS for c in inp.p4_issues.get(r.txn_id, [])) for r in inp.rows]


@tx(
    name="dq_has_missing_field",
    version=1,
    family="data_quality",
    description="True when P4 reported a missing_required or missing_optional issue on this transaction.",
    inputs=(P4_DOC,),
    kind=Kind.BOOL,
    units="flag",
    semantics=Semantics.DATASET,
    scope="transaction",
    missing=Missing.NULL_IF_NOT_APPLICABLE,
    missing_detail="never null when available; unavailable without a current P4 run",
    unusual=Unusual.TRUE,
    requires=("p4_current",),
)
def dq_has_missing_field(inp: FeatureInput) -> list[Any]:
    return [any(c in MISSING_CHECKS for c in inp.p4_issues.get(r.txn_id, [])) for r in inp.rows]


@tx(
    name="dq_is_normalized",
    version=1,
    family="data_quality",
    description="True when at least one field of the row comes from an applied, logged P4 transformation "
    "(normalised view differs from the evidence).",
    inputs=("normalized_values (P4 transformations)",),
    kind=Kind.BOOL,
    units="flag",
    semantics=Semantics.ROW,
    scope="transaction",
    missing=Missing.NULL_IF_NOT_APPLICABLE,
    missing_detail="never null",
    unusual=Unusual.CONTEXT,
    requires=(),
)
def dq_is_normalized(inp: FeatureInput) -> list[Any]:
    return [r.normalized for r in inp.rows]


@tx(
    name="dq_balance_discontinuity",
    version=1,
    family="data_quality",
    description="True when the current P4 run reported a balance_continuity issue on this transaction: its "
    "running balance does not follow from the previous balance and the amount (P4 does the arithmetic and "
    "records the kind of mismatch).",
    inputs=(P4_DOC, "P4 balance_continuity check"),
    kind=Kind.BOOL,
    units="flag",
    semantics=Semantics.DATASET,
    scope="transaction",
    missing=Missing.NULL_IF_NOT_APPLICABLE,
    missing_detail="never null when available; unavailable without a current P4 run or without a checked "
    "running balance (P4 says why)",
    unusual=Unusual.TRUE,
    requires=("p4_current", "running_balance"),
)
def dq_balance_discontinuity(inp: FeatureInput) -> list[Any]:
    return ["balance_continuity" in inp.p4_issues.get(r.txn_id, []) for r in inp.rows]
