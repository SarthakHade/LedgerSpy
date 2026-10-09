"""P13 on real imports: P5 ledger + masters, PaySim, a large generated graph, P9 isolation, the API.

The P5 ground truth is read only to SCORE graph results (which injected scenarios are found), never as input.
"""

import json
import random
import secrets
import shutil
import time
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ledgerspy.api.app import create_app
from ledgerspy.cases import store
from ledgerspy.cases.db import db_path_for
from ledgerspy.config import load_config
from ledgerspy.graph import cycles, metrics, model, service
from ledgerspy.resolution import masters, validation
from ledgerspy.risk import service as risk_service
from tests.backend.conftest import BASE_URL, PORT, make_ingest
from tests.backend.test_features_datasets import PAYSIM, REFERENCE, case_overrides
from tests.backend.test_forensics_datasets import _table_digest, imported

GT = json.loads((REFERENCE / "ground_truth" / "SYNTHETIC_ground_truth.json").read_text(encoding="utf-8"))
SC = {s["scenario_id"]: s for s in GT["scenarios"]}


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    inputs = tmp_path_factory.mktemp("inputs")
    shutil.copy(REFERENCE / "SYNTHETIC_ledger.csv", inputs / "SYNTHETIC_ledger.csv")
    h = make_ingest(tmp_path_factory.mktemp("ws"))
    pay = imported(h, PAYSIM)
    h.case_id = store.create_case(
        h.conn, name="syn", actor="t", period_start=date(2023, 4, 1), period_end=date(2026, 3, 31)
    )["case_id"]
    syn = imported(h, inputs / "SYNTHETIC_ledger.csv")
    for etype, (fname, mapping) in validation.P5_FILES.items():
        masters.import_master(
            h.conn, h.cfg, h.workspace, h.case_id, REFERENCE / fname, etype, "t", mapping=mapping
        )
    cfg = load_config(
        overrides={"workspace": {"path": str(h.workspace)}, "server": {"port": PORT}, **case_overrides()}
    )
    cfg_pay = load_config(overrides={"workspace": {"path": str(h.workspace)}, "server": {"port": PORT}})
    risk_service.clear_cache()
    p9 = risk_service.run_risk(h.conn, cfg, syn)
    digest = _table_digest(h.conn)
    service.clear_cache()
    r = service.run(h.conn, cfg, syn)
    yield {"h": h, "syn": syn, "pay": pay, "cfg": cfg, "cfg_pay": cfg_pay, "r": r, "p9": p9, "digest": digest}
    h.conn.close()


def _labels(r, ids):
    return [r.graph.nodes[i]["label"] for i in ids]


# ------------------------------------------------------------------------------------------ P5


def test_p5_graph_shape_and_identity(env):
    r = env["r"]
    st = r.graph.stats
    assert st["nodes"] == len(r.graph.nodes) and st["edges_by_type"]["paid"] > 100
    assert st["currencies"] == ["INR"] and st["excluded_rows"] == {}
    assert st["transactions_in_graph"] == 7271
    assert all(re.fullmatch(r"CE-[0-9a-f]{16}", n) for n in r.graph.nodes)
    types = st["nodes_by_type"]
    assert types["group_company"] == 2 and types["employee"] == 37 and types.get("vendor", 0) > 80
    # P12 identities: conflicting look-alikes are separate nodes joined only by a review edge.
    pse = [e for e in r.graph.edges if e["type"] == "possible_same_entity"]
    labels = {frozenset(_labels(r, [e["source"], e["target"]])) for e in pse}
    assert frozenset({"Ulvane Alloys Private Limited", "Ulvanee Alloy Pvt Ltd"}) in labels


def test_circular_trading_rings_are_found_and_ordinary_cycles_are_not_flagged(env):
    r = env["r"]
    review = [c for c in r.cycles["cycles"] if c["status"] == "review"]
    found = {}
    for sid in ("S-O-01", "S-O-02"):
        affected = set(SC[sid]["affected"]["ledger_txn_ids"])  # scoring only
        hits = [c for c in review if {t["txn_id"] for t in c["best_chain"]} <= affected]
        found[sid] = hits
    assert len(found["S-O-01"]) == 1 and found["S-O-01"][0]["length"] == 3
    assert len(found["S-O-02"]) == 1 and found["S-O-02"][0]["length"] == 4
    assert len(review) == 2  # the other structural cycles (ordinary trade, group funding) are not for review
    assert all(c["value_retention"] >= 0.9 and c["span_days"] <= 30 for c in review)
    assert not r.cycles["summary"]["truncated"]


