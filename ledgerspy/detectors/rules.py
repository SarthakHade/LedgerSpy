"""P7 forensic rules: deterministic, explainable, label-free.

Every rule reads P6 registry features (and, for duplicates, the canonical fields of P6 feature rows) plus its
own `rules.<rule_id>` configuration. Nothing else: no labels, no `extra`, no ground truth, no file. A rule
whose inputs the dataset lacks reports `not_available` (or `not_applicable`) with the reason instead of
guessing; a subject whose needed value is null is counted as `insufficient`, never judged on a substituted
value.

Signals carry: the rule and its version, the subject, every transaction the evidence points at, severity, a
binary strength and a configured weight (placeholders that P9 calibrates), the evidence, the feature values
used and an explanation. Rules never conclude "fraud"; each explanation says what was observed and why it
merits review.
"""

import math
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from statistics import median
from typing import Any

from ledgerspy.canonical import SignalFamily
from ledgerspy.config.schema import BenfordConfig, RulesConfig
from ledgerspy.features.data import FeatureInput, FeatureRow
from ledgerspy.features.matrix import FeatureRun
from ledgerspy.forensics import duplicates
from ledgerspy.forensics.models import (
    DISABLED,
    EVALUATED,
    NOT_APPLICABLE,
    NOT_AVAILABLE,
    ForensicSignal,
    RuleOutcome,
)
from ledgerspy.hashing import canonical_json, sha256_hex


def money(minor: int | float | None) -> str:
    return "—" if minor is None else f"{minor / 100:,.2f}"


@dataclass
class RuleContext:
    inp: FeatureInput
    run: FeatureRun
    rules: RulesConfig
    benford: BenfordConfig
    # P4 issue details for rules that consume P4 results: check → txn_id → [observed dict]
    p4_details: dict[str, dict[str, list[dict[str, Any]]]] = field(default_factory=dict)

    def tx(self, name: str) -> list[Any]:
        return self.run.transactions.columns[name]

    def ent(self, name: str) -> list[Any]:
        return self.run.entities.columns[name]

    def feature_status(self, name: str) -> dict[str, str]:
        return {**self.run.transactions.status, **self.run.entities.status}[name]

    def stream(self, r: FeatureRow) -> str | None:
        return f"{r.flow}:{r.cp}" if r.cp and r.flow else None


@dataclass(frozen=True)
class RuleSpec:
    rule_id: str
    version: int
    family: SignalFamily
    description: str
    features: tuple[str, ...]  # P6 features the rule needs computed
    capabilities: tuple[str, ...]  # dataset capabilities the rule needs beyond its features
    not_applicable_when: tuple[
        str, ...
    ]  # capabilities whose absence means "does not apply" rather than "missing"
    semantics: str  # point_in_time | post_period | dataset
    fn: Callable[["RuleContext", Any, "RuleSpec"], RuleOutcome]


RULES: dict[str, RuleSpec] = {}


def rule(**meta: Any):
    def wrap(fn):
        spec = RuleSpec(fn=fn, **meta)
        if spec.rule_id in RULES:
            raise ValueError(f"rule {spec.rule_id} defined twice")
        RULES[spec.rule_id] = spec
        return fn

    return wrap


def _signal(
    spec: RuleSpec, params: Any, subject_type: str, subject_id: str, txn_ids, evidence, features, text
):
    return ForensicSignal(
        rule_id=spec.rule_id,
        rule_version=spec.version,
        family=spec.family,
        subject_type=subject_type,
        subject_id=subject_id,
        txn_ids=tuple(txn_ids),
        severity=params.severity,
        strength=1.0,
        weight=params.weight,
        evidence=evidence,
        features=features,
        explanation=text,
    )


def _outcome(spec: RuleSpec) -> RuleOutcome:
    return RuleOutcome(spec.rule_id, spec.version, spec.family, EVALUATED)


