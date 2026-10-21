"""Stage 2.8: enforced disk-growth bounds for every request (V1) and safe data-folder cleanup (V5).

Concurrency is forced with events (the `Gate` stand-in) or observed with a sampler thread that can only miss a
peak, never invent one; the real workspace app is used wherever the bound itself is under test.
"""

import asyncio
import dataclasses
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from ledgerspy import storage_limits
from ledgerspy.cases.db import connect, transaction
from ledgerspy.config import load_config
from ledgerspy.paths import REPO_ROOT
from ledgerspy_demo import allowances
from ledgerspy_demo.allowances import (
    MUTATING,
    OperationClass,
    classify,
    journal_bytes,
    pdf_render_bytes,
    plan,
)
from ledgerspy_demo.datadir import (
    DATA_MARKER,
    RUN_MARKER,
    TMP_MARKER,
    DataDirError,
    DemoTempFiles,
    check_data_dir_setting,
    prepare_data_dir,
)
from ledgerspy_demo.limits import MIB, DemoLimits
from ledgerspy_demo.settings import DemoSettings
from ledgerspy_demo.workspace import tree_bytes
from tests.demo.conftest import Visitor, demo_env
from tests.demo.test_demo_concurrency_limits import code, entered, visitor_with_gate

LEDGER = REPO_ROOT / "data" / "synthetic" / "reference" / "SYNTHETIC_ledger.csv"


def run(coro):
    return asyncio.run(coro)


def ledger_csv(rows: int, tag: str) -> bytes:
    """`rows` synthetic ledger rows with fresh transaction IDs (distinct content per tag)."""
    lines = LEDGER.read_text(encoding="utf-8").splitlines()
    out = [lines[0]]
    for i, line in enumerate(lines[1 : rows + 1]):
        cells = line.split(",")
        cells[0] = f"LVC-{tag}-{i:06d}"
        out.append(",".join(cells))
    return ("\n".join(out) + "\n").encode()


def with_limits(d, **changes) -> None:
    d.manager.limits = d.door.limits = dataclasses.replace(d.manager.limits, **changes)


def ws_of(d, v):
    return d.manager._resident[v.cookie.split(".")[0]]


async def real_visitor(d, octet: int) -> Visitor:
    v = Visitor(d.door, client=(f"198.51.100.{octet}", 1))
    assert (await v.open()).status == 200
    return v


async def seed_case(v: Visitor) -> str:
    return (await v.call("GET", "/api/datasets")).json()["items"][0]["case_id"]


async def upload_and_map(v: Visitor, case: str, body: bytes, name: str):
    r = await v.call("POST", f"/api/cases/{case}/files?filename={name}", body=body)
    assert r.status == 201, r.body[:300]
    ds = r.json()
    return ds["dataset_id"], ds["mapping_suggestion"]["proposed_mapping"]


def sample_path(route_path: str) -> str:
    return route_path.replace("{", "").replace("}", "")


# ----------------------------------------------------------------------------- classification and allowances


def test_every_state_changing_route_of_the_workspace_app_has_an_explicit_bound(make_demo):
    d = make_demo()

    async def go():
        v = await real_visitor(d, 70)
        app = ws_of(d, v).app
        seen = 0
        explicit = [(m, p) for m, p, _c in allowances.STATE_CHANGING]
        for path, methods in app.openapi()["paths"].items():  # every API route of the workspace app
            for method in {m.upper() for m in methods} & MUTATING:
                seen += 1
                assert classify(method, sample_path(path)) is not None, (method, path)
                assert any(m == method and re.fullmatch(p, sample_path(path)) for m, p in explicit)
        assert seen == len(allowances.STATE_CHANGING)  # nothing extra, nothing missing
        # a state-changing request without a known bound is refused before it reaches the workspace
        r = await v.call("DELETE", "/api/cases/C-0")
        assert r.status == 403 and code(r) == "operation_not_available"

    run(go())


