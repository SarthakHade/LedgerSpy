"""P15 investigation workflow on real imports (P5 synthetic ledger + masters, a P5-derived PDF statement, the
P5 CSV statement, reconciliation runs): lifecycle, links, notes, decisions, OCR corrections and invalidation,
audit, persistence, API security. Synthetic data only; ground truth is never read."""

import hashlib
import os
import secrets
import sqlite3
import stat
from datetime import date

import pytest
from fastapi.testclient import TestClient

from ledgerspy.api.app import create_app
from ledgerspy.cases import audit, store
from ledgerspy.cases.db import MIGRATIONS, SCHEMA_VERSION, connect, current_version, db_path_for, init_db
from ledgerspy.config import load_config
from ledgerspy.errors import LedgerSpyError
from ledgerspy.graph import service as graph_service
from ledgerspy.investigations import model
from ledgerspy.investigations import service as inv
from ledgerspy.pdfstatements import corrections
from ledgerspy.pdfstatements import service as pdf_service
from ledgerspy.reconciliation import service as recon
from ledgerspy.resolution import masters, validation
from ledgerspy.resolution import service as resolution_service
from ledgerspy.risk import service as risk_service
from tests.backend.conftest import BASE_URL, FIXTURES, PORT, make_ingest
from tests.backend.test_features_datasets import REFERENCE, case_overrides
from tests.backend.test_forensics_datasets import _table_digest, imported
from tests.backend.test_pdf_datasets import p5_bank_rows, p5_pdf

A = "analyst"


def scope(su, value):
    p = next(x for x in su["proposals"]["scopes"] if x["ledger_value"] == value)
    return recon.ScopeIn(
        account_key=p["account_key"], ledger_column=p["ledger_column"], ledger_value=p["ledger_value"]
    )


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    h = make_ingest(tmp_path_factory.mktemp("ws"))
    other_case = h.case_id
    other_ds = imported(h, FIXTURES / "ledger_normal.csv")
    h.case_id = store.create_case(
        h.conn, name="syn", actor="t", period_start=date(2023, 4, 1), period_end=date(2026, 3, 31)
    )["case_id"]
    led = imported(h, REFERENCE / "SYNTHETIC_ledger.csv")
    for etype, (fname, mapping) in validation.P5_FILES.items():
        masters.import_master(
            h.conn, h.cfg, h.workspace, h.case_id, REFERENCE / fname, etype, "t", mapping=mapping
        )
    cfg = load_config(
        overrides={"workspace": {"path": str(h.workspace)}, "server": {"port": PORT}, **case_overrides()}
    )
    csv_bank = h.upload(REFERENCE / "SYNTHETIC_bank_statement.csv", kind="bank_statement")
    h.confirm(csv_bank)
    pdf = h.upload(p5_pdf(p5_bank_rows()), "lve.pdf", kind="bank_statement")
    h.confirm(pdf)
    su = recon.setup(h.conn, cfg, led, csv_bank["dataset_id"])
    run_csv = recon.start(
        h.conn,
        cfg,
        recon.RunRequest(
            ledger_dataset_id=led,
            bank_dataset_id=csv_bank["dataset_id"],
            scopes=[scope(su, "LVE"), scope(su, "LVC")],
            ledger_debit_means="outflow",
        ),
        "t",
    )
    risk_service.clear_cache()
    p9 = risk_service.run_risk(h.conn, cfg, led)
    digest = _table_digest(h.conn)
    yield {
        "h": h,
        "cfg": cfg,
        "led": led,
        "csv": csv_bank["dataset_id"],
        "pdf": pdf["dataset_id"],
        "run": run_csv,
        "p9": p9.result_sha256,
        "signals_sha": hashlib.sha256(repr(sorted(p9.signals)).encode()).hexdigest(),
        "digest": digest,
        "other_case": other_case,
        "other_ds": other_ds,
        "su": su,
    }
    h.conn.close()


def new(env, title="Review", links=None, priority="medium"):
    return inv.create(
        env["h"].conn, env["cfg"], env["h"].case_id, title, "Factual description.", priority, A, links or []
    )


# ------------------------------------------------------------------------------------------ lifecycle


