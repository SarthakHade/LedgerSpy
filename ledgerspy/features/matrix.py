"""Feature matrices and the feature manifest.

`build(inp)` evaluates every implemented feature that the dataset supports, at transaction and entity level.
Unsupported features get no column; their status says why. The manifest identifies exactly what was computed
(feature versions and definition hashes, effective features configuration, dataset and data version, P4 run)
and `matrix_sha256` identifies the values, so a later run or model can record and verify its inputs.
"""

from dataclasses import dataclass, field
from typing import Any

from ledgerspy.features import entity
from ledgerspy.features.data import FeatureInput
from ledgerspy.features.spec import REGISTRY, FeatureSpec, Kind, Level, lock_entries
from ledgerspy.hashing import canonical_json, sha256_hex

MANIFEST_VERSION = 1


def registry_sha256() -> str:
    """Identifies the whole registry: every feature's (version, definition hash)."""
    return sha256_hex(canonical_json(lock_entries()))


@dataclass
class FeatureMatrix:
    level: Level
    subjects: list[str]  # txn_id (transaction level) or stream ID (entity level)
    source_rows: list[int] | None
    columns: dict[str, list[Any]] = field(default_factory=dict)
    basis: dict[str, list[str | None]] = field(default_factory=dict)  # features with a peer fallback
    status: dict[str, dict[str, str]] = field(default_factory=dict)  # every registered feature of the level

    def values_sha256(self) -> str:
        return sha256_hex(
            canonical_json({"subjects": self.subjects, "columns": self.columns, "basis": self.basis})
        )

    def row(self, i: int) -> dict[str, Any]:
        return {name: col[i] for name, col in self.columns.items()}


@dataclass
class FeatureRun:
    transactions: FeatureMatrix
    entities: FeatureMatrix
    manifest: dict[str, Any]


def _check(spec: FeatureSpec, values: list[Any]) -> None:
    """Every value must have the declared type (None allowed): a wrong type is a bug, not data."""
    expected = {
        Kind.FLOAT: (float, int),
        Kind.INT: (int,),
        Kind.BOOL: (bool,),
        Kind.CATEGORY: (str,),
        Kind.DATE: (str,),
    }[spec.kind]
    for v in values:
        if v is None:
            continue
        if spec.kind != Kind.BOOL and isinstance(v, bool) or not isinstance(v, expected):
            raise TypeError(f"feature {spec.name}: {v!r} is not {spec.kind.value}")


def _evaluate(inp: FeatureInput, level: Level, n: int, matrix: FeatureMatrix) -> None:
    for spec in sorted(REGISTRY.values(), key=lambda s: s.name):
        if spec.level != level:
            continue
        if spec.status != "implemented":
            matrix.status[spec.name] = {"status": "not_available_yet", "reason": spec.deferred_reason}
            continue
        reason = inp.available(spec.requires)
        if reason is not None:
            matrix.status[spec.name] = {"status": "not_available", "reason": reason}
            continue
        result = spec.fn(inp)  # type: ignore[misc]
        values, basis = result if isinstance(result, tuple) else (result, None)
        if len(values) != n:
            raise AssertionError(f"feature {spec.name} returned {len(values)} values for {n} subjects")
        values = [float(v) if spec.kind == Kind.FLOAT and v is not None else v for v in values]
        _check(spec, values)
        matrix.columns[spec.name] = values
        if basis is not None:
            matrix.basis[spec.name] = basis
        matrix.status[spec.name] = {"status": "computed", "reason": ""}


def build(inp: FeatureInput) -> FeatureRun:
    tx = FeatureMatrix(Level.TRANSACTION, [r.txn_id for r in inp.rows], [r.source_row for r in inp.rows])
    _evaluate(inp, Level.TRANSACTION, len(inp.rows), tx)
    subjects = entity.subjects(inp)
    ent = FeatureMatrix(Level.ENTITY, subjects, None)
    _evaluate(inp, Level.ENTITY, len(subjects), ent)
    return FeatureRun(tx, ent, manifest(inp, tx, ent))


def manifest(inp: FeatureInput, tx: FeatureMatrix, ent: FeatureMatrix) -> dict[str, Any]:
    features = {}
    for name, spec in sorted(REGISTRY.items()):
        st = (tx.status if spec.level == Level.TRANSACTION else ent.status)[name]
        entry = {"level": spec.level.value, "status": st["status"], "semantics": spec.semantics.value}
        if spec.status == "implemented":
            entry.update(version=spec.version, definition_sha256=spec.definition_sha256())
        if st["reason"]:
            entry["reason"] = st["reason"]
        features[name] = entry
    return {
        "manifest_version": MANIFEST_VERSION,
        "registry_sha256": registry_sha256(),
        "features_config_sha256": sha256_hex(canonical_json(inp.cfg.model_dump(mode="json"))),
        "dataset": {
            "dataset_id": inp.dataset_id,
            "adapter": inp.adapter,
            "time_basis": inp.time_basis,
            "data_version": inp.data_version,
            "p4_run_id": inp.p4_run_id,
            "rows": len(inp.rows),
        },
        "capabilities": {k: ("available" if v is None else v) for k, v in sorted(inp.capabilities.items())},
        "features": features,
        "transaction_matrix_sha256": tx.values_sha256(),
        "entity_matrix_sha256": ent.values_sha256(),
    }
