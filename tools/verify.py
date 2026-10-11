"""LedgerSpy release verification (P22): one command for every required check.

    python tools/verify.py                      run every check (foreground); exit 0 / 1 / 2
    python tools/verify.py --offline            also run the offline bundle check (Windows, slow)
    python tools/verify.py --detach             same, as a detached process (survives the terminal)
    python tools/verify.py status <dir> [--wait]   read a run's result (exit code = its outcome)
    python tools/verify.py package-check --bundle <zip>   validate one release bundle

Each check runs as its own subprocess with a timeout; stdout / stderr go to `<dir>/<check>.log`. Counts come
from machine-readable reports, never from log text alone: pytest's JUnit XML; the JSON reports the frontend
checks write (accepted only if written during this run); the package check's own JSON. `<dir>/run.json` is
rewritten atomically after every step and carries `complete` / `exit_code` only when the run really finished,
so a killed or interrupted run can never read as passed. Results go to `verify-results/<UTC time>/`
(git-ignored).

Exit codes: 0 every required check passed; 1 a required check failed; 2 incomplete (a required check could not
run, was interrupted or never finished); 3 usage error.
"""

import argparse
import contextlib
import ctypes
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET  # noqa: S405 — parses our own pytest report, never untrusted XML
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
OUT_ROOT = ROOT / "verify-results"
FRONTEND_OUT = ROOT / "tests" / "frontend" / "out"

EXIT_PASSED, EXIT_FAILED, EXIT_INCOMPLETE, EXIT_USAGE = 0, 1, 2, 3
FINAL = ("passed", "failed", "unavailable", "interrupted", "incomplete", "not_run")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ------------------------------------------------------------------------------------------- checks


@dataclass
class Check:
    name: str
    required: bool
    argv: list[str]
    timeout: int
    parse: str  # junit / json_list / harness_json / ruff_check / ruff_format / package_json / offline_json
    report: Path | None = None  # machine-readable report the check writes
    needs: list[str] = field(default_factory=list)  # prerequisites: "node", "playwright", "bundle", "windows"
    note: str = ""


def checks_for(out: Path, bundle: Path | None, offline: bool, offline_work: Path | None) -> list[Check]:
    cs = [
        Check("ruff-lint", True, [PY, "-m", "ruff", "check", "."], 600, "ruff_check"),
        Check("ruff-format", True, [PY, "-m", "ruff", "format", "--check", "."], 600, "ruff_format"),
        Check(
            "backend",
            True,
            [
                PY,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "-rs",
                f"--junitxml={out / 'backend-junit.xml'}",
                "tests/backend",
            ],
            3 * 3600,
            "junit",
            out / "backend-junit.xml",
        ),
        Check(
            "node-harness",
            True,
            ["node", "tests/frontend/harness.js"],
            600,
            "harness_json",
            FRONTEND_OUT / "harness-results.json",
            ["node"],
        ),
        Check(
            "browser",
            True,
            [PY, "tests/frontend/browser_check.py"],
            1800,
            "json_list",
            FRONTEND_OUT / "browser-results.json",
            ["playwright"],
        ),
        Check(
            "p4-panel",
            True,
            [PY, "tests/frontend/integrity_panel_check.py"],
            900,
            "json_list",
            FRONTEND_OUT / "integrity-panel-results.json",
            ["playwright"],
        ),
        Check(
            "package",
            True,
            [
                PY,
                str(Path(__file__).resolve()),
                "package-check",
                "--bundle",
                str(bundle or ""),
                "--json",
                str(out / "package.json"),
            ],
            900,
            "package_json",
            out / "package.json",
            ["bundle"],
        ),
    ]
    if offline:
        cs.append(
            Check(
                "offline-bundle",
                False,
                [
                    PY,
                    "packaging/offline_check.py",
                    "--bundle",
                    str(bundle or ""),
                    "--work",
                    str(offline_work),
                ],
                2400,
                "offline_json",
                (offline_work or out) / "offline-check.json",
                ["bundle", "windows"],
                "optional: installs the bundle under a network guard (Windows only)",
            )
        )
    return cs


