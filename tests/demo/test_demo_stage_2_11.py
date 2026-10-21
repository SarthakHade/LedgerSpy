"""Stage 2.11: public-demo hardening.

A  anonymous visits allocate nothing; only POST /demo/workspace/new creates a workspace, under per-client,
   service-wide and capacity limits that forged proxy headers cannot escape.
B  memory / CPU bounds: two resident workspaces by default, XLSX archive limits checked before parsing, a
   column limit, one PDF worker job at a time service-wide, no page rendering where its file limit cannot be
   enforced.
C  retries are recognised by Idempotency-Key + route + body digest, never by the address alone.
D  PDF workers run in their own process tree with their own temporary folder: a timeout kills the whole tree,
   and the folder is removed only after it has exited; Tesseract gets that folder.
F  a graceful shutdown refuses new work and waits for running work, never releasing what it still holds.

Concurrency is forced with events (the `Gate` stand-in); real time is used only where a timeout is the thing
under test (the worker-tree test), never to order events.
"""

import asyncio
import ctypes
import io
import os
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

import pytest

from ledgerspy import storage_limits
from ledgerspy.paths import REPO_ROOT
from ledgerspy.pdfstatements import ocr
from ledgerspy_demo import allowances
from ledgerspy_demo.limits import MIB, DemoLimits
from ledgerspy_demo.workspace import OCR_DISABLED, tree_bytes
from tests.demo.conftest import ORIGIN, Visitor, request
from tests.demo.test_demo_concurrency_limits import code, entered, settle, visitor_with_gate

LEDGER = REPO_ROOT / "data" / "synthetic" / "reference" / "SYNTHETIC_ledger.csv"


def run(coro):
    return asyncio.run(coro)


async def create(door, octet: int, extra: dict | None = None):
    return await request(
        door,
        "POST",
        "/demo/workspace/new",
        {"origin": ORIGIN, **(extra or {})},
        client=(f"198.51.100.{octet}", 1),
    )


# ----------------------------------------------------------------------------------------- A: creation


def test_concurrent_anonymous_page_views_allocate_nothing(make_demo):
    d = make_demo()

    async def go():
        results = await asyncio.gather(
            *(request(d.door, "GET", "/", client=(f"198.51.100.{i}", 1)) for i in range(20))
        )
        assert {r.status for r in results} == {200} and all(r.header("set-cookie") is None for r in results)
        assert d.manager.workspace_ids_on_disk() == [] and d.manager.resident_ids == []
        r = await request(d.door, "GET", "/api/datasets")
        assert r.status == 401 and code(r) == "no_demo_workspace"
        assert b"W-" not in r.body

    run(go())


def test_workspace_creation_needs_a_same_origin_post(make_demo):
    d = make_demo()

    async def go():
        assert (await request(d.door, "GET", "/demo/workspace/new")).status == 404  # GET never creates
        assert (await request(d.door, "POST", "/demo/workspace/new")).status == 403  # no Origin
        evil = {"origin": "https://evil.example.org"}
        assert (await request(d.door, "POST", "/demo/workspace/new", evil)).status == 403
        cross = {"origin": ORIGIN, "sec-fetch-site": "cross-site"}
        assert (await request(d.door, "POST", "/demo/workspace/new", cross)).status == 403
        assert d.manager.workspace_ids_on_disk() == []
        assert (await create(d.door, 1)).status == 303

    run(go())


def test_the_service_wide_creation_rate_holds_under_concurrency_and_spoofing(make_demo):
    d = make_demo(new_workspaces_per_hour=3, new_workspaces_per_hour_per_client=3, max_resident_workspaces=2)

    async def go():
        # eight different clients (and forged forwarding headers) at once: the service-wide cap still holds
        results = await asyncio.gather(
            *(create(d.door, 10 + i, {"x-forwarded-for": f"203.0.113.{i}"}) for i in range(8))
        )
        statuses = sorted(r.status for r in results)
        assert statuses.count(303) == 3 and statuses.count(429) == 5, statuses
        assert {code(r) for r in results if r.status == 429} == {"demo_creation_limit"}
        assert len(d.manager.workspace_ids_on_disk()) == 3
        assert all(r.header("retry-after") for r in results if r.status == 429)

    run(go())