def test_lifecycle_valid_and_invalid_transitions(env):
    c = env["h"].conn
    i = new(env)["investigation_id"]
    assert inv.get(c, i)["status"] == "open" and inv.get(c, i)["allowed_transitions"] == [
        "in_review",
        "awaiting_evidence",
        "closed",
    ]
    with pytest.raises(LedgerSpyError) as e:
        inv.transition(c, i, "concluded", A)
    assert e.value.code == "invalid_transition" and e.value.http_status == 409
    inv.transition(c, i, "in_review", A)
    inv.transition(c, i, "awaiting_evidence", A, reason="Waiting for the bank's remittance advice.")
    inv.transition(c, i, "in_review", A)
    with pytest.raises(LedgerSpyError) as e:
        inv.transition(c, i, "concluded", A)
    assert e.value.code == "outcome_required"
    with pytest.raises(LedgerSpyError) as e:
        inv.transition(c, i, "concluded", A, outcome="inconclusive", conclusion="short")
    assert e.value.code == "rationale_required"
    x = inv.transition(
        c,
        i,
        "concluded",
        A,
        outcome="inconclusive",
        conclusion="The records neither confirm nor rule out the concern.",
    )
    assert x["outcome"] == "inconclusive" and x["status"] == "concluded"
    with pytest.raises(LedgerSpyError) as e:
        inv.transition(c, i, "closed", A)
    assert e.value.code == "rationale_required"
    x = inv.transition(c, i, "closed", A, reason="Concluded and documented for the file.")
    assert x["closure_rationale"].startswith("Concluded") and x["allowed_transitions"] == ["in_review"]
    for fn in (
        lambda: inv.add_note(c, i, "late note", None, A),
        lambda: inv.decide(c, env["cfg"], i, None, "reviewed_no_issue", "after closure", A),
        lambda: inv.update(c, i, {"title": "x"}, A),
        lambda: inv.correct_ocr(
            c, env["cfg"], i, env["pdf"], "PX-0000000000000000", "reference", "X1", "after closure", A
        ),
    ):
        with pytest.raises(LedgerSpyError) as e:
            fn()
        assert e.value.code == "investigation_closed"
    with pytest.raises(LedgerSpyError):
        inv.transition(c, i, "in_review", A, reason="short")
    inv.transition(c, i, "in_review", A, reason="New evidence received from the bank.")
    events = inv.activity_page(c, i, 1, 100)["items"]
    assert [(e["from_status"], e["to_status"]) for e in events if e["event"] == "status_changed"] == [
        ("open", "in_review"),
        ("in_review", "awaiting_evidence"),
        ("awaiting_evidence", "in_review"),
        ("in_review", "concluded"),
        ("concluded", "closed"),
        ("closed", "in_review"),
    ]
    seqs = [e["audit_seq"] for e in events]
    assert seqs == sorted(seqs) and audit.verify(c).ok
    acts = {r[0] for r in c.execute("SELECT action FROM audit_log WHERE object = ?", (f"investigation:{i}",))}
    assert {"investigation.created", "investigation.status_changed"} <= acts


def test_create_validation_and_no_invented_identity(env):
    c = env["h"].conn
    with pytest.raises(LedgerSpyError) as e:
        inv.create(c, env["cfg"], "C-01J00000000000000000000000", "t", "", "low", A)
    assert e.value.code == "case_not_found"
    with pytest.raises(LedgerSpyError):
        inv.create(c, env["cfg"], env["h"].case_id, "t", "", "urgent", A)
    with pytest.raises(LedgerSpyError):
        inv.create(c, env["cfg"], env["h"].case_id, "   ", "", "low", A)
    x = new(env)
    assert x["created_by"] == A and x["outcome"] is None and x["conclusion"] is None  # nothing invented


# ------------------------------------------------------------------------------------------ links


def test_every_link_type_resolves_within_scope(env):
    h, cfg, c = env["h"], env["cfg"], env["h"].conn
    run = env["run"]
    res = risk_service.run_risk(c, cfg, env["led"])
    signal = sorted(res.signals)[0]
    top = res.rows[0]["txn_id"]
    ent = res.entities[0]["entity_id"]
    rr = resolution_service.run(c, cfg, env["led"])
    pair = sorted(rr.res.pairs)[0]
    centity = rr.res.entities[0]["canonical_entity_id"]
    g = graph_service.run(c, cfg, env["led"])
    gnode = sorted(g.graph.nodes)[0]
    gedge = g.graph.edges[0]["edge_id"]
    gcycle = g.cycles["cycles"][0]["cycle_id"]
    gsig = g.signals[0]["signal_id"]
    amb = next(r for r in run.results if r["status"] == "ambiguous")
    item = pdf_service.items_page(c, env["pdf"], "transaction", None, None, 1, 1)["items"][0]["item_id"]
    led_sha = c.execute(
        "SELECT evidence_sha256 FROM datasets WHERE dataset_id = ?", (env["led"],)
    ).fetchone()[0]
    specs = [
        ("transaction", env["led"], None, top),
        (
            "transaction",
            env["csv"],
            None,
            run.results[0]["bank_txn_ids"][0] if run.results[0]["bank_txn_ids"] else amb["bank_txn_ids"][0],
        ),
        ("risk_transaction", env["led"], None, top),
        ("risk_entity", env["led"], None, ent),
        ("signal", env["led"], None, signal),
        ("entity", env["led"], None, centity),
        ("entity_pair", env["led"], None, pair),
        ("graph_node", env["led"], None, gnode),
        ("graph_edge", env["led"], None, gedge),
        ("graph_cycle", env["led"], None, gcycle),
        ("graph_signal", env["led"], None, gsig),
        ("reconciliation_run", None, run.run_id, run.run_id),
        ("reconciliation_result", None, run.run_id, amb["result_id"]),
        ("reconciliation_finding", None, run.run_id, run.findings[0]["signal_id"]),
        ("duplicate_group", None, run.run_id, run.duplicates[0]["group_id"]),
        ("evidence_file", None, None, led_sha),
        ("pdf_page", env["pdf"], None, "2"),
        ("pdf_item", env["pdf"], None, item),
    ]
    assert {s[0] for s in specs} == set(model.LINK_TYPES)
    i = new(env, "All evidence types")["investigation_id"]
    for t, ds, rid, ref in specs:
        out = inv.link(
            c,
            cfg,
            i,
            {"link_type": t, "dataset_id": ds, "run_id": rid, "ref_id": ref, "reason": f"evidence {t}"},
            A,
        )
        assert out["active"] and len(out["snapshot_sha256"]) == 64, t
        assert inv.link_detail(c, cfg, i, out["link_id"])["verification"]["state"] == "current", t
    links = inv.links_page(c, i, True, None, 1, 100)
    assert links["total"] == len(specs)
    assert {x["link_type"]: x["finding_status"] for x in links["items"]}["risk_transaction"] == "new"
    with pytest.raises(LedgerSpyError) as e:
        inv.link(
            c,
            cfg,
            i,
            {"link_type": "transaction", "dataset_id": env["led"], "ref_id": top, "reason": "again"},
            A,
        )
    assert e.value.code == "duplicate_link" and e.value.http_status == 409
    m = inv.manifest(c, h.workspace, i)
    assert {f["verification"] for f in m["evidence_verification"]} == {"verified"} and led_sha in m[
        "evidence_files"
    ]
    assert inv.manifest(c, h.workspace, i)["manifest_sha256"] == m["manifest_sha256"]  # deterministic


