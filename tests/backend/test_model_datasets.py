"""P11 on real imports: persisted P10 models on PaySim and the synthetic ledger, inference, point-in-time
inputs, applicability, P9 isolation, the API and the CLI.

One workspace holds both datasets. Models use the light P10 test configuration (fewer trees and bootstrap
resamples). The production-eligible artefact here is a TEST STAND-IN: the synthetic labels relabelled as
`audited_outcomes` to exercise the approval path end to end; no such labels exist in LedgerSpy.
"""

import dataclasses
import json
import re
import secrets
import shutil
from datetime import date

import pytest
from fastapi.testclient import TestClient

from ledgerspy.api.app import create_app
from ledgerspy.cases import store
from ledgerspy.cases.db import db_path_for
from ledgerspy.cli import main
from ledgerspy.features import matrix
from ledgerspy.features import service as feature_service
from ledgerspy.normalize.view import load_view
from ledgerspy.paths import DEFAULT_CONFIG_PATH
from ledgerspy.risk import service as risk_service
from ledgerspy.supervised import artifact, experiment, inference, metrics, registry, service, splits, train
from ledgerspy.supervised.artifact import ArtifactError
from ledgerspy.supervised.dataset import frame_from_run
from tests.backend.conftest import BASE_URL, PORT, make_ingest
from tests.backend.test_features_datasets import PAYSIM, REFERENCE, case_overrides
from tests.backend.test_forensics_datasets import _table_digest, imported
from tests.backend.test_supervised_datasets import GT, light

PROBABILITY = re.compile(r"probab", re.I)


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    inputs = tmp_path_factory.mktemp("inputs")
    shutil.copy(REFERENCE / "SYNTHETIC_ledger.csv", inputs / "SYNTHETIC_ledger.csv")
    h = make_ingest(tmp_path_factory.mktemp("ws"))
    pay_id = imported(h, PAYSIM)
    h.case_id = store.create_case(
        h.conn, name="syn", actor="t", period_start=date(2023, 4, 1), period_end=date(2026, 3, 31)
    )["case_id"]
    syn_id = imported(h, inputs / "SYNTHETIC_ledger.csv")
    cfg_pay = light(h.workspace, {"server": {"port": PORT}})
    cfg_syn = light(h.workspace, case_overrides(), {"server": {"port": PORT}})
    p9_before = {
        d: risk_service.run_risk(h.conn, c, d).result_sha256
        for d, c in ((pay_id, cfg_pay), (syn_id, cfg_syn))
    }
    digest_before = _table_digest(h.conn)
    pay = service.prepare(h.conn, cfg_pay, pay_id, "paysim")
    syn = service.prepare(h.conn, cfg_syn, syn_id, "synthetic", GT)

    def make(prepared, cfg, source, model, ablation=None, imbalance="balanced"):
        return train.build_artifact(*prepared, cfg.settings, source, model, imbalance, "group", ablation)

    docs = {
        "pay_lr": make(pay, cfg_pay, "paysim", "logistic_regression"),
        "pay_gb": make(pay, cfg_pay, "paysim", "gradient_boosting"),
        "pay_gb_ablation": make(pay, cfg_pay, "paysim", "gradient_boosting", "no_simulator_fields"),
        "syn_gb": make(syn, cfg_syn, "synthetic", "gradient_boosting"),
        "stand_in": make(syn, cfg_syn, "audited_outcomes", "gradient_boosting"),
        "disposable": make(syn, cfg_syn, "synthetic", "logistic_regression", imbalance="none"),
    }
    ids = {k: registry.register(h.conn, h.workspace, d, "tester")["model_id"] for k, d in docs.items()}
    yield {
        "h": h,
        "pay_id": pay_id,
        "syn_id": syn_id,
        "cfg_pay": cfg_pay,
        "cfg_syn": cfg_syn,
        "pay": pay,
        "syn": syn,
        "docs": docs,
        "ids": ids,
        "p9_before": p9_before,
        "digest_before": digest_before,
    }
    h.conn.close()


