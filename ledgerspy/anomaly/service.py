"""P8 dataset anomaly report: Isolation Forest, robust peer z-scores and tail percentiles for one dataset.

Inputs: the P6 feature build of the dataset (normalised view, current P4 run) — the same inputs P7 reads.
Nothing else is read: not labels, not `extra`, not files, not ground truth, not P7 signals. Nothing is
written.

The report is reproducible from (dataset data version, effective configuration, feature registry, detector
versions); `report_sha256` identifies it. Subject-level columns are in txn_id order, so the report does not
depend on the order rows were supplied in. P9 consumes `signals` (one contract with P7) and may use `scores`;
this module does not fuse.
"""

import sqlite3
from typing import Any

from ledgerspy.anomaly import evaluation, isolation_forest, robust_stats
from ledgerspy.anomaly.eligibility import EligibilityError, population
from ledgerspy.anomaly.models import DetectorOutcome
from ledgerspy.config.loader import ConfigError, EffectiveConfig
from ledgerspy.config.schema import LedgerSpyConfig
from ledgerspy.features import matrix
from ledgerspy.features import service as feature_service
from ledgerspy.features.data import FeatureInput
from ledgerspy.features.matrix import FeatureRun
from ledgerspy.hashing import canonical_json, sha256_hex
from ledgerspy.normalize.view import load_view

REPORT_VERSION = 1
DETECTOR_VERSIONS = {
    isolation_forest.DETECTOR_ID: isolation_forest.VERSION,
    robust_stats.ROBUST_ID: robust_stats.ROBUST_VERSION,
    robust_stats.TAIL_ID: robust_stats.TAIL_VERSION,
}
NOTE = (
    "Anomaly signals mean 'unusual relative to the stated population', never 'fraudulent'. They are "
    "label-free evidence for review; risk fusion and tiers are P9. Precision/recall are not computed here: "
    "no labels are read in the detection path."
)
# Per-row score columns copied into the report, per detector.
SCORE_COLUMNS = {
    isolation_forest.DETECTOR_ID: ("score", "percentile"),
    robust_stats.ROBUST_ID: ("max_abs_z",),
    robust_stats.TAIL_ID: ("max_tail_percentile",),
}


def analyze(inp: FeatureInput, run: FeatureRun, settings: LedgerSpyConfig) -> dict[str, Any]:
    """Run every anomaly detector on a feature build. Pure: same inputs, same report."""
    cfg = settings.anomaly
    pop = population(inp, run, cfg.reference)
    try:
        outcomes: list[DetectorOutcome] = [
            isolation_forest.detect(inp, run, pop, cfg),
            robust_stats.robust_peer(inp, run, pop, cfg),
            robust_stats.tail_percentile(inp, run, pop, cfg),
        ]
    except EligibilityError as exc:
        raise ConfigError(str(exc)) from exc
    subjects = run.transactions.subjects
    order = pop.order
    results = sorted(
        (r for o in outcomes for r in o.results), key=lambda r: (r.detector_id, r.subject_type, r.subject_id)
    )
    scores: dict[str, Any] = {"txn_ids": [subjects[i] for i in order], "source_rows": None}
    if run.transactions.source_rows is not None:
        scores["source_rows"] = [run.transactions.source_rows[i] for i in order]
    for o in outcomes:
        cols = {"status": o.scores.get("status")} | {c: o.scores.get(c) for c in SCORE_COLUMNS[o.detector_id]}
        scores[o.detector_id] = {
            c: ([v[i] for i in order] if v is not None else None) for c, v in cols.items()
        }
    if_scores = scores[isolation_forest.DETECTOR_ID].get("score")
    ranked = (
        evaluation.ranking(dict(zip(scores["txn_ids"], if_scores, strict=True)))
        if if_scores is not None
        else []
    )
    review = cfg.evaluation.review_capacity
    affected = sorted({t for r in results for t in r.txn_ids})
    by_detector: dict[str, int] = {}
    for r in results:
        by_detector[r.detector_id] = by_detector.get(r.detector_id, 0) + 1
    report: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "dataset": run.manifest["dataset"],
        "inputs": {
            "feature_registry_sha256": run.manifest["registry_sha256"],
            "features_config_sha256": run.manifest["features_config_sha256"],
            "transaction_matrix_sha256": run.manifest["transaction_matrix_sha256"],
            "anomaly_config_sha256": sha256_hex(canonical_json(cfg.model_dump(mode="json"))),
            "detector_versions": dict(sorted(DETECTOR_VERSIONS.items())),
            "labels_read": False,
            "scenario_truth_read": False,
        },
        "population": pop.describe(),
        "summary": {
            "detectors": len(outcomes),
            "status": {o.detector_id: o.status for o in outcomes},
            "rows": len(subjects),
            "rows_scored": {o.detector_id: o.rows_scored for o in outcomes},
            "rows_insufficient": {o.detector_id: o.rows_insufficient for o in outcomes},
            "rows_reference_only": {o.detector_id: o.rows_reference_only for o in outcomes},
            "features_used": {o.detector_id: len(o.features.get("used", [])) for o in outcomes},
            "features_excluded": {o.detector_id: len(o.features.get("excluded", [])) for o in outcomes},
            "signals": len(results),
            "signals_by_detector": dict(sorted(by_detector.items())),
            "affected_transactions": len(affected),
        },
        "detectors": [o.summary() for o in outcomes],
        "signals": [r.to_dict() for r in results],
        "scores": scores,
        "evaluation": {
            "labels_used": False,
            "review_capacity": review,
            "top_k_isolation_forest": {str(k): ranked[:k] for k in review},
            "score_distribution": {
                isolation_forest.DETECTOR_ID: evaluation.score_distribution(if_scores or []),
                robust_stats.ROBUST_ID: evaluation.score_distribution(
                    scores[robust_stats.ROBUST_ID].get("max_abs_z") or []
                ),
                robust_stats.TAIL_ID: evaluation.score_distribution(
                    scores[robust_stats.TAIL_ID].get("max_tail_percentile") or []
                ),
            },
            "diagnostics_available": (
                "precision@k, recall@k, PR-AUC and top-k scenario coverage exist in "
                "ledgerspy.anomaly.evaluation for data with legitimately available labels; they are computed "
                "outside the detection path."
            ),
        },
        "affected": {"transactions": affected},
        "note": NOTE,
    }
    report["report_sha256"] = sha256_hex(canonical_json(report))
    return report


def build_inputs(
    conn: sqlite3.Connection, cfg: EffectiveConfig, dataset_id: str
) -> tuple[FeatureInput, FeatureRun]:
    view = load_view(conn, dataset_id)
    inp = feature_service.input_from_view(
        view, cfg, feature_service.p4_issues(conn, dataset_id, view.data_version)
    )
    return inp, matrix.build(inp)


def run_anomaly(conn: sqlite3.Connection, cfg: EffectiveConfig, dataset_id: str) -> dict[str, Any]:
    """The dataset anomaly report (read-only)."""
    inp, run = build_inputs(conn, cfg, dataset_id)
    return analyze(inp, run, cfg.settings)


__all__ = ["analyze", "build_inputs", "run_anomaly"]
