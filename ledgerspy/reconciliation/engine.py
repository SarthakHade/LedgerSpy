"""Staged, deterministic ledger ↔ bank matching (pure: rows + configuration in, results out).

Candidates are generated only inside a block: the same scope (bank account ↔ ledger filter), the same currency
and the same flow (money in / out of the account). Different currencies are never compared or summed, and no
row is matched across accounts.

Stage 1 / 2 — one-to-one, equal amount, normal date window (`dates.bank_before_days` / `bank_after_days`):
    exact_match    when a reference or the counterparty AGREES (see `link`),
    probable_match when the evidence score ≥ `scoring.probable_min_score` without such confirmation.
    A pair with a conflicting reference or counterparty is never matched automatically here.
Stage 3 — grouped settlements: one line on one side equals the sum of a DATE-CONTIGUOUS run (2…max_group_size
    rows, within `grouping.window_days`) of rows of the same counterparty on the other side. Runs only — no
    subset search; more than `grouping.max_candidates` rows for one settlement truncates the search
    (reported).
Stage 4 — partial: an amount difference within `partial` tolerances and / or a date outside the normal but
    inside the extended window, both only with an agreeing reference or counterparty; or equal amount and date
    with a CONFLICTING reference / counterparty (surfaced, never reconciled).

Selection (every stage): a pair is accepted only when each row is the other's best candidate by more than
`scoring.ambiguity_margin` (mutual best, repeated until nothing changes). Rows left with comparable candidates
form an `ambiguous` result listing every competing candidate; the first candidate is never picked. The order
of input rows does not matter: candidates are sorted by stable keys and ties are never broken arbitrarily.

The evidence score is the weighted mean of the components available for a pair (amount, date, and reference /
counterparty when evaluable). It ranks evidence; it is NOT a probability and is not calibrated.
"""

from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from ledgerspy.config.schema import ReconciliationConfig
from ledgerspy.errors import require
from ledgerspy.hashing import canonical_json, sha256_hex
from ledgerspy.reconciliation.records import RRow, account_label, alnum

VERSION = 1
DETECTOR = {"name": "ledgerspy.reconciliation", "version": VERSION}
STATUSES = (
    "exact_match",
    "probable_match",
    "grouped_match",
    "partial_match",
    "ambiguous",
    "duplicate_candidate",
    "unmatched_ledger",
    "unmatched_bank",
    "insufficient_data",
    "not_applicable",
)
MATCHED = ("exact_match", "probable_match", "grouped_match")
UNMATCHED = ("unmatched_ledger", "unmatched_bank", "duplicate_candidate")


# ------------------------------------------------------------------------------------------- pair evidence


@dataclass
class Pair:
    lg: RRow
    b: RRow
    score: float
    components: dict[str, float]
    ref: str
    cp: str
    date_diff: int
    amount_diff: int
    discrepancies: list[str] = field(default_factory=list)
    status: str = ""

    def key(self) -> tuple[str, str]:
        return (self.lg.txn_id, self.b.txn_id)


def ref_status(lg: RRow, b: RRow, cfg: ReconciliationConfig) -> str:
    """agree: equal, or one side's reference appears in the other's description; conflict: two references of
    the same shape that differ; unknown otherwise."""
    lr, br, m = alnum(lg.reference), alnum(b.reference), cfg.reference.min_chars
    if lr and br and lr == br:
        return "agree"
    if lr and len(lr) >= m and lr in b.text_alnum:
        return "agree"
    if br and len(br) >= m and br in lg.text_alnum:
        return "agree"
    if (
        cfg.reference.conflict_on_same_shape
        and lr
        and br
        and min(len(lr), len(br)) >= m
        and lg.ref_shape == b.ref_shape
    ):
        return "conflict"
    return "unknown"


def cp_status(lg: RRow, b: RRow) -> str:
    """agree: the ledger counterparty is named in the bank description; conflict: other known parties (not the
    ledger's own side) are named and the counterparty is not; unknown: nothing to compare."""
    if lg.cp_entity is None or not b.named:
        return "unknown"
    if lg.cp_entity in b.named:
        return "agree"
    return "conflict" if b.named - {lg.own_entity} else "unknown"


def _date_score(d: int, before: int, after: int) -> float:
    span = after if d >= 0 else before
    return max(0.0, 1.0 - abs(d) / (span + 1))


