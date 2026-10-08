"""Train one supervised model the P10 way and package it as a P11 artefact (`ledgerspy model train`).

The procedure is exactly one P10 experiment run: build the label-free pipeline and the training data (labels
only through the label adapter), split, fit the preprocessor and the model on the training split, select the
threshold and fit calibration on the validation split, evaluate once on the test split, and compare with the
label-free baselines on the same test rows. The fitted state is then sealed into an artefact whose initial
status is `experiment`. Training never activates a model.
"""

import platform
import sqlite3
from pathlib import Path
from typing import Any

from ledgerspy import __version__
from ledgerspy.config.loader import ConfigError, EffectiveConfig
from ledgerspy.features.spec import REGISTRY
from ledgerspy.hashing import canonical_json, sha256_hex
from ledgerspy.supervised import artifact, experiment, models, service, splits

WRITER = "ledgerspy model train"
SCORE_SEMANTICS = (
    "An uncalibrated ranking score in (0, 1): higher means more similar to the training positives. It is "
    "not "
    "a fraud probability and does not change the P9 review-risk score."
)


def _contract(data, removed: set[str], settings) -> dict[str, Any]:
    feats = []
    excluded = []
    for e in data.contract["features"]:
        if e["status"] != "included":
            excluded.append({"feature": e["feature"], "reason": e["reason"]})
            continue
        spec = REGISTRY[e["feature"]]
        feats.append(
            {
                "feature": e["feature"],
                "kind": e["kind"],
                "version": spec.version,
                "definition_sha256": spec.definition_sha256(),
                "level": spec.level.value,
                "semantics": spec.semantics.value,
                "transform": e["transform"],
            }
        )
    c = {
        "contract_version": data.contract["feature_list_version"],
        "feature_registry_sha256": data.contract["feature_registry_sha256"],
        "features_config_sha256": sha256_hex(canonical_json(settings.features.model_dump(mode="json"))),
        "features": feats,
        "excluded": excluded,
        "compatibility": {
            "mode": "exact",
            "missing_feature": "reject",
            "reordered_features": "reject",
            "changed_feature_definition": "reject",
            "registry_change": "reject",
            "extra_features": "ignore (never read)",
            "features_config_change": "warn",
        },
    }
    c["contract_sha256"] = artifact.contract_sha256(c)
    return c