def test_employee_vendor_and_shared_identifier_findings(env):
    r = env["r"]
    ev = {
        (f["employee"]["record"].split(":id:")[1], f["organisation"]["record"].split(":id:")[1]): f[
            "classification"
        ]
        for f in r.employee_vendor
    }
    assert ev[("E0002", "V0078")] == "possible_conflict"  # S-F-01: ghost vendor paid into the buyer's account
    assert (
        ev[("E0018", "V0077")] == "possible_conflict"
    )  # S-M-01: vendor account = AP executive's salary account
    assert ev[("E0032", "V0082")] == "shared_attribute"  # S-N-01: shared phone
    assert ev[("E0013", "V0047")] == "shared_attribute (address only)"  # S-M-02: declared, same address
    sig = {s["rule_id"]: s for s in r.signals}
    cluster = [s for s in r.signals if s["rule_id"] == "graph_shared_identifier_cluster"]
    assert len(cluster) == 1 and set(cluster[0]["evidence"]["labels"]) == {
        "Brightwell Supply Co",
        "Brightwel Supplies",
        "BW Supply House",
    }
    pt = [s for s in r.signals if s["rule_id"] == "graph_pass_through"]
    assert len(pt) == 1 and set(pt[0]["txn_ids"]) <= set(SC["S-P-02"]["affected"]["ledger_txn_ids"])
    assert (
        all(s["fused_in_p9"] is False and s["weight"] is None for s in r.signals)
        and "graph_circular_flow" in sig
    )


def test_ghost_vendor_support_never_concludes(env):
    g = env["r"].ghost
    assert g["master_data"] == "vendor master imported" and g["items"]
    labels = {i["label"]: {x["indicator"] for x in i["indicators"]} for i in g["items"]}
    assert "shares_strong_identifier_with_employee" in labels["Wyndel Advisors Private Limited"]
    assert "no_master_record" in labels["Central Tax Payment Gateway"]
    assert all(i["status"] == "indicators_present" and "not a finding" in i["conclusion"] for i in g["items"])


def test_metrics_on_p5(env):
    m = env["r"].metrics
    assert m["graph"]["pagerank"]["status"] == "converged" and m["graph"]["betweenness"]["status"].startswith(
        "computed"
    )
    lvc = next(n for n, x in env["r"].graph.nodes.items() if x["label"] == "Larkspur Vale Components Pvt Ltd")
    assert m["nodes"][lvc]["pagerank_percentile"] == 1.0  # the audited company is the hub by construction
    assert abs(sum(x["pagerank"] for x in m["nodes"].values()) - 1) < 1e-6


def test_determinism_isolation_and_no_mutation(env):
    h, cfg = env["h"], env["cfg"]
    service.clear_cache()
    again = service.run(h.conn, cfg, env["syn"])
    assert again.graph.sha256 == env["r"].graph.sha256 and again.signals == env["r"].signals
    assert again.cycles == env["r"].cycles
    assert _table_digest(h.conn) == env["digest"]
    risk_service.clear_cache()
    assert (
        risk_service.run_risk(h.conn, cfg, env["syn"]).result_sha256 == env["p9"].result_sha256
    )  # P9 unchanged


def test_time_window(env):
    h, cfg = env["h"], env["cfg"]
    w = service.run(h.conn, cfg, env["syn"], date(2024, 7, 5), date(2024, 12, 31))
    assert w.graph.window == {"from": "2024-07-05", "to": "2024-12-31"}
    assert (
        w.graph.stats["transactions_in_graph"] < 7271 and w.graph.stats["excluded_rows"]["outside_window"] > 0
    )
    s_o_01 = set(SC["S-O-01"]["affected"]["ledger_txn_ids"])
    assert not any({t["txn_id"] for t in (c["best_chain"] or [])} & s_o_01 for c in w.cycles["cycles"])


# ------------------------------------------------------------------------------------------ PaySim and scale


def test_paysim_graph_and_explicit_limits(env):
    t = time.perf_counter()
    r = service.run(env["h"].conn, env["cfg_pay"], env["pay"])
    assert time.perf_counter() - t < 120
    assert (
        r.graph.stats["nodes_by_type"] == {"account": r.graph.stats["nodes"]}
        and r.graph.stats["nodes"] > 10_000
    )
    assert (
        r.metrics["graph"]["betweenness"]["status"] == "unavailable"
        and "never sampled" in r.metrics["graph"]["betweenness"]["reason"]
    )
    assert r.employee_vendor == [] and "cannot be assessed" in r.ghost["master_data"]
    v = service.visualization(r, 50, 100, None)
    assert v["truncated"]["nodes"] and len(v["nodes"]) == 50 and v["truncated"]["nodes_total"] > 50


