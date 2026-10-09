"""Build a feature run for a canonicalised dataset in the workspace.

Inputs are the normalised view (`ledgerspy.normalize.view.load_view`) and, for data-quality features, the
issues of the latest P4 integrity run of the *current* data version, and (P12) the master records imported
for the dataset's case. Nothing else is read: not the `labels` table, not `extra`, not any file. The
effective configuration (with case overrides) is passed in and hashed into the manifest.
"""

import json
import sqlite3

from ledgerspy.config.loader import EffectiveConfig
from ledgerspy.features import data, matrix
from ledgerspy.features.data import FeatureInput
from ledgerspy.normalize.view import DatasetView, load_view

# Canonical fields that count as "mapped" for a view (the mapping names `amount` for amount_minor).
_MAPPED_ALIASES = {"amount": "amount_minor", "debit_amount": "amount_minor", "credit_amount": "amount_minor"}


def mapped_fields(view: DatasetView) -> set[str]:
    out = {_MAPPED_ALIASES.get(f, f) for f in view.mapping["fields"]}
    if view.mapped("direction"):
        out.add("direction")
    return out


def p4_issues(
    conn: sqlite3.Connection, dataset_id: str, data_version: str
) -> tuple[str | None, dict[str, list[str]], dict[str, dict[str, str]]]:
    """Row-level issue codes and check statuses of the latest integrity run on this data version.

    (None, {}, {}) if there is none.
    """
    run = conn.execute(
        "SELECT run_id, report FROM integrity_runs WHERE dataset_id = ? AND data_version = ? ORDER BY "
        "created_at DESC, run_id DESC LIMIT 1",
        (dataset_id, data_version),
    ).fetchone()
    if run is None:
        return None, {}, {}
    checks = {
        c["check"]: {
            "status": c["status"],
            "reason": c.get("basis", "") if c["status"] == "not_available" else "",
        }
        for c in json.loads(run[1])["checks"]
    }
    issues: dict[str, list[str]] = {}
    for txn_id, check in conn.execute(
        "SELECT txn_id, check_code FROM integrity_issues WHERE run_id = ? AND txn_id IS NOT NULL ORDER BY "
        "txn_id, check_code, issue_id",
        (run[0],),
    ):
        issues.setdefault(txn_id, []).append(check)
    return run[0], issues, checks


def input_from_view(
    view: DatasetView,
    cfg: EffectiveConfig,
    p4: tuple[str | None, dict[str, list[str]], dict[str, dict[str, str]]],
    masters: tuple = (),
) -> FeatureInput:
    fcfg = cfg.settings.features
    profile = fcfg.adapters.get(view.adapter)
    rows = data.rows_from_records(view.rows, profile.counterparty if profile else "by_direction")
    run_id, issues, checks = p4
    return FeatureInput(
        dataset_id=view.dataset_id,
        adapter=view.adapter,
        time_basis=view.time_basis,
        data_version=view.data_version,
        rows=rows,
        capabilities=data.capabilities(
            adapter=view.adapter,
            time_basis=view.time_basis,
            mapped=mapped_fields(view),
            rows=rows,
            cfg=fcfg,
            p4_current=run_id is not None,
            p4_checks=checks,
            masters=bool(masters),
        ),
        cfg=fcfg,
        p4_issues=issues,
        p4_run_id=run_id,
        p4_checks=checks,
        masters=tuple(masters),
    )


def masters_for(conn: sqlite3.Connection, dataset_id: str) -> tuple:
    """The imported master records of the dataset's case (P12), in a fixed order; () if none."""
    from ledgerspy.resolution.masters import records_for_case

    row = conn.execute("SELECT case_id FROM datasets WHERE dataset_id = ?", (dataset_id,)).fetchone()
    return records_for_case(conn, row[0]) if row else ()


def input_for(conn: sqlite3.Connection, cfg: EffectiveConfig, view: DatasetView) -> FeatureInput:
    """The feature input of a dataset view: P4 issues of its data version and its case's master records."""
    return input_from_view(
        view,
        cfg,
        p4_issues(conn, view.dataset_id, view.data_version),
        masters=masters_for(conn, view.dataset_id),
    )


def build_for_dataset(conn: sqlite3.Connection, cfg: EffectiveConfig, dataset_id: str) -> matrix.FeatureRun:
    return matrix.build(input_for(conn, cfg, load_view(conn, dataset_id)))