def run_rules(ctx: RuleContext) -> list[RuleOutcome]:
    out = []
    for rule_id in sorted(RULES):
        spec = RULES[rule_id]
        params = getattr(ctx.rules, rule_id)
        if not params.enabled:
            out.append(RuleOutcome(rule_id, spec.version, spec.family, DISABLED, "disabled in configuration"))
            continue
        reason, status = None, NOT_AVAILABLE
        for cap in spec.capabilities:
            why = ctx.inp.available((cap,))
            if why is not None:
                reason, status = why, NOT_APPLICABLE if cap in spec.not_applicable_when else NOT_AVAILABLE
                break
        if reason is None:
            for name in spec.features:
                st = ctx.feature_status(name)
                if st["status"] != "computed":
                    reason = f"feature {name} is {st['status']}: {st['reason']}"
                    break
        if reason is not None:
            out.append(RuleOutcome(rule_id, spec.version, spec.family, status, reason))
            continue
        outcome = spec.fn(ctx, params, spec)
        outcome.signals.sort(key=lambda s: (s.subject_type, s.subject_id))
        out.append(outcome)
    return out


# ============================================================================================ duplication


@rule(
    rule_id="duplicate_exact",
    version=1,
    family=SignalFamily.DUPLICATION,
    description="Two or more rows with the same payer, payee, amount, direction, posting date (simulation "
    "step for simulated clocks) and normalised reference (when references are mapped). Every row after the "
    "first of a group is signalled; the first is treated as the original.",
    features=("amount_minor",),
    capabilities=("timestamp", "counterparty"),
    not_applicable_when=(),
    semantics="dataset",
)
def duplicate_exact(ctx: RuleContext, p, spec: RuleSpec) -> RuleOutcome:
    out = _outcome(spec)
    use_ref = ctx.inp.capabilities.get("reference") is None
    by_date = ctx.inp.capabilities.get("calendar_time") is None
    rows = ctx.inp.rows  # a simulated clock has no "date": the exact simulated timestamp is compared instead
    groups, skipped = duplicates.exact_groups(rows, use_ref, by_date)
    out.evaluated, out.insufficient = len(rows) - skipped, skipped
    dq = (
        ctx.tx("dq_has_duplicate_issue")
        if ctx.feature_status("dq_has_duplicate_issue")["status"] == "computed"
        else None
    )
    for g in groups:
        first = g[0]
        group_id = "G-" + sha256_hex(canonical_json([r.txn_id for r in g]))[:12]
        for r in g[1:]:
            out.signals.append(
                _signal(
                    spec,
                    p,
                    "transaction",
                    r.txn_id,
                    [x.txn_id for x in g],
                    {
                        "group_id": group_id,
                        "original": first.txn_id,
                        "group": [x.txn_id for x in g],
                        "matched": ["payer", "payee", "amount", "direction", "posting date"]
                        + (["reference"] if use_ref else []),
                        "value": r.amount,
                    },
                    {"amount_minor": r.amount, "dq_has_duplicate_issue": dq[r.i] if dq else None},
                    f"Same payer, payee, amount {money(r.amount)}, direction, date"
                    + (" and reference" if use_ref else "")
                    + f" as {first.txn_id} ({len(g)} identical rows). Check whether it was booked or paid "
                    "twice.",
                )
            )
    return out


@rule(
    rule_id="duplicate_near",
    version=1,
    family=SignalFamily.DUPLICATION,
    description="A later row to the same payee and direction within window_days of an earlier one whose "
    "amount matches (equal, within tolerance, or two adjacent digits transposed) and whose reference "
    "matches (equal after normalisation, an added letter suffix, or two adjacent characters transposed). "
    "Without references the amount must be equal. A changed digit in the reference is not a match "
    "(consecutive invoices differ).",
    features=("amount_minor",),
    capabilities=("calendar_time", "counterparty"),
    not_applicable_when=(),
    semantics="point_in_time",
)
def duplicate_near(ctx: RuleContext, p, spec: RuleSpec) -> RuleOutcome:
    out = _outcome(spec)
    use_ref = ctx.inp.capabilities.get("reference") is None
    matches, skipped = duplicates.near_matches(
        ctx.inp.rows, p.window_days, p.amount_relative_tolerance, use_ref
    )
    out.evaluated, out.insufficient = len(ctx.inp.rows) - skipped, skipped
    by_later: dict[str, list[duplicates.NearMatch]] = defaultdict(list)
    for m in matches:
        by_later[m.later.txn_id].append(m)
    for txn_id in sorted(by_later):
        ms = sorted(by_later[txn_id], key=lambda m: (m.days_apart, m.earlier.source_row))
        best, r = ms[0], ms[0].later
        out.signals.append(
            _signal(
                spec,
                p,
                "transaction",
                txn_id,
                [m.earlier.txn_id for m in ms] + [txn_id],
                {
                    "earlier": [
                        {
                            "txn_id": m.earlier.txn_id,
                            "amount_minor": m.earlier.amount,
                            "reference": m.earlier.reference,
                            "amount_match": m.amount,
                            "reference_match": m.reference,
                            "days_apart": round(m.days_apart, 3),
                        }
                        for m in ms
                    ],
                    "value": round(best.days_apart, 3),
                },
                {"amount_minor": r.amount},
                f"Payment of {money(r.amount)} (ref {r.reference}) to the same payee {best.days_apart:.1f} "
                "days "
                f"after {best.earlier.txn_id} ({money(best.earlier.amount)}, ref {best.earlier.reference}): "
                f"amount {best.amount.replace('_', ' ')}, reference {best.reference.replace('_', ' ')}. "
                "Check whether the same invoice was paid twice.",
            )
        )
    return out


