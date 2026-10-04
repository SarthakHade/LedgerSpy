"""P4: every validator on fixtures with known injected issues: exact counts, severities, row links."""

import pytest

from ledgerspy.quality.engine import run_integrity
from tests.backend.conftest import load_quality_fixture

PERIOD = ("2024-04-01", "2024-04-30")


@pytest.fixture
def check(ingest):
    """Run the integrity check on a fixture; return (report, {check: entry}, issues by check)."""

    def run(name, period=PERIOD):
        ds = load_quality_fixture(ingest, name, period)
        report = run_integrity(ingest.conn, ingest.cfg, ds["dataset_id"], "tester")
        by_check = {c["check"]: c for c in report["checks"]}
        rows = ingest.conn.execute(
            "SELECT * FROM integrity_issues WHERE run_id = ? ORDER BY source_row, check_code",
            (report["run_id"],),
        ).fetchall()
        issues: dict[str, list] = {}
        for r in rows:
            issues.setdefault(r["check_code"], []).append(dict(r))
        return report, by_check, issues

    return run


def counts(report):
    return {k: v for k, v in report["summary"]["issues_by_check"].items() if v}


# ---------- 1. clean ledger ----------


def test_clean_ledger_has_no_issues_and_full_readiness(check):
    report, by_check, _ = check("clean.csv")
    assert counts(report) == {}
    assert report["readiness"]["score"] == 100.0 and report["readiness"]["status"] == "ready"
    assert report["summary"]["clean_rows"] == report["summary"]["total_rows"] == 5
    assert all(c["status"] in ("ok", "not_available") for c in report["checks"])
    assert report["summary"]["checks_not_available"] == {
        "balance_continuity": "no running-balance column in the source"
    }
    assert by_check["out_of_period"]["status"] == "ok"  # the case period is declared


# ---------- 2. missing values ----------


def test_missing_values(check):
    report, by_check, issues = check("missing_values.csv")
    assert counts(report) == {"missing_required": 4, "missing_optional": 3}
    assert report["missing_values"]["required"] == {
        "posted_at": {"missing": 1, "percent": 20.0},
        "amount_minor": {"missing": 2, "percent": 40.0},
        "direction": {"missing": 1, "percent": 20.0},
    }
    assert report["missing_values"]["optional"]["payee_raw"] == {"missing": 1, "percent": 20.0}
    assert report["missing_values"]["optional"]["description"] == {"missing": 2, "percent": 40.0}
    assert [(i["source_row"], i["field"]) for i in issues["missing_required"]] == [
        (2, "posted_at"),
        (3, "amount_minor"),
        (4, "direction"),
        (5, "amount_minor"),
    ]
    # 4 of 5 rows ≥ 5% → escalated; optional gaps 3 rows of 5 ≥ 20% → warning
    assert by_check["missing_required"]["severity"] == "critical"
    assert by_check["missing_required"]["severity_basis"]["base"] == "error"
    assert by_check["missing_optional"]["severity"] == "warning"


# ---------- 3. invalid numeric value ----------


def test_invalid_numeric_values(check):
    report, by_check, issues = check("invalid_numeric.csv")
    assert counts(report) == {"invalid_value": 3}
    got = sorted((i["source_row"], i["observed"]) for i in issues["invalid_value"])
    assert [r for r, _ in got] == [2, 3, 4]
    assert by_check["invalid_value"]["details"] == {
        "by_field_and_code": {"amount.too_many_decimals": 1, "amount.unparseable": 2}
    }
    with_fix = [i for i in issues["invalid_value"] if i["fix_id"]]
    assert [i["source_row"] for i in with_fix] == [3]  # only '1 234.50' has another reading
    assert report["fixes"] == {
        "proposed_this_run": 1,
        "safe": 0,
        "require_confirmation": 1,
        "all_by_status": {"proposed": 1, "accepted": 0, "rejected": 0, "applied": 0, "reverted": 0},
    }


# ---------- 4. invalid / implausible dates ----------


def test_invalid_dates(check):
    report, _, issues = check("invalid_date.csv")
    assert counts(report) == {"unparseable_date": 3, "implausible_date": 1, "out_of_period": 1}
    assert [i["source_row"] for i in issues["unparseable_date"]] == [2, 3, 4]
    assert [i["source_row"] for i in issues["implausible_date"]] == [5]
    assert (
        report["fixes"]["require_confirmation"] == 2
    )  # '05/04/2024' (2 readings) and '2024/04/07' (1 reading)


# ---------- 5–6. duplicates ----------


def test_exact_duplicates(check):
    report, by_check, issues = check("duplicates.csv")
    assert by_check["exact_duplicate"]["details"] == {"groups": 1, "rows_in_groups": 3, "redundant_rows": 2}
    assert [i["source_row"] for i in issues["exact_duplicate"]] == [2, 5]
    assert all(
        '"duplicate_of_row":1' in i["observed"] and '"group_size":3' in i["observed"]
        for i in issues["exact_duplicate"]
    )
    assert counts(report) == {"exact_duplicate": 2, "duplicate_source_id": 2}  # the repeated invoice number


def test_near_duplicates(check):
    report, _, issues = check("near_duplicates.csv")
    assert counts(report) == {"near_duplicate": 2, "needs_normalization": 1}
    assert [i["source_row"] for i in issues["near_duplicate"]] == [2, 5]
    assert '"near_row":1' in issues["near_duplicate"][0]["observed"]
    assert '"near_row":4' in issues["near_duplicate"][1]["observed"]  # 'CITY  POWER CO' ≈ 'City Power Co'
    assert '"hours_apart":48.0' in issues["near_duplicate"][1]["observed"]


# ---------- 7–9. amounts and period ----------


