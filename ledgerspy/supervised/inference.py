"""P11 supervised inference: score a dataset with a registered, verified, compatible model artefact.

Steps:
1. Resolve the model. Without a model ID: the active production model, which must still be approved,
   verify against its pinned hash and match the feature registry; no active model → `no_active_model` (P9
   and the rest of LedgerSpy are unaffected). With a model ID: that registered model, as production only if
   it is the active approved model, otherwise only when `experimental` is requested explicitly (retired
   models never run).
2. Build the dataset's P6 features exactly as P9 does. Only row and point-in-time features can be in an
   artefact (checked on load and against the running registry), so a transaction's inputs use only rows
   before it, never the rest of the ledger. Every feature the model reads must be computed for this dataset,
   otherwise the model is `not_applicable`; nothing is substituted.
3. Transform with the persisted, training-only preprocessing state (nothing is fitted here), score in
   batches (scoring is row-independent, so batches never change a score), and apply the persisted
   validation threshold if asked.

The result is labelled an uncalibrated ranking score, never a fraud probability. Nothing is written to the
database and the P9 risk result is never read or changed; a JSON record of the run (no labels) goes to
`<workspace>/inference/<inference_id>.json`.
"""

import json
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from ledgerspy.config.loader import EffectiveConfig
from ledgerspy.errors import LedgerSpyError
from ledgerspy.features import service as feature_service
from ledgerspy.features.matrix import FeatureRun
from ledgerspy.hashing import canonical_json, sha256_hex
from ledgerspy.supervised import artifact, registry
from ledgerspy.supervised.artifact import LoadedArtifact
from ledgerspy.supervised.calibration import apply as apply_platt
from ledgerspy.supervised.dataset import Frame, frame_from_run

INFERENCE_VERSION = 1
SCORE_TYPE = "uncalibrated ranking score"
INFERENCE_ID = re.compile(r"^I-[0-9a-f]{16}$")
NOTE = (
    "Experimental supervised model output: a ranking score, not a fraud probability. It is shown "
    "separately "
    "from, and never changes, the P9 review-risk score."
)

_CACHE: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
_CACHE_SIZE = 4
_LOCK = threading.Lock()


def _resolve(
    conn: sqlite3.Connection, workspace: Path, model_id: str | None, experimental: bool
) -> tuple[LoadedArtifact, str, str]:
    active = registry.active_model_id(conn)
    if model_id is None:
        if active is None:
            raise LookupError
        model_id = active
    status = registry.recorded_status(conn, registry._row(conn, model_id)["model_id"])
    production = model_id == active and status == "approved"
    if not production:
        if status == "retired":
            raise registry.ModelError(
                "model_retired", f"{model_id} is retired and cannot be used", http_status=409
            )
        if not experimental:
            raise registry.ModelError(
                "model_not_permitted",
                f"{model_id} is {status}, not the active approved model; request it explicitly as an "
                "experiment",
                http_status=409,
            )
    loaded = registry.load(conn, workspace, model_id)
    return loaded, ("production" if production else "experimental"), status


def score_frame(loaded: LoadedArtifact, frame: Frame, batch_size: int) -> list[float]:
    """Scores for every row of the frame, in frame order (batched; each row scored independently)."""
    std = loaded.doc["model"]["input_standardised"]
    out: list[float] = []
    for start in range(0, frame.n, batch_size):
        rows = list(range(start, min(start + batch_size, frame.n)))
        out.extend(loaded.score(loaded.preprocessor.transform(frame, rows, std)))
    return out


