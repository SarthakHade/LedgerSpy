"""Bounded, deterministic circular-flow detection on the money graph.

Enumeration. Simple directed cycles of length `min_length`..`max_length` over `paid` edges (self-loops
excluded; edges with fewer than `min_edge_txns` transactions or less than `min_edge_amount_minor` excluded).
Each cycle is found once, from its smallest node ID (canonical rotation), by depth-first search that only
visits nodes with a larger ID, in sorted order. The search stops at `max_cycles` cycles or
`max_search_steps` expansions and then reports `truncated` with the reason: a partial result is never
presented as complete.

Assessment. A structural cycle is ordinary in commerce (a vendor that also buys scrap; group funding). Each
cycle
is assessed on its transactions:
- time-ordered chain: one payment per edge, each on or after the previous one, completing within
  `window_days`.
  A chain may start on any edge of the cycle (every rotation is tried). From each starting payment (up to
  `max_chains` per rotation; repeated payments give several chains) a bounded depth-first search over the
  payments inside the window finds the chain that retains the most value (then the shortest span); the
  search budget per cycle is `max_search_steps` / 100 and reaching it is reported as a limitation;
- value retention: smallest / largest amount in that chain (same currency only);
- status: `review` (time-ordered within the window and retention ≥ `retention_min`), `ordered_low_retention`,
  `not_time_ordered` (structural only) or `insufficient_data` (missing dates or amounts, stated).
"""

from collections import defaultdict
from datetime import datetime
from typing import Any

from ledgerspy.config.schema import GraphCycles
from ledgerspy.graph.model import Graph
from ledgerspy.hashing import canonical_json, sha256_hex


def enumerate_cycles(succ: dict[str, list[str]], cfg: GraphCycles) -> tuple[list[list[str]], dict[str, Any]]:
    cycles: list[list[str]] = []
    steps = 0
    truncated = None
    for s in sorted(succ):
        path = [s]
        on_path = {s}
        stack = [iter(succ.get(s, []))]
        while stack:
            nxt = next(stack[-1], None)
            if nxt is None:
                stack.pop()
                on_path.discard(path.pop())
                continue
            steps += 1
            if steps > cfg.max_search_steps:
                truncated = f"search budget of {cfg.max_search_steps} expansions reached"
                break
            if nxt == s:
                if len(path) >= cfg.min_length:
                    cycles.append(list(path))
                    if len(cycles) >= cfg.max_cycles:
                        truncated = f"maximum of {cfg.max_cycles} cycles reached"
                        break
                continue
            if nxt < s or nxt in on_path or len(path) >= cfg.max_length:
                continue
            path.append(nxt)
            on_path.add(nxt)
            stack.append(iter(succ.get(nxt, [])))
        if truncated:
            break
    return cycles, {"steps": steps, "truncated": truncated is not None, "truncation_reason": truncated}


def _ts(t: dict[str, Any]) -> datetime | None:
    return datetime.fromisoformat(t["ts"]) if t["ts"] else None


