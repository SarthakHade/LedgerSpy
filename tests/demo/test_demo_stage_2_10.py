"""Stage 2.10: the four blockers of the Stage 2.9 review, and the offline inference newline regression.

B1 the real start-up sequence works and every entry point applies the same configuration; B2 only
Content-Length framing reaches a workspace (tested through a real loopback HTTP server) and file growth is
reconciled apart from the database; B3 the rollback journal stays within its bound however small the
page cache; B4 one process owns a data folder, and a second one fails closed without touching the first
one's files.
"""

import asyncio
import contextlib
import json
import os
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from ledgerspy import storage_limits
from ledgerspy.cases.db import connect, transaction
from ledgerspy.paths import REPO_ROOT
from ledgerspy.supervised import inference
from ledgerspy_demo.allowances import PDF_WORKER_FSIZE_ENV, journal_bytes, pdf_render_bytes
from ledgerspy_demo.app import build_demo_app, configure_process, release_process
from ledgerspy_demo.datadir import DataDirError, DataDirLock, prepare_data_dir
from ledgerspy_demo.frontdoor import FrontDoor
from ledgerspy_demo.frontend import build_demo_frontend
from ledgerspy_demo.settings import DemoSettings
from ledgerspy_demo.workspace import WorkspaceManager, tree_bytes
from tests.demo.conftest import demo_env
from tests.demo.test_demo_concurrency_limits import code, visitor_with_gate

LEDGER = REPO_ROOT / "data" / "synthetic" / "reference" / "SYNTHETIC_ledger.csv"


@pytest.fixture
def process_env(monkeypatch):
    """Configuring the demo changes process-wide state (environment, tempfile, the lock): restored after."""
    import tempfile as tf

    names = ("TMPDIR", "TEMP", "TMP", PDF_WORKER_FSIZE_ENV)
    saved = {name: os.environ.get(name) for name in names}  # restored exactly, including absence
    monkeypatch.setattr(tf, "tempdir", None)
    try:
        yield
    finally:
        release_process()
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


# ------------------------------------------------------------------------------------------ B1: start-up


def test_the_entry_point_sequence_builds_the_demo_with_one_configuration(tmp_path, seed_dir, process_env):
    settings = DemoSettings.from_env(demo_env(tmp_path / "data"))
    door = build_demo_app(
        settings, seed_dir=seed_dir
    )  # what __main__ calls; it configures the process itself
    assert isinstance(door, FrontDoor)
    process = configure_process(settings)  # idempotent for the same folder
    data = (tmp_path / "data").resolve()
    assert process.data_dir == data and process.lock.held
    assert Path(tempfile.gettempdir()).resolve().parent == data / "tmp"  # a marked run folder
    assert (
        Path(process.os_temp_dir).resolve() != Path(tempfile.gettempdir()).resolve()
    )  # checked before redirect
    assert os.environ[PDF_WORKER_FSIZE_ENV] == str(pdf_render_bytes(settings.limits))
    with pytest.raises(DataDirError, match="already runs the demo"):
        configure_process(DemoSettings.from_env(demo_env(tmp_path / "other")))
    release_process()
    assert not process.lock.held