# ============================================================================================ amount


def poisson_tail(k: int, lam: float) -> float:
    """P(X ≥ k) for X ~ Poisson(lam), summed directly from k upward (accurate when tiny)."""
    if k <= 0:
        return 1.0
    if lam <= 0:
        return 0.0
    term = math.exp(k * math.log(lam) - lam - math.lgamma(k + 1))
    total, i = 0.0, k
    while term > total * 1e-17 and i < k + 100_000:
        total += term
        i += 1
        term *= lam / i
    return min(1.0, total)


def binomial_tail(k: int, n: int, p: float) -> float:
    """P(X ≥ k) for X ~ Binomial(n, p), summed in log space."""
    if k <= 0:
        return 1.0
    if p <= 0:
        return 0.0
    if p >= 1:
        return 1.0
    total = 0.0
    for i in range(k, n + 1):
        total += math.exp(
            math.lgamma(n + 1)
            - math.lgamma(i + 1)
            - math.lgamma(n - i + 1)
            + i * math.log(p)
            + (n - i) * math.log1p(-p)
        )
    return min(1.0, total)


@rule(
    rule_id="round_amount",
    version=1,
    family=SignalFamily.AMOUNT,
    description="A counterparty stream whose amounts are round (round_level ≥ features.round_level_min) far "
    "more often than its flow's population: at least min_rows amounts, round share ≥ min_round_share, at "
    "least min_distinct_round_amounts different round values (a fixed contractual amount repeated is not "
    "fabrication), and a one-sided binomial p-value ≤ max_p_value against the population round rate.",
    features=("round_level", "ent_round_share"),
    capabilities=("counterparty", "direction"),
    not_applicable_when=(),
    semantics="post_period",
)
def round_amount(ctx: RuleContext, p, spec: RuleSpec) -> RuleOutcome:
    out = _outcome(spec)
    level_min = ctx.inp.cfg.round_level_min
    levels = ctx.tx("round_level")
    pop: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    streams: dict[str, list[FeatureRow]] = defaultdict(list)
    for r in ctx.inp.rows:
        if levels[r.i] is None or r.flow is None:
            continue
        pop[r.flow][0] += levels[r.i] >= level_min
        pop[r.flow][1] += 1
        sid = ctx.stream(r)
        if sid:
            streams[sid].append(r)
    share = dict(zip(ctx.run.entities.subjects, ctx.ent("ent_round_share"), strict=True))
    for sid in sorted(streams):
        rows = streams[sid]
        if len(rows) < p.min_rows:
            out.insufficient += 1
            continue
        out.evaluated += 1
        round_rows = [r for r in rows if levels[r.i] >= level_min]
        k, n = len(round_rows), len(rows)
        flow = sid.split(":", 1)[0]
        rate = pop[flow][0] / pop[flow][1]
        distinct = len({r.amount for r in round_rows})
        pval = binomial_tail(k, n, rate)
        if k / n >= p.min_round_share and distinct >= p.min_distinct_round_amounts and pval <= p.max_p_value:
            out.signals.append(
                _signal(
                    spec,
                    p,
                    "entity",
                    sid,
                    sorted(r.txn_id for r in round_rows),
                    {
                        "round_rows": k,
                        "rows": n,
                        "round_share": round(k / n, 4),
                        "population_round_rate": round(rate, 6),
                        "distinct_round_amounts": distinct,
                        "binomial_p_value": pval,
                        "round_unit_major": 10**level_min,
                        "examples": [r.amount for r in sorted(round_rows, key=lambda r: r.source_row)[:10]],
                        "value": round(k / n, 4),
                    },
                    {"ent_round_share": share.get(sid), "round_level_min": level_min},
                    f"{k} of {n} amounts to this counterparty are multiples of {10**level_min:,} "
                    f"({k / n:.0%}) "
                    f"across {distinct} different values, against {rate:.1%} for all {flow} payments "
                    f"(binomial p = {pval:.1e}). Fabricated amounts are often round; review the supporting "
                    "invoices.",
                )
            )
    return out


