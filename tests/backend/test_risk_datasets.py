"""P9 on real imports and over the API: synthetic validation (recorded baseline), PaySim, isolation, caching,
determinism and the risk endpoints.

Ground truth and PaySim labels are used here only to evaluate results after scoring.
"""

import csv
import io
import json
import secrets
import shutil
import socket
from datetime import date

import pytest
from fastapi.testclient import TestClient

from ledgerspy.api.app import create_app
from ledgerspy.cases import store
from ledgerspy.cases.db import db_path_for, init_db
from ledgerspy.config import load_config
from ledgerspy.normalize.view import load_view
from ledgerspy.quality.engine import run_integrity
from ledgerspy.risk import calibration, service
from ledgerspy.risk.models import TIER_RANK
from tests.backend.conftest import BASE_URL, PORT, make_ingest
from tests.backend.test_anomaly_datasets import P8_TARGETS
from tests.backend.test_features_datasets import PAYSIM, REFERENCE, case_overrides
from tests.backend.test_forensics_datasets import _table_digest, imported

GT = json.loads((REFERENCE / "ground_truth" / "SYNTHETIC_ground_truth.json").read_text(encoding="utf-8"))
SC = {s["scenario_id"]: s for s in GT["scenarios"]}
INJECTED = {  # scenarios with ledger rows (bank-only and master-data-only scenarios have none)
    s: set(v["affected"]["ledger_txn_ids"])
    for s, v in SC.items()
    if not v["is_control"] and v["affected"]["ledger_txn_ids"]
}
CONTROLS = {s: set(v["affected"]["ledger_txn_ids"]) for s, v in SC.items() if v["is_control"]}
INJECTED_ROWS = set().union(*INJECTED.values())
# Recorded baseline (docs/p9-acceptance.md): injected scenarios with ≥ 1 medium-or-higher row.
BASELINE_SEEN = {
    "S-A-01",
    "S-A-02",
    "S-B-01",
    "S-B-02",
    "S-C-01",
    "S-C-02",
    "S-D-01",
    "S-D-02",
    "S-F-01",
    "S-G-01",
    "S-G-02",
    "S-H-01",
    "S-H-02",
    "S-H-03",
    "S-I-01",
    "S-J-01",
    "S-J-02",
    "S-K-01",
    "S-K-02",
    "S-K-03",
    "S-M-01",
    "S-N-01",
    "S-O-01",
    "S-O-02",
    "S-P-01",
    "S-P-02",
    "S-Q-01",
    "S-X-01",
}
# Scenarios no P7/P8 detector can see per row (they need P12–P14 or are aggregates / data quality by design).
OUT_OF_SCOPE = {
    "S-E-01",
    "S-E-02",
    "S-R-01",
    "S-S-01",
    "S-S-03",
    "S-S-04",
    "S-S-05",
    "S-S-06",
    "S-S-07",
    "S-T-01",
    "S-V-01",
    "S-W-01",
}
# Look-alike controls that must never reach high.
QUIET_CONTROLS = [
    "S-M-02",
    "S-N-04",
    "S-U-01",
    "S-U-02",
    "S-U-04",
    "S-U-05",
    "S-U-06",
    "S-U-07",
    "S-U-09",
    "S-U-10",
    "S-U-12",
    "S-U-13",
]


@pytest.fixture(scope="module")
def synthetic(tmp_path_factory):
    inputs = tmp_path_factory.mktemp("inputs")
    shutil.copy(REFERENCE / "SYNTHETIC_ledger.csv", inputs / "SYNTHETIC_ledger.csv")
    h = make_ingest(tmp_path_factory.mktemp("ws"))
    h.case_id = store.create_case(
        h.conn, name="syn", actor="t", period_start=date(2023, 4, 1), period_end=date(2026, 3, 31)
    )["case_id"]
    dsid = imported(h, inputs / "SYNTHETIC_ledger.csv")
    cfg = load_config(overrides={"workspace": {"path": str(h.workspace)}, **case_overrides()})
    yield h, dsid, cfg, service.run_risk(h.conn, cfg, dsid)
    h.conn.close()


@pytest.fixture(scope="module")
def paysim(tmp_path_factory):
    h = make_ingest(tmp_path_factory.mktemp("ws"))
    dsid = imported(h, PAYSIM)
    yield h, dsid, service.run_risk(h.conn, h.cfg, dsid)
    h.conn.close()


def flagged(res, min_tier: str) -> set[str]:
    return {r["txn_id"] for r in res.rows if TIER_RANK[r["tier"]] >= TIER_RANK[min_tier]}


# ---------------------------------------------------------------------------------------------- synthetic


