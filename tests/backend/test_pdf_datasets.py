"""P14b on real imports: PDF statements through upload → extraction → review → mapping → P14 reconciliation.

The PDFs are generated for these tests from the P5 synthetic bank statement (test-only; the expected values
are read here to SCORE extraction and are never used by the runtime). PaySim has no bank statement: not
applicable.
"""

import csv
import hashlib
import json
import secrets
import sqlite3
import time
from collections import Counter
from datetime import date

import pytest
from fastapi.testclient import TestClient

from ledgerspy.api.app import create_app
from ledgerspy.cases import store
from ledgerspy.cases.db import db_path_for
from ledgerspy.config import load_config
from ledgerspy.errors import LedgerSpyError
from ledgerspy.ingest.errors import IngestError
from ledgerspy.pdfstatements import ocr
from ledgerspy.pdfstatements import service as pdf_service
from ledgerspy.reconciliation import service as recon
from ledgerspy.reconciliation.records import NameIndex, Scope, account_key, alnum, bank_rows
from ledgerspy.resolution import masters, validation
from ledgerspy.risk import service as risk_service
from tests.backend import pdf_builder as B
from tests.backend.conftest import BASE_URL, PORT, make_ingest
from tests.backend.test_features_datasets import REFERENCE, case_overrides
from tests.backend.test_forensics_datasets import imported

CFG0 = load_config()
TESSERACT = ocr.discover(CFG0.settings.pdf.ocr.model_dump(mode="json"))
needs_ocr = pytest.mark.skipif(not TESSERACT.available, reason=f"local OCR unavailable: {TESSERACT.reason}")
LVE = "SYN608293236964"


def _fmt(v: str) -> str:
    return f"{float(v):,.2f}" if v else ""


def p5_bank_rows(account: str = LVE) -> list[dict]:
    with (REFERENCE / "SYNTHETIC_bank_statement.csv").open(encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r["Account No"] == account]


def p5_pdf(rows: list[dict], scanned_pages=(), **kw) -> bytes:
    first = float(rows[0]["Balance"]) - float(rows[0]["Deposit"] or 0) + float(rows[0]["Withdrawal"] or 0)
    spec = [
        {
            "date": r["Txn Date"],
            "description": r["Narration"],
            "reference": r["Ref No"],
            "debit": _fmt(r["Withdrawal"]),
            "credit": _fmt(r["Deposit"]),
            "balance": _fmt(r["Balance"]),
        }
        for r in rows
    ]
    info = [
        "SYNTHETIC BANK - STATEMENT OF ACCOUNT",
        f"Account No: {rows[0]['Account No']}",
        "Currency: INR",
        f"Opening Balance: {first:,.2f}",
    ]
    return B.statement_pdf(
        spec,
        layout="p5",
        info=info,
        closing=f"Closing Balance: {spec[-1]['balance']}",
        size=6.0,
        rows_per_page=40,
        scanned_pages=scanned_pages,
        dpi=300,
        **kw,
    )


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    h = make_ingest(tmp_path_factory.mktemp("ws"))
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
    risk_service.clear_cache()
    p9 = risk_service.run_risk(h.conn, cfg, led).result_sha256
    rows = p5_bank_rows()
    pdf = p5_pdf(rows)
    t = time.perf_counter()
    ds = h.upload(pdf, "lve_statement.pdf", kind="bank_statement")
    upload_s = time.perf_counter() - t
    csv_ds = h.upload(REFERENCE / "SYNTHETIC_bank_statement.csv", kind="bank_statement")
    h.confirm(csv_ds)
    yield {
        "h": h,
        "cfg": cfg,
        "led": led,
        "p9": p9,
        "rows": rows,
        "pdf": pdf,
        "ds": ds,
        "csv": csv_ds,
        "upload_s": upload_s,
    }
    h.conn.close()


def _items(h, ds_id, **kw):
    return pdf_service.items_page(
        h.conn, ds_id, kw.get("kind"), kw.get("review"), kw.get("pdf_page"), 1, 5000
    )