def _score(components: dict[str, float], cfg: ReconciliationConfig) -> float:
    w = cfg.scoring.weights
    weights = {"amount": w.amount, "date": w.date, "reference": w.reference, "counterparty": w.counterparty}
    total = sum(weights[k] for k in components)
    return round(sum(weights[k] * v for k, v in components.items()) / total, 6) if total else 0.0


def evaluate(lg: RRow, b: RRow, cfg: ReconciliationConfig, extended: bool = False) -> Pair:
    dc = cfg.dates
    d = (b.date - lg.date).days  # type: ignore[operator]
    diff = b.amount - lg.amount  # type: ignore[operator]
    ref, cp = ref_status(lg, b, cfg), cp_status(lg, b)
    before, after = (
        (dc.extended_before_days, dc.extended_after_days)
        if extended
        else (
            dc.bank_before_days,
            dc.bank_after_days,
        )
    )
    comp = {"amount": 1.0, "date": round(_date_score(d, before, after), 6)}
    if diff:
        comp["amount"] = round(max(0.0, 1.0 - abs(diff) / (tolerance(lg.amount, b.amount, cfg) + 1)), 6)
    if ref != "unknown":
        comp["reference"] = 1.0 if ref == "agree" else 0.0
    if cp != "unknown":
        comp["counterparty"] = 1.0 if cp == "agree" else 0.0
    return Pair(lg, b, _score(comp, cfg), comp, ref, cp, d, diff)


def tolerance(a: int, b: int, cfg: ReconciliationConfig) -> int:
    return int(min(cfg.partial.max_abs_minor, cfg.partial.max_relative * max(a, b)))


def in_normal_window(d: int, cfg: ReconciliationConfig) -> bool:
    return -cfg.dates.bank_before_days <= d <= cfg.dates.bank_after_days


# ------------------------------------------------------------------------------------------- selection


def _components(edges: list[tuple[str, str]]) -> list[set[str]]:
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    groups: dict[str, set[str]] = defaultdict(set)
    for x in list(parent):
        groups[find(x)].add(x)
    return [groups[k] for k in sorted(groups)]


def select(pairs: list[Pair], margin: float) -> tuple[list[Pair], list[list[Pair]]]:
    """Mutual best by more than `margin`, repeated; returns (accepted, contested components)."""
    by_l: dict[str, list[Pair]] = defaultdict(list)
    by_b: dict[str, list[Pair]] = defaultdict(list)
    for p in pairs:
        by_l[p.lg.txn_id].append(p)
        by_b[p.b.txn_id].append(p)
    for lst in by_l.values():
        lst.sort(key=lambda p: (-p.score, p.b.txn_id))
    for lst in by_b.values():
        lst.sort(key=lambda p: (-p.score, p.lg.txn_id))
    act_l, act_b = set(by_l), set(by_b)
    accepted: list[Pair] = []

    def best(lst: list[Pair], other_active: set[str], other: str) -> Pair | None:
        c = [p for p in lst if getattr(p, other).txn_id in other_active]
        if not c:
            return None
        return c[0] if len(c) == 1 or c[0].score - c[1].score > margin else None

    while True:
        bl = {lid: p for lid in sorted(act_l) if (p := best(by_l[lid], act_b, "b")) is not None}
        bb = {bid: p for bid in sorted(act_b) if (p := best(by_b[bid], act_l, "lg")) is not None}
        new = [p for p in bl.values() if bb.get(p.b.txn_id) is p]
        if not new:
            break
        for p in new:
            accepted.append(p)
            act_l.discard(p.lg.txn_id)
            act_b.discard(p.b.txn_id)
    rest = [p for p in pairs if p.lg.txn_id in act_l and p.b.txn_id in act_b]
    comps = _components([("l:" + p.lg.txn_id, "b:" + p.b.txn_id) for p in rest])
    member = {x: i for i, c in enumerate(comps) for x in c}
    contested: list[list[Pair]] = [[] for _ in comps]
    for p in rest:
        contested[member["l:" + p.lg.txn_id]].append(p)
    return accepted, [sorted(c, key=Pair.key) for c in contested if c]


# ------------------------------------------------------------------------------------------- engine


@dataclass
class Outcome:
    results: list[dict[str, Any]]
    truncation: dict[str, Any]
    blocks: int
    candidate_pairs: int