LIM = DemoLimits()
PAGE = 4096


@pytest.mark.parametrize(
    "op, query, declared, files, renders",
    [
        (allowances.DB_WRITE, {}, 0, 0, False),
        (allowances.UPLOAD, {"kind": "ledger"}, 300_000, 300_000, False),
        (allowances.UPLOAD, {"kind": "bank_statement"}, 300_000, 300_000, True),  # a PDF may be extracted
        (allowances.PDF_EXTRACT, {}, 0, 0, True),
        (allowances.REPORT, {}, 0, LIM.max_report_bytes, False),
        (allowances.INFERENCE, {}, 0, LIM.max_inference_bytes, False),
    ],
)
def test_each_state_changing_class_reserves_its_full_enforced_bound(op, query, declared, files, renders):
    ws_bytes, db_bytes = 10 * MIB, 7 * MIB
    p = plan(op, LIM, ws_bytes, db_bytes, PAGE, declared, query)
    room = LIM.max_workspace_bytes - ws_bytes
    assert p.files == files and p.db_growth == room - files and p.db_cap == db_bytes + room - files
    assert p.journal == journal_bytes(p.db_cap, PAGE) >= p.db_cap  # every page that existed, journaled once
    assert p.render == (pdf_render_bytes(LIM) if renders else 0)
    assert p.persistent == room  # the workspace cannot pass its quota
    assert p.total == p.db_growth + p.journal + p.files + p.render


def test_reads_reserve_only_their_temporary_renders_and_cannot_grow_the_database():
    p = plan(allowances.READ, LIM, 10 * MIB, 7 * MIB, PAGE, 0, {})
    assert (p.db_growth, p.journal, p.files, p.render, p.db_cap) == (0, 0, 0, 0, 7 * MIB)
    p = plan(allowances.PDF_PAGE_IMAGE, LIM, 10 * MIB, 7 * MIB, PAGE, 0, {})
    assert p.total == p.render == pdf_render_bytes(LIM)
    with pytest.raises(allowances.InsufficientRoom):  # a file that does not fit is refused up front
        plan(allowances.UPLOAD, LIM, LIM.max_workspace_bytes - 1000, 7 * MIB, PAGE, 2000, {})


def test_the_database_cap_and_read_only_mode_are_enforced_by_sqlite(tmp_path):
    db = tmp_path / "w.db"
    c = connect(db)
    c.execute("CREATE TABLE t (v BLOB)")
    c.close()
    size = db.stat().st_size
    try:
        storage_limits.set_db_max_bytes(db, size + 16 * 1024)
        c = connect(db)
        with pytest.raises(sqlite3.OperationalError), transaction(c):
            for _ in range(50):
                c.execute("INSERT INTO t VALUES (?)", (os.urandom(4000),))
        assert c.execute("SELECT count(*) FROM t").fetchone()[0] == 0  # rolled back
        assert db.stat().st_size <= size + 16 * 1024 and not (tmp_path / "w.db-journal").exists()
        c.close()
        storage_limits.set_db_max_bytes(db, size, read_only=True)
        c = connect(db)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            c.execute("INSERT INTO t VALUES (x'00')")
        c.close()
    finally:
        storage_limits.set_db_max_bytes(db, None)


def test_offline_connections_and_defaults_are_unchanged(tmp_path):
    c = connect(tmp_path / "offline.db")  # nothing registered: the offline application's connection
    try:
        assert c.execute("PRAGMA max_page_count").fetchone()[0] > 10**9
        assert c.execute("PRAGMA temp_store").fetchone()[0] == 0
        assert c.execute("PRAGMA query_only").fetchone()[0] == 0
    finally:
        c.close()
    s = load_config().settings
    assert s.pdf.render.max_pixels == 40_000_000 and s.server.host == "127.0.0.1"
    assert os.environ.get(storage_limits.WORKER_FILE_LIMIT_ENV) is None  # no worker limit outside the demo