def build_artifact(
    data,
    label_free: dict[str, list[float | None]],
    identity: dict[str, Any],
    label_info: dict[str, Any],
    settings,
    label_source: str,
    model_name: str,
    imbalance: str,
    strategy: str,
    ablation: str | None,
) -> dict[str, Any]:
    """Fit on train, threshold / calibrate on validation, evaluate on test, and seal the artefact."""
    sc = settings.supervised
    removed: set[str] = set()
    if ablation is not None:
        if ablation not in sc.features.ablations:
            raise ConfigError(f"unknown ablation {ablation!r} (configured: {sorted(sc.features.ablations)})")
        removed = set(sc.features.ablations[ablation])
        if not removed <= set(data.numeric) | set(data.categorical):
            raise ConfigError(f"ablation {ablation!r} removes features this dataset does not have")
    split = splits.make(strategy, data, sc.splits, sc.seed)
    desc = split.describe(data)
    if not all(desc["positives"][p] for p in splits.PARTS):
        raise ConfigError(
            f"{strategy}: a split has no positives ({desc['positives']}); no model can be trained"
        )
    rec, pre, model = experiment._run_model(
        data, split, model_name, imbalance, ablation, removed, settings, identity, False, fitted=True
    )
    rec.pop("_seconds", None)
    k = sc.evaluation.top_k[-1]
    baselines = [
        {
            "baseline": b["baseline"],
            "pr_auc": b["test"]["pr_auc"],
            "roc_auc": b["test"]["roc_auc"],
            f"precision@{k}": b["test"]["at_k"][str(k)]["precision"],
            f"recall@{k}": b["test"]["at_k"][str(k)]["recall"],
        }
        for b in experiment._baselines(data, split, label_free, settings)
    ]
    test = rec["test"]
    warnings = []
    if test["positives"] < 20:
        warnings.append(f"only {test['positives']} test positives: metrics are highly uncertain")
    domain, eligible, note = artifact.TRAINING_DOMAINS[label_source]
    cal = rec["calibration"]
    psha = model.sha256()
    pre_state = pre.state()
    pre_state["preprocessor_sha256"] = artifact.preprocessor_sha256(pre_state)
    contract = _contract(data, removed, settings)
    doc = {
        "artifact_format": artifact.ARTIFACT_FORMAT,
        "artifact_version": artifact.ARTIFACT_VERSION,
        "model": {
            "type": model_name,
            "implementation": models.IMPLEMENTATION,
            "implementation_version": models.IMPLEMENTATION_VERSION,
            "hyperparameters": rec["hyperparameters"],
            "parameters": model.parameters(),
            "parameters_sha256": psha,
            "input_columns": pre.names,
            "input_standardised": model_name == "logistic_regression",
            "output": "sigmoid of the model's raw score: " + SCORE_SEMANTICS,
            "fit": experiment._rnd(model.describe()),
        },
        "feature_names": [f["feature"] for f in contract["features"]],
        "feature_contract": contract,
        "preprocessor": pre_state,
        "threshold_policy": {
            "rule": rec["threshold"]["rule"],
            "selected_on": "validation split",
            "review_share": sc.evaluation.review_share,
            "value": rec["threshold"]["value"],
            "validation_rows": rec["threshold"]["validation_rows"],
            "validation_top_k": rec["threshold"]["validation_top_k"],
            "model_parameters_sha256": psha,
        },
        "calibration": {
            "status": cal["status"],
            "method": cal["method"],
            "fitted_on": "validation split",
            "parameters": cal.get("parameters"),
            "output_label": cal["output_label"],
            "validation_positives": rec["validation"]["positives"],
            "min_validation_positives": sc.calibration.min_validation_positives,
            "max_ece": sc.calibration.max_ece,
            "test_ece_uncalibrated": cal["uncalibrated_test"]["ece"],
            "test_ece_calibrated": cal["calibrated_test"]["ece"] if "calibrated_test" in cal else None,
            "probability_claim_permitted": False,
            "model_parameters_sha256": psha,
        },
        "training": {
            "dataset_id": identity["dataset_id"],
            "source_file": identity["file"],
            "evidence_sha256": identity["evidence_sha256"],
            "data_version": identity["data_version"],
            "transaction_matrix_sha256": identity["transaction_matrix_sha256"],
            "label_source": label_source,
            "labels_sha256": identity["labels_sha256"],
            "training_domain": domain,
            "production_eligible": eligible,
            "domain_note": note,
            "split": experiment._rnd(
                {k2: v for k2, v in desc.items() if k2 != "largest_components"}
                | {
                    "fractions": sc.splits.model_dump(exclude={"strategies"}),
                    "label_definition": identity["label_source"],
                    "labels": label_info,
                }
            ),
            "imbalance": rec["imbalance"],
            "ablation": ablation,
            "features_removed": sorted(removed),
            "seed": sc.seed,
            "supervised_config_sha256": identity["supervised_config_sha256"],
            "p10_contract_sha256": identity["feature_contract_sha256"],
        },
        "evaluation": experiment._rnd(
            {
                "experiment_id": rec["experiment_id"],
                "evaluated_on": "test split (once)",
                "test": {
                    "rows": test["rows"],
                    "positives": test["positives"],
                    "prevalence": test["prevalence"],
                    "pr_auc": test["pr_auc"],
                    "roc_auc": test["roc_auc"],
                    "at_k": test["at_k"],
                    "at_validation_threshold": test.get("at_threshold"),
                    "confidence_intervals": test["confidence_intervals"],
                },
                "validation": {
                    "rows": rec["validation"]["rows"],
                    "positives": rec["validation"]["positives"],
                    "pr_auc": rec["validation"]["pr_auc"],
                },
                "baselines_on_same_test": baselines,
                "warnings": warnings,
            }
        ),
        "model_card": {
            "name": f"{model_name} ({rec['imbalance']}; {strategy} split"
            + (f"; ablation {ablation}" if ablation else "")
            + f") trained on {domain}",
            "intended_use": "Experimental supervised ranking evidence for a reviewer, shown separately from "
            "the P9 review-risk score. Development and research use only unless approved under the P11 "
            "approval "
            "gate.",
            "not_intended_for": [
                "replacing or changing the P9 review-risk score",
                "reporting a fraud probability",
                "automatic decisions about transactions or people",
                "client ledgers outside the training domain (a PaySim model is never applicable to them)",
            ],
            "training_domain": domain,
            "score_semantics": SCORE_SEMANTICS,
            "limitations": [
                note,
                *experiment.LIMITATIONS,
                "Hyperparameters are fixed (no tuning); the threshold reflects validation review "
                "capacity, not "
                "a fraud cut-off.",
            ],
            "documentation": "docs/supervised-model-card.md; docs/model-lifecycle.md",
        },
        "creation": {
            "ledgerspy_version": __version__,
            "python": platform.python_version(),
            "writer": WRITER,
            "timestamp_policy": "no timestamps inside the artefact (it is deterministic); the registry "
            "records when it was registered and every status change",
        },
        "initial_status": "experiment",
    }
    return artifact.seal(doc)


def train(
    conn: sqlite3.Connection,
    cfg: EffectiveConfig,
    dataset_id: str,
    label_source: str,
    ground_truth: Path | None,
    model_name: str,
    imbalance: str,
    strategy: str,
    ablation: str | None = None,
) -> dict[str, Any]:
    data, label_free, identity, info = service.prepare(conn, cfg, dataset_id, label_source, ground_truth)
    return build_artifact(
        data,
        label_free,
        identity,
        info,
        cfg.settings,
        label_source,
        model_name,
        imbalance,
        strategy,
        ablation,
    )
