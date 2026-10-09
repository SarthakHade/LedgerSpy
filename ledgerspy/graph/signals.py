"""Graph evidence: employee–vendor findings, ghost-vendor support and graph signals.

Graph signals use the P7 signal shape (deterministic `signal_id` from detector, version and subject) in the
`relationship` family. They are an INDEPENDENT evidence family: P9 does not fuse them (weight null), so no
P9 score changes. Every signal lists the entities, transactions, metrics, evidence, explanation and
limitations.

Detectors (version 1):
- `graph_circular_flow`: a cycle whose payments follow it in time within the window and retain value;
- `graph_reciprocal_flow`: A→B and B→A within `reciprocal_window_days` with similar amounts;
- `graph_pass_through`: a receipt followed within `pass_through_window_days` by payments of 75–125 % of it to
  ≥ `pass_through_min_new_counterparties` counterparties first seen in that window;
- `graph_employee_vendor_identifier`: an employee and an organisation share a bank account or tax ID;
- `graph_shared_identifier_cluster`: ≥ `shared_identifier_cluster_min` organisations linked by shared bank
  accounts or tax IDs;
- `graph_high_betweenness`: a counterparty (not a group company) in the top betweenness percentile of its
  peers:
  context only, never evidence of fraud on its own.
"""

from collections import defaultdict
from datetime import datetime
from typing import Any

from ledgerspy.config.schema import GraphConfig
from ledgerspy.forensics.models import signal_id
from ledgerspy.graph.model import Graph
from ledgerspy.resolution.engine import Resolution

VERSION = 1
GROUP_TYPES = ("group_company",)


def _signal(
    rule, subject_type, subject_id, entities, txns, severity, metrics, evidence, explanation, limitations
):
    return {
        "signal_id": signal_id(rule, VERSION, subject_type, subject_id),
        "rule_id": rule,
        "rule_version": VERSION,
        "code": f"relationship.{rule}",
        "family": "relationship",
        "subject_type": subject_type,
        "subject_id": subject_id,
        "entity_ids": sorted(entities),
        "txn_ids": sorted(txns),
        "severity": severity,
        "strength": 1.0,
        "weight": None,
        "fused_in_p9": False,
        "metrics": metrics,
        "evidence": evidence,
        "explanation": explanation,
        "limitations": limitations,
    }


# ------------------------------------------------------------------------------------------ employee–vendor

EVIDENCE_TO_OBTAIN_EV = [
    "the employee's declaration of interests and any related-party register",
    "vendor onboarding documents (who requested and approved the vendor)",
    "bank confirmation of the account holder for the shared account",
    "approvals of the payments to this vendor (was the employee involved?)",
]


def employee_vendor(g: Graph, res: Resolution) -> list[dict[str, Any]]:
    out = []
    for pid in sorted(res.pairs):
        p = res.pairs[pid]
        ra, rb = res.records[p["a"]], res.records[p["b"]]
        if {ra.entity_class, rb.entity_class} != {"person", "organisation"}:
            continue
        emp, org = (ra, rb) if ra.entity_class == "person" else (rb, ra)
        if emp.entity_type != "employee":
            continue
        ids = p["identifiers"]
        strong = [f for f in ("bank_account", "tax_id") if ids[f] == "match"]
        contact = [f for f in ("phone", "email") if ids[f] == "match"]
        generic = [f for f in ("phone", "email") if ids[f] == "shared_contact"]
        address = bool(p["address"] and p["address"]["same_address"])
        if strong:
            cls = "possible_conflict"
        elif contact:
            cls = "shared_attribute"
        elif address:
            cls = "shared_attribute (address only)"
        elif generic:
            cls = "insufficient_evidence"
        else:
            continue
        en, on = res.entity_of[emp.key], res.entity_of[org.key]
        received = [e for e in g.money() if e["target"] == on]
        txn_ids = sorted(t for e in received for t in e["evidence"]["txn_ids"])
        shared = {f: emp.norm.get(f) for f in strong + contact + generic}
        if address:
            shared["address"] = emp.raw.get("address")
        out.append(
            {
                "finding_id": "EV-" + pid[2:],
                "classification": cls,
                "employee": {"node_id": en, "record": emp.key, "name": emp.name.raw},
                "organisation": {
                    "node_id": on,
                    "record": org.key,
                    "name": org.name.raw,
                    "type": org.entity_type,
                },
                "shared": shared,
                "pair_id": pid,
                "payments_to_organisation": {
                    "transactions": len(txn_ids),
                    "amount_minor": {
                        c: sum(e["amount_minor"] or 0 for e in received if (e["currency"] or "unknown") == c)
                        for c in sorted({e["currency"] or "unknown" for e in received})
                    },
                    "txn_ids": txn_ids[:200],
                    "txn_ids_truncated": len(txn_ids) > 200,
                },
                "explanation": (
                    f"Employee {emp.name.raw} and {org.entity_type} {org.name.raw} share "
                    + ", ".join(shared)
                    + (
                        ": a strong identifier, a possible undeclared conflict of interest."
                        if strong
                        else ": a shared attribute, not proof of a relationship."
                    )
                ),
                "not_concluded": "ownership or control is never inferred; a declared relationship register "
                "is not "
                "ingested, so no relationship is reported as confirmed",
                "evidence_to_obtain": EVIDENCE_TO_OBTAIN_EV,
            }
        )
    order = [
        "possible_conflict",
        "shared_attribute",
        "shared_attribute (address only)",
        "insufficient_evidence",
    ]
    return sorted(out, key=lambda f: (order.index(f["classification"]), f["finding_id"]))


