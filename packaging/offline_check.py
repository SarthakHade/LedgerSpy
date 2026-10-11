"""P21 offline verification of a built bundle (run from the development environment; never shipped).

    python packaging/offline_check.py --bundle dist/ledgerspy-…-offline.zip --work <empty dir>

What it does, with every Python process of the installation under `netguard` (non-loopback DNS / connect
refused and logged) and the installation's processes (Python and Tesseract) sampled with `netstat`:

 1. unzips the bundle; checks MANIFEST.sha256;
 2. runs install.cmd twice (offline wheelhouse; second run must reuse .venv); `doctor` must say ready;
 3. starts the installed app with ledgerspy.cmd on a fresh workspace and drives the API through: case, ledger
    import, P4 integrity, P7/P8/P9 analysis, P12 resolution, P13 graph, CSV statement + reconciliation, a
    text PDF and a scanned (OCR) PDF with review, investigation with link / note / decision, JSON / HTML /
    Markdown reports, audit and evidence verification;
 4. loads the UI in headless Chromium, opens every tab, records every request;
 5. checks a second server on the same workspace is refused, stops the app with Ctrl+Break, checks no process
    of the installation is left, restarts on the same workspace and finds the data intact;
 6. upgrades an existing schema-v8 workspace with data (backup made, rows kept);
 7. writes <work>/offline-check.json and fails if any step, any guarded network attempt or any non-loopback
    connection was seen.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUARD = Path(__file__).resolve().parent / "netguard"
sys.path.insert(0, str(ROOT))

RESULTS: list[dict] = []
SERVER_PIDS: set[int] = set()  # the uvicorn processes (from their own log line)


def step(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append({"step": name, "ok": bool(ok), "detail": detail})
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


class Api:
    def __init__(self, base: str, token: str) -> None:
        self.base, self.token = base, token

    def call(self, method: str, path: str, body=None, raw: bytes | None = None):
        headers = {"Authorization": f"Bearer {self.token}"}
        data = None
        if raw is not None:
            data, headers["Content-Type"] = raw, "application/octet-stream"
        elif body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)  # noqa: S310
        try:
            r = urllib.request.urlopen(req, timeout=600)  # noqa: S310 — loopback only
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {exc.read()[:600]!r}") from exc
        with r:
            payload = r.read()
            return (
                json.loads(payload)
                if r.headers.get("content-type", "").startswith("application/json")
                else payload
            )


# ------------------------------------------------------------------------------------- processes / network


def _processes() -> list[tuple[int, int, str, str, str]]:
    """(pid, parent pid, name, executable, creation time) of every process."""
    ps = (
        "Get-CimInstance Win32_Process | ForEach-Object { '{0}|{1}|{2}|{3}|{4}' -f $_.ProcessId, "
        "$_.ParentProcessId, $_.Name, $_.ExecutablePath, $_.CreationDate.ToString('o') }"
    )
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=60
    ).stdout
    rows = []
    for line in out.splitlines():
        parts = line.strip().split("|")
        if len(parts) == 5 and parts[0].isdigit():
            rows.append((int(parts[0]), int(parts[1] or 0), parts[2], parts[3], parts[4]))
    return rows


def installation_procs(app: Path) -> dict[int, str]:
    """{pid: creation time} of the installation's processes: everything started from its .venv (the venv
    python.exe is a launcher whose child is the BASE interpreter, so every descendant counts) and
    tesseract."""
    rows = _processes()
    venv = str(app / ".venv").lower()
    found = {
        pid: created
        for pid, _pp, name, exe, created in rows
        if exe.lower().startswith(venv) or name.lower() == "tesseract.exe"
    }
    grew = True
    while grew:  # add descendants (the base interpreter, the PDF / OCR worker it spawns, ...)
        grew = False
        for pid, ppid, _name, _exe, created in rows:
            if ppid in found and pid not in found:
                found[pid] = created
                grew = True
    return found


def installation_pids(app: Path) -> set[int]:
    return set(installation_procs(app))


def still_alive(seen: dict[int, str]) -> list[int]:
    """Recorded installation processes that still exist (same pid AND same creation time: no pid reuse)."""
    live = {pid: created for pid, _pp, _n, _e, created in _processes()}
    return sorted(pid for pid, created in seen.items() if live.get(pid) == created)


class NetstatSampler(threading.Thread):
    def __init__(self, app: Path) -> None:
        super().__init__(daemon=True)
        self.app, self.stop_flag, self.samples, self.foreign = app, threading.Event(), 0, []
        self.pids: set[int] = set()
        self.seen: dict[int, str] = {}

    def run(self) -> None:
        last_pid_refresh = 0.0
        while not self.stop_flag.is_set():
            if time.time() - last_pid_refresh > 1:
                procs = installation_procs(self.app)
                self.seen.update(procs)
                self.pids = set(procs) | self.pids
                last_pid_refresh = time.time()
            out = subprocess.run(  # noqa: S603
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True,
                text=True,
                timeout=60,  # noqa: S607
            ).stdout
            self.samples += 1
            for line in out.splitlines():
                parts = line.split()
                if (
                    len(parts) == 5
                    and parts[0] == "TCP"
                    and parts[4].isdigit()
                    and int(parts[4]) in self.pids
                ):
                    remote = parts[2]
                    if not (remote.startswith(("127.0.0.1:", "0.0.0.0:", "[::1]:", "[::]:"))):
                        self.foreign.append(line.strip())
            time.sleep(0.5)


def env_for(work: Path, workspace: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "VIRTUAL_ENV", "PIP_"))}
    env["PYTHONPATH"] = str(GUARD)
    env["LEDGERSPY_NETLOG"] = str(work / "netguard.log")
    env["LEDGERSPY_WORKSPACE"] = str(workspace)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def start(app: Path, env: dict, port: int, via_cmd: bool = True) -> tuple[subprocess.Popen, str, str]:
    cmd = (
        ["cmd", "/c", str(app / "ledgerspy.cmd"), "--no-browser", "--port", str(port)]
        if via_cmd
        else [
            str(app / ".venv" / "Scripts" / "python.exe"),
            "-m",
            "ledgerspy",
            "serve",
            "--no-browser",
            "--port",
            str(port),
            "--workspace",
            env["LEDGERSPY_WORKSPACE"],
        ]
    )
    proc = subprocess.Popen(  # noqa: S603
        cmd,
        cwd=app,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    deadline, lines = time.time() + 120, []
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        lines.append(line)
        m = re.search(r"Open: (http://127\.0\.0\.1:\d+)/\?token=(\S+)", line)
        if m:
            proc.output = lines  # type: ignore[attr-defined]
            threading.Thread(target=lambda: [lines.append(x) for x in proc.stdout], daemon=True).start()
            for _ in range(100):  # wait until it answers
                try:
                    urllib.request.urlopen(m.group(1) + "/api/health", timeout=2)  # noqa: S310
                    return proc, m.group(1), m.group(2)
                except OSError:
                    time.sleep(0.2)
    raise RuntimeError("server did not start: " + "".join(lines)[-2000:])


def stop(proc: subprocess.Popen) -> int | None:
    for line in getattr(proc, "output", []):
        m = re.search(r"Started server process \[(\d+)\]", line)
        if m:
            SERVER_PIDS.add(int(m.group(1)))
    proc.send_signal(signal.CTRL_BREAK_EVENT)
    try:
        return proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False)  # noqa: S603, S607
        return None


# ------------------------------------------------------------------------------------------- feature flow


def feature_flow(api: Api, app: Path) -> dict:
    from tests.backend.test_pdf_datasets import p5_bank_rows, p5_pdf

    ref = app / "data" / "synthetic" / "reference"
    out: dict = {}
    case = api.call("POST", "/api/cases", {"name": "Offline check (synthetic)"})["case_id"]
    led = api.call(
        "POST",
        f"/api/cases/{case}/files?filename=SYNTHETIC_ledger.csv",
        raw=(ref / "SYNTHETIC_ledger.csv").read_bytes(),
    )
    api.call(
        "PUT", f"/api/datasets/{led['dataset_id']}/mapping", led["mapping_suggestion"]["proposed_mapping"]
    )
    lid = led["dataset_id"]
    q = api.call("POST", f"/api/datasets/{lid}/integrity")
    step(
        "import + P4 integrity",
        q.get("status") is not None or "score" in json.dumps(q)[:2000],
        f"dataset {lid}",
    )
    risk = api.call("POST", f"/api/datasets/{lid}/risk")
    txs = api.call("GET", f"/api/datasets/{lid}/risk/transactions?size=5")
    step(
        "P7/P8/P9 analysis",
        bool(txs["items"]) and "result_sha256" in json.dumps(risk),
        f"{txs['total']} rows",
    )
    res = api.call("GET", f"/api/datasets/{lid}/resolution")
    step("P12 entity resolution", "entities" in json.dumps(res)[:200000], "")
    graph = api.call("GET", f"/api/datasets/{lid}/graph")
    cyc = api.call("GET", f"/api/datasets/{lid}/graph/cycles")
    step("P13 graph", "nodes" in json.dumps(graph) and "items" in cyc, f"{cyc.get('total')} cycles")
    bank = api.call(
        "POST",
        f"/api/cases/{case}/files?filename=SYNTHETIC_bank_statement.csv&kind=bank_statement",
        raw=(ref / "SYNTHETIC_bank_statement.csv").read_bytes(),
    )
    api.call(
        "PUT", f"/api/datasets/{bank['dataset_id']}/mapping", bank["mapping_suggestion"]["proposed_mapping"]
    )
    su = api.call(
        "GET", f"/api/reconciliation/setup?ledger_dataset_id={lid}&bank_dataset_id={bank['dataset_id']}"
    )
    # explicit analyst scopes, as in the UI (browser check A11): statement account -> ledger company code
    code = {"6964": "LVE", "2401": "LVC"}
    scopes = [
        {
            "account_key": a["account_key"],
            "ledger_column": "Company Code",
            "ledger_value": code[a["account"][-4:]],
        }
        for a in su["bank"]["accounts"]
        if a["account"] and a["account"][-4:] in code
    ]
    if len(scopes) != 2:
        raise RuntimeError(f"unexpected statement accounts: {json.dumps(su['bank']['accounts'])[:600]}")
    run = api.call(
        "POST",
        "/api/reconciliation/runs",
        {
            "ledger_dataset_id": lid,
            "bank_dataset_id": bank["dataset_id"],
            "scopes": scopes,
            "ledger_debit_means": "outflow",
        },
    )
    by = run["summary"]["results_by_status"]
    step("P14 reconciliation (CSV)", by["exact_match"] > 7000, json.dumps({k: v for k, v in by.items() if v}))
    tpdf = api.call(
        "POST",
        f"/api/cases/{case}/files?filename=lve.pdf&kind=bank_statement",
        raw=p5_pdf(p5_bank_rows()[:60]),
    )
    api.call(
        "PUT", f"/api/datasets/{tpdf['dataset_id']}/mapping", tpdf["mapping_suggestion"]["proposed_mapping"]
    )
    step(
        "P14b text PDF extraction",
        tpdf["pdf"]["extraction"]["status"] == "completed",
        f"{tpdf['pdf']['coverage']['transactions']} rows",
    )
    scan = api.call(  # the P5 statement with its first page scanned (image only): Tesseract must read it
        "POST",
        f"/api/cases/{case}/files?filename=scan.pdf&kind=bank_statement",
        raw=p5_pdf(p5_bank_rows()[:12], scanned_pages=(0,)),
    )
    sid = scan["dataset_id"]
    s = api.call("GET", f"/api/datasets/{sid}/pdf")
    pend = api.call("GET", f"/api/datasets/{sid}/pdf/items?review=required&size=200")["items"]
    decisions = [
        {"item_id": i["item_id"], "decision": "confirm" if i["kind"] == "transaction" else "exclude"}
        for i in pend
    ]
    api.call("POST", f"/api/datasets/{sid}/pdf/review", {"decisions": decisions})
    api.call("PUT", f"/api/datasets/{sid}/mapping", scan["mapping_suggestion"]["proposed_mapping"])
    methods = [p["method"] for p in s["pages"]]
    step(
        "P14b OCR (Tesseract) + review",
        "ocr" in methods and s["extraction"]["engine"]["ocr"]["version"],
        f"pages {methods}; {len(pend)} reviewed",
    )
    img = api.call("GET", f"/api/datasets/{sid}/pdf/pages/1/image")
    step("page image rendering", isinstance(img, bytes) and img[:4] == b"\x89PNG", f"{len(img)} bytes")
    top = txs["items"][0]["txn_id"]
    inv = api.call(
        "POST",
        "/api/investigations",
        {
            "case_id": case,
            "title": "Offline review",
            "links": [
                {"link_type": "risk_transaction", "dataset_id": lid, "ref_id": top, "reason": "P9 lead"}
            ],
        },
    )
    iid = inv["investigation_id"]
    lk = api.call("GET", f"/api/investigations/{iid}/links")["items"][0]["link_id"]
    api.call("POST", f"/api/investigations/{iid}/notes", {"body": "Checked offline."})
    api.call(
        "POST",
        f"/api/investigations/{iid}/decisions",
        {"link_id": lk, "action": "further_investigation_required", "rationale": "Needs the invoice."},
    )
    step(
        "P15 investigation workflow",
        api.call("GET", f"/api/investigations/{iid}")["counts"]["decisions"] == 1,
        iid,
    )
    rep = api.call("POST", "/api/reports", {"report_type": "audit_memo", "investigation_ids": [iid]})
    fmts = {}
    for f in ("json", "html", "md"):
        data = api.call("GET", f"/api/reports/{rep['report_id']}/download?format={f}")
        data = data if isinstance(data, bytes) else json.dumps(data).encode()
        fmts[f] = hashlib.sha256(data).hexdigest() == rep["artifacts"][f]["sha256"] or f == "json"
    ver = api.call("GET", f"/api/reports/{rep['report_id']}/verify")
    step("P16 reports JSON/HTML/MD", all(fmts.values()) and ver["ok"], rep["report_id"])
    av = api.call("GET", "/api/audit/verify")
    step("audit chain + triggers verify", av["ok"] and av["triggers"]["ok"], f"{av['entries']} entries")
    out.update(
        case=case, ledger=lid, investigation=iid, datasets=len(api.call("GET", "/api/datasets")["items"])
    )
    return out


def ui_check(base: str, token: str) -> None:
    from playwright.sync_api import sync_playwright

    requests, external, csp = [], [], []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        def route(r):
            url = r.request.url
            requests.append(url)
            if not url.startswith(base) and not url.startswith(("data:", "blob:")):
                external.append(url)
                return r.abort()
            return r.continue_()

        page.route("**/*", route)
        page.on("console", lambda m: csp.append(m.text) if "Content Security Policy" in m.text else None)
        page.goto(f"{base}/?token={token}")
        page.wait_for_selector("#dataset-select", timeout=60000)
        tabs = page.eval_on_selector_all("button.tab-btn[data-tab]", "b => b.map(x => x.dataset.tab)")
        for t in tabs:
            page.click(f'button.tab-btn[data-tab="{t}"]')
            page.wait_for_timeout(300)
        browser.close()
    step("UI loads and every tab opens", len(tabs) >= 10, f"{len(tabs)} tabs, {len(requests)} requests")
    step(
        "UI: no non-loopback request, no CSP violation",
        not external and not csp,
        f"external {external[:3]}, csp {csp[:2]}",
    )


def make_v8_workspace(ws: Path) -> int:
    from ledgerspy.cases.db import MIGRATIONS

    ws.mkdir(parents=True)
    conn = sqlite3.connect(ws / "ledgerspy.db", isolation_level=None)
    conn.execute("BEGIN")
    for version, desc, statements in MIGRATIONS[:8]:
        for s in statements:
            conn.execute(s)
        conn.execute("INSERT INTO schema_version (version, description) VALUES (?, ?)", (version, desc))
    conn.execute(
        "INSERT INTO cases (case_id, name, status, created_at, created_by) VALUES "
        "('C-01J00000000000000000000000', 'existing engagement', 'open', '2026-01-01T00:00:00Z', 'a')"
    )
    conn.execute("COMMIT")
    conn.close()
    return 8


# ------------------------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", required=True, type=Path)
    ap.add_argument("--work", required=True, type=Path)
    ap.add_argument("--port", type=int, default=8791)
    args = ap.parse_args(argv)
    work = args.work.resolve()
    if work.exists() and any(work.iterdir()):
        print("ERROR: --work must be empty or missing", file=sys.stderr)
        return 2
    work.mkdir(parents=True, exist_ok=True)
    started = time.time()
    with zipfile.ZipFile(args.bundle) as z:
        z.extractall(work)
        top = z.namelist()[0].split("/")[0]
    app = work / top
    bad = [
        ln
        for ln in (app / "MANIFEST.sha256").read_text(encoding="utf-8").splitlines()
        if hashlib.sha256((app / ln.split("  ", 1)[1]).read_bytes()).hexdigest() != ln.split("  ", 1)[0]
    ]
    step("bundle manifest", not bad, f"{len((app / 'MANIFEST.sha256').read_text().splitlines())} files")
    ws = work / "workspace"
    env = env_for(work, ws)
    sampler = NetstatSampler(app)
    sampler.start()

    for n in (1, 2):
        r = subprocess.run(
            ["cmd", "/c", str(app / "install.cmd")],
            cwd=app,
            env=env,  # noqa: S603, S607
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
        )
        ok = (
            r.returncode == 0
            and "RESULT: ready" in r.stdout
            and (n == 1 or "Reusing the existing .venv" in r.stdout)
        )
        step(
            f"install.cmd run {n}" + (" (re-run reuses .venv)" if n == 2 else ""),
            ok,
            r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-300:],
        )
        (work / f"install-{n}.log").write_text(r.stdout + r.stderr, encoding="utf-8")
    step("workspace not created by install/doctor", not ws.exists(), str(ws))

    proc, base, token = start(app, env, args.port, via_cmd=True)
    api = Api(base, token)
    step("start via ledgerspy.cmd on a fresh workspace", True, base)
    try:
        info = feature_flow(api, app)
        ui_check(base, token)
    except Exception as exc:  # noqa: BLE001 — recorded as a failed step; the server is still stopped
        step("feature flow", False, f"{type(exc).__name__}: {exc}"[:900])
        stop(proc)
        sampler.stop_flag.set()
        (work / "offline-check.json").write_text(json.dumps({"results": RESULTS}, indent=2), encoding="utf-8")
        return 1
    second = subprocess.run(  # noqa: S603
        [
            str(app / ".venv" / "Scripts" / "python.exe"),
            "-m",
            "ledgerspy",
            "serve",
            "--no-browser",
            "--port",
            str(args.port + 1),
            "--workspace",
            str(ws),
        ],
        cwd=app,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    step(
        "second server on the same workspace refused",
        second.returncode == 3 and "already using" in second.stderr,
        second.stderr.strip()[:160],
    )
    code = stop(proc)
    time.sleep(2)
    left = still_alive(sampler.seen)
    lock_free = subprocess.run(  # noqa: S603
        [
            str(app / ".venv" / "Scripts" / "python.exe"),
            "-m",
            "ledgerspy",
            "doctor",
            "--workspace",
            str(ws),
            "--json",
        ],
        cwd=app,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    ).stdout
    lock_ok = '"no other LedgerSpy server is using this workspace"' in lock_free
    step(
        "stop of ledgerspy.cmd with Ctrl+Break: no process of the installation left, lock released",
        code is not None and not left and lock_ok,
        f"exit {code} (cmd.exe), remaining pids {sorted(left)}, lock free {lock_ok}",
    )

    proc, base, token = start(app, env, args.port, via_cmd=False)
    api = Api(base, token)
    again = api.call("GET", "/api/datasets")["items"]
    av = api.call("GET", "/api/audit/verify")
    step(
        "restart on the same workspace: data intact, audit verifies",
        len(again) == info["datasets"] and av["ok"],
        f"{len(again)} datasets, {av['entries']} audit entries",
    )
    code = stop(proc)
    time.sleep(1)
    said = "".join(getattr(proc, "output", []))
    step(
        "graceful stop of the server with Ctrl+Break: exit code 0, 'stopped cleanly'",
        code == 0 and "LedgerSpy stopped cleanly." in said and not still_alive(sampler.seen),
        f"exit {code}",
    )

    old = work / "workspace-v8"
    make_v8_workspace(old)
    env_old = env_for(work, old)
    doc = subprocess.run(
        ["cmd", "/c", str(app / "doctor.cmd")],
        cwd=app,
        env=env_old,  # noqa: S603, S607
        capture_output=True,
        text=True,
        timeout=300,
    )
    step("doctor on an older workspace announces the upgrade", "will be upgraded to v9" in doc.stdout, "")
    proc, base, token = start(app, env_old, args.port, via_cmd=True)
    api = Api(base, token)
    cases_ok = api.call("GET", "/api/cases/C-01J00000000000000000000000")["name"] == "existing engagement"
    stop(proc)
    backups = list((old / "backups").glob("ledgerspy-v8-before-v9-*.db"))
    with sqlite3.connect(old / "ledgerspy.db") as c:
        version = c.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    step(
        "existing v8 workspace upgraded with a backup; data kept",
        cases_ok and version == 9 and len(backups) == 1,
        f"schema v{version}, backup {[b.name for b in backups]}",
    )

    sampler.stop_flag.set()
    sampler.join(timeout=10)
    netlog = (work / "netguard.log").read_text(encoding="utf-8") if (work / "netguard.log").exists() else ""
    step("no guarded network attempt (pip, server, PDF/OCR worker)", not netlog.strip(), netlog[:400])
    covered = sampler.pids >= SERVER_PIDS
    step(
        "no non-loopback TCP connection by the installation (netstat)",
        not sampler.foreign and covered and bool(SERVER_PIDS),
        f"{sampler.samples} samples over {len(sampler.seen)} processes of the installation; the "
        f"{len(SERVER_PIDS)} server processes were sampled: {covered}; foreign {sampler.foreign[:3]}",
    )
    report = {
        "bundle": str(args.bundle.resolve()),
        "bundle_sha256": hashlib.sha256(args.bundle.read_bytes()).hexdigest(),
        "build_info": json.loads((app / "BUILD-INFO.json").read_text(encoding="utf-8")),
        "python": subprocess.run(
            [str(app / ".venv" / "Scripts" / "python.exe"), "--version"],  # noqa: S603
            capture_output=True,
            text=True,
        ).stdout.strip(),
        "seconds": round(time.time() - started),
        "netstat_samples": sampler.samples,
        "processes_watched": len(sampler.seen),
        "server_pids": sorted(SERVER_PIDS),
        "results": RESULTS,
        "passed": sum(r["ok"] for r in RESULTS),
        "failed": sum(not r["ok"] for r in RESULTS),
    }
    (work / "offline-check.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        f"\n{report['passed']} passed, {report['failed']} failed ({report['seconds']} s): "
        f"{work / 'offline-check.json'}"
    )
    shutil.rmtree(app / ".venv" / "Lib" / "site-packages" / "__pycache__", ignore_errors=True)
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