def model_frame(loaded: LoadedArtifact, run: FeatureRun) -> Frame:
    """The model's input columns from a feature run; `not_applicable` if any is not computed."""
    status = run.transactions.status
    missing = {
        f: status.get(f, {}).get("reason") or status.get(f, {}).get("status", "not evaluated")
        for f in loaded.preprocessor.features
        if status.get(f, {}).get("status") != "computed"
    }
    if missing:
        raise artifact.ArtifactError(
            "model_not_applicable",
            "this dataset does not provide every feature the model reads; nothing is substituted",
            {"missing_features": missing},
            http_status=409,
        )
    pre = loaded.preprocessor
    return frame_from_run(run, [e["feature"] for e in pre.numeric], [e["feature"] for e in pre.categorical])


def scores_sha256(txn_ids: list[str], scores: list[float]) -> str:
    pairs = sorted(zip(txn_ids, scores, strict=True))
    return sha256_hex(canonical_json([[t, repr(s)] for t, s in pairs]))


def infer(
    conn: sqlite3.Connection,
    cfg: EffectiveConfig,
    workspace: Path,
    dataset_id: str,
    model_id: str | None = None,
    experimental: bool = False,
    apply_threshold: bool = True,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        loaded, mode, status = _resolve(conn, workspace, model_id, experimental)
    except LookupError:
        return {
            "kind": "supervised_inference",
            "inference_status": "no_active_model",
            "note": "No supervised model is active; nothing was scored. P9 review risk does not need one.",
        }
    t_load = time.perf_counter() - t0
    doc = loaded.doc

    t1 = time.perf_counter()
    run = feature_service.build_for_dataset(conn, cfg, dataset_id)
    compat = artifact.require_compatible(doc, run.manifest["features_config_sha256"])
    frame = model_frame(loaded, run)
    t_feat = time.perf_counter() - t1

    t2 = time.perf_counter()
    batch = cfg.settings.supervised.inference.batch_size
    scores = score_frame(loaded, frame, batch)
    t_score = time.perf_counter() - t2

    thr = doc["threshold_policy"]
    cal = doc["calibration"]
    calibrated = cal["status"] == "calibrated"
    cal_scores = apply_platt(scores, cal["parameters"]["a"], cal["parameters"]["b"]) if calibrated else None
    source_rows = dict(zip(run.transactions.subjects, run.transactions.source_rows or [], strict=False))
    order = sorted(range(frame.n), key=lambda i: (-scores[i], frame.txn_ids[i]))
    rows = []
    for rank, i in enumerate(order, 1):
        r: dict[str, Any] = {
            "txn_id": frame.txn_ids[i],
            "source_row": source_rows.get(frame.txn_ids[i]),
            "rank": rank,
            "score": scores[i],
        }
        if apply_threshold:
            r["at_or_above_threshold"] = scores[i] >= thr["value"]
        if cal_scores is not None:
            r["calibrated_score"] = cal_scores[i]
        rows.append(r)

    ds = conn.execute(
        "SELECT evidence_sha256, original_filename, adapter FROM datasets WHERE dataset_id = ?", (dataset_id,)
    ).fetchone()
    man = run.manifest
    dataset = {
        "dataset_id": dataset_id,
        "evidence_sha256": ds["evidence_sha256"] if ds else None,
        "data_version": man["dataset"]["data_version"],
        "p4_run_id": man["dataset"]["p4_run_id"],
        "adapter": man["dataset"]["adapter"],
        "rows": frame.n,
        "transaction_matrix_sha256": man["transaction_matrix_sha256"],
        "features_config_sha256": man["features_config_sha256"],
    }
    inference_config = {
        "inference_version": INFERENCE_VERSION,
        "mode": mode,
        "apply_threshold": apply_threshold,
    }
    config_sha = sha256_hex(canonical_json(inference_config))
    inference_id = (
        "I-"
        + sha256_hex(
            canonical_json({"artifact_sha256": loaded.sha256, "dataset": dataset, "config": config_sha})
        )[:16]
    )

    tr = doc["training"]
    warnings = list(compat["warnings"])
    if mode == "experimental":
        warnings.append(f"experimental model (status {status}): not approved for production use")
    if not tr["production_eligible"]:
        warnings.append(f"trained on {tr['training_domain']}: {tr['domain_note']}")
    if tr["label_source"] == "paysim" and dataset["adapter"] != "paysim":
        warnings.append("out of domain: a PaySim-trained model scoring a non-PaySim dataset")
    if ds is not None and ds["evidence_sha256"] == tr["evidence_sha256"]:
        warnings.append(
            "this is the training evidence file: scores of its training rows are in-sample and optimistic"
        )
    flagged = sum(1 for r in rows if r.get("at_or_above_threshold"))
    metadata = {
        "kind": "supervised_inference",
        "inference_id": inference_id,
        "inference_status": "completed",
        "inference_mode": mode,
        "label": "experimental supervised model" if mode == "experimental" else "approved supervised model",
        "note": NOTE,
        "model": {
            "model_id": loaded.model_id,
            "artifact_sha256": loaded.sha256,
            "model_type": loaded.model_type,
            "status": status,
            "training_domain": tr["training_domain"],
            "production_eligible": tr["production_eligible"],
            "artifact_version": doc["artifact_version"],
        },
        "feature_contract": {
            "contract_version": doc["feature_contract"]["contract_version"],
            "contract_sha256": doc["feature_contract"]["contract_sha256"],
            "feature_registry_sha256": doc["feature_contract"]["feature_registry_sha256"],
            "features_read": loaded.preprocessor.features,
            "compatibility": "exact match",
            "inference_time_policy": "row and point-in-time P6 features only (no future rows); persisted "
            "training preprocessing; nothing fitted on the inference rows",
        },
        "calibration": {
            "status": cal["status"],
            "output_label": cal["output_label"],
            "calibrated_score_included": calibrated,
        },
        "score_type": SCORE_TYPE,
        "threshold": {
            "value": thr["value"],
            "rule": thr["rule"],
            "selected_on": thr["selected_on"],
            "applied": apply_threshold,
            "rows_at_or_above": flagged if apply_threshold else None,
        },
        "dataset": dataset,
        "inference_config": inference_config,
        "inference_config_sha256": config_sha,
        "scores_sha256": scores_sha256(frame.txn_ids, scores),
        "warnings": warnings,
        "timings_seconds": {
            "artifact_load": round(t_load, 4),
            "features": round(t_feat, 4),
            "transform_and_score": round(t_score, 4),
            "total": round(time.perf_counter() - t0, 4),
        },
    }
    result = {"metadata": metadata, "rows": rows}
    _write(workspace, result)
    with _LOCK:
        _CACHE[inference_id] = result
        _CACHE.move_to_end(inference_id)
        while len(_CACHE) > _CACHE_SIZE:
            _CACHE.popitem(last=False)
    return result


def page(result: dict[str, Any], page_no: int, size: int) -> dict[str, Any]:
    if "metadata" not in result:
        return result
    rows = result["rows"]
    start = (page_no - 1) * size
    return {
        "metadata": result["metadata"],
        "total": len(rows),
        "page": page_no,
        "size": size,
        "rows": rows[start : start + size],
    }


def _dir(workspace: Path) -> Path:
    return Path(workspace) / "inference"


def _write(workspace: Path, result: dict[str, Any]) -> None:
    d = _dir(workspace)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{result['metadata']['inference_id']}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def read_metadata(workspace: Path, inference_id: str) -> dict[str, Any]:
    if not INFERENCE_ID.fullmatch(inference_id or ""):
        raise LedgerSpyError("invalid_inference_id", "an inference ID is I- followed by 16 hex digits")
    with _LOCK:
        hit = _CACHE.get(inference_id)
    if hit is not None:
        return hit["metadata"]
    path = _dir(workspace) / f"{inference_id}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))["metadata"]
    except (OSError, ValueError, KeyError) as exc:
        raise LedgerSpyError("not_found", f"no inference record {inference_id}", http_status=404) from exc


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()
