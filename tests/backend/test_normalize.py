"""P4: fix workflow — proposed → accepted/rejected → applied (logged) → reverted; data and evidence safety."""

import hashlib
import json
import sqlite3

import pytest

from ledgerspy.cases import audit, evidence
from ledgerspy.errors import LedgerSpyError
from ledgerspy.normalize import service as norm
from ledgerspy.normalize.transforms import clean_identifier, collapse_whitespace, safe_pipeline
from ledgerspy.normalize.view import canonical_value, effective_value
from ledgerspy.quality.engine import run_integrity
from tests.backend.conftest import QUALITY, load_quality_fixture


@pytest.fixture
def normalized(ingest):
    ds = load_quality_fixture(ingest, "normalization.csv")
    report = run_integrity(ingest.conn, ingest.cfg, ds["dataset_id"], "tester")
    fixes = {(f["source_row"], f["field"]): f for f in norm.list_fixes(ingest.conn, ds["dataset_id"])}
    return ingest, ds["dataset_id"], report, fixes


def accept(h, dsid, *fixes, value=None):
    return norm.decide(
        h.conn,
        dsid,
        [{"fix_id": f["fix_id"], "decision": "accept", "value": value} for f in fixes],
        "analyst",
    )


# ---------- pure transforms ----------


def test_transforms():
    assert collapse_whitespace("Acme   Supplies\tLtd ") == "Acme Supplies Ltd"
    assert clean_identifier("RCPT​-0077") == "RCPT-0077"
    assert clean_identifier("５０２０") == "5020"
    assert safe_pipeline("pay  ment", whitespace=True, identifier=False, case="upper") == (
        "PAY MENT",
        ["collapse_whitespace", "case_upper"],
    )
    assert safe_pipeline("OK", whitespace=True, identifier=False, case="upper") == ("OK", [])


# ---------- proposals ----------


def test_proposals_are_complete_and_change_nothing(normalized):
    h, dsid, _, fixes = normalized
    assert {k: (f["transform"], f["original_value"], f["proposed_value"]) for k, f in fixes.items()} == {
        (1, "payee_raw"): ("collapse_whitespace", "Acme  Supplies Ltd", "Acme Supplies Ltd"),
        (2, "account"): ("identifier_normalize", "５０２０", "5020"),
        (2, "description"): ("collapse_whitespace", "Courier   charges", "Courier charges"),
        (2, "txn_type"): ("case_upper", "payment", "PAYMENT"),
        (3, "reference"): ("identifier_normalize", "RCPT​-0077", "RCPT-0077"),
        (3, "txn_type"): ("case_upper", "receipt", "RECEIPT"),
    }
    assert all(f["status"] == "proposed" and not f["requires_confirmation"] for f in fixes.values())
    assert h.conn.execute("SELECT COUNT(*) FROM transformations").fetchone()[0] == 0
    assert h.conn.execute("SELECT COUNT(*) FROM normalized_values").fetchone()[0] == 0


def test_rerun_keeps_fix_ids_and_does_not_duplicate(normalized):
    h, dsid, _, fixes = normalized
    run_integrity(h.conn, h.cfg, dsid, "tester")
    again = {(f["source_row"], f["field"]): f["fix_id"] for f in norm.list_fixes(h.conn, dsid)}
    assert again == {k: f["fix_id"] for k, f in fixes.items()}


def test_nothing_is_applied_without_acceptance(normalized):
    h, dsid, _, _ = normalized
    result = norm.apply_accepted(h.conn, dsid, "analyst")
    assert result["applied"] == [] and result["conflicts"] == []
    assert h.conn.execute("SELECT COUNT(*) FROM normalized_values").fetchone()[0] == 0


# ---------- accepted fix → real, logged transformation ----------


