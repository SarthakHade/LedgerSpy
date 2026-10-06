"""Helpers for P8 tests: in-memory records → P6 features → P8 anomaly report."""

import random
from typing import Any

from ledgerspy.anomaly import service
from ledgerspy.config import load_config
from ledgerspy.config.loader import _deep_merge
from ledgerspy.features import matrix
from tests.backend.features_support import make, rec

NO_WARMUP = {"reference": {"warmup_days": 0}}


def settings(anomaly: dict | None = None, features: dict | None = None):
    overrides: dict[str, Any] = {"anomaly": _deep_merge(NO_WARMUP, anomaly or {})}
    if features:
        overrides["features"] = features
    return load_config(overrides=overrides).settings


def build(records: list[dict[str, Any]], features: dict | None = None, **make_kw: Any):
    inp = make(records, **make_kw, **(features or {}))
    return inp, matrix.build(inp)


def report(
    records: list[dict[str, Any]],
    *,
    anomaly: dict | None = None,
    features: dict | None = None,
    **make_kw: Any,
) -> dict[str, Any]:
    inp, run = build(records, features, **make_kw)
    return service.analyze(inp, run, settings(anomaly, features))


def ledger(vendors: int = 10, rows_per_vendor: int = 40, seed: int = 7) -> list[dict[str, Any]]:
    """An ordinary ledger: each vendor has its own amount level with ~20% log-normal spread, one payment
    every few days, business hours."""
    rng = random.Random(seed)  # noqa: S311 — test data
    out, n = [], 0
    for v in range(vendors):
        level = 10_000_00 * (1 + v)
        for k in range(rows_per_vendor):
            n += 1
            out.append(
                rec(
                    n,
                    at=k * 3.0 + v * 0.1 + rng.random() * 0.2,
                    amount=int(level * rng.lognormvariate(0, 0.2)),
                    payee=f"Vendor {v:02d}",
                    account=f"51{v % 3:02d}",
                )
            )
    return out


def detector(rep: dict, detector_id: str) -> dict:
    return next(d for d in rep["detectors"] if d["detector_id"] == detector_id)


def signals(rep: dict, detector_id: str) -> list[dict]:
    return [s for s in rep["signals"] if s["detector_id"] == detector_id]


def flagged(rep: dict, detector_id: str) -> list[str]:
    return [s["subject_id"] for s in signals(rep, detector_id)]


def score_of(rep: dict, detector_id: str, column: str) -> dict[str, Any]:
    return dict(zip(rep["scores"]["txn_ids"], rep["scores"][detector_id][column], strict=True))
