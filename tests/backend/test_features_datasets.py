"""P6 on real imports: PaySim and the P5 synthetic company, through P3 ingestion and P4 integrity.

Ground truth is used here only to *evaluate* (which rows to look at); features are computed from datasets
imported without the ground-truth folder and never read labels or `extra`.
"""

import csv
import io
import json
import random
import re
import shutil
import socket
from datetime import date
from pathlib import Path

import pytest

from ledgerspy.cases import store
from ledgerspy.config import load_config
from ledgerspy.features import report as feature_report
from ledgerspy.features import service
from ledgerspy.features.data import party_key
from ledgerspy.features.spec import Level, implemented
from ledgerspy.hashing import canonical_json
from ledgerspy.normalize import service as norm
from ledgerspy.normalize.view import load_view
from ledgerspy.quality.engine import run_integrity
from tests.backend.conftest import load_quality_fixture, make_ingest

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = ROOT / "data" / "synthetic" / "reference"
PAYSIM = ROOT / "data" / "raw" / "paysim_sample_10k.csv"
GT = json.loads((REFERENCE / "ground_truth" / "SYNTHETIC_ground_truth.json").read_text(encoding="utf-8"))
SC = {s["scenario_id"]: s for s in GT["scenarios"]}


def case_overrides() -> dict:
    """The synthetic company's own calendar and approval limits, from its input (not ground-truth) files."""
    with (REFERENCE / "SYNTHETIC_holiday_calendar.csv").open(encoding="utf-8", newline="") as f:
        holidays = [r["date"] for r in csv.DictReader(f)]
    with (REFERENCE / "SYNTHETIC_group_entities.csv").open(encoding="utf-8", newline="") as f:
        lvc = next(r for r in csv.DictReader(f) if r["entity_code"] == "LVC")
    limits = [
        {"name": "expense_claim", "amount_minor": round(float(lvc["expense_claim_approval_limit"]) * 100)},
        {"name": "vendor_payment", "amount_minor": round(float(lvc["vendor_payment_approval_limit"]) * 100)},
    ]
    return {"features": {"calendar": {"holidays": holidays}, "approval_thresholds": limits}}


def imported(h, path: Path, name: str | None = None) -> str:
    ds = h.upload(path, name) if name else h.upload(path)
    h.confirm(ds)
    return ds["dataset_id"]


@pytest.fixture(scope="module")
def synthetic(tmp_path_factory):
    inputs = tmp_path_factory.mktemp("inputs")
    shutil.copy(REFERENCE / "SYNTHETIC_ledger.csv", inputs / "SYNTHETIC_ledger.csv")
    assert not (inputs / "ground_truth").exists()
    h = make_ingest(tmp_path_factory.mktemp("ws"))
    h.case_id = store.create_case(
        h.conn,
        name="Larkspur Vale (SYNTHETIC)",
        actor="t",
        period_start=date(2023, 4, 1),
        period_end=date(2026, 3, 31),
    )["case_id"]
    dsid = imported(h, inputs / "SYNTHETIC_ledger.csv")
    run_integrity(h.conn, h.cfg, dsid, "t")
    case_cfg = load_config(overrides={"workspace": {"path": str(h.workspace)}, **case_overrides()})
    run = service.build_for_dataset(h.conn, case_cfg, dsid)
    plain = service.build_for_dataset(h.conn, h.cfg, dsid)
    yield h, dsid, run, plain, case_cfg
    h.conn.close()


@pytest.fixture(scope="module")
def paysim(tmp_path_factory):
    h = make_ingest(tmp_path_factory.mktemp("ws"))
    dsid = imported(h, PAYSIM)
    run_integrity(h.conn, h.cfg, dsid, "t")
    yield h, dsid, service.build_for_dataset(h.conn, h.cfg, dsid)
    h.conn.close()


def computed(m) -> set[str]:
    return {n for n, st in m.status.items() if st["status"] == "computed"}


# ---------------------------------------------------------------------------------------------- PaySim

PAYSIM_SUPPORTED = {
    "amount_minor",
    "log_amount",
    "round_level",
    "is_outflow",
    "txn_type",
    "amount_robust_z",
    "amount_pctl_prior",
    "amount_to_cp_median_ratio",
    "cp_prev_amount_ratio",
    "cp_prior_count",
    "pair_prior_count",
    "is_reciprocal_pair",
    "payee_distinct_payers_prior",
    "payer_distinct_payees_prior",
    "pair_share_of_payer_outflow",
    "dq_issue_count",
    "dq_has_duplicate_issue",
    "dq_has_missing_field",
    "dq_is_normalized",
    "dq_balance_discontinuity",  # P4 checked the PaySim balance columns
}
PAYSIM_ENTITY = {
    "ent_txn_count",
    "ent_total_amount_minor",
    "ent_mean_amount_minor",
    "ent_median_amount_minor",
    "ent_log_amount_dispersion",
    "ent_round_share",
    "ent_share_of_flow",
    "ent_fan_in",
    "ent_fan_out",
}