def test_large_generated_graph_is_bounded():
    rng = random.Random(13)  # noqa: S311
    t0 = datetime(2024, 1, 1)

    class R:
        __slots__ = ("txn_id", "source_row", "ts", "amount", "payer", "payee")

    n_nodes, n_rows = 3000, 20000
    frows, vrows = [], []
    for i in range(n_rows):
        x = R()
        x.txn_id, x.source_row = f"T{i:06d}", i + 1
        x.ts, x.amount = t0 + timedelta(hours=i), rng.randrange(1000, 10**7)
        x.payer, x.payee = f"party {rng.randrange(n_nodes)}", f"party {rng.randrange(n_nodes)}"
        frows.append(x)
        vrows.append({"payer_raw": x.payer, "payee_raw": x.payee, "currency": "INR"})

    class Res:  # identity resolution: every name its own entity
        def __init__(self, keys):
            self.entity_of = {"l:" + k: "CE-" + format(i, "016x") for i, k in enumerate(sorted(keys))}
            self.entities = [
                {
                    "canonical_entity_id": v,
                    "members": [k],
                    "master_records": 0,
                    "anchor": k,
                    "display_name": k[2:],
                    "entity_types": ["ledger_party"],
                    "entity_class": "unknown",
                    "resolution_status": "unique",
                }
                for k, v in self.entity_of.items()
            ]
            self.records, self.pairs = {}, {}

    keys = {r.payer for r in frows} | {r.payee for r in frows}
    res = Res(keys)
    for k in res.entity_of:
        res.records[k] = type("Rec", (), {"source": "ledger", "key": k, "name": None})()
    cfg = load_config().settings.graph
    t = time.perf_counter()
    g = model.build(vrows, frows, res)
    m = metrics.compute(g, cfg.centrality)
    small = cfg.cycles.model_copy(update={"max_cycles": 100})
    c = cycles.detect(g, small)
    assert time.perf_counter() - t < 120
    assert g.stats["nodes"] == len(keys) and len(g.money()) > 15000
    assert c["summary"]["truncated"] and c["summary"]["cycles"] == 100  # explicit, never silent
    assert (
        m["graph"]["betweenness"]["status"].startswith("computed")
        or m["graph"]["betweenness"]["status"] == "unavailable"
    )


# ------------------------------------------------------------------------------------------ API


@pytest.fixture(scope="module")
def api(env):
    token = secrets.token_urlsafe(32)
    with TestClient(create_app(env["cfg"], db_path_for(env["cfg"].settings), token), base_url=BASE_URL) as c:
        yield c, {"Authorization": f"Bearer {token}"}, f"/api/datasets/{env['syn']}/graph"


ENDPOINTS = [
    "",
    "/nodes",
    "/nodes/CE-0000000000000000",
    "/edges",
    "/edges/G-0000000000000000",
    "/cycles",
    "/metrics",
    "/employee-vendor",
    "/ghost-vendors",
    "/signals",
    "/visualization",
]


def test_graph_endpoints_require_the_token(api):
    c, _, base = api
    for p in ENDPOINTS:
        assert c.get(base + p).status_code == 401, p
        assert c.get(base + p, headers={"Authorization": "Bearer wrong"}).status_code == 401, p


def test_graph_api_pages_limits_and_details(api, env):
    c, auth, base = api
    s = c.get(base, headers=auth).json()
    assert s["graph_sha256"] == env["r"].graph.sha256 and s["signals"]["fused_in_p9"] is False
    assert "owns_or_controls" in s["unavailable_edge_types"]
    p1 = c.get(base + "/nodes?size=20", headers=auth).json()
    p2 = c.get(base + "/nodes?size=20&page=2", headers=auth).json()
    assert p1["total"] == s["stats"]["nodes"] and len(p1["items"]) == 20
    assert not {n["node_id"] for n in p1["items"]} & {n["node_id"] for n in p2["items"]}
    assert (
        c.get(base + "/nodes?size=501", headers=auth).status_code == 422
    )  # above graph.limits.max_page_size
    assert c.get(base + "/nodes?size=5001", headers=auth).status_code == 422
    node = p1["items"][0]["node_id"]
    d = c.get(base + f"/nodes/{node}", headers=auth).json()
    assert d["neighbourhood"]["edges_total"] >= len(d["neighbourhood"]["edges"]) and d["metrics"]
    paid = c.get(base + "/edges?type=paid&size=1", headers=auth).json()["items"][0]
    ed = c.get(base + f"/edges/{paid['edge_id']}", headers=auth).json()
    assert (
        len(ed["transactions_detail"]) == min(ed["transactions"], 500)
        and ed["transactions_detail"][0]["txn_id"]
    )
    assert c.get(base + "/nodes/CE-0000000000000000", headers=auth).status_code == 404
    assert c.get(base + "/nodes/not-a-node", headers=auth).status_code == 422
    assert c.get(base + "/edges?type=bogus", headers=auth).status_code == 422
    cyc = c.get(base + "/cycles?status=review", headers=auth).json()
    assert cyc["total"] == 2 and all(x["best_chain"] for x in cyc["items"])
    v = c.get(base + "/visualization?max_nodes=10&edge_types=paid", headers=auth).json()
    assert len(v["nodes"]) == 10 and v["truncated"]["nodes"] and {e["type"] for e in v["edges"]} <= {"paid"}
    assert all("x" not in n and "y" not in n for n in v["nodes"])  # no invented coordinates
    assert c.get(base + "/visualization?max_nodes=100000", headers=auth).status_code == 422
    assert c.get(base + "?from=2025-01-01&to=2024-01-01", headers=auth).status_code == 422
    assert c.get(base + "?from=not-a-date", headers=auth).status_code == 422
    ev = c.get(base + "/employee-vendor", headers=auth).json()
    assert ev["total"] == len(env["r"].employee_vendor)
    sig = c.get(base + "/signals", headers=auth).json()
    assert sig["fused_in_p9"] is False and sig["total"] == len(env["r"].signals)


import re  # noqa: E402 — used by the shape test above
