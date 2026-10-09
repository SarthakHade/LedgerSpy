"""P13 units: graph construction, cycles, metrics, relationships, determinism and safety."""

import random
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from ledgerspy.config import load_config
from ledgerspy.features.resolution import resolution_of
from ledgerspy.graph import cycles, metrics, model, signals
from ledgerspy.resolution.records import MasterRecord
from tests.backend.features_support import make, rec

ROOT = Path(__file__).resolve().parents[2]
GCFG = load_config().settings.graph
T0 = datetime(2024, 1, 1, 10, 0)


def row(
    n: int,
    payer: str | None,
    payee: str | None,
    day: float,
    amount: int | None = 100_000,
    currency: str = "INR",
):
    r = rec(n, payer=payer, payee=payee, at=T0 + timedelta(days=day), amount=amount)
    r["currency"] = currency
    return r


def graph(records, masters: tuple = (), window=(None, None)):
    inp = make(records, masters=masters)
    res = resolution_of(inp)
    return model.build(records, inp.rows, res, window), res


def node_of(g, res, name):
    from ledgerspy.features.data import party_key

    return res.entity_of["l:" + party_key(name)]


def ring(scale=1.0, gap=3, extra=()):
    """A pays B, B pays C, C pays A: time-ordered, value retained."""
    return [
        row(1, "Alpha Pvt Ltd", "Beta Traders", 0, 1_000_000),
        row(2, "Beta Traders", "Gamma Agencies", gap, round(970_000 * scale)),
        row(3, "Gamma Agencies", "Alpha Pvt Ltd", 2 * gap, round(950_000 * scale)),
        *extra,
    ]


# ------------------------------------------------------------------------------------------ construction


def test_money_edges_follow_payer_to_payee_and_aggregate_with_traceability():
    recs = [
        row(1, "Alpha Pvt Ltd", "Beta Traders", 0, 100),
        row(2, "Alpha Pvt Ltd", "Beta Traders", 5, 250),
        row(3, "Beta Traders", "Alpha Pvt Ltd", 9, 40),
    ]
    g, res = graph(recs)
    a, b = node_of(g, res, "Alpha Pvt Ltd"), node_of(g, res, "Beta Traders")
    ab = next(e for e in g.money() if e["source"] == a and e["target"] == b)
    assert (
        ab["transactions"] == 2
        and ab["amount_minor"] == 350
        and ab["evidence"]["txn_ids"] == ["T00001", "T00002"]
    )
    assert (ab["first_date"], ab["last_date"]) == ("2024-01-01", "2024-01-06") and ab["directed"]
    ba = next(e for e in g.money() if e["source"] == b)
    assert ba["target"] == a and ba["transactions"] == 1 and ab["edge_id"] != ba["edge_id"]
    assert re.fullmatch(r"G-[0-9a-f]{16}", ab["edge_id"]) and g.txns["T00001"]["edge_id"] == ab["edge_id"]


def test_canonical_identities_are_respected_and_ambiguous_names_stay_apart():
    recs = [
        row(1, "Alpha Pvt Ltd", "Javrel Office Supplies Pvt Ltd", 0),
        row(2, "Alpha Pvt Ltd", "Javrel Office Supplies Private Limited", 1),
        row(3, "Alpha Pvt Ltd", "Kesrin Housekeeping Pvt Ltd", 2),
        row(4, "Alpha Pvt Ltd", "Kesrinn Housekeeping Pvt Ltd", 3),
    ]
    g, res = graph(recs)
    j1, j2 = (
        node_of(g, res, "Javrel Office Supplies Pvt Ltd"),
        node_of(g, res, "Javrel Office Supplies Private Limited"),
    )
    assert j1 == j2 and g.nodes[j1]["members"] == 2  # confirmed match: one node
    k1, k2 = node_of(g, res, "Kesrin Housekeeping Pvt Ltd"), node_of(g, res, "Kesrinn Housekeeping Pvt Ltd")
    assert k1 != k2  # a name-only near match is never merged
    links = [e for e in g.edges if e["type"] == "possible_same_entity"]
    assert len(links) == 1 and {links[0]["source"], links[0]["target"]} == {k1, k2}
    assert "needs review in P12: not merged" in links[0]["limitations"]
    assert len([e for e in g.money() if e["target"] == j1]) == 1  # both spellings' payments on one edge