def test_negative_and_zero_amounts(check):
    report, by_check, issues = check("negative_zero.csv")
    assert counts(report) == {"negative_amount": 1, "zero_amount": 2}
    assert [i["source_row"] for i in issues["negative_amount"]] == [2]
    assert [i["source_row"] for i in issues["zero_amount"]] == [3, 4]
    assert by_check["negative_amount"]["severity"] == "info"  # credit notes are legitimate
    assert by_check["zero_amount"]["severity"] == "error"  # 2 of 5 rows ≥ 5% escalation


def test_out_of_period(check):
    report, _, issues = check("out_of_period.csv")
    assert counts(report) == {"out_of_period": 2}
    assert [i["source_row"] for i in issues["out_of_period"]] == [1, 4]


def test_out_of_period_needs_a_case_period(check):
    report, by_check, _ = check("out_of_period.csv", period=None)
    assert by_check["out_of_period"]["status"] == "not_available"
    assert "no audit period" in by_check["out_of_period"]["basis"]
    assert "out_of_period" not in counts(report)


# ---------- 10. duplicate source IDs ----------


def test_duplicate_source_ids(check):
    report, by_check, issues = check("duplicate_ids.csv")
    assert counts(report) == {"duplicate_source_id": 3, "unmapped_columns": 1}
    assert by_check["duplicate_source_id"]["details"] == {
        "reference": {"duplicate_rows": 2},
        "Txn ID": {"duplicate_rows": 1},
    }
    assert sorted(i["source_row"] for i in issues["duplicate_source_id"]) == [2, 3, 4]


# ---------- 11. balance continuity ----------


def test_balance_discontinuity(check):
    report, by_check, issues = check("balance.csv")
    assert by_check["balance_continuity"]["status"] == "issues"
    assert "debit raises the balance (explains 3 of 4 transitions)" in by_check["balance_continuity"]["basis"]
    (issue,) = issues["balance_continuity"]
    assert issue["source_row"] == 4
    assert '"expected_balance":75000' in issue["observed"] and '"balance":74000' in issue["observed"]
    assert by_check["near_duplicate"]["status"] == "not_available"  # no payee column: not a silent pass


def test_balance_not_checked_without_a_balance_column(check):
    _, by_check, _ = check("clean.csv")
    assert by_check["balance_continuity"]["status"] == "not_available"


# ---------- 12. unmapped columns ----------


def test_unmapped_columns(check):
    report, _, issues = check("unmapped.csv")
    assert report["unmapped_columns"] == ["Cost Centre", "Project", "Approver"]
    assert counts(report) == {"unmapped_columns": 3}
    assert all(i["source_row"] is None and i["severity"] == "info" for i in issues["unmapped_columns"])
    assert report["readiness"]["score"] == 100.0  # INFO issues never reduce readiness


# ---------- 13. multiple simultaneous issues ----------


def test_multiple_simultaneous_issues(check):
    report, _, issues = check("multi.csv")
    assert counts(report) == {
        "missing_required": 1,
        "unparseable_date": 1,
        "exact_duplicate": 1,
        "negative_amount": 1,
        "zero_amount": 1,
        "out_of_period": 1,
        "duplicate_source_id": 1,
    }
    rows = {c: [i["source_row"] for i in v] for c, v in issues.items()}
    assert rows["exact_duplicate"] == [2] and rows["missing_required"] == [3]
    assert rows["zero_amount"] == [4] and rows["out_of_period"] == [4]  # one row, two issues
    assert rows["unparseable_date"] == [5] and rows["negative_amount"] == [6]
    s = report["summary"]
    assert s["rows_with_issues"] == 4 and s["clean_rows"] == 2 and s["rows_with_info_only"] == 1


# ---------- 14. normalisation-required values ----------


def test_values_needing_normalisation(check):
    report, _, issues = check("normalization.csv")
    assert counts(report) == {"malformed_identifier": 1, "needs_normalization": 5, "field_coverage": 1}
    (bad,) = issues["malformed_identifier"]
    assert bad["source_row"] == 3 and "U+200B" in bad["message"] and bad["fix_id"]
    fields = sorted((i["source_row"], i["field"]) for i in issues["needs_normalization"])
    assert fields == [(1, "payee_raw"), (2, "account"), (2, "description"), (2, "txn_type"), (3, "txn_type")]
    assert report["fixes"]["safe"] == 6


# ---------- 15. ambiguous values ----------


def test_ambiguous_values_need_confirmation(check, ingest):
    from ledgerspy.normalize.service import list_fixes

    report, _, issues = check("ambiguous.csv")
    assert counts(report) == {"unparseable_date": 2}
    fixes = {f["source_row"]: f for f in list_fixes(ingest.conn, report["dataset"]["dataset_id"])}
    assert fixes[2]["requires_confirmation"] and fixes[2]["proposed_value"] is None
    assert fixes[2]["candidates"] == ["2024-04-05T00:00:00", "2024-05-04T00:00:00"]
    assert fixes[3]["requires_confirmation"] and fixes[3]["proposed_value"] == "2024-04-09T00:00:00"


# ---------- traceability ----------


def test_every_row_issue_links_to_its_transaction(check, ingest):
    report, _, issues = check("multi.csv")
    for items in issues.values():
        for i in items:
            assert i["issue_id"].startswith("I-") and i["dataset_id"] == report["dataset"]["dataset_id"]
            if i["source_row"] is None:
                continue
            txn = ingest.conn.execute(
                "SELECT source_row FROM transactions WHERE dataset_id = ? AND txn_id = ?",
                (i["dataset_id"], i["txn_id"]),
            ).fetchone()
            assert txn is not None and txn["source_row"] == i["source_row"]
            assert (
                i["message"] and i["observed"] and i["severity"] in ("info", "warning", "error", "critical")
            )
