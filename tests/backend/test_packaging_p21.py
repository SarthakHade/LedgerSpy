"""P21 offline packaging: startup safety, diagnostics, migrations with backup, the dependency lock and the
bundle builder. The end-to-end, network-disabled installation check is packaging/offline_check.py (recorded in
docs/p21-acceptance.md); these tests cover the pieces that can run inside the suite."""

import hashlib
import importlib.metadata
import json
import os
import re
import socket
import sqlite3
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from ledgerspy import cli
from ledgerspy.cases.db import MIGRATIONS, SCHEMA_VERSION, connect, init_db
from ledgerspy.config import ConfigError, load_config
from ledgerspy.runtime import WorkspaceLock, WorkspaceLocked, doctor, locked_dependencies, pinned_dependencies

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "packaging"))
import build_bundle  # noqa: E402

# ------------------------------------------------------------------------------------------- configuration


def test_invalid_configuration_is_refused_with_a_clear_error(tmp_path, capsys):
    with pytest.raises(ConfigError):
        load_config(overrides={"server": {"host": "0.0.0.0"}})
    bad = tmp_path / "bad.yaml"
    bad.write_text("server: [unclosed\n", encoding="utf-8")
    assert cli.main(["doctor", "--config", str(bad), "--workspace", str(tmp_path / "ws")]) == 1
    assert "FAIL  configuration" in capsys.readouterr().out
    assert (
        cli.main(["serve", "--config", str(bad), "--workspace", str(tmp_path / "ws2"), "--no-browser"]) == 2
    )
    assert "ERROR: configuration" in capsys.readouterr().err


# ------------------------------------------------------------------------------------------- doctor


def test_doctor_reports_without_creating_anything(tmp_path, capsys):
    ws = tmp_path / "not" / "yet"
    code = cli.main(["doctor", "--workspace", str(ws), "--port", "8799", "--json"])
    checks = {c["name"]: c for c in json.loads(capsys.readouterr().out)}
    assert code == 0 and not ws.exists() and not (tmp_path / "not").exists()
    assert {
        "python",
        "dependencies",
        "frontend",
        "configuration",
        "workspace",
        "database",
        "workspace lock",
        "port",
        "pdf renderer",
        "ocr",
    } <= set(checks)
    assert checks["frontend"]["status"] == "ok" and "strict CSP" in checks["frontend"]["detail"]
    assert checks["dependencies"]["status"] == "ok"
    assert "will be created" in checks["database"]["detail"]


def test_doctor_explains_missing_ocr_and_stays_ready(tmp_path):
    cfg = tmp_path / "cfg.yaml"
    text = (REPO / "config" / "default.yaml").read_text(encoding="utf-8")
    text = re.sub(
        r"executable_candidates: \[[^\]]*\]", 'executable_candidates: ["no-such-tesseract"]', text, flags=re.S
    )
    cfg.write_text(text, encoding="utf-8")
    checks = {c.name: c for c in doctor(str(cfg), str(tmp_path / "ws"), 8799)}
    assert checks["ocr"].status == "warn" and "no Tesseract executable found" in checks["ocr"].detail
    assert "scanned pages are reported as not processed" in checks["ocr"].detail
    assert not any(c.status == "fail" for c in checks.values())