class _State:
    def __init__(self, ledger: list[RRow], bank: list[RRow], cfg: ReconciliationConfig) -> None:
        self.cfg = cfg
        self.ledger = sorted(ledger, key=lambda r: r.txn_id)
        self.bank = sorted(bank, key=lambda r: r.txn_id)
        self.results: list[dict[str, Any]] = []
        self.done: set[tuple[str, str]] = set()
        self.truncated: dict[str, set[str]] = defaultdict(set)
        self.pairs_seen = 0
        self.blocks: dict[tuple, tuple[list[RRow], list[RRow]]] = defaultdict(lambda: ([], []))
        for r in self.ledger:
            if not r.na and not r.reasons:
                self.blocks[(r.scope, r.currency, r.flow)][0].append(r)
        for r in self.bank:
            if not r.na and not r.reasons:
                self.blocks[(r.scope, r.currency, r.flow)][1].append(r)

    def free(self, r: RRow) -> bool:
        return (r.side, r.txn_id) not in self.done

    def take(self, rows: list[RRow]) -> None:
        for r in rows:
            # every row lands in exactly one result (explicit, so it also holds under python -O)
            require(self.free(r), "a row was assigned to two results", side=r.side, txn_id=r.txn_id)
            self.done.add((r.side, r.txn_id))

    def block_rows(self):
        for key in sorted(self.blocks, key=lambda k: tuple("" if x is None else str(x) for x in k)):
            ls, bs = self.blocks[key]
            yield key, [r for r in ls if self.free(r)], [r for r in bs if self.free(r)]


def _sorted_by_date(rows: list[RRow]) -> list[RRow]:
    return sorted(rows, key=lambda r: (r.date, r.txn_id))


def _stage_one_to_one(st: _State) -> None:
    cfg, maxc = st.cfg, st.cfg.limits.max_candidates_per_row
    pairs: list[Pair] = []
    for _key, ls, bs in st.block_rows():
        by_amt: dict[int, list[RRow]] = defaultdict(list)
        for b in bs:
            by_amt[b.amount].append(b)  # type: ignore[index]
        for lg in ls:
            cands = [b for b in by_amt.get(lg.amount, ()) if in_normal_window((b.date - lg.date).days, cfg)]
            if len(cands) > maxc:
                cands = sorted(cands, key=lambda b: (abs((b.date - lg.date).days), b.txn_id))[:maxc]
                st.truncated["one_to_one_candidates"].add(lg.txn_id)
            for b in cands:
                p = evaluate(lg, b, cfg)
                st.pairs_seen += 1
                if "conflict" in (p.ref, p.cp):
                    continue  # surfaced in stage 4, never matched automatically
                if "agree" in (p.ref, p.cp):
                    p.status = "exact_match"
                elif p.score >= cfg.scoring.probable_min_score:
                    p.status = "probable_match"
                else:
                    continue
                pairs.append(p)
    accepted, contested = select(pairs, cfg.scoring.ambiguity_margin)
    for p in sorted(accepted, key=Pair.key):
        st.take([p.lg, p.b])
        st.results.append(_pair_result(p, p.status, "one_to_one"))
    for comp in contested:
        _ambiguous(st, comp, "one_to_one")


def _runs(target: int, rows: list[RRow], max_size: int) -> list[list[RRow]]:
    out = []
    for i in range(len(rows)):
        total = 0
        for j in range(i, min(len(rows), i + max_size)):
            total += rows[j].amount  # type: ignore[operator]
            if total > target:
                break
            if total == target and j > i:
                out.append(rows[i : j + 1])
    return out


