"""The feature registry: one definition per feature.

A feature exists only as a `FeatureSpec` in `REGISTRY`: name, version, meaning, inputs, output type, units,
point-in-time rule, scope, peer group, missing-data and fallback behaviour, direction of "unusual", the
dataset
capabilities it needs, and its single implementation. Nothing else in LedgerSpy may compute a feature:
detectors and models read the matrix built from this registry (`ledgerspy.features.matrix`).

Features that the data cannot support yet are registered as `deferred` with the reason and the phase that will
make them possible. They have no implementation and never appear as matrix columns.

Versioning: `definition_sha256` hashes the metadata and the implementation's source. `registry_lock.json`
records (version, definition_sha256) per feature; a test fails when a definition changes without a version
bump.
"""

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ledgerspy.hashing import canonical_json, sha256_hex


class Level(StrEnum):
    TRANSACTION = "transaction"  # one value per transaction
    ENTITY = "entity"  # one value per counterparty entity


class Semantics(StrEnum):
    ROW = "row"  # uses only the transaction's own fields (and configuration)
    POINT_IN_TIME = "point_in_time"  # the row plus rows with a strictly earlier posting timestamp
    DATASET = "dataset"  # uses dataset-wide results (P4 integrity issues): context, not "known at the time"
    POST_PERIOD = "post_period"  # summary over the whole period: analytical, never "known at" a transaction


class Kind(StrEnum):
    FLOAT = "float"
    INT = "int"
    BOOL = "bool"
    CATEGORY = "category"
    DATE = "date"


class Missing(StrEnum):
    """What a null value means. Every feature declares exactly one policy."""

    NULL_IF_INPUT_MISSING = "null_if_input_missing"  # null when a required input field of the row is null
    NULL_IF_INSUFFICIENT_HISTORY = (
        "null_if_insufficient_history"  # null + basis `unavailable` after fallbacks
    )
    NULL_IF_NOT_APPLICABLE = (
        "null_if_not_applicable"  # null when the definition does not apply (stated per feature)
    )


class Unusual(StrEnum):
    HIGHER = "higher"
    LOWER = "lower"
    BOTH = "both"  # far from typical in either direction
    TRUE = "true"  # the flag being true is the unusual case
    CONTEXT = "context"  # not directional on its own (an encoding or context value)


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    version: int
    level: Level
    family: (
        str  # amount, timing, frequency, velocity, sequence, behaviour, relationship, data_quality, profile
    )
    description: str
    inputs: tuple[str, ...]  # canonical fields (normalised view), P4 outputs or configuration keys
    kind: Kind
    units: str
    semantics: Semantics
    scope: str  # what the value is about: transaction, counterparty stream, payer/payee pair, …
    missing: Missing
    missing_detail: str  # exactly when the value is null
    unusual: Unusual
    requires: tuple[str, ...]  # dataset capabilities (`ledgerspy.features.data.CAPABILITIES`)
    fn: Callable[..., Any] | None
    peer_group: str = "none"
    fallback: str = "none"
    params: tuple[str, ...] = ()  # `features.*` configuration keys the definition reads
    reference: str = "docs/feature-registry.md"
    status: str = "implemented"  # implemented | deferred
    deferred_reason: str = ""

    def metadata(self) -> dict[str, Any]:
        out = {k: v for k, v in self.__dict__.items() if k != "fn"}
        out["level"], out["semantics"] = self.level.value, self.semantics.value
        out["kind"], out["missing"], out["unusual"] = self.kind.value, self.missing.value, self.unusual.value
        out["inputs"], out["requires"], out["params"] = (
            list(self.inputs),
            list(self.requires),
            list(self.params),
        )
        return out

    def definition_sha256(self) -> str:
        """Hash of everything that defines the feature: its metadata and its implementation's source."""
        meta = self.metadata()
        meta.pop("version")
        source = inspect.getsource(self.fn) if self.fn is not None else ""
        return sha256_hex(canonical_json({"metadata": meta, "source": source}))


REGISTRY: dict[str, FeatureSpec] = {}


def feature(**meta: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register the decorated function as the one implementation of a feature."""

    def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        spec = FeatureSpec(fn=fn, **meta)
        _add(spec)
        return fn

    return wrap


def deferred(
    name: str, family: str, level: Level, description: str, reason: str, inputs: tuple[str, ...]
) -> None:
    """Register a feature the current data cannot support. It has no implementation and is never computed."""
    _add(
        FeatureSpec(
            name=name,
            version=0,
            level=level,
            family=family,
            description=description,
            inputs=inputs,
            kind=Kind.FLOAT,
            units="—",
            semantics=Semantics.ROW,
            scope="—",
            missing=Missing.NULL_IF_NOT_APPLICABLE,
            missing_detail="never computed",
            unusual=Unusual.CONTEXT,
            requires=(),
            fn=None,
            status="not_available_yet",
            deferred_reason=reason,
        )
    )


def _add(spec: FeatureSpec) -> None:
    if spec.name in REGISTRY:
        raise ValueError(f"feature {spec.name} is defined twice")
    if not spec.name.replace("_", "").isalnum() or spec.name != spec.name.lower():
        raise ValueError(f"feature names are lower_snake_case: {spec.name}")
    if spec.status == "implemented" and (spec.fn is None or spec.version < 1 or not spec.description):
        raise ValueError(f"feature {spec.name} needs an implementation, a version ≥ 1 and a description")
    REGISTRY[spec.name] = spec


def implemented(level: Level | None = None) -> list[FeatureSpec]:
    return [s for s in REGISTRY.values() if s.status == "implemented" and (level is None or s.level == level)]


def lock_entries() -> dict[str, dict[str, Any]]:
    """The content of registry_lock.json for the current definitions."""
    return {
        name: {"version": s.version, "definition_sha256": s.definition_sha256()}
        for name, s in sorted(REGISTRY.items())
        if s.status == "implemented"
    }