def infer(env, key, dataset="pay", **kw):
    cfg = env["cfg_pay"] if dataset == "pay" else env["cfg_syn"]
    ds = env["pay_id"] if dataset == "pay" else env["syn_id"]
    return inference.infer(
        env["h"].conn, cfg, env["h"].workspace, ds, env["ids"][key], experimental=True, **kw
    )


def keys_of(obj) -> set[str]:
    if isinstance(obj, dict):
        return set(obj) | {k for v in obj.values() for k in keys_of(v)}
    if isinstance(obj, list):
        return {k for v in obj for k in keys_of(v)}
    return set()


# ------------------------------------------------------------------------------------------ round trip


@pytest.mark.parametrize("key", ["pay_lr", "pay_gb", "syn_gb"])
def test_persisted_p10_models_reproduce_the_in_memory_model(env, key):
    """train → save → load → infer gives the in-memory model's scores and threshold decisions exactly."""
    doc = env["docs"][key]
    prepared, cfg, ds = (
        (env["pay"], env["cfg_pay"], env["pay_id"])
        if key.startswith("pay")
        else (env["syn"], env["cfg_syn"], env["syn_id"])
    )
    data, _, identity, _ = prepared
    sc = cfg.settings.supervised
    split = splits.make("group", data, sc.splits, sc.seed)
    rec, pre, model = experiment._run_model(
        data, split, doc["model"]["type"], "balanced", None, set(), cfg.settings, identity, False, fitted=True
    )
    run = feature_service.build_for_dataset(env["h"].conn, cfg, ds)
    std = doc["model"]["type"] == "logistic_regression"
    f = frame_from_run(run, [e["feature"] for e in pre.numeric], [e["feature"] for e in pre.categorical])
    in_memory = dict(zip(f.txn_ids, model.score(pre.transform(f, list(range(f.n)), std)), strict=True))
    res = infer(env, key, "pay" if key.startswith("pay") else "syn")
    persisted = {r["txn_id"]: r["score"] for r in res["rows"]}
    assert persisted == in_memory  # exact
    t = doc["threshold_policy"]["value"]
    assert t == rec["threshold"]["value"]
    assert all(r["at_or_above_threshold"] == (in_memory[r["txn_id"]] >= t) for r in res["rows"])
    # The artefact's recorded test metrics are reproduced from the persisted model's scores.
    te = split.parts["test"]
    ap = metrics.average_precision([persisted[data.txn_ids[i]] for i in te], [data.y[i] for i in te])
    assert round(ap, 6) == doc["evaluation"]["test"]["pr_auc"]
    assert res["metadata"]["model"]["artifact_sha256"] == doc["integrity"]["artifact_sha256"]


def test_artefacts_from_real_data_are_deterministic(env):
    again = train.build_artifact(
        *env["pay"], env["cfg_pay"].settings, "paysim", "gradient_boosting", "balanced", "group", None
    )
    assert artifact.to_bytes(again) == artifact.to_bytes(env["docs"]["pay_gb"])


def test_paysim_artefacts_are_experimental_and_never_production(env):
    h = env["h"]
    for key in ("pay_lr", "pay_gb", "pay_gb_ablation"):
        doc = env["docs"][key]
        assert doc["training"]["training_domain"] == "paysim_simulation"
        assert doc["training"]["production_eligible"] is False
        d = registry.describe(h.conn, h.workspace, env["ids"][key])
        assert d["status"] == d["effective_status"] == "experiment"
        assert d["label"] == "experimental supervised model"
        with pytest.raises(registry.ModelError):
            registry.set_status(
                h.conn, h.workspace, env["ids"][key], "candidate", "tester", "it beat the baselines"
            )
    assert doc["calibration"]["status"] == "not_attempted"  # too few validation positives
    assert "uncalibrated" in doc["calibration"]["output_label"]