def missing_prerequisites(c: Check, bundle: Path | None) -> str | None:
    for need in c.needs:
        if need == "node" and shutil.which("node") is None:
            return "Node.js is not installed (needed for the frontend harness)"
        if need == "playwright" and importlib.util.find_spec("playwright") is None:
            return (
                "Playwright is not installed in this Python (pip install playwright; playwright install "
                "chromium)"
            )
        if need == "bundle" and (bundle is None or not bundle.is_file()):
            return "no release bundle: build one (packaging/build_bundle.py) or pass --bundle"
        if need == "windows" and os.name != "nt":
            return "the offline bundle check runs on Windows only (the bundle targets win_amd64)"
    return None


# ------------------------------------------------------------------------------------------- parsers


def parse_junit(path: Path) -> dict[str, Any]:
    root = ET.parse(path).getroot()  # noqa: S314 — our own report
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    total = sum(int(s.get("tests", 0)) for s in suites)
    failures = sum(int(s.get("failures", 0)) for s in suites)
    errors = sum(int(s.get("errors", 0)) for s in suites)
    skipped = sum(int(s.get("skipped", 0)) for s in suites)
    reasons: dict[str, int] = {}
    for case in root.iter("testcase"):
        sk = case.find("skipped")
        if sk is not None:
            msg = (sk.get("message") or "skipped").strip()
            reasons[msg] = reasons.get(msg, 0) + 1
    return {
        "total": total,
        "passed": total - failures - errors - skipped,
        "failed": failures,
        "errors": errors,
        "skipped": skipped,
        "skip_reasons": reasons,
    }


def parse_report(c: Check, started: float, log: str) -> tuple[dict[str, Any] | None, str]:
    """Counts from the check's machine-readable report. (None, why) when there is no trustworthy report."""
    if c.parse == "ruff_check":
        m = re.search(r"Found (\d+) error", log)
        n = int(m.group(1)) if m else 0
        if "All checks passed!" not in log and not m:
            return None, "no Ruff summary in the output"
        return {"failed": n}, ""
    if c.parse == "ruff_format":
        m = re.search(r"(\d+) files? already formatted", log)
        bad = re.search(r"(\d+) files? would be reformatted", log)
        if not m and not bad:
            return None, "no Ruff format summary in the output"
        return {"passed": int(m.group(1)) if m else 0, "failed": int(bad.group(1)) if bad else 0}, ""
    if c.report is None or not c.report.is_file():
        return None, f"no report at {c.report}"
    if c.report.stat().st_mtime < started - 1:
        return None, f"the report {c.report.name} predates this run (stale)"
    if c.parse == "junit":
        try:
            return parse_junit(c.report), ""
        except ET.ParseError as exc:
            return None, f"unreadable JUnit report: {exc}"
    data = json.loads(c.report.read_text(encoding="utf-8"))
    if c.parse == "json_list":
        ok = sum(1 for r in data if r.get("ok"))
        return {"total": len(data), "passed": ok, "failed": len(data) - ok}, ""
    if c.parse == "harness_json":
        return {
            "total": data["passed"] + data["failed"],
            "passed": data["passed"],
            "failed": data["failed"],
        }, ""
    if c.parse in ("package_json", "offline_json"):
        res = data.get("results", [])
        ok = sum(1 for r in res if r.get("ok"))
        counts = {"total": len(res), "passed": ok, "failed": len(res) - ok}
        if c.parse == "package_json":
            counts["info"] = data.get("info", {})
        return counts, ""
    return None, f"unknown parser {c.parse}"


