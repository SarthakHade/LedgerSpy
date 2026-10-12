"""P24 end-to-end acceptance of a release bundle (development tool; never shipped).

    python tools/e2e_acceptance.py --bundle dist/ledgerspy-<v>-win_amd64-py312-offline.zip --work <empty dir>

It installs the bundle by the documented procedure (docs/installation.md §2), runs the packaged application,
and walks the documented workflow (docs/user-guide.md) through the supported interfaces: the HTTP API that the
UI uses, and the documented command line for master data and the integrity checks. Every expected value is
derived from the fixture files themselves (CSV rows, amounts, SHA-256), or from the P5 ground truth, an
evaluation-only file that the application never reads. The UI rendering of the same API values is checked by
tests/frontend/browser_check.py
(A1-A15).

Writes <work>/e2e-results.json (one entry per check: id, stage, action, fixture, expected, actual, status) and
exits 0 only if no check FAILED. A check that cannot run here is NOT TESTED, never PASS.
"""

import argparse
import csv
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packaging"))
import offline_check as oc  # noqa: E402 — start / stop / netguard / netstat helpers (P21)

RESULTS: list[dict] = []
FAILED = "FAIL"


def record(cid, stage, action, fixture, expected, actual, ok, evidence=""):
    status = "PASS" if ok is True else ("NOT TESTED" if ok is None else FAILED)
    RESULTS.append(
        {
            "id": cid,
            "stage": stage,
            "action": action,
            "fixture": fixture,
            "expected": expected,
            "actual": actual,
            "status": status,
            "evidence": evidence,
        }
    )
    print(f"  {status:10} {cid} {stage}: {actual}"[:400], flush=True)
    return ok


class Api:
    def __init__(self, base, token):
        self.base, self.token = base, token

    def req(self, method, path, body=None, raw=None, token=True):
        """(status, parsed JSON or bytes, raw bytes)."""
        headers = {"Authorization": f"Bearer {self.token}"} if token else {}
        data = None
        if raw is not None:
            data, headers["Content-Type"] = raw, "application/octet-stream"
        elif body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)  # noqa: S310
        try:
            with urllib.request.urlopen(r, timeout=900) as resp:  # noqa: S310 — loopback only
                b = resp.read()
                ct = resp.headers.get("content-type", "")
                return resp.status, (json.loads(b) if ct.startswith("application/json") else b), b
        except urllib.error.HTTPError as exc:
            b = exc.read()
            try:
                return exc.code, json.loads(b), b
            except ValueError:
                return exc.code, b, b

    def ok(self, method, path, body=None, raw=None):
        st, out, _ = self.req(method, path, body, raw)
        if st >= 400:
            raise RuntimeError(
                f"{method} {path} -> {st}: "
                + (json.dumps(out)[:600] if not isinstance(out, bytes) else repr(out[:300]))
            )
        return out


def code_of(o) -> str | None:
    """The error code of a refusal: {"status": "rejected", "error": {"code": ...}} or a validation error."""
    if not isinstance(o, dict):
        return None
    if isinstance(o.get("error"), dict):
        return o["error"].get("code")
    return "request_validation" if "detail" in o else o.get("code")


def money_minor(text: str) -> int:
    return int((Decimal(text) * 100).to_integral_value())


def csv_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ------------------------------------------------------------------------------------------- install


