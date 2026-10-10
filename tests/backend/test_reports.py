"""P16 audit reports on real imports (P5 synthetic ledger + masters, the P5 CSV statement, P5-derived PDF
statements, reconciliation runs, P15 investigations): content, traceability, totals, stale runs, unconfirmed
OCR, memo, determinism, versioning, artefact safety, API security. Synthetic data only in these tests; the
report code never reads labels or ground truth (checked with a SQLite authorizer)."""

import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ledgerspy.api.app import create_app
from ledgerspy.cases import audit
from ledgerspy.cases.db import MIGRATIONS, SCHEMA_VERSION, connect, current_version, db_path_for, init_db
from ledgerspy.errors import LedgerSpyError
from ledgerspy.investigations import service as inv
from ledgerspy.reconciliation import engine as recon_engine
from ledgerspy.reconciliation import service as recon
from ledgerspy.reports import build, model, render
from ledgerspy.reports import store as reports
from ledgerspy.risk import service as risk_service
from tests.backend import test_investigations as p15
from tests.backend.conftest import BASE_URL
from tests.backend.test_forensics_datasets import _table_digest
from tests.backend.test_investigations import A, new, scope
from tests.backend.test_pdf_datasets import TESSERACT, needs_ocr, p5_bank_rows, p5_pdf

REPO = Path(__file__).resolve().parents[2]
env = p15.env  # the shared P15 fixture (P5 imports, statements, a reconciliation run)


def ws(env):
    return env["h"].workspace


def preview(env, report_type, **params):
    return reports.preview(env["h"].conn, env["cfg"], ws(env), report_type, params)["content"]


@pytest.fixture(scope="module")
def case(env):
    """One investigation with findings, records, decisions (supporting + contradicting), notes and a hostile
    note; a second one concluded `inconclusive`; a third with nothing linked."""
    c, cfg = env["h"].conn, env["cfg"]
    res = risk_service.run_risk(c, cfg, env["led"])
    top = res.rows[0]["txn_id"]
    signal = sorted(res.signals)[0]
    run = env["run"]
    exact = next(r for r in run.results if r["status"] == "exact_match")
    grouped = next((r for r in run.results if r["status"] == "grouped_match"), None)
    i = inv.create(
        c, cfg, env["h"].case_id, "Vendor <img src=x onerror=alert(1)> review", "Factual.", "high", A
    )["investigation_id"]
    lk = {}

    def link(name, spec):
        lk[name] = inv.link(c, cfg, i, spec | {"reason": f"evidence: {name}"}, A)["link_id"]

    link("risk", {"link_type": "risk_transaction", "dataset_id": env["led"], "ref_id": top})
    link(
        "txn", {"link_type": "transaction", "dataset_id": env["led"], "ref_id": top}
    )  # same txn: counted once
    link("signal", {"link_type": "signal", "dataset_id": env["led"], "ref_id": signal})
    link("result", {"link_type": "reconciliation_result", "run_id": run.run_id, "ref_id": exact["result_id"]})
    if grouped:
        link(
            "grouped",
            {"link_type": "reconciliation_result", "run_id": run.run_id, "ref_id": grouped["result_id"]},
        )
    link(
        "dup", {"link_type": "duplicate_group", "run_id": run.run_id, "ref_id": run.duplicates[0]["group_id"]}
    )
    led_sha = c.execute(
        "SELECT evidence_sha256 FROM datasets WHERE dataset_id = ?", (env["led"],)
    ).fetchone()[0]
    link("file", {"link_type": "evidence_file", "ref_id": led_sha})
    inv.decide(
        c,
        cfg,
        i,
        lk["risk"],
        "finding_confirmed",
        "Invoice 4471 is missing; payment exceeds PO.",
        A,
        supporting=[lk["txn"], lk["file"]],
        contradicting=[lk["result"]],
    )
    inv.decide(c, cfg, i, lk["signal"], "finding_dismissed", "Timing explained by the approved schedule.", A)
    inv.decide(c, cfg, i, None, "evidence_insufficient", "Bank confirmation still outstanding.", A)
    inv.add_note(c, i, "<script>alert('x')</script> =cmd|' /C calc'!A0 note", None, A)
    j = new(env, "Concluded review")["investigation_id"]
    inv.link(
        c,
        cfg,
        j,
        {"link_type": "transaction", "dataset_id": env["led"], "ref_id": top, "reason": "same row"},
        A,
    )
    inv.transition(c, j, "in_review", A)
    inv.transition(
        c,
        j,
        "concluded",
        A,
        outcome="inconclusive",
        conclusion="The records neither confirm nor rule out the concern.",
    )
    empty = new(env, "Nothing linked yet")["investigation_id"]
    return {"i": i, "j": j, "empty": empty, "links": lk, "top": top, "led_sha": led_sha}


