"""Run a supervised evaluation for one canonicalised dataset (local, offline; results written as JSON).

Steps:
1. Build the P6 features and the label-free P7 → P8 → P9 analysis exactly as P9 does; its scores serve as
   baselines. Nothing about it changes, and no label is involved.
2. Read labels through the label adapter: PaySim's `labels` table, or an explicitly given P5 ground-truth
   file.
3. Build the training data (feature contract) and run the experiments.

The report goes to `<workspace>/experiments/<experiment set id>.json`. No model is persisted: model storage
and inference are P11. No database table is written.
"""

import json
import sqlite3
from pathlib import Path
from typing import Any

from ledgerspy.anomaly import service as anomaly_service
from ledgerspy.config.loader import ConfigError, EffectiveConfig
from ledgerspy.features import matrix
from ledgerspy.features import service as feature_service
from ledgerspy.forensics import service as forensic_service
from ledgerspy.hashing import canonical_json, sha256_hex
from ledgerspy.normalize.view import load_view
from ledgerspy.risk import service as risk_service
from ledgerspy.supervised import dataset, experiment, labels


def prepare(
    conn: sqlite3.Connection,
    cfg: EffectiveConfig,
    dataset_id: str,
    label_source: str,
    ground_truth: Path | None = None,
) -> tuple[Any, dict, dict, dict]:
    settings = cfg.settings
    sc = settings.supervised
    view = load_view(conn, dataset_id)
    inp = feature_service.input_from_view(
        view, cfg, feature_service.p4_issues(conn, dataset_id, view.data_version)
    )
    run = matrix.build(inp)
    details = forensic_service.p4_details(conn, inp.p4_run_id, ("balance_continuity",))
    forensic = forensic_service.analyze(inp, run, settings, details)
    anomaly = anomaly_service.analyze(inp, run, settings)
    risk = risk_service.analyze(view, inp, run, forensic, anomaly, settings, {"status": "not_used"})

    if label_source == "paysim":
        lab = labels.paysim_labels(conn, dataset_id, sc.target)
    elif label_source == "synthetic":
        if ground_truth is None:
            raise ConfigError("synthetic labels need an explicit --ground-truth file")
        lab = labels.synthetic_labels(ground_truth, [r.txn_id for r in inp.rows], sc.target)
    else:
        raise ConfigError(f"unknown label source {label_source!r} (paysim | synthetic)")
    if not lab.y or not sum(lab.y.values()):
        raise ConfigError("no positive labels: a supervised evaluation is not possible for this dataset")

    try:
        data = dataset.build(inp, run, lab.y, lab.scenario_groups, sc.features)
    except dataset.ContractError as exc:
        raise ConfigError(str(exc)) from exc
    a_ids = anomaly["scores"]["txn_ids"]
    if_col = (anomaly["scores"].get("isolation_forest") or {}).get("score")
    if_by = dict(zip(a_ids, if_col, strict=True)) if if_col else {}
    label_free = {
        "p8_isolation_forest": [if_by.get(t) for t in data.txn_ids],
        "p9_review_risk": [risk.by_txn[t]["score"] for t in data.txn_ids],
    }
    ds = conn.execute(
        "SELECT evidence_sha256, original_filename FROM datasets WHERE dataset_id = ?", (dataset_id,)
    ).fetchone()
    identity = {
        "dataset_id": dataset_id,
        "file": ds[1],
        "evidence_sha256": ds[0],
        "data_version": view.data_version,
        "feature_registry_sha256": run.manifest["registry_sha256"],
        "transaction_matrix_sha256": run.manifest["transaction_matrix_sha256"],
        "feature_contract_sha256": data.contract["contract_sha256"],
        "label_source": lab.source,
        "labels_sha256": lab.sha256,
        "supervised_config_sha256": sha256_hex(canonical_json(sc.model_dump(mode="json"))),
        "seed": sc.seed,
    }
    return data, label_free, identity, lab.description


def evaluate(
    conn: sqlite3.Connection,
    cfg: EffectiveConfig,
    dataset_id: str,
    label_source: str,
    ground_truth: Path | None = None,
) -> dict[str, Any]:
    """Prepare the training data (labels via the adapter) and run every configured experiment."""
    data, label_free, identity, info = prepare(conn, cfg, dataset_id, label_source, ground_truth)
    return experiment.run(data, label_free, cfg.settings, identity, info)


def write(report: dict[str, Any], workspace: Path) -> Path:
    out = workspace / "experiments"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"S-{report['result_sha256'][:16]}.json"
    path.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    return path