def test_currencies_are_never_combined():
    recs = [
        row(1, "Alpha Pvt Ltd", "Beta Traders", 0, 100, "INR"),
        row(2, "Alpha Pvt Ltd", "Beta Traders", 1, 7, "USD"),
    ]
    g, res = graph(recs)
    es = g.money()
    assert sorted(e["currency"] for e in es) == ["INR", "USD"] and g.stats["currencies"] == ["INR", "USD"]
    m = metrics.compute(g, GCFG.centrality)
    a = node_of(g, res, "Alpha Pvt Ltd")
    assert m["nodes"][a]["outflow"] == {"INR": 100, "USD": 7}
    assert m["nodes"][a]["concentration_out"] is None  # several currencies: not computed
    assert m["graph"]["pagerank"]["weights"].startswith("transaction count")


def test_missing_data_is_counted_not_guessed():
    recs = [
        row(1, "Alpha Pvt Ltd", None, 0),
        row(2, "Alpha Pvt Ltd", "Beta Traders", 1, None),
        row(3, "Alpha Pvt Ltd", "Beta Traders", 2, 50),
    ]
    g, _ = graph(recs)
    assert g.stats["excluded_rows"] == {"missing_payer_or_payee": 1}
    (e,) = g.money()
    assert (
        e["amount_minor"] == 50
        and e["transactions"] == 2
        and "1 transaction(s) without an amount" in e["limitations"]
    )


def test_graph_is_deterministic_and_independent_of_row_order():
    recs = ring(extra=[row(4, "Alpha Pvt Ltd", "Delta Steel", 4), row(5, "Delta Steel", "Beta Traders", 8)])
    g1, _ = graph(recs)
    g2, _ = graph(recs)
    shuffled = recs[:]
    random.Random(4).shuffle(shuffled)  # noqa: S311
    g3, _ = graph(shuffled)
    assert g1.sha256 == g2.sha256 == g3.sha256
    assert [e["edge_id"] for e in g1.edges] == [e["edge_id"] for e in g3.edges]
    c1, c3 = cycles.detect(g1, GCFG.cycles), cycles.detect(g3, GCFG.cycles)
    assert c1 == c3
    snapshot = [dict(r) for r in recs]
    graph(recs)
    assert recs == snapshot  # source rows are never modified


def test_time_window_filters_rows_and_reports_it():
    g, _ = graph(ring(), window=(date(2024, 1, 2), None))
    assert g.stats["excluded_rows"] == {"outside_window": 1} and g.window == {
        "from": "2024-01-02",
        "to": None,
    }


# ------------------------------------------------------------------------------------------ cycles


def test_known_cycle_is_found_with_its_evidence():
    g, res = graph(ring())
    out = cycles.detect(g, GCFG.cycles)
    (c,) = out["cycles"]
    assert c["status"] == "review" and c["length"] == 3 and c["span_days"] == 6
    assert [t["txn_id"] for t in c["best_chain"]] == ["T00001", "T00002", "T00003"]
    assert c["value_retention"] == 0.95 and c["reasons_for_review"]
    assert set(c["path"]) == {node_of(g, res, n) for n in ("Alpha Pvt Ltd", "Beta Traders", "Gamma Agencies")}
    assert not out["summary"]["truncated"]


def test_chain_may_start_anywhere_in_the_cycle():
    recs = [  # the payment from Gamma happens first
        row(1, "Gamma Agencies", "Alpha Pvt Ltd", 0, 1_000_000),
        row(2, "Alpha Pvt Ltd", "Beta Traders", 2, 990_000),
        row(3, "Beta Traders", "Gamma Agencies", 4, 980_000),
    ]
    (c,) = cycles.detect(graph(recs)[0], GCFG.cycles)["cycles"]
    assert c["status"] == "review" and c["best_chain"][0]["txn_id"] == "T00001"


def test_non_cycle_control_and_two_party_flows():
    chain = [row(1, "Alpha Pvt Ltd", "Beta Traders", 0), row(2, "Beta Traders", "Gamma Agencies", 1)]
    assert cycles.detect(graph(chain)[0], GCFG.cycles)["cycles"] == []
    back = [row(1, "Alpha Pvt Ltd", "Beta Traders", 0), row(2, "Beta Traders", "Alpha Pvt Ltd", 1)]
    assert cycles.detect(graph(back)[0], GCFG.cycles)["cycles"] == []  # length 2 is the reciprocal signal


def test_time_window_and_retention_decide_the_status():
    slow = ring(gap=40)
    (c,) = cycles.detect(graph(slow)[0], GCFG.cycles)["cycles"]
    assert c["status"] == "not_time_ordered" and c["best_chain"] is None and c["reasons_for_review"] == []
    (c,) = cycles.detect(graph(ring(scale=0.3))[0], GCFG.cycles)["cycles"]
    assert c["status"] == "ordered_low_retention"