# ------------------------------------------------------------------------------------------ evidence


def test_pdf_evidence_is_preserved_and_the_extraction_registered(env):
    h, ds, pdf = env["h"], env["ds"], env["pdf"]
    sha = hashlib.sha256(pdf).hexdigest()
    assert (
        ds["evidence"]["sha256"] == sha
        and ds["evidence"]["format"] == "pdf"
        and ds["kind"] == "bank_statement"
    )
    stored = h.workspace / "evidence" / ds["evidence"]["stored_name"]
    assert stored.read_bytes() == pdf  # byte-for-byte, never rewritten
    s = ds["pdf"]
    assert s["extraction"]["status"] == "completed" and len(s["pages"]) == 20
    assert s["data_state"] == "extracted_awaiting_review_and_confirmation"
    assert {p["method"] for p in s["pages"]} == {"text"} and s["coverage"]["classified_share"] == 1.0
    n = h.conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'pdf.extracted'").fetchone()[0]
    assert n >= 1
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        h.conn.execute("UPDATE pdf_extraction_items SET kind = 'x'")
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        h.conn.execute("DELETE FROM pdf_extractions")


def test_multi_page_extraction_matches_the_source_statement(env):
    """Scored against the CSV the PDF was generated from (evaluation only)."""
    h, ds, rows = env["h"], env["ds"], env["rows"]
    txns = _items(h, ds["dataset_id"], kind="transaction")["items"]
    assert len(txns) == len(rows) == 784
    wrong = [
        t["item_id"]
        for t, r in zip(txns, rows, strict=True)
        if (
            t["fields"].get("date"),
            t["fields"].get("description"),
            t["fields"].get("reference"),
            t["fields"].get("debit") or "",
            t["fields"].get("credit") or "",
            t["fields"].get("balance"),
        )
        != (
            r["Txn Date"],
            r["Narration"],
            r["Ref No"],
            _fmt(r["Withdrawal"]),
            _fmt(r["Deposit"]),
            _fmt(r["Balance"]),
        )
    ]
    assert wrong == []
    assert all(not t["review_required"] and t["review_state"] == "auto_accepted" for t in txns)
    st = ds["pdf"]["statement"]
    assert st["checks"]["status"] == "evaluated" and st["checks"]["breaks"] == []
    assert st["checks"]["checks"]["opening_matches"] and st["checks"]["checks"]["closing_matches"]
    assert {t["page"] for t in txns} == set(range(1, 21)) and all(len(t["bbox"]) == 4 for t in txns)


def test_confirmed_pdf_reconciles_like_the_csv_statement(env):
    h, cfg, ds = env["h"], env["cfg"], env["ds"]
    m = ds["mapping_suggestion"]["proposed_mapping"]
    assert ds["mapping_suggestion"]["requires_confirmation"] == []  # ISO dates, withdrawal / deposit headers
    assert m["fields"]["txn_id"] == "pdf_item_id" and m["bank"]["debit_means"] == "outflow"
    r = h.confirm(ds)
    assert r["rows"] == 784 and r["issues"]["total"] == 0 and r["adapter"] == "bank_statement"
    canon = h.transactions(ds["dataset_id"])
    assert canon[0]["extra"]["pdf_review"] == "auto_accepted" and canon[0]["extra"]["pdf_page"] == "1"
    assert canon[0]["extra"]["pdf_text"].startswith(canon[0]["extra"]["pdf_text"][:10])
    runs = {}
    for name, bank in (("pdf", ds["dataset_id"]), ("csv", env["csv"]["dataset_id"])):
        su = recon.setup(h.conn, cfg, env["led"], bank)
        p = next(x for x in su["proposals"]["scopes"] if x["ledger_value"] == "LVE")
        runs[name] = recon.start(
            h.conn,
            cfg,
            recon.RunRequest(
                ledger_dataset_id=env["led"],
                bank_dataset_id=bank,
                scopes=[
                    recon.ScopeIn(
                        account_key=p["account_key"],
                        ledger_column=p["ledger_column"],
                        ledger_value=p["ledger_value"],
                    )
                ],
                ledger_debit_means="outflow",
            ),
            "t",
        )
    pdf_counts = runs["pdf"].summary["results_by_status"]
    csv_counts = runs["csv"].summary["results_by_status"]
    pdf_counts.pop("not_applicable"), csv_counts.pop("not_applicable")  # the CSV holds both accounts
    assert pdf_counts == csv_counts and pdf_counts["exact_match"] > 700
    assert (
        runs["pdf"].summary["matched_amount_minor"]["bank"]
        == runs["csv"].summary["matched_amount_minor"]["bank"]
    )
    exact = next(x for x in runs["pdf"].results if x["status"] == "exact_match")
    d = recon.result_detail(h.conn, runs["pdf"], exact["result_id"])
    bank_ev = next(e for e in d["source_evidence"] if e["side"] == "bank")
    prov = bank_ev["pdf_provenance"]
    assert prov["pdf_method"] == "text" and prov["pdf_review"] == "auto_accepted" and prov["pdf_page"]
    assert bank_ev["evidence_sha256"] == ds["evidence"]["sha256"]
    assert exact["bank_rows"][0]["extraction"]["method"] == "text"
    risk_service.clear_cache()
    assert risk_service.run_risk(h.conn, cfg, env["led"]).result_sha256 == env["p9"]  # P9 unchanged


