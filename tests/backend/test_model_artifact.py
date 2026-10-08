"""P11 units: artefact format, hashing, safe loading, contract compatibility, preprocessing persistence,
batch inference, registry lifecycle and the approval gate.

Artefacts here are built by the real P11 builder (`train.build_artifact`) from a small labelled table whose
columns are real P6 registry features, so every check runs against the production code paths.
"""

import ast
import copy
import dataclasses
import json
import pickle
import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from ledgerspy.cases import audit
from ledgerspy.cases.db import connect, init_db
from ledgerspy.config import load_config
from ledgerspy.features.matrix import registry_sha256
from ledgerspy.features.spec import REGISTRY, Semantics
from ledgerspy.supervised import artifact, experiment, inference, models, registry, splits, train
from ledgerspy.supervised.artifact import ArtifactError
from ledgerspy.supervised.dataset import Frame, TrainingData

ROOT = Path(__file__).resolve().parents[2]
NUMERIC = ["log_amount", "amount_robust_z", "hour_of_day", "is_after_hours"]
SETTINGS = load_config(
    overrides={
        "supervised": {
            "models": {"gradient_boosting": {"n_estimators": 20}},
            "evaluation": {"bootstrap": {"resamples": 20}},
        }
    }
).settings
IDENTITY = {
    "dataset_id": "D-TOY",
    "file": "toy.csv",
    "evidence_sha256": "a" * 64,
    "data_version": "v-toy",
    "transaction_matrix_sha256": "b" * 64,
    "label_source": "toy labels",
    "labels_sha256": "c" * 64,
    "supervised_config_sha256": "d" * 64,
    "feature_contract_sha256": "e" * 64,
    "seed": 42,
}


def toy(n: int = 1500, seed: int = 3) -> TrainingData:
    rng = random.Random(seed)  # noqa: S311 — test data
    y = [int(rng.random() < 0.15) for _ in range(n)]
    hours = [float(rng.randint(0, 23)) for _ in range(n)]
    entries = []
    for f in NUMERIC + ["txn_type"]:
        spec = REGISTRY[f]
        entries.append(
            {
                "feature": f,
                "kind": "categorical" if f == "txn_type" else "numeric",
                "status": "included",
                "reason": "",
                "version": spec.version,
                "semantics": spec.semantics.value,
                "transform": "none",
            }
        )
    entries.append(
        {
            "feature": "cp_count_7d",
            "kind": "numeric",
            "status": "excluded",
            "reason": "not available for this dataset: test",
            "version": 1,
            "semantics": "point_in_time",
            "transform": "none",
        }
    )
    t0 = datetime(2024, 1, 1)
    return TrainingData(
        txn_ids=[f"T-{i:016x}" for i in range(n)],
        y=y,
        timestamps=[t0 + timedelta(hours=i) for i in range(n)],
        entity_groups=[f"out:e{i % 80}" for i in range(n)],
        scenario_groups=[[] for _ in range(n)],
        numeric={
            "log_amount": [rng.gauss(8.0 + 1.2 * v, 1.0) for v in y],
            "amount_robust_z": [None if i % 9 == 0 else rng.gauss(0.8 * v, 1.0) for i, v in enumerate(y)],
            "hour_of_day": hours,
            "is_after_hours": [1.0 if h >= 19 else 0.0 for h in hours],
        },
        categorical={"txn_type": [rng.choice("AB" if v else "ABCC") for v in y]},
        contract={
            "feature_list_version": 1,
            "feature_registry_sha256": registry_sha256(),
            "features": entries,
            "contract_sha256": "f" * 64,
        },
    )


DATA = toy()


def build(model: str, label_source: str = "synthetic", data: TrainingData = DATA) -> dict:
    return train.build_artifact(
        data,
        {},
        IDENTITY,
        {"positives": "toy"},
        SETTINGS,
        label_source,
        model,
        "balanced",
        "random_stratified",
        None,
    )


@pytest.fixture(scope="module")
def docs():
    return {
        "logistic_regression": build("logistic_regression"),
        "gradient_boosting": build("gradient_boosting"),
        "eligible": build("gradient_boosting", "audited_outcomes"),
        "paysim": build("logistic_regression", "paysim"),
    }