# ----------------------------------------------------------------------------- real workspace app bounds


def test_a_real_mapping_near_the_quota_is_stopped_at_the_database_cap(make_demo):
    d = make_demo(requests_per_minute=500)

    async def go():
        v = await real_visitor(d, 71)
        ws = ws_of(d, v)
        did, mapping = await upload_and_map(v, await seed_case(v), ledger_csv(2000, "NQ"), "near.csv")
        quota = tree_bytes(ws.path) + 48 * 1024  # mapping writes about 2.4 x the file into the database
        with_limits(d, max_workspace_bytes=max(quota, d.manager.limits.max_upload_bytes))
        r = await v.call("PUT", f"/api/datasets/{did}/mapping", json_body=mapping)
        assert r.status == 507 and code(r) == "storage_limit_reached", r.body[:300]
        assert tree_bytes(ws.path) <= d.manager.limits.max_workspace_bytes  # the quota held
        assert d.manager.budget.outstanding == 0 and not ws.lock.locked()
        assert (await v.call("GET", f"/api/datasets/{did}")).status == 200  # reads still work
        assert d.manager.overshoots == 0 and ws.activity.storage_blocked is None
        with_limits(d, max_workspace_bytes=DemoLimits().max_workspace_bytes)  # with room, it now succeeds
        assert (await v.call("PUT", f"/api/datasets/{did}/mapping", json_body=mapping)).status == 200

    run(go())


def test_a_report_larger_than_its_output_limit_is_refused_before_anything_is_written(make_demo):
    d = make_demo(max_report_bytes=8000, requests_per_minute=500)

    async def go():
        v = await real_visitor(d, 72)
        ws = ws_of(d, v)
        inv = await v.call(
            "POST", "/api/investigations", json_body={"title": "x", "case_id": await seed_case(v)}
        )
        assert inv.status == 201
        before = sorted((ws.path / "reports").glob("*")) if (ws.path / "reports").exists() else []
        r = await v.call(
            "POST",
            "/api/reports",
            json_body={"report_type": "audit_memo", "investigation_ids": [inv.json()["investigation_id"]]},
        )
        assert r.status == 507 and code(r) == "storage_limit", r.body[:300]
        after = sorted((ws.path / "reports").glob("*")) if (ws.path / "reports").exists() else []
        assert after == before  # no artifact written
        assert (await v.call("GET", "/api/reports")).json()["items"] == []  # and none registered
        assert d.manager.budget.outstanding == 0 and ws.activity.storage_blocked is None

    run(go())


def test_an_unexpected_overshoot_fails_closed_for_further_changes(make_demo):
    d = make_demo()

    async def go():
        v, ws, gate = await visitor_with_gate(d, 73, block=False)
        room = d.manager.workspace_room(ws)

        async def writes_too_much(scope, receive, send):  # far beyond anything its plan allows
            await receive()
            (ws.path / "rogue.bin").write_bytes(b"x" * (room + 256 * 1024))
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        ws.app = writes_too_much
        assert (await v.call("POST", "/api/cases", json_body={"name": "x"})).status == 200
        assert d.manager.overshoots == 1 and ws.activity.storage_blocked
        ws.app = gate
        r = await v.call("POST", "/api/cases", json_body={"name": "y"})
        assert r.status == 507 and code(r) == "workspace_storage_overshoot" and gate.calls == 0
        assert (await v.call("GET", "/api/datasets")).status == 201  # reads still work (the stand-in: 201)

    run(go())


class Sampler:
    """Measures the service's usage in a thread until stopped; it can miss a peak, never invent one."""

    def __init__(self, path: Path) -> None:
        self.path, self.peak, self._stop = path, 0, threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            self.peak = max(self.peak, tree_bytes(self.path))

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()


