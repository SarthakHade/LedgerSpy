"""P6: the registry itself: complete metadata, one unit test per feature, versioning, one implementation."""

import dataclasses
import re
from pathlib import Path

import pytest

import tests.backend.test_features_units as units
from ledgerspy.config.schema import FeaturesConfig
from ledgerspy.features.data import CAPABILITIES
from ledgerspy.features.lock import read_lock
from ledgerspy.features.matrix import registry_sha256
from ledgerspy.features.spec import REGISTRY, FeatureSpec, Level, Missing, Semantics, deferred, implemented

ROOT = Path(__file__).resolve().parents[2]
FAMILIES = {
    "amount",
    "timing",
    "frequency",
    "velocity",
    "sequence",
    "behaviour",
    "relationship",
    "data_quality",
    "profile",
}
IMPLEMENTED = implemented()
DEFERRED = [s for s in REGISTRY.values() if s.status != "implemented"]


def test_registry_inventory():
    assert len(IMPLEMENTED) == 59 and len(DEFERRED) == 10
    assert len(implemented(Level.TRANSACTION)) == 40 and len(implemented(Level.ENTITY)) == 19


@pytest.mark.parametrize("spec", IMPLEMENTED, ids=lambda s: s.name)
def test_every_feature_has_complete_metadata(spec: FeatureSpec):
    assert spec.version >= 1 and spec.fn is not None
    for text in (spec.description, spec.units, spec.scope, spec.missing_detail):
        assert isinstance(text, str) and len(text) >= 3
    assert spec.inputs and spec.family in FAMILIES
    assert set(spec.requires) <= set(CAPABILITIES)
    fields = FeaturesConfig.model_fields
    for param in spec.params:  # every configuration key a definition reads exists
        head, *rest = param.split(".")
        assert head in fields, param
        if rest:
            assert rest[0] in fields[head].annotation.model_fields, param
    if spec.semantics == Semantics.POST_PERIOD:
        assert spec.level == Level.ENTITY  # whole-period summaries are never transaction features
    if spec.missing == Missing.NULL_IF_INSUFFICIENT_HISTORY:
        assert spec.semantics in (Semantics.POINT_IN_TIME, Semantics.POST_PERIOD)
    if spec.fallback != "none":
        assert "→" in spec.peer_group and "unavailable" in spec.peer_group


@pytest.mark.parametrize("spec", DEFERRED, ids=lambda s: s.name)
def test_deferred_features_say_why_and_have_no_implementation(spec: FeatureSpec):
    assert spec.status == "not_available_yet" and spec.fn is None and spec.version == 0
    assert len(spec.deferred_reason) > 30 and spec.inputs  # what is missing, and why


def test_every_feature_has_its_unit_test():
    tests = {n.removeprefix("test_") for n in dir(units) if n.startswith("test_")}
    assert {s.name for s in IMPLEMENTED} <= tests
    assert not tests - {s.name for s in IMPLEMENTED}, "unit tests for features that do not exist"


def test_a_feature_cannot_be_registered_twice():
    with pytest.raises(ValueError, match="defined twice"):
        deferred("log_amount", "amount", Level.TRANSACTION, "x", "y", ())


def test_the_lock_matches_every_definition():
    """A definition change without a version bump fails here (then: python -m ledgerspy.features.lock)."""
    lock = read_lock()
    assert set(lock) == {s.name for s in IMPLEMENTED}
    for s in IMPLEMENTED:
        assert lock[s.name] == {"version": s.version, "definition_sha256": s.definition_sha256()}, (
            f"{s.name} changed: bump its version and regenerate registry_lock.json"
        )


def test_definition_changes_are_detectable():
    s = REGISTRY["amount_robust_z"]
    base = s.definition_sha256()
    for change in (
        {"description": s.description + " (changed)"},
        {"inputs": (*s.inputs, "reference")},
        {"missing": Missing.NULL_IF_INPUT_MISSING},
        {"fallback": "global only"},
        {"semantics": Semantics.DATASET},
        {"params": ()},
        {"fn": units.at},  # a different implementation
    ):
        assert dataclasses.replace(s, **change).definition_sha256() != base, change
    # the version is part of the lock, not of the definition hash: a bump alone is visible in the lock
    assert dataclasses.replace(s, version=2).definition_sha256() == base
    assert len(registry_sha256()) == 64


def test_no_feature_is_implemented_outside_the_registry():
    """No other Python module defines a function named after a registered feature, and nothing outside
    ledgerspy/features imports the private helpers."""
    names = {s.name for s in IMPLEMENTED}
    for path in (ROOT / "ledgerspy").rglob("*.py"):
        if "features" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        defined = set(re.findall(r"^\s*def (\w+)\(", text, re.M))
        assert not defined & names, (path, defined & names)
        assert "features.history" not in text and "features.transaction" not in text, path


def test_legacy_frontend_features_are_documented_not_duplicated():
    """The P1 in-browser heuristic still exists; it is documented as legacy (removed when the UI uses P9)."""
    doc = (ROOT / "docs" / "feature-registry.md").read_text(encoding="utf-8")
    assert "frontend/js/ml-engine.js" in doc and "legacy" in doc.lower()
    js = (ROOT / "frontend" / "js" / "ml-engine.js").read_text(encoding="utf-8")
    assert "engineerFeatures" in js  # unchanged by P6 (no new JS feature code)


def test_every_feature_named_in_the_docs_exists():
    """The scenario matrix and the registry doc may only cite registered features (no invented names)."""
    names = set(REGISTRY)
    matrix_doc = (ROOT / "docs" / "synthetic-scenario-matrix.md").read_text(encoding="utf-8")
    p6_column = [
        line.split(" | ")[-2] for line in matrix_doc.splitlines() if line.startswith("| ") and " | " in line
    ]
    cited = {n for cell in p6_column for n in re.findall(r"`([a-z][a-z0-9_]+)`", cell)} - {"ent_"}
    assert cited and cited <= names, cited - names
    registry_doc = (ROOT / "docs" / "feature-registry.md").read_text(encoding="utf-8")
    for spec in IMPLEMENTED:
        assert f"**`{spec.name}`** · v{spec.version} ·" in registry_doc, spec.name
    for spec in DEFERRED:
        assert f"| `{spec.name}` |" in registry_doc, spec.name