@rule(
    rule_id="structuring",
    version=1,
    family=SignalFamily.STRUCTURING,
    description="At least min_parts transactions of one counterparty stream within window_days, each just "
    "below the same configured approval threshold (threshold_proximity ≥ min_proximity) and together above "
    "it: a split that keeps every part under the limit.",
    features=("threshold_proximity", "amount_minor"),
    capabilities=("counterparty", "timestamp"),
    not_applicable_when=(),
    semantics="post_period",
)
def structuring(ctx: RuleContext, p, spec: RuleSpec) -> RuleOutcome:
    out = _outcome(spec)
    prox = ctx.tx("threshold_proximity")
    limits = sorted(t.amount_minor for t in ctx.inp.cfg.approval_thresholds)
    names = {t.amount_minor: t.name for t in ctx.inp.cfg.approval_thresholds}
    cands: dict[tuple, list[FeatureRow]] = defaultdict(list)
    for r in ctx.inp.rows:
        if prox[r.i] is None or r.ts is None:
            out.insufficient += 1
            continue
        out.evaluated += 1
        if prox[r.i] >= p.min_proximity and ctx.stream(r):
            limit = next(t for t in limits if r.amount < t)  # the threshold threshold_proximity refers to
            cands[(ctx.stream(r), limit)].append(r)
    window = timedelta(days=p.window_days)
    for (_sid, limit), rows in sorted(cands.items()):
        rows.sort(key=lambda r: (r.ts, r.source_row, r.txn_id))
        i = 0
        while i < len(rows):
            j = i
            while j + 1 < len(rows) and rows[j + 1].ts - rows[i].ts <= window:
                j += 1
            part = rows[i : j + 1]
            total = sum(r.amount for r in part)
            if len(part) >= p.min_parts and total > limit:
                group_id = "G-" + sha256_hex(canonical_json([r.txn_id for r in part]))[:12]
                for r in part:
                    out.signals.append(
                        _signal(
                            spec,
                            p,
                            "transaction",
                            r.txn_id,
                            [x.txn_id for x in part],
                            {
                                "group_id": group_id,
                                "threshold": names[limit],
                                "threshold_minor": limit,
                                "parts": [
                                    {
                                        "txn_id": x.txn_id,
                                        "amount_minor": x.amount,
                                        "posted_at": x.ts.isoformat(),
                                    }
                                    for x in part
                                ],
                                "aggregate_minor": total,
                                "span_days": round((part[-1].ts - part[0].ts).total_seconds() / 86400, 3),
                                "value": total,
                            },
                            {"threshold_proximity": prox[r.i], "amount_minor": r.amount},
                            f"{len(part)} payments to the same counterparty within {p.window_days} days, "
                            "each "
                            f"between {p.min_proximity:.0%} and 100% of the {names[limit]} limit "
                            f"{money(limit)}, together {money(total)}. Check whether one purchase was split "
                            "to stay under the approval limit.",
                        )
                    )
                i = j + 1
            else:
                i += 1
    return out


# ============================================================================================ timing