# ------------------------------------------------------------------------------------------- case report


def test_case_report_separates_findings_assessments_notes_and_conclusion(env, case):
    r = preview(env, "case_investigation", investigation_ids=[case["i"]])
    assert r["report_type"] == "case_investigation" and r["engagement"]["case_id"] == env["h"].case_id
    assert {f["origin"] for f in r["automated_findings"]} == {"automated"}
    assert {a["origin"] for a in r["analyst_assessments"]} == {"analyst"}
    assert {n["origin"] for n in r["analyst_notes"]} == {"analyst"}
    risk = next(f for f in r["automated_findings"] if f["references"][0]["link_type"] == "risk_transaction")
    assert "not a probability" in risk["text"] and risk["analyst_status"] == "confirmed"
    assert risk["traceability"] == "traceable"
    ref = risk["references"][0]
    assert ref["verification"] == "current" and ref["evidence"] == [
        {"sha256": case["led_sha"], "verification": "verified"}
    ]
    row = (
        env["h"]
        .conn.execute(
            "SELECT source_row FROM transactions WHERE dataset_id = ? AND txn_id = ?",
            (env["led"], case["top"]),
        )
        .fetchone()[0]
    )
    assert ref["locations"][0]["source_row"] == row  # the source location of the claim
    sig = next(f for f in r["automated_findings"] if f["references"][0]["link_type"] == "signal")
    assert sig["analyst_status"] == "dismissed"  # dismissed findings are kept, with their status
    conf = next(a for a in r["analyst_assessments"] if a["action"] == "finding_confirmed")
    assert [x["link_id"] for x in conf["contradicting"]] == [case["links"]["result"]]  # contradiction shown
    assert {x["link_id"] for x in conf["references"]} == {case["links"][k] for k in ("risk", "txn", "file")}
    case_level = next(a for a in r["analyst_assessments"] if a["subject"] == "case")
    assert case_level["traceability"] == "no_reference" and case_level["unresolved"] is True
    assert [u["subject"] for u in r["unresolved"]] == ["case"]
    assert r["conclusion"] is None and r["conclusion_status"] == "not_recorded"
    assert r["activity"]["total"] == len(r["activity"]["items"]) and r["activity"]["items"][0]["audit_hash"]


def test_linked_values_count_each_transaction_once_and_trace_to_rows(env, case):
    r = preview(env, "case_investigation", investigation_ids=[case["i"]])
    v = r["linked_values"]
    assert v["distinct_transactions"] == 1 and len(v["transactions"][0]["link_ids"]) == 2  # risk + txn link
    amount, cur = (
        env["h"]
        .conn.execute(
            "SELECT amount_minor, currency FROM transactions WHERE dataset_id = ? AND txn_id = ?",
            (env["led"], case["top"]),
        )
        .fetchone()
    )
    assert v["totals"] == [
        {
            "dataset_id": env["led"],
            "currency": cur,
            "transactions": 1,
            "gross_value": v["transactions"][0]["amount"],
        }
    ]
    assert v["transactions"][0]["amount"]["minor"] == amount and "not a loss" in v["note"]


def test_conclusion_only_when_recorded_and_missing_data_is_stated(env, case):
    j = preview(env, "case_investigation", investigation_ids=[case["j"]])
    assert j["conclusion"]["outcome"] == "inconclusive" and j["conclusion"]["origin"] == "analyst"
    e = preview(env, "case_investigation", investigation_ids=[case["empty"]])
    assert e["automated_findings"] == [] and e["linked_records"] == [] and e["conclusion"] is None
    assert e["linked_values"]["totals"] == [] and e["linked_values"]["distinct_transactions"] == 0
    html = render.to_html(e, {})
    assert "No conclusion recorded" in html and "No transaction is linked directly" in html


# ------------------------------------------------------------------------------------------- reconciliation


def test_reconciliation_report_totals_reconcile_with_rows_and_keep_accounts_apart(env):
    run = env["run"]
    r = preview(env, "bank_reconciliation", run_id=run.run_id)
    assert r["run_state"] == "current" and r["run"]["result_sha256"] == run.result_sha256
    bd = r["totals"]["breakdown"]
    for side in ("ledger", "bank"):
        assert sum(b["rows"] for b in bd if b["side"] == side) == run.summary[side + "_rows"]
        by = {}
        for b in bd:
            if b["side"] == side and b["value"]:
                by.setdefault(b["currency"], {}).setdefault(b["status"], 0)
                by[b["currency"]][b["status"]] += b["value"]["minor"]
        assert {c: {s: v for s, v in x.items() if v} for c, x in by.items()} == {
            c: {s: v for s, v in x.items() if v}
            for c, x in run.summary["amounts_by_status_minor"][side].items()
        }
    # every row counted once: the rows listed by the breakdown are exactly the run's distinct row IDs
    ids = {(s, t) for res in run.results for s in ("ledger", "bank") for t in res[s + "_txn_ids"]}
    assert len(ids) == sum(b["rows"] for b in bd)
    assert len({b["account"] for b in bd if b["side"] == "bank"}) == 2  # two statement accounts, kept apart
    ex = r["exceptions"]
    assert ex["total"] == sum(1 for x in run.results if x["status"] not in recon_engine.MATCHED)
    assert ex["listed"] == min(ex["total"], model.MAX_LISTED_ROWS)
    assert all(x["status"] not in recon_engine.MATCHED for x in ex["items"])
    assert "not losses" in r["totals"]["note"] and r["duplicate_candidates"]["total"] == len(run.duplicates)


