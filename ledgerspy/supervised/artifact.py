"""P11 model artefacts: a self-describing, hash-verified JSON document per fitted supervised model.

Format. One UTF-8 JSON object (`<workspace>/models/M-<16 hex>.json`). No pickle, joblib or any executable
content: loading parses data, validates it against a closed schema and rebuilds the model from numbers. Floats
are written with Python's shortest round-trip repr, so saved parameters are bit-identical when re-loaded.

Integrity. `artifact_sha256` = SHA-256 of the canonical JSON (sorted keys, no whitespace, UTF-8, NaN/Infinity
refused) of every top-level field except `integrity` and `model_id`; `model_id` = "M-" + its first 16 hex
digits. The hash therefore does not depend on key order, whitespace, file name or location. A modified
artefact fails the check. The hash proves integrity, not authorship: anyone can re-seal an edited document,
which is why the registry pins each registered artefact's hash and every use re-checks the file against it.

Load order (`load_bytes`): size limit → strict JSON parse (no NaN/Infinity, no duplicate keys) → format and
version → closed schema (unknown or missing fields, wrong types) → hash → semantic checks (parameters match
the declared model and inputs, preprocessing matches the feature list, threshold and calibration belong to
this model, the training domain policy, the model hash) → rebuild the model. Compatibility with the running
LedgerSpy (`compatibility`) is a separate step because it depends on the current feature registry.

Contract policy (P11: exact contract only): every feature the artefact declares must exist in the current P6
registry with the same version, definition hash, level, kind and safe (row / point-in-time) semantics, under
the same registry hash; the feature list must be in the artefact's order; nothing is renamed, substituted or
dropped. Features the current registry has beyond the artefact's are ignored (never read). A different P6
features configuration (calendars, thresholds) is reported as a warning.
"""

import json
import os
import re
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from ledgerspy.config.schema import GradientBoostingConfig, LogisticRegressionConfig
from ledgerspy.errors import LedgerSpyError
from ledgerspy.features.matrix import registry_sha256
from ledgerspy.features.spec import REGISTRY, Kind, Level, Semantics
from ledgerspy.hashing import canonical_json, sha256_hex
from ledgerspy.supervised import models
from ledgerspy.supervised.dataset import FORBIDDEN, Preprocessor

ARTIFACT_FORMAT = "ledgerspy-supervised-model"
ARTIFACT_VERSION = 1
SUPPORTED_VERSIONS = (1,)
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
MODEL_ID = re.compile(r"^M-[0-9a-f]{16}$")
CANONICALISATION = "canonical JSON: sorted keys, separators (',', ':'), UTF-8, NaN/Infinity refused"
COVERS = "every top-level field except 'integrity' and 'model_id'"
MAX_NODES_PER_TREE = 1 << 12

# Label source → (training domain, eligible for production client-ledger inference, note). A policy in code,
# not configuration: no setting can make a simulator- or scenario-trained model production-eligible.
TRAINING_DOMAINS: dict[str, tuple[str, bool, str]] = {
    "paysim": (
        "paysim_simulation",
        False,
        "PaySim mobile-money simulation: a development proxy whose fraud is defined by the simulator; "
        "never "
        "a basis for scoring client ledgers.",
    ),
    "synthetic": (
        "synthetic_injected_scenarios",
        False,
        "LedgerSpy P5 synthetic company with injected scenarios: for development and testing only.",
    ),
    "audited_outcomes": (
        "audited_outcomes",
        True,
        "Confirmed outcomes of audits in the target domain. No adapter produces these labels yet.",
    ),
}


class ArtifactError(LedgerSpyError):
    """An artefact that cannot be used: malformed, unsupported, tampered, invalid or incompatible."""

    def __init__(
        self, code: str, message: str, details: dict[str, Any] | None = None, http_status: int = 422
    ):
        super().__init__(code, message, details, http_status)


# ------------------------------------------------------------------------------------------------ schema

Sha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
FeatureName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
Text = Annotated[str, StringConstraints(min_length=1, max_length=4000)]
Label = Annotated[str, StringConstraints(pattern=r"^[^\x00-\x1f\x7f]{1,200}$")]  # column / category names
Finite = Annotated[float, Field(allow_inf_nan=False)]
Index = Annotated[int, Field(ge=0)]
Json = Any  # descriptive blocks (split, metrics): data only, never interpreted as code


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class LogisticParameters(_Strict):
    intercept: Finite
    coef: list[Finite]