def test_the_active_workspace_cap_holds_under_concurrency(make_demo):
    d = make_demo(max_active_workspaces=4, max_resident_workspaces=2, new_workspaces_per_hour=50)

    async def go():
        results = await asyncio.gather(*(create(d.door, 30 + i) for i in range(10)))
        created = [r for r in results if r.status == 303]
        assert len(created) <= 4 and len(d.manager.workspace_ids_on_disk()) == len(created)
        assert {code(r) for r in results if r.status != 303} <= {"demo_full", "demo_busy"}
        assert len(d.manager.resident_ids) <= 2

    run(go())


def test_creation_responses_never_reveal_a_workspace_id(make_demo):
    d = make_demo()

    async def go():
        r = await create(d.door, 40)
        wid = r.header("set-cookie").split("=", 1)[1].split(".")[0]
        assert r.header("location") == "/" and wid not in r.body.decode() and wid not in r.header("location")
        v = Visitor(d.door, cookie=r.header("set-cookie").split(";")[0].split("=", 1)[1])
        d.manager._close(d.manager._resident[wid])
        os.rename(d.manager.root / wid, d.manager.root / f"{wid}-gone")
        r = await v.call("GET", "/api/datasets")  # an unusable workspace: reported without its ID
        assert r.status == 409 and wid not in r.body.decode()

    run(go())


# ----------------------------------------------------------------------------------------- B: limits


def test_default_limits_suit_a_small_host():
    lim = DemoLimits()
    assert lim.max_resident_workspaces == 2 and lim.max_concurrent_pdf_jobs == 1
    assert lim.xlsx_max_uncompressed_bytes == 16 * MIB and lim.xlsx_max_zip_entries == 100
    assert lim.max_columns == 64 and lim.new_workspaces_per_hour == 20


def test_the_workspace_app_gets_the_stricter_archive_and_column_limits(make_demo):
    d = make_demo()
    s = d.manager.config_for(d.data).settings
    assert s.ingest.max_columns == 64 and s.ingest.xlsx.max_uncompressed_bytes == 16 * MIB
    assert s.ingest.xlsx.max_zip_entries == 100 and s.pdf.render.max_pixels == 12_000_000
    if allowances.RENDER_LIMIT_ENFORCED:
        assert OCR_DISABLED not in s.pdf.ocr.executable_candidates
    else:  # Windows: no OCR, so no page is rendered for it
        assert s.pdf.ocr.executable_candidates == [OCR_DISABLED]
        assert ocr.discover(s.pdf.ocr.model_dump(mode="json")).available is False