def _record_reconciliations(d, monkeypatch) -> list:
    seen = []
    real = d.door._reconcile

    def spy(job):
        seen.append((job.plan.op.name, tree_bytes(job.ws.path) - job.ws_before, job.plan.persistent))
        real(job)

    monkeypatch.setattr(d.door, "_reconcile", spy)
    return seen


async def _chain(v: Visitor, tag: str) -> list[int]:
    """Upload, mapping, integrity and an investigation report in one workspace (real app)."""
    statuses = []
    case = await seed_case(v)
    r = await v.call("POST", f"/api/cases/{case}/files?filename={tag}.csv", body=ledger_csv(1200, tag))
    statuses.append(r.status)
    if r.status != 201:
        return statuses
    ds = r.json()
    r = await v.call(
        "PUT",
        f"/api/datasets/{ds['dataset_id']}/mapping",
        json_body=ds["mapping_suggestion"]["proposed_mapping"],
    )
    statuses.append(r.status)
    r = await v.call("POST", f"/api/datasets/{ds['dataset_id']}/integrity")
    statuses.append(r.status)
    inv = await v.call("POST", "/api/investigations", json_body={"title": tag, "case_id": case})
    statuses.append(inv.status)
    if inv.status == 201:
        r = await v.call(
            "POST",
            "/api/reports",
            json_body={
                "report_type": "case_investigation",
                "investigation_ids": [inv.json()["investigation_id"]],
            },
        )
        statuses.append(r.status)
    return statuses


def test_concurrent_real_operations_in_several_workspaces_stay_within_their_bounds(make_demo, monkeypatch):
    d = make_demo(requests_per_minute=500, max_resident_workspaces=3)  # three workspaces work at once

    async def go():
        vs = [await real_visitor(d, 80 + i) for i in range(3)]
        seen = _record_reconciliations(d, monkeypatch)
        with Sampler(d.data) as sampler:
            results = await asyncio.gather(*[_chain(v, f"C{i}") for i, v in enumerate(vs)])
        assert all(s == [201, 200, 201, 201, 201] for s in results), results
        assert seen and all(grown <= bound for _op, grown, bound in seen), (
            seen
        )  # every real write in its bound
        assert sampler.peak <= d.manager.budget.limit_bytes - d.manager.budget.headroom_bytes
        assert d.manager.budget.outstanding == 0 and d.manager.overshoots == 0

    run(go())


def test_a_tight_service_budget_refuses_concurrent_work_instead_of_exceeding_it(make_demo, monkeypatch):
    d = make_demo(requests_per_minute=500, max_resident_workspaces=3)  # three workspaces work at once

    async def go():
        vs = [await real_visitor(d, 90 + i) for i in range(3)]
        budget = d.manager.budget
        one = max(
            plan(
                allowances.DB_WRITE,
                d.manager.limits,
                tree_bytes(ws_of(d, v).path),
                ws_of(d, v).db_path.stat().st_size,
                allowances.sqlite_page_size(ws_of(d, v).db_path),
                0,
                {},
            ).total
            for v in vs
        )
        budget.limit_bytes = (
            d.manager.service_bytes() + budget.headroom_bytes + one + one // 2
        )  # one at a time
        seen = _record_reconciliations(d, monkeypatch)
        with Sampler(d.data) as sampler:
            results = await asyncio.gather(*[_chain(v, f"T{i}") for i, v in enumerate(vs)])
        flat = [s for chain in results for s in chain]
        assert 507 in flat and 201 in flat, results  # some work refused, some admitted
        assert all(grown <= bound for _op, grown, bound in seen)
        assert sampler.peak <= budget.limit_bytes - budget.headroom_bytes  # never beyond the budget
        assert budget.outstanding == 0

    run(go())