def test_invalid_cross_scope_and_hostile_references(env):
    c, cfg = env["h"].conn, env["cfg"]
    i = new(env, "hostile")["investigation_id"]

    def bad(spec, code):
        with pytest.raises(LedgerSpyError) as e:
            inv.link(c, cfg, i, {"reason": "test link"} | spec, A)
        assert e.value.code == code, (spec, e.value.code)

    bad({"link_type": "transaction", "dataset_id": env["other_ds"], "ref_id": "x"}, "cross_scope_reference")
    bad(
        {"link_type": "transaction", "dataset_id": env["led"], "ref_id": "../../etc/passwd"},
        "invalid_reference",
    )
    bad(
        {"link_type": "transaction", "dataset_id": env["led"], "ref_id": "C:\\Windows\\win.ini"},
        "invalid_reference",
    )
    bad({"link_type": "transaction", "dataset_id": env["led"], "ref_id": "NOPE-1"}, "not_found")
    bad({"link_type": "transaction", "dataset_id": "../x", "ref_id": "x"}, "invalid_reference")
    bad({"link_type": "evidence_file", "ref_id": "../" + "a" * 61}, "invalid_reference")
    bad({"link_type": "evidence_file", "ref_id": "a" * 64}, "not_found")
    other_sha = c.execute(
        "SELECT evidence_sha256 FROM datasets WHERE dataset_id = ?", (env["other_ds"],)
    ).fetchone()[0]
    bad({"link_type": "evidence_file", "ref_id": other_sha}, "cross_scope_reference")
    bad({"link_type": "pdf_page", "dataset_id": env["pdf"], "ref_id": "999"}, "not_found")
    bad({"link_type": "pdf_page", "dataset_id": env["led"], "ref_id": "1"}, "not_a_pdf_dataset")
    bad({"link_type": "risk_transaction", "dataset_id": env["csv"], "ref_id": "x"}, "wrong_dataset_kind")
    bad(
        {
            "link_type": "reconciliation_result",
            "run_id": "RR-0000000000000000",
            "ref_id": "RM-0000000000000000",
        },
        "not_found",
    )
    bad(
        {"link_type": "signal", "dataset_id": env["led"], "ref_id": "S-; DROP TABLE cases"},
        "invalid_reference",
    )
    bad({"link_type": "bogus", "ref_id": "x"}, "invalid_link_type")
    bad(
        {"link_type": "transaction", "dataset_id": env["led"], "run_id": env["run"].run_id, "ref_id": "x"},
        "invalid_reference",
    )
    bad({"link_type": "transaction", "dataset_id": env["led"], "ref_id": "LVC\x00JV"}, "invalid_reference")
    with pytest.raises(LedgerSpyError) as e:
        inv.link(
            c,
            cfg,
            i,
            {"link_type": "transaction", "dataset_id": env["led"], "ref_id": "LVC-JV-000001", "reason": ""},
            A,
        )
    assert e.value.code == "rationale_required"
    # Nothing was written for any refused request.
    assert (
        c.execute("SELECT COUNT(*) FROM investigation_links WHERE investigation_id = ?", (i,)).fetchone()[0]
        == 0
    )
    assert [e["event"] for e in inv.activity_page(c, i, 1, 100)["items"]] == ["created"]


def test_unlink_removes_only_the_case_link(env):
    c, cfg = env["h"].conn, env["cfg"]
    i = new(env, "unlink")["investigation_id"]
    lk = inv.link(
        c,
        cfg,
        i,
        {
            "link_type": "transaction",
            "dataset_id": env["led"],
            "ref_id": "LVC-JV-000001",
            "reason": "first posting",
        },
        A,
    )
    out = inv.unlink(c, i, lk["link_id"], "Linked by mistake.", A)
    assert out["removed"] and "unchanged" in out["note"]
    d = inv.link_detail(c, cfg, i, lk["link_id"])
    assert d["active"] is False and d["removal"]["reason"] == "Linked by mistake."
    assert c.execute("SELECT COUNT(*) FROM transactions WHERE txn_id = 'LVC-JV-000001'").fetchone()[0] == 1
    with pytest.raises(LedgerSpyError) as e:
        inv.unlink(c, i, lk["link_id"], "again please", A)
    assert e.value.code == "already_unlinked"
    with pytest.raises(LedgerSpyError) as e:
        inv.decide(c, cfg, i, lk["link_id"], "reviewed_no_issue", "after removal", A)
    assert e.value.code == "link_removed"
    again = inv.link(
        c,
        cfg,
        i,
        {
            "link_type": "transaction",
            "dataset_id": env["led"],
            "ref_id": "LVC-JV-000001",
            "reason": "re-linked with the right reason",
        },
        A,
    )
    assert again["link_id"] != lk["link_id"]  # a new link; the removed one stays in history
    assert inv.links_page(c, i, None, None, 1, 10)["total"] == 2
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute("DELETE FROM investigation_link_removals")
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        c.execute("UPDATE investigation_links SET reason = 'x'")