def _xlsx(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


@pytest.mark.parametrize(
    "entries, expected",
    [
        ({"[Content_Types].xml": b"<x/>", "xl/worksheets/sheet1.xml": b"\0" * (17 * MIB)}, "xlsx_zip_bomb"),
        ({f"xl/p{i}.xml": b"<x/>" for i in range(120)}, "xlsx_too_many_entries"),
    ],
)
def test_xlsx_archives_are_refused_before_parsing(make_demo, entries, expected):
    d = make_demo()
    body = _xlsx(entries)
    assert len(body) < d.settings.limits.max_upload_bytes  # small on the wire: the expansion is the attack

    async def go():
        v = Visitor(d.door)
        await v.open()
        case = (await v.call("GET", "/api/datasets")).json()["items"][0]["case_id"]
        ws = d.manager._resident[v.cookie.split(".")[0]]
        before = tree_bytes(ws.path)
        r = await v.call("POST", f"/api/cases/{case}/files?filename=book.xlsx", body=body)
        assert r.status >= 400 and code(r) == expected, r.body[:300]
        assert b"Traceback" not in r.body and str(ws.path).encode() not in r.body
        assert tree_bytes(ws.path) - before < 64 * 1024  # nothing kept but bookkeeping

    run(go())


def test_too_many_columns_are_refused(make_demo):
    d = make_demo()
    header = ",".join(f"c{i}" for i in range(70))
    body = (header + "\n" + ",".join("1" for _ in range(70)) + "\n").encode()

    async def go():
        v = Visitor(d.door)
        await v.open()
        case = (await v.call("GET", "/api/datasets")).json()["items"][0]["case_id"]
        r = await v.call("POST", f"/api/cases/{case}/files?filename=wide.csv", body=body)
        assert r.status >= 400 and code(r) == "too_many_columns"

    run(go())


def test_pdf_jobs_share_one_service_wide_slot(make_demo):
    d = make_demo()
    url = "/api/cases/C-0/files?filename=s.pdf&kind=bank_statement"

    async def go():
        a, ws_a, gate_a = await visitor_with_gate(d, 50)
        b, ws_b, gate_b = await visitor_with_gate(d, 51, block=False)
        first = asyncio.create_task(a.call("POST", url, body=b"x" * 100))
        await entered(gate_a)  # A's PDF job holds the only slot
        d.door.lock_wait_seconds = 0.05  # B gives up waiting for the slot (the slot stays held meanwhile)
        r = await b.call("POST", url, body=b"y" * 100)
        assert r.status == 503 and code(r) == "pdf_busy" and gate_b.calls == 0
        assert d.manager.budget.outstanding == 1  # B's reservation was released with its refusal
        plain = await b.call("POST", "/api/cases/C-0/files?filename=l.csv", body=b"z" * 100)
        assert plain.status == 201  # work without the PDF worker is not held up
        gate_a.go.set()
        assert (await first).status == 201
        assert (await b.call("POST", url, body=b"y" * 100)).status == 201
        assert d.manager.budget.outstanding == 0 and not ws_a.lock.locked() and not ws_b.lock.locked()

    run(go())


@pytest.mark.skipif(allowances.RENDER_LIMIT_ENFORCED, reason="Windows-only: rendering is refused there")
def test_page_images_are_refused_where_the_render_limit_is_not_enforced(make_demo):
    d = make_demo()

    async def go():
        v, _ws, gate = await visitor_with_gate(d, 52, block=False)
        r = await v.call("GET", "/api/datasets/D-0/pdf/pages/1/image")
        assert r.status == 403 and code(r) == "not_available_on_this_platform" and gate.calls == 0

    run(go())


# ----------------------------------------------------------------------------------------- C: idempotency


def test_idempotency_keys_tie_a_result_to_one_route_and_body(make_demo):
    d = make_demo(max_uploads_per_workspace=20, uploads_per_hour=50)

    async def go():
        v, ws, gate = await visitor_with_gate(d, 60, block=False)
        k = {"idempotency-key": "key-aaaaaaaa"}
        first = await v.call(
            "POST", "/api/cases/C-0/files?filename=a.csv&kind=ledger", body=b"x" * 500, headers=k
        )
        assert first.status == 201 and gate.calls == 1
        # the same request, query order differing: replayed, not run again
        same = await v.call(
            "POST", "/api/cases/C-0/files?kind=ledger&filename=a.csv", body=b"x" * 500, headers=k
        )
        assert same.status == 201 and same.header("idempotent-replayed") == "true" and gate.calls == 1
        assert d.manager.uploads_so_far(ws) == 1
        other_body = await v.call(
            "POST", "/api/cases/C-0/files?filename=a.csv&kind=ledger", body=b"y" * 500, headers=k
        )
        other_route = await v.call("POST", "/api/cases/C-0/files?filename=b.csv", body=b"x" * 500, headers=k)
        assert code(other_body) == code(other_route) == "idempotency_key_reused" and gate.calls == 1
        assert (await v.call("POST", "/api/cases", json_body={"n": 1}, headers=k)).status == 409
        fresh = await v.call("POST", "/api/cases/C-0/files?filename=a.csv", body=b"x" * 500,
                             headers={"idempotency-key": "key-bbbbbbbb"})  # fmt: skip
        assert fresh.status == 201 and gate.calls == 2  # a different key is a different request
        for bad in ("short", "x" * 65, "has space!", "ünïcode-key"):
            r = await v.call("POST", "/api/cases", json_body={}, headers={"idempotency-key": bad})
            assert r.status == 400 and code(r) == "invalid_idempotency_key"
        assert gate.calls == 2

    run(go())


def test_idempotency_keys_are_per_workspace_and_expire(make_demo):
    d = make_demo(max_uploads_per_workspace=20, uploads_per_hour=50)

    async def go():
        a, _ws_a, gate_a = await visitor_with_gate(d, 61, block=False)
        b, _ws_b, gate_b = await visitor_with_gate(d, 62, block=False)
        k = {"idempotency-key": "shared-key-1"}
        assert (await a.call("POST", "/api/cases", json_body={"n": 1}, headers=k)).status == 201
        assert (await b.call("POST", "/api/cases", json_body={"n": 2}, headers=k)).status == 201  # B's own
        assert gate_a.calls == 1 and gate_b.calls == 1
        d.clock.now += d.settings.limits.idempotency_ttl_seconds + 1
        r = await a.call("POST", "/api/cases", json_body={"n": 3}, headers=k)  # expired: a new request
        assert r.status == 201 and r.header("idempotent-replayed") is None and gate_a.calls == 2

    run(go())


def test_the_idempotency_registry_is_bounded(make_demo):
    d = make_demo(requests_per_minute=500)

    async def go():
        v, ws, _gate = await visitor_with_gate(d, 63, block=False)
        from ledgerspy_demo.frontdoor import IDEMPOTENCY_MAX_ENTRIES

        for i in range(IDEMPOTENCY_MAX_ENTRIES + 5):
            r = await v.call(
                "POST", "/api/cases", json_body={"i": i}, headers={"idempotency-key": f"bounded-{i:04d}"}
            )
            assert r.status == 201
        reg = ws.activity.idempotency
        assert len(reg) == IDEMPOTENCY_MAX_ENTRIES and "bounded-0000" not in reg  # the oldest went first
        assert f"bounded-{IDEMPOTENCY_MAX_ENTRIES + 4:04d}" in reg

    run(go())


def test_concurrent_retries_with_one_key_run_once(make_demo):
    d = make_demo()

    async def go():
        v, _ws, gate = await visitor_with_gate(d, 64)
        k = {"idempotency-key": "concurrent-1"}
        first = asyncio.create_task(v.call("POST", "/api/cases", json_body={"n": 1}, headers=k))
        await entered(gate)
        retries = await asyncio.gather(
            *(v.call("POST", "/api/cases", json_body={"n": 1}, headers=k) for _ in range(5))
        )
        assert {code(r) for r in retries} == {"idempotent_request_in_progress"} and gate.calls == 1
        gate.go.set()
        assert (await first).status == 201
        replay = await v.call("POST", "/api/cases", json_body={"n": 1}, headers=k)
        assert replay.status == 201 and replay.header("idempotent-replayed") == "true" and gate.calls == 1

    run(go())


def test_a_large_upload_is_fingerprinted_while_it_streams(make_demo):
    d = make_demo()

    async def go():
        v, _ws, gate = await visitor_with_gate(d, 65, block=False)
        body = (LEDGER.read_bytes() * 2)[: d.settings.limits.max_upload_bytes]
        k = {"idempotency-key": "large-upload-1"}
        assert (
            await v.call("POST", "/api/cases/C-0/files?filename=big.csv", body=body, headers=k)
        ).status == 201
        replay = await v.call("POST", "/api/cases/C-0/files?filename=big.csv", body=body, headers=k)
        assert replay.header("idempotent-replayed") == "true" and gate.calls == 1
        changed = body[:-1] + b"#"
        r = await v.call("POST", "/api/cases/C-0/files?filename=big.csv", body=changed, headers=k)
        assert code(r) == "idempotency_key_reused" and gate.calls == 1

    run(go())


# ----------------------------------------------------------------------------------------- D: worker trees

CHILD = textwrap.dedent(
    """
    import os, subprocess, sys, time
    # writes into the temporary folder it was given, starts a grandchild, and never finishes on its own
    open(os.path.join(os.environ["TMPDIR"], "work.png"), "wb").write(b"x" * 1000)
    g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    open(sys.argv[1], "w").write(f"{os.getpid()} {g.pid} {os.environ['TMPDIR']}")
    time.sleep(120)
    """
)


def _alive(pid: int) -> bool:
    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x00100000 | 0x1000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED_INFO
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) != 0  # WAIT_OBJECT_0: it has exited
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    with open(f"/proc/{pid}/stat") if os.path.exists(f"/proc/{pid}/stat") else open(os.devnull) as f:
        return " Z " not in f.read()  # a zombie has exited


