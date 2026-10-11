"""
LedgerSpy browser check (P1 criteria, rewritten for the P9 API-driven UI). Optional dev tool.

Starts `ledgerspy serve` on a temporary workspace, imports the PaySim sample and the synthetic ledger through the
API, then drives the real page in headless Chromium:
  A3  a hostile CSV imported through the UI renders as text (no dialogs, no injected elements)
  A4  only loopback requests; no CSP violations
  A5  displayed numbers equal the API (hero tiers, donut, table rows, explanation contributions)
  A5  unavailable modules and unsupported detectors are labelled, not simulated
  A6  transaction IDs are the backend IDs; detail = the API explanation for that ID
  A7  tier filter, search, pagination, row detail, chatbot and every tab work
  A8  opened as a plain file the page computes nothing and names the requirement
  A9  no JavaScript errors and no dialogs during the run
  A10 (P13) the relationship graph is drawn from the graph API
  A11 (P14) bank reconciliation shows the reconciliation API's results; PDF/OCR is labelled P14b
  A12 (P14b) a PDF statement's extraction review shows the PDF API's rows, page image and decisions
  A13 (P15) an investigation opened from a finding: link, note (rendered as text), transition, decision via API
  A14 (P16) reports: a case-report preview equals the API's content, a stored draft memo is versioned and
      downloads as escaped HTML with its recorded SHA-256

Requires: playwright + Chromium (no download at runtime).
Usage (from repo root):  python tests/frontend/browser_check.py [--screenshots DIR]
"""

from __future__ import annotations

import json
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
PAYSIM = ROOT / "data" / "raw" / "paysim_sample_10k.csv"
SYNTHETIC = ROOT / "data" / "synthetic" / "reference" / "SYNTHETIC_ledger.csv"
BANK = ROOT / "data" / "synthetic" / "reference" / "SYNTHETIC_bank_statement.csv"
MALICIOUS = ROOT / "tests" / "frontend" / "fixtures" / "malicious.csv"
results: list[dict] = []


def check(cid: str, name: str, cond: bool, detail: str = "") -> None:
    results.append({"id": cid, "name": name, "ok": bool(cond), "detail": detail})
    print(f"  {'PASS' if cond else 'FAIL'}  [{cid}] {name}{' — ' + detail if detail else ''}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def api(base: str, token: str, method: str, path: str, body=None, raw: bool = False):
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if body is not None:
        data = body if raw else json.dumps(body).encode()
        headers["Content-Type"] = "application/octet-stream" if raw else "application/json"
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)  # noqa: S310 — loopback
    with urllib.request.urlopen(req, timeout=120) as r:  # noqa: S310
        return json.loads(r.read())


def imported(base: str, token: str, path: Path, name: str) -> str:
    case = api(base, token, "POST", "/api/cases", {"name": name})
    ds = api(
        base,
        token,
        "POST",
        f"/api/cases/{case['case_id']}/files?filename={path.name}",
        path.read_bytes(),
        raw=True,
    )
    api(
        base,
        token,
        "PUT",
        f"/api/datasets/{ds['dataset_id']}/mapping",
        ds["mapping_suggestion"]["proposed_mapping"],
    )
    api(base, token, "POST", f"/api/datasets/{ds['dataset_id']}/integrity")
    return ds["dataset_id"]


def analyse(page, dataset_id: str) -> None:
    page.select_option("#dataset-select", dataset_id)
    page.click("#run-btn")
    page.wait_for_function(
        "() => /Analysis finished/.test(document.getElementById('main-status-text').textContent)",
        timeout=120000,
    )


def remove_workspace(folder: Path) -> None:
    """Delete this run's temporary workspace (P22). Evidence files are read-only by design, so clear the
    flag first; only the mkdtemp folder created by this run is touched."""
    import os
    import shutil
    import stat

    import time

    def retry(func, path, _exc):
        os.chmod(path, stat.S_IWRITE)
        func(path)

    hook = {"onexc": retry} if sys.version_info >= (3, 12) else {"onerror": retry}
    for _ in range(10):  # the stopped server may still hold the database for a moment on Windows
        try:
            shutil.rmtree(folder, **hook)
            return
        except OSError:
            time.sleep(0.5)
    print(f"WARNING: could not remove the temporary workspace {folder}", file=sys.stderr)


