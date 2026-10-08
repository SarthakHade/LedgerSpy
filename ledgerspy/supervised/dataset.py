"""Training data contract and the train-only preprocessor.

Contract. A training row is the P6 feature values of one transaction plus its label. Features come only from
the configured allow-list (`supervised.features`), and each must be a registered, implemented,
transaction-level P6 feature with row or point-in-time semantics: post-period and dataset (P4) features
summarise the whole period and would leak the future into a historical row. Never features: transaction /
entity / invoice identifiers, labels or anything derived from them, P7 signals, P8 anomaly scores, P9 risk
scores, scenario IDs. The contract records every configured feature with its status and reason, and is
hashed with the feature-registry version.

Preprocessing is fitted on the TRAINING split only and then applied unchanged to validation and test:
- numeric: optional signed log1p; median imputation + a missing indicator when training rows have nulls;
  dropped when constant or more than `max_missing_share` null in training (recorded); standardised with
  training mean/SD (used by logistic regression; the tree model is invariant to it);
- categorical: one-hot over the categories seen in training (an unseen category is all zeros).

P11 persists the fitted preprocessor (`state()` / `from_state()`, exact floats) inside the model artefact.
Inference applies that state to a `Frame` of P6 values; nothing is ever re-fitted on the inference rows, and a
row's design values depend only on that row.
"""

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ledgerspy.config.schema import SupervisedFeatures
from ledgerspy.features.data import FeatureInput
from ledgerspy.features.matrix import FeatureRun, registry_sha256
from ledgerspy.features.spec import REGISTRY, Kind, Level, Semantics
from ledgerspy.hashing import canonical_json, sha256_hex

SAFE = (Semantics.ROW, Semantics.POINT_IN_TIME)
FORBIDDEN = re.compile(
    r"(^|_)(id|txn_id|label|target|fraud|isfraud|scenario|ground|truth|risk|anomaly|signal)($|_)"
)


class ContractError(ValueError):
    """A configured feature is not a P6 registry feature (a configuration error)."""


@dataclass
class TrainingData:
    txn_ids: list[str]
    y: list[int]
    timestamps: list[datetime | None]
    entity_groups: list[str]  # counterparty stream (split metadata only)
    scenario_groups: list[list[str]]  # scenario IDs (split metadata only; never features)
    numeric: dict[str, list[float | None]]
    categorical: dict[str, list[str | None]]
    contract: dict[str, Any]
    excluded_rows: dict[str, int] = field(default_factory=dict)

    @property
    def n(self) -> int:
        return len(self.txn_ids)


def _status(name: str, run: FeatureRun, kind: str) -> tuple[str, str]:
    spec = REGISTRY.get(name)
    if spec is None:
        raise ContractError(f"supervised feature {name!r} is not a P6 registry feature")
    if FORBIDDEN.search(name):
        return "excluded", "identifier / label / detector-output name"
    if spec.status != "implemented":
        return "excluded", "not available yet in P6"
    if spec.level != Level.TRANSACTION:
        return "excluded", "not transaction level"
    if spec.semantics not in SAFE:
        return (
            "excluded",
            f"{spec.semantics.value} semantics (whole-period information would leak the future)",
        )
    if kind == "numeric" and spec.kind not in (Kind.FLOAT, Kind.INT, Kind.BOOL):
        return "excluded", f"not numeric ({spec.kind.value})"
    if kind == "categorical" and spec.kind != Kind.CATEGORY:
        return "excluded", f"not categorical ({spec.kind.value})"
    st = run.transactions.status.get(name, {"status": "not_available", "reason": "not evaluated"})
    if st["status"] != "computed":
        return "excluded", "not available for this dataset: " + st["reason"]
    return "included", ""