def raw(doc: dict) -> bytes:
    return artifact.to_bytes(doc)


def mutate(doc: dict, fn, reseal: bool = True) -> dict:
    d = copy.deepcopy(doc)
    fn(d)
    return artifact.seal(d) if reseal else d


def refused(data: bytes, expected: str | None = None) -> ArtifactError:
    with pytest.raises(ArtifactError) as e:
        artifact.load_bytes(data, expected)
    return e.value


def frame(data: TrainingData = DATA) -> Frame:
    return Frame(list(data.txn_ids), copy.deepcopy(data.numeric), copy.deepcopy(data.categorical))


# ------------------------------------------------------------------------------------------ artefact format


def test_artefacts_are_deterministic(docs):
    for name in ("logistic_regression", "gradient_boosting"):
        again = build(name)
        assert raw(again) == raw(docs[name])
        assert again["integrity"]["artifact_sha256"] == docs[name]["integrity"]["artifact_sha256"]
        loaded = artifact.load_bytes(raw(docs[name]))
        assert raw(loaded.doc) == raw(docs[name])  # parse → serialise is the identity


def test_hash_ignores_key_order_whitespace_and_location(docs, tmp_path):
    doc = docs["gradient_boosting"]
    sha = doc["integrity"]["artifact_sha256"]
    reordered = json.dumps(dict(reversed(list(doc.items()))), indent=7).encode()
    compact = json.dumps(doc, separators=(",", ":"), sort_keys=False).encode()
    for variant in (reordered, compact):
        assert artifact.load_bytes(variant).sha256 == sha
    a = artifact.save(doc, tmp_path / "one")
    b = artifact.save(doc, tmp_path / "elsewhere" / "two")
    assert a.read_bytes() == b.read_bytes() and a.name == b.name == f"{doc['model_id']}.json"
    assert artifact.load_file(b).sha256 == sha
    assert artifact.save(doc, tmp_path / "one") == a  # identical re-save is a no-op


def test_artefact_is_self_describing(docs):
    doc = docs["gradient_boosting"]
    assert doc["model_id"] == "M-" + doc["integrity"]["artifact_sha256"][:16]
    assert set(doc) == {
        "artifact_format",
        "artifact_version",
        "model_id",
        "model",
        "feature_names",
        "feature_contract",
        "preprocessor",
        "threshold_policy",
        "calibration",
        "training",
        "evaluation",
        "model_card",
        "creation",
        "initial_status",
        "integrity",
    }
    assert doc["feature_names"] == NUMERIC + ["txn_type"]
    assert doc["feature_contract"]["feature_registry_sha256"] == registry_sha256()
    assert doc["feature_contract"]["compatibility"]["mode"] == "exact"
    assert doc["feature_contract"]["excluded"] == [
        {"feature": "cp_count_7d", "reason": "not available for this dataset: test"}
    ]
    assert doc["model"]["implementation_version"] == models.IMPLEMENTATION_VERSION
    assert doc["threshold_policy"]["selected_on"] == "validation split"
    assert doc["calibration"]["probability_claim_permitted"] is False
    assert doc["initial_status"] == "experiment"
    assert doc["training"]["training_domain"] == "synthetic_injected_scenarios"
    assert doc["training"]["production_eligible"] is False
    assert set(doc["creation"]) == {"ledgerspy_version", "python", "writer", "timestamp_policy"}  # no clock
    assert "not a fraud probability" in doc["model_card"]["score_semantics"]


# ------------------------------------------------------------------------------------------ round trip