@rule(
    rule_id="temporal",
    version=1,
    family=SignalFamily.TIMING,
    description="An off-hours posting (after business hours, non-working weekday or configured holiday: "
    "is_off_hours) to a counterparty for which off-hours work is not an established routine "
    "(cp_prior_offhours_months < routine_after_months).",
    features=("is_off_hours", "cp_prior_offhours_months"),
    capabilities=(),
    not_applicable_when=(),
    semantics="point_in_time",
)
def temporal(ctx: RuleContext, p, spec: RuleSpec) -> RuleOutcome:
    out = _outcome(spec)
    off, months = ctx.tx("is_off_hours"), ctx.tx("cp_prior_offhours_months")
    extra = {
        n: ctx.tx(n)
        for n in ("is_after_hours", "is_non_working_weekday", "is_holiday", "hour_of_day", "hour_deviation")
        if ctx.feature_status(n)["status"] == "computed"
    }
    for r in ctx.inp.rows:
        if off[r.i] is None or months[r.i] is None:
            out.insufficient += 1
            continue
        out.evaluated += 1
        if not off[r.i] or months[r.i] >= p.routine_after_months:
            continue
        why = [
            label
            for name, label in (
                ("is_after_hours", "after business hours"),
                ("is_non_working_weekday", "on a non-working weekday"),
                ("is_holiday", "on a holiday"),
            )
            if name in extra and extra[name][r.i]
        ]
        feats = {"is_off_hours": True, "cp_prior_offhours_months": months[r.i]} | {
            n: c[r.i] for n, c in extra.items()
        }
        out.signals.append(
            _signal(
                spec,
                p,
                "transaction",
                r.txn_id,
                [r.txn_id],
                {"posted_at": r.ts.isoformat(), "conditions": why, "value": months[r.i]},  # type: ignore[union-attr]
                feats,
                f"Posted {', '.join(why) or 'outside working time'} ({r.ts:%a %d %b %Y %H:%M}); off-hours "
                "postings to "
                f"this counterparty occurred in {months[r.i]} earlier month(s), so it is not an established "
                "routine. Confirm who posted it and why.",
            )
        )
    return out


# ============================================================================================ entity


@rule(
    rule_id="new_vendor_large",
    version=1,
    family=SignalFamily.ENTITY,
    description="A payment to a counterparty with at most max_prior_count earlier transactions "
    "(cp_prior_count) whose amount is far above its peers: amount_robust_z ≥ min_robust_z with the robust "
    "baseline taken from a peer group listed in peer_bases (a new counterparty has no own baseline; a "
    "global baseline mixes unlike payments and is not accepted). Rows in the first min_days_observed days "
    "of the data are not judged: a long-standing counterparty looks new until the data has been observed a "
    "while.",
    features=("cp_prior_count", "amount_robust_z", "is_outflow"),
    capabilities=("calendar_time",),  # the warm-up gate is measured in days of real time
    not_applicable_when=(),
    semantics="point_in_time",
)
def new_vendor_large(ctx: RuleContext, p, spec: RuleSpec) -> RuleOutcome:
    out = _outcome(spec)
    prior, z, outflow = ctx.tx("cp_prior_count"), ctx.tx("amount_robust_z"), ctx.tx("is_outflow")
    basis = ctx.run.transactions.basis["amount_robust_z"]
    start = min((r.ts for r in ctx.inp.rows if r.ts is not None), default=None)
    for r in ctx.inp.rows:
        if outflow[r.i] is not True or prior[r.i] is None or prior[r.i] > p.max_prior_count:
            continue
        if (r.ts - start).total_seconds() < p.min_days_observed * 86400:  # type: ignore[operator]
            out.insufficient += 1  # too early in the data to know whether the counterparty is new
            continue
        if z[r.i] is None or basis[r.i] not in p.peer_bases:
            out.insufficient += 1
            continue
        out.evaluated += 1
        if z[r.i] >= p.min_robust_z:
            out.signals.append(
                _signal(
                    spec,
                    p,
                    "transaction",
                    r.txn_id,
                    [r.txn_id],
                    {"counterparty": ctx.stream(r), "peer_basis": basis[r.i], "value": round(z[r.i], 4)},
                    {
                        "cp_prior_count": prior[r.i],
                        "amount_robust_z": z[r.i],
                        "amount_robust_z__basis": basis[r.i],
                    },
                    f"Payment of {money(r.amount)} to a counterparty with {prior[r.i]} earlier "
                    "transaction(s) is "
                    f"{z[r.i]:.1f} robust deviations above its {basis[r.i].replace('_', ' ')} peer group. "
                    "Check the vendor's onboarding and the basis for the amount.",
                )
            )
    return out