def assess(
    path: list[str], edge_of: dict[tuple[str, str], list[dict[str, Any]]], g: Graph, cfg: GraphCycles
) -> dict[str, Any]:
    hops = list(zip(path, path[1:] + path[:1], strict=True))
    hop_edges = [edge_of[h] for h in hops]
    hop_txns = []
    for es in hop_edges:
        ts = [g.txns[t] for e in es for t in e["evidence"]["txn_ids"]]
        hop_txns.append(sorted(ts, key=lambda t: (t["ts"] or "", t["txn_id"])))
    limitations = []
    if any(not any(_ts(t) for t in ts) for ts in hop_txns):
        limitations.append("an edge has no dated transaction: time ordering cannot be assessed")
    dated = [[t for t in ts if _ts(t)] for ts in hop_txns]
    budget = max(1000, cfg.max_search_steps // 100)
    steps = 0
    best_key, best = None, None
    starts = 0
    complete_starts = 0

    def key_of(chain):
        amts = [t["amount_minor"] for t in chain]
        same = len({t["currency"] for t in chain}) == 1 and None not in amts and max(amts) > 0
        ret = min(amts) / max(amts) if same else -1.0
        return (-ret, _ts(chain[-1]) - _ts(chain[0]), chain[0]["ts"], [t["txn_id"] for t in chain])

    for r in range(len(path)):
        order = [dated[(r + i) % len(path)] for i in range(len(path))]
        for first in order[0][: cfg.max_chains]:
            starts += 1
            t0 = _ts(first)
            found = False
            stack = [(1, [first])]
            while stack:
                hop, chain = stack.pop()
                if hop == len(path):
                    found = True
                    k = key_of(chain)
                    if best_key is None or k < best_key:
                        best_key, best = k, chain
                    continue
                prev = _ts(chain[-1])
                for t in reversed(order[hop]):
                    d = _ts(t)
                    if d < prev or (d - t0).days > cfg.window_days:  # type: ignore[operator]
                        continue
                    steps += 1
                    if steps > budget:
                        break
                    stack.append((hop + 1, [*chain, t]))
                if steps > budget:
                    break
            complete_starts += found
            if steps > budget:
                break
        if steps > budget:
            limitations.append(f"chain search budget ({budget} steps) reached: the best chain may be missed")
            break
    chains = range(complete_starts)
    retention = None
    if best:
        amts = [t["amount_minor"] for t in best]
        curs = {t["currency"] for t in best}
        if None in amts:
            limitations.append("a chain payment has no amount: value retention unavailable")
        elif len(curs) > 1:
            limitations.append(
                "the chain mixes currencies: value retention unavailable (no conversion source)"
            )
        elif max(amts) > 0:
            retention = round(min(amts) / max(amts), 6)
    if best is None:
        status = "insufficient_data" if limitations else "not_time_ordered"
    elif retention is None:
        status = "insufficient_data"
    elif retention >= cfg.retention_min:
        status = "review"
    else:
        status = "ordered_low_retention"
    per_edge = []
    for (a, b), es in zip(hops, hop_edges, strict=True):
        per_edge.append(
            {
                "from": a,
                "to": b,
                "edge_ids": [e["edge_id"] for e in es],
                "transactions": sum(e["transactions"] for e in es),
                "amount_minor": {e["currency"] or "unknown": e["amount_minor"] for e in es},
                "first_date": min((e["first_date"] for e in es if e["first_date"]), default=None),
                "last_date": max((e["last_date"] for e in es if e["last_date"]), default=None),
            }
        )
    reasons = []
    if best:
        span = (_ts(best[-1]) - _ts(best[0])).days
        reasons.append(f"payments follow the cycle in time order within {span} day(s)")
        if retention is not None and retention >= cfg.retention_min:
            reasons.append(f"value largely retained along the cycle (smallest/largest = {retention:.2f})")
    else:
        span = None
    cid = "C-" + sha256_hex(canonical_json(path))[:16]
    return {
        "cycle_id": cid,
        "length": len(path),
        "path": path,
        "edges": per_edge,
        "status": status,
        "ordered_chains": len(chains),
        "chains_evaluated": starts,
        "best_chain": [
            {k: t[k] for k in ("txn_id", "date", "ts", "amount_minor", "currency", "payer", "payee")}
            for t in best
        ]
        if best
        else None,
        "span_days": span,
        "value_retention": retention,
        "reasons_for_review": reasons if status == "review" else [],
        "txn_ids": sorted({t for es in hop_edges for e in es for t in e["evidence"]["txn_ids"]}),
        "limitations": limitations
        + [
            "a cycle in payments is common in ordinary trade (suppliers that are also customers, group "
            "funding)"
        ],
    }


def detect(g: Graph, cfg: GraphCycles) -> dict[str, Any]:
    edge_of: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for e in g.money():
        if e["self_loop"] or e["transactions"] < cfg.min_edge_txns:
            continue
        if (e["amount_minor"] or 0) < cfg.min_edge_amount_minor:
            continue
        edge_of[(e["source"], e["target"])].append(e)
    succ: dict[str, list[str]] = defaultdict(list)
    for a, b in sorted(edge_of):
        succ[a].append(b)
    paths, info = enumerate_cycles(dict(succ), cfg)
    found = [assess(p, edge_of, g, cfg) for p in paths]
    found.sort(
        key=lambda c: (
            ["review", "ordered_low_retention", "insufficient_data", "not_time_ordered"].index(c["status"]),
            c["length"],
            c["cycle_id"],
        )
    )
    by_status = defaultdict(int)
    for c in found:
        by_status[c["status"]] += 1
    return {
        "cycles": found,
        "summary": {
            "searched_edges": sum(len(v) for v in edge_of.values()),
            "cycles": len(found),
            "by_status": dict(sorted(by_status.items())),
            "length_range": [cfg.min_length, cfg.max_length],
            "window_days": cfg.window_days,
            **info,
        },
    }