def test_synthetic_scores_every_row_within_the_review_budget(synthetic):
    s = synthetic[3].summary
    assert s["counts"]["scored"] == s["counts"]["transactions"] == 7271
    g = s["volume_guard"]
    assert not g["calibration_warning"] and g["high_or_critical"] <= g["high_budget"]
    assert s["counts"]["detectors_unavailable"] == 1  # balance continuity: a GL has no running balance
    assert s["readiness"]["status"] in ("ready", "ready_with_warnings")


def test_scenario_coverage_matches_the_recorded_baseline(synthetic):
    res = synthetic[3]
    medium = flagged(res, "medium")
    seen = {s for s, rows in INJECTED.items() if rows & medium}
    assert seen == BASELINE_SEEN, sorted(seen ^ BASELINE_SEEN)
    assert not (set(INJECTED) - seen - OUT_OF_SCOPE) - {"S-L-01"}  # every in-scope scenario but one is seen
    assert len([s for s in P8_TARGETS if INJECTED[s] & medium]) == 11  # S-L-01 is the documented miss


def test_high_tier_is_dominated_by_injected_scenarios(synthetic):
    high = flagged(synthetic[3], "high")
    assert len(high) == 58 and len(high & INJECTED_ROWS) == 52


@pytest.mark.parametrize("sid", QUIET_CONTROLS)
def test_look_alike_controls_never_reach_high(synthetic, sid):
    assert not CONTROLS[sid] & flagged(synthetic[3], "high"), sid


def test_controls_flagged_at_medium_are_recorded_not_hidden(synthetic):
    """Documented in docs/risk-engine.md §Known behaviour: P8's bimodal-cadence limitation on the two-company
    payroll stream and P7's first quarter-ends before the overtime routine is established."""
    medium = flagged(synthetic[3], "medium")
    assert len(CONTROLS["S-U-12"] & medium) == 32
    assert len(CONTROLS["S-U-08"] & flagged(synthetic[3], "high")) == 1
    assert len(CONTROLS["S-U-14"] & flagged(synthetic[3], "high")) == 1  # P7 rule + P8 tail on one row


def test_every_high_row_is_corroborated_and_explained(synthetic):
    h, dsid, cfg, res = synthetic
    for t in flagged(res, "high"):
        row = res.by_txn[t]
        assert row["independent_sources"] >= cfg.settings.risk.tiers.min_sources_high
        d = service.transaction_detail(res, t, cfg.settings)
        ex = d["explanation"]
        assert len({c["family"] for c in ex["all_contributors"]}) >= 2
        assert round(sum(c["contribution"] for c in ex["all_contributors"]) * 100) == round(ex["score"] * 100)
        assert all(c["explanation"] and c["evidence"] for c in ex["all_contributors"])


def test_anomaly_evidence_alone_never_reaches_high(synthetic):
    res = synthetic[3]
    for r in res.rows:
        groups = {g.group for g in res.fused[r["txn_id"]].groups} if r["txn_id"] in res.fused else set()
        if groups == {"anomaly"}:
            assert r["score"] <= 45.0 and r["tier"] in ("low", "medium")


def test_entities_aggregate_and_rank(synthetic):
    res = synthetic[3]
    s = res.summary["entities"]
    assert s["total"] == len(res.entities) == 159
    ranks = [e["review_rank"] for e in res.entities if e["review_rank"]]
    assert ranks == list(range(1, len(ranks) + 1))
    assert sum(e["transactions"] for e in res.entities) == 7271
    top = res.entities[0]
    assert top["tier"] in ("high", "critical") and top["top_transactions"][0]["txn_id"] in res.by_txn


# ---------------------------------------------------------------------------------------------- PaySim


def test_paysim_scores_with_anomaly_evidence_only_and_says_why(paysim):
    _, _, res = paysim
    s = res.summary
    assert s["counts"]["tiers"]["high"] == s["counts"]["tiers"]["critical"] == 0  # no rule can corroborate
    assert s["counts"]["detectors_unavailable"] == 7
    assert all(d["reason"] for d in s["detectors_unavailable"])
    assert {f["family"] for f in s["method"]["families"] if f["signals"]} == {
        "benford",
        "statistical_anomaly",
        "peer_anomaly",
        "tail_anomaly",
        "data_quality",
    }
    assert s["monthly"]["status"] == "not_available"


def test_paysim_data_quality_flags_never_create_risk(paysim):
    _, _, res = paysim
    dq_only = [t for t, f in res.fused.items() if all(not x.scored for x in f.families)]
    assert len(dq_only) > 700
    for t in dq_only:
        assert res.by_txn[t]["score"] == 0.0 and res.by_txn[t]["tier"] == "low"
        assert res.by_txn[t]["data_quality_flags"] >= 1