# ------------------------------------------------------------------------------------- ghost-vendor support

EVIDENCE_TO_OBTAIN_GHOST = [
    "the vendor-master record and its creation / approval trail",
    "tax registration (e.g. GSTIN / PAN) verified with the issuing authority",
    "proof of delivery or service for the paid invoices (goods receipts, timesheets)",
    "bank confirmation of the beneficiary account holder",
]


def ghost_vendor_support(g: Graph, res: Resolution) -> dict[str, Any]:
    masters = any(r.source == "master" and r.entity_type == "vendor" for r in res.records.values())
    edges_of = defaultdict(list)
    for e in g.edges:
        if e["type"] != "paid":
            edges_of[e["source"]].append(e)
            edges_of[e["target"]].append(e)
    received_by = defaultdict(list)
    for e in g.money():
        if not e["self_loop"]:
            received_by[e["target"]].append(e)
    items = []
    for nid, n in sorted(g.nodes.items()):
        if "payee" not in n["roles"] or n["node_type"] in ("group_company", "employee", "account"):
            continue
        received = received_by.get(nid, [])
        if not received:
            continue
        indicators = []
        if masters and not n["has_master_record"]:
            indicators.append(
                {"indicator": "no_master_record", "detail": "paid, but linked to no master record"}
            )
        for e in edges_of.get(nid, []):
            other = e["target"] if e["source"] == nid else e["source"]
            if e["type"] == "potential_conflict_of_interest":
                indicators.append(
                    {
                        "indicator": "shares_strong_identifier_with_employee",
                        "edge_id": e["edge_id"],
                        "other": other,
                    }
                )
            elif (
                e["type"] in ("shares_bank_account", "shares_tax_id")
                and g.nodes[other]["node_type"] != "employee"
            ):
                indicators.append(
                    {"indicator": f"{e['type']}_with_other_entity", "edge_id": e["edge_id"], "other": other}
                )
        if n["has_master_record"]:
            for m in [res.records[k] for k in res.entity(nid)["members"] if k.startswith("m:")]:
                missing = [f for f in ("tax_id", "phone", "email", "bank_account") if not m.raw.get(f)]
                if missing:
                    indicators.append(
                        {"indicator": "incomplete_master_record", "record": m.key, "missing": missing}
                    )
        if not indicators:
            continue
        txn_ids = sorted(t for e in received for t in e["evidence"]["txn_ids"])
        items.append(
            {
                "node_id": nid,
                "label": n["label"],
                "status": "indicators_present",
                "indicators": indicators,
                "payments": {"transactions": len(txn_ids), "txn_ids": txn_ids[:200]},
                "conclusion": "none: these are leads for verification, not a finding that the vendor is "
                "fictitious",
                "evidence_to_obtain": EVIDENCE_TO_OBTAIN_GHOST,
            }
        )
    return {
        "master_data": "vendor master imported"
        if masters
        else "no vendor master imported: a missing master record cannot be assessed",
        "items": items,
    }


# ------------------------------------------------------------------------------------------ signals


def _dt(t: dict[str, Any]) -> datetime | None:
    return datetime.fromisoformat(t["ts"]) if t.get("ts") else None


