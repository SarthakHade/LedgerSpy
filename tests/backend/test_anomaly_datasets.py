"""P8 on real imports: synthetic scenario validation, PaySim proxy, isolation, determinism, API, 100k rows.

Ground truth and PaySim labels are used here only to *evaluate* scores after detection.
"""

import csv
import io
import json
import shutil
import socket
import time
from datetime import date
from pathlib import Path

import pytest

from ledgerspy.anomaly import evaluation, service
from ledgerspy.cases import store
from ledgerspy.config import load_config
from ledgerspy.normalize.view import load_view
from ledgerspy.synth.generator import generate_to, params_from
from tests.backend.conftest import make_ingest
from tests.backend.test_features_datasets import PAYSIM, REFERENCE, case_overrides
from tests.backend.test_forensics_datasets import _table_digest, imported
from tests.backend.test_quality_api import canonical_dataset
from tests.backend.test_synth_generation import CFG

GT = json.loads((REFERENCE / "ground_truth" / "SYNTHETIC_ground_truth.json").read_text(encoding="utf-8"))
SC = {s["scenario_id"]: s for s in GT["scenarios"]}
# P5 scenarios that produce genuine statistical outliers in amount, velocity, dormancy, timing or vendor
# behaviour — chosen from the scenario definitions before any P8 run. Duplicates, look-alike names,
# relationships, bank mismatches, digit patterns, splitting and graph patterns belong to other detectors.
P8_TARGETS = [
    "S-A-01",  # payment ~9x the vendor's median
    "S-A-02",  # payment ~7x the vendor's others
    "S-G-01",  # new vendor paid ~4x the category median
    "S-H-01",  # monthly vendor paid nine times in two weeks
    "S-H-02",  # six payments in 18 days
    "S-H-03",  # dormant 16 months, then six payments at ~3x the old level
    "S-I-01",  # employee claim ~20x their usual
    "S-K-01",  # late-night / Sunday / holiday postings
    "S-K-03",  # unusually large late-night year-end payments
    "S-L-01",  # test-then-exploit: small payments then a large one
    "S-Q-01",  # gradual escalation in size and frequency
    "S-X-01",  # new vendor, burst, one payment ~7x
]
# Look-alikes of the P8 targets (ordinary new vendor, seasonal velocity, routine period-end overtime, fixed
# rents / retainers): they must not be systematically flagged by any P8 detector.
P8_CONTROLS = ["S-U-01", "S-U-02", "S-U-04", "S-U-06", "S-U-08"]
SCORE_COL = {
    "isolation_forest": "score",
    "robust_peer": "max_abs_z",
    "tail_percentile": "max_tail_percentile",
}
# Recorded baseline (docs/p8-acceptance.md): target scenarios with at least one flagged row, per detector.
BASELINE_HITS = {
    "isolation_forest": {"S-A-02", "S-G-01", "S-H-02", "S-L-01", "S-Q-01", "S-X-01"},
    "robust_peer": {"S-A-01", "S-A-02", "S-H-01", "S-H-02", "S-I-01", "S-K-01", "S-X-01"},
    "tail_percentile": {
        "S-A-01",
        "S-A-02",
        "S-H-01",
        "S-H-02",
        "S-H-03",
        "S-I-01",
        "S-K-01",
        "S-K-03",
        "S-X-01",
    },
}


def rows_of(sid: str) -> set[str]:
    return set(SC[sid]["affected"]["ledger_txn_ids"])


TARGET_ROWS = set().union(*(rows_of(s) for s in P8_TARGETS))


def flagged(rep: dict, detector_id: str) -> set[str]:
    return {t for s in rep["signals"] if s["detector_id"] == detector_id for t in s["txn_ids"]}


def scores(rep: dict, detector_id: str) -> dict[str, float | None]:
    col = rep["scores"][detector_id][SCORE_COL[detector_id]]
    return dict(zip(rep["scores"]["txn_ids"], col, strict=True))


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
    started = time.perf_counter()
    rep = service.run_anomaly(h.conn, cfg, dsid)
    yield h, dsid, cfg, rep, time.perf_counter() - started
    h.conn.close()