def test_accepted_fix_is_applied_and_logged(normalized):
    h, dsid, before, fixes = normalized
    fix = fixes[(3, "reference")]
    accepted = accept(h, dsid, fix)
    assert accepted[0]["status"] == "accepted" and accepted[0]["decided_by"] == "analyst"
    result = norm.apply_accepted(h.conn, dsid, "analyst")
    (t,) = result["applied"]
    assert (t["field"], t["original_value"], t["normalized_value"]) == ("reference", "RCPT​-0077", "RCPT-0077")
    log = norm.list_transformations(h.conn, dsid)
    assert len(log) == 1
    entry = log[0]
    assert entry["kind"] == "apply" and entry["fix_id"] == fix["fix_id"] and entry["txn_id"] == fix["txn_id"]
    assert (
        entry["transform"] == "identifier_normalize" and entry["reason"] and entry["applied_by"] == "analyst"
    )
    assert entry["normalize_run_id"].startswith("M-") and entry["applied_at"]
    # the normalised view changed; the canonical P3 row did not
    assert effective_value(h.conn, dsid, fix["txn_id"], "reference") == "RCPT-0077"
    assert canonical_value(h.conn, dsid, fix["txn_id"], "reference") == "RCPT​-0077"
    # the malformed_identifier issue is gone on the next run; readiness goes up
    after = run_integrity(h.conn, h.cfg, dsid, "tester")
    assert after["summary"]["issues_by_check"]["malformed_identifier"] == 0
    assert after["readiness"]["score"] > before["readiness"]["score"]
    assert after["data_version"]["normalized_values"] == 1
    assert norm.list_fixes(h.conn, dsid, "applied")[0]["fix_id"] == fix["fix_id"]


def test_applying_all_safe_fixes_reaches_full_readiness(normalized):
    h, dsid, _, fixes = normalized
    accept(h, dsid, *fixes.values())
    assert len(norm.apply_accepted(h.conn, dsid, "analyst")["applied"]) == 6
    after = run_integrity(h.conn, h.cfg, dsid, "tester")
    assert after["summary"]["issues_by_check"]["needs_normalization"] == 0
    assert after["readiness"]["score"] == 100.0 and after["readiness"]["status"] == "ready"


# ---------- rejected fix ----------


def test_rejected_fix_is_never_applied(normalized):
    h, dsid, _, fixes = normalized
    fix = fixes[(1, "payee_raw")]
    with pytest.raises(LedgerSpyError) as e:
        norm.decide(h.conn, dsid, [{"fix_id": fix["fix_id"], "decision": "reject"}], "analyst")
    assert "reason" in e.value.details["errors"][0]["message"]
    norm.decide(
        h.conn,
        dsid,
        [{"fix_id": fix["fix_id"], "decision": "reject", "reason": "vendor master uses the double space"}],
        "analyst",
    )
    assert norm.apply_accepted(h.conn, dsid, "analyst")["applied"] == []
    assert effective_value(h.conn, dsid, fix["txn_id"], "payee_raw") == "Acme  Supplies Ltd"
    run_integrity(h.conn, h.cfg, dsid, "tester")  # re-detection keeps the decision
    (row,) = norm.list_fixes(h.conn, dsid, "rejected")
    assert row["decision_reason"] == "vendor master uses the double space"
    decisions = h.conn.execute("SELECT decision, reason FROM fix_decisions").fetchall()
    assert [tuple(d) for d in decisions] == [("reject", "vendor master uses the double space")]


# ---------- reversibility ----------


@pytest.mark.parametrize(
    "key",
    [
        (1, "payee_raw"),
        (2, "account"),
        (2, "description"),
        (2, "txn_type"),
        (3, "reference"),
        (3, "txn_type"),
    ],
)
def test_original_normalized_reverse_reconstructs_the_canonical_value(normalized, key):
    h, dsid, _, fixes = normalized
    fix = fixes[key]
    txn, field = fix["txn_id"], key[1]
    original = canonical_value(h.conn, dsid, txn, field)
    accept(h, dsid, fix)
    (t,) = norm.apply_accepted(h.conn, dsid, "analyst")["applied"]
    assert effective_value(h.conn, dsid, txn, field) == fix["proposed_value"] != original
    result = norm.revert(h.conn, dsid, t["transformation_id"], "analyst", "wrong call")
    assert result["restored_value"] == original
    assert effective_value(h.conn, dsid, txn, field) == original
    assert h.conn.execute("SELECT COUNT(*) FROM normalized_values").fetchone()[0] == 0
    log = norm.list_transformations(h.conn, dsid)
    assert [x["kind"] for x in log] == ["apply", "revert"]
    assert log[1]["reverts"] == t["transformation_id"] and log[1]["normalized_value"] == original
    assert norm.list_fixes(h.conn, dsid, "reverted")[0]["fix_id"] == fix["fix_id"]


def test_the_log_alone_reconstructs_every_original(normalized):
    h, dsid, _, fixes = normalized
    accept(h, dsid, *fixes.values())
    norm.apply_accepted(h.conn, dsid, "analyst")
    for entry in norm.list_transformations(h.conn, dsid):
        assert entry["original_value"] == canonical_value(h.conn, dsid, entry["txn_id"], entry["field"])