def test_paysim_gets_only_the_features_it_can_support(paysim):
    _, _, run = paysim
    assert computed(run.transactions) == PAYSIM_SUPPORTED == set(run.transactions.columns)
    assert computed(run.entities) == PAYSIM_ENTITY
    st = {**run.transactions.status, **run.entities.status}
    for name in (
        "hour_of_day",
        "day_of_week",
        "is_after_hours",
        "cp_days_since_prev",
        "cp_count_30d",
        "ent_max_gap_days",
    ):
        assert st[name]["status"] == "not_available" and "simulation" in st[name]["reason"], name
    assert "not a corporate ledger" in st["threshold_proximity"]["reason"]
    assert "not a GL/category" in st["amount_to_account_median_ratio"]["reason"]
    assert run.manifest["dataset"]["time_basis"] == "simulation_step"
    assert {s for s in run.manifest["features"] if "fraud" in s.lower() or "label" in s.lower()} == set()


def test_paysim_robust_z_falls_back_to_the_type_peer_group(paysim):
    _, _, run = paysim
    basis = {b for b in run.transactions.basis["amount_robust_z"] if b}
    assert basis == {
        "counterparty",
        "txn_type",
        "unavailable",
    }  # senders are near-unique: few entity baselines


# ---------------------------------------------------------------------------------------------- synthetic


def test_synthetic_company_gets_the_corporate_feature_matrix(synthetic):
    _, _, run, plain, _ = synthetic
    tx = {s.name for s in implemented(Level.TRANSACTION)}
    assert computed(run.transactions) == tx - {"txn_type", "dq_balance_discontinuity"}  # no type, no balance
    assert run.transactions.status["txn_type"]["reason"] == "no transaction type is mapped"
    assert computed(run.entities) == {s.name for s in implemented(Level.ENTITY)}
    # without the case calendar and limits, those two features are honestly unavailable
    assert computed(plain.transactions) == tx - {
        "txn_type",
        "dq_balance_discontinuity",
        "is_holiday",
        "threshold_proximity",
    }
    assert len(run.transactions.subjects) == 7271 and len(run.entities.subjects) == 159


def _rows(synthetic, sid: str) -> list[int]:
    _, _, run, _, _ = synthetic
    index = {t: i for i, t in enumerate(run.transactions.subjects)}
    return [index[t] for t in SC[sid]["affected"]["ledger_txn_ids"]]


def _col(synthetic, name):
    return synthetic[2].transactions.columns[name]


def test_injected_scenarios_are_visible_in_the_features(synthetic):
    h, dsid, run, _, _ = synthetic
    f = lambda n: _col(synthetic, n)  # noqa: E731
    (a01,) = _rows(synthetic, "S-A-01")
    assert f("amount_to_cp_median_ratio")[a01] > 5 and f("amount_robust_z")[a01] > 3
    burst = sorted(SC["S-H-03"]["stages"]["reactivated"])
    first = run.transactions.subjects.index(burst[0])
    assert f("cp_days_since_prev")[first] > 365  # dormant for 494 days
    last = max(_rows(synthetic, "S-L-01"))
    assert f("cp_prev_amount_ratio")[last] == 35.0  # ₹3,85,000 after ₹11,000
    for i in _rows(synthetic, "S-K-03"):
        assert f("is_after_hours")[i] is True and f("days_to_fiscal_year_end")[i] == 0
    for i in _rows(synthetic, "S-K-01"):
        assert f("is_after_hours")[i] or f("is_non_working_weekday")[i] or f("is_holiday")[i]
    assert any(f("is_holiday")[i] for i in _rows(synthetic, "S-K-01"))  # 25 December
    ghost = sorted(_rows(synthetic, "S-F-01"))
    assert [f("cp_reference_gap")[i] for i in ghost[1:]] == [1] * 5  # invoices 001 … 006
    assert all(0.97 < f("threshold_proximity")[i] < 1 for i in _rows(synthetic, "S-J-01"))
    assert all(0.9 < f("threshold_proximity")[i] < 1 for i in _rows(synthetic, "S-F-01"))
    assert max(f("payer_new_payees_7d")[i] for i in _rows(synthetic, "S-P-02")) >= 6
    assert max(f("cp_velocity_ratio")[i] or 0 for i in _rows(synthetic, "S-H-01")) > 3
    assert any(
        f("is_reciprocal_pair")[i] for i in _rows(synthetic, "S-U-10")
    )  # a vendor that also buys scrap
    (dup,) = [run.transactions.subjects.index(t) for t in SC["S-C-01"]["stages"]["duplicate"]]
    assert f("dq_has_duplicate_issue")[dup] is True
    assert all(f("dq_issue_count")[i] >= 1 for i in _rows(synthetic, "S-V-01"))