@pytest.fixture(scope="module")
def paysim(tmp_path_factory):
    h = make_ingest(tmp_path_factory.mktemp("ws"))
    dsid = imported(h, PAYSIM)
    yield h, dsid, service.run_anomaly(h.conn, h.cfg, dsid)
    h.conn.close()


# ---------------------------------------------------------------------------------------------- synthetic


def test_every_detector_runs_on_the_synthetic_ledger(synthetic):
    _, _, _, rep, seconds = synthetic
    assert rep["summary"]["status"] == dict.fromkeys(SCORE_COL, "evaluated")
    assert rep["summary"]["features_used"] == {"isolation_forest": 17, "robust_peer": 4, "tail_percentile": 6}
    warm = rep["population"]["warm_up"]
    assert warm["applied"] and warm["rows"] == 535  # the first 90 days: history not yet representative
    assert all(d["insufficient_reasons"]["warm_up"] == 535 for d in rep["detectors"])
    model = rep["detectors"][0]["details"]["model"]
    assert (model["random_state"], model["n_estimators"], model["psi"]) == (42, 100, 256)
    assert seconds < 20  # 7,271 rows (P6 + P8): practical on a laptop


def test_target_scenarios_match_the_recorded_baseline(synthetic):
    rep = synthetic[3]
    union = set()
    for det, expected in BASELINE_HITS.items():
        rows = flagged(rep, det)
        hit = {s for s in P8_TARGETS if rows & rows_of(s)}
        assert hit == expected, (det, sorted(hit ^ expected))
        union |= hit
    assert union == set(P8_TARGETS)  # every statistical-outlier scenario is seen by at least one detector


def test_isolation_forest_ranking_against_the_recorded_baseline(synthetic):
    rep = synthetic[3]
    sc = scores(rep, "isolation_forest")
    ev = evaluation.evaluate(sc, TARGET_ROWS, [50, 100, 250])
    assert ev["prevalence"] == pytest.approx(0.0171, abs=1e-3)
    assert ev["pr_auc"] == pytest.approx(0.037, abs=2e-3) and ev["pr_auc"] > 2 * ev["prevalence"]
    assert ev["at_k"]["50"]["precision"] == pytest.approx(0.08)
    ranked = evaluation.ranking(sc)
    targets = {s: rows_of(s) for s in P8_TARGETS}
    assert evaluation.coverage_at_k(ranked, targets, 50)["covered"] == [
        "S-A-02",
        "S-G-01",
        "S-H-02",
        "S-L-01",
        "S-X-01",
    ]
    assert evaluation.coverage_at_k(ranked, targets, 250)["covered_count"] == 9


def test_tail_ranking_against_the_recorded_baseline(synthetic):
    ev = evaluation.evaluate(scores(synthetic[3], "tail_percentile"), TARGET_ROWS, [50])
    assert ev["pr_auc"] == pytest.approx(0.131, abs=2e-3) and ev["at_k"]["50"]["precision"] == pytest.approx(
        0.34
    )


@pytest.mark.parametrize("detector_id", sorted(SCORE_COL))
def test_p8_look_alike_controls_are_not_systematically_flagged(synthetic, detector_id):
    rows = flagged(synthetic[3], detector_id)
    for sid in P8_CONTROLS:
        assert len(rows & rows_of(sid)) / len(rows_of(sid)) < 0.25, (detector_id, sid)


def test_known_failure_modes_are_recorded_not_hidden(synthetic):
    """Documented in docs/anomaly-model-card.md §Known failure modes."""
    rep = synthetic[3]
    # Two group companies pay one 'payroll clearing a/c' minutes apart: the stream's gaps alternate between
    # ~30 days and ~0 days, a bimodal cadence that a single robust z cannot describe.
    assert len(flagged(rep, "robust_peer") & rows_of("S-U-12")) == 32
    # The largest payments of the company are, by construction, in the top 1% of amounts.
    assert len(flagged(rep, "tail_percentile") & rows_of("S-U-12")) == 33
    # Brand-new parties are isolated quickly: first payments of the legitimate instalment / chain controls.
    assert rows_of("S-U-05") <= flagged(rep, "isolation_forest")
    # Dormancy (S-H-03) is seen by the tail only: the vendor has too few rows for a counterparty peer group,
    # and its ~3x amounts are ordinary within its account.
    assert not flagged(rep, "robust_peer") & rows_of("S-H-03")


