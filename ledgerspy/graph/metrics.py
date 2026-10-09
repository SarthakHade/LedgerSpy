"""Network metrics on the money graph (`paid` edges, self-loops excluded) and components over all edges.

Per node (null = not applicable / unavailable, never a disguised zero):
- in_degree / out_degree: distinct payers / payees; in_txns / out_txns: transaction counts;
- inflow / outflow: amounts per currency (never summed across currencies);
- reciprocity: share of the node's money counterparties that it both pays and is paid by (null without any);
- concentration_out: Herfindahl index of outflow across payees (1 = one payee), single-currency outflow only;
  top_payee_share likewise; counterparty_diversity: distinct counterparties in either direction;
- clustering: local clustering coefficient of the undirected money projection (null below degree 2);
- pagerank: weighted PageRank (damping d; weights = amounts when the graph has one currency, else transaction
  counts); dangling mass is spread uniformly;
- betweenness: exact Brandes betweenness on the directed unweighted money graph, normalised by (n−1)(n−2);
  unavailable (with the reason) above `betweenness_max_nodes` or `betweenness_max_work` = nodes × money
  edges (never silently sampled);
- percentiles: rank among peers of the same node type that have money edges;
- components: weakly connected components of the money graph and of the full graph (money + attributes).
Interpretation: centrality describes position in the observed payment network. The audited group's own
entities are hubs by construction; high centrality is never evidence of fraud on its own.
"""

from bisect import bisect_right
from collections import defaultdict, deque
from typing import Any

from ledgerspy.config.schema import GraphCentrality
from ledgerspy.graph.model import Graph


def _components(nodes: list[str], links: list[tuple[str, str]]) -> dict[str, int]:
    parent = {n: n for n in nodes}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in links:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    roots = sorted({find(n) for n in nodes})
    index = {r: i for i, r in enumerate(roots)}
    return {n: index[find(n)] for n in nodes}


def _pagerank(nodes, out_w, cfg: GraphCentrality) -> tuple[dict[str, float], dict[str, Any]]:
    n = len(nodes)
    if n == 0:
        return {}, {"status": "empty graph", "iterations": 0}
    d = cfg.pagerank_damping
    pr = {v: 1.0 / n for v in nodes}
    tot = {u: sum(ws.values()) for u, ws in out_w.items()}
    incoming: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for u in sorted(out_w):
        for v, w in sorted(out_w[u].items()):
            incoming[v].append((u, w / tot[u]))
    it = 0
    for it in range(1, cfg.pagerank_max_iter + 1):
        dangling = sum(pr[v] for v in nodes if not tot.get(v))
        new = {
            v: (1 - d) / n + d * (dangling / n + sum(pr[u] * w for u, w in incoming.get(v, ())))
            for v in nodes
        }
        delta = sum(abs(new[v] - pr[v]) for v in nodes)
        pr = new
        if delta < cfg.pagerank_tol:
            return pr, {"status": "converged", "iterations": it}
    return pr, {"status": "not converged (max_iter reached)", "iterations": it}


def _betweenness(nodes: list[str], succ: dict[str, list[str]]) -> dict[str, float]:
    """Brandes (2001), directed, unweighted; normalised by (n−1)(n−2)."""
    bc = dict.fromkeys(nodes, 0.0)
    for s in nodes:
        stack, pred = [], defaultdict(list)
        sigma = defaultdict(float)
        sigma[s] = 1.0
        dist = {s: 0}
        q = deque([s])
        while q:
            v = q.popleft()
            stack.append(v)
            for w in succ.get(v, ()):
                if w not in dist:
                    dist[w] = dist[v] + 1
                    q.append(w)
                if dist[w] == dist[v] + 1:
                    sigma[w] += sigma[v]
                    pred[w].append(v)
        delta = defaultdict(float)
        while stack:
            w = stack.pop()
            for v in pred[w]:
                delta[v] += sigma[v] / sigma[w] * (1 + delta[w])
            if w != s:
                bc[w] += delta[w]
    n = len(nodes)
    scale = 1.0 / ((n - 1) * (n - 2)) if n > 2 else 0.0
    return {v: bc[v] * scale for v in nodes}


def _percentiles(values: dict[str, float | None], peers: dict[str, str]) -> dict[str, float | None]:
    by_group: dict[str, list[float]] = defaultdict(list)
    for v, x in values.items():
        if x is not None and v in peers:
            by_group[peers[v]].append(x)
    for grp in by_group.values():
        grp.sort()
    out = {}
    for v, x in values.items():
        if x is None or v not in peers:
            out[v] = None
            continue
        grp = by_group[peers[v]]
        out[v] = round(bisect_right(grp, x) / len(grp), 6)  # share of peers with a value ≤ x
    return out


