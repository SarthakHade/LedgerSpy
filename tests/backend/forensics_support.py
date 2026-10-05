"""Helpers for P7 tests: in-memory records → P6 features → P7 forensic report."""

from typing import Any

from ledgerspy.config import load_config
from ledgerspy.features import matrix
from ledgerspy.forensics import service
from tests.backend.features_support import make


def report(
    records: list[dict[str, Any]],
    *,
    features: dict | None = None,
    rules: dict | None = None,
    benford: dict | None = None,
    p4: dict | None = None,
    p4_checks: dict | None = None,
    details: dict | None = None,
    **make_kw: Any,
) -> dict[str, Any]:
    overrides = {k: v for k, v in (("features", features), ("rules", rules), ("benford", benford)) if v}
    settings = load_config(overrides=overrides or None).settings
    inp = make(records, p4=p4, p4_checks=p4_checks, **make_kw, **(features or {}))
    return service.analyze(inp, matrix.build(inp), settings, details)


def signals(rep: dict, rule_id: str) -> list[dict]:
    return [s for s in rep["signals"] if s["rule_id"] == rule_id]


def flagged(rep: dict, rule_id: str) -> list[str]:
    return [s["subject_id"] for s in signals(rep, rule_id)]


def outcome(rep: dict, rule_id: str) -> dict:
    return next(r for r in rep["rules"] if r["rule_id"] == rule_id)
