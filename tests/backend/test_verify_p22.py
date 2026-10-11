"""P22 release verification: the orchestrator's honesty (exit codes, stale / missing reports, interruption,
timeouts, missing prerequisites), result aggregation, package integrity checks, temporary-folder cleanup and
the acceptance inventory. The checks run here are small, deterministic stand-ins; the real run is recorded in
docs/p22-acceptance.md."""

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(REPO / "packaging"))
import build_bundle  # noqa: E402
import verify  # noqa: E402

PY = sys.executable


def fake(name, code: str, required=True, parse="ruff_check", report=None, needs=None, timeout=60):
    return verify.Check(name, required, [PY, "-c", code], timeout, parse, report, needs or [])


def run(tmp_path, checks, **kw) -> tuple[int, dict]:
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    code = verify.run_all(checks, out, kw.get("bundle"), kw.get("offline_work"))
    return code, json.loads((out / "run.json").read_text(encoding="utf-8"))


PASS = "print('All checks passed!')"
FAIL = "import sys; print('Found 2 errors.'); sys.exit(1)"


# ------------------------------------------------------------------------------------------- exit codes


def test_all_required_passing_is_the_only_success(tmp_path):
    code, run_ = run(tmp_path, [fake("a", PASS), fake("b", PASS)])
    assert code == 0 and run_["status"] == "passed" and run_["complete"] is True
    assert [c["status"] for c in run_["checks"]] == ["passed", "passed"]
    assert all(
        c["duration_s"] >= 0 and c["started"] and c["ended"] and c["exit_code"] == 0 for c in run_["checks"]
    )


def test_a_failing_check_propagates_a_nonzero_exit_code(tmp_path):
    code, run_ = run(tmp_path, [fake("a", PASS), fake("b", FAIL), fake("c", PASS)])
    assert code == verify.EXIT_FAILED and run_["status"] == "failed"
    b = run_["checks"][1]
    assert b["status"] == "failed" and b["exit_code"] == 1 and b["counts"] == {"failed": 2}
    assert run_["checks"][2]["status"] == "passed"  # later checks still run and are reported separately
    assert (tmp_path / "out" / "b.log").read_text(encoding="utf-8").strip() == "Found 2 errors."


def test_an_optional_failure_does_not_hide_required_results_but_is_reported(tmp_path):
    code, run_ = run(tmp_path, [fake("req", PASS), fake("opt", FAIL, required=False)])
    assert code == 0 and run_["checks"][1]["status"] == "failed" and run_["checks"][1]["required"] is False


# ------------------------------------------------------------------------------------------- reports


def test_exit_zero_without_a_trustworthy_report_is_never_a_pass(tmp_path):
    rep = tmp_path / "r.json"
    rep.write_text(json.dumps([{"ok": True}]), encoding="utf-8")
    old = time.time() - 3600
    os.utime(rep, (old, old))  # written by an earlier run
    checks = [
        fake("stale", "pass", parse="json_list", report=rep),
        fake("missing", "pass", parse="json_list", report=tmp_path / "never-written.json"),
        fake("silent", "pass"),  # exit 0 but no tool summary at all
    ]
    code, run_ = run(tmp_path, checks)
    assert code == verify.EXIT_INCOMPLETE and run_["status"] == "incomplete"
    assert [c["status"] for c in run_["checks"]] == ["incomplete", "incomplete", "incomplete"]
    assert "stale" in run_["checks"][0]["detail"] and "no report" in run_["checks"][1]["detail"]


def test_a_report_with_failures_fails_even_with_exit_zero(tmp_path):
    rep = tmp_path / "r.json"
    code_ = f"import json; json.dump([{{'ok': True}}, {{'ok': False}}], open(r'{rep}', 'w'))"
    code, run_ = run(tmp_path, [fake("ui", code_, parse="json_list", report=rep)])
    assert code == verify.EXIT_FAILED and run_["checks"][0]["counts"] == {
        "total": 2,
        "passed": 1,
        "failed": 1,
    }


JUNIT = """<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" errors="1" failures="2"
skipped="3" tests="20"><testcase classname="a" name="t1">
<skipped message="local OCR unavailable: x"/></testcase>
<testcase classname="a" name="t2"><skipped message="local OCR unavailable: x"/></testcase>
<testcase classname="a" name="t3"><skipped message="other"/></testcase></testsuite></testsuites>"""