def test_the_budget_counts_reservations_and_temporary_files(make_demo):
    d = make_demo()

    async def go():
        v, ws, gate = await visitor_with_gate(d, 95)
        other, _ws2, gate2 = await visitor_with_gate(d, 96, block=False)
        budget = d.manager.budget
        # a bank-statement upload may be a PDF: it reserves the page-render bound on every platform (page
        # images themselves are refused where the bound is not enforced, see the Stage 2.11 tests)
        url = "/api/cases/C-0/files?filename=s.csv&kind=bank_statement"

        def need(w) -> int:
            return plan(
                allowances.UPLOAD,
                d.manager.limits,
                tree_bytes(w.path),
                w.db_path.stat().st_size,
                allowances.sqlite_page_size(w.db_path),
                100,
                {"kind": "bank_statement"},
            ).total

        first = need(ws)
        budget.limit_bytes = d.manager.service_bytes() + budget.headroom_bytes + first + 1000
        upload = asyncio.create_task(v.call("POST", url, body=b"x" * 100))
        await entered(gate)  # the upload holds its whole bound, page render included
        assert budget.reserved_bytes == first and pdf_render_bytes(d.manager.limits) < first
        r = await other.call("POST", url, body=b"y" * 100)
        assert r.status == 507 and code(r) == "demo_storage_full" and gate2.calls == 0
        gate.go.set()
        assert (await upload).status == 201
        budget.limit_bytes = d.manager.service_bytes() + budget.headroom_bytes + need(_ws2) + 1000
        temp = d.data / "temp-file.png"  # a temporary file in the measured folder counts like any other
        temp.write_bytes(b"x" * 2000)
        r = await other.call("POST", url, body=b"y" * 100)
        assert r.status == 507 and code(r) == "demo_storage_full"
        temp.unlink()
        assert (await other.call("POST", url, body=b"y" * 100)).status == 201
        assert budget.outstanding == 0

    run(go())


def test_reads_run_read_only_and_writes_get_their_cap(make_demo):
    d = make_demo()

    async def go():
        v, ws, _gate = await visitor_with_gate(d, 97, block=False)
        seen = []

        async def observe(scope, receive, send):
            await receive()
            seen.append(storage_limits.db_limit(ws.db_path))
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        ws.app = observe
        await v.call("GET", "/api/datasets")
        await v.call("POST", "/api/cases", json_body={"name": "x"})
        size = ws.db_path.stat().st_size
        assert seen[0] == (size, True)  # a read: no growth, and no writes at all
        assert seen[1][1] is False and seen[1][0] == size + d.manager.workspace_room(ws)
        assert storage_limits.db_limit(ws.db_path) is None  # cleared once the request ended

    run(go())


@pytest.mark.skipif(
    sys.platform == "win32", reason="RLIMIT_FSIZE exists on POSIX only (the demo runs on Linux)"
)
def test_the_pdf_worker_cannot_write_a_file_larger_than_its_limit(tmp_path):
    import subprocess

    script = (
        "from ledgerspy import storage_limits; assert storage_limits.apply_worker_file_limit();"
        f"open({str(tmp_path / 'big.png')!r}, 'wb').write(b'x' * 200_000)"
    )
    env = {**os.environ, storage_limits.WORKER_FILE_LIMIT_ENV: "100000", "PYTHONPATH": str(REPO_ROOT)}
    r = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, timeout=60)  # noqa: S603
    assert r.returncode != 0  # stopped by the kernel (SIGXFSZ) or the write failed
    assert (tmp_path / "big.png").stat().st_size <= 100_000


# ----------------------------------------------------------------------------- V5: the data folder


@pytest.mark.parametrize(
    "raw",
    [
        "/",
        "C:\\",
        "C:/",
        "D:",
        "\\\\server\\share",
        "\\\\server\\share\\demo",
        "//server/share/demo",
        "\\\\?\\C:\\demo",
        "/srv/demo/../../etc",
        "C:\\demo\\..\\Windows",
        "/srv/./demo",
        "relative/demo",
        "/tmp",
        "/var/tmp",
        "/home",
        "C:\\Windows",
        "C:\\Users",
        " /srv/demo",
        "",
    ],
)
def test_unsafe_data_folder_settings_are_refused(raw):
    with pytest.raises(DataDirError):
        check_data_dir_setting(raw)