def _stage_grouped(st: _State) -> None:
    cfg = st.cfg
    g = cfg.grouping
    groups: list[dict[str, Any]] = []
    for key, ls, bs in st.block_rows():
        led_by_cp: dict[str, list[RRow]] = defaultdict(list)
        for lg in _sorted_by_date(ls):
            if lg.cp_entity:
                led_by_cp[lg.cp_entity].append(lg)
        bank_by_cp: dict[str, list[RRow]] = defaultdict(list)
        for b in _sorted_by_date(bs):
            for e in sorted(b.named):
                bank_by_cp[e].append(b)

        def window(lst: list[RRow], lo: date, hi: date) -> list[RRow]:
            dates = [r.date for r in lst]
            return lst[bisect_left(dates, lo) : bisect_right(dates, hi)]

        def search(settle: RRow, pool: list[RRow], orient: str, cp: str, key: tuple = key) -> None:
            if len(pool) > g.max_candidates:
                pool = _sorted_by_date(
                    sorted(pool, key=lambda r: (abs((r.date - settle.date).days), r.txn_id))[
                        : g.max_candidates
                    ]
                )
                st.truncated["grouping_candidates"].add(settle.txn_id)
            for run in _runs(settle.amount, [r for r in pool if r.amount < settle.amount], g.max_group_size):
                groups.append({"settle": settle, "parts": run, "orient": orient, "cp": cp, "block": key})

        before, win = timedelta(days=cfg.dates.bank_before_days), timedelta(days=g.window_days)
        for b in _sorted_by_date(bs):  # one bank line settles several ledger rows
            for e in sorted(b.named):
                pool = window(led_by_cp.get(e, []), b.date - win, b.date + before)
                search(b, pool, "bank_settles_ledger", e)
        for lg in _sorted_by_date(ls):  # one ledger row settled by several bank lines
            if lg.cp_entity:
                pool = window(bank_by_cp.get(lg.cp_entity, []), lg.date - before, lg.date + win)
                search(lg, pool, "ledger_settled_by_bank", lg.cp_entity)
    if not groups:
        return
    rows_of = [{(r.side, r.txn_id) for r in [x["settle"], *x["parts"]]} for x in groups]
    edges = []
    for i, rs in enumerate(rows_of):
        for r in sorted(rs):
            edges.append((f"g:{i:09d}", "r:" + "|".join(r)))
    for comp in _components(edges):
        idx = sorted(int(x[2:]) for x in comp if x.startswith("g:"))
        if len(idx) == 1:
            x = groups[idx[0]]
            rows = [x["settle"], *x["parts"]]
            st.take(rows)
            st.results.append(_group_result(x, cfg))
        else:
            rows = {}
            for i in idx:
                for r in [groups[i]["settle"], *groups[i]["parts"]]:
                    rows[(r.side, r.txn_id)] = r
            alts = [_group_alt(groups[i]) for i in idx]
            members = [rows[k] for k in sorted(rows)]
            st.take(members)
            st.results.append(_ambiguous_result(members, alts, "grouped"))


def _stage_partial(st: _State) -> None:
    cfg, maxc = st.cfg, st.cfg.limits.max_candidates_per_row
    dc = cfg.dates
    pairs: list[Pair] = []
    for _key, ls, bs in st.block_rows():
        bs_sorted = sorted(bs, key=lambda b: (b.amount, b.txn_id))
        amounts = [b.amount for b in bs_sorted]
        for lg in ls:
            lo = bisect_left(amounts, lg.amount - cfg.partial.max_abs_minor)
            hi = bisect_right(amounts, lg.amount + cfg.partial.max_abs_minor)
            cands = []
            for b in bs_sorted[lo:hi]:
                d = (b.date - lg.date).days
                if not -dc.extended_before_days <= d <= dc.extended_after_days:
                    continue
                diff = b.amount - lg.amount
                if diff and abs(diff) > tolerance(lg.amount, b.amount, cfg):
                    continue
                cands.append(b)
            if len(cands) > maxc:
                cands = sorted(
                    cands, key=lambda b: (abs(b.amount - lg.amount), abs((b.date - lg.date).days), b.txn_id)
                )[:maxc]
                st.truncated["partial_candidates"].add(lg.txn_id)
            for b in cands:
                p = evaluate(lg, b, cfg, extended=True)
                st.pairs_seen += 1
                disc = []
                if p.amount_diff:
                    disc.append("amount")
                if not in_normal_window(p.date_diff, cfg):
                    disc.append("date")
                if p.ref == "conflict":
                    disc.append("reference")
                if p.cp == "conflict":
                    disc.append("counterparty")
                if not disc:
                    continue
                if ("amount" in disc or "date" in disc) and "agree" not in (p.ref, p.cp):
                    continue
                p.discrepancies, p.status = disc, "partial_match"
                pairs.append(p)
    accepted, contested = select(pairs, cfg.scoring.ambiguity_margin)
    for p in sorted(accepted, key=Pair.key):
        st.take([p.lg, p.b])
        st.results.append(_pair_result(p, "partial_match", "partial"))
    for comp in contested:
        _ambiguous(st, comp, "partial")


# ------------------------------------------------------------------------------------------- results


def result_id(status: str, ledger_ids: list[str], bank_ids: list[str]) -> str:
    return "RM-" + sha256_hex(canonical_json([status, sorted(ledger_ids), sorted(bank_ids)]))[:16]


def _scope_of(rows: list[RRow]) -> dict[str, Any]:
    r = rows[0]
    bank = next((x for x in rows if x.side == "bank"), None)
    return {
        "scope_id": r.scope,
        "account": account_label(bank.account_norm) if bank else None,
        "currency": r.currency,
        "flow": r.flow,
    }