def test_unconfirmed_extracted_lines_never_enter_matching():
    view = [
        {
            "txn_id": "PX-0000000000000001",
            "source_row": 1,
            "posted_at": "2024-01-02T00:00:00",
            "amount_minor": 100,
            "currency": "INR",
            "direction": "out",
            "account": "A1",
            "reference": None,
            "description": "X",
            "extra": {"pdf_review": "review_required", "pdf_method": "ocr"},
            "normalized_fields": [],
        }
    ]
    r = bank_rows(view, True, [Scope("SC-1", account_key(alnum("A1")), None, None)], NameIndex([], 6, 12))[0]
    assert "unconfirmed_extraction" in r.reasons and r.extraction["method"] == "ocr"


# ------------------------------------------------------------------------------------------ review


@needs_ocr
def test_ocr_rows_are_review_gated_and_exclusions_respected(env):
    h = env["h"]
    rows = p5_bank_rows()[:30]
    ds = h.upload(p5_pdf(rows, scanned_pages=(0,)), "scan.pdf", kind="bank_statement")
    did = ds["dataset_id"]
    s = ds["pdf"]
    assert s["pages"][0]["method"] == "ocr" and s["review"]["can_confirm"] is False
    assert s["extraction"]["engine"]["ocr"]["version"] == TESSERACT.version
    pending = _items(h, did, review="pending")["items"]
    assert pending and all(i["review_required"] for i in pending)
    assert all("ocr_derived" in i["reasons"] for i in pending if i["kind"] == "transaction")
    with pytest.raises(IngestError) as e:
        h.confirm(ds)
    assert e.value.code == "pdf_review_pending" and e.value.http_status == 409
    unparsed = [i for i in pending if i["kind"] == "unparsed"]
    if unparsed:
        with pytest.raises(LedgerSpyError) as e:
            pdf_service.review(h.conn, did, [{"item_id": unparsed[0]["item_id"], "decision": "confirm"}], "t")
        assert e.value.code == "review_invalid"
    txn = [i for i in pending if i["kind"] == "transaction"]
    decisions = [
        {"item_id": i["item_id"], "decision": "exclude" if k == 0 else "confirm", "reason": "page checked"}
        for k, i in enumerate(txn)
    ]
    decisions += [{"item_id": i["item_id"], "decision": "exclude"} for i in unparsed]
    out = pdf_service.review(h.conn, did, decisions, "reviewer")
    assert out["review"]["pending"] == 0 and out["review"]["can_confirm"]
    m = ds["mapping_suggestion"]["proposed_mapping"]
    r = h.confirm(ds, m)
    canon = h.transactions(did)
    assert r["rows"] == len(txn) - 1 and txn[0]["item_id"] not in {c["txn_id"] for c in canon}
    assert {c["extra"]["pdf_review"] for c in canon} == {"reviewer_confirmed"}
    assert {c["extra"]["pdf_method"] for c in canon} == {"ocr"}
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        h.conn.execute("UPDATE pdf_review_decisions SET decision = 'confirm'")
    with pytest.raises(LedgerSpyError) as e:
        pdf_service.review(h.conn, did, decisions[:1], "t")
    assert e.value.code == "already_confirmed"


