"""P4 on the real PaySim sample: exact results, honest limitations, determinism, evidence untouched."""

import hashlib
import json

import pytest

from ledgerspy.normalize.service import list_fixes
from ledgerspy.quality.engine import evaluate, run_integrity
from tests.backend.conftest import PAYSIM, make_ingest

PAYSIM_SHA = hashlib.sha256(PAYSIM.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def paysim(tmp_path_factory):
    h = make_ingest(tmp_path_factory.mktemp("p4paysim"))
    ds = h.upload(PAYSIM)
    h.confirm(ds)
    report = run_integrity(h.conn, h.cfg, ds["dataset_id"], "tester")
    yield h, ds["dataset_id"], report
    h.conn.close()


def test_paysim_issue_counts(paysim):
    _, _, r = paysim
    assert {k: v for k, v in r["summary"]["issues_by_check"].items() if v} == {
        "missing_required": 6,
        "missing_optional": 22,
        "balance_continuity": 4894,
        "unmapped_columns": 10,
    }
    assert r["missing_values"]["required"] == {
        "posted_at": {"missing": 0, "percent": 0.0},
        "amount_minor": {"missing": 2, "percent": 0.02},
        "direction": {"missing": 4, "percent": 0.039},
    }
    assert {f: v["missing"] for f, v in r["missing_values"]["optional"].items()} == {
        "account": 6,
        "payer_raw": 6,
        "payee_raw": 6,
        "txn_type": 4,
    }


def test_paysim_balance_artefacts_are_info_not_errors(paysim):
    _, _, r = paysim
    bc = next(c for c in r["checks"] if c["check"] == "balance_continuity")
    assert bc["severity"] == "info" and "synthetic dataset override" in bc["severity_basis"]["rule"]
    assert bc["details"] == {
        "balance_unchanged": 2471,
        "balance_zeroed": 1618,
        "consistent": 5225,
        "not_checkable": 8,
        "other_mismatch": 805,
    }
    assert sum(bc["details"].values()) == 10_127


def test_paysim_readiness_is_honest(paysim):
    _, _, r = paysim
    ready = r["readiness"]
    # 6 rows lack an amount or a type in the source → blocking errors; nothing can be invented to fix them
    assert ready["status"] == "not_ready" and ready["score"] == 99.9 and ready["blocking_issues"] == 6
    comp = {c["component"]: c for c in ready["components"]}
    assert comp["completeness"]["affected_rows"] == 6
    assert comp["consistency"]["affected_rows"] == 0  # INFO balance artefacts do not lower readiness
    assert r["summary"]["clean_rows"] == 10_121 and r["summary"]["rows_with_issues"] == 6


def test_paysim_limitations_are_explicit(paysim):
    _, _, r = paysim
    assert r["dataset"]["synthetic"] is True and r["dataset"]["time_basis"] == "simulation_step"
    assert set(r["summary"]["checks_not_available"]) == {
        "implausible_date",
        "out_of_period",
        "duplicate_source_id",
    }
    assert "simulated clock" in r["summary"]["checks_not_available"]["out_of_period"]
    assert set(r["not_applicable_fields"]) == {"document_date", "reference", "description", "currency"}
    assert r["field_coverage"]["direction"]["status"] == "derived"
    assert r["summary"]["issues_by_check"]["field_coverage"] == 0  # not applicable ≠ missing
    assert r["limitations"][0].startswith("SYNTHETIC source")
    assert "simulated clock" in r["limitations"][1]
    assert list_fixes(paysim[0].conn, paysim[1]) == []  # nothing to normalise; nothing invented


def test_paysim_issue_links_to_its_transaction(paysim):
    h, dsid, r = paysim
    issue = h.conn.execute(
        "SELECT * FROM integrity_issues WHERE run_id = ? AND check_code = 'missing_required' AND field = "
        "'amount_minor' ORDER BY source_row LIMIT 1",
        (r["run_id"],),
    ).fetchone()
    txn = h.conn.execute(
        "SELECT * FROM transactions WHERE dataset_id = ? AND txn_id = ?", (dsid, issue["txn_id"])
    ).fetchone()
    assert txn["source_row"] == issue["source_row"] and txn["amount_minor"] is None
    p3 = h.conn.execute(
        "SELECT code FROM ingest_issues WHERE dataset_id = ? AND source_row = ? AND field = 'amount'",
        (dsid, issue["source_row"]),
    ).fetchone()
    assert p3["code"] == "missing"  # the P3 record and the P4 issue agree


def test_paysim_runs_are_deterministic(paysim):
    h, dsid, r = paysim
    again = run_integrity(h.conn, h.cfg, dsid, "tester")
    assert again["run_id"] != r["run_id"] and again["report_sha256"] == r["report_sha256"]

    def ids(run):
        sql = "SELECT issue_id FROM integrity_issues WHERE run_id = ? ORDER BY issue_id"
        return [x[0] for x in h.conn.execute(sql, (run,))]

    assert ids(again["run_id"]) == ids(r["run_id"])
    report, issues, _ = evaluate(h.conn, h.cfg, dsid)
    stripped = {
        k: v for k, v in r.items() if k not in ("run_id", "created_at", "created_by", "report_sha256")
    }
    stripped["fixes"] = {k: v for k, v in stripped["fixes"].items() if k != "all_by_status"}
    assert json.dumps(report, sort_keys=True) == json.dumps(stripped, sort_keys=True)


def test_paysim_evidence_unchanged(paysim):
    h, _, r = paysim
    assert r["dataset"]["evidence_sha256"] == PAYSIM_SHA
    stored = h.workspace / "evidence" / f"{PAYSIM_SHA}.csv"
    assert hashlib.sha256(stored.read_bytes()).hexdigest() == PAYSIM_SHA
    assert hashlib.sha256(PAYSIM.read_bytes()).hexdigest() == PAYSIM_SHA


def test_paysim_p3_issues_are_each_reported_once(paysim):
    _, _, r = paysim
    ing = next(c for c in r["checks"] if c["check"] == "ingestion_issues")
    assert ing["status"] == "ok" and ing["issue_count"] == 0
    assert ing["details"] == {
        "recorded": 6,
        "by_field_and_code": {"amount.missing": 2, "direction.missing": 4},
        "surfaced_by": {"missing_required": 6},
    }
    assert r["summary"]["issues_by_check"]["missing_required"] == 6