@pytest.fixture
def worker_root(tmp_path):
    root = tmp_path / "run" / "workers"
    root.mkdir(parents=True)
    storage_limits.set_worker_temp_root(root)
    yield root
    storage_limits.set_worker_temp_root(None)


def test_a_timed_out_worker_tree_is_killed_before_its_folder_is_removed(tmp_path, worker_root):
    pids = tmp_path / "pids.txt"
    with pytest.raises(subprocess.TimeoutExpired):
        storage_limits.run_worker(
            [sys.executable, "-c", CHILD, str(pids)], "", 5, dict(os.environ), str(tmp_path)
        )
    worker, grandchild, work = pids.read_text().split()
    assert Path(work).parent == worker_root  # its own folder, inside the validated root
    assert not _alive(int(worker)) and not _alive(int(grandchild))  # the whole tree is gone
    assert not Path(work).exists() and list(worker_root.iterdir()) == []  # then the folder was removed
    assert storage_limits.live_workers() == 0


def test_a_finished_or_failed_worker_leaves_no_folder(tmp_path, worker_root):
    ok = storage_limits.run_worker(
        [sys.executable, "-c", "import os,sys; print(os.environ['TMPDIR']); sys.stdout.flush()"],
        "", 60, dict(os.environ), str(tmp_path),
    )  # fmt: skip
    assert ok.returncode == 0 and Path(ok.stdout.strip()).parent == worker_root
    failed = storage_limits.run_worker(
        [sys.executable, "-c", "import sys; sys.exit(3)"], "", 60, dict(os.environ), str(tmp_path)
    )
    assert failed.returncode == 3 and list(worker_root.iterdir()) == []