def judge(c: Check, exit_code: int | None, counts: dict | None, why: str) -> tuple[str, str]:
    """A check passes only with exit code 0 AND a trustworthy report showing no failure / error."""
    if exit_code is None:
        return "failed", f"timed out after {c.timeout} s (process tree killed)"
    if counts is None:
        return ("failed" if exit_code else "incomplete"), f"exit {exit_code}; {why}"
    bad = counts.get("failed", 0) + counts.get("errors", 0)
    if exit_code != 0 or bad:
        return "failed", f"exit {exit_code}; {bad} failed / errors"
    if c.parse in ("junit", "json_list", "harness_json", "package_json", "offline_json") and not counts.get(
        "total"
    ):
        return "incomplete", "the report lists no test at all"
    if "passed" not in counts:
        return "passed", "exit 0; clean"
    return "passed", f"exit 0; {counts['passed']} passed" + (
        f", {counts['skipped']} skipped" if counts.get("skipped") else ""
    )


# ------------------------------------------------------------------------------------------- run state


def write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,  # noqa: S603, S607
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def remove_tree(folder: Path) -> bool:
    """Delete a temporary folder this run created. Evidence and backups inside it are read-only by design,
    so the flag is cleared first; a file still held by a dying process gets a few retries. True if gone."""

    def retry(func, path, _exc):
        os.chmod(path, stat.S_IWRITE)
        func(path)

    hook = {"onexc": retry} if sys.version_info >= (3, 12) else {"onerror": retry}
    for _ in range(10):
        try:
            shutil.rmtree(folder, **hook)
            break
        except OSError:
            time.sleep(0.5)
    return not folder.exists()


def kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False)  # noqa: S603, S607
    else:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=30)


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return (
                ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == 0x102
            )  # WAIT_TIMEOUT: still running
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def outcome(run: dict) -> tuple[str, int]:
    """Overall status from the per-check results. Success needs EVERY required check to have passed."""
    if not run.get("complete"):
        alive = pid_alive(run.get("pid", -1))
        return ("running", EXIT_INCOMPLETE) if alive else ("interrupted", EXIT_INCOMPLETE)
    req = [c for c in run["checks"] if c["required"]]
    if any(c["status"] == "failed" for c in req):
        return "failed", EXIT_FAILED
    if any(c["status"] != "passed" for c in req) or not req:
        return "incomplete", EXIT_INCOMPLETE
    return "passed", EXIT_PASSED