# ------------------------------------------------------------------------------------------ notes / decisions


def test_notes_and_decisions_are_append_only_with_history(env):
    c, cfg = env["h"].conn, env["cfg"]
    i = new(env, "decisions")["investigation_id"]
    res = risk_service.run_risk(c, cfg, env["led"])
    t = res.rows[0]["txn_id"]
    lk = inv.link(
        c,
        cfg,
        i,
        {"link_type": "risk_transaction", "dataset_id": env["led"], "ref_id": t, "reason": "P9 lead"},
        A,
    )
    ev = inv.link(
        c,
        cfg,
        i,
        {"link_type": "transaction", "dataset_id": env["led"], "ref_id": t, "reason": "the posting itself"},
        A,
    )
    xss = "<img src=x onerror=alert(1)> invoice checked"
    n = inv.add_note(c, i, xss, lk["link_id"], A)
    assert n["body"] == xss  # stored verbatim; the UI renders it as text
    with pytest.raises(LedgerSpyError) as e:
        inv.decide(c, cfg, i, lk["link_id"], "duplicate_confirmed", "not a duplicate group", A)
    assert e.value.code == "invalid_action"
    with pytest.raises(LedgerSpyError) as e:
        inv.decide(c, cfg, i, lk["link_id"], "finding_dismissed", "  ", A)
    assert e.value.code == "rationale_required"
    with pytest.raises(LedgerSpyError):
        inv.decide(c, cfg, i, lk["link_id"], "finding_confirmed", "supported", A, supporting=["LK-nothere"])
    d1 = inv.decide(
        c,
        cfg,
        i,
        lk["link_id"],
        "further_investigation_required",
        "Need the invoice.",
        A,
        supporting=[ev["link_id"]],
    )
    assert d1["unresolved"] is True and d1["supporting"] == [ev["link_id"]]
    d2 = inv.decide(
        c,
        cfg,
        i,
        lk["link_id"],
        "finding_dismissed",
        "Invoice INV-1 supports the payment.",
        A,
        contradicting=[ev["link_id"]],
    )
    assert d2["unresolved"] is False
    hist = inv.decisions_page(c, i, lk["link_id"], 1, 10)["items"]
    assert [x["action"] for x in hist] == ["further_investigation_required", "finding_dismissed"]
    assert inv.links_page(c, i, True, "risk_transaction", 1, 5)["items"][0]["finding_status"] == "dismissed"
    s = inv.summary(c, i)
    assert s["findings"]["by_analyst_status"] == {"dismissed": 1} and s["investigation"]["status"] == "open"
    for table in (
        "investigation_links",
        "investigation_link_removals",
        "investigation_notes",
        "investigation_decisions",
        "investigation_events",
    ):  # every table holds rows here, so each row trigger really fires (ocr_corrections: see the OCR tests)
        assert c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] > 0, table  # noqa: S608
        for sql in (f"UPDATE {table} SET rowid = rowid", f"DELETE FROM {table}"):  # noqa: S608 — fixed names
            with pytest.raises(sqlite3.DatabaseError, match="append-only|immutable"):
                c.execute(sql)
    with pytest.raises(sqlite3.DatabaseError):
        c.execute("DELETE FROM investigations")
    case_dec = inv.decide(c, cfg, i, None, "evidence_insufficient", "Bank confirmation outstanding.", A)
    assert case_dec["link_id"] is None and "case" in inv.summary(c, i)["decisions"]["unresolved_subjects"]


def test_ambiguity_resolution_needs_a_listed_candidate_and_leaves_the_run_unchanged(env):
    c, cfg, run = env["h"].conn, env["cfg"], env["run"]
    before = run.result_sha256
    amb = next(r for r in run.results if r["status"] == "ambiguous")
    exact = next(r for r in run.results if r["status"] == "exact_match")
    i = new(env, "ambiguity")["investigation_id"]
    la = inv.link(
        c,
        cfg,
        i,
        {
            "link_type": "reconciliation_result",
            "run_id": run.run_id,
            "ref_id": amb["result_id"],
            "reason": "two equal payments",
        },
        A,
    )
    le = inv.link(
        c,
        cfg,
        i,
        {
            "link_type": "reconciliation_result",
            "run_id": run.run_id,
            "ref_id": exact["result_id"],
            "reason": "a clean match",
        },
        A,
    )
    with pytest.raises(LedgerSpyError) as e:
        inv.decide(
            c,
            cfg,
            i,
            la["link_id"],
            "ambiguity_resolved",
            "pick",
            A,
            payload={"chosen": {"ledger_txn_ids": ["X"], "bank_txn_ids": ["Y"]}},
        )
    assert e.value.code == "invalid_resolution" and e.value.details["candidates"]
    cand = amb["candidates"][0]
    d = inv.decide(
        c,
        cfg,
        i,
        la["link_id"],
        "ambiguity_resolved",
        "Bank advice UTR matches invoice 1.",
        A,
        payload={
            "chosen": {"ledger_txn_ids": [cand["ledger_txn_id"]], "bank_txn_ids": [cand["bank_txn_id"]]}
        },
    )
    assert d["payload"]["effect"]["chosen"]["ledger_txn_ids"] == [cand["ledger_txn_id"]]
    with pytest.raises(LedgerSpyError) as e:
        inv.decide(c, cfg, i, le["link_id"], "ambiguity_resolved", "not ambiguous", A, payload={"chosen": {}})
    assert e.value.code == "not_ambiguous"
    with pytest.raises(LedgerSpyError) as e:
        inv.decide(c, cfg, i, la["link_id"], "match_accepted", "ambiguous is no match", A)
    assert e.value.code == "not_a_match"
    inv.decide(c, cfg, i, le["link_id"], "match_accepted", "Amount, date and payee agree with the advice.", A)
    detail = recon.result_detail(c, recon.get_run(c, cfg, run.run_id), amb["result_id"])
    assert [x["action"] for x in detail["analyst_decisions"]] == ["ambiguity_resolved"]
    assert detail["result"]["status"] == "ambiguous"  # the computed result is unchanged
    assert recon.get_run(c, cfg, run.run_id).result_sha256 == before


