"""P7 on real imports: synthetic scenario validation, PaySim compatibility, isolation, determinism, API.

Ground truth and PaySim labels are used here only to *evaluate* signals after detection.
"""

import csv
import hashlib
import io
import json
import shutil
import socket
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

import pytest

from ledgerspy.cases import store
from ledgerspy.config import load_config
from ledgerspy.forensics import service
from ledgerspy.normalize.view import load_view
from ledgerspy.quality.engine import run_integrity
from tests.backend.conftest import make_ingest
from tests.backend.test_features_datasets import PAYSIM, REFERENCE, case_overrides
from tests.backend.test_quality_api import canonical_dataset

GT = json.loads((REFERENCE / "ground_truth" / "SYNTHETIC_ground_truth.json").read_text(encoding="utf-8"))
SC = {s["scenario_id"]: s for s in GT["scenarios"]}
TARGETS = {  # rule → injected scenarios it is designed to see (where its prerequisites hold)
    "duplicate_exact": ["S-C-01"],
    "duplicate_near": ["S-C-02", "S-D-01", "S-D-02"],
    "round_amount": ["S-B-01"],
    "structuring": ["S-J-01", "S-J-02"],
    "temporal": ["S-K-01", "S-K-03"],
    "new_vendor_large": ["S-X-01", "S-P-01"],
    "dormancy": ["S-H-03"],
    "period_end": ["S-K-03"],
    "velocity": ["S-H-01"],
    "benford_subgroup": ["S-T-01"],
}
CONTROLS_PER_RULE = {  # look-alike controls that must stay quiet for the rule they resemble
    "duplicate_exact": ["S-U-05", "S-U-13"],
    "duplicate_near": ["S-U-05", "S-U-13"],
    "round_amount": ["S-U-01", "S-U-02"],
    "structuring": ["S-U-07"],
    "period_end": ["S-U-08"],
    "velocity": ["S-U-06", "S-U-12"],
    "new_vendor_large": ["S-U-04"],
}


def imported(h, path: Path) -> str:
    ds = h.upload(path)
    h.confirm(ds)
    run_integrity(h.conn, h.cfg, ds["dataset_id"], "t")
    return ds["dataset_id"]


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
    rep = service.run_forensics(h.conn, cfg, dsid)
    yield h, dsid, cfg, rep, time.perf_counter() - started
    h.conn.close()


@pytest.fixture(scope="module")
def paysim(tmp_path_factory):
    h = make_ingest(tmp_path_factory.mktemp("ws"))
    dsid = imported(h, PAYSIM)
    yield h, dsid, service.run_forensics(h.conn, h.cfg, dsid)
    h.conn.close()


def hits(rep: dict, rule_id: str) -> set[str]:
    return {t for s in rep["signals"] if s["rule_id"] == rule_id for t in s["txn_ids"]}


# ---------------------------------------------------------------------------------------------- synthetic


def test_every_rule_runs_on_the_synthetic_ledger(synthetic):
    _, _, _, rep, seconds = synthetic
    status = {r["rule_id"]: r["status"] for r in rep["rules"]}
    assert status.pop("balance_continuity") == "not_applicable"  # a GL has no running balance
    assert set(status.values()) == {"evaluated"}
    assert seconds < 20  # 7,271 rows, three years: practical on a laptop


@pytest.mark.parametrize("rule_id", sorted(TARGETS))
def test_injected_scenarios_are_visible_to_their_detector(synthetic, rule_id):
    rep = synthetic[3]
    flagged = hits(rep, rule_id)
    for sid in TARGETS[rule_id]:
        assert flagged & set(SC[sid]["affected"]["ledger_txn_ids"]), (rule_id, sid)


@pytest.mark.parametrize("rule_id", sorted(CONTROLS_PER_RULE))
def test_look_alike_controls_stay_quiet(synthetic, rule_id):
    rep = synthetic[3]
    flagged = hits(rep, rule_id)
    for sid in CONTROLS_PER_RULE[rule_id]:
        assert not flagged & set(SC[sid]["affected"]["ledger_txn_ids"]), (rule_id, sid)