def _base(status: str, stage: str | None, ledger: list[RRow], bank: list[RRow]) -> dict[str, Any]:
    ledger = sorted(ledger, key=lambda r: r.txn_id)
    bank = sorted(bank, key=lambda r: r.txn_id)
    rows = ledger + bank
    dates = [r.date for r in rows if r.date]
    return {
        "result_id": result_id(status, [r.txn_id for r in ledger], [r.txn_id for r in bank]),
        "status": status,
        "stage": stage,
        "scope": _scope_of(rows),
        "ledger_txn_ids": [r.txn_id for r in ledger],
        "bank_txn_ids": [r.txn_id for r in bank],
        "ledger_rows": [r.summary() for r in ledger],
        "bank_rows": [r.summary() for r in bank],
        "first_date": min(dates).isoformat() if dates else None,
        "last_date": max(dates).isoformat() if dates else None,
        "amount_difference_minor": None,
        "date_difference_days": None,
        "evidence_score": None,
        "score_components": {},
        "discrepancies": [],
        "link": {},
        "components": [],
        "candidates": [],
        "duplicate_group_ids": [],
        "reasons": [],
        "explanation": "",
        "limitations": [],
        "detector": DETECTOR,
    }


def _money(m: int | None, cur: str | None) -> str:
    if m is None:
        return "—"
    return f"{m // 100:,}.{m % 100:02d} {cur or ''}".strip()


LINK_TEXT = {
    ("agree", "agree"): "reference and counterparty agree",
    ("agree", "unknown"): "reference agrees",
    ("unknown", "agree"): "counterparty named in the bank description",
}


def _pair_result(p: Pair, status: str, stage: str) -> dict[str, Any]:
    out = _base(status, stage, [p.lg], [p.b])
    cur = p.lg.currency
    out |= {
        "amount_difference_minor": p.amount_diff,
        "date_difference_days": p.date_diff,
        "evidence_score": p.score,
        "score_components": p.components,
        "discrepancies": p.discrepancies,
        "link": {"reference": p.ref, "counterparty": p.cp, "bank_named_parties": _named(p.b)},
    }
    when = f"bank date {p.date_diff:+d} day(s) from the ledger date"
    if status == "exact_match":
        out["explanation"] = (
            f"Ledger {p.lg.txn_id} and bank line {p.b.txn_id}: the same amount {_money(p.lg.amount, cur)} "
            f"({p.lg.flow}), {when}, {LINK_TEXT.get((p.ref, p.cp), 'reference or counterparty agrees')}."
        )
    elif status == "probable_match":
        out["explanation"] = (
            f"Ledger {p.lg.txn_id} and bank line {p.b.txn_id}: the same amount {_money(p.lg.amount, cur)} "
            f"({p.lg.flow}), {when}, and no competing candidate; neither a reference nor the counterparty "
            f"could be compared. Evidence score {p.score:.2f} (not a probability)."
        )
        out["limitations"].append(
            "Matched on amount, date and uniqueness only: the bank description names no known party and "
            "the references are not comparable. Confirm against the payment advice."
        )
    else:
        parts = []
        if "amount" in p.discrepancies:
            parts.append(
                f"the bank amount differs by {_money(p.amount_diff, cur)} ({_money(p.b.amount, cur)} vs "
                f"{_money(p.lg.amount, cur)} in the ledger)"
            )
        if "date" in p.discrepancies:
            parts.append(
                f"the bank date is {p.date_diff:+d} day(s) from the ledger date, outside the normal "
                "clearing window"
            )
        if "reference" in p.discrepancies:
            parts.append(f"the references conflict ({p.lg.reference!r} vs {p.b.reference!r})")
        if "counterparty" in p.discrepancies:
            named = ", ".join(n["name"] for n in _named(p.b)) or "another party"
            parts.append(
                f"the bank description names {named}, not the ledger counterparty {p.lg.counterparty!r}"
            )
        out["explanation"] = (
            f"Candidate match ledger {p.lg.txn_id} ↔ bank line {p.b.txn_id} with discrepancies: "
            + "; ".join(parts)
            + ". Not reconciled: review required."
        )
        out["limitations"].append("A partial match is a review item, never counted as reconciled.")
    return out


def _named(b: RRow) -> list[dict[str, Any]]:
    return [{"entity": e, "name": n, "truncated": t} for e, n, t in b.named_detail]


def _group_alt(x: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": x["orient"],
        "settlement": x["settle"].txn_id,
        "components": [r.txn_id for r in x["parts"]],
        "counterparty_entity": x["cp"],
    }


