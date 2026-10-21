"""Stage 2.6: the demo's limits under concurrency, and truthful processing timeouts (H1 and M3 of the review).

Every interleaving here is forced with events, never with sleeps: the stand-in workspace app (`Gate`) blocks
until the test opens it, and `settle` only lets the event loop run until a condition holds (bounded rounds,
no wall-clock time). Processing timeouts are made short only while the inner work is held closed, so the
timeout always fires first.
"""

import asyncio
import dataclasses
import itertools
import threading

from ledgerspy_demo import allowances
from ledgerspy_demo.budget import StorageBudget
from ledgerspy_demo.workspace import tree_bytes
from tests.demo.conftest import Visitor

_names = itertools.count(1)


def run(coro):
    return asyncio.run(coro)


async def settle(condition, rounds: int = 1000) -> None:
    """Let other tasks and callbacks run until `condition()` holds (deterministic: no clock involved)."""
    for _ in range(rounds):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not reached")


class Gate:
    """Stands in for a workspace app. Each call reads its body, waits until `go` is set, writes the body into
    the workspace (so storage really grows), then answers `status` (or raises when `fail`)."""

    def __init__(self, ws, status: int = 201, fail: bool = False, block: bool = True) -> None:
        self.ws, self.status, self.fail = ws, status, fail
        self.go = asyncio.Event()
        if not block:
            self.go.set()
        self.entered = asyncio.Event()
        self.calls = self.done = self.active = self.max_active = 0

    async def __call__(self, scope, receive, send):
        body = (await receive()).get("body", b"")
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered.set()
        try:
            await self.go.wait()
            if body:
                (self.ws.path / f"gate-{next(_names)}.bin").write_bytes(body)
            if self.fail:
                raise RuntimeError("inner failure")
            await send(
                {
                    "type": "http.response.start",
                    "status": self.status,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send({"type": "http.response.body", "body": b"done"})
        finally:
            self.active -= 1
            self.done += 1


async def visitor_with_gate(d, last_octet: int, **gate):
    v = Visitor(d.door, client=(f"198.51.100.{last_octet}", 1))
    assert (await v.open()).status == 200
    ws = d.manager._resident[v.cookie.split(".")[0]]
    ws.app = Gate(ws, **gate)
    return v, ws, ws.app


def with_limits(d, **changes) -> None:
    """Set a limit once the workspace exists (its exact size is known only then); validated as usual."""
    d.manager.limits = d.door.limits = dataclasses.replace(d.manager.limits, **changes)


def upload(v: Visitor, body: bytes, name: str = "f.csv", headers: dict | None = None):
    return v.call("POST", f"/api/cases/C-0/files?filename={name}", body=body, headers=headers)


def code(r) -> str | None:
    return r.json()["error"]["code"] if r.status >= 400 else None


def upload_plan(d, ws, nbytes: int) -> int:
    """The reservation the production admission computes for an upload of `nbytes` into `ws` right now."""
    return allowances.plan(
        allowances.UPLOAD,
        d.manager.limits,
        ws_bytes=tree_bytes(ws.path),
        db_bytes=ws.db_path.stat().st_size,
        page_size=allowances.sqlite_page_size(ws.db_path),
        declared_bytes=nbytes,
        query={},
    ).total


async def entered(gate) -> None:
    """Wait until the gate is reached; the bound only turns a refused request into a failure, not a hang."""
    await asyncio.wait_for(gate.entered.wait(), timeout=30)


def assert_released(d, *workspaces) -> None:
    assert d.manager.budget.outstanding == 0 and d.manager.budget.reserved_bytes == 0
    for ws in workspaces:
        assert not ws.lock.locked()


# ------------------------------------------------------------------------------------- H1: concurrent limits


def test_concurrent_uploads_cannot_exceed_the_upload_count(make_demo):
    d = make_demo(max_uploads_per_workspace=2)

    async def go():
        v, ws, gate = await visitor_with_gate(d, 10, block=False)
        results = await asyncio.gather(*[upload(v, b"x" * 100, f"f{i}.csv") for i in range(6)])
        assert sorted(r.status for r in results) == [201, 201, 413, 413, 413, 413]
        assert {code(r) for r in results if r.status == 413} == {"upload_limit"}
        assert d.manager.uploads_so_far(ws) == 2 and gate.calls == 2 and gate.max_active == 1
        assert_released(d, ws)

    run(go())


def test_concurrent_uploads_cannot_exceed_the_hourly_upload_limit(make_demo):
    d = make_demo(uploads_per_hour=2, max_uploads_per_workspace=10)

    async def go():
        v, ws, gate = await visitor_with_gate(d, 11, block=False)
        results = await asyncio.gather(*[upload(v, b"x" * 100, f"f{i}.csv") for i in range(5)])
        assert sorted(r.status for r in results) == [201, 201, 429, 429, 429]
        assert {code(r) for r in results if r.status == 429} == {"upload_rate_limited"}
        assert len(ws.uploads) == 2 and gate.calls == 2
        assert_released(d, ws)

    run(go())


def test_concurrent_uploads_cannot_exceed_the_workspace_quota(make_demo):
    d = make_demo()

    async def go():
        v, ws, gate = await visitor_with_gate(d, 12, block=False)
        size = 1000  # each upload writes what it declared
        quota = tree_bytes(ws.path) + 3 * size + size // 2  # exactly three uploads fit
        with_limits(d, max_workspace_bytes=quota)
        results = await asyncio.gather(*[upload(v, b"x" * size, f"f{i}.csv") for i in range(8)])
        assert sorted(r.status for r in results) == [201] * 3 + [413] * 5
        assert {code(r) for r in results if r.status == 413} == {"workspace_quota_exceeded"}
        assert tree_bytes(ws.path) <= quota  # measured after the writes, not only declared
        assert_released(d, ws)

    run(go())


def test_concurrent_writes_in_different_workspaces_cannot_exceed_the_service_budget(make_demo):
    d = make_demo()

    async def go():
        a, ws_a, gate_a = await visitor_with_gate(d, 13)
        b, ws_b, gate_b = await visitor_with_gate(d, 14, block=False)
        need_a, need_b = upload_plan(d, ws_a, 100), upload_plan(d, ws_b, 100)
        assert need_a == need_b  # two identical seed copies
        budget = d.manager.budget
        # room for exactly one in-flight upload (its full bound), not two, and not a new seed copy either
        budget.limit_bytes = d.manager.service_bytes() + budget.headroom_bytes + need_a + 1024 * 1024
        first = asyncio.create_task(upload(a, b"a" * 100))
        await entered(gate_a)  # A is admitted and holds its reservation while it works
        assert budget.outstanding == 1 and budget.reserved_bytes == need_a
        r = await upload(b, b"b" * 100)
        assert r.status == 507 and code(r) == "demo_storage_full" and gate_b.calls == 0
        r = await Visitor(d.door, client=("198.51.100.15", 1)).open()  # a new workspace does not fit either
        assert r.status == 507 and code(r) == "demo_storage_full"
        gate_a.go.set()
        assert (await first).status == 201
        assert budget.outstanding == 0
        assert (await upload(b, b"b" * 100)).status == 201  # A's reservation was released
        assert_released(d, ws_a, ws_b)

    run(go())


def test_storage_budget_reservations_are_atomic_across_threads():
    budget = StorageBudget(limit_bytes=1000, headroom_bytes=100, measure=lambda: 400)
    barrier = threading.Barrier(24)
    granted = []

    def worker():
        barrier.wait()  # all threads reserve at the same moment
        res = budget.reserve(50, "thread")
        if res is not None:
            granted.append(res)

    threads = [threading.Thread(target=worker) for _ in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(granted) == 10 and budget.reserved_bytes == 500  # 400 used + 500 + 100 head-room = 1000
    granted[0].release()
    granted[0].release()  # idempotent: released once only
    assert budget.outstanding == 9 and budget.reserve(51, "too big") is None
    assert budget.reserve(50, "fits again") is not None


# -------------------------------------------------------------------------- release on every path


def test_reservations_are_released_after_failed_refused_and_cancelled_requests(make_demo):
    d = make_demo(max_uploads_per_workspace=5)

    async def go():
        v, ws, gate = await visitor_with_gate(d, 20, block=False)
        gate.status = 422  # the workspace app rejects the upload: not counted, nothing held
        assert (await upload(v, b"x" * 100, "bad.csv")).status == 422
        assert d.manager.uploads_so_far(ws) == 0
        assert_released(d, ws)

        gate.status, gate.fail = 201, True  # the workspace app raises
        try:
            await upload(v, b"x" * 100, "boom.csv")
            raise AssertionError("the inner exception should propagate")
        except RuntimeError:
            pass
        assert d.manager.uploads_so_far(ws) == 0
        assert_released(d, ws)

        gate.fail = False
        r = await upload(v, b"x" * (d.settings.limits.max_upload_bytes + 1), "huge.csv")  # refused up front
        assert r.status == 413 and code(r) == "file_too_large" and gate.calls == 2
        assert_released(d, ws)

        gate.go.clear()  # cancelled while the inner work runs: it cannot be stopped, so it keeps its hold
        gate.entered.clear()
        task = asyncio.create_task(upload(v, b"x" * 100, "cancelled.csv"))
        await entered(gate)
        task.cancel()
        await settle(task.done)
        assert d.manager.budget.outstanding == 1 and ws.lock.locked()
        gate.go.set()
        await settle(lambda: d.manager.budget.outstanding == 0)
        assert_released(d, ws)
        assert d.manager.uploads_so_far(ws) == 1  # it completed, so it counts
        assert ws.activity.pending is not None and ws.activity.pending.status == 201

    run(go())


# ------------------------------------------------------------------------------------ M3: truthful timeouts


def test_a_timed_out_upload_keeps_its_hold_until_it_ends_and_is_then_counted(make_demo):
    d = make_demo(max_uploads_per_workspace=5)

    async def go():
        v, ws, gate = await visitor_with_gate(d, 30)
        other, ws_other, _ = await visitor_with_gate(d, 31, block=False)
        d.door.processing_timeout = 0.01  # the gate stays closed, so the timeout always fires first
        r = await upload(v, b"x" * 100, "slow.csv")
        assert r.status == 504 and code(r) == "still_processing"
        assert r.json()["error"]["details"] == {"may_complete": True}
        assert "may still complete" in r.json()["error"]["message"] and "Retry-After".lower() not in dict(
            r.headers
        )
        # the work goes on writing: its lock and reservation are still held, and the workspace is not idle
        assert ws.lock.locked() and d.manager.budget.outstanding == 1 and not d.manager._idle(ws)
        assert d.manager.uploads_so_far(ws) == 0
        # a new state-changing request is refused at once, without starting anything
        r = await upload(v, b"y" * 100, "other.csv")
        assert r.status == 409 and code(r) == "previous_request_still_processing" and gate.calls == 1
        # other workspaces are unaffected
        assert (await upload(other, b"z" * 100)).status == 201
        gate.go.set()
        await settle(lambda: not ws.lock.locked())
        assert_released(d, ws, ws_other)
        assert d.manager.uploads_so_far(ws) == 1 and len(ws.uploads) == 1  # the eventual success is counted
        assert ws.activity.pending.done and ws.activity.pending.status == 201

    run(go())


def test_a_retry_after_a_timeout_never_processes_or_counts_twice(make_demo):
    """Stage 2.11 (V2): a retry is recognised by its Idempotency-Key, route and body, never by its address
    alone; without a key, a different request to the same address is processed normally."""
    d = make_demo(max_uploads_per_workspace=5)

    async def go():
        v, ws, gate = await visitor_with_gate(d, 40)
        key = {"idempotency-key": "retry-key-0001"}
        d.door.processing_timeout = 0.01
        assert (await upload(v, b"x" * 100, "same.csv", key)).status == 504
        retry = await upload(v, b"x" * 100, "same.csv", key)  # retried while it still runs
        assert retry.status == 409 and code(retry) == "idempotent_request_in_progress"
        other = await upload(
            v, b"y" * 100, "same.csv"
        )  # any other change meanwhile: refused, nothing started
        assert other.status == 409 and code(other) == "previous_request_still_processing"
        gate.go.set()
        await settle(lambda: not ws.lock.locked())
        d.door.processing_timeout = float(d.settings.limits.max_processing_seconds)
        retry = await upload(v, b"x" * 100, "same.csv", key)  # retried after it completed: its result
        assert retry.status == 201 and retry.body == b"done" and retry.header("idempotent-replayed") == "true"
        assert gate.calls == 1 and d.manager.uploads_so_far(ws) == 1  # processed and counted once
        reused = await upload(v, b"z" * 100, "same.csv", key)  # the same key for a different body
        assert reused.status == 409 and code(reused) == "idempotency_key_reused" and gate.calls == 1
        again = await upload(v, b"x" * 100, "same.csv")  # the same content without a key: a new request
        assert again.status == 201 and gate.calls == 2 and d.manager.uploads_so_far(ws) == 2
        assert_released(d, ws)

    run(go())


def test_a_timed_out_read_is_reported_as_discarded_and_needs_no_pending_record(make_demo):
    d = make_demo()

    async def go():
        v, ws, gate = await visitor_with_gate(d, 45)
        d.door.processing_timeout = 0.01
        r = await v.call("GET", "/api/datasets")
        assert r.status == 504 and code(r) == "processing_timeout" and ws.activity.pending is None
        assert ws.lock.locked() and d.manager.budget.outstanding == 0  # reads hold no reservation
        gate.go.set()
        await settle(lambda: not ws.lock.locked())
        assert_released(d, ws)

    run(go())


# ------------------------------------------------------------------------------- deadlocks and cleanup


def test_mixed_concurrent_load_finishes_without_deadlock_and_leaves_nothing_held(make_demo):
    # six workspaces in use at once: the resident bound is raised for this test (default 2)
    d = make_demo(
        max_uploads_per_workspace=3,
        requests_per_minute=500,
        uploads_per_hour=500,
        max_resident_workspaces=6,
    )

    async def go():
        visitors = [await visitor_with_gate(d, 50 + i, block=False) for i in range(3)]
        calls = []
        for i, (v, _ws, _gate) in enumerate(visitors):
            calls += [upload(v, b"x" * 100, f"f{n}.csv") for n in range(5)]
            calls += [v.call("POST", "/api/cases", json_body={"name": f"c{i}"}) for _ in range(3)]
            calls += [v.call("GET", "/api/datasets") for _ in range(3)]
        calls += [Visitor(d.door, client=(f"198.51.100.{60 + i}", 1)).open() for i in range(3)]
        # a deadlock would never finish: the bound is a detector, the work itself takes milliseconds
        results = await asyncio.wait_for(asyncio.gather(*calls), timeout=60)
        assert all(r.status in (200, 201, 413) for r in results)
        for _v, ws, gate in visitors:
            assert d.manager.uploads_so_far(ws) == 3 and gate.max_active == 1 and ws.leases == 0
        assert_released(d, *(ws for _v, ws, _g in visitors))

    run(go())


def test_temporary_files_are_kept_in_the_measured_data_folder(make_demo, monkeypatch):
    import os
    import tempfile
    from pathlib import Path

    from ledgerspy_demo.datadir import DemoTempFiles, prepare_data_dir

    d = make_demo()
    for name in ("TMPDIR", "TEMP", "TMP"):  # restored after the test (also when absent before)
        monkeypatch.setenv(name, os.environ.get(name, ""))
    monkeypatch.setattr(tempfile, "tempdir", None)
    data = prepare_data_dir(str(d.data / "owned"))  # a demo-owned folder inside the measured data folder
    run = DemoTempFiles(data).start()
    before = d.manager.service_bytes()
    with tempfile.TemporaryDirectory(prefix="ls-ocr-") as t:  # as the OCR step and page renders create them
        assert Path(t).resolve().parent == run.resolve()
        (Path(t) / "page.png").write_bytes(b"x" * 5000)
        assert d.manager.service_bytes() >= before + 5000  # counted by every budget admission