@pytest.mark.parametrize("name", ["logistic_regression", "gradient_boosting"])
def test_train_save_load_infer_reproduces_scores_threshold_and_metadata(docs, name, tmp_path):
    doc = docs[name]
    sc = SETTINGS.supervised
    split = splits.make("random_stratified", DATA, sc.splits, sc.seed)
    rec, pre, model = experiment._run_model(
        DATA, split, name, "balanced", None, set(), SETTINGS, IDENTITY, False, fitted=True
    )
    loaded = artifact.load_file(artifact.save(doc, tmp_path))
    std = name == "logistic_regression"
    rows = list(range(DATA.n))
    assert loaded.preprocessor.transform(DATA, rows, std) == pre.transform(DATA, rows, std)  # identical
    before = model.score(pre.transform(DATA, rows, std))
    after = inference.score_frame(loaded, frame(), 256)
    assert after == before  # bit-identical (documented tolerance: exact)
    t = loaded.doc["threshold_policy"]["value"]
    assert t == rec["threshold"]["value"]
    assert [s >= t for s in after] == [s >= t for s in before]
    assert loaded.doc["model"]["parameters_sha256"] == model.sha256() == rec["model_sha256"]
    assert loaded.doc["preprocessor"]["design_columns"] == pre.names
    assert loaded.doc["evaluation"]["experiment_id"] == rec["experiment_id"]
    assert loaded.doc["calibration"]["status"] == rec["calibration"]["status"]


def test_preprocessing_state_is_persisted_exactly(docs):
    doc = docs["logistic_regression"]
    sc = SETTINGS.supervised
    split = splits.make("random_stratified", DATA, sc.splits, sc.seed)
    _, pre, _ = experiment._run_model(
        DATA, split, "logistic_regression", "balanced", None, set(), SETTINGS, IDENTITY, False, fitted=True
    )
    state = pre.state()
    state["preprocessor_sha256"] = artifact.preprocessor_sha256(state)
    assert doc["preprocessor"] == state  # medians, means, SDs, indicators, categories, order: exact
    names = {e["feature"]: e for e in state["numeric"]}
    assert names["amount_robust_z"]["indicator"] is True  # nulls in training → imputation + indicator
    assert state["categorical"] == [{"feature": "txn_type", "categories": ["A", "B", "C"]}]


def test_inference_population_cannot_change_preprocessing(docs):
    loaded = artifact.load_bytes(raw(docs["logistic_regression"]))
    state = copy.deepcopy(loaded.preprocessor.state())
    base = inference.score_frame(loaded, frame(), 512)
    shifted = frame()
    shifted.numeric["log_amount"] = [v * 10 for v in shifted.numeric["log_amount"]]  # a different population
    shifted.categorical["txn_type"] = ["Z"] * shifted.n  # a category never seen in training
    extra = frame()
    extra.txn_ids += ["T-extra"] * 500
    for col in extra.numeric.values():
        col += [1e9] * 500
    extra.categorical["txn_type"] += ["Z"] * 500
    inference.score_frame(loaded, shifted, 512)
    mixed = inference.score_frame(loaded, extra, 512)
    assert loaded.preprocessor.state() == state  # nothing re-fitted on any inference population
    assert loaded.doc == artifact.load_bytes(raw(docs["logistic_regression"])).doc
    assert mixed[: DATA.n] == base  # a row's score depends on that row only


# ------------------------------------------------------------------------------------------ batch inference


def test_batch_inference_is_deterministic_order_and_partition_invariant(docs):
    for name in ("logistic_regression", "gradient_boosting"):
        loaded = artifact.load_bytes(raw(docs[name]))
        f = frame()
        snapshot = copy.deepcopy(f)
        ref = inference.score_frame(loaded, f, 10_000)
        assert inference.score_frame(loaded, f, 10_000) == ref  # repeated
        for size in (1, 7, 333):
            assert inference.score_frame(loaded, f, size) == ref  # batch partitioning
        perm = list(range(f.n))
        random.Random(9).shuffle(perm)  # noqa: S311
        shuffled = Frame(
            [f.txn_ids[i] for i in perm],
            {k: [v[i] for i in perm] for k, v in f.numeric.items()},
            {k: [v[i] for i in perm] for k, v in f.categorical.items()},
        )
        got = dict(zip(shuffled.txn_ids, inference.score_frame(loaded, shuffled, 100), strict=True))
        assert got == dict(zip(f.txn_ids, ref, strict=True))  # subject-level invariance
        assert inference.scores_sha256(shuffled.txn_ids, list(got.values())) != ""  # defined
        assert inference.scores_sha256(
            shuffled.txn_ids, [got[t] for t in shuffled.txn_ids]
        ) == inference.scores_sha256(f.txn_ids, ref)
        assert f == snapshot  # input not mutated