# ------------------------------------------------------------------------------------------ inference


def test_inference_metadata_determinism_and_no_side_effects(env):
    h = env["h"]
    a = infer(env, "pay_gb")
    b = infer(env, "pay_gb")
    md = a["metadata"]
    assert a["rows"] == b["rows"] and md["scores_sha256"] == b["metadata"]["scores_sha256"]  # repeatable
    assert md["inference_id"] == b["metadata"]["inference_id"]
    assert md["inference_status"] == "completed" and md["inference_mode"] == "experimental"
    assert md["label"] == "experimental supervised model"
    assert md["score_type"] == "uncalibrated ranking score"
    assert (
        md["model"]["model_id"] == env["ids"]["pay_gb"] and md["model"]["model_type"] == "gradient_boosting"
    )
    assert md["calibration"]["status"] == "not_attempted"
    assert md["feature_contract"]["feature_registry_sha256"] == matrix.registry_sha256()
    assert md["feature_contract"]["compatibility"] == "exact match"
    assert (
        md["threshold"]["applied"]
        and md["threshold"]["value"] == env["docs"]["pay_gb"]["threshold_policy"]["value"]
    )
    assert md["dataset"]["evidence_sha256"] and md["dataset"]["transaction_matrix_sha256"]
    assert re.fullmatch(r"[0-9a-f]{64}", md["inference_config_sha256"])
    assert md["dataset"]["rows"] == len(a["rows"])
    assert any("training evidence file" in w for w in md["warnings"])  # in-sample warning
    assert not any(PROBABILITY.search(k) for k in keys_of(a))  # no "probability" field anywhere
    assert [r["rank"] for r in a["rows"]] == list(range(1, len(a["rows"]) + 1))
    assert all(x["score"] >= y["score"] for x, y in zip(a["rows"], a["rows"][1:], strict=False))
    without = infer(env, "pay_gb", apply_threshold=False)
    assert "at_or_above_threshold" not in without["rows"][0]
    assert [r["score"] for r in without["rows"]] == [r["score"] for r in a["rows"]]
    # Nothing written to the database; the inference record is local JSON.
    assert _table_digest(h.conn) == env["digest_before"]
    assert (h.workspace / "inference" / f"{md['inference_id']}.json").is_file()
    latest = b["metadata"]  # the same inference ID: the record holds the latest run (timings differ)
    assert inference.read_metadata(h.workspace, md["inference_id"]) == latest
    inference.clear_cache()
    assert inference.read_metadata(h.workspace, md["inference_id"]) == latest  # from disk