def install(bundle: Path, work: Path) -> Path:
    app_parent = work / "app"
    with zipfile.ZipFile(bundle) as z:
        z.extractall(app_parent)
    app = next(app_parent.iterdir())
    lines = (app / "MANIFEST.sha256").read_text(encoding="utf-8").splitlines()
    bad = [p for h, p in (ln.split("  ", 1) for ln in lines) if sha256_file(app / p) != h]
    listed = {p for _, p in (ln.split("  ", 1) for ln in lines)} | {"MANIFEST.sha256"}
    present = {p.relative_to(app).as_posix() for p in app.rglob("*") if p.is_file()}
    record(
        "I1",
        "install",
        "unzip; check every file against MANIFEST.sha256 (operations.md §10)",
        bundle.name,
        "every listed file matches; no unlisted file",
        f"{len(lines)} listed, mismatched {bad}, unlisted {sorted(present - listed)}",
        not bad and present == listed,
    )
    env = oc.env_for(work, work / "ws")
    out = subprocess.run(  # noqa: S603, S607 — the bundle's own installer
        ["cmd", "/c", str(app / "install.cmd")],
        cwd=app,
        env=env,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    (work / "install.log").write_text(out.stdout + out.stderr, encoding="utf-8")
    record(
        "I2",
        "install",
        "install.cmd (offline wheelhouse, --no-index --require-hashes)",
        "bundle",
        "exit 0 and doctor RESULT: ready",
        f"exit {out.returncode}; "
        + ("RESULT: ready" if "RESULT: ready" in out.stdout else out.stdout[-300:]),
        out.returncode == 0 and "RESULT: ready" in out.stdout,
        "install.log",
    )
    return app


def cli(app: Path, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 — the installed interpreter, fixed arguments
        [str(app / ".venv" / "Scripts" / "python.exe"), "-m", "ledgerspy", *args],
        cwd=app,
        env=env,
        capture_output=True,
        text=True,
        timeout=1800,
    )


# ------------------------------------------------------------------------------------------- workflow


def workflow(api: Api, app: Path, env: dict, ws: Path) -> dict:  # noqa: C901 — one linear script
    ref = app / "data" / "synthetic" / "reference"
    gt = json.loads(
        (
            ROOT / "data" / "synthetic" / "reference" / "ground_truth" / "SYNTHETIC_ground_truth.json"
        ).read_text(encoding="utf-8")
    )
    scen = {s["scenario_id"]: s for s in gt["scenarios"]}
    st: dict = {}

    # W1 the UI page and the API answer (the UI itself: browser check). The printed link carries the token;
    # without it the page is the documented "LedgerSpy is locked" page (HTTP 401).
    s0, locked, _ = api.req("GET", "/", token=False)
    import http.cookiejar

    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    with opener.open(
        f"{api.base}/?token={api.token}", timeout=60
    ) as resp:  # the browser's way: cookie + redirect
        s1, page = resp.status, resp.read()
    s2, health, _ = api.req("GET", "/api/health", token=False)
    s3, _, _ = api.req("GET", "/api/datasets", token=False)
    record(
        "W1",
        "start",
        "ledgerspy.cmd; open the printed link; the page without token; /api/health; API without token",
        "fresh workspace",
        "link opens the UI (200); without token the locked page (401); health OK; API refuses (401)",
        f"link {s1}, bare / {s0} locked page {b'LedgerSpy is locked' in locked}, health {s2}, "
        f"API no token {s3}",
        s1 == 200
        and b"<title>" in page
        and s0 == 401
        and b"LedgerSpy is locked" in locked
        and s2 == 200
        and s3 == 401,
    )

    # W2 case
    case = api.ok("POST", "/api/cases", {"name": "P24 acceptance (synthetic company)"})["case_id"]
    record("W2", "case", "POST /api/cases", "—", "a new engagement case", case, case.startswith("C-"))

    # W3 ledger import, derived expectations
    led_path = ref / "SYNTHETIC_ledger.csv"
    led_rows = csv_rows(led_path)
    led_total = sum(money_minor(r["Amount"]) for r in led_rows)
    up = api.ok("POST", f"/api/cases/{case}/files?filename=SYNTHETIC_ledger.csv", raw=led_path.read_bytes())
    lid = up["dataset_id"]
    api.ok("PUT", f"/api/datasets/{lid}/mapping", up["mapping_suggestion"]["proposed_mapping"])
    ds = api.ok("GET", f"/api/datasets/{lid}")
    canon = api.ok("GET", f"/api/datasets/{lid}/transactions?size=1")
    stored = ws / "evidence" / ds["evidence"]["stored_name"]
    record(
        "W3",
        "ledger import",
        "upload + confirm proposed mapping",
        "SYNTHETIC_ledger.csv",
        f"rows {len(led_rows)}; evidence sha256 = file sha256 {sha256_file(led_path)[:16]}…; stored copy "
        "read-only",
        f"rows {ds['row_count']} (canonical {canon['total']}); evidence "
        f"{ds['evidence']['sha256'][:16]}…; stored copy equal: "
        f"{sha256_file(stored) == ds['evidence']['sha256']}",
        ds["row_count"] == len(led_rows) == canon["total"]
        and ds["evidence"]["sha256"] == sha256_file(led_path)
        and sha256_file(stored) == ds["evidence"]["sha256"]
        and ds["status"] == "canonicalised",
    )

    # W4 master data by the documented command (installation.md §4), into the served workspace
    masters = {
        "vendor": "SYNTHETIC_vendor_master.csv",
        "employee": "SYNTHETIC_employee_master.csv",
        "customer": "SYNTHETIC_customer_master.csv",
    }
    codes = {}
    for etype, fname in masters.items():
        r = cli(
            app,
            env,
            "entities",
            "import-master",
            "--workspace",
            str(ws),
            "--case-id",
            case,
            "--file",
            str(ref / fname),
            "--entity-type",
            etype,
            "--use-suggested",
        )
        codes[etype] = r.returncode
    listed = api.ok("GET", f"/api/cases/{case}/masters")["master_files"]
    want = {e: len(csv_rows(ref / f)) for e, f in masters.items()}
    got = {m["entity_type"]: m.get("record_count", m.get("records")) for m in listed}
    record(
        "W4",
        "master data",
        "documented CLI: entities import-master --workspace <served> --case-id "
        "--use-suggested (server running)",
        ", ".join(masters.values()),
        f"exit 0 each; records per type = CSV rows {want}",
        f"exits {codes}; listed {got}",
        all(c == 0 for c in codes.values()) and got == want,
    )
    r = cli(
        app,
        env,
        "entities",
        "import-master",
        "--case-id",
        case,
        "--file",
        str(ref / masters["vendor"]),
        "--entity-type",
        "vendor",
        "--use-suggested",
    )  # the old, undocumented form: no --workspace
    record(
        "W4b",
        "master data",
        "same command WITHOUT --workspace (the defect fixed before P23)",
        "vendor master",
        "refused: case not found in the folder's own workspace; served workspace unchanged",
        f"exit {r.returncode}; {r.stderr.strip()[:120]}",
        r.returncode != 0 and "no case" in r.stderr,
    )
    stray = app / "workspace"
    if stray.exists():
        shutil.rmtree(stray, ignore_errors=True)

    # W5 integrity
    q = api.ok("POST", f"/api/datasets/{lid}/integrity")
    issues = api.ok("GET", f"/api/datasets/{lid}/integrity/issues?size=500")
    by_check = q["summary"]["issues_by_check"]
    rows_ok = all(1 <= (i.get("source_row") or 1) <= len(led_rows) + 1 for i in issues["items"])
    record(
        "W5",
        "data quality",
        "POST integrity; GET issues",
        "SYNTHETIC_ledger.csv",
        "total_rows = CSV rows; issues_total = Σ issues_by_check = listed issues; every issue cites a row "
        "in the file; unavailable checks say why",
        f"rows {q['summary']['total_rows']}, issues "
        f"{q['summary']['issues_total']} (Σ {sum(by_check.values())}, listed {issues['total']}), readiness "
        f"{q['readiness']['score']} {q['readiness']['status']}, not available "
        f"{sorted(q['summary']['checks_not_available'])}",
        q["summary"]["total_rows"] == len(led_rows)
        and q["summary"]["issues_total"] == sum(by_check.values()) == issues["total"]
        and rows_ok
        and all(q["summary"]["checks_not_available"].values()),
    )

    # W6 analysis: features -> rules -> anomaly -> P9
    risk = api.ok("POST", f"/api/datasets/{lid}/risk")
    rows, page_no = [], 1
    while True:
        p = api.ok("GET", f"/api/datasets/{lid}/risk/transactions?size=500&page={page_no}&sort=source_row")
        rows += p["items"]
        if page_no >= p["pages"]:
            break
        page_no += 1
    tiers = risk["counts"]["tiers"]
    t = risk["method"]["tiers"] if isinstance(risk["method"]["tiers"], dict) else {}
    bad_tier = [
        r["txn_id"]
        for r in rows
        if r["status"] == "scored"
        and (
            (r["tier"] == "critical" and not (r["score"] >= 75 and r["independent_sources"] >= 3))
            or (r["tier"] == "high" and not (r["score"] >= 50 and r["independent_sources"] >= 2))
            or (r["tier"] == "medium" and not r["score"] >= 25)
        )
    ]
    hc_minor = sum(r["amount_minor"] for r in rows if r["tier"] in ("high", "critical"))
    by_id = {r["txn_id"]: r for r in rows}
    csv_ids = {r["Txn ID"] for r in led_rows}
    record(
        "W6",
        "analysis",
        "POST risk; page every transaction",
        "SYNTHETIC_ledger.csv",
        f"{len(led_rows)} rows, each once, IDs = the file's Txn ID; tier counts = rows by tier; "
        "total amount = "
        f"CSV Σ {led_total}; high+critical amount = Σ of those rows; tiers obey 25 / 50+2 / 75+3",
        f"{len(rows)} rows, tiers {tiers}, total_minor {risk['amounts']['total_minor']}, hc_minor "
        f"{risk['amounts']['high_or_critical_minor']} (Σ rows {hc_minor}), tier rule violations "
        f"{len(bad_tier)}",
        len(rows) == len(by_id) == len(led_rows)
        and set(by_id) == csv_ids
        and sum(tiers.values()) + risk["counts"]["insufficient_data"] == len(rows)
        and all(sum(1 for r in rows if r["tier"] == k) == v for k, v in tiers.items())
        and risk["amounts"]["total_minor"] == led_total
        and risk["amounts"]["high_or_critical_minor"] == hc_minor
        and not bad_tier,
        f"result_sha256 {risk['result_sha256']}; method tiers {t}",
    )
    st["p9_sha"] = risk["result_sha256"]

    # W7 explanations: exact decomposition, evidence, wording
    top = [r for r in rows if r["tier"] in ("high", "critical")]
    bad_sum, no_ev, worded = [], [], True
    for r in top:
        ex = api.ok("GET", f"/api/datasets/{lid}/risk/transactions/{r['txn_id']}")
        contrib = sum(c["contribution"] for c in ex["explanation"]["all_contributors"])
        if abs(contrib - ex["explanation"]["score"]) > 0.011:
            bad_sum.append(r["txn_id"])
        if not all(c.get("evidence") and c.get("explanation") for c in ex["explanation"]["all_contributors"]):
            no_ev.append(r["txn_id"])
        lim = " ".join(ex["explanation"]["limitations"]).lower()
        worded &= "probab" in lim or "rank" in lim
    record(
        "W7",
        "explanations",
        "GET every high/critical explanation",
        "SYNTHETIC_ledger.csv",
        "contributions add up to the score (±0.01); every contributor has evidence and a sentence; "
        "limitations "
        "state the score is a review ranking, not a probability",
        f"{len(top)} rows; sum mismatches {bad_sum}; without evidence {no_ev}; wording ok {worded}",
        not bad_sum and not no_ev and worded,
    )

    # W8 entities, similarity, graph
    res = api.ok("GET", f"/api/datasets/{lid}/resolution")
    conf = api.ok("GET", f"/api/datasets/{lid}/resolution/pairs?status=conflicting_identity&size=500")
    merged_conflicts = [
        p for p in conf["items"] if p["a"]["canonical_entity_id"] == p["b"]["canonical_entity_id"]
    ]
    record(
        "W8",
        "entity resolution",
        "GET resolution; pairs status=conflicting_identity",
        "ledger + masters",
        "master records = imported rows; conflicting identities found and never merged",
        f"records {res['stats']['records_by_source']}; conflicting {conf['total']}; merged conflicts "
        f"{len(merged_conflicts)}",
        res["stats"]["records_by_source"].get("master") == sum(want.values())
        and conf["total"] > 0
        and not merged_conflicts,
    )
    cyc = api.ok("GET", f"/api/datasets/{lid}/graph/cycles?size=100")
    # p13-acceptance.md: S-O-01 is a 3-party ring (chain of 3; its 4th ground-truth row is the intercompany
    # mirror booking kept in both books), S-O-02 a 4-party ring; only these two cycles are flagged `review`.
    review = [c for c in cyc["items"] if c["status"] == "review"]
    chains = [{t["txn_id"] for t in (c.get("best_chain") or [])} for c in review]
    found = {}
    for sid, length in (("S-O-01", 3), ("S-O-02", 4)):
        rows_ = set(scen[sid]["affected"]["ledger_txn_ids"])
        found[sid] = any(ch <= rows_ and len(ch) == length for ch in chains)
    unknown = {t["txn_id"] for c in cyc["items"] for t in (c.get("best_chain") or [])} - csv_ids
    record(
        "W8b",
        "graph",
        "GET graph/cycles",
        "ledger + masters; ground truth S-O-01/02",
        "each ring is a `review` cycle whose chain is drawn from its rows (3 and 4 parties); only those two "
        "are flagged; every chain transaction exists in the file",
        f"{cyc['total']} cycles, {len(review)} review; rings found {found}; "
        f"unknown txn ids {sorted(unknown)[:3]}",
        all(found.values()) and len(review) == 2 and not unknown,
    )

    # W9 bank statement + reconciliation
    bank_path = ref / "SYNTHETIC_bank_statement.csv"
    bank_rows = csv_rows(bank_path)
    bank_total = sum(
        money_minor(r["Withdrawal"] or "0") + money_minor(r["Deposit"] or "0") for r in bank_rows
    )
    b = api.ok(
        "POST",
        f"/api/cases/{case}/files?filename=SYNTHETIC_bank_statement.csv&kind=bank_statement",
        raw=bank_path.read_bytes(),
    )
    bid = b["dataset_id"]
    api.ok("PUT", f"/api/datasets/{bid}/mapping", b["mapping_suggestion"]["proposed_mapping"])
    api.ok("POST", f"/api/datasets/{bid}/integrity")
    su = api.ok("GET", f"/api/reconciliation/setup?ledger_dataset_id={lid}&bank_dataset_id={bid}")
    code = {"6964": "LVE", "2401": "LVC"}
    scopes = [
        {
            "account_key": a["account_key"],
            "ledger_column": "Company Code",
            "ledger_value": code[a["account"][-4:]],
        }
        for a in su["bank"]["accounts"]
    ]
    run = api.ok(
        "POST",
        "/api/reconciliation/runs",
        {"ledger_dataset_id": lid, "bank_dataset_id": bid, "scopes": scopes, "ledger_debit_means": "outflow"},
    )
    s = run["summary"]
    sums = {
        side: sum(v for cur in s["amounts_by_status_minor"][side].values() for v in cur.values())
        for side in ("ledger", "bank")
    }
    rows_once = all(
        sum(s["rows_by_status"][side].values()) == n
        for side, n in (("ledger", len(led_rows)), ("bank", len(bank_rows)))
    )
    record(
        "W9",
        "reconciliation",
        "import statement; confirm mapping; scopes LVE/LVC; run",
        "SYNTHETIC_bank_statement.csv",
        f"bank rows {len(bank_rows)}; every row exactly once per side; Σ "
        f"amounts by status = file totals (ledger {led_total}, bank {bank_total})",
        f"rows {s['ledger_rows']}/{s['bank_rows']}, once {rows_once}, Σ ledger {sums['ledger']}, Σ bank "
        f"{sums['bank']}; results {s['results_by_status']}",
        s["bank_rows"] == len(bank_rows) and rows_once and sums == {"ledger": led_total, "bank": bank_total},
        run["run_id"],
    )
    expect = {
        "S-S-01": "unmatched_ledger",
        "S-S-02": "unmatched_bank",
        "S-S-03": "partial_match",
        "S-S-04": "grouped_match",
        "S-S-05": "grouped_match",
        "S-S-06": "partial_match",
        "S-S-07": "partial_match",
    }  # p14-acceptance.md; S-S-07 with the employee master imported
    wrong = {}
    for sid, status in expect.items():
        a = scen[sid]["affected"]
        for tid in a["ledger_txn_ids"] + a["bank_txn_ids"]:
            items = api.ok("GET", f"/api/reconciliation/runs/{run['run_id']}/results?q={tid}&size=50")[
                "items"
            ]
            got_s = {i["status"] for i in items if tid in i["ledger_txn_ids"] + i["bank_txn_ids"]}
            if got_s != {status}:
                wrong[tid] = sorted(got_s)
    record(
        "W9b",
        "reconciliation",
        "look up every S-S scenario row's result",
        "ground truth S-S-01…07",
        "ledger-only → unmatched_ledger, bank-only → unmatched_bank, delayed and fees → partial, batched and "
        "one-to-many → grouped, payee mismatch (with masters) → partial",
        f"mismatches {wrong}",
        not wrong,
    )
    amb = api.ok("GET", f"/api/reconciliation/runs/{run['run_id']}/results?status=ambiguous&size=50")["items"]
    record(
        "W9c",
        "reconciliation",
        "GET ambiguous results",
        "same run",
        "each ambiguous result lists ≥ 2 candidates and none is chosen",
        f"{len(amb)} ambiguous; candidates {[len(a['candidates']) for a in amb]}",
        bool(amb) and all(len(a["candidates"]) >= 2 for a in amb),
    )
    st.update(case=case, lid=lid, bid=bid, run=run["run_id"], led_rows=led_rows, top=top)
    return st


def pdf_flow(api: Api, st: dict) -> None:
    from tests.backend.test_pdf_datasets import p5_bank_rows, p5_pdf

    case, lid = st["case"], st["lid"]
    lve = p5_bank_rows()[:40]
    # W10a scanned page: OCR, review gate
    scan = api.ok(
        "POST",
        f"/api/cases/{case}/files?filename=scan.pdf&kind=bank_statement",
        raw=p5_pdf(lve[:12], scanned_pages=(0,)),
    )
    sid = scan["dataset_id"]
    s = api.ok("GET", f"/api/datasets/{sid}/pdf")
    req = api.ok("GET", f"/api/datasets/{sid}/pdf/items?review=required&size=200")["items"]
    early, out, _ = api.req(
        "PUT", f"/api/datasets/{sid}/mapping", scan["mapping_suggestion"]["proposed_mapping"]
    )
    ocr_rows = [i for i in req if i["method"] == "ocr"]
    record(
        "W10",
        "PDF / OCR",
        "upload a P5 statement PDF whose page 1 is scanned; confirm before review",
        "generated PDF (12 rows, page 1 image only)",
        "page 1 by OCR; every OCR line review-required; confirmation refused until reviewed",
        f"methods {[p['method'] for p in s['pages']]}; review-required "
        f"{len(req)} (OCR {len(ocr_rows)}); early confirm HTTP {early} {code_of(out)}",
        "ocr" in [p["method"] for p in s["pages"]] and ocr_rows and early >= 400,
    )
    dec = [
        {"item_id": i["item_id"], "decision": "confirm" if i["kind"] == "transaction" else "exclude"}
        for i in req
    ]
    api.ok("POST", f"/api/datasets/{sid}/pdf/review", {"decisions": dec})
    later, _, _ = api.req(
        "PUT", f"/api/datasets/{sid}/mapping", scan["mapping_suggestion"]["proposed_mapping"]
    )
    record(
        "W10b",
        "PDF / OCR",
        "decide every review-required line; confirm",
        "same PDF",
        "confirmation accepted after review",
        f"HTTP {later}",
        later == 200,
    )

    # W10c text PDF reconciled, then a correction after confirmation makes the run stale
    tp = api.ok("POST", f"/api/cases/{case}/files?filename=lve40.pdf&kind=bank_statement", raw=p5_pdf(lve))
    tid = tp["dataset_id"]
    pend = api.ok("GET", f"/api/datasets/{tid}/pdf/items?review=required&size=200")["items"]
    if pend:
        api.ok(
            "POST",
            f"/api/datasets/{tid}/pdf/review",
            {
                "decisions": [
                    {
                        "item_id": i["item_id"],
                        "decision": "confirm" if i["kind"] == "transaction" else "exclude",
                    }
                    for i in pend
                ]
            },
        )
    api.ok("PUT", f"/api/datasets/{tid}/mapping", tp["mapping_suggestion"]["proposed_mapping"])
    su = api.ok("GET", f"/api/reconciliation/setup?ledger_dataset_id={lid}&bank_dataset_id={tid}")
    acc = su["bank"]["accounts"][0]["account_key"]
    run = api.ok(
        "POST",
        "/api/reconciliation/runs",
        {
            "ledger_dataset_id": lid,
            "bank_dataset_id": tid,
            "scopes": [{"account_key": acc, "ledger_column": "Company Code", "ledger_value": "LVE"}],
            "ledger_debit_means": "outflow",
        },
    )
    exact = api.ok("GET", f"/api/reconciliation/runs/{run['run_id']}/results?status=exact_match&size=5")[
        "items"
    ][0]
    item = exact["bank_txn_ids"][0]
    inv = api.ok("POST", "/api/investigations", {"case_id": case, "title": "PDF correction check"})[
        "investigation_id"
    ]
    bad, out, _ = api.req(
        "POST",
        f"/api/investigations/{inv}/ocr-corrections",
        {
            "dataset_id": tid,
            "item_id": item,
            "field": "balance",
            "value": "1,000.00",
            "rationale": "balance misread",
        },
    )
    fix = api.ok(
        "POST",
        f"/api/investigations/{inv}/ocr-corrections",
        {
            "dataset_id": tid,
            "item_id": item,
            "field": "reference",
            "value": "SYNRCORRECTED001",
            "rationale": "Page 1 shows SYNRCORRECTED001.",
        },
    )
    runs = {r["run_id"]: r for r in api.ok("GET", "/api/reconciliation/runs")["items"]}
    gone, _, _ = api.req("GET", f"/api/reconciliation/runs/{run['run_id']}/results?size=1")
    record(
        "W10c",
        "PDF / OCR",
        "text PDF → reconcile → correct a confirmed line's reference via an "
        "investigation; try a balance correction",
        "generated text PDF (40 LVE rows)",
        "balance correction refused after confirmation; reference correction recorded as a transformation; "
        "the dependent run becomes stale and its rows are no longer served as current",
        f"balance HTTP {bad} {code_of(out)}; invalidated "
        f"{fix['correction']['invalidated_reconciliation_runs']}; run stale "
        f"{runs[run['run_id']].get('stale')}; results HTTP {gone}",
        bad >= 400
        and fix["correction"]["invalidated_reconciliation_runs"] == [run["run_id"]]
        and runs[run["run_id"]].get("stale") is True
        and gone >= 400,
    )
    st.update(stale_run=run["run_id"], pdf_inv=inv)


def investigation_and_reports(api: Api, st: dict, ws: Path) -> None:
    case, lid, run = st["case"], st["lid"], st["run"]
    top = st["top"][0]["txn_id"]
    partial = api.ok("GET", f"/api/reconciliation/runs/{run}/results?status=partial_match&size=1")["items"][0]
    hostile = "<img src=x onerror=alert(1)> note"
    inv = api.ok(
        "POST",
        "/api/investigations",
        {
            "case_id": case,
            "title": "P24 review of top lead",
            "links": [
                {
                    "link_type": "risk_transaction",
                    "dataset_id": lid,
                    "ref_id": top,
                    "reason": "highest P9 priority",
                },
                {
                    "link_type": "reconciliation_result",
                    "run_id": run,
                    "ref_id": partial["result_id"],
                    "reason": "partial",
                },
            ],
        },
    )
    iid = inv["investigation_id"]
    links = api.ok("GET", f"/api/investigations/{iid}/links")["items"]
    risk_link = next(x for x in links if x["link_type"] == "risk_transaction")["link_id"]
    api.ok("POST", f"/api/investigations/{iid}/notes", {"body": hostile})
    api.ok("POST", f"/api/investigations/{iid}/transitions", {"to_status": "in_review"})
    api.ok(
        "POST",
        f"/api/investigations/{iid}/decisions",
        {"link_id": risk_link, "action": "finding_confirmed", "rationale": "Invoice and approval reviewed."},
    )
    bad, _, _ = api.req("POST", f"/api/investigations/{iid}/transitions", {"to_status": "closed"})
    api.ok(
        "POST",
        f"/api/investigations/{iid}/transitions",
        {
            "to_status": "concluded",
            "outcome": "concerns_substantiated",
            "conclusion": "The payment lacks a supporting invoice.",
        },
    )
    act = api.ok("GET", f"/api/investigations/{iid}/activity?size=100")["items"]
    risk_now = api.ok("GET", f"/api/datasets/{lid}/risk")["result_sha256"]
    record(
        "W11",
        "investigation",
        "create with links; hostile note; in_review; confirm finding; invalid transition; conclude",
        "top P9 row + a partial reconciliation result",
        "invalid transition refused; every action an audit-referenced event; P9 result unchanged",
        f"invalid HTTP {bad}; events {[e['event'] for e in act]}; P9 unchanged {risk_now == st['p9_sha']}",
        bad >= 400 and all(e.get("audit_seq") for e in act) and risk_now == st["p9_sha"],
    )

    # W12 reports
    reps = {
        "case_investigation": api.ok(
            "POST", "/api/reports", {"report_type": "case_investigation", "investigation_ids": [iid]}
        ),
        "bank_reconciliation": api.ok(
            "POST", "/api/reports", {"report_type": "bank_reconciliation", "run_id": run}
        ),
        "audit_memo": api.ok(
            "POST", "/api/reports", {"report_type": "audit_memo", "investigation_ids": [iid]}
        ),
        "evidence_manifest": api.ok(
            "POST", "/api/reports", {"report_type": "evidence_manifest", "investigation_ids": [iid]}
        ),
    }
    hash_ok, files = {}, {}
    for kind, r in reps.items():
        for f in ("json", "html", "md"):
            _, _, raw = api.req("GET", f"/api/reports/{r['report_id']}/download?format={f}")
            files[(kind, f)] = raw
            hash_ok[f"{kind}.{f}"] = hashlib.sha256(raw).hexdigest() == r["artifacts"][f]["sha256"]
        stored = list((ws / "reports").rglob(f"*{r['report_id']}*"))
        hash_ok[f"{kind}.stored"] = bool(stored)
    record(
        "W12",
        "reports",
        "generate the four report types; download JSON / HTML / MD",
        "investigation + run",
        "every downloaded artefact's SHA-256 = the recorded one; artefacts stored in the workspace",
        f"{sum(hash_ok.values())}/{len(hash_ok)} ok; failures {[k for k, v in hash_ok.items() if not v]}",
        all(hash_ok.values()),
    )
    run_summary = api.ok("GET", f"/api/reconciliation/runs/{run}")["summary"]
    rc = (
        json.loads(files[("bank_reconciliation", "json")])["content"]
        if b'"content"' in files[("bank_reconciliation", "json")][:200]
        else json.loads(files[("bank_reconciliation", "json")])
    )
    rc = rc.get("content", rc)
    ci = json.loads(files[("case_investigation", "json")])
    ci = ci.get("content", ci)
    led_by_id = {r["Txn ID"]: r for r in st["led_rows"]}
    lv = ci["linked_values"]["transactions"]
    amounts_ok = all(
        t["amount"]["minor"] == money_minor(led_by_id[t["txn_id"]]["Amount"])
        for t in lv
        if t["txn_id"] in led_by_id
    )
    memo_md = files[("audit_memo", "md")].decode("utf-8")
    html = files[("case_investigation", "html")].decode("utf-8")
    record(
        "W12b",
        "reports",
        "compare report content with the run, the file and the analyst records",
        "reports above",
        "recon report counts = run summary; case report linked amount = CSV Amount; memo is "
        "a DRAFT and claims no approval; hostile note escaped in HTML",
        f"counts equal {rc['status_counts']['results'] == run_summary['results_by_status']}; amounts equal "
        f"{amounts_ok} ({len(lv)} rows); memo draft {'DRAFT' in memo_md.upper()}; escaped "
        f"{'&lt;img src=x' in html and '<img src=x' not in html}",
        rc["status_counts"]["results"] == run_summary["results_by_status"]
        and amounts_ok
        and bool(lv)
        and "DRAFT" in memo_md.upper()
        and "&lt;img src=x" in html
        and "<img src=x" not in html,
    )
    synth = [d.get("synthetic") for d in ci["scope"]["datasets"]]
    record(
        "W12c",
        "reports",
        "dataset 'synthetic' flag in the case report",
        "generic ledger (no synthetic declaration)",
        "unknown is reported as unknown (null), never as false",
        f"{synth}",
        all(x is None for x in synth),
    )
    stale = api.ok("POST", "/api/reports", {"report_type": "bank_reconciliation", "run_id": st["stale_run"]})
    sc = api.ok("GET", f"/api/reports/{stale['report_id']}")["content"]
    record(
        "W14",
        "stale inputs",
        "report on the run invalidated in W10c",
        "stale run",
        "shown as superseded with its recorded summary only",
        f"run_state {sc['run_state']}: {(sc.get('run_state_reason') or '')[:80]}",
        sc["run_state"] != "current",
    )
    st.update(iid=iid)


def integrity_checks(api: Api, app: Path, env: dict, ws: Path, work: Path) -> None:
    a = cli(app, env, "audit", "verify", "--workspace", str(ws))
    e = cli(app, env, "evidence", "verify", "--workspace", str(ws))
    m = re.search(r"head seq (\d+) hash ([0-9a-f]{64})", a.stdout)
    anchor = api.ok("GET", f"/api/audit/verify?anchor_seq={m.group(1)}&anchor_hash={m.group(2)}") if m else {}
    record(
        "W13",
        "integrity",
        "documented: audit verify, evidence verify (installed CLI); API anchor check",
        "the P24 workspace",
        "exit 0 both; anchor matches",
        f"audit exit {a.returncode} "
        f"({a.stdout.strip()[-90:]}); evidence exit {e.returncode}; anchor {anchor.get('anchor')}",
        a.returncode == 0 and e.returncode == 0 and bool(anchor.get("ok")) and anchor["anchor"]["matches"],
    )


def tamper_copy(app: Path, env: dict, ws: Path, work: Path, anchor) -> None:
    """On a COPY of the stopped workspace: altered evidence, a dropped trigger and an edit are detected."""
    copy = work / "ws-tamper"
    shutil.copytree(ws, copy)
    ev = next((copy / "evidence").iterdir())
    ev.chmod(0o644)
    ev.write_bytes(ev.read_bytes() + b"x")
    e = cli(app, env, "evidence", "verify", "--workspace", str(copy))
    con = sqlite3.connect(copy / "ledgerspy.db")
    con.execute("DROP TRIGGER investigation_notes_no_update")
    con.execute(
        "UPDATE investigation_notes SET body = 'edited' "
        "WHERE rowid = (SELECT MIN(rowid) FROM investigation_notes)"
    )
    con.commit()
    con.close()
    a = cli(app, env, "audit", "verify", "--workspace", str(copy))
    record(
        "W13b",
        "integrity",
        "tamper a COPY: append a byte to an evidence file; drop an append-only trigger and edit a note",
        "copy of the P24 workspace",
        "evidence verify FAIL (exit 1); audit verify FAIL (exit 1) naming the trigger",
        f"evidence exit {e.returncode}: {e.stdout.strip()[:100]}; audit "
        f"exit {a.returncode}: {a.stdout.strip()[:140]}",
        e.returncode == 1 and a.returncode == 1 and "investigation_notes_no_update" in a.stdout,
    )
    shutil.rmtree(copy, ignore_errors=True)


def invalid_inputs(api: Api, st: dict) -> None:
    case = st["case"]
    checks = {}
    s, o, _ = api.req("POST", f"/api/cases/{case}/files?filename=empty.csv", raw=b"")
    checks["empty file"] = (s, code_of(o))
    s, o, _ = api.req("POST", f"/api/cases/{case}/files?filename=bad.xlsx", raw=b"PK\x03\x04garbage" * 10)
    checks["corrupt xlsx"] = (s, code_of(o))
    s, o, _ = api.req("POST", f"/api/cases/{case}/files?filename=x.csv", raw=b"a;b\n1;2\n")
    nomap = o.get("dataset_id") if isinstance(o, dict) else None
    if nomap:
        s2, o2, _ = api.req(
            "PUT",
            f"/api/datasets/{nomap}/mapping",
            {"adapter": "generic_ledger", "fields": {"description": "a"}, "label_columns": [], "options": {}},
        )
        checks["mapping without required fields"] = (s2, code_of(o2))
        s3, o3, _ = api.req("POST", f"/api/datasets/{nomap}/risk")
        checks["analysis of an unconfirmed dataset"] = (s3, code_of(o3))
    s, o, _ = api.req("GET", "/api/datasets/D-00000000000000000000000000/risk")
    checks["unknown dataset"] = (s, code_of(o))
    s, o, _ = api.req("POST", "/api/reports", {"report_type": "audit_memo", "investigation_ids": []})
    checks["memo without investigations"] = (s, code_of(o))
    s, o, _ = api.req(
        "POST",
        "/api/reconciliation/runs",
        {
            "ledger_dataset_id": st["lid"],
            "bank_dataset_id": st["lid"],
            "scopes": [],
            "ledger_debit_means": "outflow",
        },
    )
    checks["reconcile a ledger against itself"] = (s, code_of(o))
    record(
        "W15",
        "invalid inputs",
        "empty / corrupt / unmappable files, unconfirmed analysis, unknown IDs, "
        "invalid report and run requests",
        "hand-made inputs",
        "each refused with an explicit 4xx code; no result produced",
        json.dumps(checks),
        all(400 <= v[0] < 500 and v[1] for v in checks.values()),
    )


def reproduce(api_factory, app, env, st, work):
    """Restart the packaged app on the same workspace: recomputed results must be identical."""
    api = api_factory()
    risk = api.ok("POST", f"/api/datasets/{st['lid']}/risk")
    run = api.ok("GET", f"/api/reconciliation/runs/{st['run']}")
    r1 = api.ok("POST", "/api/reports/preview", {"report_type": "bank_reconciliation", "run_id": st["run"]})
    record(
        "W16",
        "reproducibility",
        "stop (Ctrl+Break), restart on the same workspace; recompute P9; reload the "
        "recorded run (recomputed and checked against its hash)",
        "P24 workspace",
        "P9 result_sha256 identical; run result identical; data intact",
        f"P9 {risk['result_sha256'] == st['p9_sha']}; run {run['result_sha256'] == st['run_sha']}; preview "
        f"{r1['content']['content_sha256'][:12]}",
        risk["result_sha256"] == st["p9_sha"] and run["result_sha256"] == st["run_sha"],
    )


def paysim(api: Api) -> None:
    pay = ROOT / "data" / "raw" / "paysim_sample_10k.csv"
    if not pay.is_file():
        record(
            "W17",
            "public ledger",
            "PaySim sample",
            "data/raw/paysim_sample_10k.csv",
            "—",
            "file missing",
            None,
        )
        return
    rows = csv_rows(pay)
    case = api.ok("POST", "/api/cases", {"name": "P24 PaySim (public simulation)"})["case_id"]
    up = api.ok("POST", f"/api/cases/{case}/files?filename=paysim_sample_10k.csv", raw=pay.read_bytes())
    api.ok("PUT", f"/api/datasets/{up['dataset_id']}/mapping", up["mapping_suggestion"]["proposed_mapping"])
    q = api.ok("POST", f"/api/datasets/{up['dataset_id']}/integrity")
    r = api.ok("POST", f"/api/datasets/{up['dataset_id']}/risk")
    labels = up["mapping_suggestion"]["label_columns"]
    record(
        "W17",
        "public ledger",
        "import, integrity, analysis of the PaySim sample",
        "paysim_sample_10k.csv",
        f"rows {len(rows)}; isFraud split off as a label; no high/critical "
        "(P9 record: nothing corroborates); "
        "unavailable detectors stated",
        f"rows {up['row_count']}; labels {labels}; tiers {r['counts']['tiers']}; "
        f"unavailable {r['counts']['detectors_unavailable']}; readiness {q['readiness']['status']}",
        up["row_count"] == len(rows)
        and "isFraud" in labels
        and r["counts"]["high_or_critical"] == 0
        and r["counts"]["detectors_unavailable"] > 0,
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="P24 end-to-end acceptance of a release bundle")
    ap.add_argument("--bundle", type=Path, required=True)
    ap.add_argument("--work", type=Path, required=True, help="empty folder (kept; delete it afterwards)")
    ap.add_argument("--port", type=int, default=8797)
    args = ap.parse_args(argv)
    work = args.work.resolve()
    if work.exists() and any(work.iterdir()):
        print("ERROR: --work must be empty", file=sys.stderr)
        return 3
    work.mkdir(parents=True, exist_ok=True)
    ws = work / "ws"
    app = install(args.bundle.resolve(), work)
    env = oc.env_for(work, ws)
    sampler = oc.NetstatSampler(app)
    sampler.start()
    proc, base, token = oc.start(app, env, args.port)
    api = Api(base, token)
    st: dict = {}
    try:
        st = workflow(api, app, env, ws)
        st["run_sha"] = api.ok("GET", f"/api/reconciliation/runs/{st['run']}")["result_sha256"]
        pdf_flow(api, st)
        investigation_and_reports(api, st, ws)
        integrity_checks(api, app, env, ws, work)
        invalid_inputs(api, st)
        paysim(api)
    except Exception as exc:  # a crash is a failed check, reported, never a pass
        record("X", "workflow", "—", "—", "no unhandled error", f"{type(exc).__name__}: {exc}"[:600], False)
    finally:
        code = oc.stop(proc)
    record("S1", "shutdown", "Ctrl+Break to ledgerspy.cmd", "—", "graceful exit 0", f"exit {code}", code == 0)
    if st.get("run_sha"):
        proc, base, token = oc.start(app, env, args.port)
        try:
            reproduce(lambda: Api(base, token), app, env, st, work)
        except Exception as exc:
            record(
                "W16",
                "reproducibility",
                "—",
                "—",
                "no unhandled error",
                f"{type(exc).__name__}: {exc}"[:600],
                False,
            )
        finally:
            oc.stop(proc)
        tamper_copy(app, env, ws, work, None)
    sampler.stop_flag.set()
    sampler.join(timeout=10)
    netlog = (work / "netguard.log").read_text(encoding="utf-8") if (work / "netguard.log").exists() else ""
    record(
        "N1",
        "network",
        "netguard in every installed Python process; netstat of the installation's processes",
        "whole run",
        "no non-loopback attempt or connection",
        f"guard log {len(netlog)} bytes; "
        f"{len(sampler.foreign)} foreign connections in {sampler.samples} samples",
        not netlog.strip() and not sampler.foreign,
    )
    record(
        "N2",
        "network",
        "the same flow on a machine with networking physically disabled",
        "target laptop",
        "—",
        "not performed here (would change system settings); the user's manual step",
        None,
    )
    (work / "e2e-results.json").write_text(
        json.dumps(RESULTS, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    counts = {k: sum(1 for r in RESULTS if r["status"] == k) for k in ("PASS", FAILED, "NOT TESTED")}
    print(f"\n{counts['PASS']} passed, {counts[FAILED]} failed, {counts['NOT TESTED']} not tested")
    return 1 if counts[FAILED] else 0


if __name__ == "__main__":
    sys.exit(main())