@rule(
    rule_id="balance_continuity",
    version=1,
    family=SignalFamily.DATA_QUALITY,
    description="P4 found a running balance that does not follow from the previous balance and the amount "
    "(dq_balance_discontinuity). P4 does the arithmetic; mismatch kinds listed in ignore_kinds (documented "
    "simulator artefacts) are not signalled. Not applicable when the source has no running balance.",
    features=("dq_balance_discontinuity",),
    capabilities=("p4_current", "running_balance"),
    not_applicable_when=("running_balance",),
    semantics="dataset",
)
def balance_continuity(ctx: RuleContext, p, spec: RuleSpec) -> RuleOutcome:
    out = _outcome(spec)
    flag = ctx.tx("dq_balance_discontinuity")
    details = ctx.p4_details.get("balance_continuity", {})
    out.evaluated = len(ctx.inp.rows)
    for r in ctx.inp.rows:
        if not flag[r.i]:
            continue
        issues = [d for d in details.get(r.txn_id, []) if d["observed"].get("kind") not in p.ignore_kinds]
        if not issues:
            continue
        obs = issues[0]["observed"]
        out.signals.append(
            _signal(
                spec,
                p,
                "transaction",
                r.txn_id,
                [r.txn_id],
                {"p4_issues": issues, "value": obs.get("kind", "mismatch")},
                {"dq_balance_discontinuity": True},
                "The running balance does not follow from the previous balance and the amount "
                f"({issues[0]['message']}). "
                "An unexplained balance jump can hide an unrecorded transaction; reconcile it.",
            )
        )
    return out


@rule(
    rule_id="dormancy",
    version=1,
    family=SignalFamily.ENTITY,
    description="The first transaction after at least min_dormant_days without any (cp_days_since_prev) "
    "whose amount is at least min_amount_ratio × the counterparty's earlier median "
    "(amount_to_cp_median_ratio).",
    features=("cp_days_since_prev", "amount_to_cp_median_ratio"),
    capabilities=(),
    not_applicable_when=(),
    semantics="point_in_time",
)
def dormancy(ctx: RuleContext, p, spec: RuleSpec) -> RuleOutcome:
    out = _outcome(spec)
    gap, ratio = ctx.tx("cp_days_since_prev"), ctx.tx("amount_to_cp_median_ratio")
    for r in ctx.inp.rows:
        if gap[r.i] is None or gap[r.i] < p.min_dormant_days:
            continue
        if ratio[r.i] is None:
            out.insufficient += 1
            continue
        out.evaluated += 1
        if ratio[r.i] >= p.min_amount_ratio:
            out.signals.append(
                _signal(
                    spec,
                    p,
                    "transaction",
                    r.txn_id,
                    [r.txn_id],
                    {
                        "counterparty": ctx.stream(r),
                        "dormant_days": round(gap[r.i], 2),
                        "value": round(gap[r.i], 2),
                    },
                    {"cp_days_since_prev": gap[r.i], "amount_to_cp_median_ratio": ratio[r.i]},
                    f"First transaction with this counterparty in {gap[r.i]:.0f} days, at {ratio[r.i]:.1f}× "
                    "its earlier median amount. Check why a dormant relationship resumed at a higher level.",
                )
            )
    return out