def test_main_fails_closed_and_releases_everything_on_an_unsafe_folder(tmp_path, monkeypatch, process_env):
    from ledgerspy_demo import __main__ as entry

    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "precious.txt").write_text("keep")
    for k, v in demo_env(busy).items():
        monkeypatch.setenv(k, v)
    assert entry.main() == 2
    assert (busy / "precious.txt").read_text() == "keep" and not (busy / "tmp").exists()
    from ledgerspy_demo import app as app_module

    assert app_module._PROCESS is None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http(port: int, request: bytes, timeout: float = 60) -> tuple[int, dict, bytes]:
    """One raw HTTP/1.1 exchange (Connection: close), as a client would send it."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall(request)
        data = b""
        while chunk := s.recv(65536):
            data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1]) if lines and lines[0].startswith("HTTP/") else 0
    hdrs = {}
    for line in lines[1:]:
        k, _, v = line.partition(":")
        hdrs.setdefault(k.strip().lower(), v.strip())
    return status, hdrs, body


def test_the_real_demo_process_starts_serves_and_exits_cleanly(tmp_path):
    """`python -m ledgerspy_demo` with an isolated data folder: it serves, stops on a signal with status 0,
    and leaves the data folder unlocked."""
    port = _free_port()
    data = tmp_path / "data"
    env = {**os.environ, **demo_env(data)}
    env["LEDGERSPY_DEMO_PUBLIC_ORIGIN"] = f"http://localhost:{port}"
    env["LEDGERSPY_DEMO_PORT"] = str(port)
    env["PYTHONPATH"] = str(REPO_ROOT)
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "ledgerspy_demo"], cwd=REPO_ROOT, env=env, creationflags=flags,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )  # fmt: skip
    try:
        status = None
        deadline = time.monotonic() + 240  # seed build included; a readiness poll, not a correctness wait
        while time.monotonic() < deadline and proc.poll() is None:
            try:
                status, _h, body = _http(port, f"GET /demo/status HTTP/1.1\r\nHost: localhost:{port}\r\n"
                                         "Connection: close\r\n\r\n".encode(), timeout=5)  # fmt: skip
                break
            except OSError:
                time.sleep(0.2)
        assert status == 200, (
            proc.stdout.read().decode(errors="replace") if proc.poll() is not None else status
        )
        assert json.loads(body)["synthetic_data_only"] is True
        host = f"Host: localhost:{port}\r\nConnection: close\r\n"
        # Stage 2.11: the landing page allocates nothing; a same-origin POST creates the workspace
        status, hdrs, body = _http(port, f"GET / HTTP/1.1\r\n{host}\r\n".encode())
        assert status == 200 and b"synthetic data only" in body and "set-cookie" not in hdrs
        assert not (data / "workspaces").exists() or not any((data / "workspaces").iterdir())
        status, hdrs, _ = _http(
            port,
            f"POST /demo/workspace/new HTTP/1.1\r\n{host}Origin: http://localhost:{port}\r\n"
            "Content-Length: 0\r\n\r\n".encode(),
        )
        assert status == 303 and hdrs["location"] == "/"
        cookie = hdrs["set-cookie"].split(";")[0]
        status, _h, body = _http(port, f"GET / HTTP/1.1\r\n{host}Cookie: {cookie}\r\n\r\n".encode())
        token = re.search(rb'name="ledgerspy-session-token" content="([^"]+)"', body).group(1).decode()
        assert status == 200
        status, _h, _b = _http(port, f"GET /api/datasets HTTP/1.1\r\n{host}Cookie: {cookie}\r\n\r\n".encode())
        assert status == 401  # the workspace app's own token check
        auth = f"Cookie: {cookie}\r\nAuthorization: Bearer {token}\r\n"
        status, _h, body = _http(port, f"GET /api/datasets HTTP/1.1\r\n{host}{auth}\r\n".encode())
        case = json.loads(body)["items"][0]["case_id"]
        too_big = 3 * 1024 * 1024
        status, _h, body = _http(  # refused on the declared size alone, before any body is read
            port,
            f"POST /api/cases/{case}/files?filename=x.csv HTTP/1.1\r\n{host}{auth}"
            f"Content-Length: {too_big}\r\n\r\n".encode(),
        )
        assert status == 413 and json.loads(body)["error"]["code"] == "file_too_large"
        with pytest.raises(DataDirError):  # the running process owns the folder
            DataDirLock(data.resolve()).acquire()
        proc.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGTERM)
        assert proc.wait(timeout=60) == 0, proc.stdout.read().decode(errors="replace")
        lock = DataDirLock(data.resolve())
        lock.acquire()  # released on exit
        lock.release()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)


# ------------------------------------------------------------------------------------------ B2: framing


class LoopbackDemo:
    """The production front door and real workspace apps behind a real uvicorn server on 127.0.0.1."""

    def __init__(self, tmp_path: Path, seed_dir: Path) -> None:
        self.port = _free_port()
        env = demo_env(tmp_path / "loop", requests_per_minute=500)
        env["LEDGERSPY_DEMO_PUBLIC_ORIGIN"] = f"http://localhost:{self.port}"
        (tmp_path / "loop").mkdir()
        self.settings = DemoSettings.from_env(env)
        frontend = build_demo_frontend(tmp_path / "loop" / "frontend", 7, False)
        self.manager = WorkspaceManager(self.settings, seed_dir, frontend)
        self.door = FrontDoor(self.settings, self.manager)
        self.server = uvicorn.Server(
            uvicorn.Config(self.door, host="127.0.0.1", port=self.port, log_level="error", lifespan="on")
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.monotonic() + 60
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert self.server.started
        status, hdrs, _ = self.request(  # the explicit creation (Stage 2.11)
            "POST", "/demo/workspace/new", f"Origin: http://localhost:{self.port}\r\nContent-Length: 0\r\n"
        )
        assert status == 303, status
        self.cookie = re.search(r"ls_demo_ws=([^;]+)", hdrs["set-cookie"]).group(1)
        status, hdrs, body = self.request("GET", "/")
        assert status == 200
        self.token = re.search(rb'name="ledgerspy-session-token" content="([^"]+)"', body).group(1).decode()
        self.ws = self.manager._resident[self.cookie.split(".")[0]]
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=30)

    def request(self, method: str, path: str, extra: str = "", body: bytes = b"", auth: bool = True):
        h = f"{method} {path} HTTP/1.1\r\nHost: localhost:{self.port}\r\nConnection: close\r\n"
        if getattr(self, "cookie", None) and auth:
            h += f"Cookie: ls_demo_ws={self.cookie}\r\n"
            if getattr(self, "token", None):
                h += f"Authorization: Bearer {self.token}\r\n"
        return _http(self.port, (h + extra + "\r\n").encode() + body)


def _spy(ws) -> list:
    calls, real = [], ws.app

    async def spy(scope, receive, send):
        calls.append(scope["path"])
        await real(scope, receive, send)

    ws.app = spy
    return calls


def _chunked(data: bytes) -> bytes:
    return b"%x\r\n" % len(data) + data + b"\r\n0\r\n\r\n"


def test_chunked_or_conflicting_framing_never_reaches_a_workspace(tmp_path, seed_dir):
    with LoopbackDemo(tmp_path, seed_dir) as demo:
        status, _h, body = demo.request(
            "POST", "/api/cases", "Content-Type: application/json\r\nContent-Length: 13\r\n", b'{"name": "x"}'
        )
        case = json.loads(body)["case_id"]
        calls = _spy(demo.ws)
        before = tree_bytes(demo.ws.path)
        upload = f"/api/cases/{case}/files?filename=f.csv"
        # The front door refuses on the headers alone, so only the headers are sent: a server that answers and
        # closes while a client is still sending a body may reset the connection before the answer is read.
        for framing in (
            "Transfer-Encoding: chunked\r\n",  # chunked alone
            "Content-Length: 100\r\nTransfer-Encoding: chunked\r\n",  # conflicting (h11 would use chunked)
            "Transfer-Encoding: gzip, chunked\r\n",
        ):
            status, _h, body = demo.request("POST", upload, framing)
            assert status == 400
            if body.startswith(b"{"):  # by the front door (an encoding h11 rejects never gets that far)
                assert json.loads(body)["error"]["code"] == "transfer_encoding_not_supported"
        status, _h, _b = demo.request(
            "POST", "/api/cases", "Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n"
        )
        assert status == 400
        assert calls == [] and tree_bytes(demo.ws.path) == before  # nothing reached the workspace app


def test_a_body_longer_than_its_content_length_is_cut_at_the_declared_size(tmp_path, seed_dir):
    with LoopbackDemo(tmp_path, seed_dir) as demo:
        status, _h, body = demo.request(
            "POST", "/api/cases", "Content-Type: application/json\r\nContent-Length: 13\r\n", b'{"name": "x"}'
        )
        case = json.loads(body)["case_id"]
        evidence = demo.ws.path / "evidence"
        known = {p.name for p in evidence.iterdir()}
        csv = LEDGER.read_bytes()[:100_000]
        # the server may answer after 300 bytes and close with the rest unread (Linux then resets)
        with contextlib.suppress(ConnectionResetError):
            demo.request("POST", f"/api/cases/{case}/files?filename=cut.csv", "Content-Length: 300\r\n", csv)
        new = [p for p in evidence.iterdir() if p.name not in known]
        assert all(p.stat().st_size <= 300 for p in new)  # never more than declared was stored


def test_an_ordinary_upload_still_works_through_the_real_server(tmp_path, seed_dir):
    with LoopbackDemo(tmp_path, seed_dir) as demo:
        status, _h, body = demo.request(
            "POST", "/api/cases", "Content-Type: application/json\r\nContent-Length: 13\r\n", b'{"name": "x"}'
        )
        case = json.loads(body)["case_id"]
        lines = LEDGER.read_text(encoding="utf-8").splitlines(keepends=True)[:501]
        csv = "".join(lines).replace("Team lunch", "Team lunch ok", 1).encode()
        status, _h, body = demo.request(
            "POST", f"/api/cases/{case}/files?filename=ok.csv", f"Content-Length: {len(csv)}\r\n", csv
        )
        assert status == 201, body[:300]
        assert demo.manager.overshoots == 0 and demo.ws.activity.storage_blocked is None


def test_file_growth_is_reconciled_apart_from_the_database_allowance(make_demo):
    d = make_demo()

    async def go():
        v, ws, _gate = await visitor_with_gate(d, 120, block=False)

        async def writes_files_within_the_db_allowance(scope, receive, send):
            await receive()
            (ws.path / "extra.bin").write_bytes(
                b"x" * 300_000
            )  # far below the room, but a DB_WRITE has 0 files
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        ws.app = writes_files_within_the_db_allowance
        assert (await v.call("POST", "/api/cases", json_body={"name": "x"})).status == 200
        assert d.manager.overshoots == 1 and "files +" in ws.activity.storage_blocked
        r = await v.call("POST", "/api/cases", json_body={"name": "y"})
        assert r.status == 507 and code(r) == "workspace_storage_overshoot"

    asyncio.run(go())


# ------------------------------------------------------------------------------------------ B3: journal


def _large_db(path: Path, rows: int = 2500) -> None:
    c = sqlite3.connect(path, isolation_level=None)
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v BLOB)")
    c.execute("BEGIN")
    for i in range(rows):
        c.execute("INSERT INTO t VALUES (?, ?)", (i, os.urandom(3000)))
    c.execute("COMMIT")
    c.close()


def _journal_before_commit(conn, journal: Path, statement: str) -> int:
    conn.execute("BEGIN")
    conn.execute(statement)
    size = journal.stat().st_size if journal.exists() else 0
    conn.execute("ROLLBACK")
    return size


def test_the_rollback_journal_stays_within_its_bound_with_a_tiny_page_cache(tmp_path):
    db = tmp_path / "w.db"
    _large_db(db)
    size = db.stat().st_size
    journal = tmp_path / "w.db-journal"
    raw = sqlite3.connect(db, isolation_level=None)  # without the demo limit: the offline default
    try:
        assert raw.execute("PRAGMA cache_spill").fetchone()[0] != 0  # offline default unchanged (spill on)
        raw.execute("PRAGMA cache_size = 5")
        spilled = _journal_before_commit(raw, journal, "UPDATE t SET v = randomblob(3000)")
    finally:
        raw.close()
    cap = size + 64 * 1024
    bound = journal_bytes(cap, 4096)
    assert spilled > journal_bytes(size, 4096)  # why: each spill adds a sector-sized journal header
    storage_limits.set_db_max_bytes(db, cap)
    try:
        c = connect(db)
        assert c.execute("PRAGMA cache_spill").fetchone()[0] == 0
        c.execute("PRAGMA cache_size = 5")
        assert _journal_before_commit(c, journal, "UPDATE t SET v = randomblob(3000)") <= bound
        # SQLite still refuses growth past the cap, and the transaction is rolled back with no journal left
        with pytest.raises(sqlite3.OperationalError), transaction(c):
            for _ in range(200):
                c.execute("INSERT INTO t (v) VALUES (randomblob(3000))")
        assert c.execute("SELECT count(*) FROM t").fetchone()[0] == 2500
        assert db.stat().st_size <= cap and not journal.exists()
        c.close()
    finally:
        storage_limits.set_db_max_bytes(db, None)


# ------------------------------------------------------------------------------------------ B4: one owner

OWNER_SCRIPT = r"""
import os, sys
from ledgerspy_demo.app import configure_process
from ledgerspy_demo.datadir import DataDirError
from ledgerspy_demo.settings import DemoSettings
try:
    process = configure_process(DemoSettings.from_env(os.environ))