def _fake_run(results, summary):
    class R:
        pass

    run = R()
    run.run_id, run.results, run.summary, run.duplicates, run.findings = (
        "RR-0000000000000000",
        results,
        summary,
        [],
        [],
    )
    run.scopes, run.truncation, run.readiness, run.limitations = [], {}, {}, []
    return run


def _res(rid, status, account, ledger, bank):
    def row(side, t, amt, cur):
        return {
            "side": side,
            "txn_id": t,
            "source_row": 1,
            "date": "2024-01-01",
            "amount_minor": amt,
            "currency": cur,
            "account": account if side == "bank" else None,
            "reference": None,
            "counterparty": None,
        }

    return {
        "result_id": rid,
        "status": status,
        "scope": {"account": account, "currency": (ledger or bank)[0][2]},
        "ledger_rows": [row("ledger", *x) for x in ledger],
        "bank_rows": [row("bank", *x) for x in bank],
        "ledger_txn_ids": [x[0] for x in ledger],
        "bank_txn_ids": [x[0] for x in bank],
        "first_date": "2024-01-01",
        "last_date": "2024-01-01",
        "amount_difference_minor": None,
        "date_difference_days": None,
        "discrepancies": [],
        "candidates": [],
        "reasons": [],
        "explanation": "",
        "limitations": [],
    }


def test_currencies_and_grouped_matches_are_never_merged_or_double_counted(env):
    """Isolated unit test with an in-memory run: two currencies, a row whose currency is unknown, two
    accounts, and a grouped match of two ledger rows against one bank row."""
    results = [
        _res("RM-1", "grouped_match", "A-1", [("L1", 600, "INR"), ("L2", 400, "INR")], [("B1", 1000, "INR")]),
        _res("RM-2", "unmatched_bank", "A-2", [], [("B2", 50, "USD")]),
        _res("RM-3", "unmatched_ledger", "A-1", [("L3", 70, "USD")], []),
        _res("RM-4", "unmatched_bank", "A-2", [], [("B3", 30, None)]),
    ]
    summary = recon_engine.summarize(results, [object()] * 3, [object()] * 3)
    summary["balance_check"] = {"status": "unavailable", "accounts": []}
    ctx = build._Ctx(env["h"].conn, env["cfg"], ws(env))
    out = build._run_content(ctx, env["h"].conn, _fake_run(results, summary))
    bd = {
        (b["side"], b["account"], b["currency"], b["status"]): (b["rows"], b["value"]["minor"])
        for b in out["totals"]["breakdown"]
    }
    assert bd[("ledger", "A-1", "INR", "grouped_match")] == (2, 1000) and bd[
        ("bank", "A-1", "INR", "grouped_match")
    ] == (1, 1000)
    assert bd[("bank", "A-2", "USD", "unmatched_bank")] == (1, 50) and bd[
        ("ledger", "A-1", "USD", "unmatched_ledger")
    ] == (1, 70)
    # an unknown currency is its own bucket, labelled as such, never folded into a known currency
    assert bd[("bank", "A-2", "unknown", "unmatched_bank")] == (1, 30)
    unknown = next(b for b in out["totals"]["breakdown"] if b["currency"] == "unknown")
    assert unknown["value"]["currency"] is None and unknown["value"]["text"].endswith("(currency unknown)")
    assert sorted(m["currency"] or "unknown" for m in out["totals"]["unmatched"]["bank"]) == [
        "USD",
        "unknown",
    ]
    assert out["totals"]["matched"]["ledger"] == [ctx.money(1000, "INR")]  # grouped rows once, not twice
    bad = dict(summary, amounts_by_status_minor={"ledger": {"INR": {"grouped_match": 999}}, "bank": {}})
    with pytest.raises(LedgerSpyError) as e:
        build._run_content(ctx, env["h"].conn, _fake_run(results, bad))
    assert e.value.code == "totals_mismatch"