def test_doctor_flags_a_newer_or_older_database(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    c = sqlite3.connect(ws / "ledgerspy.db")
    c.execute("CREATE TABLE schema_version (version INTEGER, description TEXT)")
    c.execute("INSERT INTO schema_version VALUES (?, 'future')", (SCHEMA_VERSION + 1,))
    c.commit()
    c.close()
    db = {c.name: c for c in doctor(None, str(ws), 8799)}["database"]
    assert db.status == "fail" and "newer than this LedgerSpy" in db.detail


# ------------------------------------------------------------------------------------------- startup


def test_one_server_per_workspace(tmp_path):
    first = WorkspaceLock(tmp_path).acquire()
    try:
        with pytest.raises(WorkspaceLocked, match="already using the workspace"):
            WorkspaceLock(tmp_path).acquire()
        code = subprocess.run(  # a real second process is refused before touching the database
            [
                sys.executable,
                "-m",
                "ledgerspy",
                "serve",
                "--no-browser",
                "--workspace",
                str(tmp_path),
                "--port",
                "8798",
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert code.returncode == 3 and "already using the workspace" in code.stderr
        assert not (tmp_path / "ledgerspy.db").exists()
    finally:
        first.release()
    WorkspaceLock(tmp_path).acquire().release()  # released locks can be taken again


def test_port_in_use_is_reported_before_startup(tmp_path, capsys):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    port = s.getsockname()[1]
    try:
        assert cli.main(["serve", "--no-browser", "--workspace", str(tmp_path), "--port", str(port)]) == 3
        assert "already in use" in capsys.readouterr().err
        assert not (tmp_path / "ledgerspy.db").exists()
    finally:
        s.close()


def test_a_newer_database_stops_startup_cleanly(tmp_path, capsys):
    c = sqlite3.connect(tmp_path / "ledgerspy.db")
    c.execute("CREATE TABLE schema_version (version INTEGER, description TEXT)")
    c.execute("INSERT INTO schema_version VALUES (?, 'future')", (SCHEMA_VERSION + 1,))
    c.commit()
    c.close()
    assert cli.main(["serve", "--no-browser", "--workspace", str(tmp_path), "--port", "8797"]) == 2
    assert "newer than this LedgerSpy" in capsys.readouterr().err
    WorkspaceLock(tmp_path).acquire().release()  # the lock was released on the error path


# ------------------------------------------------------------------------------------------- migrations


def _v8(path: Path) -> None:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("BEGIN")
    for version, desc, statements in MIGRATIONS[:8]:
        for s in statements:
            conn.execute(s)
        conn.execute("INSERT INTO schema_version (version, description) VALUES (?, ?)", (version, desc))
    conn.execute(
        "INSERT INTO cases (case_id, name, status, created_at, created_by) VALUES ('C-1', 'kept', 'open', "
        "'t', 'a')"
    )
    conn.execute("COMMIT")
    conn.close()


def test_upgrading_an_existing_workspace_backs_it_up_first(tmp_path):
    db = tmp_path / "ledgerspy.db"
    _v8(db)
    assert init_db(db, "tester") == SCHEMA_VERSION
    backups = list((tmp_path / "backups").glob(f"ledgerspy-v8-before-v{SCHEMA_VERSION}-*.db"))
    assert len(backups) == 1 and not os.access(backups[0], os.W_OK)
    b = sqlite3.connect(backups[0])
    assert b.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 8
    assert b.execute("SELECT name FROM cases").fetchone()[0] == "kept"
    b.close()
    c = connect(db)
    assert c.execute("SELECT name FROM cases").fetchone()[0] == "kept"
    c.close()
    assert init_db(db, "tester") == SCHEMA_VERSION  # current: no second backup
    assert len(list((tmp_path / "backups").iterdir())) == 1
    os.chmod(backups[0], stat.S_IREAD | stat.S_IWRITE)


def test_a_fresh_workspace_needs_no_backup(tmp_path):
    init_db(tmp_path / "ledgerspy.db", "tester")
    assert not (tmp_path / "backups").exists()


# ------------------------------------------------------------------------------------------- lock file


def test_requirements_lock_is_complete_hashed_and_matches_the_tested_environment():
    text = (REPO / "requirements.lock").read_text(encoding="utf-8")
    entries = re.findall(r"^([A-Za-z0-9_.-]+)==(\S+) \\\n\s+--hash=sha256:([0-9a-f]{64})$", text, re.M)
    names = [line for line in text.splitlines() if line and not line.startswith(("#", " "))]
    assert len(entries) == len(names) >= 19  # every pinned line carries a hash

    def norm(n: str) -> str:
        return re.sub(r"[-_.]+", "-", n).lower()

    lock = {norm(n): v for n, v in locked_dependencies().items()}
    for name, version in pinned_dependencies().items():  # direct pins agree with pyproject.toml
        assert lock[norm(name)] == version, name
    for name, version in lock.items():  # and the whole closure is what the suite runs with
        assert importlib.metadata.version(name) == version, name


# ------------------------------------------------------------------------------------------- bundle


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    out = tmp_path_factory.mktemp("dist")
    path, info = build_bundle.build(None, out)
    return path, info


def test_bundle_contains_only_the_application_and_its_metadata(bundle):
    path, info = bundle
    with zipfile.ZipFile(path) as z:
        names = [n.split("/", 1)[1] for n in z.namelist()]
        files = {n.split("/", 1)[1]: z.read(n) for n in z.namelist()}
    assert {
        "install.cmd",
        "ledgerspy.cmd",
        "doctor.cmd",
        "INSTALL.md",
        "requirements.lock",
        "BUILD-INFO.json",
        "MANIFEST.sha256",
        "frontend/index.html",
        "config/default.yaml",
        "ledgerspy/runtime.py",
        "ledgerspy/features/registry_lock.json",
        "data/synthetic/reference/SYNTHETIC_ledger.csv",
    } <= set(names)
    forbidden = re.compile(
        r"(^tests/|^archive/|^packaging/|^\.claude|ground_truth|__pycache__|\.pyc$|\.db$|"
        r"^workspace|^data/raw|^docs/audits|\.venv|^dist/)"
    )
    assert [n for n in names if forbidden.search(n)] == []
    manifest = dict(reversed(line.split("  ", 1)) for line in files["MANIFEST.sha256"].decode().splitlines())
    assert set(manifest) == set(names) - {"MANIFEST.sha256"}
    assert all(hashlib.sha256(files[n]).hexdigest() == h for n, h in manifest.items())
    assert (
        info["target"].startswith("Windows x64, CPython 3.12")
        and "not a standalone executable" in info["note"]
    )
    for cmd in ("install.cmd", "ledgerspy.cmd", "doctor.cmd"):
        assert b"\r\n" in files[cmd] and b"\n" not in files[cmd].replace(b"\r\n", b"")  # CRLF batch files
    install = files["install.cmd"].decode()
    assert "--no-index" in install and "--require-hashes" in install and "PIP_NO_INDEX=1" in install
    assert (
        "--workspace" in files["ledgerspy.cmd"].decode() and "LOCALAPPDATA" in files["ledgerspy.cmd"].decode()
    )


def test_bundle_is_reproducible(bundle, tmp_path):
    path, _ = bundle
    again, _ = build_bundle.build(None, tmp_path)
    assert hashlib.sha256(again.read_bytes()).hexdigest() == hashlib.sha256(path.read_bytes()).hexdigest()


def test_bundle_scan_refuses_secrets_paths_and_data():
    for files, what in (
        (
            {"docs/x.md": b"open http://127.0.0.1:8765/?token=AbCdEfGhIjKlMnOpQrStUvWxYz012345"},
            "session token",
        ),
        ({"docs/x.md": b"see C:\\Users\\alice\\Documents\\ledger.csv"}, "user-specific Windows path"),
        ({"docs/x.md": b"see /home/alice/ledger.csv"}, "user-specific Unix path"),
        ({"workspace/ledgerspy.db": b"SQLite"}, "forbidden file"),
        ({"ledgerspy/__pycache__/x.pyc": b""}, "forbidden file"),
        ({"data/synthetic/reference/ground_truth/labels.json": b"{}"}, "forbidden file"),
    ):
        with pytest.raises(build_bundle.BuildError, match=what):
            build_bundle.scan(files)
    build_bundle.scan({"INSTALL.md": b"%LOCALAPPDATA%\\LedgerSpy and C:\\Program Files\\Tesseract-OCR"})


def test_bundle_refuses_a_wheel_that_does_not_match_the_lock(tmp_path):
    (tmp_path / "fastapi-0.142.2-py3-none-any.whl").write_bytes(b"not the locked wheel")
    with pytest.raises(build_bundle.BuildError, match="does not match requirements.lock"):
        build_bundle.collect(tmp_path)


# ------------------------------------------------------------------------------------------- offline guard


def test_the_offline_checks_network_guard_blocks_and_logs(tmp_path):
    log = tmp_path / "net.log"
    code = (
        "import socket\n"
        "s = socket.socket(); s.settimeout(2)\n"
        "try:\n    s.connect(('10.255.255.1', 80)); print('CONNECTED')\nexcept OSError as e: "
        "print('BLOCKED', e)\ntry:\n    socket.getaddrinfo('pypi.org', 443); print('RESOLVED')\nexcept "
        "OSError as e: print('BLOCKED-DNS')\nsrv = socket.socket(); srv.bind(('127.0.0.1', 0)); "
        "srv.listen()\nc = socket.socket(); c.connect(srv.getsockname()); print('LOOPBACK-OK')\n"
    )
    env = {**os.environ, "PYTHONPATH": str(REPO / "packaging" / "netguard"), "LEDGERSPY_NETLOG": str(log)}
    out = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60, check=False
    ).stdout
    assert "BLOCKED network disabled" in out and "BLOCKED-DNS" in out and "LOOPBACK-OK" in out
    assert "CONNECTED" not in out and "RESOLVED" not in out
    logged = log.read_text(encoding="utf-8")
    assert "10.255.255.1" in logged and "pypi.org" in logged