def test_inference_reads_no_future_rows(env):
    """Scores of transactions up to a cut-off are identical whether or not later rows exist."""
    h, cfg, ds = env["h"], env["cfg_syn"], env["syn_id"]
    loaded = registry.load(h.conn, h.workspace, env["ids"]["syn_gb"])
    view = load_view(h.conn, ds)
    inp = feature_service.input_from_view(view, cfg, feature_service.p4_issues(h.conn, ds, view.data_version))
    times = sorted(r.ts for r in inp.rows if r.ts is not None)
    cut = times[len(times) // 2]
    past = dataclasses.replace(inp, rows=[r for r in inp.rows if r.ts is None or r.ts <= cut], cache={})
    full_run, past_run = matrix.build(inp), matrix.build(past)
    full = inference.model_frame(loaded, full_run)
    part = inference.model_frame(loaded, past_run)
    a = dict(zip(full.txn_ids, inference.score_frame(loaded, full, 4096), strict=True))
    b = dict(zip(part.txn_ids, inference.score_frame(loaded, part, 4096), strict=True))
    assert len(b) < len(a) and {t: a[t] for t in b} == b
    # Every feature the model reads is a row or point-in-time feature of the running registry.
    assert {f["semantics"] for f in loaded.doc["feature_contract"]["features"]} <= {"row", "point_in_time"}


def test_applicability_across_domains(env):
    # The PaySim model reads txn_type, which the synthetic ledger does not provide: not applicable.
    with pytest.raises(ArtifactError) as e:
        infer(env, "pay_gb", "syn")
    assert e.value.code == "model_not_applicable" and "txn_type" in e.value.details["missing_features"]
    # Without simulator fields it can run, but only as an out-of-domain experiment, loudly labelled.
    res = infer(env, "pay_gb_ablation", "syn")
    w = " ".join(res["metadata"]["warnings"])
    assert "out of domain" in w and "paysim_simulation" in w and "features configuration" in w
    assert res["metadata"]["inference_mode"] == "experimental"


# ------------------------------------------------------------------------------------------ P9 isolation


def test_supervised_models_never_affect_p9(env, monkeypatch):
    h = env["h"]
    # Garbage in the models directory, a tampered registered artefact and inference runs.
    (registry.models_dir(h.workspace) / "M-ffffffffffffffff.json").write_text("{not json", encoding="utf-8")
    path = artifact.path_for(registry.models_dir(h.workspace), env["ids"]["disposable"])
    path.chmod(0o644)
    path.write_bytes(path.read_bytes().replace(b'"review_share": 0.02', b'"review_share": 0.03'))
    assert (
        registry.describe(h.conn, h.workspace, env["ids"]["disposable"])["effective_status"]
        == "integrity_failed"
    )
    with pytest.raises(ArtifactError):
        infer(env, "disposable", "syn")
    infer(env, "syn_gb", "syn")
    listing = registry.list_models(h.conn, h.workspace)
    assert "M-ffffffffffffffff.json" in listing["unregistered_files"]
    risk_service.clear_cache()
    for ds, cfg in ((env["pay_id"], env["cfg_pay"]), (env["syn_id"], env["cfg_syn"])):
        assert risk_service.run_risk(h.conn, cfg, ds).result_sha256 == env["p9_before"][ds]
    # Every artefact incompatible with the running feature registry: supervised refuses, P9 is unchanged.
    with monkeypatch.context() as m:
        m.setattr(artifact, "registry_sha256", lambda: "0" * 64)
        assert (
            registry.describe(h.conn, h.workspace, env["ids"]["syn_gb"])["effective_status"] == "incompatible"
        )
        with pytest.raises(ArtifactError) as e:
            infer(env, "syn_gb", "syn")
        assert e.value.code == "artifact_incompatible"
        risk_service.clear_cache()
        assert (
            risk_service.run_risk(h.conn, env["cfg_syn"], env["syn_id"]).result_sha256
            == env["p9_before"][env["syn_id"]]
        )
    # And with no models directory at all.
    moved = h.workspace / "models_moved"
    registry.models_dir(h.workspace).rename(moved)
    try:
        risk_service.clear_cache()
        assert (
            risk_service.run_risk(h.conn, env["cfg_syn"], env["syn_id"]).result_sha256
            == env["p9_before"][env["syn_id"]]
        )
        assert (
            registry.describe(h.conn, h.workspace, env["ids"]["syn_gb"])["effective_status"]
            == "integrity_failed"
        )
    finally:
        moved.rename(registry.models_dir(h.workspace))
    assert _table_digest(h.conn) == env["digest_before"]


# ------------------------------------------------------------------------------------------ API


@pytest.fixture(scope="module")
def api(env):
    token = secrets.token_urlsafe(32)
    with TestClient(
        create_app(env["cfg_syn"], db_path_for(env["cfg_syn"].settings), token), base_url=BASE_URL
    ) as c:
        yield c, {"Authorization": f"Bearer {token}"}


def test_model_endpoints_require_the_token(api, env):
    c, _ = api
    mid = env["ids"]["syn_gb"]
    for method, path, body in [
        ("get", "/api/models", None),
        ("get", "/api/models/active", None),
        ("get", f"/api/models/{mid}", None),
        (
            "post",
            f"/api/datasets/{env['syn_id']}/supervised/inference",
            {"model_id": mid, "experimental": True},
        ),
        ("get", "/api/supervised/inference/I-0123456789abcdef", None),
    ]:
        r = c.request(method, path, json=body)
        assert r.status_code == 401, path
        r = c.request(method, path, json=body, headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401, path


def test_model_api_lists_inspects_and_runs_experiments(api, env):
    c, auth = api
    url = f"/api/datasets/{env['syn_id']}/supervised/inference"
    listing = c.get("/api/models", headers=auth).json()
    by_id = {m["model_id"]: m for m in listing["models"]}
    assert by_id[env["ids"]["syn_gb"]]["effective_status"] == "experiment"
    assert by_id[env["ids"]["syn_gb"]]["label"] == "experimental supervised model"
    assert by_id[env["ids"]["syn_gb"]]["score_type"] == "uncalibrated ranking score"
    meta = c.get(f"/api/models/{env['ids']['syn_gb']}", headers=auth).json()
    assert (
        "parameters" not in meta["metadata"]["model"]
        and meta["metadata"]["model"]["parameter_counts"]["trees"]
    )
    assert meta["metadata"]["integrity"]["artifact_sha256"] == meta["artifact_sha256"]
    assert meta["history"][0]["status"] == "experiment"
    assert c.get("/api/models/M-0000000000000000", headers=auth).status_code == 404
    assert c.get("/api/models/active", headers=auth).json()["status"] == "no_active_model"
    assert c.post(url, json={}, headers=auth).json()["inference_status"] == "no_active_model"
    r = c.post(url, json={"model_id": env["ids"]["syn_gb"]}, headers=auth)
    assert r.status_code == 409 and r.json()["error"]["code"] == "model_not_permitted"
    r = c.post(url, json={"model_id": env["ids"]["syn_gb"], "experimental": True, "size": 10}, headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["metadata"]["label"] == "experimental supervised model" and len(body["rows"]) == 10
    assert body["total"] == body["metadata"]["dataset"]["rows"]
    assert not any(PROBABILITY.search(k) for k in keys_of(body))
    got = c.get(f"/api/supervised/inference/{body['metadata']['inference_id']}", headers=auth).json()
    assert got == body["metadata"]
    r = c.post(url, json={"model_id": env["ids"]["pay_gb"], "experimental": True}, headers=auth)
    assert r.status_code == 409 and r.json()["error"]["code"] == "model_not_applicable"


def test_model_api_refuses_paths_and_unknown_fields(api, env):
    c, auth = api
    url = f"/api/datasets/{env['syn_id']}/supervised/inference"
    for body in (
        {"path": str(registry.models_dir(env["h"].workspace) / f"{env['ids']['syn_gb']}.json")},
        {"model_id": "../../models/x"},
        {"model_id": "C:\\ws\\models\\M-0123456789abcdef.json"},
        {"model_id": env["ids"]["syn_gb"], "file": "x.json"},
        {"model_id": env["ids"]["syn_gb"], "experimental": "yes"},
        {"size": 501},
    ):
        assert c.post(url, json=body, headers=auth).status_code == 422, body
    for path in (
        "/api/models/..%2F..%2Fetc%2Fpasswd",
        "/api/models/M-0123",
        "/api/supervised/inference/../x",
    ):
        assert c.get(path, headers=auth).status_code in (404, 422), path


def test_production_inference_needs_an_approved_active_model(api, env):
    """End to end with the TEST STAND-IN (synthetic labels declared as audited outcomes)."""
    c, auth = api
    h = env["h"]
    url = f"/api/datasets/{env['syn_id']}/supervised/inference"
    mid = env["ids"]["stand_in"]
    p9 = c.get(f"/api/datasets/{env['syn_id']}/risk", headers=auth).json()["identity"]
    registry.set_status(h.conn, h.workspace, mid, "candidate", "tester", "test stand-in evaluated")
    registry.set_status(
        h.conn, h.workspace, mid, "approved", "reviewer", "test stand-in approved for the test"
    )
    assert (
        c.post(url, json={}, headers=auth).json()["inference_status"] == "no_active_model"
    )  # approved ≠ active
    registry.activate(h.conn, h.workspace, mid, "reviewer", "test stand-in activated")
    try:
        active = c.get("/api/models/active", headers=auth).json()
        assert active["active"] and active["usable"] and active["model_id"] == mid
        res = c.post(url, json={"size": 5}, headers=auth).json()
        assert res["metadata"]["inference_mode"] == "production"
        assert res["metadata"]["label"] == "approved supervised model"
        assert res["metadata"]["score_type"] == "uncalibrated ranking score"
        # The supervised score never appears in, or changes, P9.
        risk = c.get(f"/api/datasets/{env['syn_id']}/risk", headers=auth).json()
        assert risk["identity"]["cache_key"] == p9["cache_key"]
        assert (
            risk_service.run_risk(h.conn, env["cfg_syn"], env["syn_id"]).result_sha256
            == env["p9_before"][env["syn_id"]]
        )
        assert "supervised" not in json.dumps(risk).lower()
    finally:
        registry.deactivate(h.conn, h.workspace, "reviewer", "end of test")
    assert c.post(url, json={}, headers=auth).json()["inference_status"] == "no_active_model"


# ------------------------------------------------------------------------------------------ CLI


def test_cli_model_lifecycle(env, tmp_path, capsys):
    h = env["h"]
    cfg_text = DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
    light_text = cfg_text.replace("n_estimators: 100", "n_estimators: 15").replace(
        "resamples: 500", "resamples: 40"
    )
    assert light_text != cfg_text
    cfg_file = tmp_path / "light.yaml"
    cfg_file.write_text(light_text, encoding="utf-8")
    common = ["--config", str(cfg_file), "--workspace", str(h.workspace)]
    capsys.readouterr()
    assert (
        main(
            [
                "model",
                "train",
                "--dataset-id",
                env["pay_id"],
                "--labels",
                "paysim",
                "--model",
                "logistic_regression",
                "--imbalance",
                "none",
                *common,
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "EXPERIMENT" in out and "not a fraud probability" in out and "production eligible: False" in out
    mid = re.search(r"model (M-[0-9a-f]{16})", out).group(1)
    assert main(["model", "list", *common]) == 0
    assert mid in capsys.readouterr().out
    assert main(["model", "promote", mid, "--reason", "it ranks well", *common]) == 1
    err = capsys.readouterr().err
    assert "[FAIL] training_domain_production_eligible" in err
    assert main(["model", "verify", mid, *common]) == 0
    capsys.readouterr()
    tampered = tmp_path / "tampered.json"
    tampered.write_bytes(
        (registry.models_dir(h.workspace) / f"{mid}.json").read_bytes().replace(b'"seed": 42', b'"seed": 43')
    )
    assert main(["model", "verify", "--file", str(tampered), *common]) == 1
    assert "modified" in capsys.readouterr().err
    assert main(["model", "infer", "--dataset-id", env["pay_id"], "--model-id", mid, *common]) == 1
    capsys.readouterr()
    out_file = tmp_path / "scores.json"
    assert (
        main(
            [
                "model",
                "infer",
                "--dataset-id",
                env["pay_id"],
                "--model-id",
                mid,
                "--experimental",
                "--out",
                str(out_file),
                *common,
            ]
        )
        == 0
    )
    md = json.loads(capsys.readouterr().out)
    assert md["score_type"] == "uncalibrated ranking score" and md["inference_mode"] == "experimental"
    assert len(json.loads(out_file.read_text(encoding="utf-8"))["rows"]) == md["dataset"]["rows"]
    assert main(["model", "retire", mid, "--reason", "test over", *common]) == 0
    capsys.readouterr()
    assert registry.recorded_status(h.conn, mid) == "retired"