def test_stale_run_is_never_presented_as_current(env, case):
    h, cfg, c = env["h"], env["cfg"], env["h"].conn
    ds = h.upload(p5_pdf(p5_bank_rows()[:40]), "stale.pdf", kind="bank_statement")
    h.confirm(ds)
    su = recon.setup(c, cfg, env["led"], ds["dataset_id"])
    req = recon.RunRequest(
        ledger_dataset_id=env["led"],
        bank_dataset_id=ds["dataset_id"],
        scopes=[scope(su, "LVE")],
        ledger_debit_means="outflow",
    )
    run = recon.start(c, cfg, req, "t")
    exact = next(r for r in run.results if r["status"] == "exact_match")
    i = new(env, "stale run")["investigation_id"]
    inv.link(
        c,
        cfg,
        i,
        {
            "link_type": "reconciliation_result",
            "run_id": run.run_id,
            "ref_id": exact["result_id"],
            "reason": "match",
        },
        A,
    )
    assert preview(env, "bank_reconciliation", run_id=run.run_id)["run_state"] == "current"
    inv.correct_ocr(
        c,
        cfg,
        i,
        ds["dataset_id"],
        exact["bank_txn_ids"][0],
        "reference",
        "SYNRSTALE0000001",
        "Page shows SYNRSTALE0000001.",
        A,
    )
    r = preview(env, "bank_reconciliation", run_id=run.run_id)
    assert r["run_state"] == "stale" and r["results"] is None and "totals" not in r and "exceptions" not in r
    assert "NOT presented as a current reconciliation" in r["run_state_reason"]
    cr = preview(env, "case_investigation", investigation_ids=[i])
    rec = cr["linked_records"][0]
    assert rec["references"][0]["verification"] == "stale" and rec["traceability"] == "unverified"
    assert cr["scope"]["reconciliation_runs"] == [{"run_id": run.run_id, "stale": True}]
    memo = preview(env, "audit_memo", investigation_ids=[i])
    assert any(
        "is stale" in x for x in next(s for s in memo["sections"] if s["id"] == "recommendations")["items"]
    )
    html = render.to_html(r, {})
    assert "STALE" in html and "not a current reconciliation" in html


@needs_ocr
def test_unconfirmed_ocr_values_are_not_presented_as_facts(env):
    h, cfg, c = env["h"], env["cfg"], env["h"].conn
    ds = h.upload(p5_pdf(p5_bank_rows()[:12], scanned_pages=(0,)), "scan.pdf", kind="bank_statement")
    from ledgerspy.pdfstatements import service as pdf_service

    item = next(
        x
        for x in pdf_service.items_page(c, ds["dataset_id"], "transaction", None, None, 1, 50)["items"]
        if x["review_required"]
    )
    i = new(env, "ocr")["investigation_id"]
    inv.link(
        c,
        cfg,
        i,
        {
            "link_type": "pdf_item",
            "dataset_id": ds["dataset_id"],
            "ref_id": item["item_id"],
            "reason": "scanned line",
        },
        A,
    )
    r = preview(env, "case_investigation", investigation_ids=[i])
    ev = r["linked_records"][0]
    assert ev["claim_type"] == "extraction_value" and any(
        "not confirmed financial facts" in x for x in ev["limitations"]
    )
    assert r["linked_values"]["totals"] == []  # an extraction value never enters a total
    assert r["data_quality"]["unconfirmed_extraction"] == [ev["references"][0]["link_id"]]
    assert TESSERACT.available


# ------------------------------------------------------------------------------------------- memo


def test_memo_is_a_draft_with_every_number_traced_and_all_sections(env, case):
    m = preview(env, "audit_memo", investigation_ids=[case["i"], case["j"], case["empty"]])
    assert m["status"] == "draft" and m["approval"] == {
        "status": "not_reviewed",
        "supported": False,
        "note": model.DRAFT_NOTICE,
    }
    ids = [s["id"] for s in m["sections"]]
    assert ids == [
        "scope",
        "executive_summary",
        "methodology",
        "material_findings",
        "amounts",
        "relationships",
        "reconciliation",
        "evidence_balance",
        "limitations",
        "decisions_and_conclusion",
        "recommendations",
    ]
    assert m["numbers_check"]["unsupported"] == [] and m["numbers_check"]["numbers_stated"] > 0
    f = m["facts"]
    cases = m["case_reports"]
    assert f["investigations"] == 3 and f["automated_findings"] == sum(
        len(c["automated_findings"]) for c in cases
    )
    assert f["conclusions_recorded"] == 1 and f["findings_dismissed"] == 1
    material = next(s for s in m["sections"] if s["id"] == "material_findings")["findings"]
    assert all("Analyst status: dismissed" not in x["text"] for x in material)  # dismissed: listed elsewhere
    concl = next(s for s in m["sections"] if s["id"] == "decisions_and_conclusion")
    assert any("inconclusive" in p for p in concl["paragraphs"]) and any(
        "Dismissed" in x for x in concl["items"]
    )
    rec = next(s for s in m["sections"] if s["id"] == "recommendations")
    assert rec["paragraphs"] == [model.RECOMMENDATION_NOTICE] and rec["items"]
    balance = next(s for s in m["sections"] if s["id"] == "evidence_balance")["items"]
    assert any("Contradicting: reconciliation_result" in x for x in balance)
    # the cross-check really fails on an invented number
    m["sections"][1]["paragraphs"].append("Losses of 987654321 were found.")
    with pytest.raises(LedgerSpyError) as e:
        build.check_numbers(m)
    assert e.value.code == "memo_number_unsupported" and e.value.details["numbers"] == ["987654321"]