Leaf = tuple[Literal["leaf"], Finite]
Split = tuple[Literal["split"], Index, Index, Index, Index, Finite]


class BoostingParameters(_Strict):
    init: Finite
    cuts: list[list[Finite]]
    trees: list[Annotated[list[Leaf | Split], Field(min_length=1, max_length=MAX_NODES_PER_TREE)]]


class ModelBlock(_Strict):
    type: Literal["logistic_regression", "gradient_boosting"]
    implementation: Literal["ledgerspy.supervised.models (pure Python)"]
    implementation_version: int
    hyperparameters: dict[str, int | float]
    parameters: LogisticParameters | BoostingParameters
    parameters_sha256: Sha
    input_columns: list[Label] = Field(min_length=1)
    input_standardised: bool
    output: Text
    fit: dict[str, int | float | bool]


class FeatureEntry(_Strict):
    feature: FeatureName
    kind: Literal["numeric", "categorical"]
    version: int = Field(ge=1)
    definition_sha256: Sha
    level: Literal["transaction"]
    semantics: Literal["row", "point_in_time"]
    transform: Literal["none", "signed_log1p"]


class ExcludedFeature(_Strict):
    feature: FeatureName
    reason: Text


class CompatibilityPolicy(_Strict):
    mode: Literal["exact"]
    missing_feature: Literal["reject"]
    reordered_features: Literal["reject"]
    changed_feature_definition: Literal["reject"]
    registry_change: Literal["reject"]
    extra_features: Literal["ignore (never read)"]
    features_config_change: Literal["warn"]


class FeatureContract(_Strict):
    contract_version: int = Field(ge=1)
    contract_sha256: Sha
    feature_registry_sha256: Sha
    features_config_sha256: Sha
    features: list[FeatureEntry] = Field(min_length=1)
    excluded: list[ExcludedFeature]
    compatibility: CompatibilityPolicy


class NumericStep(_Strict):
    feature: FeatureName
    transform: Literal["none", "signed_log1p"]
    median: Finite
    mean: Finite
    sd: Annotated[float, Field(gt=0.0, allow_inf_nan=False)]
    indicator: bool


class CategoricalStep(_Strict):
    feature: FeatureName
    categories: list[Label] = Field(min_length=2)


class DroppedFeature(_Strict):
    feature: FeatureName
    reason: Text


class PreprocessorBlock(_Strict):
    fitted_on: Literal["training split only"]
    numeric: list[NumericStep]
    categorical: list[CategoricalStep]
    dropped: list[DroppedFeature]
    design_columns: list[Label] = Field(min_length=1)
    preprocessor_sha256: Sha


class ThresholdPolicy(_Strict):
    rule: Text
    selected_on: Literal["validation split"]
    review_share: Annotated[float, Field(gt=0.0, lt=0.5)]
    value: Annotated[float, Field(ge=0.0, le=1.0)]
    validation_rows: int = Field(ge=1)
    validation_top_k: int = Field(ge=1)
    model_parameters_sha256: Sha


class PlattParameters(_Strict):
    a: Finite
    b: Finite


class CalibrationBlock(_Strict):
    status: Literal["not_attempted", "calibrated", "not_validated"]
    method: Literal["platt"]
    fitted_on: Literal["validation split"]
    parameters: PlattParameters | None
    output_label: Text
    validation_positives: int = Field(ge=0)
    min_validation_positives: int = Field(ge=1)
    max_ece: Annotated[float, Field(gt=0.0, lt=1.0)]
    test_ece_uncalibrated: Finite | None
    test_ece_calibrated: Finite | None
    probability_claim_permitted: Literal[False]
    model_parameters_sha256: Sha


class TrainingBlock(_Strict):
    dataset_id: Text
    source_file: Text
    evidence_sha256: Sha
    data_version: Text
    transaction_matrix_sha256: Sha
    label_source: Literal["paysim", "synthetic", "audited_outcomes"]
    labels_sha256: Sha
    training_domain: Literal["paysim_simulation", "synthetic_injected_scenarios", "audited_outcomes"]
    production_eligible: bool
    domain_note: Text
    split: dict[str, Json]
    imbalance: Text
    ablation: Text | None
    features_removed: list[FeatureName]
    seed: int
    supervised_config_sha256: Sha
    p10_contract_sha256: Sha