@needs_ocr
def test_ocr_unavailable_is_explicit_and_reextraction_is_deterministic(env, tmp_path):
    h = env["h"]
    bad = load_config(
        overrides={
            "workspace": {"path": str(h.workspace)},
            "pdf": {"ocr": {"executable_candidates": ["no-such-tesseract"]}},
        }
    )
    staged_pdf = p5_pdf(p5_bank_rows()[:20], scanned_pages=(0,))
    good_cfg, h.cfg = h.cfg, bad
    try:
        ds = h.upload(staged_pdf, "needs_ocr.pdf", kind="bank_statement")
    finally:
        h.cfg = good_cfg
    s = ds["pdf"]
    assert s["extraction"]["status"] == "incomplete" and s["extraction"]["not_processed_pages"] == [1]
    assert "no Tesseract executable found" in s["pages"][0]["reason"] and s["coverage"]["transactions"] == 0
    with pytest.raises(IngestError) as e:
        h.confirm(ds)
    assert e.value.code == "pdf_extraction_incomplete"
    again = pdf_service.reextract(h.conn, good_cfg, h.workspace, ds["dataset_id"], "t")
    assert (
        again["new_extraction"]
        and again["extraction"]["status"] == "completed"
        and len(again["history"]) == 2
    )
    same = pdf_service.reextract(h.conn, good_cfg, h.workspace, ds["dataset_id"], "t")
    assert (
        same["new_extraction"] is False
        and same["extraction"]["items_sha256"] == again["extraction"]["items_sha256"]
    )


@needs_ocr
def test_a_killed_extraction_registers_nothing_and_keeps_the_evidence(env):
    h = env["h"]
    ds = h.upload(p5_pdf(p5_bank_rows()[:20]), "small.pdf", kind="bank_statement")
    before = h.conn.execute("SELECT COUNT(*) FROM pdf_extractions").fetchone()[0]
    slow = load_config(
        overrides={"workspace": {"path": str(h.workspace)}, "pdf": {"worker_timeout_seconds": 1}}
    )
    big = p5_pdf(p5_bank_rows()[:240], scanned_pages=tuple(range(6)))
    ds2 = h.upload(big, "big_scan.pdf", kind="bank_statement")
    with pytest.raises(LedgerSpyError) as e:
        pdf_service.reextract(h.conn, slow, h.workspace, ds2["dataset_id"], "t")
    assert e.value.code == "pdf_extraction_timeout"
    after = h.conn.execute("SELECT COUNT(*) FROM pdf_extractions").fetchone()[0]
    assert (
        after == before + 1
    )  # only big_scan's original (successful) extraction; the killed one registered nothing
    assert (
        h.conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'pdf.extraction_failed'").fetchone()[0]
        >= 1
    )
    stored = h.workspace / "evidence" / ds2["evidence"]["stored_name"]
    assert hashlib.sha256(stored.read_bytes()).hexdigest() == ds2["evidence"]["sha256"]
    assert pdf_service.summary(h.conn, ds2["dataset_id"])["extraction"]["status"] == "completed"
    assert ds["pdf"]["extraction"]["status"] == "completed"


# ------------------------------------------------------------------------------------------ hostile input


