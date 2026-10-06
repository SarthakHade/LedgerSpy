"""Helpers for P9 tests: in-memory records → P6 → P7 + P8 → P9 risk result (no workspace needed)."""

import json
from types import SimpleNamespace
from typing import Any

from ledgerspy.anomaly import service as anomaly_service
from ledgerspy.config import load_config
from ledgerspy.config.loader import _deep_merge
from ledgerspy.features import matrix
from ledgerspy.forensics import service as forensic_service
from ledgerspy.risk import service
from ledgerspy.risk.fusion import SignalInput, fuse
from tests.backend.features_support import make

NO_WARMUP = {"anomaly": {"reference": {"warmup_days": 0}}}


_SETTINGS: dict[str, Any] = {}


def settings(**overrides: Any):
    key = json.dumps(overrides, sort_keys=True, default=str)
    if key not in _SETTINGS:
        _SETTINGS[key] = load_config(overrides=_deep_merge(NO_WARMUP, overrides)).settings
    return _SETTINGS[key]


def result(
    records: list[dict[str, Any]], *, readiness: dict | None = None, make_kw: dict | None = None, **overrides
):
    s = settings(**overrides)
    inp = make(records, **(make_kw or {}), **overrides.get("features", {}))
    run = matrix.build(inp)
    forensic = forensic_service.analyze(inp, run, s, {})
    anomaly = anomaly_service.analyze(inp, run, s)
    view = SimpleNamespace(
        dataset_id=inp.dataset_id,
        adapter=inp.adapter,
        time_basis=inp.time_basis,
        data_version="v",
        rows=records,
    )
    return service.analyze(view, inp, run, forensic, anomaly, s, readiness or {"status": "not_run"}), s


def fused(signals: dict[str, list[tuple[str, str, float]]], **overrides):
    """fuse({family: [(signal_id, detector, evidence), …]}) with the (overridden) default configuration."""
    s = settings(**overrides)
    return fuse({f: [SignalInput(*x) for x in items] for f, items in signals.items()}, s.risk)