def test_no_control_is_systematically_flagged(synthetic):
    rep = synthetic[3]
    row_signals = [s for s in rep["signals"] if s["rule_id"] != "benford_subgroup"]  # Benford cites samples
    flagged = {t for s in row_signals for t in s["txn_ids"]}
    for s in GT["scenarios"]:
        rows = set(s["affected"]["ledger_txn_ids"])
        if s["is_control"] and rows:
            assert len(rows & flagged) / len(rows) < 0.25, (s["scenario_id"], len(rows & flagged), len(rows))


def test_period_end_and_temporal_explain_the_year_end_cluster(synthetic):
    rep = synthetic[3]
    (pe,) = [s for s in rep["signals"] if s["rule_id"] == "period_end"]
    assert set(pe["txn_ids"]) == set(SC["S-K-03"]["affected"]["ledger_txn_ids"])
    assert pe["evidence"]["period_end"] == "2025-03-31" and pe["evidence"]["rows_in_window"] == 5


def test_benford_finds_the_digit_subgroup_and_reports_gating(synthetic):
    b = synthetic[3]["benford"]
    assert {t: b["dataset"][t]["status"] for t in b["dataset"]} == dict.fromkeys(b["dataset"], "analyzed")
    cp = b["subgroups"]["counterparty"]["tests"]["first"]
    assert cp["groups_total"] == 159 and cp["groups_analyzed"] == 2 and cp["groups_skipped_small"] == 157
    t01 = next(
        s
        for s in synthetic[3]["signals"]
        if s["rule_id"] == "benford_subgroup"
        and set(s["txn_ids"]) & set(SC["S-T-01"]["affected"]["ledger_txn_ids"])
    )
    assert t01["evidence"]["band"] == "nonconformity" and set(t01["evidence"]["over_represented"]) <= {
        7,
        8,
        9,
    }
    assert b["subgroups"]["txn_type"]["status"] == "not_applicable"


def test_known_miss_is_reported_not_hidden(synthetic):
    """S-G-01 (new vendor at ~4x its category median) stays below the 3-robust-sigma gate: the
    raw-material peer group is widely dispersed. Documented in docs/forensics.md; the feature evidence
    remains for P8/P9."""
    rep = synthetic[3]
    assert not hits(rep, "new_vendor_large") & set(SC["S-G-01"]["affected"]["ledger_txn_ids"])


# ---------------------------------------------------------------------------------------------- PaySim

PAYSIM_UNAVAILABLE = {
    "dormancy",
    "duplicate_near",
    "new_vendor_large",  # its warm-up gate needs real time
    "period_end",
    "structuring",
    "temporal",
    "velocity",
}


def test_paysim_runs_and_reports_the_detectors_it_cannot_support(paysim):
    _, _, rep = paysim
    status = {r["rule_id"]: r for r in rep["rules"]}
    assert {k for k, r in status.items() if r["status"] == "not_available"} == PAYSIM_UNAVAILABLE
    for k in PAYSIM_UNAVAILABLE:
        assert status[k]["reason"]
    assert "simulation" in status["temporal"]["reason"] and "corporate" in status["structuring"]["reason"]
    assert {k for k, r in status.items() if r["status"] == "evaluated"} == {
        "balance_continuity",
        "duplicate_exact",
        "round_amount",
    }
    kinds = {s["evidence"]["value"] for s in rep["signals"] if s["rule_id"] == "balance_continuity"}
    assert kinds == {"other_mismatch"}  # unchanged/zeroed balances are documented simulator artefacts
    assert rep["benford"]["subgroups"]["fiscal_year"]["status"] == "not_applicable"


def _rows_by_source(h, dsid, rep) -> dict:
    src = {r["txn_id"]: r["source_row"] for r in load_view(h.conn, dsid).rows}
    out = defaultdict(set)
    for s in rep["signals"]:
        out[s["rule_id"]] |= {src[t] for t in s["txn_ids"]}
    return dict(out)


