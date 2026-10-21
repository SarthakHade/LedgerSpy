"""Relationship graph for one dataset (read-only): build, metrics, cycles, findings, signals, views.

Inputs: the dataset's normalised view and P6 feature input (`features.service.input_for`), and its P12
resolution (the same memoised run P6 / P9 use). Labels, ground truth and supervised (P11) outputs are never
read. P9 risk is read AFTER the analysis, only to annotate nodes for display and prioritisation (`p9_risk`);
it is not an input to any graph detector. Results are cached by identity (dataset, data version, masters,
configuration, window).
"""

import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any

from ledgerspy.cache import NamespacedLRU, namespace
from ledgerspy.config.loader import EffectiveConfig
from ledgerspy.errors import LedgerSpyError
from ledgerspy.features import service as feature_service
from ledgerspy.features.resolution import resolution_of
from ledgerspy.graph import cycles as cycle_mod
from ledgerspy.graph import metrics as metric_mod
from ledgerspy.graph import model, signals
from ledgerspy.hashing import canonical_json, sha256_hex
from ledgerspy.normalize.view import load_view
from ledgerspy.resolution.masters import records_sha256

NOTE = (
    "Graph metrics and signals are additional evidence for review, not proof of fraud. They are a separate "
    "evidence family and do not change P9 review-risk scores."
)


@dataclass
class GraphResult:
    dataset_id: str
    identity: dict[str, Any]
    graph: model.Graph
    metrics: dict[str, Any]
    cycles: dict[str, Any]
    employee_vendor: list[dict[str, Any]]
    ghost: dict[str, Any]
    signals: list[dict[str, Any]]
    p9: dict[str, dict[str, Any]]
    timings: dict[str, float]


_CACHE = NamespacedLRU(6)  # per database (ledgerspy.cache)


def _p9_by_node(conn, cfg, dataset_id: str, res, g: model.Graph) -> dict[str, dict[str, Any]]:
    """Display-only P9 metadata per node: the highest P9 entity score among the node's ledger streams."""
    from ledgerspy.risk import service as risk_service

    try:
        r = risk_service.run_risk(conn, cfg, dataset_id)
    except LedgerSpyError:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for e in r.entities:
        for s in e["member_streams"]:
            node = res.entity_of.get("l:" + s.split(":", 1)[1])
            if node is None or node not in g.nodes:
                continue
            cur = out.get(node)
            if cur is None or e["score"] > cur["score"]:
                out[node] = {
                    "score": e["score"],
                    "tier": e["tier"],
                    "entity_id": e["entity_id"],
                    "flow": e["flow"],
                }
    return out


def run(
    conn, cfg: EffectiveConfig, dataset_id: str, frm: date | None = None, to: date | None = None
) -> GraphResult:
    if frm and to and frm > to:
        raise LedgerSpyError("invalid_window", "`from` must not be after `to`")
    view = load_view(conn, dataset_id)
    masters = feature_service.masters_for(conn, dataset_id)
    st = cfg.settings
    ident = {
        "dataset_id": dataset_id,
        "data_version": view.data_version,
        "masters_sha256": records_sha256(masters),
        "graph_config_sha256": sha256_hex(canonical_json(st.graph.model_dump(mode="json"))),
        "features_config_sha256": sha256_hex(canonical_json(st.features.model_dump(mode="json"))),
        "risk_config_sha256": sha256_hex(canonical_json(st.risk.model_dump(mode="json"))),
        "graph_version": model.GRAPH_VERSION,
        "window": [frm.isoformat() if frm else None, to.isoformat() if to else None],
    }
    key = sha256_hex(canonical_json(ident))
    ns = namespace(conn)
    hit = _CACHE.get(ns, key)
    if hit is not None:
        return hit
    t0 = time.perf_counter()
    inp = feature_service.input_for(conn, cfg, view)
    res = resolution_of(inp)
    t1 = time.perf_counter()
    g = model.build(view.rows, inp.rows, res, (frm, to))
    t2 = time.perf_counter()
    met = metric_mod.compute(g, st.graph.centrality)
    t3 = time.perf_counter()
    cyc = cycle_mod.detect(g, st.graph.cycles)
    t4 = time.perf_counter()
    ev = signals.employee_vendor(g, res)
    ghost = signals.ghost_vendor_support(g, res)
    sig = signals.compute(g, res, met, cyc, ev, st.graph)
    t5 = time.perf_counter()
    p9 = _p9_by_node(conn, cfg, dataset_id, res, g)
    out = GraphResult(
        dataset_id,
        ident | {"graph_sha256": g.sha256, "resolution_sha256": res.sha256},
        g,
        met,
        cyc,
        ev,
        ghost,
        sig,
        p9,
        {
            "features_and_resolution": round(t1 - t0, 4),
            "build": round(t2 - t1, 4),
            "metrics": round(t3 - t2, 4),
            "cycles": round(t4 - t3, 4),
            "findings_and_signals": round(t5 - t4, 4),
            "p9_annotation": round(time.perf_counter() - t5, 4),
        },
    )
    _CACHE.put(ns, key, out)
    return out


