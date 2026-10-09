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


def main() -> int:
    shots = None
    if "--screenshots" in sys.argv:
        shots = Path(sys.argv[sys.argv.index("--screenshots") + 1])
        shots.mkdir(parents=True, exist_ok=True)
    port = free_port()
    ws = Path(tempfile.mkdtemp()) / "ws"
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
                page.click(f'button.tab-btn[onclick*="{tab}"]')
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
                recon: !!document.querySelector('#tab-bsr .na-card'),
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
            page.click('button.tab-btn[onclick*="tab-re"]')
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
            page.click('button.tab-btn[onclick*="tab-bsr"]')
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
                "A partial match shows its discrepancy and source rows; PDF/OCR is labelled as coming in P14b",
                "Not reconciled" in detail and "SYNTHETIC_bank_statement.csv row" in detail and "P14b" in p14b,
                detail[:90],
            )
            heights = {}
            for tab in ["tab-ov", "tab-di", "tab-fi", "tab-bsr", "tab-tr", "tab-an", "tab-re", "tab-ex"]:
                page.click(f'button.tab-btn[onclick*="{tab}"]')
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
                page.click('button.tab-btn[onclick*="tab-ov"]')
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