def _group_result(x: dict[str, Any], cfg: ReconciliationConfig) -> dict[str, Any]:
    s, parts = x["settle"], x["parts"]
    ledger = [s] if s.side == "ledger" else parts
    bank = parts if s.side == "ledger" else [s]
    out = _base("grouped_match", "grouped", ledger, bank)
    comps = []
    diffs = []
    for r in parts:
        d = (s.date - r.date).days if s.side == "bank" else (r.date - s.date).days
        diffs.append(d)
        comps.append(
            {
                "txn_id": r.txn_id,
                "side": r.side,
                "date": r.date.isoformat(),
                "amount_minor": r.amount,
                "date_difference_days": d,
            }
        )
    dsc = sum(_date_score(d, cfg.dates.bank_before_days, cfg.grouping.window_days) for d in diffs) / len(
        diffs
    )
    comp = {"amount": 1.0, "date": round(dsc, 6), "counterparty": 1.0}
    out |= {
        "amount_difference_minor": 0,
        "date_difference_days": {"min": min(diffs), "max": max(diffs)},
        "evidence_score": _score(comp, cfg),
        "score_components": comp,
        "link": {"counterparty": "agree", "counterparty_entity": x["cp"], "type": x["orient"]},
        "components": comps,
    }
    cur = s.currency
    if s.side == "bank":
        out["explanation"] = (
            f"Bank line {s.txn_id} ({_money(s.amount, cur)}) equals the sum of {len(parts)} ledger rows of "
            f"the same counterparty posted {parts[0].date} … {parts[-1].date} (date-contiguous run): a "
            "grouped settlement."
        )
    else:
        out["explanation"] = (
            f"Ledger row {s.txn_id} ({_money(s.amount, cur)}) equals the sum of {len(parts)} bank lines "
            f"naming the same counterparty, {parts[0].date} … {parts[-1].date}: a split settlement."
        )
    out["limitations"].append(
        "Only date-contiguous runs of one counterparty are searched (no subset search); confirm the "
        "remittance advice lists these items."
    )
    return out


def _ambiguous_result(members: list[RRow], alternatives: list[dict[str, Any]], stage: str) -> dict[str, Any]:
    ledger = [r for r in members if r.side == "ledger"]
    bank = [r for r in members if r.side == "bank"]
    out = _base("ambiguous", stage, ledger, bank)
    out["candidates"] = alternatives
    out["explanation"] = (
        f"{len(ledger)} ledger row(s) and {len(bank)} bank line(s) have competing candidates with comparable "
        f"evidence ({len(alternatives)} alternatives); none was selected automatically."
    )
    out["limitations"].append("Resolve by inspecting the source documents; nothing here is reconciled.")
    return out


def _ambiguous(st: _State, comp: list[Pair], stage: str) -> None:
    rows: dict[tuple[str, str], RRow] = {}
    for p in comp:
        rows[("ledger", p.lg.txn_id)] = p.lg
        rows[("bank", p.b.txn_id)] = p.b
    members = [rows[k] for k in sorted(rows)]
    alts = [
        {
            "type": "pair",
            "ledger_txn_id": p.lg.txn_id,
            "bank_txn_id": p.b.txn_id,
            "evidence_score": p.score,
            "score_components": p.components,
            "amount_difference_minor": p.amount_diff,
            "date_difference_days": p.date_diff,
            "discrepancies": p.discrepancies,
            "link": {"reference": p.ref, "counterparty": p.cp},
        }
        for p in sorted(comp, key=lambda p: (-p.score, p.lg.txn_id, p.b.txn_id))
    ]
    st.take(members)
    st.results.append(_ambiguous_result(members, alts, stage))


def _single(r: RRow, status: str, explanation: str, limitations: list[str]) -> dict[str, Any]:
    out = _base(status, None, [r] if r.side == "ledger" else [], [r] if r.side == "bank" else [])
    out["reasons"] = list(r.reasons)
    out["explanation"] = explanation
    out["limitations"] = limitations
    return out


REASON_TEXT = {
    "missing_date": "no usable date",
    "missing_amount": "no usable amount",
    "negative_amount_with_direction": "a negative amount with an explicit direction (not reinterpreted)",
    "zero_amount": "a zero amount",
    "missing_currency": "no currency (never assumed)",
    "missing_direction": "no money-in / money-out direction",
    "missing_account": "no bank account identifier",
    "ambiguous_scope": "the row matches more than one scope filter",
    "outside_scope": "outside every confirmed scope",
    "account_not_in_scope": "a bank account that is not in any confirmed scope",
    "outside_statement_coverage": "its clearing window extends beyond the period the other side covers",
    "unconfirmed_extraction": "a PDF-extracted line neither auto-accepted nor confirmed by a reviewer",
}


