"""P10 on real imports: label isolation, leakage, reproducibility, PaySim and synthetic experiments, CLI.

The experiments here use a lighter configuration (fewer trees and bootstrap resamples) for test speed; the
acceptance record (docs/p10-acceptance.md) uses the default configuration.
"""

import copy
import json
import re
import shutil
from datetime import date
from pathlib import Path

import pytest

from ledgerspy.cases import store
from ledgerspy.cli import main
from ledgerspy.config import ConfigError, load_config
from ledgerspy.risk import service as risk_service
from ledgerspy.supervised import experiment, service, splits
from tests.backend.conftest import make_ingest
from tests.backend.test_features_datasets import PAYSIM, REFERENCE, case_overrides
from tests.backend.test_forensics_datasets import _table_digest, imported

ROOT = Path(__file__).resolve().parents[2]
GT = REFERENCE / "ground_truth" / "SYNTHETIC_ground_truth.json"
LIGHT = {
    "supervised": {
        "models": {"gradient_boosting": {"n_estimators": 15}},
        "evaluation": {"bootstrap": {"resamples": 40}},
        "permutation_repeats": 1,
    }
}


def light(ws, *extra):
    over = {"workspace": {"path": str(ws)}, **copy.deepcopy(LIGHT)}
    for e in extra:
        for k, v in e.items():
            over[k] = over.get(k, {}) | v if isinstance(v, dict) and isinstance(over.get(k), dict) else v
    return load_config(overrides=over)


@pytest.fixture(scope="module")
def synthetic(tmp_path_factory):
    inputs = tmp_path_factory.mktemp("inputs")
    shutil.copy(REFERENCE / "SYNTHETIC_ledger.csv", inputs / "SYNTHETIC_ledger.csv")
    h = make_ingest(tmp_path_factory.mktemp("ws"))
    h.case_id = store.create_case(
        h.conn, name="syn", actor="t", period_start=date(2023, 4, 1), period_end=date(2026, 3, 31)
    )["case_id"]
    dsid = imported(h, inputs / "SYNTHETIC_ledger.csv")
    cfg = light(h.workspace, case_overrides())
    report = service.evaluate(h.conn, cfg, dsid, "synthetic", GT)
    yield h, dsid, cfg, report
    h.conn.close()


@pytest.fixture(scope="module")
def paysim(tmp_path_factory):
    h = make_ingest(tmp_path_factory.mktemp("ws"))
    dsid = imported(h, PAYSIM)
    cfg = light(h.workspace)
    yield h, dsid, cfg, service.evaluate(h.conn, cfg, dsid, "paysim")
    h.conn.close()


def runs(report, **match):
    return [r for r in report["runs"] if all(r.get(k) == v for k, v in match.items())]


# ------------------------------------------------------------------------------------------ label isolation


def test_only_the_label_adapter_reads_labels_and_no_detector_imports_supervised():
    pkg = ROOT / "ledgerspy"
    readers = sorted(
        str(p.relative_to(pkg)).replace("\\", "/")
        for p in pkg.rglob("*.py")
        if re.search(r"FROM labels", p.read_text(encoding="utf-8"))
    )
    assert readers == ["supervised/labels.py"], readers
    for sub in ("features", "detectors", "forensics", "anomaly", "risk", "api", "normalize", "quality"):
        for p in (pkg / sub).rglob("*.py"):
            assert "ledgerspy.supervised" not in p.read_text(encoding="utf-8"), p
    sup = [p for p in (pkg / "supervised").glob("*.py") if p.name != "labels.py"]
    for p in sup:
        code = p.read_text(encoding="utf-8")
        assert "ground_truth.json" not in code and "FROM labels" not in code, p


def test_no_api_endpoint_or_frontend_exposes_a_fraud_probability():
    routes = "".join(
        p.read_text(encoding="utf-8") for p in (ROOT / "ledgerspy" / "api" / "routes").glob("*.py")
    )
    assert "supervised" not in routes
    fe = "".join(
        p.read_text(encoding="utf-8")
        for p in (ROOT / "frontend").rglob("*.*")
        if p.suffix in (".js", ".html")
    )
    assert not re.search(
        r"(?<!not a )(?<!never a )(fraud probability|probability of fraud)|ML accuracy", fe, re.I
    )


# ---------------------------------------------------------------------------------------------- data contract