def test_duplicate_and_entity_decisions_are_recorded_only(env):
    c, cfg, run = env["h"].conn, env["cfg"], env["run"]
    rr_before = resolution_service.run(c, cfg, env["led"]).res.sha256
    i = new(env, "dups and entities")["investigation_id"]
    g = run.duplicates[0]
    lg = inv.link(
        c,
        cfg,
        i,
        {
            "link_type": "duplicate_group",
            "run_id": run.run_id,
            "ref_id": g["group_id"],
            "reason": "repeated payment",
        },
        A,
    )
    inv.decide(
        c, cfg, i, lg["link_id"], "duplicate_confirmed", "Same invoice paid twice per vendor ledger.", A
    )
    page = recon.duplicates_page(recon.get_run(c, cfg, run.run_id), None, 1, 100, c)
    row = next(x for x in page["items"] if x["group_id"] == g["group_id"])
    assert row["confirmed"] is False and row["analyst_decisions"][0]["action"] == "duplicate_confirmed"
    pair = sorted(resolution_service.run(c, cfg, env["led"]).res.pairs)[0]
    lp = inv.link(
        c,
        cfg,
        i,
        {
            "link_type": "entity_pair",
            "dataset_id": env["led"],
            "ref_id": pair,
            "reason": "possible same vendor",
        },
        A,
    )
    d = inv.decide(c, cfg, i, lp["link_id"], "entity_link_accepted", "Same bank account and tax ID.", A)
    assert "not merged" in d["action_text"]
    resolution_service.clear_cache()
    assert resolution_service.run(c, cfg, env["led"]).res.sha256 == rr_before  # no silent merge


# ------------------------------------------------------------------------------------------ OCR corrections


def test_correction_before_confirmation_is_an_overlay(env):
    h, cfg, c = env["h"], env["cfg"], env["h"].conn
    ds = h.upload(p5_pdf(p5_bank_rows()[:30]), "small.pdf", kind="bank_statement")
    did = ds["dataset_id"]
    item = pdf_service.items_page(c, did, "transaction", None, None, 1, 1)["items"][0]
    items_sha = pdf_service.summary(c, did)["extraction"]["items_sha256"]
    with pytest.raises(LedgerSpyError) as e:
        corrections.correct(c, cfg, did, item["item_id"], "debit", "1,0O0.00", "typo check", A)
    assert e.value.code == "invalid_value" if "debit" in item["fields"] else "invalid_field"
    with pytest.raises(LedgerSpyError) as e:
        corrections.correct(c, cfg, did, item["item_id"], "reference", "NEWREF0000000001", "", A)
    assert e.value.code == "rationale_required"
    fix = corrections.correct(
        c, cfg, did, item["item_id"], "reference", "NEWREF0000000001", "The page shows NEWREF0000000001.", A
    )
    assert fix["stage"] == "before_confirmation" and fix["original_value"] == item["fields"]["reference"]
    assert fix["page"] == item["page"] and fix["source_text"] == item["text"]
    assert pdf_service.summary(c, did)["extraction"]["items_sha256"] == items_sha  # extraction unchanged
    assert (
        pdf_service.item_detail(c, did, item["item_id"])["item"]["fields"]["reference"]
        == item["fields"]["reference"]
    )
    # A correction never makes an unconfirmed row trusted: were this row review-required, it would still need
    # an explicit "confirm" decision to reach the confirmed table (an exclusion drops it, corrected or not).
    eid = pdf_service.current_extraction_id(
        c.execute("SELECT * FROM datasets WHERE dataset_id = ?", (did,)).fetchone()
    )
    result = pdf_service.load_result(c, eid)
    for it in result["items"]:
        if it["item_id"] == item["item_id"]:
            it["review_required"] = True
    pending = pdf_service.pending_corrections(c, eid)
    assert set(pending) == {item["item_id"]}

    def tabled(decisions):
        header, rows = pdf_service.table(result, decisions, pending)
        col = header.index("pdf_item_id")
        return {r[col]: dict(zip(header, r, strict=True)) for r in rows}

    assert item["item_id"] not in tabled({})
    assert item["item_id"] not in tabled({item["item_id"]: "exclude"})
    confirmed = tabled({item["item_id"]: "confirm"})[item["item_id"]]
    assert confirmed["pdf_review"] == "reviewer_corrected" and "NEWREF0000000001" in confirmed.values()
    assert item["item_id"] in tabled(None)  # the upload preview still lists it, as review_required
    assert tabled(None)[item["item_id"]]["pdf_review"] == "review_required"
    h.confirm(ds)
    row = next(t for t in h.transactions(did) if t["txn_id"] == item["item_id"])
    assert row["reference"] == "NEWREF0000000001" and row["extra"]["pdf_review"] == "reviewer_corrected"
    assert item["fields"]["reference"] in row["extra"]["pdf_corrections"]
    stored = h.workspace / "evidence" / ds["evidence"]["stored_name"]
    assert hashlib.sha256(stored.read_bytes()).hexdigest() == ds["evidence"]["sha256"]


