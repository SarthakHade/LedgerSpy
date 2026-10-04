import http.client
import re
import sqlite3
import subprocess
import sys
import time

from ledgerspy.cases import store
from ledgerspy.cases.db import connect
from ledgerspy.cli import main
from ledgerspy.paths import REPO_ROOT
from tests.backend.conftest import free_loopback_port


def test_audit_verify_succeeds_on_clean_db(db_path, capsys):
    assert main(["audit", "verify", "--db", str(db_path)]) == 0
    assert capsys.readouterr().out.startswith("OK:")


def test_audit_verify_uses_the_configured_workspace(tmp_path, db_path, capsys):
    assert main(["audit", "verify", "--workspace", str(tmp_path / "ws")]) == 0


def test_audit_verify_fails_after_manual_edit(db_path, capsys):
    conn = connect(db_path)
    store.create_case(conn, name="Acme", actor="tester")
    conn.close()
    assert main(["audit", "verify", "--db", str(db_path)]) == 0

    edit = sqlite3.connect(db_path, isolation_level=None)
    edit.execute("DROP TRIGGER audit_log_no_update")
    edit.execute(
        "UPDATE audit_log SET details = replace(details, 'Acme', 'Acme2') WHERE action = 'case.created'"
    )
    edit.close()
    capsys.readouterr()
    assert main(["audit", "verify", "--db", str(db_path)]) == 1
    assert capsys.readouterr().out.startswith("FAIL:")


def test_audit_verify_missing_db(tmp_path, capsys):
    assert main(["audit", "verify", "--db", str(tmp_path / "none.db")]) == 2
    assert not (tmp_path / "none.db").exists()


def test_audit_verify_on_a_non_ledgerspy_db(tmp_path):
    path = tmp_path / "other.db"
    sqlite3.connect(path).close()
    assert main(["audit", "verify", "--db", str(path)]) == 1


def test_bad_config_exits_with_error(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("config_version: 1\n", encoding="utf-8")
    assert main(["audit", "verify", "--config", str(bad)]) == 2
    assert "configuration" in capsys.readouterr().err


def test_serve_starts_and_serves_the_ui(tmp_path):
    port = free_loopback_port()
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
            str(tmp_path / "ws"),
        ],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        url = None
        deadline = time.time() + 30
        while time.time() < deadline and url is None:
            line = proc.stdout.readline()
            if not line:
                break
            m = re.search(r"Open: (http://127\.0\.0\.1:\d+/\?token=\S+)", line)
            url = m.group(1) if m else None
        assert url, "serve did not print its URL"
        token = url.split("token=", 1)[1]

        def get(path, headers=None):
            for _ in range(100):
                try:
                    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    c.request("GET", path, headers=headers or {})
                    r = c.getresponse()
                    return r.status, dict(r.getheaders()), r.read().decode("utf-8")
                except ConnectionRefusedError:
                    time.sleep(0.1)
            raise AssertionError("server never accepted connections")

        status, headers, _ = get(f"/?token={token}")
        assert status == 303
        cookie = headers["set-cookie"].split(";", 1)[0]
        status, _, body = get("/", {"Cookie": cookie})
        assert status == 200 and 'id="file-input"' in body
        assert get("/api/cases/C-01J00000000000000000000000")[0] == 401
        assert (tmp_path / "ws" / "ledgerspy.db").is_file()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
