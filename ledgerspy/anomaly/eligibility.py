"""Which rows and which P6 features an anomaly detector may use.

Population. In `dataset` mode every row is both reference and scored (transductive: what is unusual in this
period?). With real calendar time, rows in the first `warmup_days` of the data are neither: their
point-in-time history is not yet representative (every long-standing vendor looks new), so they are
`insufficient_data` (warm_up). In `chronological` mode the earliest share of rows by posting time is the
reference (fit, medians, cutoffs) and only later rows are scored, so no baseline contains a scored row's
future. Rows sharing the split timestamp all fall on the scored side. Row order never matters: rows are
handled in txn_id order.

Features. A configured feature is used only when it is
- registered and implemented in the P6 registry (the only feature definitions),
- transaction level and numeric (float, int, bool),
- row or point-in-time semantics: dataset (P4) and post-period features summarise the whole period and would
  leak the future in chronological mode,
- not an identifier or label by name (a defence in depth: the P6 matrix cannot contain either),
- computed for this dataset (else `not_available` with P6's reason),
- at most `max_missing_share` null and at least `min_distinct_values` distinct in the reference rows.
Every decision is recorded with its reason.
"""

import math
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from ledgerspy.config.schema import AnomalyEligibility, AnomalyReference
from ledgerspy.features.data import FeatureInput
from ledgerspy.features.matrix import FeatureRun
from ledgerspy.features.spec import REGISTRY, Kind, Level, Semantics

NUMERIC = (Kind.FLOAT, Kind.INT, Kind.BOOL)
SAFE_SEMANTICS = (Semantics.ROW, Semantics.POINT_IN_TIME)
# Names that must never be model inputs, whatever a configuration says.
FORBIDDEN = re.compile(
    r"(^|_)(label|labels|target|fraud|isfraud|isflaggedfraud|scenario|ground|truth|is_control|txn_id|id|"
    r"source_row)($|_)"
)


class EligibilityError(ValueError):
    """A configured feature is not in the P6 registry (a configuration error, not a data condition)."""


@dataclass(frozen=True)
class Population:
    mode: str
    order: list[int]  # every matrix row index, in txn_id order
    reference: list[int]  # txn_id order
    scoring: list[int]  # txn_id order
    excluded: dict[int, str]  # row index → why it is neither reference nor scored
    status: str  # "ok" or "insufficient_data" / "not_available"
    reason: str = ""
    split: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "status": self.status,
            "reason": self.reason,
            "rows": len(self.order),
            "reference_rows": len(self.reference),
            "scoring_rows": len(self.scoring),
            "excluded_rows": len(self.excluded),
            **self.split,
        }


def population(inp: FeatureInput, run: FeatureRun, cfg: AnomalyReference) -> Population:
    subjects = run.transactions.subjects
    order = sorted(range(len(subjects)), key=lambda i: subjects[i])
    by_index = {r.i: r for r in inp.rows}
    excluded: dict[int, str] = {}
    warm: dict[str, Any] = {"days": cfg.warmup_days, "applied": False}
    calendar = inp.available(("calendar_time",))
    if cfg.warmup_days and calendar is None:
        dated = [by_index[i].ts for i in order if by_index[i].ts is not None]
        if dated:
            end = min(dated) + timedelta(days=cfg.warmup_days)
            excluded = {i: "warm_up" for i in order if by_index[i].ts is not None and by_index[i].ts < end}
            warm.update(applied=True, until=end.isoformat(), rows=len(excluded))
    elif cfg.warmup_days:
        warm["reason"] = calendar
    split: dict[str, Any] = {"warm_up": warm}
    if cfg.mode == "dataset":
        kept = [i for i in order if i not in excluded]
        return Population("dataset", order, kept, kept, excluded, "ok", split=split)
    reason = inp.available(("timestamp",))
    if reason is not None:
        return Population("chronological", order, [], [], excluded, "not_available", reason, split)
    excluded |= {i: "no_timestamp" for i in order if by_index[i].ts is None}
    dated_rows = sorted(
        (by_index[i] for i in order if i not in excluded), key=lambda r: (r.ts, r.source_row, r.txn_id)
    )
    m = int(cfg.chronological_fraction * len(dated_rows))
    if m == 0 or m >= len(dated_rows):
        return Population(
            "chronological",
            order,
            [],
            [],
            excluded,
            "insufficient_data",
            "too few dated rows to split",
            split,
        )
    split_ts = dated_rows[m].ts
    reference = [i for i in order if i not in excluded and by_index[i].ts < split_ts]
    scoring = [i for i in order if i not in excluded and by_index[i].ts >= split_ts]
    if not reference:
        return Population(
            "chronological", order, [], [], excluded, "insufficient_data", "no rows before the split", split
        )
    split.update(
        split_timestamp=split_ts.isoformat(),
        fraction=cfg.chronological_fraction,
        rule="reference = rows posted strictly before the split timestamp; scored = the rest",
    )
    return Population("chronological", order, reference, scoring, excluded, "ok", split=split)


def signed_log1p(x: float) -> float:
    return math.copysign(math.log1p(abs(x)), x)


def transform(value: Any, how: str) -> float | None:
    """A matrix value as a float for scoring (bool → 0/1), after the configured monotone transform."""
    if value is None:
        return None
    x = float(value)
    return signed_log1p(x) if how == "signed_log1p" else x


def transformed_column(inp: FeatureInput, run: FeatureRun, name: str, how: str) -> list[float | None]:
    """The matrix column after the transform, computed once per feature build."""
    return inp.memo(
        f"anomaly:{name}:{how}", lambda: [transform(v, how) for v in run.transactions.columns[name]]
    )


def check_feature(
    name: str, run: FeatureRun, reference: list[int], cfg: AnomalyEligibility
) -> dict[str, Any]:
    """Eligibility of one configured feature: {"feature", "status": used|excluded, "reason", …}."""
    spec = REGISTRY.get(name)
    if spec is None:
        raise EligibilityError(f"anomaly configuration names {name!r}, which is not a P6 registry feature")
    entry: dict[str, Any] = {"feature": name, "version": spec.version, "semantics": spec.semantics.value}

    def excluded(reason: str, detail: str = "") -> dict[str, Any]:
        return entry | {"status": "excluded", "reason": reason, "detail": detail}

    if FORBIDDEN.search(name):
        return excluded("identifier_or_label", "names like labels or identifiers are never model inputs")
    if spec.status != "implemented":
        return excluded("not_available_yet", spec.deferred_reason)
    if spec.level != Level.TRANSACTION:
        return excluded("not_transaction_level", "P8 scores transactions")
    if spec.kind not in NUMERIC:
        return excluded("not_numeric", f"{spec.kind.value} feature")
    if spec.semantics not in SAFE_SEMANTICS:
        return excluded(
            "not_point_in_time", f"{spec.semantics.value} semantics summarise the period (future leakage)"
        )
    status = run.transactions.status.get(name, {"status": "not_available", "reason": "not evaluated"})
    if status["status"] != "computed":
        return excluded("not_available", status["reason"])
    column = run.transactions.columns[name]
    values = [column[i] for i in reference]
    nulls = sum(1 for v in values if v is None)
    share = nulls / len(values) if values else 1.0
    distinct = len({v for v in values if v is not None})
    entry.update(missing_share=round(share, 6), distinct=distinct)
    if share > cfg.max_missing_share:
        return excluded("excessive_missing", f"{share:.1%} null in the reference rows")
    if distinct < cfg.min_distinct_values:
        return excluded("constant", f"{distinct} distinct value(s) in the reference rows")
    return entry | {"status": "used", "reason": ""}