def run_all(checks: list[Check], out: Path, bundle: Path | None, offline_work: Path | None = None) -> int:
    run = {
        "tool": "tools/verify.py",
        "started": now(),
        "ended": None,
        "complete": False,
        "pid": os.getpid(),
        "status": "running",
        "exit_code": None,
        "source": {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain"))},
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "bundle": str(bundle) if bundle else None,
        "checks": [
            {"name": c.name, "required": c.required, "status": "pending", "note": c.note} for c in checks
        ],
    }
    state = out / "run.json"
    write_json(state, run)
    interrupted = False
    for i, c in enumerate(checks):
        rec = run["checks"][i]
        if interrupted:
            rec["status"] = "not_run"
            continue
        why = missing_prerequisites(c, bundle)
        if why:
            rec.update(status="unavailable", detail=why)
            write_json(state, run)
            print(f"[{c.name}] UNAVAILABLE - {why}", flush=True)
            continue
        log = out / f"{c.name}.log"
        start = time.time()
        rec.update(status="running", started=now(), command=c.argv, log=str(log))
        write_json(state, run)
        print(f"[{c.name}] running ...", flush=True)
        code: int | None
        with open(log, "w", encoding="utf-8", errors="replace") as fh:
            kw: dict[str, Any] = (
                {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                if os.name == "nt"
                else {"start_new_session": True}
            )
            proc = subprocess.Popen(
                c.argv,
                cwd=ROOT,
                stdout=fh,
                stderr=subprocess.STDOUT,  # noqa: S603
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                **kw,
            )
            try:
                code = proc.wait(timeout=c.timeout)
            except subprocess.TimeoutExpired:
                kill_tree(proc)
                code = None
            except KeyboardInterrupt:
                kill_tree(proc)
                rec.update(
                    status="interrupted",
                    ended=now(),
                    duration_s=round(time.time() - start, 1),
                    detail="interrupted by the user (Ctrl+C); the check did not finish",
                )
                interrupted = True
                write_json(state, run)
                continue
        text = log.read_text(encoding="utf-8", errors="replace")
        counts, why = parse_report(c, start, text)
        status, detail = judge(c, code, counts, why)
        rec.update(
            status=status,
            exit_code=code,
            ended=now(),
            duration_s=round(time.time() - start, 1),
            counts=counts,
            detail=detail,
            report=str(c.report) if c.report else None,
        )
        if c.report and c.report.is_file() and c.report.parent != out:
            shutil.copy2(c.report, out / c.report.name)  # keep the evidence with the run
        if c.name == "offline-bundle" and offline_work and offline_work.exists():
            for keep in ("offline-check.json", "install-1.log", "install-2.log", "netguard.log"):
                if (offline_work / keep).is_file():
                    shutil.copy2(offline_work / keep, out / f"offline-{keep}")
            remove_tree(offline_work.parent)  # the temporary install (venv, workspaces, read-only evidence)
            rec["temporary_install_removed"] = not offline_work.parent.exists()
        write_json(state, run)
        print(f"[{c.name}] {status.upper()} - {detail}", flush=True)
    run["complete"] = not interrupted
    run["ended"] = now()
    status, code = outcome(run) if not interrupted else ("interrupted", EXIT_INCOMPLETE)
    run.update(status=status, exit_code=code, complete=True)
    write_json(state, run)
    print_summary(run, out)
    return code


def print_summary(run: dict, out: Path) -> None:
    print(f"\nLedgerSpy verification - {run['status'].upper()} (exit {run['exit_code']})")
    print(
        f"source {run['source']['commit'][:12]}{' (uncommitted changes)' if run['source']['dirty'] else ''}"
        f" · python {run['python']} · results {out}"
    )
    for c in run["checks"]:
        counts = c.get("counts") or {}
        n = ", ".join(
            f"{k} {v}" for k, v in counts.items() if k in ("passed", "failed", "errors", "skipped", "total")
        )
        tag = "required" if c["required"] else "optional"
        print(
            f"  {c['status'].upper():11} {c['name']:15} {tag:8} {c.get('duration_s', '')!s:>8}s  {n}  "
            f"{c.get('detail', '')}"
        )
        for reason, k in (counts.get("skip_reasons") or {}).items():
            print(f"{'':39}skipped {k}: {reason}")


# ------------------------------------------------------------------------------------------- package check


def package_check(bundle: Path) -> dict[str, Any]:
    """Validate a release bundle (changes nothing): integrity, contents, provenance, reproducibility."""
    sys.path.insert(0, str(ROOT / "packaging"))
    import build_bundle  # noqa: PLC0415

    results: list[dict[str, Any]] = []
    info: dict[str, Any] = {"bundle": str(bundle), "sha256": hashlib.sha256(bundle.read_bytes()).hexdigest()}

    def add(name: str, ok: bool, detail: str = "") -> None:
        results.append({"check": name, "ok": bool(ok), "detail": detail})

    with zipfile.ZipFile(bundle) as z:
        names = z.namelist()
        tops = {n.split("/", 1)[0] for n in names}
        add("single top-level folder", len(tops) == 1, ", ".join(sorted(tops)))
        top = sorted(tops)[0]
        files = {n.split("/", 1)[1]: z.read(n) for n in names if not n.endswith("/")}
    manifest_text = files.get("MANIFEST.sha256", b"").decode("utf-8")
    manifest = {}
    for line in manifest_text.splitlines():
        digest, _, rel = line.partition("  ")
        manifest[rel] = digest
    listed, present = set(manifest), set(files) - {"MANIFEST.sha256"}
    bad = sorted(r for r in listed & present if hashlib.sha256(files[r]).hexdigest() != manifest[r])
    add(
        "manifest covers exactly the bundle's files",
        listed == present,
        f"unlisted {sorted(present - listed)[:5]}, missing {sorted(listed - present)[:5]}",
    )
    add(
        "every SHA-256 matches the manifest",
        not bad and bool(manifest),
        f"{len(manifest)} files; mismatched {bad[:5]}",
    )
    try:
        build_bundle.scan({k: v for k, v in files.items() if k not in ("MANIFEST.sha256", "BUILD-INFO.json")})
        add("no tokens, databases, workspaces, caches, ground truth or user paths", True, "")
    except build_bundle.BuildError as exc:
        add("no tokens, databases, workspaces, caches, ground truth or user paths", False, str(exc)[:400])
    required = {
        "install.cmd",
        "ledgerspy.cmd",
        "doctor.cmd",
        "INSTALL.md",
        "requirements.lock",
        "BUILD-INFO.json",
        "frontend/index.html",
        "config/default.yaml",
        "ledgerspy/__main__.py",
    }
    add("required files present", required <= set(files), f"missing {sorted(required - set(files))}")

    build = json.loads(files.get("BUILD-INFO.json", b"{}") or b"{}")
    commit = (build.get("source") or {}).get("commit", "")
    dirty = (build.get("source") or {}).get("dirty", True)
    head = git("rev-parse", "HEAD")
    info.update(
        top=top, source_commit=commit, built_from_dirty_tree=dirty, head=head, matches_head=commit == head
    )
    add(
        "built from a committed, clean tree",
        bool(commit) and not dirty,
        f"commit {commit[:12]}, dirty {dirty}",
    )

    # provenance: every application file equals that commit's blob; every wheel equals that commit's lock
    known = git("cat-file", "-t", commit) == "commit" if commit else False
    add("source commit exists in this repository", known, commit[:12])
    if known:
        mismatch, checked = [], 0
        lock = git("show", f"{commit}:requirements.lock")
        lock_hashes = set(re.findall(r"--hash=sha256:([0-9a-f]{64})", lock))
        for rel, data in sorted(files.items()):
            if rel in ("MANIFEST.sha256", "BUILD-INFO.json"):
                continue
            if rel.startswith("wheelhouse/"):
                ok = hashlib.sha256(data).hexdigest() in lock_hashes
            else:
                src = {"INSTALL.md": "docs/installation.md"}.get(rel, rel)
                if rel.endswith(".cmd") and "/" not in rel:
                    src = f"packaging/templates/{rel}"
                blob = subprocess.run(
                    ["git", "show", f"{commit}:{src}"],
                    cwd=ROOT,
                    capture_output=True,  # noqa: S603, S607
                    check=False,
                )
                ref = blob.stdout
                if rel.endswith(".cmd"):  # stored LF in git, shipped CRLF (.gitattributes eol=crlf)
                    ref = ref.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
                ok = blob.returncode == 0 and (
                    ref == data
                    or ref.replace(b"\r\n", b"\n") == data.replace(b"\r\n", b"\n")
                    and not rel.endswith(".cmd")
                )
            checked += 1
            if not ok:
                mismatch.append(rel)
        add(
            "every file matches its source commit (wheels: that commit's lock)",
            not mismatch,
            f"{checked} files compared; mismatched {mismatch[:5]}",
        )
    # reproducibility: only meaningful for a bundle of the current, clean HEAD
    if commit and commit == head and not dirty and not git("status", "--porcelain"):
        with tempfile.TemporaryDirectory(prefix="ledgerspy-verify-") as tmp:
            wh = Path(tmp) / "wheelhouse"
            wh.mkdir()
            for rel, data in files.items():
                if rel.startswith("wheelhouse/"):
                    (wh / rel.split("/", 1)[1]).write_bytes(data)
            again, _ = build_bundle.build(wh, Path(tmp) / "dist")
            same = again.read_bytes() == bundle.read_bytes()
            add("rebuild from HEAD is byte-identical", same, "")
        info["temporary_rebuild_removed"] = not Path(tmp).exists()
        info["reproducibility"] = "checked"
    else:
        info["reproducibility"] = (
            "not checked: the bundle is not from the current clean HEAD "
            f"(bundle {commit[:12]}, HEAD {head[:12]}, working tree "
            f"{'dirty' if git('status', '--porcelain') else 'clean'})"
        )
    return {"results": results, "info": info, "ok": all(r["ok"] for r in results)}


# ------------------------------------------------------------------------------------------- CLI


def newest_bundle() -> Path | None:
    found = sorted((ROOT / "dist").glob("ledgerspy-*-offline.zip"), key=lambda p: p.stat().st_mtime)
    return found[-1] if found else None


def cmd_status(out: Path, wait: bool) -> int:
    state = out / "run.json"
    appear_by = time.time() + 120  # a just-started detached run may not have written run.json yet
    while True:
        if not state.is_file():
            if wait and time.time() < appear_by:
                time.sleep(2)
                continue
            print(f"no run.json in {out}", file=sys.stderr)
            return EXIT_USAGE
        run = json.loads(state.read_text(encoding="utf-8"))
        status, code = outcome(run)
        if not wait or status != "running":
            break
        time.sleep(10)
    if run.get("complete"):
        print_summary(run, out)
    else:
        print(f"run in {out}: {status.upper()} - started {run['started']}, not complete")
        for c in run["checks"]:
            print(f"  {c['status'].upper():11} {c['name']}")
    return code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LedgerSpy release verification (P22)")
    sub = ap.add_subparsers(dest="cmd")
    st = sub.add_parser("status", help="report a run's outcome")
    st.add_argument("dir", type=Path)
    st.add_argument("--wait", action="store_true")
    pk = sub.add_parser("package-check", help="validate one release bundle")
    pk.add_argument("--bundle", type=Path, required=True)
    pk.add_argument("--json", type=Path)
    ap.add_argument("--out", type=Path, help="results folder (default verify-results/<UTC time>)")
    ap.add_argument("--bundle", type=Path, help="release bundle (default: newest dist/*-offline.zip)")
    ap.add_argument("--offline", action="store_true", help="also run the optional offline bundle check")
    ap.add_argument("--detach", action="store_true", help="run detached; follow with: status <dir> --wait")
    args = ap.parse_args(argv)

    if args.cmd == "status":
        return cmd_status(args.dir, args.wait)
    if args.cmd == "package-check":
        if not args.bundle.is_file():
            print(f"ERROR: no bundle at {args.bundle}", file=sys.stderr)
            return EXIT_INCOMPLETE
        res = package_check(args.bundle)
        if args.json:
            write_json(args.json, res)
        for r in res["results"]:
            print(
                f"{'PASS' if r['ok'] else 'FAIL'}  {r['check']}"
                + (f" - {r['detail']}" if r["detail"] else "")
            )
        for k, v in res["info"].items():
            print(f"      {k}: {v}")
        return EXIT_PASSED if res["ok"] else EXIT_FAILED

    out = args.out or OUT_ROOT / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out.mkdir(parents=True, exist_ok=True)
    bundle = args.bundle or newest_bundle()
    if args.detach:
        cmd = (
            [PY, str(Path(__file__).resolve()), "--out", str(out)]
            + (["--bundle", str(bundle)] if bundle else [])
            + (["--offline"] if args.offline else [])
        )
        flags = (
            (0x00000008 | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
        )  # DETACHED_PROCESS
        with open(out / "orchestrator.log", "w", encoding="utf-8") as fh:
            p = subprocess.Popen(
                cmd,
                cwd=ROOT,
                stdout=fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,  # noqa: S603
                creationflags=flags,
                start_new_session=os.name != "nt",
            )
        print(f"started (pid {p.pid}); results in {out}")
        print(f'follow with: {PY} tools/verify.py status "{out}" --wait')
        return EXIT_PASSED
    work = Path(tempfile.mkdtemp(prefix="ledgerspy-offline-")) / "work" if args.offline else None
    return run_all(checks_for(out, bundle, args.offline, work), out, bundle, work)


if __name__ == "__main__":
    sys.exit(main())