def test_every_signal_explains_itself_and_never_claims_fraud(synthetic):
    rep = synthetic[3]
    for s in rep["signals"]:
        assert s["family"] == "model_anomaly" and s["status"] == "triggered"
        assert "not a finding of fraud" in s["explanation"]
        assert s["population"]["n"] > 0 and s["anomaly_score"] > 0
        assert "fraudulent" not in s["explanation"].replace("not a finding of fraud", "")


# ---------------------------------------------------------------------------------------------- PaySim


def test_paysim_proxy_runs_with_the_features_it_supports(paysim):
    _, _, rep = paysim
    assert rep["summary"]["status"] == dict.fromkeys(SCORE_COL, "evaluated")
    assert (
        rep["population"]["warm_up"]["applied"] is False
        and "simulation" in rep["population"]["warm_up"]["reason"]
    )
    used = {d["detector_id"]: [f["feature"] for f in d["features"]["used"]] for d in rep["detectors"]}
    assert used == {
        "isolation_forest": ["log_amount", "amount_robust_z", "amount_pctl_prior", "cp_prior_count"],
        "robust_peer": ["log_amount"],
        "tail_percentile": ["amount_minor"],
    }
    iforest = rep["detectors"][0]
    reasons = {e["feature"]: e["reason"] for e in iforest["features"]["excluded"]}
    assert (
        reasons["cp_velocity_ratio"] == "not_available"
        and reasons["cp_prev_amount_ratio"] == "excessive_missing"
    )
    assert iforest["rows_scored"] == 10118 and iforest["triggered"] == 100  # capped by review capacity
    peers = rep["detectors"][1]["details"]["by_feature"]["log_amount"]["peers"]
    assert peers["account"]["status"] == "not_available" and peers["txn_type"]["status"] == "evaluated"


def test_paysim_label_diagnostics_after_detection(paysim):
    h, dsid, rep = paysim
    # evaluation only: labels are read here, after the report exists
    labels = dict(
        h.conn.execute(
            "SELECT txn_id, value FROM labels WHERE dataset_id = ? AND column_name = 'isFraud'", (dsid,)
        )
    )
    positives = {t for t, v in labels.items() if v == "1"}
    assert len(positives) == 68
    results = {d: evaluation.evaluate(scores(rep, d), positives, [50, 100, 250]) for d in SCORE_COL}
    for d, ev in results.items():
        assert ev["pr_auc"] > ev["prevalence"], d  # better than random ranking, far from a usable classifier
    assert results["tail_percentile"]["pr_auc"] == pytest.approx(0.0603, abs=1e-3)
    assert results["isolation_forest"]["pr_auc"] == pytest.approx(0.0131, abs=1e-3)


def test_paysim_labels_never_influence_scores(paysim, tmp_path):
    h, dsid, rep = paysim
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
    other = service.run_anomaly(h.conn, h.cfg, other_id)

    def by_source(d, rep_):
        src = {r["txn_id"]: r["source_row"] for r in load_view(h.conn, d).rows}
        return {det: {src[t]: v for t, v in scores(rep_, det).items()} for det in SCORE_COL}, sorted(
            (s["detector_id"], src[s["subject_id"]]) for s in rep_["signals"]
        )

    assert by_source(other_id, other) == by_source(dsid, rep)
    assert [d["details"].get("model", {}).get("model_sha256") for d in other["detectors"]] == [
        d["details"].get("model", {}).get("model_sha256") for d in rep["detectors"]
    ]


# ---------------------------------------------------------------------------------------------- isolation


def test_anomaly_code_reads_no_labels_extra_ground_truth_or_p7_signals():
    root = Path(__file__).resolve().parents[2] / "ledgerspy" / "anomaly"
    for path in root.glob("*.py"):
        code = path.read_text(encoding="utf-8")
        assert "FROM labels" not in code and "ground_truth" not in code and "isFraud" not in code, path
        assert '["extra"]' not in code and ".extra" not in code, path
        assert "run_rules" not in code and "run_forensics" not in code and "forensics.service" not in code, (
            path
        )