def compute(
    g: Graph,
    res: Resolution,
    metrics: dict[str, Any],
    cycles: dict[str, Any],
    ev: list[dict],
    cfg: GraphConfig,
) -> list[dict[str, Any]]:
    sc = cfg.signals
    out = []
    for c in cycles["cycles"]:
        if c["status"] != "review":
            continue
        out.append(
            _signal(
                "graph_circular_flow",
                "group",
                c["cycle_id"],
                c["path"],
                [t["txn_id"] for t in c["best_chain"]],
                "high",
                {
                    "length": c["length"],
                    "span_days": c["span_days"],
                    "value_retention": c["value_retention"],
                    "ordered_chains": c["ordered_chains"],
                },
                {"path": c["path"], "best_chain": c["best_chain"]},
                f"Money flows around a {c['length']}-party cycle in time order within {c['span_days']} "
                "day(s), "
                f"keeping {c['value_retention']:.0%} of its value.",
                c["limitations"],
            )
        )
    money = defaultdict(list)
    for e in g.money():
        if not e["self_loop"]:
            money[(e["source"], e["target"])].extend(g.txns[t] for t in e["evidence"]["txn_ids"])
    for (a, b), ab in sorted(money.items()):
        if a > b or (b, a) not in money:
            continue
        ba = money[(b, a)]
        best = None
        for x in ab:
            for y in ba:
                if (
                    not (_dt(x) and _dt(y))
                    or x["amount_minor"] in (None, 0)
                    or y["amount_minor"] in (None, 0)
                ):
                    continue
                if x["currency"] != y["currency"]:
                    continue
                gap = abs((_dt(y) - _dt(x)).days)
                ratio = min(x["amount_minor"], y["amount_minor"]) / max(x["amount_minor"], y["amount_minor"])
                if gap <= sc.reciprocal_window_days and ratio >= sc.reciprocal_amount_ratio:
                    key = (gap, -ratio, x["txn_id"], y["txn_id"])
                    if best is None or key < best[0]:
                        best = (key, x, y, gap, ratio)
        if best:
            _, x, y, gap, ratio = best
            group = any(g.nodes[n]["node_type"] in GROUP_TYPES for n in (a, b))
            out.append(
                _signal(
                    "graph_reciprocal_flow",
                    "group",
                    f"{a}|{b}",
                    [a, b],
                    [x["txn_id"], y["txn_id"]],
                    "medium",
                    {
                        "gap_days": gap,
                        "amount_ratio": round(ratio, 6),
                        "a_to_b_txns": len(ab),
                        "b_to_a_txns": len(ba),
                    },
                    {"payment": x, "return_payment": y, "group_internal": group},
                    f"{g.nodes[a]['label']} and {g.nodes[b]['label']} paid each other similar amounts "
                    f"({ratio:.0%}) {gap} day(s) apart.",
                    ["two-way trade (a supplier that also buys) and group funding are common"]
                    + (["one party is a group company: intra-group funding"] if group else []),
                )
            )
    # pass-through to newly seen counterparties (not "new" during the warm-up after the data starts)
    first_in: dict[str, datetime] = {}
    all_dates = [d for t in g.txns.values() if (d := _dt(t))]
    start = min(all_dates) if all_dates else None
    for e in g.money():
        for t in e["evidence"]["txn_ids"]:
            d = _dt(g.txns[t])
            if d and (e["target"] not in first_in or d < first_in[e["target"]]):
                first_in[e["target"]] = d
    first_in = {k: v for k, v in first_in.items() if (v - start).days >= sc.pass_through_warmup_days}  # type: ignore[operator]
    ins_by, outs_by = defaultdict(list), defaultdict(list)
    for e in g.money():
        if e["self_loop"]:
            continue
        for t in e["evidence"]["txn_ids"]:
            ins_by[e["target"]].append(g.txns[t])
            outs_by[e["source"]].append((e["target"], g.txns[t]))
    for nid in sorted(g.nodes):
        ins = sorted(ins_by.get(nid, []), key=lambda t: (t["ts"] or "", t["txn_id"]))
        outs = [
            (d, t) for d, t in outs_by.get(nid, []) if d in first_in
        ]  # payments to newly seen parties only
        if not ins or len(outs) < sc.pass_through_min_new_counterparties:
            continue
        best = None
        for r in ins:
            dr = _dt(r)
            if not dr or not r["amount_minor"]:
                continue
            win = [
                (dst, t)
                for dst, t in outs
                if dst in first_in
                and 0 <= (first_in[dst] - dr).days <= sc.pass_through_window_days
                and _dt(t)
                and 0 <= (_dt(t) - dr).days <= sc.pass_through_window_days
                and t["currency"] == r["currency"]
            ]
            new = sorted({d for d, _ in win})
            total = sum(t["amount_minor"] or 0 for _, t in win)
            ratio = total / r["amount_minor"]
            if (
                len(new) >= sc.pass_through_min_new_counterparties
                and sc.pass_through_ratio_low <= ratio <= sc.pass_through_ratio_high
            ):
                key = (-len(new), abs(1 - ratio), r["txn_id"])
                if best is None or key < best[0]:
                    best = (key, r, win, new, ratio)
        if best:
            _, r, win, new, ratio = best
            out.append(
                _signal(
                    "graph_pass_through",
                    "entity",
                    nid,
                    [nid, *new],
                    [r["txn_id"], *(t["txn_id"] for _, t in win)],
                    "high",
                    {
                        "new_counterparties": len(new),
                        "out_to_in_ratio": round(ratio, 6),
                        "window_days": sc.pass_through_window_days,
                    },
                    {"receipt": r, "payments": [t for _, t in win], "new_counterparties": new},
                    f"{g.nodes[nid]['label']} received a payment and within {sc.pass_through_window_days} "
                    "days paid "
                    f"{ratio:.0%} of it on to {len(new)} counterparties first seen in that window.",
                    ["a company paying new suppliers after a receipt can be ordinary (new projects)"],
                )
            )
    for f in ev:
        if f["classification"] != "possible_conflict":
            continue
        out.append(
            _signal(
                "graph_employee_vendor_identifier",
                "group",
                f["pair_id"],
                [f["employee"]["node_id"], f["organisation"]["node_id"]],
                f["payments_to_organisation"]["txn_ids"],
                "high",
                {"shared": sorted(f["shared"]), "payments": f["payments_to_organisation"]["transactions"]},
                {"finding_id": f["finding_id"], "shared": f["shared"]},
                f["explanation"],
                [f["not_concluded"]],
            )
        )
    strong = [e for e in g.edges if e["type"] in ("shares_bank_account", "shares_tax_id")]
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for e in strong:
        if "employee" in (g.nodes[e["source"]]["node_type"], g.nodes[e["target"]]["node_type"]):
            continue
        ra, rb = find(e["source"]), find(e["target"])
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    clusters = defaultdict(list)
    for x in parent:
        clusters[find(x)].append(x)
    for _root, members in sorted(clusters.items()):
        if len(members) < sc.shared_identifier_cluster_min:
            continue
        members = sorted(members)
        es = [e for e in strong if e["source"] in members and e["target"] in members]
        txn_ids = [t for e in g.money() if e["target"] in members for t in e["evidence"]["txn_ids"]]
        out.append(
            _signal(
                "graph_shared_identifier_cluster",
                "group",
                "|".join(members),
                members,
                txn_ids,
                "high",
                {"entities": len(members), "links": len(es)},
                {"edges": [e["edge_id"] for e in es], "labels": [g.nodes[m]["label"] for m in members]},
                f"{len(members)} distinct entities are linked by shared bank accounts or tax IDs: "
                + ", ".join(g.nodes[m]["label"] for m in members)
                + ".",
                ["shared identifiers are links, not proof that the entities are one party"],
            )
        )
    per = metrics["nodes"]
    hp = cfg.centrality.high_percentile
    for nid in sorted(per):
        m = per[nid]
        if (
            g.nodes[nid]["node_type"] in GROUP_TYPES
            or not m["betweenness"]
            or (m["betweenness_percentile"] or 0) < hp
        ):
            continue
        out.append(
            _signal(
                "graph_high_betweenness",
                "entity",
                nid,
                [nid],
                [],
                "low",
                {
                    "betweenness": m["betweenness"],
                    "betweenness_percentile": m["betweenness_percentile"],
                    "pagerank_percentile": m["pagerank_percentile"],
                },
                {"in_degree": m["in_degree"], "out_degree": m["out_degree"]},
                f"{g.nodes[nid]['label']} lies on many shortest payment paths between other parties "
                f"(betweenness percentile {m['betweenness_percentile']:.2f} among "
                f"{g.nodes[nid]['node_type']} nodes).",
                ["context only: high centrality is not evidence of fraud"],
            )
        )
    return sorted(out, key=lambda s: (s["rule_id"], s["signal_id"]))