@rule(
    rule_id="period_end",
    version=1,
    family=SignalFamily.TIMING,
    description="A cluster of at least min_cluster transactions of one counterparty stream in the last "
    "window_days of a period (fiscal year or month), at least min_concentration × the stream's normal rate "
    "for a window that long and with a median amount ≥ min_amount_ratio × the stream's median. Contextual: "
    "a routine period-end posting is neither a cluster nor unusually large. Uses the whole period "
    "(post-period analysis).",
    features=("amount_minor", "ent_median_amount_minor", "ent_txn_count"),
    capabilities=("calendar_time", "counterparty"),
    not_applicable_when=(),
    semantics="post_period",
)
def period_end(ctx: RuleContext, p, spec: RuleSpec) -> RuleOutcome:
    out = _outcome(spec)
    feature = "days_to_fiscal_year_end" if p.period == "fiscal_year" else "days_to_month_end"
    if ctx.feature_status(feature)["status"] != "computed":
        out.status, out.reason = NOT_AVAILABLE, f"feature {feature} is not computed"
        return out
    to_end = ctx.tx(feature)
    med = dict(zip(ctx.run.entities.subjects, ctx.ent("ent_median_amount_minor"), strict=True))
    streams: dict[str, list[FeatureRow]] = defaultdict(list)
    for r in ctx.inp.rows:
        if ctx.stream(r) and r.ts is not None and to_end[r.i] is not None:
            streams[ctx.stream(r)].append(r)  # type: ignore[index]
    for sid in sorted(streams):
        rows = streams[sid]
        if len(rows) < p.min_stream_rows or not med.get(sid):
            out.insufficient += 1
            continue
        out.evaluated += 1
        span = max((max(r.ts for r in rows) - min(r.ts for r in rows)).days + 1, 1)  # type: ignore[type-var]
        clusters: dict[str, list[FeatureRow]] = defaultdict(list)
        for r in rows:
            if to_end[r.i] < p.window_days:
                clusters[(r.ts.date() + timedelta(days=to_end[r.i])).isoformat()].append(r)  # type: ignore[union-attr]
        outside = len(rows) - sum(len(c) for c in clusters.values())
        expected = outside / span * p.window_days
        for end, c in sorted(clusters.items()):
            amounts = [r.amount for r in c if r.amount is not None]
            if len(c) < p.min_cluster or not amounts:
                continue
            concentration = len(c) / expected if expected > 0 else math.inf
            amount_ratio = median(amounts) / med[sid]
            if concentration >= p.min_concentration and amount_ratio >= p.min_amount_ratio:
                ids = sorted(r.txn_id for r in c)
                out.signals.append(
                    _signal(
                        spec,
                        p,
                        "group",
                        f"{sid}@{end}",
                        ids,
                        {
                            "counterparty": sid,
                            "period": p.period,
                            "period_end": end,
                            "window_days": p.window_days,
                            "rows_in_window": len(c),
                            "expected_in_window": round(expected, 4),
                            "concentration": None if math.isinf(concentration) else round(concentration, 3),
                            "median_amount_minor": median(amounts),
                            "stream_median_minor": med[sid],
                            "value": len(c),
                        },
                        {"ent_median_amount_minor": med[sid], feature: [to_end[r.i] for r in c]},
                        f"{len(c)} postings to this counterparty in the last {p.window_days} day(s) before "
                        "the "
                        f"{p.period.replace('_', ' ')} end {end} (about {expected:.2f} expected), at "
                        f"{amount_ratio:.1f}× its median amount. Check cut-off: were these real period "
                        "expenses?",
                    )
                )
    return out


@rule(
    rule_id="velocity",
    version=1,
    family=SignalFamily.TIMING,
    description="Far more transactions with a counterparty in the last 30 days than its own historical pace: "
    "cp_velocity_ratio ≥ min_velocity_ratio and at least min_count_30d earlier rows in the 30 days "
    "(cp_count_30d), and the burst must be statistically unusual at the counterparty's own past rate "
    "(Poisson upper tail ≤ max_p_value; expected count = cp_count_30d / cp_velocity_ratio). Needs the P6 "
    "history gates (velocity_min_history_days, min_entity_history).",
    features=("cp_velocity_ratio", "cp_count_30d", "cp_amount_sum_30d"),
    capabilities=(),
    not_applicable_when=(),
    semantics="point_in_time",
)
def velocity(ctx: RuleContext, p, spec: RuleSpec) -> RuleOutcome:
    out = _outcome(spec)
    ratio, count, total = ctx.tx("cp_velocity_ratio"), ctx.tx("cp_count_30d"), ctx.tx("cp_amount_sum_30d")
    for r in ctx.inp.rows:
        if count[r.i] is None:
            continue
        if ratio[r.i] is None:
            out.insufficient += 1
            continue
        out.evaluated += 1
        if ratio[r.i] < p.min_velocity_ratio or count[r.i] < p.min_count_30d:
            continue
        expected = count[r.i] / ratio[r.i]  # rows the counterparty's own past rate predicts for 30 days
        pval = poisson_tail(count[r.i] + 1, expected)  # this row plus the earlier ones in the window
        if pval > p.max_p_value:
            continue
        out.signals.append(
            _signal(
                spec,
                p,
                "transaction",
                r.txn_id,
                [r.txn_id],
                {
                    "counterparty": ctx.stream(r),
                    "expected_30d": round(expected, 4),
                    "poisson_p_value": pval,
                    "value": round(ratio[r.i], 4),
                },
                {
                    "cp_velocity_ratio": ratio[r.i],
                    "cp_count_30d": count[r.i],
                    "cp_amount_sum_30d": total[r.i],
                },
                f"{count[r.i]} earlier transactions with this counterparty in the last 30 days "
                f"({money(total[r.i])}), {ratio[r.i]:.1f}× its usual pace (about {expected:.1f} expected; "
                f"Poisson p = {pval:.1e}). Check what drove the burst.",
            )
        )
    return out
