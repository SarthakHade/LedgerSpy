"""
LedgerSpy P4 browser check for the backend Data Integrity panel (optional dev tool, like browser_check.py).

Starts `ledgerspy serve` on a temporary workspace, imports a fixture through the API, then drives the real page:
  D1  every number in the panel equals the API integrity report (no value computed in the browser)
  D2  a rejected fix stays rejected: "Accept all safe fixes" only touches undecided fixes
  D3  Auto-Fix applies accepted fixes as logged transformations; the panel re-reads the new report
  D4  revert from the transformation log restores the original value
  D5  no CSP violations, no JS errors, only loopback requests
  D6  opened as a plain file, the panel says the backend is required (no numbers shown)

Requires: playwright + Chromium (see browser_check.py). Usage: python tests/frontend/integrity_panel_check.py
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
sys.path.insert(0, str(ROOT))
from tests.backend.conftest import explicit_mapping  # noqa: E402

FIXTURE = ROOT / "tests" / "backend" / "fixtures" / "quality" / "normalization.csv"
results: list[dict] = []


def check(cid: str, name: str, cond: bool, detail: str = "") -> None:
    results.append({"id": cid, "name": name, "ok": bool(cond), "detail": detail})
    print(f"  {'PASS' if cond else 'FAIL'}  [{cid}] {name}{' - ' + detail if detail else ''}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def api(base: str, token: str, method: str, path: str, body: bytes | dict | None = None, raw: bool = False):
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if body is not None:
        data = body if raw else json.dumps(body).encode()
        headers["Content-Type"] = "application/octet-stream" if raw else "application/json"
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)  # noqa: S310 — loopback
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
        return json.loads(r.read())


def main() -> int:
    port = free_port()
    ws = Path(tempfile.mkdtemp()) / "ws"
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "ledgerspy", "serve", "--no-browser", "--port", str(port), "--workspace", str(ws)],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        url = None
        while url is None:
            line = proc.stdout.readline()
            m = re.search(r"Open: (http://127\.0\.0\.1:\d+/\?token=(\S+))", line)
            if m:
                url, token = m.group(1), m.group(2)
        base = f"http://127.0.0.1:{port}"
        for _ in range(50):
            try:
                urllib.request.urlopen(base + "/api/health", timeout=2)  # noqa: S310
                break
            except OSError:
                time.sleep(0.1)
        case = api(base, token, "POST", "/api/cases", {"name": "Panel check"})
        ds = api(base, token, "POST", f"/api/cases/{case['case_id']}/files?filename=normalization.csv",
                 FIXTURE.read_bytes(), raw=True)
        api(base, token, "PUT", f"/api/datasets/{ds['dataset_id']}/mapping", explicit_mapping(ds))
        report = api(base, token, "POST", f"/api/datasets/{ds['dataset_id']}/integrity")

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            requests, errors = [], []
            page.on("request", lambda r: requests.append(r.url))
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.add_init_script("window.__csp=[];document.addEventListener('securitypolicyviolation',"
                                 "e=>window.__csp.push(e.violatedDirective));")
            page.goto(url)
            page.click("button.tab-btn[onclick*=\"tab-di\"]")
            page.wait_for_selector("#di-backend-score")

            shown = page.evaluate("""() => {
              const b = document.getElementById('di-backend-body');
              const tiles = [...b.querySelectorAll('.di-tiles')[0].querySelectorAll('.di-tile')]
                .map(t => [t.querySelector('.di-tile-l').textContent, t.querySelector('.di-tile-v').textContent]);
              const comps = [...[...b.querySelectorAll('.di-section')].find(s => s.querySelector('h4').textContent
                .startsWith('Readiness components')).querySelectorAll('tbody tr')].map(tr => [...tr.children].map(td => td.textContent));
              return { score: document.getElementById('di-backend-score').textContent, tiles: Object.fromEntries(tiles), comps };
            }""")
            s = report["summary"]
            expected_tiles = {
                "Total rows": str(s["total_rows"]), "Clean rows": str(s["clean_rows"]),
                "Rows with issues": str(s["rows_with_issues"]), "Rows with INFO only": str(s["rows_with_info_only"]),
                **{f"{k.upper()} issues": str(v) for k, v in s["issues_by_severity"].items()},
            }
            comps_ok = all(row[0] == c["component"] and row[3] == str(c["affected_rows"])
                           for row, c in zip(shown["comps"], report["readiness"]["components"], strict=True))
            check("D1", "Panel numbers equal the API integrity report",
                  shown["score"] == f"{report['readiness']['score']:.1f}%" and shown["tiles"] == expected_tiles and comps_ok,
                  json.dumps(shown["tiles"]))

            def fix_row(field: str):
                return page.locator("#di-backend-body .di-section", has_text="Proposed fixes").locator(
                    "tbody tr", has=page.locator("td:nth-child(2)", has_text=re.compile(f"^{field}$"))).first

            row = fix_row("payee_raw")
            row.locator("input.di-reason").fill("keep source spacing")
            row.get_by_role("button", name="Reject").click()
            page.wait_for_function("() => /rejected/.test(document.getElementById('di-backend-body').textContent)")
            page.locator("#di-backend-body").get_by_role("button", name=re.compile("^Accept all")).click()
            page.wait_for_function("() => !/Accept all/.test(document.getElementById('di-backend-body').textContent)")
            fixes = api(base, token, "GET", f"/api/datasets/{ds['dataset_id']}/fixes")["items"]
            status = {f["field"] + str(f["source_row"]): f["status"] for f in fixes}
            check("D2", "Bulk accept does not override a rejection",
                  status["payee_raw1"] == "rejected" and sum(v == "accepted" for v in status.values()) == 5, json.dumps(status))

            page.locator("#di-backend-body").get_by_role("button", name=re.compile("Auto-Fix")).click()
            page.wait_for_function("() => /Applied 5 transformation/.test(document.getElementById('di-backend-body').textContent)")
            latest = api(base, token, "GET", f"/api/datasets/{ds['dataset_id']}/integrity")
            log = api(base, token, "GET", f"/api/datasets/{ds['dataset_id']}/transformations")["items"]
            score = page.text_content("#di-backend-score")
            check("D3", "Auto-Fix applies accepted fixes as logged transformations and re-reads the report",
                  len(log) == 5 and score == f"{latest['readiness']['score']:.1f}%" and latest["run_id"] != report["run_id"],
                  f"{len(log)} transformations, panel {score}")

            ref = page.locator("#di-backend-body .di-section", has_text="Transformation log").locator(
                "tbody tr", has=page.locator("td:nth-child(4)", has_text="reference")).first
            ref.locator("input.di-reason").fill("check revert")
            ref.get_by_role("button", name="Revert").click()
            page.wait_for_function("() => /revert:identifier_normalize/.test(document.getElementById('di-backend-body').textContent)")
            txns = api(base, token, "GET", f"/api/datasets/{ds['dataset_id']}/transactions")["items"]
            check("D4", "Revert restores the original value", txns[2]["reference"] == "RCPT​-0077", txns[2]["reference"])

            csp = page.evaluate("window.__csp")
            external = [u for u in requests if not u.startswith(base)]
            check("D5", "No CSP violations, JS errors or non-loopback requests", not csp and not errors and not external,
                  f"csp={csp} errors={errors[:2]} external={external[:2]}")

            plain = browser.new_page()
            plain.goto((ROOT / "frontend" / "index.html").as_uri())
            text = plain.text_content("#di-backend-body")
            check("D6", "Opened as a file, the panel shows no numbers and names the requirement",
                  "ledgerspy serve" in text and not re.search(r"\d+(\.\d)?%", text), text[:80])
            browser.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    failed = sum(not r["ok"] for r in results)
    print(f"\n{len(results) - failed} passed, {failed} failed")
    out = ROOT / "tests" / "frontend" / "out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "integrity-panel-results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