def test_paysim_labels_never_influence_scores(paysim, tmp_path):
    h, dsid, res = paysim
    rows = list(csv.reader(PAYSIM.open(encoding="utf-8", newline="")))
    for col in ("isFraud", "isFlaggedFraud"):
        c = rows[0].index(col)
        for r in rows[1:]:
            r[c] = "1" if r[c] == "0" else "0"
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    flipped = tmp_path / "Datasets.csv"
    flipped.write_text(buf.getvalue(), encoding="utf-8", newline="")
    other_id = imported(h, flipped)
    other = service.run_risk(h.conn, h.cfg, other_id)

    def by_source(d, r):
        src = {x["txn_id"]: x["source_row"] for x in load_view(h.conn, d).rows}
        return {src[x["txn_id"]]: (x["score"], x["tier"], x["review_rank"]) for x in r.rows}

    assert by_source(other_id, other) == by_source(dsid, res)


# ---------------------------------------------------------------------------------------------- isolation


def test_risk_code_reads_no_labels_extra_or_ground_truth():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "ledgerspy" / "risk"
    for path in root.glob("*.py"):
        code = path.read_text(encoding="utf-8")
        assert "FROM labels" not in code and "ground_truth" not in code and "isFraud" not in code, path
        assert '["extra"]' not in code and ".extra" not in code, path


def test_risk_never_writes_and_needs_no_network(synthetic, monkeypatch):
    h, dsid, cfg, res = synthetic

    def blocked(*a, **k):
        raise AssertionError("network access attempted")

    for target, name in ((socket.socket, "connect"), (socket, "getaddrinfo"), (socket, "create_connection")):
        monkeypatch.setattr(target, name, blocked)
    before, changes = _table_digest(h.conn), h.conn.total_changes
    service.clear_cache()
    again = service.run_risk(h.conn, cfg, dsid)
    assert _table_digest(h.conn) == before and h.conn.total_changes == changes
    assert again.result_sha256 == res.result_sha256  # recomputed from scratch: identical


def test_cache_identity_follows_data_and_config(synthetic):
    h, dsid, cfg, res = synthetic
    assert service.run_risk(h.conn, cfg, dsid) is service.run_risk(h.conn, cfg, dsid)  # one coherent result
    other_cfg = load_config(
        overrides={
            "workspace": {"path": str(h.workspace)},
            **case_overrides(),
            "risk": {"tiers": {"medium": 30}},
        }
    )
    other = service.run_risk(h.conn, other_cfg, dsid)
    assert other.identity["config_sha256"] != res.identity["config_sha256"] and other is not res
    run_integrity(h.conn, h.cfg, dsid, "t")  # a new P4 run of the same data is a new input
    newer = service.run_risk(h.conn, cfg, dsid)
    assert newer.identity["p4_run_id"] != res.identity["p4_run_id"]
    assert [r["score"] for r in newer.rows] == [r["score"] for r in res.rows]  # same data, same scores


def test_volume_guard_fires_on_a_mis_weighted_configuration(synthetic):
    h, dsid, _, _ = synthetic
    loose = load_config(
        overrides={
            "workspace": {"path": str(h.workspace)},
            **case_overrides(),
            "risk": {
                "tiers": {
                    "medium": 1,
                    "high": 2,
                    "critical": 3,
                    "min_sources_high": 1,
                    "min_sources_critical": 1,
                }
            },
        }
    )
    res = service.run_risk(h.conn, loose, dsid)
    g = res.summary["volume_guard"]
    assert g["calibration_warning"] and g["exceeded_high_budget"] and "exceed" in g["message"]
    assert g["review_queue_size"] == loose.settings.risk.review.top_k
    assert g["over_capacity"] == g["high_or_critical"] - g["review_queue_size"]
    assert g["raw_tier_counts"] == res.summary["counts"]["tiers"]  # raw tiers preserved


# ---------------------------------------------------------------------------------------------- API