def main() -> int:
    shots = None
    if "--screenshots" in sys.argv:
        shots = Path(sys.argv[sys.argv.index("--screenshots") + 1])
        shots.mkdir(parents=True, exist_ok=True)
    port = free_port()
    ws = Path(tempfile.mkdtemp(prefix="ledgerspy-browser-")) / "ws"  # removed at the end (P22)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-m",
            "ledgerspy",
            "serve",
            "--no-browser",
            "--port",
            str(port),
            "--workspace",
            str(ws),
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    errors: list[str] = []
    dialogs: list[str] = []
    try:
        url = token = None
        while url is None:
            m = re.search(r"Open: (http://127\.0\.0\.1:\d+/\?token=(\S+))", proc.stdout.readline())
            if m:
                url, token = m.group(1), m.group(2)
        base = f"http://127.0.0.1:{port}"
        for _ in range(50):
            try:
                urllib.request.urlopen(base + "/api/health", timeout=2)  # noqa: S310
                break
            except OSError:
                time.sleep(0.1)
        pay_id = imported(base, token, PAYSIM, "PaySim sample")
        syn_id = imported(base, token, SYNTHETIC, "Synthetic company")

        with sync_playwright() as p:
            browser = p.chromium.launch()

            # ---------------- A8: plain file, no backend ----------------
            plain = browser.new_page()
            plain.on("pageerror", lambda e: errors.append("file: " + str(e)))
            plain.goto((ROOT / "frontend" / "index.html").as_uri())
            plain.wait_for_timeout(300)
            st = plain.text_content("#main-status-text")
            hero = [plain.text_content(f"#hero-n-{t}") for t in ("critical", "high", "medium", "low")]
            check(
                "A8",
                "Opened as a file the page computes nothing and names `ledgerspy serve`",
                "ledgerspy serve" in st
                and hero == ["—"] * 4
                and plain.text_content("#tx-body").strip() == "",
                st[:80],
            )
            plain.close()

            # ---------------- served page ----------------
            ctx = browser.new_context(viewport={"width": 1440, "height": 900}, accept_downloads=True)
            page = ctx.new_page()
            requests: list[str] = []
            page.on("request", lambda r: requests.append(r.url))
            page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.add_init_script(
                "window.__csp=[];document.addEventListener('securitypolicyviolation',"
                "e=>window.__csp.push(e.violatedDirective));"
            )
            page.goto(url)
            page.wait_for_function("() => document.getElementById('dataset-select').options.length >= 2")

            # ---------------- A3: hostile CSV through the UI import ----------------
            page.set_input_files("#file-input", str(MALICIOUS))
            page.wait_for_selector("#mapping-card button.btn-primary")
            page.select_option("#mapping-body select", "debit")
            page.click("#mapping-card button.btn-primary")
            page.wait_for_function(
                "() => /Analysis finished/.test(document.getElementById('main-status-text').textContent)",
                timeout=120000,
            )
            for tab in ["tab-ov", "tab-di", "tab-fi", "tab-bsr", "tab-tr", "tab-an", "tab-re", "tab-ex"]:
                page.click(f'button.tab-btn[data-tab="{tab}"]')
                page.wait_for_timeout(100)
            page.click("#tx-body tr.main-row >> nth=0")
            page.wait_for_function(
                "() => !/Loading explanation/.test(document.querySelector('#tx-body tr.detail-row').textContent)"
            )
            page.click("#ai-mascot")
            for q in [
                "High risk transactions",
                "show summary",
                "why is INV-0001 flagged?",
                "unavailable detectors",
            ]:
                page.fill("#chat-input", q)
                page.press("#chat-input", "Enter")
                page.wait_for_timeout(500)
            page.click("#ai-mascot")
            injected = page.evaluate("""() => ({
                img: document.querySelectorAll('img[src="x"]').length,
                script: [...document.scripts].filter(s => /alert\\(/.test(s.textContent)).length,
                svg: document.querySelectorAll('svg[onload]').length,
                bold: [...document.querySelectorAll('b')].filter(b => b.textContent === 'bold sender').length,
                textShown: document.getElementById('tx-body').innerText.includes("<img src=x onerror=alert('vendor')>"),
            })""")
            check("A3", "No dialogs fired from hostile CSV content", not dialogs, f"dialogs: {dialogs}")
            check(
                "A3",
                "No elements injected from CSV values",
                injected["img"] == 0
                and injected["script"] == 0
                and injected["svg"] == 0
                and injected["bold"] == 0,
                json.dumps(injected),
            )
            check("A3", "Hostile values are displayed as literal text", injected["textShown"])

            # ---------------- A5: PaySim, DOM = API ----------------
            analyse(page, pay_id)
            s = page.evaluate(f"() => LS_API.get('/api/datasets/{pay_id}/risk')")
            first = page.evaluate(
                f"() => LS_API.get('/api/datasets/{pay_id}/risk/transactions?size=50&sort=priority')"
            )
            hero = {t: page.text_content(f"#hero-n-{t}") for t in ("critical", "high", "medium", "low")}
            check(
                "A5",
                "Hero tier counts equal the API summary",
                hero == {t: f"{s['counts']['tiers'][t]:,}" for t in hero},
                json.dumps(hero),
            )
            donut = page.evaluate(
                """() => ['critical','high','medium','low','insufficient'].map(id => +document.getElementById('donut-' + id).getAttribute('data-count'))"""
            )
            check(
                "A5",
                "Donut segments equal the API tier counts",
                donut
                == [s["counts"]["tiers"][t] for t in ("critical", "high", "medium", "low")]
                + [s["counts"]["insufficient_data"]],
                json.dumps(donut),
            )
            ids = page.evaluate(
                "() => [...document.querySelectorAll('#tx-body .tx-id')].map(n => n.textContent)"
            )
            check(
                "A6",
                "Table rows are the API's first page in review order (backend IDs)",
                ids == [r["txn_id"] for r in first["items"]],
                f"{len(ids)} rows, first {ids[:2]}",
            )
            page.click("#tx-body tr.main-row >> nth=0")
            page.wait_for_function(
                "() => !/Loading explanation/.test(document.querySelector('#tx-body tr.detail-row').textContent)"
            )
            detail = page.evaluate(
                f"() => LS_API.get('/api/datasets/{pay_id}/risk/transactions/' + encodeURIComponent({json.dumps(ids[0])}))"
            )
            shown = page.text_content("#tx-body tr.detail-row")
            contribs_ok = all(
                f"+{c['contribution']:.2f} pts" in shown for c in detail["explanation"]["contributors"]
            )
            check(
                "A5",
                "Row detail shows the API explanation and its exact contributions",
                detail["explanation"]["headline"] in shown
                and contribs_ok
                and detail["explanation"]["limitations"][0] in shown,
                detail["explanation"]["headline"][:90],
            )
            na = page.evaluate("""() => ({
                recon: /OCR (available|UNAVAILABLE)/i.test(document.getElementById('bsr-pdf-status').textContent),  // P14b: OCR status from the API
                montecarlo: [...document.querySelectorAll('#tab-tr .na-card')].some(n => /going-concern/.test(n.textContent)),
                benchmark: [...document.querySelectorAll('#tab-ov .na-card')].some(n => /benchmark/i.test(n.textContent)),
                graph: [...document.querySelectorAll('#tab-re .na-card')].some(n => /Entity graph not available/.test(n.textContent)),
                monthly: /Not available/.test(document.getElementById('trends-monthly-bars').textContent),
                unavailable: [...document.querySelectorAll('#fi-detectors tbody tr')].filter(tr => /NOT AVAILABLE|NOT APPLICABLE/.test(tr.textContent)).length,
            })""")
            check(
                "A5",
                "Unavailable modules and unsupported PaySim detectors are labelled",
                na["recon"]
                and na["montecarlo"]
                and na["benchmark"]
                and na["graph"]
                and na["monthly"]
                and na["unavailable"] == s["counts"]["detectors_unavailable"],
                json.dumps(na),
            )

            # ---------------- A7: synthetic ledger interactions ----------------
            analyse(page, syn_id)
            s = page.evaluate(f"() => LS_API.get('/api/datasets/{syn_id}/risk')")
            page.click("#risk-filters .t-filter[data-tier='high']")
            page.wait_for_function(
                "() => [...document.querySelectorAll('#tx-body tr.main-row')].every(r => r.dataset.tier === 'high')"
            )
            tiers = page.evaluate(
                "() => [...document.querySelectorAll('#tx-body tr.main-row')].map(r => r.dataset.tier)"
            )
            filt_ok = len(tiers) == min(50, s["counts"]["tiers"]["high"]) and set(tiers) == {"high"}
            page.click("#risk-filters .t-filter[data-tier='']")
            api_total = page.evaluate(
                f"() => LS_API.get('/api/datasets/{syn_id}/risk/transactions?q=LVC-JV-0008').then(r => r.total)"
            )
            page.fill("#search-in", "LVC-JV-0008")
            page.wait_for_function(
                "n => document.getElementById('search-count-pill').textContent.startsWith(n)", arg=f"{api_total:,} match"
            )
            pill = page.text_content("#search-count-pill")
            page.fill("#search-in", "")
            page.wait_for_function(
                "() => document.getElementById('search-count-pill').style.display === 'none'"
            )
            page.click("#tx-pager button:has-text('Next')")
            page.wait_for_function(
                "() => /Page 2 of/.test(document.getElementById('tx-page-label').textContent)"
            )
            page.click("#ai-mascot")
            answers = []
            for chip in page.query_selector_all(".chat-sug"):
                chip.click()
                page.wait_for_timeout(700)
                answers.append(
                    page.evaluate(
                        "() => document.querySelector('#chat-messages .chat-msg.bot:last-child').innerText.slice(0, 50)"
                    )
                )
            top = s["top_findings"][0]["txn_id"]
            page.fill("#chat-input", f"why is {top} flagged?")
            page.press("#chat-input", "Enter")
            page.wait_for_timeout(900)
            why = page.evaluate(
                "() => document.querySelector('#chat-messages .chat-msg.bot:last-child').innerText"
            )
            page.click("#ai-mascot")

            # ---------------- A10 (P13): relationship graph from the graph API ----------------
            page.click('button.tab-btn[data-tab="tab-re"]')
            page.wait_for_selector("#re-graph-svg", state="attached", timeout=120000)
            page.wait_for_function("() => document.querySelectorAll('#re-graph-cycles tbody tr').length > 0")
            viz = page.evaluate(f"() => LS_API.get('/api/datasets/{syn_id}/graph/visualization')")
            gsum = page.evaluate(f"() => LS_API.get('/api/datasets/{syn_id}/graph')")
            cyc = page.evaluate(f"() => LS_API.get('/api/datasets/{syn_id}/graph/cycles?size=25')")
            drawn = page.evaluate(
                """() => ({
                nodes: document.querySelectorAll('#re-graph-svg circle.gr-node').length,
                edges: document.querySelectorAll('#re-graph-svg line.gr-edge').length,
                summary: document.getElementById('re-graph-summary').textContent,
                cycles: document.querySelectorAll('#re-graph-cycles tbody tr.main-row').length,
                ids: [...document.querySelectorAll('#re-graph-svg circle.gr-node')].map(c => c.dataset.nodeId).sort(),
            })"""
            )
            check(
                "A10",
                "Relationship graph is drawn from the graph API only (same nodes, edges and cycles; no invented elements)",
                drawn["nodes"] == len(viz["nodes"])
                and drawn["edges"] == len(viz["edges"])
                and drawn["ids"] == sorted(n["node_id"] for n in viz["nodes"])
                and gsum["graph_sha256"][:12] in drawn["summary"]
                and drawn["cycles"] == len(cyc["items"]),
                f"{drawn['nodes']} nodes, {drawn['edges']} edges, {drawn['cycles']} cycles",
            )
            first = cyc["items"][0]
            page.click("#re-graph-cycles tbody tr.main-row >> nth=0")
            page.wait_for_function("() => /Cycle C-/.test(document.getElementById('re-graph-detail').textContent)")
            detail = page.text_content("#re-graph-detail")
            chain_ok = first["best_chain"] is None or all(t["txn_id"] in detail for t in first["best_chain"])
            check(
                "A10",
                "Selecting a cycle shows its status and the actual transaction chain from the API",
                first["status"] in detail and chain_ok,
                first["cycle_id"] + " " + first["status"],
            )
            # ---------------- A11 (P14): bank reconciliation from the reconciliation API ----------------
            case_id = api(base, token, "GET", f"/api/datasets/{syn_id}")["case_id"]
            bds = api(base, token, "POST", f"/api/cases/{case_id}/files?filename={BANK.name}&kind=bank_statement",
                      BANK.read_bytes(), raw=True)
            api(base, token, "PUT", f"/api/datasets/{bds['dataset_id']}/mapping",
                bds["mapping_suggestion"]["proposed_mapping"])
            setup = api(base, token, "GET",
                        f"/api/reconciliation/setup?ledger_dataset_id={syn_id}&bank_dataset_id={bds['dataset_id']}")
            page.click('button.tab-btn[data-tab="tab-bsr"]')
            page.evaluate(f"() => document.dispatchEvent(new CustomEvent('ls:dataset', {{ detail: {{ datasetId: '{syn_id}' }} }}))")
            page.wait_for_selector("#bsr-run", timeout=120000)
            code = {"…6964": "LVE", "…2401": "LVC"}
            for a in setup["bank"]["accounts"]:
                page.check(f"#bsr-scope-use-{a['account_key']}")
                page.select_option(f"#bsr-scope-col-{a['account_key']}", "Company Code")
                page.fill(f"#bsr-scope-val-{a['account_key']}", code[a["account"]])
            page.select_option("#bsr-ledger-debit", "outflow")
            page.click("#bsr-run")
            page.wait_for_function("() => /Run RR-/.test(document.getElementById('bsr-summary').textContent)",
                                   timeout=120000)
            runs = api(base, token, "GET", f"/api/reconciliation/runs?ledger_dataset_id={syn_id}")["items"]
            rsum = api(base, token, "GET", f"/api/reconciliation/runs/{runs[0]['run_id']}")
            shown = page.evaluate(
                """() => Object.fromEntries([...document.querySelectorAll('#bsr-summary .bsr-stat')]
                .map(n => [n.dataset.status, Number(n.querySelector('.bsr-stat-n').textContent.replace(/,/g, ''))]))"""
            )
            check(
                "A11",
                "Bank reconciliation shows the API's status counts for the confirmed scopes (no simulated matches)",
                shown == rsum["summary"]["results_by_status"]
                and rsum["run_id"] in page.text_content("#bsr-summary")
                and shown["exact_match"] > 7000,
                json.dumps(shown),
            )
            page.select_option("#bsr-f-status", "partial_match")
            page.click("#bsr-apply")
            page.wait_for_function("() => /Partial match/.test(document.querySelector('#bsr-results tbody').textContent)")
            page.click("#bsr-results tbody tr.main-row >> nth=0")
            page.wait_for_function("() => /Source evidence/.test(document.getElementById('bsr-detail').textContent)")
            detail = page.text_content("#bsr-detail")
            p14b = page.text_content("#bsr-p14b")
            check(
                "A11",
                "A partial match shows its discrepancy and source rows; the PDF/OCR (P14b) card is shown",
                "Not reconciled" in detail and "SYNTHETIC_bank_statement.csv row" in detail and "P14b" in p14b,
                detail[:90],
            )
            # ---------------- A12 (P14b): PDF statement extraction review from the PDF API ----------------
            sys.path.insert(0, str(ROOT))
            from tests.backend import pdf_builder as pb

            spec = [{"date": f"0{d}/01/2024", "description": f"PAYMENT {d}", "debit": "10.00",
                     "balance": f"{1000 - 10 * d}.00"} for d in range(1, 7)]
            pdf = pb.statement_pdf(spec, info=["Account No: 1234567890", "Currency: INR"], rows_per_page=3,
                                   scanned_pages=(1,), dpi=300)
            pds = api(base, token, "POST", f"/api/cases/{case_id}/files?filename=scan.pdf&kind=bank_statement",
                      pdf, raw=True)
            pid = pds["dataset_id"]
            page.evaluate(f"() => document.dispatchEvent(new CustomEvent('ls:dataset', {{ detail: {{ datasetId: '{syn_id}' }} }}))")
            page.click("text=scan.pdf (pdf)", timeout=60000)
            page.wait_for_function("() => /PDF extraction review/.test(document.getElementById('bsr-setup').textContent)",
                                   timeout=120000)
            req = api(base, token, "GET", f"/api/datasets/{pid}/pdf/items?review=required&size=200")
            summ = api(base, token, "GET", f"/api/datasets/{pid}/pdf")
            n_rows = page.locator("#bsr-pdf-items tbody tr").count()
            text_ui = page.text_content("#bsr-setup")
            check(
                "A12",
                "PDF review shows the API's review-required rows, per-page methods and the OCR engine",
                n_rows == req["total"] and summ["extraction"]["extraction_id"] in text_ui
                and "Tesseract" in text_ui and [p["method"] for p in summ["pages"]] == ["text", "ocr"],
                f"{n_rows} rows, pages {[p['method'] for p in summ['pages']]}",
            )
            page.click("#bsr-pdf-items tbody tr >> nth=0 >> text=Page")
            page.wait_for_function("() => document.getElementById('bsr-pdf-canvas').width > 100", timeout=60000)
            drawn_w = page.evaluate("() => document.getElementById('bsr-pdf-canvas').width")
            page.click("#bsr-pdf-items tbody tr >> nth=0 >> text=Confirm")
            page.wait_for_function(
                f"() => LS_API.get('/api/datasets/{pid}/pdf').then(s => s.review.pending === {summ['review']['pending'] - 1})",
                timeout=60000,
            )
            after = api(base, token, "GET", f"/api/datasets/{pid}/pdf")
            page.wait_for_function("() => /confirmed/.test(document.getElementById('bsr-pdf-items').textContent)",
                                   timeout=60000)
            check(
                "A12",
                "The original page is drawn from the API and a reviewer decision is stored through the API",
                drawn_w > 100
                and after["review"]["pending"] == summ["review"]["pending"] - 1
                and after["review"]["can_confirm"] is False,
                json.dumps(after["review"]["by_state"]),
            )
            # ---------------- A13 (P15): investigation workflow from a reconciliation finding ----------------
            page.click('button.tab-btn[data-tab="tab-bsr"]')
            page.click("#bsr-results tbody tr.main-row >> nth=0")
            page.wait_for_selector("#bsr-detail >> text=Open investigation from this finding", timeout=60000)
            page.click("#bsr-detail >> text=Open investigation from this finding")
            page.wait_for_selector("#iv-create", timeout=60000)
            page.fill("#iv-title", "Partial match review")
            page.click("#iv-create")
            page.wait_for_function("() => /Linked evidence/.test(document.getElementById('iv-detail').textContent)",
                                   timeout=60000)
            invs = api(base, token, "GET", f"/api/investigations?case_id={case_id}")["items"]
            iv = next(x for x in invs if x["title"] == "Partial match review")
            ivid = iv["investigation_id"]
            xss = '<img src=x onerror="window.__xss=1"> remittance advice checked'
            page.fill("#iv-note", xss)
            page.click("#iv-note-add")
            page.wait_for_function("() => /remittance advice checked/.test(document.getElementById('iv-notes').textContent)",
                                   timeout=60000)
            note_ok = page.evaluate(
                "() => ({ text: document.getElementById('iv-notes').textContent.includes('<img src=x'),"
                " imgs: document.querySelectorAll('#iv-notes img').length, xss: window.__xss === undefined })"
            )
            ivlinks = api(base, token, "GET", f"/api/investigations/{ivid}/links")["items"]
            check(
                "A13",
                "An investigation opened from a reconciliation finding links it; a hostile note renders as text",
                len(ivlinks) == 1 and ivlinks[0]["link_type"] == "reconciliation_result"
                and note_ok == {"text": True, "imgs": 0, "xss": True},
                json.dumps(note_ok),
            )
            page.click("#iv-detail >> text=→ in review")
            page.click("#iv-t-confirm")
            page.wait_for_function("() => /in review/.test(document.getElementById('iv-detail').textContent)",
                                   timeout=60000)
            page.click("#iv-links >> text=Decide")
            page.select_option("#iv-d-action", "further_investigation_required")
            page.fill("#iv-d-rationale", "Bank fee needs the remittance advice.")
            page.click("#iv-d-save")
            page.wait_for_function("() => /Bank fee needs the remittance advice/.test("
                                   "document.getElementById('iv-detail').textContent)", timeout=60000)
            state = api(base, token, "GET", f"/api/investigations/{ivid}")
            decs = api(base, token, "GET", f"/api/investigations/{ivid}/decisions")["items"]
            acts = [e["event"] for e in api(base, token, "GET", f"/api/investigations/{ivid}/activity")["items"]]
            ver = api(base, token, "GET", "/api/audit/verify")
            offered = page.evaluate("() => [...document.querySelectorAll('#iv-detail button')].map(b => b.textContent)"
                                    ".filter(t => t.startsWith('→'))")
            check(
                "A13",
                "Status and decision changes go through the API, only valid transitions are offered, audit verifies",
                state["status"] == "in_review" and len(decs) == 1 and decs[0]["unresolved"]
                and acts == ["created", "linked", "note_added", "status_changed", "decision_recorded"]
                and sorted(offered) == sorted("→ " + t.replace("_", " ") for t in state["allowed_transitions"])
                and ver["ok"],
                json.dumps({"status": state["status"], "events": acts, "offered": offered}),
            )
            # ---------------- A14 (P16): reports from the reports API ----------------
            page.click('button.tab-btn[data-tab="tab-rep"]')
            page.wait_for_selector("#rep-type", timeout=60000)
            page.select_option("#rep-case", case_id)
            page.wait_for_selector(f"#rep-inv-{ivid}", timeout=60000)
            page.check(f"#rep-inv-{ivid}")
            page.click("#rep-preview")
            page.wait_for_function(
                r"() => /PREVIEW \(not stored\)/.test(document.getElementById('rep-preview-body').textContent)",
                timeout=120000,
            )
            pv = api(base, token, "POST", "/api/reports/preview",
                     {"report_type": "case_investigation", "investigation_ids": [ivid]})["content"]
            shown = page.evaluate(
                "() => ({ text: document.getElementById('rep-preview-body').textContent,"
                " imgs: document.querySelectorAll('#rep-preview-body img').length, xss: window.__xss === undefined })"
            )
            check(
                "A14",
                "A case-report preview shows the API's content; findings and analyst records apart; a hostile note "
                "stays text",
                pv["content_sha256"] in shown["text"]
                and "Automated findings (indicators" in shown["text"]
                and "Analyst assessments (recorded decisions)" in shown["text"]
                and "Bank fee needs the remittance advice." in shown["text"]
                and "<img src=x" in shown["text"]
                and shown["imgs"] == 0
                and shown["xss"],
                json.dumps({"sha": pv["content_sha256"][:16], "imgs": shown["imgs"]}),
            )
            page.select_option("#rep-type", "audit_memo")
            page.wait_for_selector(f"#rep-inv-{ivid}", timeout=60000)
            if not page.is_checked(f"#rep-inv-{ivid}"):
                page.check(f"#rep-inv-{ivid}")
            for _ in range(2):  # generate twice: two versions, the first one kept
                page.click("#rep-generate")
                page.wait_for_function(
                    "() => /Stored audit memo version/.test(document.getElementById('rep-msg').textContent)",
                    timeout=120000,
                )
                msg = page.evaluate("() => document.getElementById('rep-msg').textContent")
                page.evaluate("() => { document.getElementById('rep-msg').textContent = ''; }")
            memos = api(base, token, "GET", f"/api/reports?case_id={case_id}&report_type=audit_memo")["items"]
            latest = memos[0]
            dl = page.evaluate(
                "async (id) => { const b = await LS_API.blob('/api/reports/' + id + '/download?format=html');"
                " const buf = await crypto.subtle.digest('SHA-256', await b.arrayBuffer());"
                " return { text: await b.text(), sha: [...new Uint8Array(buf)].map(x => x.toString(16).padStart(2, '0')).join('') }; }",
                latest["report_id"],
            )
            listed = page.evaluate("() => document.getElementById('rep-list').textContent")
            check(
                "A14",
                "A stored draft memo is versioned, listed, and downloads as escaped HTML matching its recorded hash",
                len(memos) == 2
                and [m["version"] for m in memos] == [2, 1]
                and latest["status"] == "draft"
                and "version 2" in msg
                and latest["report_id"] in listed
                and dl["sha"] == latest["artifacts"]["html"]["sha256"]
                and "DRAFT" in dl["text"]
                and "<img src=x" not in dl["text"]
                and "&lt;img src=x" in dl["text"]
                and "default-src 'none'" in dl["text"],
                json.dumps({"versions": [m["version"] for m in memos], "sha_ok": dl["sha"] == latest["artifacts"]["html"]["sha256"]}),
            )
            heights = {}
            for tab in ["tab-ov", "tab-di", "tab-fi", "tab-bsr", "tab-tr", "tab-an", "tab-re", "tab-ex", "tab-rep"]:
                page.click(f'button.tab-btn[data-tab="{tab}"]')
                page.wait_for_timeout(120)
                heights[tab] = page.evaluate(
                    f"() => document.getElementById('{tab}').getBoundingClientRect().height"
                )
                if shots:
                    page.locator("#analysis").screenshot(path=str(shots / f"03-{tab}.png"))
            flow = {
                "filter_high": filt_ok,
                "search": [pill, api_total],
                "chips": len(answers),
                "why_top": top in why,
            }
            check(
                "A7",
                "Tier filter, search, pagination, chatbot and every tab work on API data",
                filt_ok
                and pill.startswith(f"{api_total:,} match")
                and len(answers) == 4
                and top in why
                and all(h > 100 for h in heights.values()),
                json.dumps(flow | {"heights": heights}),
            )
            if shots:
                page.click('button.tab-btn[data-tab="tab-ov"]')
                page.screenshot(path=str(shots / "02-hero.png"))
                page.locator("#insights").screenshot(path=str(shots / "04-insights.png"))

            csp = page.evaluate("window.__csp")
            external = [u for u in requests if not u.startswith(base)]
            check(
                "A4",
                "Only loopback requests and no CSP violations",
                not external and not csp,
                f"{len(requests)} requests, external {external[:2]}, csp {csp}",
            )
            ctx.close()
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        remove_workspace(ws.parent)

    check(
        "A9",
        "No JavaScript errors and no dialogs during the whole run",
        not errors and not dialogs,
        f"errors: {errors[:3]}, dialogs: {dialogs}",
    )
    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed")
    out = ROOT / "tests" / "frontend" / "out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "browser-results.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