def _coverage(rows: list[RRow]) -> dict[tuple, tuple[date, date]]:
    out: dict[tuple, list[date]] = {}
    for r in rows:
        if r.date and r.scope is not None and not r.na and not r.reasons:
            span = out.setdefault((r.scope, r.currency), [r.date, r.date])
            span[0], span[1] = min(span[0], r.date), max(span[1], r.date)
    return {k: (v[0], v[1]) for k, v in out.items()}


def reconcile(
    ledger: list[RRow], bank: list[RRow], cfg: ReconciliationConfig, duplicate_groups: list[dict[str, Any]]
) -> Outcome:
    """Every input row ends in exactly one result. `duplicate_groups` (duplicates.find) turns an unmatched row
    into `duplicate_candidate` when another member of its group is matched."""
    st = _State(ledger, bank, cfg)
    _stage_one_to_one(st)
    _stage_grouped(st)
    _stage_partial(st)
    dc = cfg.dates
    bank_cov, led_cov = _coverage(st.bank), _coverage(st.ledger)
    label_of_scope = {}
    for b in st.bank:
        if b.scope is not None:
            label_of_scope.setdefault(b.scope, account_label(b.account_norm))
    status_of = {
        (side, t): res["status"]
        for res in st.results
        for side in ("ledger", "bank")
        for t in res[side + "_txn_ids"]
    }
    groups_of: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for g in duplicate_groups:
        for t in g["txn_ids"]:
            groups_of[(g["side"], t)].append(g)

    for r in st.ledger + st.bank:
        if not st.free(r):
            continue
        st.take([r])
        if r.na:
            st.results.append(
                _single(
                    r,
                    "not_applicable",
                    "Not reconciled: " + "; ".join(REASON_TEXT[x] for x in r.reasons) + ".",
                    [],
                )
            )
            continue
        if r.reasons:
            st.results.append(
                _single(
                    r,
                    "insufficient_data",
                    "Cannot be reconciled: " + "; ".join(REASON_TEXT[x] for x in r.reasons) + ".",
                    ["Fix the source or its mapping, then run again."],
                )
            )
            continue
        other = bank_cov if r.side == "ledger" else led_cov
        span = other.get((r.scope, r.currency))
        if r.side == "ledger":
            lo, hi = r.date - timedelta(days=dc.bank_before_days), r.date + timedelta(days=dc.bank_after_days)
        else:
            lo, hi = r.date - timedelta(days=dc.bank_after_days), r.date + timedelta(days=dc.bank_before_days)
        if span is not None and (lo < span[0] or hi > span[1]):
            r.reasons.append("outside_statement_coverage")
            st.results.append(
                _single(
                    r,
                    "insufficient_data",
                    "Cannot be reconciled: "
                    + REASON_TEXT["outside_statement_coverage"]
                    + f" ({span[0]} … {span[1]}).",
                    ["Import the adjacent statement / ledger period to decide."],
                )
            )
            continue
        status = "unmatched_ledger" if r.side == "ledger" else "unmatched_bank"
        dup = [
            g
            for g in groups_of.get((r.side, r.txn_id), [])
            if any(status_of.get((g["side"], t)) in MATCHED for t in g["txn_ids"] if t != r.txn_id)
        ]
        if dup:
            res = _single(
                r,
                "duplicate_candidate",
                "",
                [
                    "A duplicate CANDIDATE, not a confirmed duplicate: nothing is excluded or deleted; "
                    "confirm against the source documents."
                ],
            )
            twins = sorted({t for g in dup for t in g["txn_ids"] if t != r.txn_id})
            res["duplicate_group_ids"] = sorted(g["group_id"] for g in dup)
            res["explanation"] = (
                f"{'Ledger row' if r.side == 'ledger' else 'Bank line'} {r.txn_id} "
                f"({_money(r.amount, r.currency)}, {r.flow}, {r.date}) is unmatched and repeats "
                f"{', '.join(twins)}, which {'is' if len(twins) == 1 else 'are'} matched: a possible "
                f"duplicate {'posting' if r.side == 'ledger' else 'bank debit/credit'}."
            )
            st.results.append(res)
            continue
        where = label_of_scope.get(r.scope, "(no bank line in this scope)")
        if r.side == "ledger":
            text = (
                f"Ledger row {r.txn_id} ({_money(r.amount, r.currency)}, {r.flow}, {r.date}) has no bank "
                f"line in account {where}: no equal amount within the date window, no grouped settlement and "
                "no candidate within the partial tolerances."
            )
        else:
            text = (
                f"Bank line {r.txn_id} ({_money(r.amount, r.currency)}, {r.flow}, {r.date}) has no ledger "
                "entry in scope: no equal amount within the date window, no grouped settlement and no "
                "candidate within the partial tolerances."
            )
        st.results.append(_single(r, status, text, []))
    return Outcome(
        results=sorted(st.results, key=lambda x: x["result_id"]),
        truncation={k: sorted(v) for k, v in sorted(st.truncated.items())},
        blocks=len(st.blocks),
        candidate_pairs=st.pairs_seen,
    )