@pytest.fixture(scope="module")
def api(tmp_path_factory):
    ws = tmp_path_factory.mktemp("api") / "ws"
    cfg = load_config(overrides={"workspace": {"path": str(ws)}, "server": {"port": PORT}})
    db = db_path_for(cfg.settings)
    init_db(db, "tester")
    token = secrets.token_urlsafe(32)
    auth = {"Authorization": f"Bearer {token}"}
    with TestClient(create_app(cfg, db, token), base_url=BASE_URL) as c:
        case = c.post("/api/cases", json={"name": "PaySim"}, headers=auth).json()["case_id"]
        ds = c.post(
            f"/api/cases/{case}/files?filename=paysim.csv",
            content=PAYSIM.read_bytes(),
            headers={**auth, "Content-Type": "application/octet-stream"},
        ).json()
        assert (
            c.put(
                f"/api/datasets/{ds['dataset_id']}/mapping",
                json=ds["mapping_suggestion"]["proposed_mapping"],
                headers=auth,
            ).status_code
            == 200
        )
        c.post(f"/api/datasets/{ds['dataset_id']}/integrity", headers=auth)
        yield c, auth, f"/api/datasets/{ds['dataset_id']}/risk"


def test_risk_endpoints_require_the_token(api):
    c, _, base = api
    for method, path in [
        ("post", base),
        ("get", base),
        ("get", base + "/transactions"),
        ("get", base + "/transactions/T-0000000000000000"),
        ("get", base + "/entities"),
        ("get", base + "/entities/E-0000000000000000"),
        ("get", base + "/detectors"),
        ("get", base + "/scatter"),
    ]:
        assert getattr(c, method)(path).status_code == 401, path


def test_summary_and_list_are_consistent_and_deterministic(api):
    c, auth, base = api
    s = c.post(base, headers=auth).json()
    assert c.get(base, headers=auth).json() == s  # repeated request: identical
    for tier, n in s["counts"]["tiers"].items():
        page = c.get(base + f"/transactions?tier={tier}&size=1", headers=auth).json()
        assert page["total"] == n and page["result_sha256"] == s["result_sha256"]
    assert c.get(base + "/transactions?size=1", headers=auth).json()["total"] == s["counts"]["transactions"]


def test_pagination_is_complete_and_stable(api):
    c, auth, base = api
    p1 = c.get(base + "/transactions?page=1&size=100", headers=auth).json()
    p2 = c.get(base + "/transactions?page=2&size=100", headers=auth).json()
    assert p1["pages"] == -(-p1["total"] // 100) and len(p1["items"]) == 100
    assert not {r["txn_id"] for r in p1["items"]} & {r["txn_id"] for r in p2["items"]}
    ranks = [r["review_rank"] for r in p1["items"] if r["review_rank"]]
    assert ranks == list(range(1, len(ranks) + 1))  # priority order
    assert c.get(base + "/transactions?size=501", headers=auth).status_code == 422
    assert c.get(base + "/transactions?tier=bogus", headers=auth).status_code == 422
    q = p1["items"][0]["txn_id"]
    found = c.get(base + f"/transactions?q={q}", headers=auth).json()
    assert found["total"] >= 1 and found["items"][0]["txn_id"] == q


def test_transaction_detail_matches_the_list_and_explains(api):
    c, auth, base = api
    row = c.get(base + "/transactions?size=1", headers=auth).json()["items"][0]
    d = c.get(base + f"/transactions/{row['txn_id']}", headers=auth).json()
    assert d["transaction"] == row and d["explanation"]["score"] == row["score"]
    ex = d["explanation"]
    assert round(sum(x["contribution"] for x in ex["all_contributors"]) * 100) == round(ex["score"] * 100)
    assert "not a probability" in " ".join(ex["limitations"])
    assert c.get(base + "/transactions/NOPE-1", headers=auth).status_code == 404


def test_entity_and_detector_and_scatter_endpoints(api):
    c, auth, base = api
    ents = c.get(base + "/entities?size=5", headers=auth).json()
    assert ents["total"] > 0 and len(ents["items"]) == 5
    e = c.get(base + f"/entities/{ents['items'][0]['entity_id']}", headers=auth).json()
    assert e["entity"]["entity_id"] == ents["items"][0]["entity_id"] and e["transactions_total"] >= 1
    assert c.get(base + "/entities/E-0000000000000000", headers=auth).status_code == 404
    det = c.get(base + "/detectors", headers=auth).json()
    assert {i["detector"] for i in det["items"]} >= {"isolation_forest", "velocity", "benford_subgroup"}
    sc = c.get(base + "/scatter", headers=auth).json()
    assert sc["rows_with_score"] <= len(sc["points"]) <= service.SCATTER_MAX_POINTS
    assert all(p["score"] is not None for p in sc["points"])


def test_unknown_dataset_is_404(api):
    c, auth, _ = api
    assert c.get("/api/datasets/D-00000000000000000000000000/risk", headers=auth).status_code == 404


def test_ranking_key_is_documented_and_total():
    rows = [{"txn_id": "A", "tier": "high", "score": 60.0, "independent_sources": 2, "amount_minor": None}]
    assert calibration.priority_key(rows[0])[0] == -TIER_RANK["high"]