def test_reinterpretation_needs_a_choice_and_reverts(ingest):
    ds = load_quality_fixture(ingest, "ambiguous.csv")
    dsid = ds["dataset_id"]
    run_integrity(ingest.conn, ingest.cfg, dsid, "tester")
    fixes = {f["source_row"]: f for f in norm.list_fixes(ingest.conn, dsid)}
    ambiguous = fixes[2]
    with pytest.raises(LedgerSpyError) as e:
        accept(ingest, dsid, ambiguous)
    assert e.value.details["errors"][0]["candidates"] == ["2024-04-05T00:00:00", "2024-05-04T00:00:00"]
    with pytest.raises(LedgerSpyError):
        accept(ingest, dsid, ambiguous, value="2024-01-01T00:00:00")  # not a candidate
    accept(ingest, dsid, ambiguous, value="2024-05-04T00:00:00")
    (t,) = norm.apply_accepted(ingest.conn, dsid, "analyst")["applied"]
    assert t["original_value"] is None and t["normalized_value"] == "2024-05-04T00:00:00"
    report = run_integrity(ingest.conn, ingest.cfg, dsid, "tester")
    assert report["summary"]["issues_by_check"]["unparseable_date"] == 1  # the other one is still open
    norm.revert(ingest.conn, dsid, t["transformation_id"], "analyst", "client confirmed day-first")
    assert effective_value(ingest.conn, dsid, ambiguous["txn_id"], "posted_at") is None


def test_reinterpreted_amount(ingest):
    ds = load_quality_fixture(ingest, "invalid_numeric.csv")
    run_integrity(ingest.conn, ingest.cfg, ds["dataset_id"], "tester")
    (fix,) = norm.list_fixes(ingest.conn, ds["dataset_id"])
    assert fix["transform"] == "reinterpret_amount" and fix["candidates"] == [123450]
    accept(ingest, ds["dataset_id"], fix)  # single candidate: confirming it is enough
    (t,) = norm.apply_accepted(ingest.conn, ds["dataset_id"], "analyst")["applied"]
    assert t["normalized_value"] == 123450
    after = run_integrity(ingest.conn, ingest.cfg, ds["dataset_id"], "tester")
    assert after["summary"]["issues_by_check"]["invalid_value"] == 2


def test_stacked_transformations_revert_in_order(normalized):
    h, dsid, _, fixes = normalized
    fix = fixes[(2, "txn_type")]
    accept(h, dsid, fix)
    (t1,) = norm.apply_accepted(h.conn, dsid, "analyst")["applied"]
    # a second, hand-crafted proposal on top of the first value
    from ledgerspy.normalize.proposals import Proposal, fix_id
    from ledgerspy.normalize.service import save_proposals
    from ledgerspy.normalize.view import encode

    second = Proposal(
        fix_id(dsid, fix["txn_id"], "txn_type", "case_lower", encode("PAYMENT")),
        fix["txn_id"],
        2,
        "txn_type",
        "case_lower",
        encode("PAYMENT"),
        encode("payment"),
        [encode("payment")],
        False,
        "test",
    )
    from ledgerspy.cases.db import transaction

    with transaction(h.conn):
        save_proposals(h.conn, dsid, [second], "Q-TEST")
    accept(h, dsid, {"fix_id": second.fix_id})
    (t2,) = norm.apply_accepted(h.conn, dsid, "analyst")["applied"]
    with pytest.raises(LedgerSpyError) as e:
        norm.revert(h.conn, dsid, t1["transformation_id"], "analyst", "x")
    assert e.value.code == "not_latest"
    norm.revert(h.conn, dsid, t2["transformation_id"], "analyst", "x")
    assert effective_value(h.conn, dsid, fix["txn_id"], "txn_type") == "PAYMENT"
    norm.revert(h.conn, dsid, t1["transformation_id"], "analyst", "x")
    assert effective_value(h.conn, dsid, fix["txn_id"], "txn_type") == "payment"
    with pytest.raises(LedgerSpyError) as e:
        norm.revert(h.conn, dsid, t1["transformation_id"], "analyst", "x")
    assert e.value.code == "already_reverted"