class EvaluationBlock(_Strict):
    experiment_id: Annotated[str, StringConstraints(pattern=r"^E-[0-9a-f]{16}$")]
    evaluated_on: Literal["test split (once)"]
    test: dict[str, Json]
    validation: dict[str, Json]
    baselines_on_same_test: list[dict[str, Json]]
    warnings: list[Text]


class ModelCard(_Strict):
    name: Text
    intended_use: Text
    not_intended_for: list[Text] = Field(min_length=1)
    training_domain: Text
    score_semantics: Text
    limitations: list[Text] = Field(min_length=1)
    documentation: Text


class CreationBlock(_Strict):
    ledgerspy_version: Text
    python: Text
    writer: Text
    timestamp_policy: Text


class IntegrityBlock(_Strict):
    algorithm: Literal["sha256"]
    canonicalisation: Literal[
        "canonical JSON: sorted keys, separators (',', ':'), UTF-8, NaN/Infinity refused"
    ]
    covers: Literal["every top-level field except 'integrity' and 'model_id'"]
    artifact_sha256: Sha


class Artifact(_Strict):
    artifact_format: Literal["ledgerspy-supervised-model"]
    artifact_version: Literal[1]
    model_id: Annotated[str, StringConstraints(pattern=r"^M-[0-9a-f]{16}$")]
    model: ModelBlock
    feature_names: list[FeatureName] = Field(min_length=1)
    feature_contract: FeatureContract
    preprocessor: PreprocessorBlock
    threshold_policy: ThresholdPolicy
    calibration: CalibrationBlock
    training: TrainingBlock
    evaluation: EvaluationBlock
    model_card: ModelCard
    creation: CreationBlock
    initial_status: Literal["experiment"]
    integrity: IntegrityBlock


# ------------------------------------------------------------------------------------------------ hashing


def content_sha256(doc: dict[str, Any]) -> str:
    """The artefact hash: canonical JSON of everything except `integrity` and `model_id`."""
    body = {k: v for k, v in doc.items() if k not in ("integrity", "model_id")}
    try:
        return sha256_hex(canonical_json(body))
    except ValueError as exc:  # NaN / Infinity
        raise ArtifactError("artifact_invalid", f"artefact contains a non-finite number: {exc}") from exc


def seal(doc: dict[str, Any]) -> dict[str, Any]:
    """Set `integrity` and `model_id` from the content hash (returns a new document)."""
    out = {k: v for k, v in doc.items() if k not in ("integrity", "model_id")}
    h = content_sha256(out)
    out["model_id"] = "M-" + h[:16]
    out["integrity"] = {
        "algorithm": "sha256",
        "canonicalisation": CANONICALISATION,
        "covers": COVERS,
        "artifact_sha256": h,
    }
    return out


def to_bytes(doc: dict[str, Any]) -> bytes:
    """The file form: deterministic (sorted keys, fixed indentation), so equal artefacts are equal files."""
    return (json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode()


def contract_sha256(contract: dict[str, Any]) -> str:
    return sha256_hex(
        canonical_json(
            {
                "contract_version": contract["contract_version"],
                "feature_registry_sha256": contract["feature_registry_sha256"],
                "features": contract["features"],
            }
        )
    )


def preprocessor_sha256(state: dict[str, Any]) -> str:
    return sha256_hex(canonical_json({k: v for k, v in state.items() if k != "preprocessor_sha256"}))


# ------------------------------------------------------------------------------------------------ loading


def _no_constants(name: str) -> Any:
    raise ValueError(f"{name} is not allowed in an artefact")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"duplicate key {k!r}")
        out[k] = v
    return out