def test_paysim_labels_never_influence_detection(paysim, tmp_path):
    h, dsid, rep = paysim
    rows = list(csv.reader(PAYSIM.open(encoding="utf-8", newline="")))
    c = rows[0].index("isFraud")
    for r in rows[1:]:
        r[c] = "1" if r[c] == "0" else "0"
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    flipped = tmp_path / "Datasets.csv"
    flipped.write_text(buf.getvalue(), encoding="utf-8", newline="")
    other_id = imported(h, flipped)
    other = service.run_forensics(h.conn, h.cfg, other_id)
    assert _rows_by_source(h, other_id, other) == _rows_by_source(h, dsid, rep)
    assert [r | {"reason": ""} for r in other["rules"]] == [r | {"reason": ""} for r in rep["rules"]]
    assert other["benford"]["dataset"] == rep["benford"]["dataset"]


# ---------------------------------------------------------------------------------------------- isolation


def test_forensics_code_reads_no_labels_extra_or_ground_truth():
    root = Path(__file__).resolve().parents[2] / "ledgerspy"
    for path in [*(root / "forensics").glob("*.py"), *(root / "detectors").glob("*.py")]:
        code = path.read_text(encoding="utf-8")
        assert "FROM labels" not in code and "ground_truth" not in code and "isFraud" not in code, path
        assert '["extra"]' not in code and ".extra" not in code, path


def _table_digest(conn) -> str:
    h = hashlib.sha256()
    for table in ("transactions", "labels", "integrity_issues", "normalized_values"):
        for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2"):  # noqa: S608 — fixed names
            h.update(repr(tuple(row)).encode())
    return h.hexdigest()


def test_forensics_never_writes_and_needs_no_network(synthetic, monkeypatch):
    h, dsid, cfg, rep, _ = synthetic

    def blocked(*a, **k):
        raise AssertionError("network access attempted")

    for target, name in ((socket.socket, "connect"), (socket, "getaddrinfo"), (socket, "create_connection")):
        monkeypatch.setattr(target, name, blocked)
    before, changes = _table_digest(h.conn), h.conn.total_changes
    again = service.run_forensics(h.conn, cfg, dsid)
    assert _table_digest(h.conn) == before and h.conn.total_changes == changes
    assert again["report_sha256"] == rep["report_sha256"]  # deterministic


def test_the_same_file_gives_the_same_signals_in_another_workspace(synthetic, tmp_path):
    _, _, _, rep, _ = synthetic
    h = make_ingest(tmp_path)
    h.case_id = store.create_case(
        h.conn, name="again", actor="t", period_start=date(2023, 4, 1), period_end=date(2026, 3, 31)
    )["case_id"]
    dsid = imported(h, REFERENCE / "SYNTHETIC_ledger.csv")
    cfg = load_config(overrides={"workspace": {"path": str(h.workspace)}, **case_overrides()})
    again = service.run_forensics(h.conn, cfg, dsid)
    for key in ("signals", "rules", "benford", "summary", "affected"):
        assert again[key] == rep[key], key
    assert {k: v for k, v in again["inputs"].items()} == rep["inputs"]
    h.conn.close()


# ------------------------------------------------------------------------------------------- P4 balance + API


def test_balance_discontinuity_end_to_end_over_the_api(client, auth):
    dsid = canonical_dataset(client, auth, "balance.csv")
    url = f"/api/datasets/{dsid}/forensics"
    assert client.post(url).status_code == 401  # token-protected
    before = client.post(url, headers=auth).json()
    bal = next(r for r in before["rules"] if r["rule_id"] == "balance_continuity")
    assert bal["status"] == "not_available" and "P4" in bal["reason"]  # no integrity run yet
    client.post(f"/api/datasets/{dsid}/integrity", headers=auth)
    rep = client.post(url, headers=auth).json()
    (s,) = [s for s in rep["signals"] if s["rule_id"] == "balance_continuity"]
    issue = s["evidence"]["p4_issues"][0]["observed"]
    assert issue["balance"] == 740_00 and issue["expected_balance"] == 750_00
    assert rep["report_sha256"] == client.post(url, headers=auth).json()["report_sha256"]