def test_anomaly_never_writes_and_needs_no_network(synthetic, monkeypatch):
    h, dsid, cfg, rep, _ = synthetic

    def blocked(*a, **k):
        raise AssertionError("network access attempted")

    for target, name in ((socket.socket, "connect"), (socket, "getaddrinfo"), (socket, "create_connection")):
        monkeypatch.setattr(target, name, blocked)
    before, changes = _table_digest(h.conn), h.conn.total_changes
    again = service.run_anomaly(h.conn, cfg, dsid)
    assert _table_digest(h.conn) == before and h.conn.total_changes == changes
    assert again["report_sha256"] == rep["report_sha256"]  # deterministic


def test_the_same_file_gives_the_same_scores_in_another_workspace(synthetic, tmp_path):
    _, _, _, rep, _ = synthetic
    h = make_ingest(tmp_path)
    h.case_id = store.create_case(
        h.conn, name="again", actor="t", period_start=date(2023, 4, 1), period_end=date(2026, 3, 31)
    )["case_id"]
    dsid = imported(h, REFERENCE / "SYNTHETIC_ledger.csv")
    cfg = load_config(overrides={"workspace": {"path": str(h.workspace)}, **case_overrides()})
    again = service.run_anomaly(h.conn, cfg, dsid)
    for key in ("signals", "scores", "detectors", "summary", "population", "inputs", "evaluation"):
        assert again[key] == rep[key], key
    h.conn.close()


# ---------------------------------------------------------------------------------------------- API


def test_anomaly_report_over_the_api(client, auth):
    dsid = canonical_dataset(client, auth, "balance.csv")
    url = f"/api/datasets/{dsid}/anomaly"
    assert client.post(url).status_code == 401  # token-protected
    rep = client.post(url, headers=auth).json()
    for key in (
        "report_version",
        "dataset",
        "inputs",
        "population",
        "summary",
        "detectors",
        "signals",
        "scores",
        "evaluation",
        "note",
        "report_sha256",
    ):
        assert key in rep, key
    det = {d["detector_id"]: d for d in rep["detectors"]}
    assert set(det) == set(SCORE_COL)
    # a handful of rows: no model is fitted and nothing is invented
    assert det["isolation_forest"]["status"] == "insufficient_data" and det["isolation_forest"]["reason"]
    assert rep["scores"]["isolation_forest"]["score"] is None and rep["signals"] == []
    for d in det.values():
        for k in (
            "version",
            "status",
            "rows_scored",
            "rows_insufficient",
            "insufficient_reasons",
            "features",
            "parameters",
            "triggered",
        ):
            assert k in d, k
    assert rep["report_sha256"] == client.post(url, headers=auth).json()["report_sha256"]
    assert client.post("/api/datasets/D-00000000000000000000000000/anomaly", headers=auth).status_code in (
        404,
        422,
    )


# ---------------------------------------------------------------------------------------------- 100k rows


def test_100k_synthetic_rows_complete_in_development_time(tmp_path):
    """Benchmark guard: a four-year, scale-14 synthetic ledger (~100k rows) through P6 features and every P8
    detector. Measured ≈ 50 s on the development laptop (target < 60 s); the guard allows CI headroom."""
    generate_to(params_from(CFG, scale=14, years=4), tmp_path / "gen")
    h = make_ingest(tmp_path / "ws")
    h.case_id = store.create_case(
        h.conn, name="big", actor="t", period_start=date(2023, 4, 1), period_end=date(2027, 3, 31)
    )["case_id"]
    dsid = imported(h, tmp_path / "gen" / "SYNTHETIC_ledger.csv")
    cfg = load_config(overrides={"workspace": {"path": str(h.workspace)}, **case_overrides()})
    started = time.perf_counter()
    inp, run = service.build_inputs(h.conn, cfg, dsid)
    rep = service.analyze(inp, run, cfg.settings)
    seconds = time.perf_counter() - started
    assert len(inp.rows) >= 99_000
    assert rep["summary"]["status"] == dict.fromkeys(SCORE_COL, "evaluated")
    assert rep["detectors"][0]["triggered"] == 100  # review capacity, not 1% of 100k rows
    assert seconds < 120, seconds
    h.conn.close()
