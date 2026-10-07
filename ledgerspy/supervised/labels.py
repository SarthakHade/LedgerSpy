"""The supervised label adapter: the ONLY module in LedgerSpy that reads labels or ground truth for modelling.

Two sources:

- PaySim: the `labels` table that P3 filled at ingestion (label columns never become transaction fields).
  Only the configured column (`isFraud`) is read; `isFlaggedFraud` is the simulator's own rule flag and is
  never used.
- The P5 synthetic ground truth: a JSON file given explicitly by the caller (never found implicitly). A row is
  positive when it belongs to an injected scenario of at least `synthetic_min_severity`, unless the scenario
  type is excluded (the ground truth marks the digit-distribution subgroup as "not labelled as fraud").
  Control rows and all other rows are negative. Scenario membership is returned only as split metadata (to
  keep a scenario's rows in one split); it is never a model input.

The returned labels are keyed by txn_id. Nothing here is visible to P6–P9: they import nothing from this
module.
"""

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ledgerspy.config.schema import SupervisedTarget
from ledgerspy.hashing import canonical_json, sha256_hex

SEVERITY = {"low": 0, "medium": 1, "high": 2, "critical": 3}


@dataclass(frozen=True)
class LabelSet:
    source: str
    y: dict[str, int]  # txn_id → 0/1
    scenario_groups: dict[str, list[str]] = field(
        default_factory=dict
    )  # txn_id → scenario IDs (split metadata)
    description: dict[str, Any] = field(default_factory=dict)

    @property
    def sha256(self) -> str:
        return sha256_hex(canonical_json({"source": self.source, "y": sorted(self.y.items())}))


def paysim_labels(conn: sqlite3.Connection, dataset_id: str, cfg: SupervisedTarget) -> LabelSet:
    y: dict[str, int] = {}
    invalid = 0
    for txn_id, value in conn.execute(
        "SELECT txn_id, value FROM labels WHERE dataset_id = ? AND column_name = ? ORDER BY txn_id",
        (dataset_id, cfg.paysim_label_column),
    ):
        if value in ("0", "1"):
            y[txn_id] = int(value)
        else:
            invalid += 1
    return LabelSet(
        source=f"labels table: {cfg.paysim_label_column}",
        y=y,
        description={
            "column": cfg.paysim_label_column,
            "rows_labelled": len(y),
            "rows_with_invalid_label": invalid,
            "positives": sum(y.values()),
            "note": "PaySim is a mobile-money simulation: a development proxy, not LedgerSpy's target "
            "domain.",
        },
    )


def synthetic_labels(ground_truth: Path, txn_ids: list[str], cfg: SupervisedTarget) -> LabelSet:
    gt = json.loads(Path(ground_truth).read_text(encoding="utf-8"))
    positive_scenarios, groups = [], {}
    pos: set[str] = set()
    for s in gt["scenarios"]:
        rows = s["affected"]["ledger_txn_ids"]
        for t in rows:
            groups.setdefault(t, []).append(s["scenario_id"])
        if (
            not s["is_control"]
            and SEVERITY[s["severity"]] >= SEVERITY[cfg.synthetic_min_severity]
            and s["scenario_type"] not in cfg.synthetic_exclude_scenario_types
            and rows
        ):
            positive_scenarios.append(s["scenario_id"])
            pos.update(rows)
    present = set(txn_ids)
    y = {t: int(t in pos) for t in txn_ids}
    return LabelSet(
        source=f"synthetic ground truth: {Path(ground_truth).name}",
        y=y,
        scenario_groups={t: sorted(v) for t, v in groups.items() if t in present},
        description={
            "rule": f"positive = rows of injected scenarios with severity ≥ {cfg.synthetic_min_severity}, "
            f"excluding types {cfg.synthetic_exclude_scenario_types}; controls and all other rows negative",
            "positive_scenarios": sorted(positive_scenarios),
            "positives": sum(y.values()),
            "rows_labelled": len(y),
            "ground_truth_sha256": sha256_hex(Path(ground_truth).read_bytes()),
        },
    )