# ------------------------------------------------------------------------------------------- summary


def summarize(results: list[dict[str, Any]], ledger: list[RRow], bank: list[RRow]) -> dict[str, Any]:
    """Aggregates computed only from the row-level results (and checked against the inputs)."""
    by_status = {s: 0 for s in STATUSES}
    rows_by_status = {"ledger": {s: 0 for s in STATUSES}, "bank": {s: 0 for s in STATUSES}}
    amounts: dict[str, dict[str, dict[str, int]]] = {"ledger": {}, "bank": {}}
    for res in results:
        by_status[res["status"]] += 1
        for side in ("ledger", "bank"):
            for row in res[side + "_rows"]:
                rows_by_status[side][res["status"]] += 1
                cur = row["currency"] or "unknown"
                a = row["amount_minor"] or 0
                amounts[side].setdefault(cur, {}).setdefault(res["status"], 0)
                amounts[side][cur][res["status"]] += a
    totals = {"ledger": len(ledger), "bank": len(bank)}
    for side in ("ledger", "bank"):
        require(  # every row exactly once (explicit, so it also holds under python -O)
            sum(rows_by_status[side].values()) == totals[side],
            "reconciliation results do not cover every row exactly once",
            side=side,
            counted=sum(rows_by_status[side].values()),
            rows=totals[side],
        )

    def pick(side: str, statuses: tuple[str, ...]) -> dict[str, int]:
        return {
            cur: s
            for cur, by in sorted(amounts[side].items())
            if (s := sum(v for k, v in by.items() if k in statuses))
        }

    eligible = {
        side: totals[side]
        - rows_by_status[side]["not_applicable"]
        - rows_by_status[side]["insufficient_data"]
        for side in ("ledger", "bank")
    }
    matched_rows = {side: sum(rows_by_status[side][s] for s in MATCHED) for side in ("ledger", "bank")}
    return {
        "ledger_rows": totals["ledger"],
        "bank_rows": totals["bank"],
        "results_by_status": by_status,
        "rows_by_status": rows_by_status,
        "exact_match": by_status["exact_match"],
        "probable_match": by_status["probable_match"],
        "grouped_match": by_status["grouped_match"],
        "partial_match": by_status["partial_match"],
        "ambiguous": by_status["ambiguous"],
        "unmatched_ledger": by_status["unmatched_ledger"],
        "unmatched_bank": by_status["unmatched_bank"],
        "duplicate_candidate": by_status["duplicate_candidate"],
        "insufficient_data": by_status["insufficient_data"],
        "not_applicable": by_status["not_applicable"],
        "eligible_rows": eligible,
        "matched_rows": matched_rows,
        "matched_row_share": {
            side: (round(matched_rows[side] / eligible[side], 6) if eligible[side] else None)
            for side in ("ledger", "bank")
        },
        "amounts_by_status_minor": {side: dict(sorted(amounts[side].items())) for side in ("ledger", "bank")},
        "matched_amount_minor": {side: pick(side, MATCHED) for side in ("ledger", "bank")},
        "partial_amount_minor": {side: pick(side, ("partial_match",)) for side in ("ledger", "bank")},
        "ambiguous_amount_minor": {side: pick(side, ("ambiguous",)) for side in ("ledger", "bank")},
        "unmatched_amount_minor": {side: pick(side, UNMATCHED) for side in ("ledger", "bank")},
        "currency_note": "Amounts are kept per currency (integer minor units in the API); different "
        "currencies are never added.",
        "matched_row_share_note": "matched rows (exact + probable + grouped) / rows that could be reconciled "
        "(excluding not-applicable and insufficient-data rows). Partial, ambiguous and unmatched rows are "
        "not reconciled.",
    }