def compute(g: Graph, cfg: GraphCentrality) -> dict[str, Any]:
    nodes = sorted(g.nodes)
    money = [e for e in g.money() if not e["self_loop"]]
    currencies = sorted({e["currency"] or "unknown" for e in money})
    single = len(currencies) <= 1
    out_amt: dict[str, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    out_cnt: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for e in money:
        out_cnt[e["source"]][e["target"]] += e["transactions"]
        if e["amount_minor"] is not None:
            out_amt[e["source"]][e["target"]][e["currency"] or "unknown"] += e["amount_minor"]
    succ = {u: sorted(vs) for u, vs in out_cnt.items()}
    pred: dict[str, set[str]] = defaultdict(set)
    for u, vs in out_cnt.items():
        for v in vs:
            pred[v].add(u)
    weights = {
        u: (
            {v: float(sum(out_amt[u][v].values())) or float(out_cnt[u][v]) for v in vs}
            if single
            else {v: float(c) for v, c in vs.items()}
        )
        for u, vs in out_cnt.items()
    }
    pr, pr_info = _pagerank(nodes, weights, cfg)
    work = len(nodes) * sum(len(vs) for vs in succ.values())
    if len(nodes) <= cfg.betweenness_max_nodes and work <= cfg.betweenness_max_work:
        bc: dict[str, float | None] = dict(_betweenness(nodes, succ))
        bc_info = {"status": "computed (exact Brandes)"}
    else:
        bc = dict.fromkeys(nodes)
        bc_info = {
            "status": "unavailable",
            "reason": f"{len(nodes)} nodes (limit {cfg.betweenness_max_nodes}), {work} node × edge work "
            f"(limit {cfg.betweenness_max_work}): exact "
            "betweenness "
            "would be too slow and it is never sampled silently",
        }
    undirected: dict[str, set[str]] = defaultdict(set)
    for u, vs in out_cnt.items():
        for v in vs:
            undirected[u].add(v)
            undirected[v].add(u)

    per: dict[str, dict[str, Any]] = {}
    for v in nodes:
        outs, ins = set(out_cnt.get(v, {})), pred.get(v, set())
        nbrs = outs | ins
        outflow = defaultdict(int)
        for t in outs:
            for c, a in out_amt[v][t].items():
                outflow[c] += a
        inflow = defaultdict(int)
        for s in ins:
            for c, a in out_amt[s][v].items():
                inflow[c] += a
        conc = top = None
        if len(outflow) == 1 and next(iter(outflow.values())) > 0:
            tot = next(iter(outflow.values()))
            shares = [sum(out_amt[v][t].values()) / tot for t in outs if out_amt[v][t]]
            conc = round(sum(s * s for s in shares), 6)
            top = round(max(shares), 6)
        deg = len(undirected.get(v, ()))
        clustering = None
        if deg >= 2:
            nb = sorted(undirected[v])
            links = sum(1 for i, a in enumerate(nb) for b in nb[i + 1 :] if b in undirected[a])
            clustering = round(2 * links / (deg * (deg - 1)), 6)
        per[v] = {
            "in_degree": len(ins),
            "out_degree": len(outs),
            "in_txns": sum(out_cnt[s][v] for s in ins),
            "out_txns": sum(out_cnt[v].values()) if v in out_cnt else 0,
            "inflow": dict(sorted(inflow.items())),
            "outflow": dict(sorted(outflow.items())),
            "reciprocity": round(len(outs & ins) / len(nbrs), 6) if nbrs else None,
            "concentration_out": conc,
            "top_payee_share": top,
            "counterparty_diversity": len(nbrs),
            "clustering": clustering,
            "pagerank": round(pr[v], 10) if v in pr else None,
            "betweenness": round(bc[v], 10) if bc.get(v) is not None else None,
        }
    peers = {v: g.nodes[v]["node_type"] for v in nodes if per[v]["counterparty_diversity"]}
    prp = _percentiles({v: per[v]["pagerank"] for v in nodes}, peers)
    bcp = _percentiles({v: per[v]["betweenness"] for v in nodes}, peers)
    money_comp = _components(nodes, [(e["source"], e["target"]) for e in money])
    all_comp = _components(nodes, [(e["source"], e["target"]) for e in g.edges])
    for v in nodes:
        per[v]["pagerank_percentile"] = prp[v]
        per[v]["betweenness_percentile"] = bcp[v]
        per[v]["money_component"] = money_comp[v]
        per[v]["component"] = all_comp[v]
    sizes = defaultdict(int)
    for v in nodes:
        sizes[all_comp[v]] += 1
    msizes = defaultdict(int)
    for v in nodes:
        if per[v]["counterparty_diversity"]:
            msizes[money_comp[v]] += 1
    pairs = {(e["source"], e["target"]) for e in money}
    n = len(nodes)
    return {
        "nodes": per,
        "graph": {
            "nodes": n,
            "money_edges": len(pairs),
            "density": round(len(pairs) / (n * (n - 1)), 8) if n > 1 else None,
            "reciprocity": round(sum(1 for a, b in pairs if (b, a) in pairs) / len(pairs), 6)
            if pairs
            else None,
            "components": len(sizes),
            "largest_component": max(sizes.values()) if sizes else 0,
            "money_components": len(msizes),
            "largest_money_component": max(msizes.values()) if msizes else 0,
            "isolated_nodes": sum(1 for v in nodes if not per[v]["counterparty_diversity"]),
            "pagerank": pr_info
            | {"weights": "amount" if single else "transaction count (several currencies)"},
            "betweenness": bc_info,
            "currencies": currencies,
        },
    }