def _parse(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise ArtifactError("artifact_malformed", f"artefact larger than {MAX_ARTIFACT_BYTES} bytes")
    try:
        doc = json.loads(raw.decode("utf-8"), parse_constant=_no_constants, object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ArtifactError("artifact_malformed", f"not a valid JSON artefact: {exc}") from exc
    if not isinstance(doc, dict):
        raise ArtifactError("artifact_malformed", "an artefact is a JSON object")
    return doc


@dataclass
class LoadedArtifact:
    doc: dict[str, Any]
    model: models.LogisticRegression | models.GradientBoosting
    preprocessor: Preprocessor
    load_seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def model_id(self) -> str:
        return self.doc["model_id"]

    @property
    def sha256(self) -> str:
        return self.doc["integrity"]["artifact_sha256"]

    @property
    def model_type(self) -> str:
        return self.doc["model"]["type"]

    def score(self, cols: list[list[float]]) -> list[float]:
        return self.model.score(cols)


def load_bytes(raw: bytes, expected_sha256: str | None = None) -> LoadedArtifact:
    """Parse, validate, verify and rebuild an artefact. Raises ArtifactError; never executes content."""
    t0 = time.perf_counter()
    doc = _parse(raw)
    if doc.get("artifact_format") != ARTIFACT_FORMAT:
        raise ArtifactError("artifact_malformed", "not a LedgerSpy supervised-model artefact")
    version = doc.get("artifact_version")
    if version not in SUPPORTED_VERSIONS or isinstance(version, bool):
        raise ArtifactError(
            "artifact_version_unsupported",
            f"artefact version {version!r} is not supported (supported: {list(SUPPORTED_VERSIONS)})",
        )
    try:
        Artifact.model_validate_json(raw, strict=True)
    except ValidationError as exc:
        problems = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:20]]
        raise ArtifactError(
            "artifact_invalid", "artefact does not match the schema", {"problems": problems}
        ) from exc
    h = content_sha256(doc)
    if h != doc["integrity"]["artifact_sha256"] or doc["model_id"] != "M-" + h[:16]:
        raise ArtifactError(
            "artifact_integrity_failed",
            "artefact hash mismatch: the content was modified after it was sealed",
            {"recorded": doc["integrity"]["artifact_sha256"], "computed": h},
        )
    if expected_sha256 is not None and h != expected_sha256:
        raise ArtifactError(
            "artifact_integrity_failed",
            "artefact is not the one registered under this model ID",
            {"registered": expected_sha256, "computed": h},
        )
    model, pre = _check_semantics(doc)
    return LoadedArtifact(doc, model, pre, round(time.perf_counter() - t0, 4))


def load_file(path: Path, expected_sha256: str | None = None) -> LoadedArtifact:
    try:
        size = path.stat().st_size
        if size > MAX_ARTIFACT_BYTES:
            raise ArtifactError("artifact_malformed", f"artefact larger than {MAX_ARTIFACT_BYTES} bytes")
        raw = path.read_bytes()
    except OSError as exc:
        raise ArtifactError(
            "artifact_missing", f"cannot read the artefact file: {exc.strerror}", http_status=404
        ) from exc
    return load_bytes(raw, expected_sha256)


def _invalid(msg: str, **details: Any) -> ArtifactError:
    return ArtifactError("artifact_invalid", msg, details or None)


def _check_tree(nodes: list[tuple], d: int, cuts: list[list[float]], max_depth: int) -> None:
    parents = [0] * len(nodes)
    depth = [0] * len(nodes)
    for i, nd in enumerate(nodes):
        if nd[0] == "leaf":
            continue
        _, f, b, left, right, gain = nd
        if not (i < left < len(nodes) and i < right < len(nodes) and left != right):
            raise _invalid("tree node children must point forward inside the tree", node=i)
        if f >= d:
            raise _invalid("tree split on an input the model does not have", node=i, input=f)
        if b >= len(cuts[f]):
            raise _invalid("tree split bin outside the input's bins", node=i, bin=b)
        if gain < 0:
            raise _invalid("negative split gain", node=i)
        for c in (left, right):
            parents[c] += 1
            depth[c] = depth[i] + 1
            if depth[c] > max_depth:
                raise _invalid("tree deeper than max_depth", node=c)
    if any(parents[i] != 1 for i in range(1, len(nodes))):
        raise _invalid("tree nodes must form a single tree (every node except the root has one parent)")