# ------------------------------------------------------------------------------------------ tampering


def test_tampering_is_detected(docs):
    doc = docs["logistic_regression"]
    sha = doc["integrity"]["artifact_sha256"]
    cases = {
        "coefficient": lambda d: d["model"]["parameters"]["coef"].__setitem__(0, 0.5),
        "threshold": lambda d: d["threshold_policy"].__setitem__("value", 0.01),
        "calibration": lambda d: d["calibration"].__setitem__("output_label", "fraud probability"),
        "domain": lambda d: d["training"].__setitem__("production_eligible", True),
        "model card": lambda d: d["model_card"].__setitem__("intended_use", "production"),
        "model id": lambda d: d.__setitem__("model_id", "M-0000000000000000"),
    }
    for what, fn in cases.items():
        e = refused(raw(mutate(doc, fn, reseal=False)))
        assert e.code == "artifact_integrity_failed", what
    # Re-sealing makes a different, self-consistent artefact; the registry's pinned hash still refuses it.
    resealed = mutate(doc, cases["threshold"])
    assert resealed["model_id"] != doc["model_id"]
    assert refused(raw(resealed), expected=sha).code == "artifact_integrity_failed"
    # A one-byte edit of the file text is caught too.
    text = raw(doc).decode()
    idx = text.index('"value": ') + len('"value": ')
    edited = text[:idx] + ("1" if text[idx] != "1" else "2") + text[idx + 1 :]
    assert refused(edited.encode()).code in (
        "artifact_integrity_failed",
        "artifact_invalid",
        "artifact_malformed",
    )