def build(
    inp: FeatureInput,
    run: FeatureRun,
    labels: dict[str, int],
    scenario_groups: dict[str, list[str]],
    cfg: SupervisedFeatures,
) -> TrainingData:
    """Training rows for every transaction that has a label (P6 features only)."""
    entries = []
    for kind, names in (("numeric", cfg.numeric), ("categorical", cfg.categorical)):
        for name in names:
            status, reason = _status(name, run, kind)
            spec = REGISTRY[name]
            entries.append(
                {
                    "feature": name,
                    "kind": kind,
                    "status": status,
                    "reason": reason,
                    "version": spec.version,
                    "semantics": spec.semantics.value,
                    "transform": "signed_log1p" if name in cfg.log_transform else "none",
                }
            )
    tx = run.transactions
    rows = {r.txn_id: r for r in inp.rows}
    index = [i for i, t in enumerate(tx.subjects) if t in labels]
    unlabelled = len(tx.subjects) - len(index)
    used = [e for e in entries if e["status"] == "included"]
    numeric = {
        e["feature"]: [tx.columns[e["feature"]][i] for i in index] for e in used if e["kind"] == "numeric"
    }
    for name in numeric:
        numeric[name] = [None if v is None else float(v) for v in numeric[name]]
    categorical = {
        e["feature"]: [tx.columns[e["feature"]][i] for i in index] for e in used if e["kind"] == "categorical"
    }
    txn_ids = [tx.subjects[i] for i in index]
    contract = {
        "feature_list_version": cfg.version,
        "feature_registry_sha256": registry_sha256(),
        "features": entries,
        "never_features": [
            "transaction, entity and invoice identifiers",
            "labels (isFraud, isFlaggedFraud, …) and anything derived from them",
            "P7 rule signals, P8 anomaly scores, P9 risk scores",
            "P5 scenario identifiers",
            "post-period / dataset-level P6 features (future information)",
        ],
    }
    contract["contract_sha256"] = sha256_hex(canonical_json(contract))
    return TrainingData(
        txn_ids=txn_ids,
        y=[labels[t] for t in txn_ids],
        timestamps=[rows[t].ts for t in txn_ids],
        entity_groups=[
            f"{rows[t].flow}:{rows[t].cp}" if rows[t].cp and rows[t].flow else "row:" + t for t in txn_ids
        ],
        scenario_groups=[scenario_groups.get(t, []) for t in txn_ids],
        numeric=numeric,
        categorical=categorical,
        contract=contract,
        excluded_rows={"unlabelled": unlabelled},
    )


@dataclass
class Frame:
    """P6 feature values of the rows to score (inference): no labels, no split metadata."""

    txn_ids: list[str]
    numeric: dict[str, list[float | None]]
    categorical: dict[str, list[str | None]]

    @property
    def n(self) -> int:
        return len(self.txn_ids)


def frame_from_run(run: FeatureRun, numeric: list[str], categorical: list[str]) -> Frame:
    """Copies of the named transaction-level P6 columns (every row of the dataset, in matrix order)."""
    tx = run.transactions
    return Frame(
        txn_ids=list(tx.subjects),
        numeric={n: [None if v is None else float(v) for v in tx.columns[n]] for n in numeric},
        categorical={n: list(tx.columns[n]) for n in categorical},
    )


def _slog(x: float) -> float:
    return math.copysign(math.log1p(abs(x)), x)