def test_maximum_cycle_length_is_respected():
    four = [
        row(1, "Alpha Pvt Ltd", "Beta Traders", 0),
        row(2, "Beta Traders", "Gamma Agencies", 1),
        row(3, "Gamma Agencies", "Delta Steel", 2),
        row(4, "Delta Steel", "Alpha Pvt Ltd", 3),
    ]
    g, _ = graph(four)
    assert len(cycles.detect(g, GCFG.cycles)["cycles"]) == 1
    short = GCFG.cycles.model_copy(update={"max_length": 3})
    assert cycles.detect(g, short)["cycles"] == []


def test_repeated_payments_give_several_ordered_chains():
    recs = ring() + [
        row(4, "Alpha Pvt Ltd", "Beta Traders", 20, 500_000),
        row(5, "Beta Traders", "Gamma Agencies", 21, 490_000),
        row(6, "Gamma Agencies", "Alpha Pvt Ltd", 22, 480_000),
    ]
    (c,) = cycles.detect(graph(recs)[0], GCFG.cycles)["cycles"]
    assert c["ordered_chains"] >= 2 and len(c["txn_ids"]) == 6


def test_enumeration_is_deterministic_and_bounded():
    succ = {str(i): [str(j) for j in range(6) if j != i] for i in range(6)}  # complete digraph: many cycles
    cfg = GCFG.cycles.model_copy(update={"max_cycles": 7})
    a, info = cycles.enumerate_cycles(succ, cfg)
    assert len(a) == 7 and info["truncated"] and "maximum of 7 cycles" in info["truncation_reason"]
    assert cycles.enumerate_cycles(succ, cfg)[0] == a
    assert all(c[0] == min(c) for c in a)  # canonical rotation: each cycle once
    tight = GCFG.cycles.model_copy(update={"max_search_steps": 1000})
    big = {str(i): [str(j) for j in range(30) if j != i] for i in range(30)}
    _, info = cycles.enumerate_cycles(big, tight)
    assert info["truncated"] and "search budget" in info["truncation_reason"]


# ------------------------------------------------------------------------------------------ metrics


def test_degree_flow_reciprocity_and_concentration():
    recs = [
        row(1, "Hub Pvt Ltd", "Beta Traders", 0, 300),
        row(2, "Hub Pvt Ltd", "Gamma Agencies", 1, 100),
        row(3, "Beta Traders", "Hub Pvt Ltd", 2, 50),
    ]
    g, res = graph(recs)
    m = metrics.compute(g, GCFG.centrality)["nodes"]
    hub = m[node_of(g, res, "Hub Pvt Ltd")]
    assert (hub["out_degree"], hub["in_degree"], hub["out_txns"], hub["in_txns"]) == (2, 1, 2, 1)
    assert hub["outflow"] == {"INR": 400} and hub["inflow"] == {"INR": 50}
    assert hub["reciprocity"] == 0.5  # pays two, is paid by one of them
    assert hub["concentration_out"] == round(0.75**2 + 0.25**2, 6) and hub["top_payee_share"] == 0.75
    assert m[node_of(g, res, "Gamma Agencies")]["clustering"] is None  # one neighbour


def test_centrality_components_disconnected_and_empty_graphs():
    path = [row(1, "Alpha Pvt Ltd", "Beta Traders", 0), row(2, "Beta Traders", "Gamma Agencies", 1)]
    other = [row(3, "Delta Steel", "Epsilon Foods", 2)]
    g, res = graph(path + other)
    out = metrics.compute(g, GCFG.centrality)
    m = out["nodes"]
    b = node_of(g, res, "Beta Traders")
    assert m[b]["betweenness"] > 0 and all(m[v]["betweenness"] == 0 for v in m if v != b)
    assert abs(sum(x["pagerank"] for x in m.values()) - 1) < 1e-6
    assert out["graph"]["components"] == 2 and out["graph"]["largest_component"] == 3
    assert out["graph"]["pagerank"]["status"] == "converged"
    empty = metrics.compute(graph([row(1, "Alpha Pvt Ltd", None, 0)])[0], GCFG.centrality)
    assert (
        empty["nodes"] == {} and empty["graph"]["density"] is None and empty["graph"]["reciprocity"] is None
    )
    capped = metrics.compute(g, GCFG.centrality.model_copy(update={"betweenness_max_nodes": 2}))
    assert (
        capped["graph"]["betweenness"]["status"] == "unavailable"
        and m
        and all(v["betweenness"] is None for v in capped["nodes"].values())
    )


# ------------------------------------------------------------------------------------------ relationships


def _m(etype, row_, **v):
    return MasterRecord("MF-00000000000000aa", etype, row_, tuple(sorted(v.items())))