def test_invalid_pdfs_are_rejected_and_nothing_is_registered(env, tmp_path):
    h = env["h"]
    n_ds = h.conn.execute("SELECT COUNT(*) FROM datasets").fetchone()[0]
    n_ev = h.conn.execute("SELECT COUNT(*) FROM evidence_files").fetchone()[0]
    for data, code in (
        (b"%PDF-1.7 broken", "pdf_malformed"),
        (B.build([{"text": [(36, 40, "x")]}], user_password="pw"), "pdf_encrypted"),
        (b"not a pdf at all", "pdf_malformed"),
    ):
        with pytest.raises(IngestError) as e:
            h.upload(data, "bad.pdf", kind="bank_statement")
        assert e.value.code == code
    few = load_config(overrides={"workspace": {"path": str(h.workspace)}, "pdf": {"max_pages": 2}})
    good, h.cfg = h.cfg, few
    try:
        with pytest.raises(IngestError) as e:
            h.upload(p5_pdf(p5_bank_rows()[:150]), "long.pdf", kind="bank_statement")
        assert e.value.code == "pdf_too_many_pages" and e.value.http_status == 413
    finally:
        h.cfg = good
    tiny = load_config(
        overrides={"workspace": {"path": str(h.workspace)}, "ingest": {"max_file_bytes": 2000}}
    )
    good, h.cfg = h.cfg, tiny
    try:
        with pytest.raises(LedgerSpyError) as e:
            h.upload(env["pdf"], "huge.pdf", kind="bank_statement")
        assert e.value.code == "file_too_large"
    finally:
        h.cfg = good
    assert h.conn.execute("SELECT COUNT(*) FROM datasets").fetchone()[0] == n_ds
    assert h.conn.execute("SELECT COUNT(*) FROM evidence_files").fetchone()[0] == n_ev


def test_two_statements_keep_their_accounts_and_currency_is_never_assumed(env):
    h = env["h"]
    a = p5_bank_rows(LVE)[:10]
    b = p5_bank_rows("SYN826770232401")[:10]

    def spec(rows):
        return [
            {
                "date": r["Txn Date"],
                "description": r["Narration"],
                "reference": r["Ref No"],
                "debit": _fmt(r["Withdrawal"]),
                "credit": _fmt(r["Deposit"]),
                "balance": _fmt(r["Balance"]),
            }
            for r in rows
        ]

    pa = B.statement(spec(a), "p5", ["Account No: " + LVE], size=6.0)
    pb = B.statement(spec(b), "p5", ["Account No: SYN826770232401"], size=6.0)
    ds = h.upload(
        B.build([{"text": pa[0], "size": 6.0}, {"text": pb[0], "size": 6.0}]),
        "two.pdf",
        kind="bank_statement",
    )
    sugg = ds["mapping_suggestion"]
    assert "currency" in {
        i["item"] for i in sugg["requires_confirmation"]
    }  # no currency stated: not invented
    assert "Currency" not in json.loads(
        h.conn.execute("SELECT header FROM datasets WHERE dataset_id = ?", (ds["dataset_id"],)).fetchone()[0]
    )
    h.confirm(ds)
    canon = h.transactions(ds["dataset_id"])
    assert Counter(c["account"] for c in canon) == {LVE: 10, "SYN826770232401": 10}
    assert {c["currency"] for c in canon} == {None}
    su = recon.setup(h.conn, env["cfg"], env["led"], ds["dataset_id"])
    assert len(su["bank"]["accounts"]) == 2 and su["missing_currency_rows"]["bank"] == 20


# ------------------------------------------------------------------------------------------ API


@pytest.fixture(scope="module")
def api(env):
    token = secrets.token_urlsafe(32)
    with TestClient(create_app(env["cfg"], db_path_for(env["cfg"].settings), token), base_url=BASE_URL) as c:
        yield c, {"Authorization": f"Bearer {token}"}