def test_contract_features_are_registry_features_without_ids_labels_or_detector_outputs(synthetic):
    report = synthetic[3]
    feats = report["contract"]["features"]
    configured = synthetic[2].settings.supervised.features
    assert [f["feature"] for f in feats] == configured.numeric + configured.categorical
    forbidden = re.compile(
        r"txn_id|source_row|label|fraud|scenario|signal|anomaly|risk|reference$|payee$|payer$"
    )
    for r in report["runs"]:
        for col in r["preprocessing"]["design_columns"]:
            assert not forbidden.search(col), col
    excluded = {f["feature"]: f["reason"] for f in feats if f["status"] == "excluded"}
    assert "txn_type" in excluded  # the synthetic ledger maps no transaction type
    assert all(f["semantics"] in ("row", "point_in_time") for f in feats if f["status"] == "included")


def test_post_period_and_unknown_features_are_rejected(synthetic):
    h, dsid, cfg, _ = synthetic
    bad = light(
        h.workspace,
        case_overrides(),
        {
            "supervised": {
                "features": {
                    "numeric": ["log_amount", "ent_txn_count"],
                    "log_transform": [],
                    "ablations": {"no_simulator_fields": []},
                }
            }
        },
    )
    data, *_ = service.prepare(h.conn, bad, dsid, "synthetic", GT)
    reasons = {f["feature"]: f["reason"] for f in data.contract["features"]}
    assert "not transaction level" in reasons["ent_txn_count"]
    worse = light(
        h.workspace,
        {
            "supervised": {
                "features": {
                    "numeric": ["isFraud"],
                    "log_transform": [],
                    "ablations": {"no_simulator_fields": []},
                }
            }
        },
    )
    with pytest.raises(ConfigError):
        service.prepare(h.conn, worse, dsid, "synthetic", GT)


def test_feature_list_is_deterministic(synthetic):
    h, dsid, cfg, report = synthetic
    data, *_ = service.prepare(h.conn, cfg, dsid, "synthetic", GT)
    assert data.contract == report["contract"]


# ---------------------------------------------------------------------------------------------- leakage


def test_test_labels_cannot_change_the_trained_model_or_threshold(synthetic):
    """Splits are stratified by label (a standard, documented use of labels); once the split is fixed, nothing
    about the test rows' labels can reach the preprocessing, the model, the threshold or the calibration."""
    h, dsid, cfg, _ = synthetic
    data, *_ = service.prepare(h.conn, cfg, dsid, "synthetic", GT)
    settings = cfg.settings
    split = splits.make("group", data, settings.supervised.splits, settings.supervised.seed)
    flipped = copy.deepcopy(data)
    for i in split.parts["test"]:
        flipped.y[i] = 1 - flipped.y[i]
    ident = {"x": 1}
    for model in ("logistic_regression", "gradient_boosting"):
        a = experiment._run_model(data, split, model, "balanced", None, set(), settings, ident, False)
        b = experiment._run_model(flipped, split, model, "balanced", None, set(), settings, ident, False)
        assert a["model_sha256"] == b["model_sha256"] and a["threshold"] == b["threshold"]
        assert a["preprocessing"] == b["preprocessing"] and a["validation"] == b["validation"]
        assert a["calibration"].get("parameters") == b["calibration"].get("parameters")
        assert a["test"]["pr_auc"] != b["test"]["pr_auc"]  # test labels only move the test metrics
    val_flip = copy.deepcopy(data)
    for i in split.parts["validation"]:
        val_flip.y[i] = 1 - val_flip.y[i]
    c = experiment._run_model(
        val_flip, split, "gradient_boosting", "balanced", None, set(), settings, ident, False
    )
    a = experiment._run_model(
        data, split, "gradient_boosting", "balanced", None, set(), settings, ident, False
    )
    assert c["model_sha256"] == a["model_sha256"]  # validation labels never reach the fit either
    assert c["threshold"] == a["threshold"]  # the threshold is a validation score quantile (review capacity)


def test_experiments_do_not_change_p9_or_write_anything(synthetic):
    h, dsid, cfg, _ = synthetic
    risk_service.clear_cache()
    before = risk_service.run_risk(h.conn, cfg, dsid).result_sha256
    digest, changes = _table_digest(h.conn), h.conn.total_changes
    service.evaluate(
        h.conn,
        light(h.workspace, case_overrides(), {"supervised": {"splits": {"strategies": ["group"]}}}),
        dsid,
        "synthetic",
        GT,
    )
    risk_service.clear_cache()
    assert risk_service.run_risk(h.conn, cfg, dsid).result_sha256 == before
    assert _table_digest(h.conn) == digest and h.conn.total_changes == changes


def test_scenario_leakage_is_measured_and_inflates_row_level_splits(synthetic):
    report = synthetic[3]
    by = {s["strategy"]: s for s in report["splits"]}
    assert (
        by["group"]["scenarios_in_train_and_test"] == []
        and by["group"]["counterparties_in_train_and_test"] == 0
    )
    assert "S-R-01" in by["random_stratified"]["scenarios_in_train_and_test"]
    gb = {r["split"]: r["test"]["pr_auc"] for r in runs(report, model="gradient_boosting", imbalance="none")}
    assert gb["random_stratified"] > gb["group"]  # memorised scenario patterns look like skill