def test_employee_vendor_strong_identifier_contact_and_address():
    masters = (
        _m(
            "employee",
            1,
            record_id="E1",
            name="Devika Nair",
            bank_account="BANK00000001",
            phone="+91 22 1111 2222",
            address="Flat 2, Lake Road, Pune 411001",
        ),
        _m("vendor", 2, record_id="V1", name="Wyndel Advisors Pvt Ltd", bank_account="BANK00000001"),
        _m("vendor", 3, record_id="V2", name="Kesrin Housekeeping Pvt Ltd", phone="022 1111 2222"),
        _m(
            "vendor",
            4,
            record_id="V3",
            name="Verrin Electricals & Co",
            address="Flat 2, Lake Road, Pune 411001",
        ),
        _m("vendor", 5, record_id="V4", name="Devika Nair Traders"),  # a similar name only: no evidence
    )
    recs = [
        row(1, "Alpha Pvt Ltd", "Wyndel Advisors Pvt Ltd", 0),
        row(2, "Alpha Pvt Ltd", "Kesrin Housekeeping Pvt Ltd", 1),
    ]
    g, res = graph(recs, masters)
    ev = signals.employee_vendor(g, res)
    cls = {f["organisation"]["name"]: f["classification"] for f in ev}
    assert cls == {
        "Wyndel Advisors Pvt Ltd": "possible_conflict",
        "Kesrin Housekeeping Pvt Ltd": "shared_attribute",
        "Verrin Electricals & Co": "shared_attribute (address only)",
    }
    wy = next(f for f in ev if f["classification"] == "possible_conflict")
    assert wy["payments_to_organisation"]["txn_ids"] == ["T00001"] and "never inferred" in wy["not_concluded"]
    coi = [e for e in g.edges if e["type"] == "potential_conflict_of_interest"]
    assert len(coi) == 1
    assert not any(e["type"] in ("owns_or_controls", "employed_by") for e in g.edges)
    assert "owns_or_controls" in model.UNAVAILABLE_EDGE_TYPES


def test_ghost_vendor_support_is_leads_not_conclusions():
    masters = (
        _m(
            "vendor",
            1,
            record_id="V1",
            name="Wyndel Advisors Pvt Ltd",
            bank_account="BANK00000001",
            tax_id="TAX-00001",
        ),
    )
    recs = [
        row(1, "Alpha Pvt Ltd", "Wyndel Advisors Pvt Ltd", 0),
        row(2, "Alpha Pvt Ltd", "Unknown Services", 1),
    ]
    g, res = graph(recs, masters)
    gh = signals.ghost_vendor_support(g, res)
    items = {i["label"]: i for i in gh["items"]}
    assert [x["indicator"] for x in items["Unknown Services"]["indicators"]] == ["no_master_record"]
    assert {x["indicator"] for x in items["Wyndel Advisors Pvt Ltd"]["indicators"]} == {
        "incomplete_master_record"
    }
    assert all("not a finding" in i["conclusion"] and i["evidence_to_obtain"] for i in gh["items"])
    assert not any(re.search(r"\bghost\b", i["status"]) for i in gh["items"])
    no_masters, res2 = graph(recs)
    assert "cannot be assessed" in signals.ghost_vendor_support(no_masters, res2)["master_data"]


def test_signals_are_traceable_and_not_fused():
    g, res = graph(ring())
    m = metrics.compute(g, GCFG.centrality)
    c = cycles.detect(g, GCFG.cycles)
    sig = signals.compute(g, res, m, c, signals.employee_vendor(g, res), GCFG)
    (s,) = [x for x in sig if x["rule_id"] == "graph_circular_flow"]
    assert s["txn_ids"] == ["T00001", "T00002", "T00003"] and len(s["entity_ids"]) == 3
    assert (
        s["weight"] is None
        and s["fused_in_p9"] is False
        and s["limitations"]
        and s["metrics"]["value_retention"] == 0.95
    )
    assert re.fullmatch(r"S-[0-9a-f]{20}", s["signal_id"])
    assert signals.compute(g, res, m, c, [], GCFG) == sig


# ------------------------------------------------------------------------------------------ safety


def test_graph_reads_no_labels_ground_truth_or_supervised_output_and_p9_never_imports_it():
    for p in (ROOT / "ledgerspy" / "graph").glob("*.py"):
        code = p.read_text(encoding="utf-8")
        assert (
            "ledgerspy.supervised" not in code and "FROM labels" not in code and "ground_truth" not in code
        ), p
    for sub in ("risk", "anomaly", "forensics", "features", "resolution", "supervised"):
        for p in (ROOT / "ledgerspy" / sub).rglob("*.py"):
            assert "ledgerspy.graph" not in p.read_text(encoding="utf-8"), p