def test_malformed_artefacts_are_refused(docs, monkeypatch):
    good = raw(docs["logistic_regression"])
    for bad in (
        b"",
        b"not json",
        good[: len(good) // 2],
        b"[1, 2, 3]",
        b'{"artifact_format": "ledgerspy-supervised-model", "artifact_version": 1, "x": NaN}',
        b'{"artifact_format": "a", "artifact_format": "ledgerspy-supervised-model"}',  # duplicate key
        b"\xff\xfe\x00",
        b'{"artifact_format": "something-else", "artifact_version": 1}',
    ):
        assert refused(bad).code == "artifact_malformed", bad[:40]
    monkeypatch.setattr(artifact, "MAX_ARTIFACT_BYTES", 1000)
    assert refused(good).code == "artifact_malformed"


def test_unsupported_versions_are_refused(docs):
    for v in (0, 2, "1", 1.5, True, None):
        e = refused(
            raw(mutate(docs["logistic_regression"], lambda d, v=v: d.__setitem__("artifact_version", v)))
        )
        assert e.code == "artifact_version_unsupported", v


def test_schema_refuses_missing_extra_and_mistyped_fields(docs):
    doc = docs["gradient_boosting"]
    bad = {
        "missing calibration": lambda d: d.pop("calibration"),
        "missing threshold value": lambda d: d["threshold_policy"].pop("value"),
        "extra top-level": lambda d: d.__setitem__("__reduce__", ["os.system", ["echo"]]),
        "extra nested": lambda d: d["model"].__setitem__("loader", "pickle"),
        "string for float": lambda d: d["model"]["parameters"].__setitem__("init", "0.1"),
        "bool for int": lambda d: d["threshold_policy"].__setitem__("validation_rows", True),
        "threshold above 1": lambda d: d["threshold_policy"].__setitem__("value", 1.5),
        "unknown model type": lambda d: d["model"].__setitem__("type", "neural_net"),
        "post-period feature": lambda d: d["feature_contract"]["features"][0].__setitem__(
            "semantics", "post_period"
        ),
        "probability claim": lambda d: d["calibration"].__setitem__("probability_claim_permitted", True),
        "non-experiment start": lambda d: d.__setitem__("initial_status", "approved"),
        "zero SD": lambda d: d["preprocessor"]["numeric"][0].__setitem__("sd", 0.0),
    }
    for what, fn in bad.items():
        assert refused(raw(mutate(doc, fn))).code == "artifact_invalid", what


def test_impossible_or_inconsistent_parameters_are_refused(docs):
    gb, lr = docs["gradient_boosting"], docs["logistic_regression"]

    def first_split(d):
        tree = d["model"]["parameters"]["trees"][0]
        return next(i for i, nd in enumerate(tree) if nd[0] == "split"), tree

    def backward(d):
        i, tree = first_split(d)
        tree[i][3] = i  # a child pointing to itself: a cycle

    def feature_out_of_range(d):
        i, tree = first_split(d)
        tree[i][1] = 999

    def bin_out_of_range(d):
        i, tree = first_split(d)
        tree[i][2] = 999

    bad = {
        "cycle": (gb, backward),
        "input out of range": (gb, feature_out_of_range),
        "bin out of range": (gb, bin_out_of_range),
        "tree count": (gb, lambda d: d["model"]["parameters"]["trees"].pop()),
        "unsorted bin edges": (gb, lambda d: d["model"]["parameters"]["cuts"][0].reverse()),
        "zero trees configured": (gb, lambda d: d["model"]["hyperparameters"].__setitem__("n_estimators", 0)),
        "coefficient count": (lr, lambda d: d["model"]["parameters"]["coef"].append(0.0)),
        "parameters hash": (lr, lambda d: d["model"].__setitem__("parameters_sha256", "0" * 64)),
        "threshold for another model": (
            lr,
            lambda d: d["threshold_policy"].__setitem__("model_parameters_sha256", "0" * 64),
        ),
        "calibration for another model": (
            lr,
            lambda d: d["calibration"].__setitem__("model_parameters_sha256", "0" * 64),
        ),
        "calibrated without parameters": (
            lr,
            lambda d: d["calibration"].update(status="calibrated", parameters=None),
        ),
        "not attempted with parameters": (
            lr,
            lambda d: d["calibration"].update(status="not_attempted", parameters={"a": 1.0, "b": 0.0}),
        ),
        "eligible PaySim": (
            docs["paysim"],
            lambda d: d["training"].__setitem__("production_eligible", True),
        ),
        "domain relabelled": (
            docs["paysim"],
            lambda d: d["training"].__setitem__("training_domain", "audited_outcomes"),
        ),
        "model inputs": (lr, lambda d: d["model"]["input_columns"].reverse()),
    }
    for what, (doc, fn) in bad.items():
        assert refused(raw(mutate(doc, fn))).code in ("artifact_invalid",), what


# ------------------------------------------------------------------------------------------ contract


def _with_contract(doc, fn):
    def apply(d):
        fn(d)
        d["feature_contract"]["contract_sha256"] = artifact.contract_sha256(d["feature_contract"])

    return mutate(doc, apply)


def test_feature_contract_exact_match(docs):
    report = artifact.compatibility(docs["gradient_boosting"])
    assert report == {"compatible": True, "mode": "exact", "problems": [], "warnings": []}
    warn = artifact.compatibility(docs["gradient_boosting"], "0" * 64)
    assert warn["compatible"] and "features configuration" in warn["warnings"][0]


def test_reordered_or_missing_features_are_refused(docs):
    doc = docs["gradient_boosting"]

    def reorder(d):
        f = d["feature_contract"]["features"]
        f[0], f[1] = f[1], f[0]
        d["feature_names"] = [x["feature"] for x in f]

    def drop(d):
        d["feature_contract"]["features"] = [
            x for x in d["feature_contract"]["features"] if x["feature"] != "hour_of_day"
        ]
        d["feature_names"] = [x["feature"] for x in d["feature_contract"]["features"]]

    assert refused(raw(_with_contract(doc, reorder))).code == "artifact_invalid"
    assert refused(raw(mutate(doc, lambda d: d["feature_names"].reverse()))).code == "artifact_invalid"
    assert refused(raw(_with_contract(doc, drop))).code == "artifact_invalid"


def test_registry_or_definition_change_makes_the_artefact_incompatible(docs, monkeypatch):
    doc = docs["gradient_boosting"]
    stale = _with_contract(
        doc, lambda d: d["feature_contract"].__setitem__("feature_registry_sha256", "0" * 64)
    )
    rep = artifact.compatibility(artifact.load_bytes(raw(stale)).doc)
    assert not rep["compatible"] and "feature registry changed" in rep["problems"][0]
    with pytest.raises(ArtifactError) as e:
        artifact.require_compatible(stale)
    assert e.value.code == "artifact_incompatible" and e.value.http_status == 409

    spec = REGISTRY["log_amount"]
    monkeypatch.setitem(REGISTRY, "log_amount", dataclasses.replace(spec, version=spec.version + 1))
    rep = artifact.compatibility(doc)
    assert not rep["compatible"] and any("log_amount: definition changed" in p for p in rep["problems"])
    monkeypatch.setitem(REGISTRY, "log_amount", dataclasses.replace(spec, semantics=Semantics.POST_PERIOD))
    rep = artifact.compatibility(doc)
    assert any("would read future rows" in p for p in rep["problems"])
    monkeypatch.delitem(REGISTRY, "log_amount")
    assert any("not in the running feature registry" in p for p in artifact.compatibility(doc)["problems"])


def test_implementation_version_mismatch_is_refused(docs, monkeypatch):
    monkeypatch.setattr(models, "IMPLEMENTATION_VERSION", models.IMPLEMENTATION_VERSION + 1)
    assert refused(raw(docs["gradient_boosting"])).code == "artifact_incompatible"


def _run(columns: dict, status: dict) -> SimpleNamespace:
    n = len(next(iter(columns.values())))
    return SimpleNamespace(
        transactions=SimpleNamespace(
            subjects=list(DATA.txn_ids[:n]), columns=columns, status=status, source_rows=None
        )
    )


def test_missing_feature_at_inference_is_not_substituted_and_extra_features_are_ignored(docs):
    loaded = artifact.load_bytes(raw(docs["gradient_boosting"]))
    cols = {**DATA.numeric, **DATA.categorical}
    status = {f: {"status": "computed", "reason": ""} for f in cols}
    ref = inference.score_frame(loaded, inference.model_frame(loaded, _run(cols, status)), 500)
    # Extra registry features present in the dataset are never read: same scores.
    extra = _run(cols | {"cp_count_7d": [1] * DATA.n}, status | {"cp_count_7d": {"status": "computed"}})
    assert inference.score_frame(loaded, inference.model_frame(loaded, extra), 500) == ref
    # A feature the model reads that is not computed: not applicable, never imputed or substituted.
    missing = status | {"hour_of_day": {"status": "not_available", "reason": "no timestamps"}}
    with pytest.raises(ArtifactError) as e:
        inference.model_frame(loaded, _run(cols, missing))
    assert e.value.code == "model_not_applicable"
    assert e.value.details["missing_features"] == {"hour_of_day": "no timestamps"}


def test_a_declared_but_dropped_feature_is_allowed_and_never_read(docs):
    # Ablation-style artefact: txn_type is declared in the contract but dropped by the preprocessor.
    doc = train.build_artifact(
        DATA,
        {},
        IDENTITY,
        {},
        load_config(
            overrides={
                "supervised": {
                    "models": {"gradient_boosting": {"n_estimators": 5}},
                    "evaluation": {"bootstrap": {"resamples": 10}},
                    "features": {"ablations": {"no_type": ["txn_type"]}},
                }
            }
        ).settings,
        "synthetic",
        "gradient_boosting",
        "balanced",
        "random_stratified",
        "no_type",
    )
    loaded = artifact.load_bytes(raw(doc))
    assert "txn_type" in loaded.doc["feature_names"] and "txn_type" not in loaded.preprocessor.features
    cols = dict(DATA.numeric)
    status = {f: {"status": "computed"} for f in cols} | {
        "txn_type": {"status": "not_available", "reason": "x"}
    }
    assert (
        len(inference.score_frame(loaded, inference.model_frame(loaded, _run(cols, status)), 100)) == DATA.n
    )


# ------------------------------------------------------------------------------------------ safe load

_EXECUTED = []


def _payload() -> None:
    _EXECUTED.append(True)


class _Evil:
    def __reduce__(self):
        return (_payload, ())


def test_loading_never_executes_code(docs, tmp_path):
    for mod in ("artifact", "registry", "inference", "train"):
        tree = ast.parse((ROOT / "ledgerspy" / "supervised" / f"{mod}.py").read_text(encoding="utf-8"))
        imported = {
            a.name.split(".")[0]
            for n in ast.walk(tree)
            if isinstance(n, ast.Import | ast.ImportFrom)
            for a in n.names
        } | {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        assert not imported & {"pickle", "joblib", "marshal", "shelve", "dill", "importlib", "yaml"}, mod
        calls = {
            n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert not calls & {"eval", "exec", "compile", "__import__"}, mod
    evil = tmp_path / "M-0123456789abcdef.json"
    evil.write_bytes(pickle.dumps(_Evil()))
    with pytest.raises(ArtifactError) as e:
        artifact.load_file(evil)
    assert e.value.code == "artifact_malformed" and not _EXECUTED
    # Code-like text inside a valid artefact is inert data.
    doc = mutate(
        docs["logistic_regression"], lambda d: d["model_card"].__setitem__("name", "__import__('os')")
    )
    assert artifact.load_bytes(raw(doc)).doc["model_card"]["name"] == "__import__('os')"


def test_model_files_are_located_by_validated_id_only(tmp_path):
    for bad in ("../../etc/passwd", "M-0123", "M-0123456789ABCDEF", "M-0123456789abcdef/../x", "", "C:\\x"):
        with pytest.raises(ArtifactError) as e:
            artifact.path_for(tmp_path, bad)
        assert e.value.code == "invalid_model_id"
    assert artifact.path_for(tmp_path, "M-0123456789abcdef") == tmp_path / "M-0123456789abcdef.json"


# ------------------------------------------------------------------------------------------ registry


@pytest.fixture
def reg(tmp_path):
    ws = tmp_path / "ws"
    db = ws / "ledgerspy.db"
    init_db(db, "tester")
    conn = connect(db)
    yield conn, ws
    conn.close()


def test_registration_starts_as_experiment_and_never_activates(reg, docs):
    conn, ws = reg
    out = registry.register(conn, ws, docs["gradient_boosting"], "tester")
    assert out["status"] == out["effective_status"] == "experiment" and not out["active"]
    assert out["already_registered"] is False
    again = registry.register(conn, ws, docs["gradient_boosting"], "tester")
    assert again["already_registered"] is True and len(registry.history(conn, out["model_id"])) == 1
    assert registry.active_model_id(conn) is None
    assert registry.active_status(conn, ws)["status"] == "no_active_model"
    # A file merely present in the models directory is listed as unregistered and cannot be used.
    artifact.save(docs["eligible"], registry.models_dir(ws))
    listing = registry.list_models(conn, ws)
    assert listing["unregistered_files"] == [f"{docs['eligible']['model_id']}.json"]
    with pytest.raises(registry.ModelError) as e:
        inference.infer(conn, None, ws, "D-X", docs["eligible"]["model_id"], experimental=True)
    assert e.value.code == "model_not_found"
    assert inference.infer(conn, None, ws, "D-X")["inference_status"] == "no_active_model"


def test_non_production_domains_stay_experimental(reg, docs):
    conn, ws = reg
    for key in ("gradient_boosting", "paysim"):
        mid = registry.register(conn, ws, docs[key], "tester")["model_id"]
        with pytest.raises(registry.ModelError) as e:
            registry.set_status(conn, ws, mid, "candidate", "tester", "looks promising")
        assert e.value.code == "promotion_refused"
        failed = [c["check"] for c in e.value.details["checks"] if not c["passed"]]
        assert failed == ["training_domain_production_eligible"]
        with pytest.raises(registry.ModelError) as e:
            registry.set_status(conn, ws, mid, "approved", "tester", "skip the queue please")
        assert e.value.code == "model_transition_refused"
        with pytest.raises(registry.ModelError) as e:
            registry.activate(conn, ws, mid, "tester", "please activate it")
        assert e.value.code == "activation_refused"
        assert registry.recorded_status(conn, mid) == "experiment"
        with pytest.raises(registry.ModelError) as e:
            inference.infer(conn, None, ws, "D-X", mid)  # not active: needs an explicit experiment request
        assert e.value.code == "model_not_permitted"
    assert registry.active_model_id(conn) is None


def test_approval_gate_activation_and_retirement(reg, docs):
    conn, ws = reg
    mid = registry.register(conn, ws, docs["eligible"], "tester")["model_id"]
    registry.set_status(conn, ws, mid, "candidate", "tester", "evaluated on audited outcomes")
    with pytest.raises(registry.ModelError) as e:
        registry.set_status(conn, ws, mid, "approved", "tester", "ok")  # reason too short
    assert e.value.code == "approval_refused"
    assert [c["check"] for c in e.value.details["checks"] if not c["passed"]] == ["approval_metadata"]
    with pytest.raises(registry.ModelError):
        registry.activate(conn, ws, mid, "tester", "activate the candidate")  # not approved yet
    out = registry.set_status(conn, ws, mid, "approved", "reviewer", "reviewed the model card and evaluation")
    assert out["status"] == "approved" and not out["active"]
    approval = registry.history(conn, mid)[-1]
    assert approval["details"]["approval"] == {
        "approved_by": "reviewer",
        "reason": "reviewed the model card and evaluation",
    }
    assert all(c["passed"] for c in approval["details"]["gate"])
    assert {c["check"] for c in approval["details"]["gate"]} == {
        "integrity",
        "feature_contract",
        "model_card",
        "evaluation_record",
        "calibration_status_known",
        "training_domain_declared",
        "training_domain_production_eligible",
        "approval_metadata",
    }
    status = registry.activate(conn, ws, mid, "reviewer", "production model for testing")
    assert status["active"] and status["usable"] and registry.active_model_id(conn) == mid
    actions = [r[0] for r in conn.execute("SELECT action FROM audit_log WHERE action LIKE 'model.%'")]
    assert actions == [
        "model.registered",
        "model.status_changed",
        "model.status_changed",
        "model.activated",
    ]
    assert audit.verify(conn).ok
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE model_status_events SET status = 'approved'")  # append-only
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM model_artifacts")

    # Tampering with the file after approval: the model stops verifying; it cannot be (re)activated.
    path = artifact.path_for(registry.models_dir(ws), mid)
    path.chmod(0o644)
    path.write_bytes(path.read_bytes().replace(b'"review_share": 0.02', b'"review_share": 0.03'))
    assert registry.describe(conn, ws, mid)["effective_status"] == "integrity_failed"
    assert registry.active_status(conn, ws)["usable"] is False
    with pytest.raises(ArtifactError):
        inference.infer(conn, None, ws, "D-X")  # the active model no longer verifies: refused
    with pytest.raises(registry.ModelError):
        registry.activate(conn, ws, mid, "reviewer", "re-activate the model")

    registry.set_status(conn, ws, mid, "retired", "reviewer", "superseded")
    assert registry.active_model_id(conn) is None and registry.recorded_status(conn, mid) == "retired"
    with pytest.raises(registry.ModelError) as e:
        registry.set_status(conn, ws, mid, "candidate", "reviewer", "bring it back")
    assert e.value.code == "model_transition_refused"
    with pytest.raises(registry.ModelError) as e:
        inference.infer(conn, None, ws, "D-X", mid, experimental=True)
    assert e.value.code == "model_retired"


def test_incompatible_artefact_is_reported_as_incompatible(reg, docs, monkeypatch):
    conn, ws = reg
    mid = registry.register(conn, ws, docs["gradient_boosting"], "tester")["model_id"]
    monkeypatch.setattr(artifact, "registry_sha256", lambda: "0" * 64)
    d = registry.describe(conn, ws, mid)
    assert d["effective_status"] == "incompatible" and d["status"] == "experiment"
    assert d["compatibility"]["problems"]
    monkeypatch.undo()
    monkeypatch.setattr(models, "IMPLEMENTATION_VERSION", models.IMPLEMENTATION_VERSION + 1)
    assert registry.describe(conn, ws, mid)["effective_status"] == "incompatible"  # implementation changed