def test_memo_templates_contain_no_hardcoded_figures():
    src = (REPO / "ledgerspy" / "reports" / "build.py").read_text(encoding="utf-8")
    body = src[src.index("def audit_memo(") : src.index("def memo_texts(")]
    literals = re.findall(r'f?"((?:[^"\\]|\\.)*)"', body)
    figures = [x for x in literals if build._numbers(re.sub(r"\{[^}]*\}", "", x))]
    assert figures == [], figures  # every figure comes from a record or a counted fact


def test_memo_refuses_investigations_from_different_engagement_cases(env, case):
    c = env["h"].conn
    other = inv.create(c, env["cfg"], env["other_case"], "elsewhere", "", "low", A)["investigation_id"]
    with pytest.raises(LedgerSpyError) as e:
        preview(env, "audit_memo", investigation_ids=[case["i"], other])
    assert e.value.code == "cross_scope_reference"
    with pytest.raises(LedgerSpyError) as e:
        preview(env, "audit_memo", investigation_ids=["IV-01J00000000000000000000000"])
    assert e.value.code == "investigation_not_found"


# ------------------------------------------------------------------------------------------- manifest


def test_evidence_manifest_lists_hashes_locations_and_integrity(env, case):
    m = preview(env, "evidence_manifest", investigation_ids=[case["i"]])
    files = {f["sha256"]: f for f in m["evidence_files"]}
    assert (
        files[case["led_sha"]]["verification"] == "verified"
        and "SYNTHETIC_ledger.csv" in files[case["led_sha"]]["original_filenames"]
    )
    assert {e["link_id"] for e in m["references"]} >= set(case["links"].values())
    risk = next(e for e in m["references"] if e["link_type"] == "risk_transaction")
    assert risk["locations"][0]["source_row"] >= 1 and risk["evidence_sha256"] == [case["led_sha"]]
    assert m["counts"]["references"] == len(m["references"]) and m["counts"]["evidence_not_verified"] == 0


# ------------------------------------------------------------------------------- determinism, storage


def test_deterministic_content_and_versioned_regeneration(env, case):
    a = preview(env, "case_investigation", investigation_ids=[case["i"]])
    b = preview(env, "case_investigation", investigation_ids=[case["i"]])
    assert a == b and a["content_sha256"] == b["content_sha256"]
    c = env["h"].conn
    r1 = reports.generate(c, env["cfg"], ws(env), "case_investigation", {"investigation_ids": [case["i"]]}, A)
    r2 = reports.generate(c, env["cfg"], ws(env), "case_investigation", {"investigation_ids": [case["i"]]}, A)
    assert (r1["version"], r2["version"]) == (1, 2) and r1["report_id"] != r2["report_id"]
    assert (
        r2["previous_report_id"] == r1["report_id"]
        and r1["content_sha256"] == r2["content_sha256"] == a["content_sha256"]
    )
    assert [v["report_id"] for v in r2["versions"]] == [r1["report_id"], r2["report_id"]]
    for r in (r1, r2):  # both versions kept, read-only, hash-checked
        for meta in r["artifacts"].values():
            p = reports.reports_dir(ws(env)) / meta["name"]
            assert p.is_file() and not os.access(p, os.W_OK)
            assert hashlib.sha256(p.read_bytes()).hexdigest() == meta["sha256"]
        assert reports.verify(c, ws(env), r["report_id"])["ok"]
    env2 = reports.content(c, ws(env), r2["report_id"])["envelope"]
    assert env2["same_content_as_previous"] is True and env2["status"] == "generated"
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute("UPDATE reports SET version = 9")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute("DELETE FROM reports")
    assert any(
        e
        for e in c.execute(
            "SELECT 1 FROM audit_log WHERE action = 'report.generated' AND object = ?",
            (f"report:{r1['report_id']}",),
        )
    )
    assert audit.verify(c).ok