def test_the_worker_policy_is_off_offline_and_tesseract_gets_the_worker_folder(tmp_path, monkeypatch):
    assert storage_limits.worker_temp_root() is None  # nothing enables it offline
    with pytest.raises(RuntimeError):
        storage_limits.run_worker([sys.executable, "-c", "pass"], "", 5, {}, str(tmp_path))
    monkeypatch.delenv(storage_limits.WORKER_TEMP_ENV, raising=False)
    offline = ocr._env()
    assert "TMPDIR" not in offline  # exactly as before: Tesseract never got TMPDIR offline
    assert offline.get("TEMP") == os.environ.get("TEMP") and storage_limits.WORKER_TEMP_ENV not in offline
    monkeypatch.setenv(storage_limits.WORKER_TEMP_ENV, str(tmp_path))
    env = ocr._env()
    assert env["TMPDIR"] == env["TEMP"] == env["TMP"] == str(tmp_path)
    monkeypatch.setenv(storage_limits.WORKER_TEMP_ENV, "relative/not/allowed")
    assert "TMPDIR" not in ocr._env()  # only an absolute, existing folder is passed on


def test_shutdown_kills_running_worker_trees(tmp_path, worker_root):
    pids = tmp_path / "pids.txt"
    errors = []

    def target():
        try:
            storage_limits.run_worker(
                [sys.executable, "-c", CHILD, str(pids)], "", 120, dict(os.environ), "/"
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    import threading

    t = threading.Thread(target=target)
    t.start()
    for _ in range(600):
        if pids.exists() and pids.read_text().strip():
            break
        t.join(0.1)
    worker, grandchild, _work = pids.read_text().split()
    assert storage_limits.kill_all_workers() == 1
    t.join(60)
    assert not t.is_alive() and not _alive(int(worker)) and not _alive(int(grandchild))
    assert list(worker_root.iterdir()) == []


# ----------------------------------------------------------------------------------------- F: shutdown


def test_shutdown_refuses_new_work_and_never_releases_running_work(make_demo):
    d = make_demo()

    async def go():
        v, ws, gate = await visitor_with_gate(d, 70)
        long = asyncio.create_task(v.call("POST", "/api/cases", json_body={"n": 1}))
        await entered(gate)
        report = await d.door.shutdown(grace=0.05, after_kill=0.05)  # the work does not finish in time
        assert report == {"running_at_start": 1, "killed_pdf_workers": 0, "still_running": 1}
        assert ws.lock.locked() and d.manager.budget.outstanding == 1  # still held by the running work
        r = await request(d.door, "GET", "/")
        assert r.status == 503 and code(r) == "shutting_down"
        r = await v.call("GET", "/api/datasets")
        assert r.status == 503 and code(r) == "shutting_down"
        gate.go.set()
        assert (await long).status == 201  # the running request completes normally
        await settle(lambda: d.door.jobs == 0)
        assert not ws.lock.locked() and d.manager.budget.outstanding == 0

    run(go())


def test_shutdown_returns_as_soon_as_running_work_ends(make_demo):
    d = make_demo()

    async def go():
        v, _ws, gate = await visitor_with_gate(d, 71)
        long = asyncio.create_task(v.call("POST", "/api/cases", json_body={"n": 1}))
        await entered(gate)
        stopping = asyncio.create_task(d.door.shutdown(grace=60))
        await settle(lambda: d.door.closing)
        gate.go.set()
        report = await asyncio.wait_for(stopping, timeout=30)  # returns on idle, long before the grace ends
        assert report["still_running"] == 0 and report["killed_pdf_workers"] == 0
        assert (await long).status == 201

    run(go())
