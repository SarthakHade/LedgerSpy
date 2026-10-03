"""
LedgerSpy P1 browser check (optional dev tool).

Drives the real page in headless Chromium and verifies the browser-level
P1 acceptance criteria:
  A3  malicious CSV content renders as text (no dialogs, no injected elements)
  A4  zero non-local requests; page renders with all network blocked
  A5  displayed numbers match computed results; unavailable modules say so
  A6  table shows ROW-n IDs (no TX-???)
  A7  upload → scan → filter → search → expand → add row → re-scan → export → chatbot
  A8  heuristic banner shows the flag rate

Requires: pip install playwright  (Chromium must be available; no download at runtime)
Usage (from repo root):  python tests/frontend/browser_check.py [--screenshots DIR]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
PAGE = (ROOT / "frontend" / "index.html").as_uri()
PAYSIM = ROOT / "data" / "raw" / "paysim_sample_10k.csv"
MALICIOUS = ROOT / "tests" / "frontend" / "fixtures" / "malicious.csv"

results: list[dict] = []


def check(cid: str, name: str, cond: bool, detail: str = "") -> None:
    results.append({"id": cid, "name": name, "ok": bool(cond), "detail": detail})
    print(f"  {'PASS' if cond else 'FAIL'}  [{cid}] {name}{' — ' + detail if detail else ''}")


def scan(page, csv_path: Path) -> None:
    page.set_input_files("#file-input", str(csv_path))
    page.wait_for_function("() => /rows detected/.test(document.getElementById('main-status-text').textContent)")
    page.click("#run-btn")
    page.wait_for_function("() => /Scan Complete/.test(document.getElementById('run-btn').textContent)", timeout=60000)


def main() -> int:
    shots = None
    if "--screenshots" in sys.argv:
        shots = Path(sys.argv[sys.argv.index("--screenshots") + 1])
        shots.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()

        # ---------------- A4: network ----------------
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, accept_downloads=True)
        page = ctx.new_page()
        requests: list[str] = []
        page.on("request", lambda r: requests.append(r.url))
        dialogs: list[str] = []
        page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        page.goto(PAGE)
        page.wait_for_load_state("load")
        external = [u for u in requests if not (u.startswith("file://") or u.startswith("data:") or u.startswith("blob:"))]
        check("A4", "Page load makes zero non-local requests", not external, f"{len(requests)} requests, external: {external}")

        # ---------------- A3: malicious CSV ----------------
        scan(page, MALICIOUS)
        for tab in ["tab-ov", "tab-di", "tab-bsr", "tab-tr", "tab-an", "tab-re", "tab-ex"]:
            page.click(f"button.tab-btn[onclick*=\"{tab}\"]")
            page.wait_for_timeout(150)
        page.click("#tx-body tr.main-row")  # expand first row
        page.click("#ai-mascot")
        for q in ["High risk transactions", "Top anomalies", "why is INV-0001 flagged?", "suspicious", "show summary"]:
            page.fill("#chat-input", q)
            page.press("#chat-input", "Enter")
            page.wait_for_timeout(600)
        page.click("#ai-mascot")
        injected = page.evaluate("""() => ({
            img: document.querySelectorAll('img[src="x"]').length,
            script: [...document.scripts].filter(s => /alert\\(/.test(s.textContent)).length,
            svg: document.querySelectorAll('svg[onload]').length,
            bold: [...document.querySelectorAll('b')].filter(b => b.textContent === 'bold sender').length,
            textShown: document.body.innerText.includes("<img src=x onerror=alert('vendor')>"),
        })""")
        check("A3", "No dialogs fired from malicious CSV content", not dialogs, f"dialogs: {dialogs}")
        check("A3", "No elements injected from CSV values",
              injected["img"] == 0 and injected["script"] == 0 and injected["svg"] == 0 and injected["bold"] == 0, json.dumps(injected))
        check("A3", "Malicious values are displayed as literal text", injected["textShown"])
        ctx.close()

        # ---------------- A4b: offline rendering ----------------
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, offline=True)
        page = ctx.new_page()
        page.route("**/*", lambda route: route.continue_() if route.request.url.startswith("file://") else route.abort())
        page.goto(PAGE)
        ok = page.evaluate("() => !!document.getElementById('upload-zone') && getComputedStyle(document.body).backgroundColor !== ''")
        css_loaded = page.evaluate("() => getComputedStyle(document.documentElement).getPropertyValue('--amber').trim() === '#f59e0b'")
        check("A4", "Page renders with network blocked (CSS + scripts are local)", ok and css_loaded)
        ctx.close()

        # ---------------- A5 / A6 / A7 / A8 on the PaySim sample ----------------
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, accept_downloads=True)
        page = ctx.new_page()
        page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(PAGE)
        if shots:
            page.screenshot(path=str(shots / "01-before-scan.png"), full_page=False)
        scan(page, PAYSIM)
        s = page.evaluate("() => window._mlSummary")

        # Donut
        donut = page.evaluate("""() => ['donut-high','donut-susp','donut-safe'].map(id => {
            const c = document.getElementById(id);
            return { n: +c.getAttribute('data-count'), len: parseFloat(c.getAttribute('stroke-dasharray')) };
        })""")
        C = 2 * 3.141592653589793 * 80
        donut_ok = [d["n"] for d in donut] == [s["high"], s["susp"], s["safe"]] and all(
            abs(d["len"] - C * d["n"] / s["total"]) < 0.01 for d in donut)
        check("A5", "Donut segments equal tier counts", donut_ok, json.dumps(donut))

        texts = page.evaluate("""() => Object.fromEntries(['hero-n-high','hero-n-susp','hero-n-safe','fg-n','qs-avg-high','qs-high-amt',
            'qs-flag-rate','qs-payees','exp-val','tier-high-txt','rel-stat-payees','rel-stat-recip','insights-total-fraud','insights-vendors']
            .map(id => [id, document.getElementById(id).textContent.trim()]))""")
        check("A5", "Hero / overview / insights values are computed (no placeholders)",
              texts["hero-n-high"] == f"{s['high']:,}" and texts["fg-n"] == str(s["avgRisk"]) and "—" not in "".join(texts.values()),
              json.dumps(texts, ensure_ascii=False))
        na = page.evaluate("""() => ({
            recon: !!document.querySelector('#tab-bsr .na-card'),
            montecarlo: [...document.querySelectorAll('#tab-tr .na-card')].some(n => /going-concern/.test(n.textContent)),
            benchmark: [...document.querySelectorAll('#tab-ov .na-card')].some(n => /benchmark/i.test(n.textContent)),
            graph: [...document.querySelectorAll('#tab-re .na-card')].some(n => /Entity graph not available/.test(n.textContent)),
            benfordBars: document.querySelectorAll('#benford-bars > div').length,
            exBars: document.querySelectorAll('#ex-bars .fac-row').length,
            memoHasFile: document.getElementById('print-doc').innerText.includes('paysim_sample_10k.csv'),
            memoHasCounts: document.getElementById('print-doc').innerText.includes(window._mlSummary.high.toLocaleString() + ' rows'),
        })""")
        check("A5", "Unavailable modules show explicit 'Not available' states",
              na["recon"] and na["montecarlo"] and na["benchmark"] and na["graph"], json.dumps(na))
        check("A5", "Benford chart, rule summary and memo are rendered from data",
              na["benfordBars"] == 9 and na["exBars"] > 0 and na["memoHasFile"] and na["memoHasCounts"], json.dumps(na))

        ids = page.evaluate("() => [...document.querySelectorAll('#tx-body .tx-id')].map(n => n.textContent)")
        check("A6", "Table shows ROW-n IDs, no TX-???",
              ids and all(i.startswith("ROW-") for i in ids) and not any("???" in i for i in ids), f"{len(ids)} rendered, first {ids[:3]}")

        notice = page.evaluate("() => { const n = document.getElementById('heuristic-notice'); return { shown: getComputedStyle(n).display !== 'none', text: n.textContent } }")
        check("A8", "Heuristic banner visible with flag rate and calibration warning",
              notice["shown"] and f"{s['flaggedPct']:.1f}%" in notice["text"] and "NOT calibrated" in notice["text"], notice["text"].strip())

        heights = {}
        for tab in ["tab-ov", "tab-di", "tab-bsr", "tab-tr", "tab-an", "tab-re", "tab-ex"]:
            page.click(f"button.tab-btn[onclick*=\"{tab}\"]")
            page.wait_for_timeout(120)
            heights[tab] = page.evaluate(f"() => document.getElementById('{tab}').getBoundingClientRect().height")
        check("A7", "Every analysis tab shows visible content when selected", all(h > 100 for h in heights.values()), json.dumps(heights))
        page.click("button.tab-btn[onclick*=\"tab-ov\"]")

        if shots:
            page.screenshot(path=str(shots / "02-hero-after-scan.png"))
            for tab in ["tab-ov", "tab-di", "tab-bsr", "tab-tr", "tab-an", "tab-re", "tab-ex"]:
                page.click(f"button.tab-btn[onclick*=\"{tab}\"]")
                page.wait_for_timeout(300)
                page.locator("#analysis").screenshot(path=str(shots / f"03-{tab}.png"))
            page.locator("#insights").screenshot(path=str(shots / "04-insights.png"))

        # ---- A7 regression flow ----
        page.click("button.t-filter[onclick*=\"'high'\"]")
        vis = page.evaluate("() => [...document.querySelectorAll('#tx-body tr.main-row')].filter(r => r.style.display !== 'none').map(r => r.dataset.r)")
        filt_ok = len(vis) > 0 and all(v == "high" for v in vis)
        page.click("button.t-filter[onclick*=\"'all'\"]")
        page.fill("#search-in", "ROW-12")
        page.dispatch_event("#search-in", "input")
        pill = page.text_content("#search-count-pill")
        page.fill("#search-in", "")
        page.dispatch_event("#search-in", "input")
        page.click("#tx-body tr.main-row >> nth=0")
        expanded = page.evaluate("() => document.getElementById('det-0').style.display === 'table-row'")
        detail = page.text_content("#det-0")

        page.click("#open-add-tx")
        page.fill("#ltf-2", "TRANSFER")
        page.fill("#ltf-4", "123456")
        page.fill("#ltf-5", "C_TEST_SENDER")
        page.fill("#ltf-8", "C_TEST_PAYEE")
        page.click("button[onclick='ltAddRow()']")
        first = page.evaluate("() => ({ id: document.querySelector('#tx-body .tx-id').textContent, badge: document.querySelector('#tx-body .badge').textContent })")
        rerun_label = page.text_content("#run-btn")
        page.click("#run-btn")
        page.wait_for_function("() => /Scan Complete/.test(document.getElementById('run-btn').textContent)", timeout=60000)
        rescored = page.evaluate("() => { const d = tableData.find(x => x.id === 'NEW-10128'); return d ? { r: d.r, score: d._mlScore } : null }")

        page.click("#open-add-tx")
        with page.expect_download() as dl:
            page.click("button[onclick='ltDownloadCsv()']")
        export_lines = Path(dl.value.path()).read_text(encoding="utf-8").strip().split("\n")
        page.click("button[onclick='closeAddTx()']")

        page.click("#ai-mascot")
        answers = []
        for chip in page.query_selector_all(".chat-sug"):
            chip.click()
            page.wait_for_timeout(600)
            answers.append(page.evaluate("() => document.querySelector('#chat-messages .chat-msg.bot:last-child').innerText.slice(0, 60)"))
        page.fill("#chat-input", "why is ROW-123 flagged?")
        page.press("#chat-input", "Enter")
        page.wait_for_timeout(600)
        why = page.evaluate("() => document.querySelector('#chat-messages .chat-msg.bot:last-child').innerText")
        memo_btn = page.evaluate("() => !document.getElementById('memo-copy-btn').disabled")

        flow = {
            "filter_high_only": filt_ok, "search_pill": pill, "row_expanded": expanded,
            "detail_has_breakdown": "pts" in (detail or ""), "added_row": first, "rerun_label": rerun_label,
            "rescored_new_row": rescored, "export_lines": len(export_lines), "chat_answers": len(answers),
            "why_row_123": "ROW-123" in why, "memo_copy_enabled": memo_btn,
        }
        check("A7", "Regression flow: filter, search, expand, add row, re-scan, export, chatbot, memo",
              filt_ok and pill and expanded and "pts" in detail and first["id"] == "NEW-10128" and "NOT SCORED" in first["badge"]
              and "Re-run" in rerun_label and rescored and rescored["r"] in ("high", "suspicious", "safe")
              and len(export_lines) == 10129 and len(answers) == 4 and "ROW-123" in why and memo_btn,
              json.dumps(flow))
        if shots:
            page.screenshot(path=str(shots / "05-chatbot.png"))
        ctx.close()
        browser.close()

    check("A7", "No JavaScript errors and no dialogs during the whole run", not errors and not dialogs, f"errors: {errors[:3]}, dialogs: {dialogs}")

    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed")
    out = ROOT / "tests" / "frontend" / "out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "browser-results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