def test_reports_never_write_sources_or_read_ground_truth(env, case):
    c, cfg = env["h"].conn, env["cfg"]
    risk_service.clear_cache()
    p9_before = risk_service.run_risk(c, cfg, env["led"]).result_sha256
    digest = _table_digest(c)
    counts = {
        t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608 — fixed names
        for t in (
            "investigation_links",
            "investigation_decisions",
            "investigation_notes",
            "transformations",
            "reconciliation_runs",
            "evidence_files",
            "datasets",
        )
    }
    seen = []

    def guard(action, arg1, arg2, dbname, source):
        if action == sqlite3.SQLITE_READ and arg1 == "labels":
            seen.append(("read", arg1))
            return sqlite3.SQLITE_DENY
        if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE) and arg1 not in (
            "reports",
            "audit_log",
        ):
            seen.append(("write", arg1))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    c.set_authorizer(guard)
    try:
        with pytest.raises(
            sqlite3.DatabaseError, match="not authorized"
        ):  # positive control: the guard bites
            c.execute("UPDATE investigations SET title = title WHERE 0")
        with pytest.raises(sqlite3.DatabaseError, match="prohibited|not authorized"):
            c.execute("SELECT COUNT(*) FROM labels").fetchone()
        assert seen == [("write", "investigations"), ("read", "labels")]
        seen.clear()
        for t, p in (
            ("case_investigation", {"investigation_ids": [case["i"]]}),
            ("audit_memo", {"investigation_ids": [case["i"], case["j"]]}),
            ("evidence_manifest", {"investigation_ids": [case["i"]]}),
            ("bank_reconciliation", {"run_id": env["run"].run_id}),
        ):
            reports.preview(c, cfg, ws(env), t, p)
            reports.generate(c, cfg, ws(env), t, p, A)
    finally:
        c.set_authorizer(None)
    assert seen == []  # no read of ground-truth labels, no write outside the report register and audit log
    assert _table_digest(c) == digest
    assert counts == {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in counts}  # noqa: S608
    risk_service.clear_cache()
    assert risk_service.run_risk(c, cfg, env["led"]).result_sha256 == p9_before == env["p9"]
    src = "\n".join(p.read_text(encoding="utf-8") for p in (REPO / "ledgerspy" / "reports").glob("*.py"))
    assert not re.search(r"\blabels\b|ground_truth|scenario|SYNTHETIC_", src)


def test_html_and_markdown_escape_hostile_text(env, case):
    c = env["h"].conn
    r = reports.generate(c, env["cfg"], ws(env), "case_investigation", {"investigation_ids": [case["i"]]}, A)
    _, html = reports.read_artifact(c, ws(env), r["report_id"], "html")
    page = html.decode()
    assert "<script>" not in page and "<img src=x" not in page
    assert (
        "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;" in page
        and "&lt;img src=x onerror=alert(1)&gt;" in page
    )
    assert "Content-Security-Policy" in page and "default-src 'none'" in page and "<script" not in page
    _, mdtext = reports.read_artifact(c, ws(env), r["report_id"], "md")
    assert "<img" not in mdtext.decode() and "&lt;img src=x onerror=alert(1)&gt;" in mdtext.decode()
    assert (
        render.md("[x](javascript:alert(1)) *b* `c` <a>")
        == "\\[x\\](javascript:alert(1)) \\*b\\* \\`c\\` &lt;a&gt;"
    )
    _, js = reports.read_artifact(c, ws(env), r["report_id"], "json")
    doc = json.loads(js)
    assert doc["content"]["analyst_notes"][0]["text"].startswith("<script>")  # the record itself is verbatim


def test_tampered_or_missing_artifacts_are_refused(env, case):
    c = env["h"].conn
    r = reports.generate(c, env["cfg"], ws(env), "evidence_manifest", {"investigation_ids": [case["i"]]}, A)
    p = reports.reports_dir(ws(env)) / r["artifacts"]["html"]["name"]
    os.chmod(p, stat.S_IREAD | stat.S_IWRITE)
    p.write_bytes(p.read_bytes() + b"<!-- edited -->")
    with pytest.raises(LedgerSpyError) as e:
        reports.read_artifact(c, ws(env), r["report_id"], "html")
    assert e.value.code == "report_tampered"
    q = reports.reports_dir(ws(env)) / r["artifacts"]["md"]["name"]
    os.chmod(q, stat.S_IREAD | stat.S_IWRITE)
    q.unlink()
    v = reports.verify(c, ws(env), r["report_id"])
    assert v == {
        "report_id": r["report_id"],
        "artifacts": {"html": "report_tampered", "json": "verified", "md": "report_missing"},
        "ok": False,
    }


def test_oversized_output_is_refused_and_leaves_nothing(env, case, monkeypatch):
    c = env["h"].conn
    before = c.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
    files = set(os.listdir(reports.reports_dir(ws(env)))) if reports.reports_dir(ws(env)).exists() else set()
    monkeypatch.setattr(model, "MAX_ARTIFACT_BYTES", 1000)
    with pytest.raises(LedgerSpyError) as e:
        reports.generate(c, env["cfg"], ws(env), "case_investigation", {"investigation_ids": [case["i"]]}, A)
    assert e.value.code == "report_too_large" and e.value.http_status == 413
    assert c.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == before
    assert set(os.listdir(reports.reports_dir(ws(env)))) == files