@pytest.mark.parametrize(
    "raw", ["/srv/ledgerspy-demo", "/var/lib/ledgerspy-demo", "C:\\ledgerspy\\demo-data"]
)
def test_ordinary_data_folder_settings_are_accepted(raw):
    check_data_dir_setting(raw)


def test_settings_refuse_an_unsafe_data_folder(tmp_path):
    env = demo_env(tmp_path)
    env["LEDGERSPY_DEMO_DATA_DIR"] = "/"
    with pytest.raises(DataDirError):
        DemoSettings.from_env(env)


def test_a_new_or_empty_folder_is_marked_and_a_marked_folder_is_accepted(tmp_path):
    new = prepare_data_dir(str(tmp_path / "new"))
    body = json.loads((new / DATA_MARKER).read_text(encoding="utf-8"))
    assert body == {"purpose": "ledgerspy-demo-data", "path": str(new)}
    (tmp_path / "empty").mkdir()
    assert prepare_data_dir(str(tmp_path / "empty")) == (tmp_path / "empty").resolve()
    assert prepare_data_dir(str(tmp_path / "new")) == new  # again: the marker matches


def test_folders_that_are_not_demo_owned_are_refused_and_left_untouched(tmp_path):
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "precious.txt").write_text("keep")
    with pytest.raises(DataDirError, match="not empty"):
        prepare_data_dir(str(busy))
    forged = tmp_path / "forged"
    forged.mkdir()
    (forged / DATA_MARKER).write_text(json.dumps({"purpose": "something-else", "path": str(forged)}))
    with pytest.raises(DataDirError, match="does not mark"):
        prepare_data_dir(str(forged))
    copied = tmp_path / "copied"
    copied.mkdir()
    (copied / DATA_MARKER).write_text(json.dumps({"purpose": "ledgerspy-demo-data", "path": str(busy)}))
    with pytest.raises(DataDirError, match="does not mark"):
        prepare_data_dir(str(copied))
    odd = tmp_path / "odd"
    odd.mkdir()
    (odd / DATA_MARKER).mkdir()
    with pytest.raises(DataDirError):
        prepare_data_dir(str(odd))
    offline = tmp_path / "offline"
    offline.mkdir()
    (offline / "ledgerspy.db").write_bytes(b"x")
    (offline / DATA_MARKER).write_text(json.dumps({"purpose": "ledgerspy-demo-data", "path": str(offline)}))
    with pytest.raises(DataDirError, match="offline LedgerSpy database"):
        prepare_data_dir(str(offline))
    assert (busy / "precious.txt").read_text() == "keep" and (offline / "ledgerspy.db").exists()


def test_the_os_temporary_folder_home_and_repository_are_refused():
    for sensitive in (tempfile.gettempdir(), os.path.expanduser("~"), str(REPO_ROOT)):
        with pytest.raises(DataDirError):
            prepare_data_dir(sensitive)