def _stream(synthetic, sid: str) -> int:
    h, dsid, run, _, _ = synthetic
    view = {r["txn_id"]: r for r in load_view(h.conn, dsid).rows}
    txn = SC[sid]["affected"]["ledger_txn_ids"][-1]
    return run.entities.subjects.index("out:" + party_key(view[txn]["payee_raw"]))


def test_entity_profiles_reflect_scenarios(synthetic):
    ent = synthetic[2].entities.columns
    assert ent["ent_round_share"][_stream(synthetic, "S-B-01")] == 1.0
    assert ent["ent_reference_sequential_share"][_stream(synthetic, "S-F-01")] == 1.0
    assert ent["ent_reference_sequential_share"][_stream(synthetic, "S-U-14")] == 1.0
    assert ent["ent_max_gap_days"][_stream(synthetic, "S-H-03")] > 365
    assert ent["ent_log_amount_trend"][_stream(synthetic, "S-Q-01")] > 0.05
    normal = [v for v in ent["ent_reference_sequential_share"] if v is not None]
    assert sorted(normal)[len(normal) // 2] < 0.5  # ordinary vendors are not sequential


# ---------------------------------------------------------------------------------------------- isolation


def test_feature_code_reads_no_labels_extra_or_ground_truth():
    for path in (ROOT / "ledgerspy" / "features").glob("*.py"):
        code = path.read_text(encoding="utf-8")
        assert not re.search(r"FROM\s+labels|ground_truth|isFraud|is_fraud", code, re.I), path
        assert not re.search(r"\[\s*[\"']extra[\"']\s*\]|\.get\(\s*[\"']extra|\.extra\b", code), path


def _rewrite(src: Path, dst: Path, change) -> Path:
    with src.open(encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    rows = change(rows)
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    dst.write_text(buf.getvalue(), encoding="utf-8", newline="")
    return dst


def _values(run) -> dict:
    return {
        "tx": {k: v for k, v in run.transactions.columns.items()},
        "basis": run.transactions.basis,
        "ent": run.entities.columns,
        "ent_subjects": run.entities.subjects,
    }


def test_changing_paysim_labels_changes_no_feature(paysim, tmp_path):
    h, _, base = paysim

    def relabel(rows, how):
        head = rows[0]
        cols = [head.index("isFraud"), head.index("isFlaggedFraud")]
        rng = random.Random(5)
        for r in rows[1:]:
            for c in cols:
                r[c] = how(rng)
        return rows

    for how in (lambda rng: "1", lambda rng: "", lambda rng: rng.choice(["0", "1"])):
        path = _rewrite(
            PAYSIM, tmp_path / f"paysim_{random.random()}.csv", lambda rows, how=how: relabel(rows, how)
        )
        dsid = imported(h, path)
        run_integrity(h.conn, h.cfg, dsid, "t")
        other = service.build_for_dataset(h.conn, h.cfg, dsid)
        assert _values(other) == _values(base)


def test_label_and_ground_truth_columns_in_a_ledger_change_no_feature(synthetic, tmp_path):
    h, dsid, _, plain, _ = synthetic

    def add(rows):
        rows[0] += ["is_fraud", "scenario_id", "Approved By 2"]
        rng = random.Random(9)
        for r in rows[1:]:
            r += [rng.choice(["0", "1"]), rng.choice(["S-A-01", "S-U-01", ""]), "E9999"]
        return rows

    path = _rewrite(REFERENCE / "SYNTHETIC_ledger.csv", tmp_path / "SYNTHETIC_ledger.csv", add)
    other = imported(h, path)
    labels = h.conn.execute(
        "SELECT DISTINCT column_name FROM labels WHERE dataset_id = ?", (other,)
    ).fetchall()
    assert [r[0] for r in labels] == ["is_fraud"]  # P3 split it off; scenario_id is just an unmapped column
    run_integrity(h.conn, h.cfg, other, "t")
    assert _values(service.build_for_dataset(h.conn, h.cfg, other)) == _values(plain)


# ---------------------------------------------------------------------------------------------- determinism


def test_the_same_file_gives_the_same_matrix_in_another_workspace(synthetic, tmp_path):
    _, _, _, plain, _ = synthetic
    h = make_ingest(tmp_path)
    h.case_id = store.create_case(
        h.conn, name="again", actor="t", period_start=date(2023, 4, 1), period_end=date(2026, 3, 31)
    )["case_id"]
    dsid = imported(h, REFERENCE / "SYNTHETIC_ledger.csv")
    run_integrity(h.conn, h.cfg, dsid, "t")
    again = service.build_for_dataset(h.conn, h.cfg, dsid)
    assert again.manifest["transaction_matrix_sha256"] == plain.manifest["transaction_matrix_sha256"]
    assert again.manifest["entity_matrix_sha256"] == plain.manifest["entity_matrix_sha256"]
    strip = lambda m: {k: v for k, v in m.items() if k != "dataset"}  # noqa: E731
    assert strip(again.manifest) == strip(plain.manifest)
    h.conn.close()


def test_building_features_writes_nothing_and_needs_no_network(synthetic, monkeypatch):
    h, dsid, _, plain, _ = synthetic

    def blocked(*a, **k):
        raise AssertionError("network access attempted")

    for target, name in ((socket.socket, "connect"), (socket, "getaddrinfo"), (socket, "create_connection")):
        monkeypatch.setattr(target, name, blocked)
    before = h.conn.total_changes
    again = service.build_for_dataset(h.conn, h.cfg, dsid)
    assert h.conn.total_changes == before
    assert again.manifest == plain.manifest


# ---------------------------------------------------------------------------------------------- P4 linkage


def test_data_quality_features_need_a_current_p4_run(ingest):
    ds = load_quality_fixture(ingest, "normalization.csv")
    dsid = ds["dataset_id"]
    st = service.build_for_dataset(ingest.conn, ingest.cfg, dsid).transactions.status
    assert st["dq_issue_count"]["status"] == "not_available"  # no integrity run yet
    run_integrity(ingest.conn, ingest.cfg, dsid, "t")
    assert "dq_issue_count" in service.build_for_dataset(ingest.conn, ingest.cfg, dsid).transactions.columns
    fixes = norm.list_fixes(ingest.conn, dsid)
    norm.decide(
        ingest.conn, dsid, [{"fix_id": f["fix_id"], "decision": "accept", "value": None} for f in fixes], "a"
    )
    norm.apply_accepted(ingest.conn, dsid, "a")
    stale = service.build_for_dataset(ingest.conn, ingest.cfg, dsid).transactions
    assert (
        stale.status["dq_issue_count"]["reason"] == "no P4 integrity run exists for the current data version"
    )
    assert sum(stale.columns["dq_is_normalized"]) == len({f["txn_id"] for f in fixes})
    run_integrity(ingest.conn, ingest.cfg, dsid, "t")
    fresh = service.build_for_dataset(ingest.conn, ingest.cfg, dsid).transactions
    assert "dq_issue_count" in fresh.columns and fresh.status["dq_issue_count"]["status"] == "computed"


# ------------------------------------------------------------------------------------------- quality report


def test_quality_report_flags_constant_and_sparse_features(synthetic, paysim):
    syn = feature_report.quality_report(synthetic[2], synthetic[4].settings.features)
    pay = feature_report.quality_report(paysim[2], paysim[0].cfg.settings.features)
    for rep in (syn, pay):
        s = rep["summary"]
        assert s["registered"] == 72 == s["computed"] + s["not_available"] + s["not_available_yet"]
        assert s["not_available_yet"] == 10
        canonical_json(rep)  # computed values only: serialisable, no NaN/Infinity
        for e in rep["transaction_features"] + rep["entity_features"]:
            if e["status"] == "computed":
                assert e["rows"] >= e["nulls"] >= 0 and "flags" in e
            else:
                assert e["reason"] and "rows" not in e  # no statistics for what was not computed
    assert syn["summary"]["computed"] == 60 and "dq_is_normalized" in syn["summary"]["constant"]
    assert pay["summary"]["computed"] == 29
    assert {"pair_prior_count", "payer_distinct_payees_prior", "is_reciprocal_pair"} <= set(
        pay["summary"]["constant"]
    )
    assert "pair_share_of_payer_outflow" in pay["summary"]["excessive_missing"]  # every PaySim sender is new
    assert "amount_robust_z" in pay["summary"]["insufficient_history"]
    z = next(e for e in pay["transaction_features"] if e["feature"] == "amount_robust_z")
    assert 0 < z["basis"]["unavailable"] <= z["nulls"]  # rows with no amount/direction have no basis at all