def test_parameters_are_validated(env, case):
    c = env["h"].conn
    for t, p, code in (
        ("bogus", {}, "invalid_report_type"),
        ("case_investigation", {"investigation_ids": [case["i"], case["j"]]}, "invalid_parameters"),
        ("case_investigation", {"run_id": env["run"].run_id}, "invalid_parameters"),
        ("bank_reconciliation", {"investigation_ids": [case["i"]]}, "invalid_parameters"),
        ("bank_reconciliation", {"run_id": "RR-0000000000000000"}, "run_not_found"),
        ("audit_memo", {}, "investigation_required"),
        ("audit_memo", {"investigation_ids": [case["i"], case["i"]]}, "duplicate_investigation"),
        (
            "evidence_manifest",
            {"investigation_ids": ["IV-01J00000000000000000000000"] * 1},
            "investigation_not_found",
        ),
    ):
        with pytest.raises(LedgerSpyError) as e:
            reports.preview(c, env["cfg"], ws(env), t, p)
        assert e.value.code == code, (t, p, e.value.code)
    with pytest.raises(LedgerSpyError) as e:
        reports.get(c, "../../etc/passwd")
    assert e.value.code == "invalid_report_id"


def test_migration_8_to_9_keeps_existing_rows(tmp_path):
    path = tmp_path / "v8.db"
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("BEGIN")
    for version, desc, statements in MIGRATIONS[:8]:
        for s in statements:
            conn.execute(s)
        conn.execute("INSERT INTO schema_version (version, description) VALUES (?, ?)", (version, desc))
    conn.execute(
        "INSERT INTO cases (case_id, name, status, created_at, created_by) "
        "VALUES ('C-1', 'c', 'open', 't', 'a')"
    )
    conn.execute("COMMIT")
    conn.close()
    assert init_db(path, "tester") == SCHEMA_VERSION >= 9
    c = connect(path)
    assert current_version(c) == SCHEMA_VERSION and c.execute("SELECT name FROM cases").fetchone()[0] == "c"
    assert c.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 0
    kinds = {r[0] for r in c.execute("SELECT type FROM sqlite_master WHERE tbl_name = 'reports'")}
    assert kinds == {"table", "trigger", "index"} and c.execute("PRAGMA foreign_key_check").fetchall() == []
    c.close()


# ------------------------------------------------------------------------------------------- API


@pytest.fixture(scope="module")
def api(env):
    token = secrets.token_urlsafe(32)
    with TestClient(create_app(env["cfg"], db_path_for(env["cfg"].settings), token), base_url=BASE_URL) as c:
        yield c, {"Authorization": f"Bearer {token}"}


def test_report_endpoints_require_the_token(api, case):
    c, auth = api
    body = {"report_type": "case_investigation", "investigation_ids": [case["i"]]}
    rid = c.post("/api/reports", json=body, headers=auth).json()["report_id"]
    for p in (
        "/api/reports",
        "/api/reports/vocabulary",
        f"/api/reports/{rid}",
        f"/api/reports/{rid}/verify",
        f"/api/reports/{rid}/download?format=html",
    ):
        assert c.get(p).status_code == 401, p
        assert c.get(p, headers={"Authorization": "Bearer wrong"}).status_code == 401, p
    for p in ("/api/reports", "/api/reports/preview"):
        assert c.post(p, json=body).status_code == 401, p