def make_dir_link(target: Path, link: Path) -> None:
    """A junction on Windows (no privilege needed), a symbolic link elsewhere."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def test_a_data_path_through_a_link_or_junction_is_refused(tmp_path):
    real = prepare_data_dir(str(tmp_path / "real"))
    make_dir_link(real, tmp_path / "alias")
    with pytest.raises(DataDirError, match="link or junction"):
        prepare_data_dir(str(tmp_path / "alias"))
    make_dir_link(tmp_path, tmp_path / "up")
    with pytest.raises(DataDirError, match="link or junction"):
        prepare_data_dir(str(tmp_path / "up" / "real"))


def _run_folder(container: Path, name: str, marker_run: str | None = None) -> Path:
    p = container / name
    p.mkdir()
    (p / "page.png").write_bytes(b"x")
    if marker_run is not None:
        (p / RUN_MARKER).write_text(json.dumps({"purpose": "ledgerspy-demo-data", "run": marker_run}))
    return p


def test_cleanup_removes_only_earlier_runs_it_can_prove_it_owns(tmp_path, monkeypatch):
    data = prepare_data_dir(str(tmp_path / "data"))
    temp = DemoTempFiles(data)
    temp.start(set_environment=False)
    c = temp.container
    owned = _run_folder(c, "run-" + "a" * 32, "run-" + "a" * 32)
    unmarked = _run_folder(c, "run-" + "b" * 32)
    forged = _run_folder(c, "run-" + "c" * 32, "run-" + "d" * 32)
    other = _run_folder(c, "important", "important")
    (c / "notes.txt").write_text("keep")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel.txt").write_text("keep")
    (outside / RUN_MARKER).write_text(
        json.dumps({"purpose": "ledgerspy-demo-data", "run": "run-" + "e" * 32})
    )
    make_dir_link(outside, c / ("run-" + "e" * 32))  # a link dressed up as a run folder
    nested = _run_folder(c, "run-" + "f" * 32, "run-" + "f" * 32)
    make_dir_link(outside, nested / "escape")  # a link inside an owned run folder
    second = DemoTempFiles(data)
    second.start(set_environment=False)  # a new process's start-up
    assert not owned.exists() and not nested.exists() and not temp.run_dir.exists()  # earlier runs: removed
    assert unmarked.exists() and forged.exists() and other.exists() and (c / "notes.txt").exists()
    assert os.path.lexists(c / ("run-" + "e" * 32))  # the link dressed up as a run folder: kept
    assert (outside / "sentinel.txt").read_text() == "keep"  # never followed, from either link
    assert sorted(second.left_alone) == sorted(
        ["important", "notes.txt", "run-" + "b" * 32, "run-" + "c" * 32, "run-" + "e" * 32]
    )
    assert second.run_dir.exists()


def test_cleanup_refuses_a_temporary_folder_it_did_not_create(tmp_path):
    data = prepare_data_dir(str(tmp_path / "data"))
    (data / "tmp").mkdir()
    old = data / "tmp" / ("run-" + "a" * 32)
    old.mkdir()
    (old / RUN_MARKER).write_text(json.dumps({"purpose": "ledgerspy-demo-data", "run": old.name}))
    with pytest.raises(DataDirError):  # no container marker: ownership not established
        DemoTempFiles(data).start(set_environment=False)
    assert old.exists()


def test_cleanup_refuses_a_temporary_folder_that_is_a_link(tmp_path):
    data = prepare_data_dir(str(tmp_path / "data"))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / TMP_MARKER).write_text(
        json.dumps({"purpose": "ledgerspy-demo-data", "path": str(data / "tmp")})
    )
    (elsewhere / "precious.txt").write_text("keep")
    make_dir_link(elsewhere, data / "tmp")
    with pytest.raises(DataDirError):
        DemoTempFiles(data).start(set_environment=False)
    assert (elsewhere / "precious.txt").read_text() == "keep"


def test_the_demo_refuses_to_build_in_an_unsafe_folder_and_deletes_nothing(tmp_path):
    from ledgerspy_demo.app import build_demo_app

    data = tmp_path / "busy"
    (data / "frontend").mkdir(parents=True)
    (data / "frontend" / "precious.txt").write_text("keep")
    settings = DemoSettings.from_env(demo_env(data))
    with pytest.raises(DataDirError):
        build_demo_app(settings)
    assert (data / "frontend" / "precious.txt").read_text() == "keep"


def test_unclassified_operation_class_cannot_be_constructed_silently():
    # every class names how it writes files; an unknown kind fails loudly instead of reserving nothing
    with pytest.raises(KeyError):
        plan(OperationClass("x", writes_db=True, files="mystery"), LIM, 0, 0, PAGE, 0, {})