@dataclass
class Preprocessor:
    """Fitted on training rows only."""

    numeric: list[dict[str, Any]] = field(
        default_factory=list
    )  # name, transform, median, mean, sd, indicator
    categorical: list[dict[str, Any]] = field(default_factory=list)  # name, categories
    dropped: list[dict[str, str]] = field(default_factory=list)

    @classmethod
    def fit(
        cls, data: TrainingData, train: list[int], cfg: SupervisedFeatures, remove: set[str]
    ) -> "Preprocessor":
        p = cls()
        for name, col in data.numeric.items():
            if name in remove:
                p.dropped.append({"feature": name, "reason": "removed by ablation"})
                continue
            vals = [col[i] for i in train]
            present = [(_slog(v) if name in cfg.log_transform else v) for v in vals if v is not None]
            share = 1 - len(present) / len(vals) if vals else 1.0
            if share > cfg.max_missing_share:
                p.dropped.append({"feature": name, "reason": f"{share:.0%} null in training rows"})
                continue
            if len(set(present)) < 2:
                p.dropped.append({"feature": name, "reason": "constant in training rows"})
                continue
            s = sorted(present)
            median = s[len(s) // 2] if len(s) % 2 else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2
            filled = present + [median] * (len(vals) - len(present))
            mean = sum(filled) / len(filled)
            sd = math.sqrt(sum((v - mean) ** 2 for v in filled) / len(filled)) or 1.0
            p.numeric.append(
                {
                    "feature": name,
                    "transform": "signed_log1p" if name in cfg.log_transform else "none",
                    "median": median,
                    "mean": mean,
                    "sd": sd,
                    "indicator": 0 < len(present) < len(vals),
                }
            )
        for name, col in data.categorical.items():
            if name in remove:
                p.dropped.append({"feature": name, "reason": "removed by ablation"})
                continue
            cats = sorted({"(missing)" if col[i] is None else col[i] for i in train})
            if len(cats) < 2:
                p.dropped.append({"feature": name, "reason": "constant in training rows"})
                continue
            p.categorical.append({"feature": name, "categories": cats})
        return p

    @property
    def names(self) -> list[str]:
        out = []
        for e in self.numeric:
            out.append(e["feature"])
            if e["indicator"]:
                out.append("missing:" + e["feature"])
        for e in self.categorical:
            out.extend(f"{e['feature']}={c}" for c in e["categories"])
        return out

    def transform(
        self, data: "TrainingData | Frame", rows: list[int], standardise: bool
    ) -> list[list[float]]:
        """Column-major design matrix for `rows`, using only fitted (training) statistics."""
        cols: list[list[float]] = []
        for e in self.numeric:
            src = data.numeric[e["feature"]]
            raw = [src[i] for i in rows]
            t = [
                e["median"] if v is None else (_slog(v) if e["transform"] == "signed_log1p" else v)
                for v in raw
            ]
            cols.append([(v - e["mean"]) / e["sd"] for v in t] if standardise else t)
            if e["indicator"]:
                cols.append([1.0 if v is None else 0.0 for v in raw])
        for e in self.categorical:
            src = data.categorical[e["feature"]]
            vals = ["(missing)" if src[i] is None else src[i] for i in rows]
            for c in e["categories"]:
                cols.append([1.0 if v == c else 0.0 for v in vals])
        return cols

    def state(self) -> dict[str, Any]:
        """The complete fitted state, exact (persisted in the P11 artefact)."""
        return {
            "fitted_on": "training split only",
            "numeric": [dict(e) for e in self.numeric],
            "categorical": [
                {"feature": e["feature"], "categories": list(e["categories"])} for e in self.categorical
            ],
            "dropped": [dict(d) for d in self.dropped],
            "design_columns": self.names,
        }

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> "Preprocessor":
        return cls(
            numeric=[dict(e) for e in state["numeric"]],
            categorical=[
                {"feature": e["feature"], "categories": list(e["categories"])} for e in state["categorical"]
            ],
            dropped=[dict(d) for d in state["dropped"]],
        )

    @property
    def features(self) -> list[str]:
        """Input features actually read, in design order."""
        return [e["feature"] for e in self.numeric] + [e["feature"] for e in self.categorical]

    def describe(self) -> dict[str, Any]:
        return {
            "fitted_on": "training split only",
            "numeric": [
                {k: (round(v, 6) if isinstance(v, float) else v) for k, v in e.items()} for e in self.numeric
            ],
            "categorical": self.categorical,
            "dropped": self.dropped,
            "design_columns": self.names,
        }