def test_conflicting_fix_is_not_applied(normalized):
    h, dsid, _, fixes = normalized
    accept(h, dsid, fixes[(2, "txn_type")])
    norm.apply_accepted(h.conn, dsid, "analyst")
    # force the stale fix back to accepted: the field no longer holds the value it was proposed for
    h.conn.execute(
        "UPDATE fix_proposals SET status = 'accepted' WHERE fix_id = ?", (fixes[(2, "txn_type")]["fix_id"],)
    )
    result = norm.apply_accepted(h.conn, dsid, "analyst")
    assert result["applied"] == [] and result["conflicts"][0]["found"] == "PAYMENT"


def test_applied_fix_cannot_be_redecided(normalized):
    h, dsid, _, fixes = normalized
    accept(h, dsid, fixes[(2, "txn_type")])
    norm.apply_accepted(h.conn, dsid, "analyst")
    with pytest.raises(LedgerSpyError) as e:
        accept(h, dsid, fixes[(2, "txn_type")])
    assert "revert it first" in e.value.details["errors"][0]["message"]


# ---------- safety ----------


def test_canonical_rows_and_logs_cannot_be_edited(normalized):
    h, dsid, _, fixes = normalized
    accept(h, dsid, fixes[(1, "payee_raw")])
    norm.apply_accepted(h.conn, dsid, "analyst")
    for sql in (
        "UPDATE transactions SET payee_raw = 'x'",
        "DELETE FROM transactions",
        "UPDATE transformations SET normalized_value = '\"x\"'",
        "DELETE FROM transformations",
        "UPDATE integrity_runs SET readiness = 100",
        "DELETE FROM integrity_issues",
        "UPDATE fix_decisions SET decision = 'reject'",
    ):
        with pytest.raises(sqlite3.DatabaseError):
            h.conn.execute(sql)
    h.conn.execute(
        "UPDATE transactions SET payee_entity_id = 'E-x' WHERE source_row = 1"
    )  # P12 stays possible


def test_p3_issues_and_labels_are_immutable(ingest):
    from tests.backend.conftest import FIXTURES

    ds = ingest.upload(FIXTURES / "blank_rows_and_gaps.csv")
    ingest.confirm(
        ds,
        {
            "adapter": "generic_ledger",
            "fields": {"posted_at": "Date", "payee_raw": "Vendor", "amount": "Amount", "direction": "Dr/Cr"},
            "options": {"decimal_separator": ".", "date_format": "%Y-%m-%d"},
        },
    )
    labelled = ingest.upload(FIXTURES / "ledger_normal.csv")
    ingest.confirm(labelled)
    assert ingest.conn.execute("SELECT COUNT(*) FROM ingest_issues").fetchone()[0] == 5
    assert ingest.conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0] == 4
    for sql in (
        "UPDATE ingest_issues SET code = 'x'",
        "DELETE FROM ingest_issues",
        "UPDATE labels SET value = '0'",
        "DELETE FROM labels",
    ):
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            ingest.conn.execute(sql)


def test_evidence_is_untouched_by_validation_and_normalisation(normalized):
    h, dsid, _, fixes = normalized
    original = hashlib.sha256((QUALITY / "normalization.csv").read_bytes()).hexdigest()
    accept(h, dsid, *fixes.values())
    t = norm.apply_accepted(h.conn, dsid, "analyst")["applied"][0]
    norm.revert(h.conn, dsid, t["transformation_id"], "analyst", "check")
    run_integrity(h.conn, h.cfg, dsid, "tester")
    ev = h.conn.execute("SELECT * FROM evidence_files").fetchone()
    assert ev["sha256"] == original
    path = h.workspace / "evidence" / ev["stored_name"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original
    assert evidence.verify_all(h.conn, h.workspace) == []


def test_every_step_is_in_the_verified_audit_chain(normalized):
    h, dsid, _, fixes = normalized
    accept(h, dsid, fixes[(2, "txn_type")])
    norm.decide(
        h.conn,
        dsid,
        [{"fix_id": fixes[(3, "txn_type")]["fix_id"], "decision": "reject", "reason": "keep source case"}],
        "analyst",
    )
    t = norm.apply_accepted(h.conn, dsid, "analyst")["applied"][0]
    norm.revert(h.conn, dsid, t["transformation_id"], "analyst", "undo")
    actions = [r[0] for r in h.conn.execute("SELECT action FROM audit_log ORDER BY seq")]
    assert actions[-5:] == [
        "integrity.run",
        "fix.accepted",
        "fix.rejected",
        "dataset.normalized",
        "transformation.reverted",
    ]
    assert audit.verify(h.conn).ok
    details = json.loads(
        h.conn.execute("SELECT details FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()[0]
    )
    assert details["restored_value"] == "payment" and details["reason"] == "undo"