# --------------------------------------------------------------------------------------- experiment content


def test_every_run_records_identity_threshold_calibration_and_labels(synthetic):
    report = synthetic[3]
    assert report["note"].startswith("Experimental evaluation") and "never a probability" in report["note"]
    ident = report["identity"]
    for key in (
        "evidence_sha256",
        "data_version",
        "feature_registry_sha256",
        "feature_contract_sha256",
        "labels_sha256",
        "supervised_config_sha256",
        "seed",
    ):
        assert ident[key] is not None, key
    for r in report["runs"]:
        assert r["experiment_id"].startswith("E-") and len(r["model_sha256"]) == 64
        assert (
            r["threshold"]["rule"].startswith("score of the top") and "validation" in r["threshold"]["rule"]
        )
        assert r["calibration"]["status"] in ("calibrated", "not_validated", "not_attempted")
        assert r["calibration"]["fitted_on"] == "validation split"
        t = r["test"]
        assert {"pr_auc", "roc_auc", "at_k", "at_threshold", "confidence_intervals"} <= set(t)
        assert "accuracy" not in json.dumps(r)
    assert {b["baseline"] for b in report["baselines"][0]["baselines"]} >= {
        "prevalence",
        "amount_only",
        "p8_isolation_forest",
        "p9_review_risk",
    }


def test_interpretation_exists_for_both_models(synthetic):
    report = synthetic[3]
    lr = next(r for r in report["runs"] if r["model"] == "logistic_regression" and "interpretation" in r)
    gb = next(r for r in report["runs"] if r["model"] == "gradient_boosting" and "interpretation" in r)
    assert lr["interpretation"]["coefficients"][0]["direction"] in ("raises score", "lowers score")
    assert abs(sum(x["share"] for x in gb["interpretation"]["gain_importance"]) - 1) < 1e-3
    assert "validation" in gb["interpretation"]["permutation_note"]
    assert "not causal" in lr["interpretation"]["caveat"]


def test_reruns_reproduce_the_result_exactly(synthetic):
    h, dsid, cfg, report = synthetic
    again = service.evaluate(h.conn, cfg, dsid, "synthetic", GT)
    assert again["result_sha256"] == report["result_sha256"]
    assert again["run_at"] != report["run_at"] or True  # timestamps are outside the hash


def test_paysim_experiment_reports_proxy_limits_and_ablation(paysim):
    _, _, _, report = paysim
    assert "proxy" in report["labels"]["note"] and report["rows"]["positives"] == 68
    assert any("chronological" in w and "no positives" in w for w in report["warnings"])
    abl = runs(report, ablation="no_simulator_fields")
    assert abl and all(set(r["features_removed"]) == {"txn_type", "is_outflow"} for r in abl)
    assert "type_rate" in {b["baseline"] for b in report["baselines"][0]["baselines"]}
    assert all(r["calibration"]["status"] == "not_attempted" for r in report["runs"])  # too few positives
    assert all("uncalibrated" in r["output_label"] for r in report["runs"])


def test_cli_writes_a_local_experiment_file(paysim, tmp_path, capsys):
    h, dsid, _, _ = paysim
    cfg_text = (ROOT / "config" / "default.yaml").read_text(encoding="utf-8")
    cfg_text = cfg_text.replace("n_estimators: 100, learning_rate", "n_estimators: 5, learning_rate")
    cfg_text = cfg_text.replace("bootstrap: {resamples: 500,", "bootstrap: {resamples: 20,")
    cfg_text = cfg_text.replace(
        "strategies: [random_stratified, chronological, group]", "strategies: [group]"
    )
    cfg_file = tmp_path / "light.yaml"
    cfg_file.write_text(cfg_text, encoding="utf-8")
    rc = main(
        [
            "evaluate",
            "--config",
            str(cfg_file),
            "--workspace",
            str(h.workspace),
            "--dataset-id",
            dsid,
            "--labels",
            "paysim",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0 and "EXPERIMENT (not a fraud probability)" in out
    files = list((h.workspace / "experiments").glob("S-*.json"))
    assert files and json.loads(files[0].read_text(encoding="utf-8"))["kind"].startswith(
        "supervised_evaluation"
    )
    assert (
        main(
            [
                "evaluate",
                "--config",
                str(cfg_file),
                "--workspace",
                str(h.workspace),
                "--dataset-id",
                dsid,
                "--labels",
                "synthetic",
            ]
        )
        == 2
    )  # synthetic labels need an explicit ground-truth file