def test_correction_after_confirmation_invalidates_dependent_reconciliation(env):
    h, cfg, c = env["h"], env["cfg"], env["h"].conn
    ds = h.upload(p5_pdf(p5_bank_rows()[:40]), "forty.pdf", kind="bank_statement")
    did = ds["dataset_id"]
    h.confirm(ds)
    su = recon.setup(c, cfg, env["led"], did)
    req = recon.RunRequest(
        ledger_dataset_id=env["led"],
        bank_dataset_id=did,
        scopes=[scope(su, "LVE")],
        ledger_debit_means="outflow",
    )
    run = recon.start(c, cfg, req, "t")
    exact = next(r for r in run.results if r["status"] == "exact_match")
    bank_id = exact["bank_txn_ids"][0]
    i = new(env, "ocr")["investigation_id"]
    lr = inv.link(
        c,
        cfg,
        i,
        {
            "link_type": "reconciliation_result",
            "run_id": run.run_id,
            "ref_id": exact["result_id"],
            "reason": "match to re-check",
        },
        A,
    )
    canon_before = c.execute(
        "SELECT * FROM transactions WHERE dataset_id = ? AND txn_id = ?", (did, bank_id)
    ).fetchone()
    items_sha = pdf_service.summary(c, did)["extraction"]["items_sha256"]
    with pytest.raises(LedgerSpyError) as e:
        inv.correct_ocr(c, cfg, i, did, bank_id, "balance", "1,000.00", "balance misread", A)
    assert e.value.code == "not_correctable_after_confirmation"
    out = inv.correct_ocr(
        c, cfg, i, did, bank_id, "reference", "SYNRCORRECTED001", "Page 1 shows SYNRCORRECTED001.", A
    )
    fix = out["correction"]
    assert fix["stage"] == "after_confirmation" and fix["canonical_field"] == "reference"
    # the PDF evidence and the stored extraction are unchanged; the correction is audited
    assert pdf_service.summary(c, did)["extraction"]["items_sha256"] == items_sha
    stored = h.workspace / "evidence" / ds["evidence"]["stored_name"]
    assert hashlib.sha256(stored.read_bytes()).hexdigest() == ds["evidence"]["sha256"]
    assert c.execute(
        "SELECT 1 FROM audit_log WHERE action = 'ocr.corrected' AND details LIKE ?",
        ("%" + fix["correction_id"] + "%",),
    ).fetchone()
    assert audit.verify(c).ok
    assert (
        fix["invalidated_reconciliation_runs"] == [run.run_id]
        and out["decision"]["action"] == "ocr_row_corrected"
    )
    t = c.execute(
        "SELECT * FROM transformations WHERE transformation_id = ?", (fix["transformation_id"],)
    ).fetchone()
    assert t["transform"] == "ocr_correction" and t["kind"] == "apply"
    assert dict(
        c.execute("SELECT * FROM transactions WHERE dataset_id = ? AND txn_id = ?", (did, bank_id)).fetchone()
    ) == dict(canon_before)  # canonical row untouched: the correction is a logged overlay
    view_val = c.execute(
        "SELECT value FROM normalized_values WHERE dataset_id = ? AND txn_id = ? AND field = 'reference'",
        (did, bank_id),
    ).fetchone()[0]
    assert view_val == '"SYNRCORRECTED001"'
    assert (
        next(r for r in recon.list_runs(c, None, did)["items"] if r["run_id"] == run.run_id)["stale"] is True
    )
    with pytest.raises(LedgerSpyError) as e:
        recon.get_run(c, cfg, run.run_id)  # the cached run is not served as valid
    assert e.value.code == "inputs_changed"
    assert inv.link_detail(c, cfg, i, lr["link_id"])["verification"]["state"] == "stale"
    new_run = recon.start(c, cfg, req, "t")
    assert (
        new_run.run_id != run.run_id
        and not next(r for r in recon.list_runs(c, None, did)["items"] if r["run_id"] == new_run.run_id)[
            "stale"
        ]
    )
    hist = corrections.list_for(c, did, bank_id)["items"]
    extracted = pdf_service.item_detail(c, did, bank_id)["item"]["fields"]["reference"]
    assert hist[0]["original_value"] == extracted == canon_before["reference"]  # the original value is kept
    assert hist[0]["corrected_value"] == "SYNRCORRECTED001" and hist[0]["created_by"] == A
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute("UPDATE ocr_corrections SET corrected_value = 'x'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute("DELETE FROM ocr_corrections")
    from ledgerspy.normalize import service as normalize_service

    normalize_service.revert(c, did, fix["transformation_id"], A, "Correction entered on the wrong row.")
    # Reversible through the logged P4 revert: the override disappears and the canonical value applies again.
    assert (
        c.execute(
            "SELECT 1 FROM normalized_values WHERE dataset_id = ? AND txn_id = ? AND field = 'reference'",
            (did, bank_id),
        ).fetchone()
        is None
    )
    assert c.execute(
        "SELECT 1 FROM transformations WHERE reverts = ? AND kind = 'revert'", (fix["transformation_id"],)
    ).fetchone()
    assert len(corrections.list_for(c, did, bank_id)["items"]) == 1  # the correction record itself stays


# ------------------------------------------------------------------------------------------ integrity


def test_p9_detectors_and_source_rows_unchanged(env):
    c, cfg = env["h"].conn, env["cfg"]
    risk_service.clear_cache()
    res = risk_service.run_risk(c, cfg, env["led"])
    assert res.result_sha256 == env["p9"]
    assert hashlib.sha256(repr(sorted(res.signals)).encode()).hexdigest() == env["signals_sha"]
    for ds in (env["led"], env["csv"]):
        assert (
            c.execute("SELECT COUNT(*) FROM normalized_values WHERE dataset_id = ?", (ds,)).fetchone()[0] == 0
        )
    assert audit.verify(c).ok


def test_persistence_across_restart_and_deterministic_summary(env):
    h = env["h"]
    i = new(env, "persist")["investigation_id"]
    inv.add_note(h.conn, i, "kept across restarts", None, A)
    s1, m1 = inv.summary(h.conn, i), inv.manifest(h.conn, h.workspace, i)
    c2 = connect(h.workspace / "ledgerspy.db")
    try:
        assert (
            inv.summary(c2, i) == s1
            and inv.manifest(c2, h.workspace, i)["manifest_sha256"] == m1["manifest_sha256"]
        )
        assert inv.notes_page(c2, i, 1, 10)["items"][0]["body"] == "kept across restarts"
    finally:
        c2.close()


def test_tampered_evidence_is_reported_in_the_manifest(env):
    h, cfg, c = env["h"], env["cfg"], env["h"].conn
    other = h.upload(FIXTURES / "debit_credit.csv")
    h.confirm(other)
    sha = other["evidence"]["sha256"]
    i = new(env, "tamper")["investigation_id"]
    inv.link(c, cfg, i, {"link_type": "evidence_file", "ref_id": sha, "reason": "the source file"}, A)
    path = h.workspace / "evidence" / other["evidence"]["stored_name"]
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    path.write_bytes(path.read_bytes() + b"\nforged,row")
    m = inv.manifest(c, h.workspace, i)
    assert {f["sha256"]: f["verification"] for f in m["evidence_verification"]}[sha] == "evidence_tampered"


def test_pagination_on_100k_rows_is_correct_and_deterministic(env):
    c = env["h"].conn
    i = new(env, "pagination")["investigation_id"]
    c.execute("BEGIN")
    c.executemany(
        "INSERT INTO investigation_notes VALUES (?, ?, NULL, ?, 'bulk', ?)",
        [(f"NT-{k:026d}", i, f"note {k}", "2026-01-01T00:00:00Z") for k in range(100_000)],
    )
    c.execute("COMMIT")
    p1 = inv.notes_page(c, i, 1, 500)
    last = inv.notes_page(c, i, 200, 500)
    assert p1["total"] == last["total"] == 100_000 and len(last["items"]) == 500
    assert p1["items"][0]["note_id"] == f"NT-{0:026d}" and last["items"][-1]["note_id"] == f"NT-{99_999:026d}"
    assert inv.notes_page(c, i, 201, 500)["items"] == []
    assert inv.notes_page(c, i, 7, 500) == inv.notes_page(c, i, 7, 500)


def test_migration_7_to_8_keeps_existing_rows(tmp_path):
    path = tmp_path / "v7.db"
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("BEGIN")
    for version, desc, statements in MIGRATIONS[:7]:
        for s in statements:
            conn.execute(s)
        conn.execute("INSERT INTO schema_version (version, description) VALUES (?, ?)", (version, desc))
    conn.execute(
        "INSERT INTO cases (case_id, name, status, created_at, created_by) "
        "VALUES ('C-1', 'c', 'open', 't', 'a')"
    )
    conn.execute("COMMIT")
    conn.close()
    assert init_db(path, "tester") == SCHEMA_VERSION >= 8
    c = connect(path)
    assert current_version(c) == SCHEMA_VERSION and c.execute("SELECT name FROM cases").fetchone()[0] == "c"
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {
        "investigations",
        "investigation_links",
        "investigation_link_removals",
        "investigation_notes",
        "investigation_decisions",
        "investigation_events",
        "ocr_corrections",
    } <= tables
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    c.close()


# ------------------------------------------------------------------------------------------ API


@pytest.fixture(scope="module")
def api(env):
    token = secrets.token_urlsafe(32)
    with TestClient(create_app(env["cfg"], db_path_for(env["cfg"].settings), token), base_url=BASE_URL) as c:
        yield c, {"Authorization": f"Bearer {token}"}


def test_investigation_endpoints_require_the_token(api, env):
    c, auth = api
    iid = c.post(
        "/api/investigations", json={"case_id": env["h"].case_id, "title": "auth"}, headers=auth
    ).json()["investigation_id"]
    gets = [
        "/api/investigations",
        f"/api/investigations/{iid}",
        f"/api/investigations/{iid}/links",
        f"/api/investigations/{iid}/notes",
        f"/api/investigations/{iid}/decisions",
        f"/api/investigations/{iid}/activity",
        f"/api/investigations/{iid}/summary",
        f"/api/investigations/{iid}/manifest",
        "/api/investigations/vocabulary",
        "/api/audit/verify",
    ]
    for p in gets:
        assert c.get(p).status_code == 401, p
        assert c.get(p, headers={"Authorization": "Bearer wrong"}).status_code == 401, p
    posts = [
        ("/api/investigations", {}),
        (f"/api/investigations/{iid}/links", {}),
        (f"/api/investigations/{iid}/notes", {"body": "x"}),
        (f"/api/investigations/{iid}/decisions", {}),
        (f"/api/investigations/{iid}/transitions", {"to_status": "in_review"}),
        (f"/api/investigations/{iid}/ocr-corrections", {}),
    ]
    ds, px = env["led"], "PX-0000000000000000"
    posts.append((f"/api/datasets/{ds}/pdf/items/{px}/corrections", {}))
    assert c.get(f"/api/datasets/{ds}/pdf/corrections").status_code == 401
    for p, body in posts:
        assert c.post(p, json=body).status_code == 401, p
    assert c.patch(f"/api/investigations/{iid}", json={"title": "y"}).status_code == 401


def test_investigation_api_flow_and_validation(api, env):
    c, auth = api
    r = c.post(
        "/api/investigations",
        headers=auth,
        json={
            "case_id": env["h"].case_id,
            "title": "API flow",
            "description": "<b>x</b>",
            "priority": "high",
            "links": [
                {
                    "link_type": "transaction",
                    "dataset_id": env["led"],
                    "ref_id": "LVC-JV-000002",
                    "reason": "intercompany funding",
                }
            ],
        },
    )
    assert r.status_code == 201
    iid = r.json()["investigation_id"]
    assert r.json()["description"] == "<b>x</b>" and r.json()["counts"]["active_links"] == 1
    lk = c.get(f"/api/investigations/{iid}/links", headers=auth).json()["items"][0]
    assert (
        c.post(
            f"/api/investigations/{iid}/decisions",
            headers=auth,
            json={
                "link_id": lk["link_id"],
                "action": "reviewed_no_issue",
                "rationale": "Board-approved funding.",
            },
        ).status_code
        == 201
    )
    assert c.post(f"/api/investigations/{iid}/notes", headers=auth, json={"body": "note"}).status_code == 201
    bad = c.post(f"/api/investigations/{iid}/transitions", headers=auth, json={"to_status": "concluded"})
    assert bad.status_code == 409 and bad.json()["error"]["code"] == "invalid_transition"
    assert (
        c.post(
            f"/api/investigations/{iid}/transitions", headers=auth, json={"to_status": "in_review"}
        ).status_code
        == 200
    )
    assert (
        c.post(
            f"/api/investigations/{iid}/notes", headers=auth, json={"body": "x", "path": "/etc"}
        ).status_code
        == 422
    )
    assert c.get("/api/investigations/IV-../../x", headers=auth).status_code in (404, 422)
    assert c.get("/api/investigations/IV-01J00000000000000000000000", headers=auth).status_code == 404
    assert c.get(f"/api/investigations/{iid}/links?size=501", headers=auth).status_code == 422
    assert c.get(f"/api/investigations/{iid}/links/LK-bad", headers=auth).status_code == 422
    x = c.post(
        f"/api/investigations/{iid}/links",
        headers=auth,
        json={
            "link_type": "transaction",
            "dataset_id": env["other_ds"],
            "ref_id": "x",
            "reason": "cross case",
        },
    )
    assert x.status_code == 422 and x.json()["error"]["code"] == "cross_scope_reference"
    act = c.get(f"/api/investigations/{iid}/activity?size=2", headers=auth).json()
    act2 = c.get(f"/api/investigations/{iid}/activity?size=2&page=2", headers=auth).json()
    assert act["total"] == 5 and [e["seq"] for e in act["items"]] < [e["seq"] for e in act2["items"]]
    lst = c.get(f"/api/investigations?case_id={env['h'].case_id}&status=in_review", headers=auth).json()
    assert iid in {x["investigation_id"] for x in lst["items"]}
    # invalid OCR corrections are refused with explicit errors and nothing is recorded
    pdf, px = env["pdf"], "PX-0000000000000000"
    before = c.get(f"/api/datasets/{pdf}/pdf/corrections", headers=auth).json()
    body = {"field": "reference", "value": "X1", "rationale": "page shows X1"}
    for p, b, code, err in [
        (f"/api/datasets/{pdf}/pdf/items/{px}/corrections", body, 404, "item_not_found"),
        (f"/api/datasets/{env['led']}/pdf/items/{px}/corrections", body, None, "not_a_pdf_dataset"),
        (f"/api/datasets/{pdf}/pdf/items/{px}/corrections", body | {"field": "payee"}, 422, None),
        (f"/api/datasets/{pdf}/pdf/items/{px}/corrections", body | {"path": "../x"}, 422, None),
        (f"/api/datasets/{pdf}/pdf/items/PX-../../x/corrections", body, None, None),
    ]:
        r = c.post(p, headers=auth, json=b)
        assert r.status_code >= 400 and (code is None or r.status_code == code), (p, r.status_code)
        assert err is None or r.json()["error"]["code"] == err, (p, r.json())
    assert c.get(f"/api/datasets/{pdf}/pdf/corrections", headers=auth).json() == before
    v = c.get("/api/audit/verify", headers=auth).json()
    assert v["ok"] and "cannot" in v["limitation"]
    s1 = c.get(f"/api/investigations/{iid}/summary", headers=auth).json()
    assert s1 == c.get(f"/api/investigations/{iid}/summary", headers=auth).json()