def test_junit_counts_and_skip_reasons_are_aggregated(tmp_path):
    p = tmp_path / "j.xml"
    p.write_text(JUNIT, encoding="utf-8")
    c = verify.parse_junit(p)
    assert c == {
        "total": 20,
        "passed": 14,
        "failed": 2,
        "errors": 1,
        "skipped": 3,
        "skip_reasons": {"local OCR unavailable: x": 2, "other": 1},
    }
    check = verify.Check("backend", True, [], 1, "junit", p)
    assert verify.judge(check, 0, c, "")[0] == "failed"  # errors / failures fail despite exit 0
    ok = dict(c, failed=0, errors=0)
    assert verify.judge(check, 0, ok, "") == ("passed", "exit 0; 14 passed, 3 skipped")
    assert verify.judge(check, 0, dict(ok, total=0, passed=0), "")[0] == "incomplete"  # no test ran at all
    assert verify.judge(check, None, ok, "")[0] == "failed"  # timeout


def test_the_real_pytest_report_is_parsed(tmp_path):
    xml = tmp_path / "real.xml"
    r = subprocess.run(
        [
            PY,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            f"--junitxml={xml}",
            "tests/backend/test_ids.py",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    counts = verify.parse_junit(xml)
    summary = re.search(r"(\d+) passed", r.stdout)
    assert r.returncode == 0 and counts["passed"] == int(summary.group(1)) and counts["failed"] == 0


# ------------------------------------------------------------------------------------------- availability


def test_missing_prerequisites_make_the_run_incomplete_not_passed(tmp_path, monkeypatch):
    monkeypatch.setattr(verify.shutil, "which", lambda _name: None)
    checks = [
        fake("ok", PASS),
        fake("node-harness", PASS, needs=["node"]),
        fake("pkg", PASS, needs=["bundle"]),
        fake("offline", PASS, required=False, needs=["bundle"]),
    ]
    code, run_ = run(tmp_path, checks, bundle=tmp_path / "absent.zip")
    st = {c["name"]: c for c in run_["checks"]}
    assert code == verify.EXIT_INCOMPLETE and run_["status"] == "incomplete"
    assert (
        st["node-harness"]["status"] == "unavailable"
        and "Node.js is not installed" in st["node-harness"]["detail"]
    )
    assert st["pkg"]["status"] == "unavailable" and "no release bundle" in st["pkg"]["detail"]
    assert st["offline"]["status"] == "unavailable" and st["ok"]["status"] == "passed"


def test_a_hung_check_is_killed_and_failed(tmp_path):
    code, run_ = run(tmp_path, [fake("hang", "import time; time.sleep(120)", timeout=3)])
    c = run_["checks"][0]
    assert code == verify.EXIT_FAILED and c["status"] == "failed" and "timed out" in c["detail"]
    assert c["duration_s"] < 60


# ------------------------------------------------------------------------------------------- interruption


def test_a_killed_run_reads_as_interrupted_never_passed(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    script = (
        f"import sys; sys.path.insert(0, r'{REPO / 'tools'}'); import verify\n"
        f"c = verify.Check('ok', True, [r'{PY}', '-c', \"print('All checks passed!')\"], 60, 'ruff_check')\n"
        f"h = verify.Check('slow', True, [r'{PY}', '-c', 'import time; time.sleep(300)'], 600, "
        f"'ruff_check')\n"
        f"verify.run_all([c, h], verify.Path(r'{out}'), None)\n"
    )
    proc = subprocess.Popen([PY, "-c", script], cwd=REPO)
    deadline = time.time() + 120
    while time.time() < deadline:
        state = out / "run.json"
        if (
            state.is_file()
            and '"slow"' in state.read_text(encoding="utf-8")
            and '"running"' in state.read_text(encoding="utf-8").split('"slow"', 1)[1][:200]
        ):
            break
        time.sleep(0.5)
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False)
    else:
        proc.kill()
    proc.wait(timeout=60)
    run_ = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert run_["complete"] is False and run_["exit_code"] is None  # no completion marker
    assert verify.outcome(run_) == ("interrupted", verify.EXIT_INCOMPLETE)
    assert verify.cmd_status(out, wait=False) == verify.EXIT_INCOMPLETE


def test_status_wait_tolerates_a_run_that_has_not_written_its_record_yet(tmp_path):
    """Found in the first real detached run: `status --wait` started before run.json existed and gave up."""
    import threading

    out = tmp_path / "late"
    out.mkdir()

    def later():
        time.sleep(3)
        (tmp_path / "x").mkdir()
        run(tmp_path / "x", [fake("a", PASS)])
        (out / "run.json").write_text((tmp_path / "x" / "out" / "run.json").read_text(encoding="utf-8"))

    threading.Thread(target=later).start()
    assert verify.cmd_status(out, wait=True) == verify.EXIT_PASSED
    assert verify.cmd_status(tmp_path / "never", wait=False) == verify.EXIT_USAGE


def test_status_of_a_finished_run_returns_its_exit_code(tmp_path):
    run(tmp_path, [fake("a", FAIL)])
    assert verify.cmd_status(tmp_path / "out", wait=False) == verify.EXIT_FAILED


# ------------------------------------------------------------------------------------------- package checks


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    path, _ = build_bundle.build(None, tmp_path_factory.mktemp("dist"))
    return path


def rezip(src: Path, dst: Path, change) -> Path:
    with zipfile.ZipFile(src) as z:
        items = {n: z.read(n) for n in z.namelist()}
    items = change(items)
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in items.items():
            z.writestr(n, d)
    return dst


def status(res: dict) -> dict[str, bool]:
    return {r["check"]: r["ok"] for r in res["results"]}


def test_a_pristine_bundle_passes_the_integrity_checks(bundle):
    s = status(verify.package_check(bundle))
    for name in (
        "single top-level folder",
        "manifest covers exactly the bundle's files",
        "every SHA-256 matches the manifest",
        "no tokens, databases, workspaces, caches, ground truth or user paths",
        "required files present",
    ):
        assert s[name], name


def test_a_tampered_file_is_detected(bundle, tmp_path):
    def edit(items):
        key = next(n for n in items if n.endswith("config/default.yaml"))
        items[key] = items[key] + b"\n# edited\n"
        return items

    s = status(verify.package_check(rezip(bundle, tmp_path / "t.zip", edit)))
    assert s["every SHA-256 matches the manifest"] is False


def test_an_unlisted_or_secret_file_is_detected(bundle, tmp_path):
    def add(items):
        top = next(iter(items)).split("/")[0]
        items[f"{top}/workspace/ledgerspy.db"] = b"SQLite format 3"
        return items

    s = status(verify.package_check(rezip(bundle, tmp_path / "u.zip", add)))
    assert s["manifest covers exactly the bundle's files"] is False
    assert s["no tokens, databases, workspaces, caches, ground truth or user paths"] is False


def test_a_consistently_rehashed_edit_is_caught_by_provenance(bundle, tmp_path):
    """Whoever edits a file AND regenerates the manifest still fails the comparison with the source commit."""

    def forge(items):
        top = next(iter(items)).split("/")[0]
        key = f"{top}/config/default.yaml"
        items[key] = items[key].replace(b"host: 127.0.0.1", b"host: 127.0.0.1 ")
        body = {n.split("/", 1)[1]: d for n, d in items.items() if not n.endswith("MANIFEST.sha256")}
        items[f"{top}/MANIFEST.sha256"] = "".join(
            f"{hashlib.sha256(body[r]).hexdigest()}  {r}\n" for r in sorted(body)
        ).encode()
        return items

    s = status(verify.package_check(rezip(bundle, tmp_path / "f.zip", forge)))
    assert s["every SHA-256 matches the manifest"] is True  # the forgery is internally consistent …
    assert (
        s["every file matches its source commit (wheels: that commit's lock)"] is False
    )  # … but not the source


def test_package_check_cli_exit_codes(bundle, tmp_path):
    missing = subprocess.run(
        [PY, "tools/verify.py", "package-check", "--bundle", str(tmp_path / "none.zip")],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert missing.returncode == verify.EXIT_INCOMPLETE
    out = tmp_path / "p.json"
    bad = rezip(
        bundle,
        tmp_path / "b.zip",
        lambda i: {k: (v + b"x" if k.endswith("README.md") else v) for k, v in i.items()},
    )
    r = subprocess.run(
        [PY, "tools/verify.py", "package-check", "--bundle", str(bad), "--json", str(out)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert r.returncode == verify.EXIT_FAILED and json.loads(out.read_text())["ok"] is False


# ------------------------------------------------------------------------------------- cleanup / artefacts


def test_the_offline_check_temporary_install_is_removed_and_its_report_kept(tmp_path):
    work = tmp_path / "offline-tmp" / "work"
    (work / "app" / ".venv").mkdir(parents=True)
    ro = work / "app" / "evidence.csv"
    ro.write_text("x", encoding="utf-8")
    os.chmod(ro, stat.S_IREAD)
    report = work / "offline-check.json"
    code_ = f"import json; json.dump({{'results': [{{'ok': True}}]}}, open(r'{report}', 'w'))"
    check = verify.Check("offline-bundle", False, [PY, "-c", code_], 60, "offline_json", report)
    code, run_ = run(tmp_path, [fake("req", PASS), check], offline_work=work)
    c = run_["checks"][1]
    assert code == 0 and c["status"] == "passed" and c["temporary_install_removed"] is True
    assert (
        not (tmp_path / "offline-tmp").exists()
        and (tmp_path / "out" / "offline-offline-check.json").is_file()
    )


def test_ui_checks_remove_their_temporary_workspace(tmp_path):
    sys.path.insert(0, str(REPO / "tests" / "frontend"))
    import browser_check
    import integrity_panel_check

    for mod in (browser_check, integrity_panel_check):
        folder = tmp_path / mod.__name__
        (folder / "ws" / "evidence").mkdir(parents=True)
        f = folder / "ws" / "evidence" / "a.csv"
        f.write_text("x", encoding="utf-8")
        os.chmod(f, stat.S_IREAD)  # evidence is read-only by design
        mod.remove_workspace(folder)
        assert not folder.exists()


def test_a_different_earlier_bundle_is_preserved_not_overwritten(tmp_path):
    target, new = tmp_path / "b.zip", tmp_path / "b.zip.part"
    target.write_bytes(b"known good")
    new.write_bytes(b"newer build")
    kept = build_bundle.preserve_previous(target, new)
    assert kept is not None and kept.read_bytes() == b"known good" and not target.exists()
    assert kept.name == f"b.{hashlib.sha256(b'known good').hexdigest()[:12]}.zip"
    target.write_bytes(b"same")
    new.write_bytes(b"same")
    assert build_bundle.preserve_previous(target, new) is None and target.exists()


# ------------------------------------------------------------------------------------------- inventory


def test_the_acceptance_inventory_references_only_things_that_exist():
    inv = json.loads((REPO / "docs" / "acceptance-inventory.json").read_text(encoding="utf-8"))
    roadmap = (REPO / inv["roadmap"]).read_text(encoding="utf-8")
    ids = [p["id"] for p in inv["phases"]]
    assert ids == [f"P{i}" for i in range(24)]  # every phase up to P23, in order
    assert (REPO / inv["gap_audit"]).is_file()
    for p in inv["phases"]:
        line = next((ln for ln in roadmap.splitlines() if ln.startswith(p["heading"])), None)
        assert line is not None, p["heading"]
        assert ("✔" in line) == (p["status"] == "done"), p["id"]  # status agrees with the roadmap
        if p["status"] == "done":
            assert p["acceptance_docs"], p["id"]
        for doc in p["acceptance_docs"]:
            assert (REPO / doc).is_file(), doc
        for ref in p["gap_sections"]:
            doc, heading = ref.split("#", 1)
            headings = [
                h.lstrip("#").strip()
                for h in (REPO / doc).read_text(encoding="utf-8").splitlines()
                if h.startswith("#")
            ]
            assert heading in headings, ref
        for t in p["evidence"]["tests"]:
            assert (REPO / t).is_file(), t
        for note in p.get("closed_before_p23", []):  # recorded as text, and no longer listed as open
            assert note and isinstance(note, str) and note not in p["roadmap_gaps"], p["id"]
        known = {
            "backend",
            "node-harness",
            "browser",
            "p4-panel",
            "package",
            "offline-bundle",
            "ruff-lint",
            "ruff-format",
        }
        assert set(p["evidence"]["checks"]) <= known, p["id"]
    covered = {t for p in inv["phases"] for t in p["evidence"]["tests"]}
    unmapped = {f"tests/backend/{f.name}" for f in (REPO / "tests" / "backend").glob("test_*.py")} - covered
    assert unmapped == set(), unmapped  # every backend test file is attributed to a phase