def test_pdf_endpoints_require_the_token(api, env):
    c, _ = api
    did = env["ds"]["dataset_id"]
    item = _items(env["h"], did, kind="transaction")["items"][0]["item_id"]
    for p in (
        "/api/pdf/ocr-status",
        f"/api/datasets/{did}/pdf",
        f"/api/datasets/{did}/pdf/items",
        f"/api/datasets/{did}/pdf/items/{item}",
        f"/api/datasets/{did}/pdf/pages/1/image",
    ):
        assert c.get(p).status_code == 401, p
        assert c.get(p, headers={"Authorization": "Bearer wrong"}).status_code == 401, p
    for p in (f"/api/datasets/{did}/pdf/review", f"/api/datasets/{did}/pdf/extract"):
        assert c.post(p, json={"decisions": [{"item_id": item, "decision": "confirm"}]}).status_code == 401, p


def test_pdf_api_upload_pages_items_images_and_limits(api, env):
    c, auth = api
    case = c.post("/api/cases", json={"name": "pdf api"}, headers=auth).json()["case_id"]
    pdf = p5_pdf(p5_bank_rows()[:60])
    r = c.post(
        f"/api/cases/{case}/files?filename=s.pdf&kind=bank_statement",
        content=pdf,
        headers=auth | {"Content-Type": "application/octet-stream"},
    )
    assert r.status_code == 201 and r.json()["evidence"]["format"] == "pdf"
    did = r.json()["dataset_id"]
    s = c.get(f"/api/datasets/{did}/pdf", headers=auth).json()
    assert s["extraction"]["status"] == "completed" and s["review"]["can_confirm"]
    p1 = c.get(f"/api/datasets/{did}/pdf/items?kind=transaction&size=25", headers=auth).json()
    p2 = c.get(f"/api/datasets/{did}/pdf/items?kind=transaction&size=25&page=2", headers=auth).json()
    assert p1["total"] == 60 and len(p1["items"]) == 25
    assert not {i["item_id"] for i in p1["items"]} & {i["item_id"] for i in p2["items"]}
    assert c.get(f"/api/datasets/{did}/pdf/items?size=501", headers=auth).status_code == 422
    assert c.get(f"/api/datasets/{did}/pdf/items?kind=bogus", headers=auth).status_code == 422
    one = c.get(f"/api/datasets/{did}/pdf/items/{p1['items'][0]['item_id']}", headers=auth).json()
    assert one["item"]["text"] and one["evidence_sha256"] == hashlib.sha256(pdf).hexdigest()
    img = c.get(f"/api/datasets/{did}/pdf/pages/1/image?dpi=72", headers=auth)
    assert img.status_code == 200 and img.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert img.headers["content-type"] == "image/png"
    assert c.get(f"/api/datasets/{did}/pdf/pages/1/image?dpi=600", headers=auth).status_code == 422
    assert c.get(f"/api/datasets/{did}/pdf/pages/99/image", headers=auth).status_code == 404
    assert c.get(f"/api/datasets/{did}/pdf/items/PX-0000000000000000", headers=auth).status_code == 404
    assert c.get(f"/api/datasets/{did}/pdf/items/../../etc", headers=auth).status_code in (404, 422)
    bad = c.post(
        f"/api/datasets/{did}/pdf/review",
        json={"decisions": [{"item_id": "x", "decision": "confirm"}]},
        headers=auth,
    )
    assert bad.status_code == 422
    nr = c.post(
        f"/api/datasets/{did}/pdf/review",
        headers=auth,
        json={"decisions": [{"item_id": p1["items"][0]["item_id"], "decision": "confirm"}]},
    )
    assert nr.status_code == 422 and nr.json()["error"]["code"] == "review_invalid"  # auto-accepted row
    led_csv = c.get(f"/api/datasets/{env['led']}/pdf", headers=auth)
    assert led_csv.status_code == 409 and led_csv.json()["error"]["code"] == "not_a_pdf_dataset"
    ocr_s = c.get("/api/pdf/ocr-status", headers=auth).json()
    assert ocr_s["engine"] == "tesseract" and ocr_s["available"] == TESSERACT.available
    assert c.post(f"/api/cases/{case}/files?filename=l.pdf", content=pdf, headers=auth).status_code == 415