except DataDirError as exc:
    print("REFUSED", exc, flush=True)
    sys.exit(3)
(process.temp.run_dir / "live.txt").write_text("in use")
print("OWNER", process.temp.run_dir, flush=True)
sys.stdin.readline()  # keep ownership until told to stop
"""


def _start_owner(data: Path) -> subprocess.Popen:
    env = {**os.environ, **demo_env(data), "PYTHONPATH": str(REPO_ROOT)}
    for name in ("TMPDIR", "TEMP", "TMP"):  # each process starts from the normal OS temporary folder
        env[name] = tempfile.gettempdir()
    return subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", OWNER_SCRIPT], cwd=REPO_ROOT, env=env, text=True,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )  # fmt: skip


def test_exactly_one_process_owns_a_data_folder_and_the_other_touches_nothing(tmp_path):
    data = tmp_path / "data"
    prepare_data_dir(str(data))  # an existing demo folder; both processes start at once
    first, second = _start_owner(data), _start_owner(data)
    procs = [first, second]
    try:
        lines = [p.stdout.readline().strip() for p in procs]
        owners = [p for p, line in zip(procs, lines, strict=True) if line.startswith("OWNER")]
        losers = [p for p, line in zip(procs, lines, strict=True) if line.startswith("REFUSED")]
        assert len(owners) == 1 and len(losers) == 1, (lines, [p.stderr.read() for p in procs if p.poll()])
        assert losers[0].wait(timeout=60) == 3
        run = Path(next(line for line in lines if line.startswith("OWNER")).split(" ", 1)[1])
        assert (run / "live.txt").read_text() == "in use"  # the loser deleted and changed nothing
        late = _start_owner(data)  # still owned: refused again, deterministically
        assert late.stdout.readline().startswith("REFUSED") and late.wait(timeout=60) == 3
        assert (run / "live.txt").read_text() == "in use"
        owners[0].stdin.write("\n")
        owners[0].stdin.flush()
        assert owners[0].wait(timeout=60) == 0
        nxt = _start_owner(data)  # released on exit: a new owner, which may now clear the earlier run
        procs.append(nxt)
        assert nxt.stdout.readline().startswith("OWNER")
        nxt.stdin.write("\n")
        nxt.stdin.flush()
        assert nxt.wait(timeout=60) == 0
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait(timeout=30)


def test_the_lock_is_released_when_configuration_fails_after_taking_it(tmp_path, monkeypatch, process_env):
    from ledgerspy_demo import datadir

    def broken_start(self, set_environment=True):
        raise DataDirError("simulated failure while preparing temporary files")

    monkeypatch.setattr(datadir.DemoTempFiles, "start", broken_start)
    settings = DemoSettings.from_env(demo_env(tmp_path / "data"))
    with pytest.raises(DataDirError, match="simulated"):
        configure_process(settings)
    lock = DataDirLock((tmp_path / "data").resolve())
    lock.acquire()  # nothing kept it
    lock.release()


# ------------------------------------------------------------------------------------------ inference newline


def _result(iid: str) -> dict:
    return {"metadata": {"inference_id": iid, "note": "naïve – ünïcode"}, "rows": [{"a": 1}, {"b": [1, 2]}]}


def test_inference_results_are_written_exactly_as_before_offline(tmp_path):
    result = _result("I-0000000000000001")
    inference._write(tmp_path, result)  # no limit registered: the offline path
    expected = tmp_path / "expected.json"
    expected.write_text(
        json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8"
    )  # the original code
    written = (tmp_path / "inference" / "I-0000000000000001.json").read_bytes()
    assert written == expected.read_bytes()
    if sys.platform == "win32":
        assert b"\r\n" in written  # text-mode newline translation kept on Windows


def test_the_inference_claim_equals_the_bytes_written(tmp_path):
    result = _result("I-0000000000000002")
    expected = tmp_path / "expected.json"
    expected.write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    size = expected.stat().st_size
    try:
        storage_limits.set_output_allowance(tmp_path, size - 1)
        with pytest.raises(Exception, match="nothing was written"):
            inference._write(tmp_path, result)
        assert not (tmp_path / "inference" / "I-0000000000000002.json").exists()
        storage_limits.set_output_allowance(tmp_path, size)  # exactly the bytes written: accepted
        inference._write(tmp_path, result)
        assert (tmp_path / "inference" / "I-0000000000000002.json").stat().st_size == size
    finally:
        storage_limits.set_output_allowance(tmp_path, None)