def _check_semantics(doc: dict[str, Any]) -> tuple[Any, Preprocessor]:
    m = doc["model"]
    if m["implementation_version"] != models.IMPLEMENTATION_VERSION:
        raise ArtifactError(
            "artifact_incompatible",
            f"model implementation version {m['implementation_version']} is not the running version "
            f"{models.IMPLEMENTATION_VERSION}",
        )
    hyper_cls = LogisticRegressionConfig if m["type"] == "logistic_regression" else GradientBoostingConfig
    try:
        hyper = hyper_cls.model_validate(m["hyperparameters"]).model_dump()
    except ValidationError as exc:
        raise _invalid("impossible hyperparameters", problems=[e["msg"] for e in exc.errors()]) from exc
    p = m["parameters"]
    d = len(m["input_columns"])
    if m["type"] == "logistic_regression":
        if set(p) != {"intercept", "coef"}:
            raise _invalid("logistic-regression parameters must be intercept and coef")
        if len(p["coef"]) != d:
            raise _invalid("coefficient count does not match the model inputs")
        if m["input_standardised"] is not True:
            raise _invalid("logistic regression reads standardised inputs")
    else:
        if set(p) != {"init", "cuts", "trees"}:
            raise _invalid("gradient-boosting parameters must be init, cuts and trees")
        if len(p["cuts"]) != d:
            raise _invalid("bin-edge count does not match the model inputs")
        for cut in p["cuts"]:
            if len(cut) > hyper["max_bins"] - 1 or any(a >= b for a, b in zip(cut, cut[1:], strict=False)):
                raise _invalid("bin edges must be strictly increasing and at most max_bins − 1")
        if len(p["trees"]) != hyper["n_estimators"]:
            raise _invalid("tree count does not match n_estimators")
        for t in p["trees"]:
            _check_tree([tuple(nd) for nd in t], d, p["cuts"], hyper["max_depth"])
        if m["input_standardised"] is not False:
            raise _invalid("the tree model reads unstandardised inputs")
    model = models.CLASSES[m["type"]].from_parameters(hyper, p)
    if model.sha256() != m["parameters_sha256"]:
        raise _invalid("model parameters do not match parameters_sha256")

    # Feature list ↔ contract ↔ preprocessor ↔ model inputs.
    contract = doc["feature_contract"]
    names = doc["feature_names"]
    if names != [f["feature"] for f in contract["features"]]:
        raise _invalid("feature_names must equal the contract's features, in order")
    if len(set(names)) != len(names):
        raise _invalid("a feature is listed twice")
    for f in names:
        if FORBIDDEN.search(f):
            raise _invalid("identifier / label / detector-output names are never features", feature=f)
    if contract_sha256(contract) != contract["contract_sha256"]:
        raise _invalid("feature contract does not match contract_sha256")
    pre_state = doc["preprocessor"]
    if preprocessor_sha256(pre_state) != pre_state["preprocessor_sha256"]:
        raise _invalid("preprocessor state does not match preprocessor_sha256")
    pre = Preprocessor.from_state(pre_state)
    kinds = {f["feature"]: f for f in contract["features"]}
    used = pre.features
    dropped = [x["feature"] for x in pre_state["dropped"]]
    if len(set(used)) != len(used) or set(used) & set(dropped):
        raise _invalid("a feature is used twice or both used and dropped")
    if sorted(used + dropped) != sorted(names):
        raise _invalid("the preprocessor must use or drop exactly the declared features")
    if used != [n for n in names if n in set(used)]:
        raise _invalid("preprocessor features are not in the declared feature order")
    for e in pre.numeric:
        if kinds[e["feature"]]["kind"] != "numeric" or kinds[e["feature"]]["transform"] != e["transform"]:
            raise _invalid("numeric step does not match the contract", feature=e["feature"])
    for e in pre.categorical:
        if kinds[e["feature"]]["kind"] != "categorical":
            raise _invalid("categorical step does not match the contract", feature=e["feature"])
        if e["categories"] != sorted(set(e["categories"])):
            raise _invalid("categories must be sorted and unique", feature=e["feature"])
    if pre.names != pre_state["design_columns"] or pre.names != m["input_columns"]:
        raise _invalid("design columns do not match the model inputs")
    if model.n_inputs != d:
        raise _invalid("model input count mismatch")

    # Threshold and calibration belong to this model.
    psha = m["parameters_sha256"]
    thr = doc["threshold_policy"]
    if thr["model_parameters_sha256"] != psha:
        raise _invalid("the threshold was not selected for this model")
    if thr["validation_top_k"] > thr["validation_rows"]:
        raise _invalid("threshold top-k exceeds the validation rows")
    cal = doc["calibration"]
    if cal["model_parameters_sha256"] != psha:
        raise _invalid("the calibration was not fitted for this model")
    if (cal["status"] == "not_attempted") != (cal["parameters"] is None):
        raise _invalid("calibration parameters must exist exactly when calibration was attempted")
    if cal["status"] == "not_attempted" and cal["validation_positives"] >= cal["min_validation_positives"]:
        raise _invalid("calibration marked not attempted although enough validation positives existed")
    if cal["status"] != "not_attempted" and cal["validation_positives"] < cal["min_validation_positives"]:
        raise _invalid("calibration fitted on fewer validation positives than the minimum")
    if cal["status"] == "calibrated" and not (
        cal["test_ece_calibrated"] is not None and cal["test_ece_calibrated"] <= cal["max_ece"]
    ):
        raise _invalid("calibration marked calibrated without a test ECE within the limit")
    if cal["status"] == "not_validated" and not (
        cal["test_ece_calibrated"] is not None and cal["test_ece_calibrated"] > cal["max_ece"]
    ):
        raise _invalid("calibration marked not validated although its test ECE is within the limit")

    # Training domain policy.
    tr = doc["training"]
    domain, eligible, _ = TRAINING_DOMAINS[tr["label_source"]]
    if tr["training_domain"] != domain or tr["production_eligible"] != eligible:
        raise _invalid("training domain / production eligibility do not match the label source policy")
    if doc["model_card"]["training_domain"] != domain:
        raise _invalid("model card training domain does not match the training block")
    return model, pre


