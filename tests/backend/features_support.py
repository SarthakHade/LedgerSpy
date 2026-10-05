"""Helpers for P6 tests: canonical records in memory → FeatureInput, and feature evaluation."""

from datetime import date, datetime, timedelta
from typing import Any

from ledgerspy.config import load_config
from ledgerspy.features import data
from ledgerspy.features.data import FeatureInput
from ledgerspy.features.spec import REGISTRY

T0 = datetime(2024, 1, 1, 10, 0)  # a Monday
ALL_FIELDS = {
    "posted_at",
    "document_date",
    "amount_minor",
    "direction",
    "account",
    "payer_raw",
    "payee_raw",
    "reference",
    "txn_type",
}


def rec(
    n: int,
    *,
    at: datetime | None | float = 0.0,
    amount: int | None = 100_00,
    direction: str | None = "debit",
    payer: str | None = "Acme Pvt Ltd",
    payee: str | None = "Vendor A",
    account: str | None = "5100",
    txn_type: str | None = None,
    document_date: date | None = None,
    reference: str | None = None,
    normalized: bool = False,
) -> dict[str, Any]:
    """A canonical record. `at` is a datetime, None, or days after T0."""
    ts = T0 + timedelta(days=at) if isinstance(at, int | float) else at
    return {
        "txn_id": f"T{n:05d}",
        "source_row": n,
        "posted_at": ts.isoformat() if ts else None,
        "document_date": document_date.isoformat() if document_date else None,
        "amount_minor": amount,
        "direction": direction,
        "account": account,
        "payer_raw": payer,
        "payee_raw": payee,
        "reference": reference,
        "txn_type": txn_type,
        "normalized_fields": ["payee_raw"] if normalized else [],
        "extra": {},
    }


def features_cfg(**overrides: Any):
    return load_config(overrides={"features": overrides} if overrides else None).settings.features


def make(
    records: list[dict[str, Any]],
    *,
    adapter: str = "generic_ledger",
    time_basis: str = "datetime",
    mapped: set[str] | None = None,
    p4: dict[str, list[str]] | None = None,
    **cfg_overrides: Any,
) -> FeatureInput:
    cfg = features_cfg(**cfg_overrides)
    profile = cfg.adapters[adapter]
    rows = data.rows_from_records(records, profile.counterparty)
    return FeatureInput(
        dataset_id="D-test",
        adapter=adapter,
        time_basis=time_basis,
        data_version="v",
        rows=rows,
        capabilities=data.capabilities(
            adapter=adapter,
            time_basis=time_basis,
            mapped=ALL_FIELDS if mapped is None else mapped,
            rows=rows,
            cfg=cfg,
            p4_current=p4 is not None,
        ),
        cfg=cfg,
        p4_issues=p4 or {},
        p4_run_id="Q-test" if p4 is not None else None,
    )


def values(inp: FeatureInput, name: str) -> list[Any]:
    """Evaluate one feature (asserting the dataset supports it)."""
    spec = REGISTRY[name]
    assert inp.available(spec.requires) is None, inp.available(spec.requires)
    out = spec.fn(inp)
    return list(out[0]) if isinstance(out, tuple) else list(out)


def basis(inp: FeatureInput, name: str) -> list[Any]:
    out = REGISTRY[name].fn(inp)
    assert isinstance(out, tuple), f"{name} has no fallback basis"
    return list(out[1])