def clear_cache() -> None:
    _CACHE.clear()


# ------------------------------------------------------------------------------------------ views


def _page(items: list, page: int, size: int, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    start = (page - 1) * size
    return {
        "total": len(items),
        "page": page,
        "size": size,
        "items": items[start : start + size],
        **(extra or {}),
    }


def _node_row(r: GraphResult, nid: str) -> dict[str, Any]:
    n = r.graph.nodes[nid]
    m = r.metrics["nodes"][nid]
    return n | {
        "metrics": m,
        "exposure": _exposure(m),
        "p9_risk": r.p9.get(nid),
        "signals": sum(1 for s in r.signals if nid in s["entity_ids"]),
    }


def _exposure(m: dict[str, Any]) -> dict[str, int]:
    """Total observed flow through the node (in + out) per currency: the documented node-size metric."""
    out: dict[str, int] = defaultdict(int)
    for side in ("inflow", "outflow"):
        for c, a in m[side].items():
            out[c] += a
    return dict(sorted(out.items()))


def summary(r: GraphResult) -> dict[str, Any]:
    g = r.graph
    return {
        "dataset_id": r.dataset_id,
        "identity": r.identity,
        "graph_sha256": g.sha256,
        "window": g.window,
        "stats": g.stats,
        "metrics": r.metrics["graph"],
        "cycles": r.cycles["summary"],
        "signals": {
            "total": len(r.signals),
            "by_detector": {
                k: sum(1 for s in r.signals if s["rule_id"] == k)
                for k in sorted({s["rule_id"] for s in r.signals})
            },
            "fused_in_p9": False,
        },
        "employee_vendor": {
            c: sum(1 for f in r.employee_vendor if f["classification"] == c)
            for c in (
                "possible_conflict",
                "shared_attribute",
                "shared_attribute (address only)",
                "insufficient_evidence",
            )
        },
        "ghost_vendor_support": {
            "master_data": r.ghost["master_data"],
            "nodes_with_indicators": len(r.ghost["items"]),
        },
        "unavailable_edge_types": model.UNAVAILABLE_EDGE_TYPES,
        "timings_seconds": r.timings,
        "note": NOTE,
    }


SORTS = {
    "exposure": lambda r, n: -sum(_exposure(r.metrics["nodes"][n]).values()),
    "pagerank": lambda r, n: -(r.metrics["nodes"][n]["pagerank"] or 0),
    "betweenness": lambda r, n: -(r.metrics["nodes"][n]["betweenness"] or 0),
    "degree": lambda r, n: -r.metrics["nodes"][n]["counterparty_diversity"],
    "label": lambda r, n: 0,
}


def nodes_page(
    r: GraphResult, page: int, size: int, node_type: str | None, q: str | None, sort: str
) -> dict[str, Any]:
    ids = list(r.graph.nodes)
    if node_type:
        ids = [n for n in ids if r.graph.nodes[n]["node_type"] == node_type]
    if q:
        needle = q.casefold()
        ids = [n for n in ids if needle in r.graph.nodes[n]["label"].casefold() or needle in n.casefold()]
    ids.sort(key=lambda n: (SORTS[sort](r, n), r.graph.nodes[n]["label"].casefold(), n))
    return _page(
        [_node_row(r, n) for n in ids[(page - 1) * size : page * size]],
        1,
        size,
        {"total": len(ids), "page": page, "graph_sha256": r.graph.sha256},
    )


def _edge_summary(e: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in e.items() if k != "evidence"}
    if e["type"] == "paid":
        out["evidence"] = {"source": e["evidence"]["source"], "transactions": len(e["evidence"]["txn_ids"])}
    else:
        out["evidence"] = e["evidence"]
    return out


def edges_page(
    r: GraphResult, page: int, size: int, edge_type: str | None, node: str | None
) -> dict[str, Any]:
    es = r.graph.edges
    if edge_type:
        es = [e for e in es if e["type"] == edge_type]
    if node:
        es = [e for e in es if node in (e["source"], e["target"])]
    out = _page([_edge_summary(e) for e in es[(page - 1) * size : page * size]], 1, size)
    return out | {"total": len(es), "page": page, "graph_sha256": r.graph.sha256}


def edge_detail(r: GraphResult, eid: str, limit: int) -> dict[str, Any]:
    e = r.graph.by_id.get(eid)
    if e is None:
        raise LedgerSpyError("not_found", f"No edge {eid!r} in this graph.", http_status=404)
    out = _edge_summary(e) | {
        "source_label": r.graph.nodes[e["source"]]["label"],
        "target_label": r.graph.nodes[e["target"]]["label"],
    }
    if e["type"] == "paid":
        ids = e["evidence"]["txn_ids"]
        out["transactions_detail"] = [r.graph.txns[t] for t in ids[:limit]]
        out["transactions_truncated"] = len(ids) > limit
    return out


def node_detail(r: GraphResult, nid: str, limit: int) -> dict[str, Any]:
    if nid not in r.graph.nodes:
        raise LedgerSpyError("not_found", f"No node {nid!r} in this graph.", http_status=404)
    eids = r.graph.incident.get(nid, [])
    edges = [r.graph.by_id[x] for x in eids]
    edges.sort(key=lambda e: (e["type"] != "paid", -(e.get("amount_minor") or 0), e["edge_id"]))
    neighbours = sorted({e["target"] if e["source"] == nid else e["source"] for e in edges} - {nid})
    return _node_row(r, nid) | {
        "neighbourhood": {
            "edges": [_edge_summary(e) for e in edges[:limit]],
            "edges_total": len(edges),
            "truncated": len(edges) > limit,
            "neighbours": [
                {"node_id": n, "label": r.graph.nodes[n]["label"], "node_type": r.graph.nodes[n]["node_type"]}
                for n in neighbours[:limit]
            ],
        },
        "cycles": [c["cycle_id"] for c in r.cycles["cycles"] if nid in c["path"]],
        "signals_detail": [s for s in r.signals if nid in s["entity_ids"]],
        "employee_vendor": [
            f["finding_id"]
            for f in r.employee_vendor
            if nid in (f["employee"]["node_id"], f["organisation"]["node_id"])
        ],
        "ghost_vendor_support": next((g for g in r.ghost["items"] if g["node_id"] == nid), None),
    }


def cycles_page(r: GraphResult, page: int, size: int, status: str | None) -> dict[str, Any]:
    cs = r.cycles["cycles"]
    if status:
        cs = [c for c in cs if c["status"] == status]
    return _page(cs, page, size, {"summary": r.cycles["summary"]})


def metrics_page(r: GraphResult, page: int, size: int, sort: str) -> dict[str, Any]:
    ids = sorted(r.graph.nodes, key=lambda n: (SORTS[sort](r, n), r.graph.nodes[n]["label"].casefold(), n))
    rows = [
        {
            "node_id": n,
            "label": r.graph.nodes[n]["label"],
            "node_type": r.graph.nodes[n]["node_type"],
            **r.metrics["nodes"][n],
        }
        for n in ids[(page - 1) * size : page * size]
    ]
    return {
        "total": len(ids),
        "page": page,
        "size": size,
        "items": rows,
        "graph": r.metrics["graph"],
        "definitions": DEFINITIONS,
    }


DEFINITIONS = {
    "in_degree / out_degree": "distinct payers / payees of the node in the money graph",
    "inflow / outflow": "sum of payment amounts received / paid, per currency (never summed across "
    "currencies)",
    "reciprocity": "share of the node's counterparties it both pays and is paid by; null without "
    "counterparties",
    "concentration_out": "Herfindahl index of outflow across payees (1 = all to one payee); single "
    "currency only",
    "counterparty_diversity": "distinct counterparties in either direction",
    "clustering": "local clustering coefficient of the undirected money graph; null below two neighbours",
    "pagerank": "weighted PageRank (damping 0.85; amount weights, or transaction counts with several "
    "currencies)",
    "betweenness": "exact normalised Brandes betweenness (directed, unweighted); unavailable above the "
    "node limit",
    "percentiles": "rank among nodes of the same type that have money edges",
    "interpretation": "position in the observed payment network; the audited group's own entities are "
    "hubs by construction; high centrality is not evidence of fraud",
}


def visualization(
    r: GraphResult, max_nodes: int, max_edges: int, edge_types: list[str] | None
) -> dict[str, Any]:
    """Nodes and typed edges for a network view. No coordinates are produced (the layout is a client choice).

    Selection: nodes in cycles or signals first, then by exposure (total flow), then by label; edges among
    the selected nodes by type order and amount. Size = exposure (documented); intensity = PageRank
    percentile.
    """
    g = r.graph
    flagged = {n for c in r.cycles["cycles"] for n in c["path"]} | {
        n for s in r.signals for n in s["entity_ids"]
    }
    ids = sorted(
        g.nodes,
        key=lambda n: (
            n not in flagged,
            -sum(_exposure(r.metrics["nodes"][n]).values()),
            g.nodes[n]["label"].casefold(),
            n,
        ),
    )
    ids = [n for n in ids if r.metrics["nodes"][n]["counterparty_diversity"] or g.incident.get(n)]
    chosen = set(ids[:max_nodes])
    types = set(edge_types or model.EDGE_TYPES)
    es = [e for e in g.edges if e["type"] in types and e["source"] in chosen and e["target"] in chosen]
    es.sort(key=lambda e: (e["type"] != "paid", -(e.get("amount_minor") or 0), e["edge_id"]))
    total_edges = len(es)
    es = es[:max_edges]
    nodes = []
    for n in sorted(chosen):
        m = r.metrics["nodes"][n]
        nodes.append(
            {
                "node_id": n,
                "label": g.nodes[n]["label"],
                "node_type": g.nodes[n]["node_type"],
                "resolution_status": g.nodes[n]["resolution_status"],
                "has_master_record": g.nodes[n]["has_master_record"],
                "size_metric": {
                    "name": "exposure (in + out flow, minor units, per currency)",
                    "value": _exposure(m),
                },
                "intensity_metric": {
                    "name": "PageRank percentile among peers",
                    "value": m["pagerank_percentile"],
                },
                "betweenness_percentile": m["betweenness_percentile"],
                "transactions": m["in_txns"] + m["out_txns"],
                "p9_risk": r.p9.get(n),
                "in_cycle": any(n in c["path"] for c in r.cycles["cycles"]),
                "signals": sorted(s["rule_id"] for s in r.signals if n in s["entity_ids"]),
            }
        )
    return {
        "graph_sha256": g.sha256,
        "window": g.window,
        "nodes": nodes,
        "edges": [
            {
                "edge_id": e["edge_id"],
                "type": e["type"],
                "source": e["source"],
                "target": e["target"],
                "directed": e["directed"],
                "currency": e.get("currency"),
                "amount_minor": e.get("amount_minor"),
                "transactions": e.get("transactions"),
                "first_date": e.get("first_date"),
                "last_date": e.get("last_date"),
                "status": e.get("status"),
            }
            for e in es
        ],
        "truncated": {
            "nodes": len(ids) > max_nodes,
            "nodes_total": len(ids),
            "edges": total_edges > max_edges,
            "edges_total": total_edges,
        },
        "cycles": [
            {"cycle_id": c["cycle_id"], "path": c["path"], "status": c["status"]}
            for c in r.cycles["cycles"]
            if set(c["path"]) <= chosen
        ][:200],
        "legend": {
            "size": "exposure: total observed flow through the node (in + out)",
            "intensity": "PageRank percentile among peers of the same type",
            "p9_risk": "P9 entity score, for display only (not a graph input)",
        },
        "note": NOTE,
    }