# ---------------------------------------------------------------------------------------------- compatibility


def compatibility(doc: dict[str, Any], features_config_sha256: str | None = None) -> dict[str, Any]:
    """Exact-contract check against the running feature registry. {compatible, problems, warnings}."""
    problems: list[str] = []
    warnings: list[str] = []
    contract = doc["feature_contract"]
    current = registry_sha256()
    if contract["feature_registry_sha256"] != current:
        problems.append(
            f"feature registry changed (artefact {contract['feature_registry_sha256'][:12]}…, "
            f"running {current[:12]}…)"
        )
    for f in contract["features"]:
        spec = REGISTRY.get(f["feature"])
        if spec is None:
            problems.append(f"{f['feature']}: not in the running feature registry")
            continue
        if spec.status != "implemented":
            problems.append(f"{f['feature']}: not implemented in the running registry")
            continue
        if spec.version != f["version"] or spec.definition_sha256() != f["definition_sha256"]:
            problems.append(
                f"{f['feature']}: definition changed (artefact v{f['version']}, running v{spec.version})"
            )
        if spec.level != Level.TRANSACTION:
            problems.append(f"{f['feature']}: not a transaction-level feature")
        if spec.semantics not in (Semantics.ROW, Semantics.POINT_IN_TIME):
            problems.append(f"{f['feature']}: {spec.semantics.value} semantics would read future rows")
        numeric = spec.kind in (Kind.FLOAT, Kind.INT, Kind.BOOL)
        if (f["kind"] == "numeric") != numeric or (f["kind"] == "categorical") != (
            spec.kind == Kind.CATEGORY
        ):
            problems.append(f"{f['feature']}: kind changed to {spec.kind.value}")
        if spec.semantics.value != f["semantics"]:
            problems.append(f"{f['feature']}: semantics changed to {spec.semantics.value}")
    if features_config_sha256 is not None and features_config_sha256 != contract["features_config_sha256"]:
        warnings.append(
            "the P6 features configuration (calendars, thresholds, windows) differs from training; feature "
            "definitions are unchanged but some values are computed with different settings"
        )
    return {"compatible": not problems, "mode": "exact", "problems": problems, "warnings": warnings}


def require_compatible(doc: dict[str, Any], features_config_sha256: str | None = None) -> dict[str, Any]:
    report = compatibility(doc, features_config_sha256)
    if not report["compatible"]:
        raise ArtifactError(
            "artifact_incompatible",
            "the model's feature contract is incompatible with this LedgerSpy feature registry",
            {"problems": report["problems"]},
            http_status=409,
        )
    return report


# ------------------------------------------------------------------------------------------------ files


def path_for(models_dir: Path, model_id: str) -> Path:
    """The only way a model file is located: <models_dir>/<model_id>.json, model_id validated."""
    if not MODEL_ID.fullmatch(model_id or ""):
        raise ArtifactError("invalid_model_id", "a model ID is M- followed by 16 hex digits", http_status=422)
    return models_dir / f"{model_id}.json"


def save(doc: dict[str, Any], models_dir: Path) -> Path:
    """Write the sealed artefact (read-only) as <models_dir>/<model_id>.json; identical re-saves: no-op."""
    load_bytes(to_bytes(doc))  # never write something the loader would refuse
    models_dir.mkdir(parents=True, exist_ok=True)
    path = path_for(models_dir, doc["model_id"])
    data = to_bytes(doc)
    if path.exists():
        if path.read_bytes() == data:
            return path
        raise ArtifactError(
            "artifact_conflict", f"{path.name} exists with different content", http_status=409
        )
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    path.chmod(stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
    return path