def test_report_api_flow_validation_and_downloads(api, env, case):
    c, auth = api
    v = c.get("/api/reports/vocabulary", headers=auth).json()
    assert (
        v["approval_supported"] is False
        and "pdf" in v["deferred_formats"]
        and v["formats"] == ["html", "json", "md"]
    )
    body = {"report_type": "audit_memo", "investigation_ids": [case["j"], case["i"]]}
    pv = c.post("/api/reports/preview", json=body, headers=auth)
    assert (
        pv.status_code == 200 and pv.json()["stored"] is False and pv.json()["content"]["status"] == "draft"
    )
    g = c.post("/api/reports", json=body, headers=auth)
    assert g.status_code == 201
    rep = g.json()
    assert rep["status"] == "draft" and rep["params"] == {"investigation_ids": sorted([case["i"], case["j"]])}
    got = c.get(f"/api/reports/{rep['report_id']}", headers=auth).json()
    assert got["content"]["content_sha256"] == rep["content_sha256"] == pv.json()["content"]["content_sha256"]
    for fmt, ctype in (("html", "text/html"), ("json", "application/json"), ("md", "text/markdown")):
        d = c.get(f"/api/reports/{rep['report_id']}/download?format={fmt}", headers=auth)
        assert d.status_code == 200 and d.headers["content-type"].startswith(ctype)
        assert d.headers["content-disposition"] == (
            f'attachment; filename="ledgerspy-audit_memo-v{rep["version"]}-{rep["report_id"]}.{fmt}"'
        )
        assert (
            hashlib.sha256(d.content).hexdigest()
            == d.headers["x-content-sha256"]
            == rep["artifacts"][fmt]["sha256"]
        )
        assert "default-src 'self'" in d.headers["content-security-policy"]
    lst = c.get(f"/api/reports?case_id={env['h'].case_id}&report_type=audit_memo&size=1", headers=auth).json()
    assert lst["size"] == 1 and len(lst["items"]) == 1 and lst["total"] >= 1
    bad = [
        (
            "post",
            "/api/reports",
            {"report_type": "case_investigation", "investigation_ids": [case["i"]], "path": "/x"},
            422,
        ),
        ("post", "/api/reports", {"report_type": "nope"}, 422),
        ("post", "/api/reports", {"report_type": "audit_memo", "investigation_ids": ["../../etc"]}, 422),
        ("post", "/api/reports", {"report_type": "audit_memo", "investigation_ids": [case["i"]] * 21}, 422),
        ("post", "/api/reports", {"report_type": "bank_reconciliation", "run_id": "RR-../x"}, 422),
        ("get", f"/api/reports/{rep['report_id']}/download?format=../../x", None, 422),
        ("get", f"/api/reports/{rep['report_id']}/download?format=pdf", None, 422),
        ("get", "/api/reports/RP-../../etc/passwd", None, 404),
        ("get", "/api/reports/RP-bad", None, 422),
        ("get", "/api/reports/RP-01J00000000000000000000000", None, 404),
        ("get", "/api/reports?size=501", None, 422),
    ]
    for method, path, payload, code in bad:
        r = getattr(c, method)(path, headers=auth, **({"json": payload} if payload is not None else {}))
        assert r.status_code == code, (path, r.status_code, r.text[:200])
    x = c.post(
        "/api/reports/preview",
        headers=auth,
        json={"report_type": "case_investigation", "investigation_ids": [case["i"], case["j"]]},
    )
    assert x.status_code == 422 and x.json()["error"]["code"] == "invalid_parameters"


# ------------------------------------------------------------------------------------------- golden template

GOLDEN = REPO / "tests" / "backend" / "fixtures" / "reports"


def _golden_memo() -> dict:
    """A small hand-written memo content (isolated test data, from no dataset) for the template check."""
    sha = "a" * 64
    finding = {
        "text": "[IV-T] P9 review-risk row T-1: score 72.5 (0–100 review priority, not a probability). "
        "Analyst status: confirmed.",
        "claim_id": "CL-0000000000000001",
        "origin": "automated",
        "evidence": [sha],
        "locations": [{"filename": "ledger.csv", "source_row": 12}],
    }
    return {
        "report_type": "audit_memo",
        "title": "Audit memo (draft) — Example <Ltd>",
        "status": "draft",
        "approval": {"status": "not_reviewed", "supported": False, "note": model.DRAFT_NOTICE},
        "engagement": {"case_id": "C-T", "name": "Example <Ltd>"},
        "investigation_ids": ["IV-T"],
        "facts": {"investigations": 1},
        "sections": [
            {
                "id": "scope",
                "title": "Engagement and investigation scope",
                "paragraphs": ["Investigations covered: 1."],
            },
            {
                "id": "material_findings",
                "title": "Material findings and evidence",
                "paragraphs": [],
                "findings": [finding],
            },
            {
                "id": "recommendations",
                "title": "Recommended next steps (recommendations, not findings)",
                "paragraphs": [model.RECOMMENDATION_NOTICE],
                "items": ["IV-T: record a conclusion | *now*."],
            },
        ],
        "case_reports": [],
        "numbers_check": {"numbers_stated": 3, "unsupported": [], "rule": "rule text"},
        "content_sha256": "b" * 64,
        "schema_version": 9,
        "format_version": 1,
        "config_sha256": "c" * 64,
        "provenance": {"case_id": "C-T", "investigation_ids": ["IV-T"], "evidence_sha256": [sha]},
    }


def test_memo_templates_match_the_golden_files():
    env_ = {
        "report_id": "RP-T",
        "version": 1,
        "generated_at": "2026-01-01T00:00:00Z",
        "generated_by": "tester",
    }
    content = _golden_memo()
    for name, text in (
        ("memo.md", render.to_markdown(content, env_)),
        ("memo.html", render.to_html(content, env_)),
    ):
        assert text == (GOLDEN / name).read_text(encoding="utf-8"), name
