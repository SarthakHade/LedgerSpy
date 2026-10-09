"""Graph construction: nodes are P12 canonical entities, edges are evidence (docs/relationship-graph.md).

Nodes. One node per P12 canonical entity (`CE-…`): its ledger names and master records, merged ONLY by
confirmed matches. Probable, ambiguous and conflicting candidates stay separate nodes, linked by a
`possible_same_entity` edge. A node's type comes from its master records (group_company, employee, vendor,
customer) or, for a ledger name without master data, its ledger roles (payer, payee, payer_payee); PaySim
codes are `account` nodes.

Edges (stable ID = "G-" + 16 hex of SHA-256 of type and endpoints):
- `paid` (directed, money): payer → payee, one edge per (payer, payee, currency) aggregating every canonical
  row between them; every transaction ID is kept. Currencies are never combined (no conversion source).
- attribute edges (undirected, from P12 pair evidence between DIFFERENT canonical entities):
  `shares_bank_account`, `shares_tax_id`, `shares_contact` (phone / e-mail; a widely shared contact is
  marked generic), `shares_address`, `possible_same_entity` (a probable, ambiguous or conflicting P12 pair)
  and `potential_conflict_of_interest` (an employee and an organisation sharing a bank account or tax ID). A
  shared attribute is a link, never an identity merge.
Not available (no source evidence is ingested): `employed_by`, `owns_or_controls`, bank-counterparty nodes
(P14). Nothing is inferred from names, addresses or e-mail domains alone.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ledgerspy.hashing import canonical_json, sha256_hex
from ledgerspy.resolution.engine import Resolution
from ledgerspy.resolution.records import ledger_key

GRAPH_VERSION = 1
ATTRIBUTE_TYPES = (
    "shares_bank_account",
    "shares_tax_id",
    "shares_contact",
    "shares_address",
    "possible_same_entity",
    "potential_conflict_of_interest",
)
EDGE_TYPES = ("paid", *ATTRIBUTE_TYPES)
UNAVAILABLE_EDGE_TYPES = {
    "employed_by": "no employer field is mapped in the employee master; employment is never inferred",
    "owns_or_controls": "no ownership / control register is ingested; ownership is never inferred from "
    "shared "
    "names, addresses or contacts",
    "received": "represented by the direction of `paid` edges (payer → payee); not duplicated",
    "resolved_as_same_entity": "confirmed matches are one node: its members and merge evidence are in the "
    "node "
    "detail (not a separate edge)",
    "bank_counterparty": "bank statements are not ingested yet (P14)",
}
_TYPE_PRIORITY = ("group_company", "employee", "vendor", "customer")


def edge_id(kind: str, a: str, b: str, extra: str = "") -> str:
    return "G-" + sha256_hex(canonical_json([kind, a, b, extra]))[:16]


@dataclass
class Graph:
    nodes: dict[str, dict[str, Any]]
    edges: list[dict[str, Any]]
    txns: dict[str, dict[str, Any]]  # txn_id → {ts, date, amount_minor, currency, source_row, edge_id}
    stats: dict[str, Any]
    window: dict[str, str | None]
    sha256: str = ""
    by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    incident: dict[str, list[str]] = field(default_factory=dict)  # node → edge IDs

    def money(self) -> list[dict[str, Any]]:
        return [e for e in self.edges if e["type"] == "paid"]


def _node_type(types: set[str], roles: set[str], cls: str) -> str:
    if cls == "account_code":
        return "account"
    master = [t for t in _TYPE_PRIORITY if t in types]
    if {"vendor", "customer"} <= set(master) and master[0] in ("vendor", "customer"):
        return "vendor_customer"
    if master:
        return master[0]
    if {"payer", "payee"} <= roles:
        return "payer_payee"
    return "payee" if "payee" in roles else "payer" if "payer" in roles else "unknown"


def build(
    view_rows: list[dict[str, Any]],
    feature_rows: list,
    res: Resolution,
    window: tuple[date | None, date | None] = (None, None),
) -> Graph:
    lo, hi = window
    excluded = Counter()
    raw_names: dict[str, Counter] = defaultdict(Counter)
    money: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    txns: dict[str, dict[str, Any]] = {}
    for fr, vr in zip(feature_rows, view_rows, strict=True):
        d = fr.ts.date() if fr.ts else None
        if lo or hi:
            if d is None:
                excluded["no_timestamp_outside_window"] += 1
                continue
            if (lo and d < lo) or (hi and d > hi):
                excluded["outside_window"] += 1
                continue
        for key, raw in ((fr.payer, vr.get("payer_raw")), (fr.payee, vr.get("payee_raw"))):
            if key and raw:
                raw_names[key][raw] += 1
        if not fr.payer or not fr.payee:
            excluded["missing_payer_or_payee"] += 1
            continue
        src, dst = res.entity_of[ledger_key(fr.payer)], res.entity_of[ledger_key(fr.payee)]
        cur = vr.get("currency") or "unknown"
        money[(src, dst, cur)].append(fr.txn_id)
        txns[fr.txn_id] = {
            "txn_id": fr.txn_id,
            "source_row": fr.source_row,
            "ts": fr.ts.isoformat() if fr.ts else None,
            "date": d.isoformat() if d else None,
            "amount_minor": None if fr.amount is None else abs(fr.amount),
            "currency": None if cur == "unknown" else cur,
            "payer": vr.get("payer_raw"),
            "payee": vr.get("payee_raw"),
        }

    # Nodes: every canonical entity that has a ledger name in the window or a master record.
    roles: dict[str, set[str]] = defaultdict(set)
    for src, dst, _ in money:
        roles[src].add("payer")
        roles[dst].add("payee")
    nodes: dict[str, dict[str, Any]] = {}
    for e in res.entities:
        cid = e["canonical_entity_id"]
        members = [res.records[m] for m in e["members"]]
        ledger_members = [m for m in members if m.source == "ledger"]
        if not e["master_records"] and cid not in roles:
            continue  # a ledger name outside the window
        spellings = Counter()
        for m in ledger_members:
            spellings.update(raw_names.get(m.key[2:], {}))
        master_names = [m.name.raw for m in members if m.source == "master" and m.name.raw]
        label = (
            sorted(master_names)[0]
            if e["anchor"].startswith("m:") and master_names
            else (
                sorted(spellings.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
                if spellings
                else e["display_name"]
            )
        )
        nodes[cid] = {
            "node_id": cid,
            "label": label,
            "node_type": _node_type(set(e["entity_types"]), roles.get(cid, set()), e["entity_class"]),
            "entity_types": e["entity_types"],
            "entity_class": e["entity_class"],
            "resolution_status": e["resolution_status"],
            "members": len(e["members"]),
            "master_records": e["master_records"],
            "ledger_names": sorted(m.key[2:] for m in ledger_members),
            "roles": sorted(roles.get(cid, set())),
            "has_master_record": e["master_records"] > 0,
        }

    edges: list[dict[str, Any]] = []
    for (src, dst, cur), ids in sorted(money.items()):
        ids = sorted(ids, key=lambda t: (txns[t]["ts"] or "", t))
        amounts = [txns[t]["amount_minor"] for t in ids]
        known = [a for a in amounts if a is not None]
        dates = [txns[t]["date"] for t in ids if txns[t]["date"]]
        eid = edge_id("paid", src, dst, cur)
        for t in ids:
            txns[t]["edge_id"] = eid
        lim = []
        if len(known) < len(amounts):
            lim.append(f"{len(amounts) - len(known)} transaction(s) without an amount")
        if len(dates) < len(ids):
            lim.append(f"{len(ids) - len(dates)} transaction(s) without a date")
        if cur == "unknown":
            lim.append("currency not mapped")
        edges.append(
            {
                "edge_id": eid,
                "type": "paid",
                "source": src,
                "target": dst,
                "directed": True,
                "currency": None if cur == "unknown" else cur,
                "transactions": len(ids),
                "amount_minor": sum(known) if known else None,
                "first_date": min(dates) if dates else None,
                "last_date": max(dates) if dates else None,
                "self_loop": src == dst,
                "status": "observed",
                "evidence": {"source": "canonical ledger rows (payer → payee)", "txn_ids": ids},
                "limitations": lim,
            }
        )

    # Attribute edges from P12 pair evidence between different canonical entities.
    attr: dict[tuple[str, str, str], dict[str, Any]] = {}

    def add(kind: str, a: str, b: str, pair: dict[str, Any], detail: dict[str, Any]) -> None:
        x, y = sorted((a, b))
        k = (kind, x, y)
        e = attr.setdefault(
            k,
            {
                "edge_id": edge_id(kind, x, y),
                "type": kind,
                "source": x,
                "target": y,
                "directed": False,
                "statuses": set(),
                "pairs": [],
                "details": [],
            },
        )
        e["statuses"].add(detail.get("status") or pair["status"])
        e["pairs"].append(pair["pair_id"])
        e["details"].append(detail)

    for pid in sorted(res.pairs):
        p = res.pairs[pid]
        a, b = res.entity_of[p["a"]], res.entity_of[p["b"]]
        if a == b or a not in nodes or b not in nodes:
            continue
        ids = p["identifiers"]
        ra, rb = res.records[p["a"]], res.records[p["b"]]
        base = {"pair_id": pid, "records": [p["a"], p["b"]], "pair_status": p["status"]}
        if ids["bank_account"] == "match":
            add(
                "shares_bank_account",
                a,
                b,
                p,
                base | {"value": ra.norm.get("bank_account"), "status": "identifier match"},
            )
        if ids["tax_id"] == "match":
            add(
                "shares_tax_id",
                a,
                b,
                p,
                base | {"value": ra.norm.get("tax_id"), "status": "identifier match"},
            )
        for f in ("phone", "email"):
            if ids[f] in ("match", "shared_contact"):
                add(
                    "shares_contact",
                    a,
                    b,
                    p,
                    base
                    | {
                        "field": f,
                        "value": ra.norm.get(f),
                        "status": "generic shared contact" if ids[f] == "shared_contact" else "contact match",
                    },
                )
        if p["address"] and p["address"]["same_address"]:
            add("shares_address", a, b, p, base | {"value": ra.raw.get("address"), "status": "same address"})
        if p["status"] in ("probable_match", "ambiguous", "conflicting_identity"):
            add(
                "possible_same_entity",
                a,
                b,
                p,
                base
                | {
                    "status": p["status"],
                    "name_similarity": p["name"]["name_similarity"],
                    "conflicts": p["conflicts"],
                },
            )
        classes = {ra.entity_class, rb.entity_class}
        if classes == {"person", "organisation"} and "match" in (ids["bank_account"], ids["tax_id"]):
            fields = [f for f in ("bank_account", "tax_id") if ids[f] == "match"]
            add(
                "potential_conflict_of_interest",
                a,
                b,
                p,
                base | {"status": "possible conflict (shared strong identifier)", "shared": fields},
            )
    for k in sorted(attr):
        e = attr[k]
        e["status"] = sorted(e.pop("statuses"))
        e["evidence"] = {
            "source": "P12 entity-resolution pair evidence",
            "pairs": e.pop("pairs"),
            "details": e.pop("details"),
        }
        e["limitations"] = ["a shared attribute is a link between distinct entities, never an identity merge"]
        if e["type"] == "possible_same_entity":
            e["limitations"].append("needs review in P12: not merged")
        edges.append(e)

    edges.sort(
        key=lambda e: (
            EDGE_TYPES.index(e["type"]),
            e["source"],
            e["target"],
            e.get("currency") or "",
            e["edge_id"],
        )
    )
    incident: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        incident[e["source"]].append(e["edge_id"])
        if e["target"] != e["source"]:
            incident[e["target"]].append(e["edge_id"])
    currencies = sorted({e["currency"] or "unknown" for e in edges if e["type"] == "paid"})
    stats = {
        "nodes": len(nodes),
        "nodes_by_type": dict(sorted(Counter(n["node_type"] for n in nodes.values()).items())),
        "edges": len(edges),
        "edges_by_type": {t: sum(1 for e in edges if e["type"] == t) for t in EDGE_TYPES},
        "transactions_in_graph": len(txns),
        "excluded_rows": dict(sorted(excluded.items())),
        "self_loops": sum(1 for e in edges if e.get("self_loop")),
        "currencies": currencies,
    }
    win = {"from": lo.isoformat() if lo else None, "to": hi.isoformat() if hi else None}
    g = Graph(nodes, edges, txns, stats, win)
    g.sha256 = sha256_hex(
        canonical_json(
            {
                "version": GRAPH_VERSION,
                "window": win,
                "nodes": [
                    [n["node_id"], n["node_type"], n["members"]]
                    for n in sorted(nodes.values(), key=lambda n: n["node_id"])
                ],
                "edges": [
                    [
                        e["edge_id"],
                        e["type"],
                        e["source"],
                        e["target"],
                        e.get("amount_minor"),
                        e["evidence"].get("txn_ids") or e["evidence"]["pairs"],
                    ]
                    for e in edges
                ],
            }
        )
    )
    g.by_id = {e["edge_id"]: e for e in edges}
    g.incident = dict(incident)
    return g
